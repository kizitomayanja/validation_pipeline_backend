import uuid
import smtplib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from ..config import get_settings
from ..db import get_conn
from ..mailer import send_password_reset_email
from ..schemas import (LoginIn, MessageOut, PasswordResetConfirmIn, PasswordResetRequestIn, RefreshIn, RegisterIn,
                       TokensOut)
from ..security import (DUMMY_HASH, create_access_token, hash_password, hash_token, new_password_reset_token,
                        new_refresh_token, rate_limit, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(rate_limit)])
_BAD_LOGIN = HTTPException(401, "Email or password is incorrect.")


def issue_tokens(conn: psycopg.Connection, user_id: uuid.UUID, role: str, family_id: uuid.UUID | None = None) -> TokensOut:
    token, digest = new_refresh_token()
    expires = datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_ttl_days)
    conn.execute("insert into refresh_tokens (user_id, family_id, token_hash, expires_at) values (%s, %s, %s, %s)",
                 (user_id, family_id or uuid.uuid4(), digest, expires))
    return TokensOut(access_token=create_access_token(user_id, role), refresh_token=token)


@router.post("/register", response_model=TokensOut, status_code=201)
def register(body: RegisterIn, conn: psycopg.Connection = Depends(get_conn)):
    try:
        user = conn.execute(
            "insert into profiles (email, password_hash, full_name) values (%s, %s, %s) returning id, role",
            (body.email.lower(), hash_password(body.password), body.full_name)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    return issue_tokens(conn, user["id"], user["role"])


@router.post("/login", response_model=TokensOut)
def login(body: LoginIn, conn: psycopg.Connection = Depends(get_conn)):
    user = conn.execute("select id, role, password_hash, is_active from profiles where email = %s",
                        (body.email.strip().lower(),)).fetchone()
    ok = verify_password(body.password, user["password_hash"] if user else DUMMY_HASH)
    if not user or not ok:
        raise _BAD_LOGIN
    if not user["is_active"]:
        raise HTTPException(403, "This account has been deactivated.")
    return issue_tokens(conn, user["id"], user["role"])


@router.post("/refresh", response_model=TokensOut)
def refresh(body: RefreshIn, conn: psycopg.Connection = Depends(get_conn)):
    """Rotation: the presented token is revoked and replaced. Presenting a token that was already
    revoked means it was copied or replayed, so the whole family (every descendant) is revoked."""
    row = conn.execute(
        """select t.id, t.family_id, t.user_id, t.revoked_at, t.expires_at, p.role, p.is_active
           from refresh_tokens t join profiles p on p.id = t.user_id
           where t.token_hash = %s for update of t""", (hash_token(body.refresh_token),)).fetchone()
    if row is None:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if row["revoked_at"] is not None:
        conn.execute("update refresh_tokens set revoked_at = now() where family_id = %s and revoked_at is null", (row["family_id"],))
        conn.commit()   # commit now: raising below would otherwise roll the revocation back
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if row["expires_at"] < datetime.now(timezone.utc) or not row["is_active"]:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    conn.execute("update refresh_tokens set revoked_at = now() where id = %s", (row["id"],))
    return issue_tokens(conn, row["user_id"], row["role"], row["family_id"])


@router.post("/logout", status_code=204)
def logout(body: RefreshIn, conn: psycopg.Connection = Depends(get_conn)):
    """Revokes the whole family so every copy of this session's refresh token stops working. Always 204."""
    conn.execute("""update refresh_tokens set revoked_at = now()
                    where revoked_at is null and family_id in (select family_id from refresh_tokens where token_hash = %s)""",
                 (hash_token(body.refresh_token),))
    return Response(status_code=204)


@router.post("/password-reset/request", response_model=MessageOut)
def request_password_reset(body: PasswordResetRequestIn, conn: psycopg.Connection = Depends(get_conn)):
    """Create a short-lived, single-use reset link without revealing whether the account exists."""
    user = conn.execute(
        "select id from profiles where email = %s and is_active = true for update",
        (str(body.email).strip().lower(),),
    ).fetchone()

    if user is not None:
        token, token_digest = new_password_reset_token()
        settings = get_settings()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_ttl_minutes)
        conn.execute(
            "update password_reset_tokens set used_at = now() where user_id = %s and used_at is null",
            (user["id"],),
        )
        conn.execute(
            "insert into password_reset_tokens (user_id, token_hash, expires_at) values (%s, %s, %s)",
            (user["id"], token_digest, expires_at),
        )
        reset_url = f"{settings.frontend_url.rstrip('/')}/reset-password?{urlencode({'token': token})}"
        try:
            send_password_reset_email(str(body.email), reset_url)
        except (OSError, RuntimeError, smtplib.SMTPException) as exc:
            raise HTTPException(503, "Unable to send the password reset email. Please try again later.") from exc

    return {"message": "If an account exists for that email, a password reset link has been sent."}


@router.post("/password-reset/confirm", response_model=MessageOut)
def confirm_password_reset(body: PasswordResetConfirmIn, conn: psycopg.Connection = Depends(get_conn)):
    """Consume a valid reset token, update the password, and revoke existing sessions."""
    token_row = conn.execute(
        """update password_reset_tokens
           set used_at = now()
           where token_hash = %s and used_at is null and expires_at > now()
           returning user_id""",
        (hash_token(body.token),),
    ).fetchone()
    if token_row is None:
        raise HTTPException(400, "This password reset link is invalid or has expired.")

    conn.execute(
        "update profiles set password_hash = %s where id = %s",
        (hash_password(body.new_password), token_row["user_id"]),
    )
    conn.execute(
        "update password_reset_tokens set used_at = now() where user_id = %s and used_at is null",
        (token_row["user_id"],),
    )
    conn.execute(
        "update refresh_tokens set revoked_at = now() where user_id = %s and revoked_at is null",
        (token_row["user_id"],),
    )
    return {"message": "Your password has been reset. Please sign in with your new password."}
