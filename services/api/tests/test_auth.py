"""Auth endpoint tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_password(client: AsyncClient) -> None:
    r = await client.post("/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    data = r.json()
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    assert isinstance(data["access_token"], str)


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": "not-the-password"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user(client: AsyncClient, user_headers: dict[str, str]) -> None:
    r = await client.get("/v1/auth/me", headers=user_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True


@pytest.mark.asyncio
async def test_service_token_rejected_for_user_endpoint(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    r = await client.get("/v1/auth/me", headers=service_headers)
    assert r.status_code in (401, 403)
