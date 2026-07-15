"""Tool dispatch tests with a fake API client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.telephony.base import CallContext, CallDirection
from app.tools import ToolContext, dispatch


def _ctx() -> ToolContext:
    api = AsyncMock()
    api.lookup_customer.return_value = {
        "id": "cust_123",
        "name": "Priya",
        "account_status": "active",
        "next_appointment": None,
        "recent_orders": [],
    }
    api.create_appointment.return_value = {
        "confirmation_id": "APT-9001",
        "id": "apt_1",
    }
    return ToolContext(
        call=CallContext(
            provider_call_id="CA-test",
            direction=CallDirection.INBOUND,
            from_number="+919812345678",
            to_number="+911140000000",
        ),
        api=api,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_lookup_customer_uses_args_phone() -> None:
    ctx = _ctx()
    res = await dispatch("lookup_customer", {"phone": "+911234567890"}, ctx)
    assert res["found"] is True
    assert res["customer_id"] == "cust_123"
    assert ctx.customer is not None
    ctx.api.lookup_customer.assert_awaited_once_with("+911234567890")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_lookup_customer_falls_back_to_caller_phone() -> None:
    ctx = _ctx()
    await dispatch("lookup_customer", {}, ctx)
    ctx.api.lookup_customer.assert_awaited_once_with("+919812345678")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_lookup_customer_not_found() -> None:
    ctx = _ctx()
    ctx.api.lookup_customer.return_value = None  # type: ignore[attr-defined]
    res = await dispatch("lookup_customer", {"phone": "+9100000"}, ctx)
    assert res["found"] is False


@pytest.mark.asyncio
async def test_schedule_appointment_returns_confirmation() -> None:
    ctx = _ctx()
    res = await dispatch(
        "schedule_appointment",
        {
            "customer_id": "cust_123",
            "service": "consultation",
            "date": "2026-05-09",
            "time": "15:00",
        },
        ctx,
    )
    assert res["success"] is True
    assert res["confirmation_id"] == "APT-9001"
    assert ctx.facts["appointment"]["confirmation_id"] == "APT-9001"


@pytest.mark.asyncio
async def test_transfer_sets_destination() -> None:
    ctx = _ctx()
    res = await dispatch(
        "transfer_to_human", {"reason": "needs billing support"}, ctx
    )
    assert res["success"] is True
    assert ctx.transfer_destination == "default_queue"


@pytest.mark.asyncio
async def test_end_call_sets_flag() -> None:
    ctx = _ctx()
    res = await dispatch("end_call", {"reason": "completed"}, ctx)
    assert res["success"] is True
    assert ctx.end_call is True
    assert ctx.end_reason == "completed"


@pytest.mark.asyncio
async def test_unknown_tool_returns_error() -> None:
    ctx = _ctx()
    res = await dispatch("definitely_not_a_tool", {}, ctx)
    assert res["success"] is False
    assert "unknown" in res["error"]


@pytest.mark.asyncio
async def test_handler_exception_is_caught() -> None:
    ctx = _ctx()
    ctx.api.create_appointment.side_effect = RuntimeError("db down")  # type: ignore[attr-defined]
    res: dict[str, Any] = await dispatch(
        "schedule_appointment",
        {
            "customer_id": "cust_123",
            "service": "x",
            "date": "2026-05-09",
            "time": "10:00",
        },
        ctx,
    )
    assert res["success"] is False
