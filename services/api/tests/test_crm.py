"""CRM endpoint tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_lookup_customer(client: AsyncClient, service_headers: dict[str, str]) -> None:
    r = await client.get(
        "/v1/crm/customers/by-phone?phone=%2B919812345678", headers=service_headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Priya"
    assert body["phone"] == "+919812345678"
    assert body["account_status"] == "active"


@pytest.mark.asyncio
async def test_lookup_customer_404(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    r = await client.get(
        "/v1/crm/customers/by-phone?phone=%2B910000000000", headers=service_headers
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_appointment(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    cust = (
        await client.get(
            "/v1/crm/customers/by-phone?phone=%2B919812345678", headers=service_headers
        )
    ).json()

    r = await client.post(
        "/v1/crm/appointments",
        headers=service_headers,
        json={
            "customer_id": cust["id"],
            "service": "consultation",
            "date": "2026-12-31",
            "time": "10:00",
            "notes": "first visit",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["confirmation_id"].startswith("APT-")
    assert body["service"] == "consultation"
    assert body["customer_id"] == cust["id"]
