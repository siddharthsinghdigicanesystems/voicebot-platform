"""Calls API.

Two audiences:
  - **Bridge** (service principal): writes — call_started, transcript appends,
    tool invocation logs, call_ended.
  - **Dashboard** (user principal): reads — list & detail.

Both audiences hit the same endpoints, gated by `require_user_or_service`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_session
from app.deps import Principal, require_service, require_user_or_service
from app.logging_setup import get_logger
from app.models import Call, ToolInvocation, TranscriptSegment
from app.schemas import (
    CallDetailOut,
    CallEndIn,
    CallListOut,
    CallStartIn,
    CallSummaryOut,
    ToolInvocationIn,
    TranscriptIn,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1/calls", tags=["calls"])


# ---------------------------------------------------------------------------
# Bridge writes
# ---------------------------------------------------------------------------


@router.post("", response_model=CallSummaryOut, status_code=status.HTTP_201_CREATED)
async def call_started(
    body: CallStartIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> Call:
    # Idempotent: if the bridge retries, return the existing record.
    existing = (
        await session.execute(
            select(Call).where(Call.provider_call_id == body.provider_call_id)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    call = Call(
        provider_call_id=body.provider_call_id,
        direction=body.direction,
        from_number=body.from_number,
        to_number=body.to_number,
        audio_format=body.audio_format,
        sample_rate=body.sample_rate,
        extra_metadata=body.metadata,
    )
    session.add(call)
    await session.flush()
    log.info("call.started", call_id=call.id, provider_call_id=call.provider_call_id)
    return call


@router.post(
    "/{call_id}/end",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def call_ended(
    call_id: str,
    body: CallEndIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> None:
    call = await session.get(Call, call_id)
    if not call:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    call.ended_at = datetime.utcnow()
    call.duration_seconds = body.duration_seconds
    call.outcome = body.outcome
    call.facts = body.facts or {}
    log.info("call.ended", call_id=call_id, outcome=body.outcome, duration=body.duration_seconds)


@router.post(
    "/{call_id}/transcript",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def append_transcript(
    call_id: str,
    body: TranscriptIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> None:
    call = await session.get(Call, call_id)
    if not call:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    # Idempotent on (call_id, provider_item_id)
    if body.provider_item_id:
        existing = (
            await session.execute(
                select(TranscriptSegment.id).where(
                    TranscriptSegment.call_id == call_id,
                    TranscriptSegment.provider_item_id == body.provider_item_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return
    seg = TranscriptSegment(
        call_id=call_id,
        role=body.role,
        text=body.text,
        provider_item_id=body.provider_item_id,
    )
    session.add(seg)


@router.post(
    "/{call_id}/tool-invocations",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def add_tool_invocation(
    call_id: str,
    body: ToolInvocationIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_service),
) -> None:
    call = await session.get(Call, call_id)
    if not call:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    inv = ToolInvocation(
        call_id=call_id,
        name=body.name,
        arguments=body.arguments,
        result=body.result,
    )
    session.add(inv)


# ---------------------------------------------------------------------------
# Dashboard reads
# ---------------------------------------------------------------------------


@router.get("", response_model=CallListOut)
async def list_calls(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    direction: str | None = Query(None, pattern="^(inbound|outbound)$"),
    outcome: str | None = None,
    q: str | None = Query(None, description="phone number substring"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> dict[str, Any]:
    stmt = select(Call)
    count_stmt = select(func.count()).select_from(Call)

    if direction:
        stmt = stmt.where(Call.direction == direction)
        count_stmt = count_stmt.where(Call.direction == direction)
    if outcome:
        stmt = stmt.where(Call.outcome == outcome)
        count_stmt = count_stmt.where(Call.outcome == outcome)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((Call.from_number.like(like)) | (Call.to_number.like(like)))
        count_stmt = count_stmt.where((Call.from_number.like(like)) | (Call.to_number.like(like)))

    stmt = stmt.order_by(desc(Call.started_at)).limit(limit).offset(offset)

    items = (await session.execute(stmt)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    return {
        "items": [CallSummaryOut.model_validate(c) for c in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(
    call_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> Call:
    call = (
        await session.execute(
            select(Call)
            .options(
                selectinload(Call.transcript),
                selectinload(Call.tool_invocations),
            )
            .where(Call.id == call_id)
        )
    ).scalar_one_or_none()
    if not call:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    return call


# ---------------------------------------------------------------------------
# Live transcript (dashboard subscribes via WebSocket)
# ---------------------------------------------------------------------------


@router.websocket("/{call_id}/live")
async def live_transcript(websocket: WebSocket, call_id: str) -> None:
    """Stream transcript segments to the dashboard as they happen.

    Auth: the client passes the JWT as `?token=...`; we accept first, verify,
    and close on failure (per FastAPI WS conventions — the Authorization
    header isn't easily forwarded by browser WS clients).
    """
    import redis.asyncio as aioredis

    from app.security import decode_token

    token = websocket.query_params.get("token", "")
    try:
        claims = decode_token(token)
        if claims.get("kind") != "user":
            await websocket.close(code=4403)
            return
    except ValueError:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    log.info("live_transcript.subscribe", call_id=call_id)

    redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(f"transcript:{call_id}")
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                await websocket.send_text(message["data"])
            except WebSocketDisconnect:
                break
    finally:
        await pubsub.unsubscribe(f"transcript:{call_id}")
        await pubsub.aclose()
        await redis.aclose()
        log.info("live_transcript.unsubscribe", call_id=call_id)
