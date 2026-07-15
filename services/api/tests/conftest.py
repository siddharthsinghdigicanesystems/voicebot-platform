"""Test fixtures.

We point SQLAlchemy at a per-test SQLite database so the API tests run
with no external dependencies. Production runs against Postgres; the
type/constraint differences relevant here (JSON, FK cascade) are honored
by SQLite well enough for our coverage.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Set required env BEFORE importing the app
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["JWT_SECRET"] = "test-secret-please-change-in-prod"
os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["TATA_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["FRONTEND_PUBLIC_URL"] = "http://localhost:5173"

from app import db as db_module  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, User  # noqa: E402
from app.routers.auth import limiter  # noqa: E402
from app.security import hash_password  # noqa: E402

# slowapi rate-limits /v1/auth/login at 5/min/IP, which trips when the test
# suite logs in for every test. Disable the limiter globally for tests; it
# is exercised separately in production.
limiter.enabled = False


@pytest_asyncio.fixture
async def session_maker() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    # Seed: admin user + demo customer
    async with maker() as s:
        s.add(User(username="admin", password_hash=hash_password("admin"), is_admin=True))
        s.add(Customer(name="Priya", phone="+919812345678", email="p@x"))
        await s.commit()

    # Override get_session
    async def _get_session():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _get_session
    try:
        yield maker
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(session_maker) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def user_token(client: AsyncClient) -> str:
    r = await client.post("/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def service_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-service-token"}


@pytest.fixture
def user_headers(user_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_token}"}
