"""Tata webhook signature + handling tests."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient


def _sign(body: bytes, secret: str = "test-webhook-secret") -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_rejects_bad_signature(client: AsyncClient) -> None:
    body = json.dumps({"event": "call.hangup", "call_sid": "X"}).encode()
    r = await client.post(
        "/v1/webhooks/tata",
        content=body,
        headers={"x-tata-signature": "deadbeef", "content-type": "application/json"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_accepts_good_signature(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    # First create a call so the webhook has something to update.
    await client.post(
        "/v1/calls",
        headers=service_headers,
        json={
            "provider_call_id": "CA-WH",
            "direction": "outbound",
            "from_number": "+91",
            "to_number": "+22",
        },
    )

    payload = {
        "event": "call.hangup",
        "call_sid": "CA-WH",
        "duration": 33,
        "outcome": "answered",
    }
    body = json.dumps(payload).encode()
    r = await client.post(
        "/v1/webhooks/tata",
        content=body,
        headers={
            "x-tata-signature": _sign(body),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 200
