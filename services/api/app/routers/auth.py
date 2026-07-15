"""Auth endpoints.

Login is intentionally simple: POST username + password, get a JWT.
For SSO, replace `login` with your IdP integration; the rest of the
codebase only depends on `Principal` from `deps.py`.

Rate-limiting on /login is via `slowapi`. We keep the config conservative
(5/min/IP) — tighten in prod.

NB: `from __future__ import annotations` is intentionally omitted here.
slowapi's `@limiter.limit` decorator wraps `login()` and FastAPI's
pydantic introspection fails to resolve the forward-ref `LoginIn` against
the wrapper's module globals. Concrete annotations sidestep that.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import Principal, require_user
from app.logging_setup import get_logger
from app.models import User
from app.schemas import LoginIn, TokenOut, UserOut
from app.security import issue_user_token, verify_password

log = get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

limiter = Limiter(key_func=get_remote_address)


@router.post("/login", response_model=TokenOut)
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: LoginIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        log.info("auth.login.failed", username=body.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    token, ttl = issue_user_token(user_id=user.id, username=user.username)
    log.info("auth.login.ok", username=user.username)
    return TokenOut(access_token=token, expires_in=ttl)


@router.get("/me", response_model=UserOut)
async def me(
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = (
        await session.execute(select(User).where(User.id == principal.id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user no longer exists")
    return user
