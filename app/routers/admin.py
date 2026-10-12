import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import get_conn
from ..deps import admin_user
from ..schemas import AdminPayoutOut, MarkPaidIn, PayoutStatus, RejectIn

router = APIRouter(prefix="/admin", tags=["admin"])

_SELECT = """
select po.id, po.amount_ugx, po.status, po.requested_at, po.provider, po.phone_number, po.account_name,
       po.user_id, p.full_name as user_name, p.email as user_email,
       (select count(*) from sentences s where s.payout_id = po.id)::int as sentence_count,
       po.provider_reference, po.rejection_reason, po.processed_at
  from payouts po join profiles p on p.id = po.user_id"""


@router.get("/payouts", response_model=list[AdminPayoutOut])
def list_payouts(status: PayoutStatus | None = Query(None), admin: dict = Depends(admin_user),
                 conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(f"{_SELECT} where (%s::text is null or po.status = %s) order by po.requested_at", (status, status)).fetchall()


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
    return conn.execute(f"{_SELECT} where po.id = %s", (payout_id,)).fetchone()


@router.post("/payouts/{payout_id}/reject", response_model=AdminPayoutOut)
def reject(payout_id: str, body: RejectIn, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    """Releases the sentences, so the money returns to the user's available balance."""
    _lock_pending(conn, payout_id)
    conn.execute("update payouts set status = 'rejected', rejection_reason = %s, processed_by = %s, processed_at = now() where id = %s",
                 (body.reason.strip(), admin["id"], payout_id))
    conn.execute("update sentences set payout_id = null where payout_id = %s", (payout_id,))
    return conn.execute(f"{_SELECT} where po.id = %s", (payout_id,)).fetchone()
