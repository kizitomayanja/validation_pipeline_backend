import uuid
import psycopg
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_conn
from ..deps import current_user
from ..schemas import TicketCreateIn, TicketDetailOut, TicketMessageIn, TicketMessageOut, TicketOut, TicketStatusUpdateIn

router = APIRouter(prefix="/tickets", tags=["inquiries"])


def _determine_target_role(category: str) -> str:
    if category in ("clinical_terminology", "review_dispute"):
        return "reviewer"
    return "admin"


@router.get("", response_model=list[TicketOut])
def list_tickets(
    scope: str | None = None,
    filter: str | None = None,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    active_scope = scope or filter or ("my" if user["role"] == "validator" else "inbox")
    role = user["role"]
    user_id = user["id"]

    query = """
        select
            t.id, t.author_id,
            coalesce(p.full_name, 'Unknown') as author_name,
            coalesce(p.email, '') as author_email,
            coalesce(p.role, 'validator') as author_role,
            t.category, t.target_role, t.subject, t.status, t.sentence_id, t.payout_id,
            t.created_at, t.updated_at,
            count(m.id) as message_count,
            coalesce((select message from ticket_messages where ticket_id = t.id order by created_at desc limit 1), '') as last_message_preview
        from support_tickets t
        left join profiles p on p.id = t.author_id
        left join ticket_messages m on m.ticket_id = t.id
    """

    params: list = []
    conditions: list[str] = []

    # Privacy Rule: Validators can strictly ONLY see tickets they authored
    if role == "validator" or active_scope == "my":
        conditions.append("t.author_id = %s")
        params.append(user_id)
    elif active_scope == "clinical":
        if role not in ("reviewer", "admin"):
            conditions.append("t.author_id = %s")
            params.append(user_id)
        else:
            conditions.append("t.target_role = 'reviewer'")
    elif active_scope == "ops":
        if role != "admin":
            conditions.append("t.author_id = %s")
            params.append(user_id)
        else:
            conditions.append("t.target_role = 'admin'")
    elif active_scope == "inbox":
        if role == "admin":
            conditions.append("(t.target_role = 'admin' or t.author_id = %s)")
            params.append(user_id)
        elif role == "reviewer":
            conditions.append("(t.target_role = 'reviewer' or t.author_id = %s)")
            params.append(user_id)
        else:
            conditions.append("t.author_id = %s")
            params.append(user_id)
    else:
        conditions.append("t.author_id = %s")
        params.append(user_id)

    if conditions:
        query += " where " + " and ".join(conditions)

    query += """ group by t.id, t.author_id, p.full_name, p.email, p.role,
                          t.category, t.target_role, t.subject, t.status, t.sentence_id, t.payout_id,
                          t.created_at, t.updated_at
                 order by t.updated_at desc"""

    try:
        return conn.execute(query, tuple(params)).fetchall()
    except psycopg.errors.UndefinedTable:
        return []


@router.post("", response_model=TicketDetailOut, status_code=201)
def create_ticket(
    body: TicketCreateIn,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    target_role = _determine_target_role(body.category)

    try:
        ticket = conn.execute(
            """
            insert into support_tickets (author_id, category, target_role, subject, sentence_id, payout_id, status)
            values (%s, %s, %s, %s, %s, %s, 'open')
            returning id, author_id, category, target_role, subject, status, sentence_id, payout_id, created_at, updated_at
            """,
            (user["id"], body.category, target_role, body.subject, body.sentence_id, body.payout_id),
        ).fetchone()

        msg = conn.execute(
            """
            insert into ticket_messages (ticket_id, sender_id, message)
            values (%s, %s, %s)
            returning id, ticket_id, sender_id, message, created_at
            """,
            (ticket["id"], user["id"], body.message),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, "Support tickets database tables have not been created yet in Supabase. Please run migrations/create_support_tickets_tables.sql.")

    msg_out = TicketMessageOut(
        id=msg["id"],
        ticket_id=msg["ticket_id"],
        sender_id=msg["sender_id"],
        sender_name=user.get("full_name") or "User",
        sender_role=user.get("role") or "validator",
        message=msg["message"],
        created_at=msg["created_at"],
    )

    return TicketDetailOut(
        id=ticket["id"],
        author_id=ticket["author_id"],
        author_name=user.get("full_name") or "User",
        author_email=user.get("email") or "",
        author_role=user.get("role") or "validator",
        category=ticket["category"],
        target_role=ticket["target_role"],
        subject=ticket["subject"],
        status=ticket["status"],
        sentence_id=ticket["sentence_id"],
        payout_id=ticket["payout_id"],
        created_at=ticket["created_at"],
        updated_at=ticket["updated_at"],
        message_count=1,
        last_message_preview=body.message[:80],
        messages=[msg_out],
    )


@router.get("/{ticket_id}", response_model=TicketDetailOut)
def get_ticket(
    ticket_id: uuid.UUID,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    try:
        ticket = conn.execute(
            """
            select
                t.id, t.author_id,
                coalesce(p.full_name, 'Unknown') as author_name,
                coalesce(p.email, '') as author_email,
                coalesce(p.role, 'validator') as author_role,
                t.category, t.target_role, t.subject, t.status, t.sentence_id, t.payout_id,
                t.created_at, t.updated_at
            from support_tickets t
            left join profiles p on p.id = t.author_id
            where t.id = %s
            """,
            (ticket_id,),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, "Support tickets database tables have not been created yet.")

    if not ticket:
        raise HTTPException(404, "Inquiry not found.")

    # Permissions check
    if user["role"] != "admin":
        if user["role"] == "reviewer" and ticket["target_role"] != "reviewer" and ticket["author_id"] != user["id"]:
            raise HTTPException(403, "Access denied.")
        elif user["role"] == "validator" and ticket["author_id"] != user["id"]:
            raise HTTPException(403, "Access denied.")

    messages_raw = conn.execute(
        """
        select m.id, m.ticket_id, m.sender_id,
               coalesce(p.full_name, 'Unknown') as sender_name,
               coalesce(p.role, 'validator') as sender_role,
               m.message, m.created_at
        from ticket_messages m
        left join profiles p on p.id = m.sender_id
        where m.ticket_id = %s
        order by m.created_at asc
        """,
        (ticket_id,),
    ).fetchall()

    messages = [TicketMessageOut(**m) for m in messages_raw]

    return TicketDetailOut(
        **ticket,
        message_count=len(messages),
        last_message_preview=messages[-1].message[:80] if messages else "",
        messages=messages,
    )


@router.post("/{ticket_id}/messages", response_model=TicketMessageOut, status_code=201)
def add_ticket_message(
    ticket_id: uuid.UUID,
    body: TicketMessageIn,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    try:
        ticket = conn.execute("select id, author_id, target_role, status from support_tickets where id = %s", (ticket_id,)).fetchone()
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, "Support tickets database tables have not been created yet.")

    if not ticket:
        raise HTTPException(404, "Inquiry not found.")

    if user["role"] != "admin":
        if user["role"] == "reviewer" and ticket["target_role"] != "reviewer" and ticket["author_id"] != user["id"]:
            raise HTTPException(403, "Access denied.")
        elif user["role"] == "validator" and ticket["author_id"] != user["id"]:
            raise HTTPException(403, "Access denied.")

    msg = conn.execute(
        """
        insert into ticket_messages (ticket_id, sender_id, message)
        values (%s, %s, %s)
        returning id, ticket_id, sender_id, message, created_at
        """,
        (ticket_id, user["id"], body.message),
    ).fetchone()

    # If ticket was resolved and author replies, reopen to in_progress
    new_status = "in_progress" if ticket["status"] == "resolved" and user["id"] == ticket["author_id"] else ticket["status"]
    conn.execute("update support_tickets set status = %s, updated_at = now() where id = %s", (new_status, ticket_id))

    return TicketMessageOut(
        id=msg["id"],
        ticket_id=msg["ticket_id"],
        sender_id=msg["sender_id"],
        sender_name=user.get("full_name") or "User",
        sender_role=user.get("role") or "validator",
        message=msg["message"],
        created_at=msg["created_at"],
    )


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket_status(
    ticket_id: uuid.UUID,
    body: TicketStatusUpdateIn,
    user: dict = Depends(current_user),
    conn: psycopg.Connection = Depends(get_conn),
):
    try:
        ticket = conn.execute("select id, author_id, target_role from support_tickets where id = %s", (ticket_id,)).fetchone()
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, "Support tickets database tables have not been created yet.")

    if not ticket:
        raise HTTPException(404, "Inquiry not found.")

    if user["role"] not in ("admin", "reviewer") and user["id"] != ticket["author_id"]:
        raise HTTPException(403, "Only support recipients or authors can update inquiry status.")

    conn.execute("update support_tickets set status = %s, updated_at = now() where id = %s", (body.status, ticket_id))

    return conn.execute(
        """
        select
            t.id, t.author_id,
            coalesce(p.full_name, 'Unknown') as author_name,
            coalesce(p.email, '') as author_email,
            coalesce(p.role, 'validator') as author_role,
            t.category, t.target_role, t.subject, t.status, t.sentence_id, t.payout_id,
            t.created_at, t.updated_at,
            count(m.id) as message_count,
            coalesce((select message from ticket_messages where ticket_id = t.id order by created_at desc limit 1), '') as last_message_preview
        from support_tickets t
        left join profiles p on p.id = t.author_id
        left join ticket_messages m on m.ticket_id = t.id
        where t.id = %s
        group by t.id, t.author_id, p.full_name, p.email, p.role,
                 t.category, t.target_role, t.subject, t.status, t.sentence_id, t.payout_id,
                 t.created_at, t.updated_at
        """,
        (ticket_id,),
    ).fetchone()
