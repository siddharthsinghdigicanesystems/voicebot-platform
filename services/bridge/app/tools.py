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

import httpx

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


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {exc.response.status_code}"


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
        "email": customer.get("email"),
        "account_status": customer.get("account_status"),
        "patient_mrn": customer.get("patient_mrn"),
        "outstanding_balance": customer.get("outstanding_balance", 0),
        "next_appointment": customer.get("next_appointment"),
        "appointments": customer.get("appointments", []),
        "lab_results": customer.get("lab_results", []),
        "recent_orders": customer.get("recent_orders", []),
    }


async def _schedule_appointment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    payload = {
        "customer_id": args["customer_id"],
        "service": args["service"],
        "date": args["date"],
        "time": args["time"],
        "doctor": args.get("doctor"),
        "department": args.get("department"),
        "location": args.get("location"),
        "notes": args.get("notes"),
        "source_call_id": ctx.call.provider_call_id,
    }
    res = await ctx.api.create_appointment(payload)
    ctx.facts["appointment"] = res
    return {
        "success": True,
        "confirmation_id": res.get("confirmation_id"),
        "status": res.get("status", "scheduled"),
        "summary": (
            f"Booked {args['service']} on {args['date']} at {args['time']}."
        ),
    }


async def _confirm_appointment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    confirmation_id = args["confirmation_id"]
    try:
        res = await ctx.api.confirm_appointment(confirmation_id)
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": _http_error_detail(exc)}
    ctx.facts["appointment_confirmed"] = res
    when = res.get("scheduled_for", "")
    return {
        "success": True,
        "confirmation_id": res.get("confirmation_id"),
        "status": res.get("status"),
        "service": res.get("service"),
        "doctor": res.get("doctor"),
        "department": res.get("department"),
        "location": res.get("location"),
        "scheduled_for": when,
        "summary": (
            f"Confirmed {res.get('service')} with {res.get('doctor') or 'the doctor'} "
            f"on {when}."
        ),
    }


async def _cancel_appointment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    confirmation_id = args["confirmation_id"]
    try:
        res = await ctx.api.cancel_appointment(confirmation_id)
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": _http_error_detail(exc)}
    ctx.facts["appointment_cancelled"] = res
    return {
        "success": True,
        "confirmation_id": res.get("confirmation_id"),
        "status": res.get("status"),
        "summary": f"Cancelled appointment {confirmation_id}.",
    }


async def _reschedule_appointment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    confirmation_id = args["confirmation_id"]
    payload = {
        "date": args["date"],
        "time": args["time"],
        "notes": args.get("notes"),
        "source_call_id": ctx.call.provider_call_id,
    }
    try:
        res = await ctx.api.reschedule_appointment(confirmation_id, payload)
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": _http_error_detail(exc)}
    ctx.facts["appointment_rescheduled"] = res
    return {
        "success": True,
        "confirmation_id": res.get("confirmation_id"),
        "status": res.get("status"),
        "scheduled_for": res.get("scheduled_for"),
        "summary": (
            f"Rescheduled to {args['date']} at {args['time']}."
        ),
    }


async def _lookup_test_results(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    customer_id = args.get("customer_id")
    if not customer_id and ctx.customer:
        customer_id = ctx.customer.get("id")
    if not customer_id:
        return {
            "success": False,
            "error": "customer_id required — call lookup_customer first",
        }
    try:
        rows = await ctx.api.list_lab_results(customer_id)
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": _http_error_detail(exc)}

    results = []
    for r in rows:
        status = r.get("status")
        item: dict[str, Any] = {
            "result_id": r.get("result_id"),
            "test_name": r.get("test_name"),
            "status": status,
            "eta_ready_at": r.get("eta_ready_at"),
            "delivered_via": r.get("delivered_via"),
            "delivered_at": r.get("delivered_at"),
            "notes": r.get("notes"),
        }
        # Only surface a short readiness phrase — never clinical numbers.
        if status in {"ready", "sent"}:
            item["caller_message"] = r.get("result_summary") or (
                "Report is ready."
                if status == "ready"
                else "Report has been sent to the patient."
            )
        elif status in {"pending", "processing"}:
            eta = r.get("eta_ready_at")
            item["caller_message"] = (
                f"Not ready yet. Expected around {eta}."
                if eta
                else "Not ready yet. Please check back later."
            )
        else:
            item["caller_message"] = f"Status is {status}."
        results.append(item)

    ctx.facts["lab_results"] = results
    return {"success": True, "count": len(results), "results": results}


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
    "confirm_appointment": _confirm_appointment,
    "cancel_appointment": _cancel_appointment,
    "reschedule_appointment": _reschedule_appointment,
    "lookup_test_results": _lookup_test_results,
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
