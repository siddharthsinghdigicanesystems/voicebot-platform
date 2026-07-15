"""Per-campaign bot config tests.

Cover create-with-overrides, voice/language validation, and the
service-only `/_contacts/{id}/bot_config` lookup the bridge uses.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _create(
    client: AsyncClient,
    user_headers: dict[str, str],
    *,
    voice: str | None = None,
    language: str = "en",
    brand: str | None = None,
    system_prompt_override: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "test",
        "language": language,
        "contacts": [{"phone": "+919812345678"}],
    }
    if voice is not None:
        body["voice"] = voice
    if brand is not None:
        body["brand"] = brand
    if system_prompt_override is not None:
        body["system_prompt_override"] = system_prompt_override
    r = await client.post("/v1/campaigns", headers=user_headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_campaign_with_bot_overrides(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    out = await _create(
        client,
        user_headers,
        voice="shimmer",
        language="hinglish",
        brand="Globex Health",
        system_prompt_override="Be ultra brief.",
    )
    assert out["voice"] == "shimmer"
    assert out["language"] == "hinglish"
    assert out["brand"] == "Globex Health"
    assert out["system_prompt_override"] == "Be ultra brief."


@pytest.mark.asyncio
async def test_create_rejects_unsupported_voice(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/v1/campaigns",
        headers=user_headers,
        json={
            "name": "x",
            "voice": "darth-vader",
            "contacts": [{"phone": "+919812345678"}],
        },
    )
    assert r.status_code == 400
    assert "voice" in r.text.lower()


@pytest.mark.asyncio
async def test_create_rejects_unsupported_language(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/v1/campaigns",
        headers=user_headers,
        json={
            "name": "x",
            "language": "klingon",
            "contacts": [{"phone": "+919812345678"}],
        },
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_default_language_is_english(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    out = await _create(client, user_headers)
    assert out["language"] == "en"
    assert out["voice"] is None
    assert out["brand"] is None


@pytest.mark.asyncio
async def test_bot_config_endpoint_returns_overrides(
    client: AsyncClient, user_headers: dict[str, str], service_headers: dict[str, str]
) -> None:
    out = await _create(
        client,
        user_headers,
        voice="ballad",
        language="hi",
        brand="Globex",
    )
    cid = out["id"]
    contacts = await client.get(
        f"/v1/campaigns/{cid}/contacts", headers=user_headers
    )
    contact_id = contacts.json()[0]["id"]

    r = await client.get(
        f"/v1/campaigns/_contacts/{contact_id}/bot_config",
        headers=service_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["campaign_id"] == cid
    assert body["voice"] == "ballad"
    assert body["language"] == "hi"
    assert body["brand"] == "Globex"
    assert body["system_prompt_override"] is None


@pytest.mark.asyncio
async def test_bot_config_endpoint_404_for_unknown_contact(
    client: AsyncClient, service_headers: dict[str, str]
) -> None:
    r = await client.get(
        "/v1/campaigns/_contacts/does-not-exist/bot_config",
        headers=service_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bot_config_endpoint_requires_service_token(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    out = await _create(client, user_headers)
    cid = out["id"]
    contacts = await client.get(
        f"/v1/campaigns/{cid}/contacts", headers=user_headers
    )
    contact_id = contacts.json()[0]["id"]
    r = await client.get(
        f"/v1/campaigns/_contacts/{contact_id}/bot_config",
        headers=user_headers,  # user token, not service
    )
    assert r.status_code in (401, 403)
