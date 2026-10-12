import psycopg
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_conn
from ..deps import current_user
from ..schemas import PayoutIn, PayoutOut

router = APIRouter(prefix="/payouts", tags=["payouts"])
_COLS = "id, amount_ugx, status, requested_at, provider, phone_number, account_name"


@router.get("", response_model=list[PayoutOut])
def list_payouts(user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(f"select {_COLS} from payouts where user_id = %s order by requested_at desc", (user["id"],)).fetchall()


@router.post("", response_model=PayoutOut, status_code=201)
def cash_out(body: PayoutIn, user: dict = Depends(current_user), conn: psycopg.Connection = Depends(get_conn)):
    """Cashes out the user's whole available balance. Runs in one transaction under a lock on the user's row,
    so two simultaneous requests can't both pay out the same sentences."""
    conn.execute("select id from profiles where id = %s for update", (user["id"],))
    method = conn.execute("select provider, phone_number, account_name from payment_methods "
                          "where id = %s and user_id = %s and deleted_at is null", (body.payment_method_id, user["id"])).fetchone()
    if method is None:
        raise HTTPException(400, "Choose a mobile money account.")
    if conn.execute("select 1 from payouts where user_id = %s and status = 'pending'", (user["id"],)).fetchone():
        raise HTTPException(409, "You already have a cash-out waiting for payment.")
    available = conn.execute("select coalesce(sum(reward_ugx), 0)::int as n from sentences where validated_by = %s and payout_id is null",
                             (user["id"],)).fetchone()["n"]
    minimum = conn.execute("select min_cashout_ugx from reward_settings").fetchone()["min_cashout_ugx"]
    if available < minimum:
        raise HTTPException(400, f"You need at least UGX {minimum:,} to cash out.")
    payout = conn.execute(
        f"""insert into payouts (user_id, amount_ugx, provider, phone_number, account_name)
            values (%s, %s, %s, %s, %s) returning {_COLS}""",
        (user["id"], available, method["provider"], method["phone_number"], method["account_name"])).fetchone()
    conn.execute("update sentences set payout_id = %s where validated_by = %s and payout_id is null", (payout["id"], user["id"]))
    return payout
