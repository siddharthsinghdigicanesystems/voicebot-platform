"""FastAPI dependencies: DB session, auth principals.

Two auth dependencies:
  - `require_user(request)` — dashboard user (JWT)
  - `require_service(request)` — bridge / worker (shared service_token)

Both raise 401 on failure with no leaks to the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.security import decode_token


@dataclass
class Principal:
    id: str
    kind: Literal["user", "service"]
    username: str | None = None


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def require_user(request: Request) -> Principal:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = decode_token(token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None
    if claims.get("kind") != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a user token")
    sub = claims.get("sub")
    username = claims.get("username")
    if not sub or not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token")
    return Principal(id=sub, kind="user", username=username)


def require_service(request: Request) -> Principal:
    token = _extract_token(request)
    if not token or token != settings.service_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "service auth required")
    return Principal(id="service", kind="service", username="service")


def require_user_or_service(request: Request) -> Principal:
    """For endpoints used by both the dashboard and internal services."""
    token = _extract_token(request)
    if token == settings.service_token:
        return Principal(id="service", kind="service", username="service")
    return require_user(request)


SessionDep = Depends(get_session)
AsyncSessionT = AsyncSession  # alias to keep router signatures readable
