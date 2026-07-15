"""Outbound campaigns.

Two responsibilities of this router:
  - Dashboard CRUD: create / list / start / pause campaigns and their contacts.
  - Worker coordination: claim the next pending contact, mark dialing/done,
    sweep stale 'dialing' rows, and apply retry/abandon transitions.

The actual dialing is done by `services/worker/`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import Principal, require_service, require_user, require_user_or_service
from app.logging_setup import get_logger
from app.models import Campaign, CampaignContact
from app.schemas import (
    SUPPORTED_LANGUAGES,
    SUPPORTED_VOICES,
    CampaignBotConfigOut,
    CampaignContactOut,
    CampaignIn,
    CampaignOut,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1/campaigns", tags=["campaigns"])


async def _campaign_summary(session: AsyncSession, campaign: Campaign) -> dict[str, Any]:
    counts = (
        await session.execute(
            select(
                func.count(CampaignContact.id).label("total"),
                func.sum(case((CampaignContact.status == "pending", 1), else_=0)).label("pending"),
                func.sum(case((CampaignContact.status == "succeeded", 1), else_=0)).label("ok"),
                func.sum(case((CampaignContact.status == "failed", 1), else_=0)).label("fail"),
            ).where(CampaignContact.campaign_id == campaign.id)
        )
    ).one()
    out = CampaignOut.model_validate(campaign).model_dump()
    out.update(
        contacts_count=int(counts.total or 0),
        pending_count=int(counts.pending or 0),
        succeeded_count=int(counts.ok or 0),
        failed_count=int(counts.fail or 0),
    )
    return out


def _validate_bot_config(body: CampaignIn) -> None:
    """Ensure voice/language are supported. We validate at the API edge so
    bad input from the dashboard fails loud and early — not later in the
    bridge where the OpenAI Realtime API would 400 on an unknown voice."""
    if body.voice is not None and body.voice not in SUPPORTED_VOICES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"voice must be one of {sorted(SUPPORTED_VOICES)}",
        )
    if body.language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"language must be one of {sorted(SUPPORTED_LANGUAGES)}",
        )


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user),
) -> dict[str, Any]:
    _validate_bot_config(body)
    campaign = Campaign(
        name=body.name,
        scheduled_at=body.scheduled_at,
        max_concurrency=body.max_concurrency,
        retry_attempts=body.retry_attempts,
        brand=body.brand,
        system_prompt_override=body.system_prompt_override,
        voice=body.voice,
        language=body.language,
    )
    session.add(campaign)
    await session.flush()
    for c in body.contacts:
        session.add(
            CampaignContact(
                campaign_id=campaign.id,
                phone=c.phone,
                name=c.name,
                payload=c.payload,
            )
        )
    await session.flush()
    log.info(
        "campaign.created",
        id=campaign.id,
        contacts=len(body.contacts),
        language=campaign.language,
        voice=campaign.voice,
        has_override=bool(campaign.system_prompt_override),
    )
    return await _campaign_summary(session, campaign)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> list[dict[str, Any]]:
    campaigns = (
        await session.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    ).scalars().all()
    return [await _campaign_summary(session, c) for c in campaigns]


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user),
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    return await _campaign_summary(session, campaign)


@router.get("/{campaign_id}/contacts", response_model=list[CampaignContactOut])
async def list_contacts(
    campaign_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user),
) -> list[CampaignContact]:
    contacts = (
        await session.execute(
            select(CampaignContact).where(CampaignContact.campaign_id == campaign_id)
        )
    ).scalars().all()
    return list(contacts)


@router.post("/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user),
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    if campaign.status not in ("draft", "paused", "scheduled"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"cannot start a campaign in '{campaign.status}'"
        )
    campaign.status = "running"
    log.info("campaign.started", id=campaign_id)
    return await _campaign_summary(session, campaign)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user),
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    campaign.status = "paused"
    log.info("campaign.paused", id=campaign_id)
    return await _campaign_summary(session, campaign)


# ---------------------------------------------------------------------------
# Worker coordination (service principal only)
# ---------------------------------------------------------------------------


@router.post("/{campaign_id}/_claim_next", response_model=CampaignContactOut | None)
async def claim_next_contact(
    campaign_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> CampaignContact | None:
    """Atomically claim the next pending contact for dialing.

    Uses SELECT FOR UPDATE SKIP LOCKED so multiple workers don't race for
    the same row. Returns null when no work is available.

    Also honors `Campaign.scheduled_at`: if set and in the future, no
    contacts are returned for this campaign even though it is `running`.
    This lets you flip a campaign to running ahead of time without dialing
    starting until the scheduled moment.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    if campaign.scheduled_at and campaign.scheduled_at > datetime.utcnow():
        # Scheduled for the future — don't pick anything up yet.
        return None

    contact = (
        await session.execute(
            select(CampaignContact)
            .where(
                CampaignContact.campaign_id == campaign_id,
                CampaignContact.status == "pending",
            )
            .order_by(CampaignContact.attempts.asc(), CampaignContact.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if contact is None:
        return None
    contact.status = "dialing"
    contact.attempts += 1
    contact.last_attempt_at = datetime.utcnow()
    return contact


def _apply_failure(contact: CampaignContact, campaign: Campaign, error: str | None) -> str:
    """Decide whether a failed dial is retryable or final.

    `attempts` was already incremented when the contact was claimed, so
    `attempts >= retry_attempts + 1` means we've exhausted all attempts
    (the +1 is the initial dial; `retry_attempts` is the number of *retries*
    on top of that).

    Returns the new status ('pending' or 'abandoned' / 'failed').
    """
    contact.last_error = error
    max_attempts = max(1, campaign.retry_attempts + 1)
    if contact.attempts < max_attempts:
        # Reset to pending so the worker can pick it up again later.
        contact.status = "pending"
        return "pending"
    contact.status = "failed"
    return "failed"


@router.post("/_contacts/{contact_id}/_complete")
async def complete_contact(
    contact_id: str,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> dict[str, str]:
    """Worker reports the outcome of an outbound dial.

    On success: terminal `succeeded`.
    On failure: retry-or-final via `_apply_failure`.
    """
    contact = await session.get(CampaignContact, contact_id)
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    success = bool(body.get("success", False))
    if success:
        contact.status = "succeeded"
        contact.last_error = None
    else:
        campaign = await session.get(Campaign, contact.campaign_id)
        if campaign is None:
            # Shouldn't happen given the FK, but defend anyway.
            contact.status = "failed"
            contact.last_error = body.get("error")
        else:
            _apply_failure(contact, campaign, body.get("error"))
    return {"status": contact.status}


@router.get(
    "/_contacts/{contact_id}/bot_config",
    response_model=CampaignBotConfigOut,
)
async def contact_bot_config(
    contact_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> dict[str, Any]:
    """Bridge calls this on outbound call setup to fetch the campaign's bot
    overrides for this specific contact.

    NULL fields signal "use bridge default" — the bridge merges this with
    `agent.build_system_prompt(...)` and `settings.openai_voice`.
    """
    contact = await session.get(CampaignContact, contact_id)
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    campaign = await session.get(Campaign, contact.campaign_id)
    if not campaign:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    return {
        "campaign_id": campaign.id,
        "brand": campaign.brand,
        "system_prompt_override": campaign.system_prompt_override,
        "voice": campaign.voice,
        "language": campaign.language,
    }


@router.post("/_sweep_stale")
async def sweep_stale_contacts(
    older_than_seconds: int = Query(120, ge=10, le=3600),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> dict[str, int]:
    """Recover contacts stuck in `dialing` longer than `older_than_seconds`.

    Why this exists: if the worker pod dies mid-dial, or a Tata API call
    hangs without the carrier ever sending us a hangup webhook, contacts
    sit in `dialing` forever and the campaign silently stalls under 100%.

    Recovery rule:
      - If `attempts <= retry_attempts`: revert to `pending` so the worker
        picks them up again.
      - Otherwise: terminal `failed` with `last_error='timeout'`.

    Idempotent. Cheap. Worker calls this once per loop iteration.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    stale = (
        await session.execute(
            select(CampaignContact, Campaign)
            .join(Campaign, Campaign.id == CampaignContact.campaign_id)
            .where(
                CampaignContact.status == "dialing",
                CampaignContact.last_attempt_at < cutoff,
            )
        )
    ).all()

    requeued = 0
    abandoned = 0
    for contact, campaign in stale:
        new_status = _apply_failure(contact, campaign, error="timeout_dialing")
        if new_status == "pending":
            requeued += 1
        else:
            abandoned += 1

    if stale:
        log.info(
            "campaigns.sweep_stale",
            requeued=requeued,
            abandoned=abandoned,
            cutoff_seconds=older_than_seconds,
        )
    return {"requeued": requeued, "abandoned": abandoned}
