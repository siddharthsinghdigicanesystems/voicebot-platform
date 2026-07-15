"""Worker's HTTP client for the api service."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class ApiClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.api_internal_url,
            timeout=httpx.Timeout(connect=2.0, read=8.0, write=8.0, pool=8.0),
            headers={"Authorization": f"Bearer {settings.service_token}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_running_campaigns(self) -> list[dict[str, Any]]:
        r = await self._client.get("/v1/campaigns")
        r.raise_for_status()
        return [c for c in r.json() if c.get("status") == "running"]

    async def claim_next(self, campaign_id: str) -> dict[str, Any] | None:
        r = await self._client.post(f"/v1/campaigns/{campaign_id}/_claim_next")
        if r.status_code == 200:
            data = r.json()
            return data if data else None
        r.raise_for_status()
        return None

    async def complete_contact(self, contact_id: str, success: bool, error: str | None) -> None:
        await self._client.post(
            f"/v1/campaigns/_contacts/{contact_id}/_complete",
            json={"success": success, "error": error},
        )

    async def sweep_stale(self, older_than_seconds: int) -> dict[str, int]:
        """Ask the API to recover contacts stuck in 'dialing'.

        Best-effort: a worker that fails to sweep just keeps dialing what it
        can claim — sweep is a janitor, not on the dial path.
        """
        r = await self._client.post(
            "/v1/campaigns/_sweep_stale",
            params={"older_than_seconds": older_than_seconds},
        )
        if r.status_code != 200:
            return {"requeued": 0, "abandoned": 0}
        return r.json()
