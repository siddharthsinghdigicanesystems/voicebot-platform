"""HTTP client for the api service.

Used by tools (CRM lookups, appointment booking) and persistence
(transcript, call lifecycle). Keeps a single AsyncClient with a sane
timeout and retries on transient errors.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)


class ApiClient:
    """Thin wrapper around httpx.AsyncClient with auth + small retry."""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.api_internal_url).rstrip("/")
        self.token = token or settings.service_token
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ApiClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_TIMEOUT,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("ApiClient not entered (use 'async with')")
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._client.request(method, path, params=params, json=json)
                if r.status_code >= 500 and attempt < retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                r.raise_for_status()
                if r.headers.get("content-type", "").startswith("application/json"):
                    return r.json()
                return r.text
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    # --- CRM (used by tools) -------------------------------------------------

    async def lookup_customer(self, phone: str) -> dict[str, Any] | None:
        # E.164 numbers contain '+', which becomes a literal space if we
        # f-string it into the URL. Pass via `params=` so httpx percent-encodes.
        try:
            return await self._request(
                "GET", "/v1/crm/customers/by-phone", params={"phone": phone}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def create_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/crm/appointments", json=payload)

    # --- Persistence (used by session lifecycle) -----------------------------

    async def call_started(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/calls", json=payload)

    async def call_ended(self, call_id: str, payload: dict[str, Any]) -> None:
        await self._request("POST", f"/v1/calls/{call_id}/end", json=payload)

    async def append_transcript(self, call_id: str, segment: dict[str, Any]) -> None:
        await self._request("POST", f"/v1/calls/{call_id}/transcript", json=segment)

    async def record_tool_invocation(self, call_id: str, payload: dict[str, Any]) -> None:
        await self._request("POST", f"/v1/calls/{call_id}/tool-invocations", json=payload)

    # --- Per-campaign bot config (used on outbound start) ---------------------

    async def get_contact_bot_config(self, contact_id: str) -> dict[str, Any] | None:
        """Fetch the bot overrides for a campaign contact.

        Returns None on 404 (e.g. the contact was deleted between dial and
        bridge connect — we fall back to the bridge defaults rather than
        failing the call).
        """
        try:
            return await self._request(
                "GET", f"/v1/campaigns/_contacts/{contact_id}/bot_config"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
