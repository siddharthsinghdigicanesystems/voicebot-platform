"""Tata webhooks.

Tata SmartFlo posts call lifecycle events (initiated, ringing, answered,
hangup, voicemail-detected) to a webhook URL. We verify the HMAC-SHA256
signature in `X-Tata-Signature` against `TATA_WEBHOOK_SECRET`.

This is separate from the streaming WebSocket the bridge handles — webhooks
are control-plane (per-call lifecycle), the bridge is data-plane (audio).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.logging_setup import get_logger
from app.models import Call, Campaign, CampaignContact
from app.routers.campaigns import _apply_failure

log = get_logger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def _verify_signature(raw: bytes, signature: str) -> bool:
    if not settings.tata_webhook_secret:
        # In dev / when not configured, accept everything but log.
        log.warning("tata.webhook.no_secret_configured")
        return True
    expected = hmac.new(
        settings.tata_webhook_secret.encode(),
        raw,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


async def _verified_payload(request: Request) -> dict[str, Any]:
    raw = await request.body()
    signature = request.headers.get("x-tata-signature", "").lower().removeprefix("sha256=")
    if not _verify_signature(raw, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook signature")
    import json

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid JSON: {exc}"
        ) from exc


@router.post("/tata", status_code=status.HTTP_200_OK)
async def tata_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Tata posts here on every call lifecycle change.

    Expected payload (Tata's exact field names vary by API version — adjust
    here, not elsewhere):

      {
        "event": "call.answered" | "call.hangup" | "call.failed" | ...,
        "call_sid": "...",
        "from": "...",
        "to": "...",
        "timestamp": "ISO8601",
        "campaign_contact_id": "...",   // we set this on outbound dial
        "duration": 42,                 // hangup only
        "outcome": "answered|busy|no_answer|failed|voicemail"
      }
    """
    payload = await _verified_payload(request)
    event = payload.get("event", "")
    call_sid = payload.get("call_sid")

    # NB: structlog reserves `event` for the log message name itself, so we
    # log it under `tata_event` to avoid a TypeError from the kwargs collision.
    log.info("tata.webhook", tata_event=event, call_sid=call_sid)

    if not call_sid:
        return {"ok": "true"}

    # Update call record if it exists (the bridge creates it on stream start;
    # webhooks may arrive before/after).
    call = (
        await session.execute(select(Call).where(Call.provider_call_id == call_sid))
    ).scalar_one_or_none()

    if event == "call.hangup" and call:
        if call.ended_at is None:
            call.ended_at = datetime.utcnow()
            call.duration_seconds = float(payload.get("duration", 0)) or call.duration_seconds
        if not call.outcome:
            call.outcome = payload.get("outcome", "completed")

    if event in ("call.failed", "call.no_answer", "call.busy"):
        contact_id = payload.get("campaign_contact_id")
        if contact_id:
            contact = await session.get(CampaignContact, contact_id)
            if contact and contact.status in ("dialing", "pending"):
                campaign = await session.get(Campaign, contact.campaign_id)
                if campaign is not None:
                    new_status = _apply_failure(contact, campaign, error=event)
                    log.info(
                        "tata.webhook.contact_transition",
                        contact_id=contact_id,
                        event=event,
                        new_status=new_status,
                        attempts=contact.attempts,
                    )
                else:
                    contact.status = "failed"
                    contact.last_error = event

    return {"ok": "true"}
