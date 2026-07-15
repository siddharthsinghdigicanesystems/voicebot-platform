"""Stale-dialing sweep + retry-on-failure tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Campaign, CampaignContact


async def _create_campaign(
    client: AsyncClient,
    user_headers: dict[str, str],
    *,
    name: str = "Test campaign",
    contacts: list[dict[str, str]] | None = None,
    retry_attempts: int = 1,
) -> str:
    r = await client.post(
        "/v1/campaigns",
        headers=user_headers,
        json={
            "name": name,
            "retry_attempts": retry_attempts,
            "contacts": contacts or [{"phone": "+919812345678"}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_complete_contact_failure_with_retries_left_reverts_to_pending(
    client: AsyncClient, user_headers: dict[str, str], service_headers: dict[str, str]
) -> None:
    cid = await _create_campaign(client, user_headers, retry_attempts=2)
    await client.post(f"/v1/campaigns/{cid}/start", headers=user_headers)

    claim = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    contact_id = claim.json()["id"]
    assert claim.json()["status"] == "dialing"
    assert claim.json()["attempts"] == 1

    r = await client.post(
        f"/v1/campaigns/_contacts/{contact_id}/_complete",
        headers=service_headers,
        json={"success": False, "error": "no_answer"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    # Re-claim succeeds: the row is pending again.
    again = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    assert again.status_code == 200
    body = again.json()
    assert body["id"] == contact_id
    assert body["attempts"] == 2


@pytest.mark.asyncio
async def test_complete_contact_failure_after_retries_exhausted_marks_failed(
    client: AsyncClient, user_headers: dict[str, str], service_headers: dict[str, str]
) -> None:
    # retry_attempts=0 -> a single attempt total.
    cid = await _create_campaign(client, user_headers, retry_attempts=0)
    await client.post(f"/v1/campaigns/{cid}/start", headers=user_headers)

    claim = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    contact_id = claim.json()["id"]

    r = await client.post(
        f"/v1/campaigns/_contacts/{contact_id}/_complete",
        headers=service_headers,
        json={"success": False, "error": "busy"},
    )
    assert r.json()["status"] == "failed"

    # Nothing left to claim.
    none_left = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    assert none_left.status_code == 200
    assert none_left.json() is None


@pytest.mark.asyncio
async def test_scheduled_at_in_future_blocks_claim(
    client: AsyncClient,
    user_headers: dict[str, str],
    service_headers: dict[str, str],
    session_maker,
) -> None:
    cid = await _create_campaign(client, user_headers)

    # Push scheduled_at into the future.
    async with session_maker() as s:
        camp = await s.get(Campaign, cid)
        assert camp is not None
        camp.scheduled_at = datetime.utcnow() + timedelta(hours=2)
        camp.status = "running"
        await s.commit()

    r = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    assert r.status_code == 200
    assert r.json() is None


@pytest.mark.asyncio
async def test_sweep_stale_requeues_when_retries_left(
    client: AsyncClient,
    user_headers: dict[str, str],
    service_headers: dict[str, str],
    session_maker,
) -> None:
    cid = await _create_campaign(client, user_headers, retry_attempts=2)
    await client.post(f"/v1/campaigns/{cid}/start", headers=user_headers)

    claim = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    contact_id = claim.json()["id"]

    # Forcibly age the row so the sweeper sees it as stale.
    async with session_maker() as s:
        contact = await s.get(CampaignContact, contact_id)
        assert contact is not None
        contact.last_attempt_at = datetime.utcnow() - timedelta(minutes=20)
        await s.commit()

    r = await client.post(
        "/v1/campaigns/_sweep_stale?older_than_seconds=60",
        headers=service_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"requeued": 1, "abandoned": 0}

    async with session_maker() as s:
        contact = await s.get(CampaignContact, contact_id)
        assert contact is not None
        assert contact.status == "pending"
        assert contact.last_error == "timeout_dialing"


@pytest.mark.asyncio
async def test_sweep_stale_abandons_when_retries_exhausted(
    client: AsyncClient,
    user_headers: dict[str, str],
    service_headers: dict[str, str],
    session_maker,
) -> None:
    cid = await _create_campaign(client, user_headers, retry_attempts=0)
    await client.post(f"/v1/campaigns/{cid}/start", headers=user_headers)

    claim = await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)
    contact_id = claim.json()["id"]

    async with session_maker() as s:
        contact = await s.get(CampaignContact, contact_id)
        assert contact is not None
        contact.last_attempt_at = datetime.utcnow() - timedelta(minutes=20)
        await s.commit()

    r = await client.post(
        "/v1/campaigns/_sweep_stale?older_than_seconds=60",
        headers=service_headers,
    )
    assert r.json() == {"requeued": 0, "abandoned": 1}

    async with session_maker() as s:
        contact = await s.get(CampaignContact, contact_id)
        assert contact is not None
        assert contact.status == "failed"


@pytest.mark.asyncio
async def test_sweep_stale_ignores_recently_dialed(
    client: AsyncClient,
    user_headers: dict[str, str],
    service_headers: dict[str, str],
    session_maker,
) -> None:
    cid = await _create_campaign(client, user_headers)
    await client.post(f"/v1/campaigns/{cid}/start", headers=user_headers)
    await client.post(f"/v1/campaigns/{cid}/_claim_next", headers=service_headers)

    r = await client.post(
        "/v1/campaigns/_sweep_stale?older_than_seconds=600",
        headers=service_headers,
    )
    assert r.json() == {"requeued": 0, "abandoned": 0}

    # Contact should still be `dialing`.
    async with session_maker() as s:
        contacts = (
            await s.execute(select(CampaignContact).where(CampaignContact.campaign_id == cid))
        ).scalars().all()
        assert all(c.status == "dialing" for c in contacts)
