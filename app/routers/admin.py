import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import get_conn
from ..deps import admin_user
from ..schemas import (
    AdminOverviewOut,
    AdminPayoutOut,
    AdminUserOut,
    AdminUserUpdateIn,
    MarkPaidIn,
    PayoutStatus,
    RejectIn,
    SettingsOut,
    SettingsUpdateIn,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_SELECT_PAYOUT = """
select po.id, po.amount_ugx, po.status, po.requested_at, po.provider, po.phone_number, po.account_name,
       po.user_id, p.full_name as user_name, p.email as user_email,
       (select count(*) from sentences s where s.payout_id = po.id)::int as sentence_count,
       po.provider_reference, po.rejection_reason, po.processed_at
  from payouts po join profiles p on p.id = po.user_id"""


# ------------------------------------------------------------------ Settings & Budget Control
@router.get("/settings", response_model=SettingsOut)
def get_settings(admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    rs = conn.execute("select rate_ugx, min_reward_ugx, min_cashout_ugx, max_budget_ugx, updated_at from reward_settings").fetchone()
    spent_row = conn.execute("select coalesce(sum(reward_ugx), 0)::int as spent from sentences where validated_by is not null").fetchone()
    spent = spent_row["spent"] if spent_row else 0
    max_b = rs["max_budget_ugx"]
    remaining = max(0, max_b - spent) if max_b is not None else None
    exhausted = (max_b is not None) and (spent >= max_b)
    return {
        **rs,
        "total_spent_ugx": spent,
        "remaining_budget_ugx": remaining,
        "is_budget_exhausted": exhausted,
    }


@router.patch("/settings", response_model=SettingsOut)
def update_settings(body: SettingsUpdateIn, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    updates = []
    params = []

    if "max_budget_ugx" in body.model_fields_set:
        updates.append("max_budget_ugx = %s")
        params.append(body.max_budget_ugx)
    if body.rate_ugx is not None:
        updates.append("rate_ugx = %s")
        params.append(body.rate_ugx)
    if body.min_reward_ugx is not None:
        updates.append("min_reward_ugx = %s")
        params.append(body.min_reward_ugx)
    if body.min_cashout_ugx is not None:
        updates.append("min_cashout_ugx = %s")
        params.append(body.min_cashout_ugx)

    if updates:
        updates.append("updated_at = now()")
        sql = f"update reward_settings set {', '.join(updates)}"
        conn.execute(sql, tuple(params))

    return get_settings(admin=admin, conn=conn)


# ------------------------------------------------------------------ Executive Overview
@router.get("/overview", response_model=AdminOverviewOut)
def get_overview(admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    # Sentences summary
    s_stats = conn.execute(
        """select count(*)::int as total,
                  count(*) filter (where validated_by is not null)::int as validated,
                  count(*) filter (where verified = true)::int as verified
           from sentences"""
    ).fetchone()

    total_s = s_stats["total"]
    validated_s = s_stats["validated"]
    verified_s = s_stats["verified"]
    unvalidated_s = total_s - validated_s
    pct_complete = round(100.0 * validated_s / total_s, 2) if total_s else 0.0
    pct_verified = round(100.0 * verified_s / total_s, 2) if total_s else 0.0

    # Budget & Payouts summary
    rs = conn.execute("select max_budget_ugx from reward_settings").fetchone()
    max_b = rs["max_budget_ugx"] if rs else None

    po_stats = conn.execute(
        """select coalesce(sum(s.reward_ugx), 0)::int as total_earned,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'pending'), 0)::int as pending,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'paid'), 0)::int as paid
           from sentences s
           left join payouts po on po.id = s.payout_id
           where s.validated_by is not null"""
    ).fetchone()

    total_earned = po_stats["total_earned"]
    pending = po_stats["pending"]
    paid = po_stats["paid"]
    remaining = max(0, max_b - total_earned) if max_b is not None else None
    exhausted = (max_b is not None) and (total_earned >= max_b)

    # Users summary
    u_stats = conn.execute(
        """select count(*)::int as total,
                  count(*) filter (where role = 'validator')::int as validators,
                  count(*) filter (where role = 'reviewer')::int as reviewers,
                  count(*) filter (where role = 'admin')::int as admins
           from profiles"""
    ).fetchone()

    return {
        "total_sentences": total_s,
        "validated_sentences": validated_s,
        "verified_sentences": verified_s,
        "unvalidated_sentences": unvalidated_s,
        "completion_percentage": pct_complete,
        "verified_percentage": pct_verified,
        "max_budget_ugx": max_b,
        "total_earned_ugx": total_earned,
        "pending_payouts_ugx": pending,
        "paid_out_ugx": paid,
        "remaining_budget_ugx": remaining,
        "is_budget_exhausted": exhausted,
        "total_users": u_stats["total"],
        "validators_count": u_stats["validators"],
        "reviewers_count": u_stats["reviewers"],
        "admins_count": u_stats["admins"],
    }


# ------------------------------------------------------------------ User Oversight
@router.get("/users", response_model=list[AdminUserOut])
def list_users(admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    rows = conn.execute(
        """select p.id, p.email, p.full_name, p.role, p.is_active, p.created_at,
                  count(s.id)::int as validated_count,
                  coalesce(sum(s.reward_ugx), 0)::int as total_earned_ugx,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'pending'), 0)::int as pending_ugx,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'paid'), 0)::int as paid_out_ugx
           from profiles p
           left join sentences s on s.validated_by = p.id
           left join payouts po on po.id = s.payout_id
           group by p.id
           order by p.created_at desc"""
    ).fetchall()
    return rows


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(user_id: str, body: AdminUserUpdateIn, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    updates = []
    params = []
    if body.role is not None:
        updates.append("role = %s")
        params.append(body.role)
    if body.is_active is not None:
        updates.append("is_active = %s")
        params.append(body.is_active)

    if not updates:
        raise HTTPException(400, "Nothing to update.")

    params.append(user_id)
    try:
        updated = conn.execute(
            f"update profiles set {', '.join(updates)} where id = %s returning id",
            tuple(params)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        updated = None

    if updated is None:
        raise HTTPException(404, "User not found.")

    # Return full user record with stats
    row = conn.execute(
        """select p.id, p.email, p.full_name, p.role, p.is_active, p.created_at,
                  count(s.id)::int as validated_count,
                  coalesce(sum(s.reward_ugx), 0)::int as total_earned_ugx,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'pending'), 0)::int as pending_ugx,
                  coalesce(sum(s.reward_ugx) filter (where po.status = 'paid'), 0)::int as paid_out_ugx
           from profiles p
           left join sentences s on s.validated_by = p.id
           left join payouts po on po.id = s.payout_id
           where p.id = %s
           group by p.id""",
        (user_id,)
    ).fetchone()
    return row


# ------------------------------------------------------------------ Payout Oversight
@router.get("/payouts", response_model=list[AdminPayoutOut])
def list_payouts(status: PayoutStatus | None = Query(None), admin: dict = Depends(admin_user),
                 conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(f"{_SELECT_PAYOUT} where (%s::text is null or po.status = %s) order by po.requested_at desc", (status, status)).fetchall()


def _lock_pending(conn: psycopg.Connection, payout_id: str) -> None:
    try:
        row = conn.execute("select status from payouts where id = %s for update", (payout_id,)).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        row = None
    if row is None:
        raise HTTPException(404, "Payout not found.")
    if row["status"] != "pending":
        raise HTTPException(409, f"This payout is already {row['status']}.")


@router.post("/payouts/{payout_id}/mark-paid", response_model=AdminPayoutOut)
def mark_paid(payout_id: str, body: MarkPaidIn, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    """Call after you have sent the money by mobile money; provider_reference is the transaction id."""
    _lock_pending(conn, payout_id)
    conn.execute("update payouts set status = 'paid', provider_reference = %s, processed_by = %s, processed_at = now() where id = %s",
                 (body.provider_reference.strip(), admin["id"], payout_id))
    conn.execute("update sentences set paid_status = true, paid_at = now() where payout_id = %s", (payout_id,))
    return conn.execute(f"{_SELECT_PAYOUT} where po.id = %s", (payout_id,)).fetchone()


@router.post("/payouts/{payout_id}/reject", response_model=AdminPayoutOut)
def reject(payout_id: str, body: RejectIn, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    """Releases the sentences, so the money returns to the user's available balance."""
    _lock_pending(conn, payout_id)
    conn.execute("update payouts set status = 'rejected', rejection_reason = %s, processed_by = %s, processed_at = now() where id = %s",
                 (body.reason.strip(), admin["id"], payout_id))
    conn.execute("update sentences set payout_id = null where payout_id = %s", (payout_id,))
    return conn.execute(f"{_SELECT_PAYOUT} where po.id = %s", (payout_id,)).fetchone()
