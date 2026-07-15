"""Tool dispatch: maps function names from the model to actual handlers.

Each handler is `async (args, ctx) -> result`. Results are JSON-serialized
and returned to the model via `OpenAIRealtimeClient.submit_tool_result`.

Two side-effecting tools (`transfer_to_human`, `end_call`) also signal the
session orchestrator to take action via `ctx.directives` — we set a flag,
the orchestrator reads it after the next loop iteration. Doing it via flags
(not callbacks) keeps the control flow linear and easy to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.api_client import ApiClient
from app.logging_setup import get_logger
from app.metrics import tool_invocations_total
from app.telephony.base import CallContext

log = get_logger(__name__)


@dataclass
class ToolContext:
    call: CallContext
    api: ApiClient
    # Directives set by tools; read by the session orchestrator each loop iteration.
    transfer_destination: str | None = None
    end_call: bool = False
    end_reason: str | None = None
    # Session enrichment: facts gathered during the call.
    customer: dict[str, Any] | None = None
    facts: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _lookup_customer(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    phone = args.get("phone") or ctx.call.from_number
    if not phone:
        return {"found": False, "error": "no phone number provided or available on call"}
    customer = await ctx.api.lookup_customer(phone)
    if customer is None:
        return {"found": False, "phone": phone}
    ctx.customer = customer
    return {
        "found": True,
        "customer_id": customer["id"],
        "name": customer.get("name"),
        "account_status": customer.get("account_status"),
        "next_appointment": customer.get("next_appointment"),
        "recent_orders": customer.get("recent_orders", []),
    }


async def _schedule_appointment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    payload = {
        "customer_id": args["customer_id"],
        "service": args["service"],
        "date": args["date"],
        "time": args["time"],
        "notes": args.get("notes"),
        "source_call_id": ctx.call.provider_call_id,
    }
    res = await ctx.api.create_appointment(payload)
    ctx.facts["appointment"] = res
    return {
        "success": True,
        "confirmation_id": res.get("confirmation_id"),
        "summary": (
            f"Booked {args['service']} on {args['date']} at {args['time']}."
        ),
    }


async def _transfer_to_human(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ctx.transfer_destination = args.get("destination") or "default_queue"
    ctx.facts["transfer_reason"] = args.get("reason", "")
    return {
        "success": True,
        "say_to_caller": (
            "Sure, I'll connect you with a colleague who can help. Please hold for just a moment."
        ),
    }


async def _end_call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ctx.end_call = True
    ctx.end_reason = args.get("reason", "completed")
    return {"success": True}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "lookup_customer": _lookup_customer,
    "schedule_appointment": _schedule_appointment,
    "transfer_to_human": _transfer_to_human,
    "end_call": _end_call,
}


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        log.warning("tool.unknown", name=name)
        tool_invocations_total.labels(tool=name, outcome="unknown").inc()
        return {"success": False, "error": f"unknown tool '{name}'"}
    try:
        result = await handler(args, ctx)
        outcome = "ok" if result.get("success", True) and not result.get("error") else "error"
        tool_invocations_total.labels(tool=name, outcome=outcome).inc()
        log.info("tool.executed", name=name, outcome=outcome)
        return result
    except Exception as exc:  # noqa: BLE001
        tool_invocations_total.labels(tool=name, outcome="exception").inc()
        log.error("tool.exception", name=name, error=str(exc))
        return {"success": False, "error": "internal error"}
