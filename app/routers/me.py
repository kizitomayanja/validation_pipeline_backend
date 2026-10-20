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
    return conn.execute(
        """select b.total_earned_ugx, b.available_ugx, b.pending_ugx, b.paid_out_ugx, b.validated_count,
                  b.verified_count, b.pending_review_count, b.pending_review_ugx, rs.min_cashout_ugx
           from user_balances b cross join reward_settings rs where b.user_id = %s""", (user["id"],)).fetchone()


_METHOD_COLS = "id, provider, phone_number, account_name, is_default"


@router.get("/payment-methods", response_model=list[MethodOut])
def list_methods(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(f"select {_METHOD_COLS} from payment_methods where user_id = %s and deleted_at is null order by created_at",
                        (user["id"],)).fetchall()


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
        """update payment_methods set is_default = true where id = (
             select id from payment_methods where user_id = %s and deleted_at is null order by created_at limit 1)
           and not exists (select 1 from payment_methods where user_id = %s and deleted_at is null and is_default)""",
        (user["id"], user["id"]))
    return Response(status_code=204)
