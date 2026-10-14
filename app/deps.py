import jwt
import psycopg
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import get_conn
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)
_UNAUTH = {"WWW-Authenticate": "Bearer"}


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 conn: psycopg.Connection = Depends(get_conn)) -> dict:
    if creds is None:
        raise HTTPException(401, "Sign in to continue.", headers=_UNAUTH)
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session has expired.", headers=_UNAUTH)
    user = conn.execute("select id, email, full_name, role, is_active from profiles where id = %s", (claims["sub"],)).fetchone()
    if user is None or not user["is_active"]:
        raise HTTPException(401, "This account is not active.", headers=_UNAUTH)
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only.")
    return user


def reviewer_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("reviewer", "admin"):
        raise HTTPException(403, "Reviewers and admins only.")
    return user

