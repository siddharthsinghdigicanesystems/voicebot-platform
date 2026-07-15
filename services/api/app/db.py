"""Database session and base.

Async SQLAlchemy 2.0 with asyncpg. The session-per-request dependency
in `deps.py` rolls back on exceptions and commits on clean exit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def make_engine() -> AsyncEngine:
    """Build the async engine.

    Pool kwargs (`pool_size`, `max_overflow`) are only valid for the
    QueuePool that Postgres uses. SQLite (the test target) uses StaticPool
    and rejects them, so we conditionally pass them.
    """
    url = settings.database_url
    kwargs: dict[str, object] = {
        "echo": False,
        "pool_pre_ping": True,
    }
    is_sqlite = url.startswith("sqlite")
    if not is_sqlite:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        if settings.database_ssl == "require":
            kwargs["connect_args"] = {"ssl": True}
    return create_async_engine(url, **kwargs)


engine: AsyncEngine = make_engine()
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields one transactional session per request."""
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
