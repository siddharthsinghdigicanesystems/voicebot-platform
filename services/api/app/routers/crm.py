"""Mock CRM endpoints.

In a real deployment, replace these with calls to your CRM (Salesforce,
Zoho, HubSpot, custom) via an outbound HTTP client. The bridge calls
**these** endpoints during conversations, so swapping CRMs only requires
re-implementing this router — bridge code is unaffected.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import Principal, require_user_or_service
from app.logging_setup import get_logger
from app.models import Appointment, Customer, LabResult
from app.schemas import (
    AppointmentIn,
    AppointmentOut,
    AppointmentRescheduleIn,
    CustomerOut,
    LabResultOut,
)

log = get_logger(__name__)

router = APIRouter(prefix="/v1/crm", tags=["crm"])

_ACTIVE_APPT_STATUSES = {"scheduled", "confirmed"}


def _appt_summary(a: Appointment) -> dict[str, Any]:
    return {
        "confirmation_id": a.confirmation_id,
        "service": a.service,
        "date": a.scheduled_for.date().isoformat(),
        "time": a.scheduled_for.strftime("%H:%M"),
        "status": a.status,
        "doctor": a.doctor,
        "department": a.department,
        "location": a.location,
        "notes": a.notes,
    }


def _lab_summary(r: LabResult) -> dict[str, Any]:
    return {
        "result_id": r.result_id,
        "test_name": r.test_name,
        "status": r.status,
        # Never expose detailed clinical values to the voice layer — only
        # readiness / delivery. Summary is a short non-diagnostic phrase.
        "result_summary": r.result_summary if r.status in {"ready", "sent"} else None,
        "eta_ready_at": r.eta_ready_at.isoformat() if r.eta_ready_at else None,
        "delivered_via": r.delivered_via,
        "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
        "notes": r.notes,
    }


def _customer_to_out(customer: Customer) -> dict[str, Any]:
    now = datetime.utcnow()
    appointments = sorted(customer.appointments, key=lambda a: a.scheduled_for)
    upcoming = [
        a
        for a in appointments
        if a.scheduled_for > now and a.status in _ACTIVE_APPT_STATUSES
    ]
    next_appt = _appt_summary(upcoming[0]) if upcoming else None

    labs = sorted(customer.lab_results, key=lambda r: r.ordered_at, reverse=True)

    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "account_status": customer.account_status,
        "patient_mrn": customer.patient_mrn,
        "outstanding_balance": float(customer.outstanding_balance or 0),
        "next_appointment": next_appt,
        "appointments": [_appt_summary(a) for a in appointments],
        "lab_results": [_lab_summary(r) for r in labs],
        "recent_orders": [],  # mock
    }


async def _load_customer_by_phone(session: AsyncSession, phone: str) -> Customer | None:
    return (
        await session.execute(
            select(Customer)
            .options(
                selectinload(Customer.appointments),
                selectinload(Customer.lab_results),
            )
            .where(Customer.phone == phone)
        )
    ).scalar_one_or_none()


async def _get_appointment_by_confirmation(
    session: AsyncSession, confirmation_id: str
) -> Appointment:
    appt = (
        await session.execute(
            select(Appointment).where(Appointment.confirmation_id == confirmation_id)
        )
    ).scalar_one_or_none()
    if not appt:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no appointment {confirmation_id}"
        )
    return appt


@router.get("/customers/by-phone", response_model=CustomerOut)
async def lookup_by_phone(
    phone: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> dict[str, Any]:
    customer = await _load_customer_by_phone(session, phone)
    if not customer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no customer with phone {phone}")
    return _customer_to_out(customer)


@router.post(
    "/appointments",
    response_model=AppointmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_appointment(
    body: AppointmentIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> Appointment:
    customer = await session.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown customer_id")
    try:
        scheduled_for = datetime.fromisoformat(f"{body.date}T{body.time}")
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid date/time: {exc}"
        ) from exc

    appt = Appointment(
        confirmation_id=f"APT-{secrets.token_hex(3).upper()}",
        customer_id=customer.id,
        service=body.service,
        scheduled_for=scheduled_for,
        status="scheduled",
        doctor=body.doctor,
        department=body.department,
        location=body.location,
        notes=body.notes,
        source_call_id=body.source_call_id,
    )
    session.add(appt)
    await session.flush()
    log.info(
        "crm.appointment.created",
        confirmation_id=appt.confirmation_id,
        customer_id=customer.id,
    )
    return appt


@router.post("/appointments/{confirmation_id}/confirm", response_model=AppointmentOut)
async def confirm_appointment(
    confirmation_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> Appointment:
    appt = await _get_appointment_by_confirmation(session, confirmation_id)
    if appt.status == "cancelled":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "cannot confirm a cancelled appointment"
        )
    if appt.status == "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "appointment already completed"
        )
    appt.status = "confirmed"
    await session.flush()
    log.info("crm.appointment.confirmed", confirmation_id=confirmation_id)
    return appt


@router.post("/appointments/{confirmation_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    confirmation_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> Appointment:
    appt = await _get_appointment_by_confirmation(session, confirmation_id)
    if appt.status == "cancelled":
        return appt
    if appt.status == "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "cannot cancel a completed appointment"
        )
    appt.status = "cancelled"
    await session.flush()
    log.info("crm.appointment.cancelled", confirmation_id=confirmation_id)
    return appt


@router.post("/appointments/{confirmation_id}/reschedule", response_model=AppointmentOut)
async def reschedule_appointment(
    confirmation_id: str,
    body: AppointmentRescheduleIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> Appointment:
    appt = await _get_appointment_by_confirmation(session, confirmation_id)
    if appt.status in {"cancelled", "completed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot reschedule a {appt.status} appointment",
        )
    try:
        scheduled_for = datetime.fromisoformat(f"{body.date}T{body.time}")
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid date/time: {exc}"
        ) from exc
    appt.scheduled_for = scheduled_for
    appt.status = "scheduled"
    if body.notes:
        appt.notes = body.notes
    if body.source_call_id:
        appt.source_call_id = body.source_call_id
    await session.flush()
    log.info(
        "crm.appointment.rescheduled",
        confirmation_id=confirmation_id,
        scheduled_for=scheduled_for.isoformat(),
    )
    return appt


@router.get(
    "/customers/{customer_id}/lab-results",
    response_model=list[LabResultOut],
)
async def list_lab_results(
    customer_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> list[LabResult]:
    customer = await session.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown customer_id")
    rows = (
        await session.execute(
            select(LabResult)
            .where(LabResult.customer_id == customer_id)
            .order_by(LabResult.ordered_at.desc())
        )
    ).scalars().all()
    return list(rows)
