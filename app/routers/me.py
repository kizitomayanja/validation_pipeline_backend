import uuid
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from ..db import get_conn
from ..deps import current_user
from ..schemas import BalanceOut, MethodIn, MethodOut, UserOut

router = APIRouter(prefix="/me", tags=["me"])
MAX_METHODS = 5


@router.get("", response_model=UserOut)
def me(user: dict = Depends(current_user)):
    return user


@router.get("/balance", response_model=BalanceOut)
def balance(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    query = """
        with rs_cfg as (
            select coalesce(min_cashout_ugx, 5000) as min_cashout_ugx
            from (select min_cashout_ugx from reward_settings limit 1) sub
            union all
            select 5000 as min_cashout_ugx
            limit 1
        ),
        stats as (
            select
                coalesce(sum(s.reward_ugx) filter (where s.verified = true), 0)::int as total_earned_ugx,
                coalesce(sum(s.reward_ugx) filter (where s.verified = true and s.payout_id is null), 0)::int as available_ugx,
                coalesce(sum(s.reward_ugx) filter (where po.status = 'pending'), 0)::int as pending_ugx,
                coalesce(sum(s.reward_ugx) filter (where po.status = 'paid'), 0)::int as paid_out_ugx,
                count(s.id) filter (where s.validated_by is not null)::int as validated_count,
                count(s.id) filter (where s.verified = true)::int as verified_count,
                count(s.id) filter (where s.validated_by is not null and s.verified = false)::int as pending_review_count,
                coalesce(sum(s.reward_ugx) filter (where s.validated_by is not null and s.verified = false), 0)::int as pending_review_ugx
            from sentences s
            left join payouts po on po.id = s.payout_id
            where s.validated_by = %(uid)s
        )
        select
            coalesce(stats.total_earned_ugx, 0) as total_earned_ugx,
            coalesce(stats.available_ugx, 0) as available_ugx,
            coalesce(stats.pending_ugx, 0) as pending_ugx,
            coalesce(stats.paid_out_ugx, 0) as paid_out_ugx,
            coalesce(stats.validated_count, 0) as validated_count,
            coalesce(stats.verified_count, 0) as verified_count,
            coalesce(stats.pending_review_count, 0) as pending_review_count,
            coalesce(stats.pending_review_ugx, 0) as pending_review_ugx,
            rs_cfg.min_cashout_ugx
        from rs_cfg
        cross join stats
    """
    try:
        row = conn.execute(query, {"uid": user["id"]}).fetchone()
        if row:
            return row
    except Exception:
        pass

    return BalanceOut(
        total_earned_ugx=0,
        available_ugx=0,
        pending_ugx=0,
        paid_out_ugx=0,
        validated_count=0,
        verified_count=0,
        pending_review_count=0,
        pending_review_ugx=0,
        min_cashout_ugx=5000,
    )


_METHOD_COLS = "id, provider, phone_number, account_name, is_default"


@router.get("/payment-methods", response_model=list[MethodOut])
def list_methods(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    try:
        return conn.execute(
            f"select {_METHOD_COLS} from payment_methods where user_id = %s and deleted_at is null order by created_at",
            (user["id"],),
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        return []


@router.post("/payment-methods", response_model=MethodOut, status_code=201)
def add_method(body: MethodIn, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    conn.execute("select id from profiles where id = %s for update", (user["id"],))   # serialise concurrent adds
    count = conn.execute("select count(*) as n from payment_methods where user_id = %s and deleted_at is null", (user["id"],)).fetchone()["n"]
    if count >= MAX_METHODS:
        raise HTTPException(400, f"You can save up to {MAX_METHODS} mobile money accounts. Remove one first.")
    try:
        return conn.execute(
            f"""insert into payment_methods (user_id, provider, phone_number, account_name, is_default)
                values (%s, %s, %s, %s, %s) returning {_METHOD_COLS}""",
            (user["id"], body.provider, body.phone_number, body.account_name, count == 0)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "That number is already saved on your account.")


@router.delete("/payment-methods/{method_id}", status_code=204)
def delete_method(method_id: str, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    try:
        row = conn.execute(
            "update payment_methods set deleted_at = now(), is_default = false "
            "where id = %s and user_id = %s and deleted_at is null returning is_default", (method_id, user["id"])).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise HTTPException(404, "Mobile money account not found.")
    # keep exactly one default: promote the oldest remaining account if none is left
    conn.execute(
        "with oldest as (select id from payment_methods where user_id = %s and deleted_at is null order by created_at limit 1) "
        "update payment_methods set is_default = true from oldest "
        "where payment_methods.id = oldest.id and not exists (select 1 from payment_methods where user_id = %s and is_default = true and deleted_at is null)",
        (user["id"], user["id"]))
    return Response(status_code=204)
