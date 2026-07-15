"""ApiClient tests.

The historical bug we guard against: phone numbers in E.164 format
(e.g. `+911234567890`) include a `+`, which is a reserved query-string
character. Naive f-string interpolation into the URL would cause the
server to decode it as a space and the lookup to 404 on every call —
so the bot would tell the caller "I can't find your record" even when
the seeded customer matched perfectly.
"""

from __future__ import annotations

import httpx
import pytest

from app.api_client import ApiClient


@pytest.mark.asyncio
async def test_lookup_customer_url_encodes_plus_in_phone() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["raw_query"] = request.url.query.decode("ascii")
        captured["phone_param"] = request.url.params["phone"]
        return httpx.Response(
            200,
            json={
                "id": "cust_1",
                "name": "Demo",
                "phone": "+911234567890",
                "email": "demo@example.com",
                "account_status": "active",
                "next_appointment": None,
                "recent_orders": [],
            },
        )

    client = ApiClient(base_url="http://api.test", token="t")
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        base_url="http://api.test",
        transport=transport,
        headers={"Authorization": "Bearer t"},
    )
    try:
        result = await client.lookup_customer("+911234567890")
    finally:
        await client._client.aclose()

    assert result is not None
    assert result["phone"] == "+911234567890"
    # httpx must have percent-encoded the leading '+' so the API doesn't
    # see it as a literal space.
    assert "phone=%2B911234567890" in captured["raw_query"]
    assert "phone=+911234567890" not in captured["raw_query"]
    assert captured["phone_param"] == "+911234567890"


@pytest.mark.asyncio
async def test_lookup_customer_returns_none_on_404() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no customer"})

    client = ApiClient(base_url="http://api.test", token="t")
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(
        base_url="http://api.test",
        transport=transport,
        headers={"Authorization": "Bearer t"},
    )
    try:
        result = await client.lookup_customer("+910000000000")
    finally:
        await client._client.aclose()

    assert result is None
