"""Call lifecycle endpoint tests (used by the bridge)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_full_call_lifecycle(
    client: AsyncClient, service_headers: dict[str, str], user_headers: dict[str, str]
) -> None:
    start = await client.post(
        "/v1/calls",
        headers=service_headers,
        json={
            "provider_call_id": "CA-1",
            "direction": "inbound",
            "from_number": "+911234567890",
            "to_number": "+911140000000",
        },
    )
    assert start.status_code == 201, start.text
    call_id = start.json()["id"]

    t1 = await client.post(
        f"/v1/calls/{call_id}/transcript",
        headers=service_headers,
        json={"role": "user", "text": "Hi, I want to confirm my appointment.", "provider_item_id": "i1"},
    )
    assert t1.status_code == 204

    # Idempotency: same provider_item_id is a no-op
    t1_dup = await client.post(
        f"/v1/calls/{call_id}/transcript",
        headers=service_headers,
        json={"role": "user", "text": "Hi, I want to confirm my appointment.", "provider_item_id": "i1"},
    )
    assert t1_dup.status_code == 204

    t2 = await client.post(
        f"/v1/calls/{call_id}/transcript",
        headers=service_headers,
        json={"role": "assistant", "text": "Sure, let me check.", "provider_item_id": "i2"},
    )
    assert t2.status_code == 204

    inv = await client.post(
        f"/v1/calls/{call_id}/tool-invocations",
        headers=service_headers,
        json={
            "name": "lookup_customer",
            "arguments": {"phone": "+911234567890"},
            "result": {"found": True},
        },
    )
    assert inv.status_code == 204

    end = await client.post(
        f"/v1/calls/{call_id}/end",
        headers=service_headers,
        json={"outcome": "completed", "duration_seconds": 42.5, "facts": {"booked": True}},
    )
    assert end.status_code == 204

    detail = await client.get(f"/v1/calls/{call_id}", headers=user_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["outcome"] == "completed"
    assert body["facts"] == {"booked": True}
    assert len(body["transcript"]) == 2
    assert len(body["tool_invocations"]) == 1


@pytest.mark.asyncio
async def test_call_started_is_idempotent(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    payload = {
        "provider_call_id": "CA-DUP",
        "direction": "inbound",
        "from_number": "+11",
        "to_number": "+22",
    }
    a = await client.post("/v1/calls", headers=service_headers, json=payload)
    b = await client.post("/v1/calls", headers=service_headers, json=payload)
    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["id"] == b.json()["id"]


@pytest.mark.asyncio
async def test_list_calls_filters(
    client: AsyncClient, service_headers: dict[str, str], user_headers: dict[str, str]
) -> None:
    for i, direction in enumerate(["inbound", "outbound", "inbound"]):
        await client.post(
            "/v1/calls",
            headers=service_headers,
            json={
                "provider_call_id": f"CA-F-{i}",
                "direction": direction,
                "from_number": "+91",
                "to_number": "+22",
            },
        )

    r = await client.get("/v1/calls?direction=inbound", headers=user_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(it["direction"] == "inbound" for it in items)
    assert len(items) >= 2


@pytest.mark.asyncio
async def test_user_cannot_write_call_lifecycle(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/v1/calls",
        headers=user_headers,
        json={
            "provider_call_id": "CA-x",
            "direction": "inbound",
            "from_number": "+1",
            "to_number": "+2",
        },
    )
    assert r.status_code == 401
