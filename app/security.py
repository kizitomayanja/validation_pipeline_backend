import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request

from .config import get_settings

_ph = PasswordHasher()
# Verified against when the email is unknown, so "no such user" and "wrong password" take the same time.
DUMMY_HASH = _ph.hash("timing-equaliser")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "role": role, "type": "access", "iat": now,
              "exp": now + timedelta(minutes=s.access_ttl_minutes)}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    if claims.get("type") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return claims


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_refresh_token() -> tuple[str, str]:
    """Returns (token to give the client, hash to store)."""
    token = secrets.token_urlsafe(48)
    return token, hash_token(token)


# --- tiny in-memory rate limiter for /auth/*. Per process: put a proxy/Redis limiter in front if you run several workers.
_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    limit = get_settings().auth_rate_limit
    key = (request.client.host if request.client else "?", request.url.path)
    now, q = time.monotonic(), _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
    q.append(now)
