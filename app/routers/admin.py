import csv
import io
from datetime import datetime, timezone
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..db import get_conn
from ..deps import admin_user
from ..schemas import (
    AdminOverviewOut,
    AdminPayoutOut,
    AdminUserOut,
    AdminUserUpdateIn,
    DatasetExportHistoryOut,
    DatasetExportStatsOut,
    MarkPaidIn,
    PayoutStatus,
    RejectIn,
    SettingsOut,
    SettingsUpdateIn,
    UserQualityProfileOut,
)


router = APIRouter(prefix="/admin", tags=["admin"])

_SELECT_PAYOUT = """
select po.id, po.amount_ugx, po.status, po.requested_at, po.provider, po.phone_number, po.account_name,
       po.user_id, p.full_name as user_name, p.email as user_email,
       (select count(*) from sentences s where s.payout_id = po.id)::int as sentence_count,
       (select count(*) from sentences s where s.payout_id = po.id and s.verified = true)::int as verified_count,
       (select coalesce(round(100.0 * count(*) filter (where s.verified = true) / nullif(count(*), 0), 1), 0.0)::float from sentences s where s.payout_id = po.id) as accuracy_rate,
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
                  count(s.id) filter (where s.verified = true)::int as verified_count,
                  coalesce(round(100.0 * count(s.id) filter (where s.verified = true) / nullif(count(s.id), 0), 1), 0.0)::float as accuracy_rate,
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


@router.get("/users/{user_id}/quality", response_model=UserQualityProfileOut)
def get_user_quality_profile(user_id: str, admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    """Fetch contributor review accuracy profile, validation volume, and their submitted translations for inspection."""
    try:
        user = conn.execute(
            """select p.id, p.email, p.full_name, p.role, p.is_active, p.created_at,
                      count(s.id)::int as validated_count,
                      count(s.id) filter (where s.verified = true)::int as verified_count,
                      count(s.id) filter (where s.verified = false)::int as pending_review_count,
                      coalesce(round(100.0 * count(s.id) filter (where s.verified = true) / nullif(count(s.id), 0), 1), 0.0)::float as accuracy_rate,
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
    except psycopg.errors.InvalidTextRepresentation:
        user = None

    if user is None:
        raise HTTPException(404, "User not found.")

    recent_sentences = conn.execute(
        """select s.id, s.noisy_english, s.attempted, s.clean_lug, s.difficulty, s.reward_ugx,
                  s.verified, s.verified_at, r.full_name as verified_by_name, s.validated_at,
                  s.payout_id, s.paid_status
           from sentences s
           left join profiles r on r.id = s.verified_by
           where s.validated_by = %s
           order by s.validated_at desc
           limit 50""",
        (user_id,)
    ).fetchall()

    return {
        **user,
        "recent_sentences": recent_sentences,
    }


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
                  count(s.id) filter (where s.verified = true)::int as verified_count,
                  coalesce(round(100.0 * count(s.id) filter (where s.verified = true) / nullif(count(s.id), 0), 1), 0.0)::float as accuracy_rate,
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


def _ensure_export_schema(conn: psycopg.Connection) -> None:
    conn.execute("alter table sentences add column if not exists exported_at timestamptz;")
    conn.execute(
        """create table if not exists dataset_exports (
            id uuid primary key default gen_random_uuid(),
            exported_by uuid not null references profiles(id),
            sentence_count integer not null,
            scope text not null check (scope in ('verified_only', 'all_validated')),
            only_new boolean not null default true,
            file_name text not null,
            created_at timestamptz not null default now()
        );"""
    )


