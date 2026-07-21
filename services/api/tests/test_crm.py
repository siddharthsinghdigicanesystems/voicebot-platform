"""CRM endpoint tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Appointment, LabResult


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
            "doctor": "Dr. Test",
            "department": "General Medicine",
            "notes": "first visit",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["confirmation_id"].startswith("APT-")
    assert body["service"] == "consultation"
    assert body["customer_id"] == cust["id"]
    assert body["status"] == "scheduled"
    assert body["doctor"] == "Dr. Test"


@pytest.mark.asyncio
async def test_confirm_reschedule_cancel_and_lab_results(
    client: AsyncClient,
    service_headers: dict[str, str],
    session_maker: async_sessionmaker,
) -> None:
    cust = (
        await client.get(
            "/v1/crm/customers/by-phone?phone=%2B919812345678", headers=service_headers
        )
    ).json()

    when = datetime.utcnow() + timedelta(days=2)
    async with session_maker() as s:
        s.add(
            Appointment(
                confirmation_id="APT-TEST1",
                customer_id=cust["id"],
                service="cardiology consultation",
                scheduled_for=when,
                status="scheduled",
                doctor="Dr. Mehta",
                department="Cardiology",
                location="Wing B",
            )
        )
        s.add(
            LabResult(
                result_id="LAB-TEST1",
                customer_id=cust["id"],
                test_name="CBC",
                status="pending",
                eta_ready_at=datetime.utcnow() + timedelta(hours=12),
                ordered_at=datetime.utcnow(),
            )
        )
        s.add(
            LabResult(
                result_id="LAB-TEST2",
                customer_id=cust["id"],
                test_name="Chest X-Ray",
                status="sent",
                result_summary="Report available; sent to registered email.",
                delivered_via="email",
                delivered_at=datetime.utcnow(),
                ordered_at=datetime.utcnow() - timedelta(days=1),
            )
        )
        await s.commit()

    confirmed = await client.post(
        "/v1/crm/appointments/APT-TEST1/confirm", headers=service_headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    rescheduled = await client.post(
        "/v1/crm/appointments/APT-TEST1/reschedule",
        headers=service_headers,
        json={"date": "2026-12-20", "time": "11:30"},
    )
    assert rescheduled.status_code == 200
    assert rescheduled.json()["status"] == "scheduled"

    cancelled = await client.post(
        "/v1/crm/appointments/APT-TEST1/cancel", headers=service_headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    labs = await client.get(
        f"/v1/crm/customers/{cust['id']}/lab-results", headers=service_headers
    )
    assert labs.status_code == 200
    body = labs.json()
    assert len(body) == 2
    statuses = {row["result_id"]: row["status"] for row in body}
    assert statuses["LAB-TEST1"] == "pending"
    assert statuses["LAB-TEST2"] == "sent"

    lookup = await client.get(
        "/v1/crm/customers/by-phone?phone=%2B919812345678", headers=service_headers
    )
    assert lookup.status_code == 200
    assert len(lookup.json()["lab_results"]) == 2

