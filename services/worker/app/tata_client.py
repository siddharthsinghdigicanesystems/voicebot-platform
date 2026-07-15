"""Tata click-to-call client.

Tata SmartFlo's outbound API initiates a call from your DID to a destination
and connects the answered leg to a target — for us, the bridge's WebSocket
streaming endpoint. The exact endpoint differs across Tata API versions; the
key fields are stable and documented in `docs/tata-integration.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class DialResult:
    success: bool
    provider_call_id: str | None = None
    error: str | None = None


class TataClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.tata_api_base_url,
            timeout=10.0,
            headers={"Authorization": f"Bearer {settings.tata_api_key}"} if settings.tata_api_key else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def dial(
        self,
        *,
        to: str,
        campaign_contact_id: str,
        custom_parameters: dict[str, Any] | None = None,
    ) -> DialResult:
        if not settings.tata_api_key:
            log.warning(
                "tata.dial.skipped_no_creds",
                to=to,
                hint="set TATA_API_KEY to enable real outbound dialing",
            )
            return DialResult(success=False, error="tata_credentials_missing")

        params = {"direction": "outbound", "campaign_contact_id": campaign_contact_id}
        if custom_parameters:
            params.update(custom_parameters)

        # Tata's "Click-to-Call with Streaming" endpoint shape (api version 1).
        # If your account uses a different shape (some Tata regions use
        # /v1/sms-or-call/initiate or /click2call), adjust here.
        payload = {
            "from": settings.tata_outbound_caller_id,
            "to": to,
            "stream": {
                "url": settings.bridge_public_ws_url,
                "track": "inbound_track",
            },
            "custom_parameters": params,
            "timeout_sec": int(settings.dial_timeout_seconds),
        }
        try:
            r = await self._client.post("/v1/calls/outbound", json=payload)
            if r.status_code >= 400:
                log.error("tata.dial.http_error", status=r.status_code, body=r.text[:300])
                return DialResult(success=False, error=f"http_{r.status_code}")
            data = r.json()
            return DialResult(success=True, provider_call_id=str(data.get("call_sid", "")))
        except httpx.HTTPError as exc:
            log.error("tata.dial.exception", error=str(exc))
            return DialResult(success=False, error=str(exc))