# ------------------------------------------------------------------ Dataset Extraction & Export
@router.get("/export/stats", response_model=DatasetExportStatsOut)
def get_export_stats(admin: dict = Depends(admin_user), conn: psycopg.Connection = Depends(get_conn)):
    _ensure_export_schema(conn)
    counts = conn.execute(
        """select count(*) filter (where validated_by is not null)::int as total_validated,
                  count(*) filter (where validated_by is not null and verified = true)::int as total_verified,
                  count(*) filter (where validated_by is not null and verified = true and exported_at is null)::int as unexported_verified,
                  count(*) filter (where validated_by is not null and exported_at is null)::int as unexported_validated
             from sentences"""
    ).fetchone()

    recent_exports = conn.execute(
        """select de.id, p.full_name as exported_by_name, de.sentence_count, de.scope, de.only_new, de.file_name, de.created_at
             from dataset_exports de
             join profiles p on p.id = de.exported_by
            order by de.created_at desc
            limit 10"""
    ).fetchall()

    total_exports = conn.execute("select count(*)::int as count from dataset_exports").fetchone()["count"]
    last_export_at = recent_exports[0]["created_at"] if recent_exports else None

    return {
        "total_validated": counts["total_validated"],
        "total_verified": counts["total_verified"],
        "unexported_verified": counts["unexported_verified"],
        "unexported_validated": counts["unexported_validated"],
        "last_export_at": last_export_at,
        "total_exports_count": total_exports,
        "recent_exports": recent_exports,
    }


@router.get("/export/sentences.csv")
def export_sentences_csv(
    scope: Literal["verified_only", "all_validated"] = Query("verified_only", description="Dataset quality scope"),
    only_new: bool = Query(True, description="Filter only sentences that haven't been exported yet"),
    mark_exported: bool = Query(True, description="Mark exported sentences with current timestamp"),
    admin: dict = Depends(admin_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    _ensure_export_schema(conn)

    query_parts = [
        """select s.id as sentence_id,
                  s.noisy_english as english_prompt,
                  s.attempted as initial_draft,
                  s.clean_lug as validated_luganda,
                  case when s.verified then 'verified' else 'validated' end as quality_status,
                  s.difficulty,
                  p.full_name as validator_name,
                  p.email as validator_email,
                  s.validated_at,
                  r.full_name as verified_by_name,
                  s.verified_at
             from sentences s
             join profiles p on p.id = s.validated_by
        left join profiles r on r.id = s.verified_by
            where s.validated_by is not null"""
    ]
    params: list = []

    if scope == "verified_only":
        query_parts.append("and s.verified = true")

    if only_new:
        query_parts.append("and s.exported_at is null")

    query_parts.append("order by s.id asc")
    full_query = " ".join(query_parts)

    rows = conn.execute(full_query, tuple(params)).fetchall()

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    filename = f"ekyama_dataset_{scope}_{timestamp_str}.csv"

    # Mark as exported and log in dataset_exports if requested and rows exist
    if rows and mark_exported:
        sentence_ids = [r["sentence_id"] for r in rows]
        conn.execute(
            "update sentences set exported_at = %s where id = any(%s)",
            (now, sentence_ids),
        )
        conn.execute(
            """insert into dataset_exports (exported_by, sentence_count, scope, only_new, file_name, created_at)
               values (%s, %s, %s, %s, %s, %s)""",
            (admin["id"], len(rows), scope, only_new, filename, now),
        )

    # Build CSV content
    buffer = io.StringIO()
    # Write UTF-8 BOM so Excel opens cleanly without mangling Luganda characters
    buffer.write("\ufeff")

    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    headers = [
        "sentence_id",
        "english_prompt",
        "initial_draft",
        "validated_luganda",
        "quality_status",
        "difficulty",
        "validator_name",
        "validator_email",
        "validated_at",
        "verified_by_name",
        "verified_at",
        "exported_at",
    ]
    writer.writerow(headers)

    for r in rows:
        writer.writerow([
            r["sentence_id"],
            r["english_prompt"],
            r["initial_draft"],
            r["validated_luganda"],
            r["quality_status"],
            r["difficulty"],
            r["validator_name"],
            r["validator_email"] or "",
            r["validated_at"].isoformat() if r["validated_at"] else "",
            r["verified_by_name"] or "",
            r["verified_at"].isoformat() if r["verified_at"] else "",
            now.isoformat(),
        ])

    csv_data = buffer.getvalue()
    buffer.close()

    return Response(
        content=csv_data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )

