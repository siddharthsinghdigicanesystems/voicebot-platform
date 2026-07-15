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
from app.models import Appointment, Customer
from app.schemas import AppointmentIn, AppointmentOut, CustomerOut

log = get_logger(__name__)

router = APIRouter(prefix="/v1/crm", tags=["crm"])


def _customer_to_out(customer: Customer) -> dict[str, Any]:
    next_appt = None
    if customer.appointments:
        upcoming = [a for a in customer.appointments if a.scheduled_for > datetime.utcnow()]
        upcoming.sort(key=lambda a: a.scheduled_for)
        if upcoming:
            a = upcoming[0]
            next_appt = {
                "service": a.service,
                "date": a.scheduled_for.date().isoformat(),
                "time": a.scheduled_for.strftime("%H:%M"),
                "confirmation_id": a.confirmation_id,
            }
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "account_status": customer.account_status,
        "next_appointment": next_appt,
        "recent_orders": [],  # mock
    }


@router.get("/customers/by-phone", response_model=CustomerOut)
async def lookup_by_phone(
    phone: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_user_or_service),
) -> dict[str, Any]:
    customer = (
        await session.execute(
            select(Customer)
            .options(selectinload(Customer.appointments))
            .where(Customer.phone == phone)
        )
    ).scalar_one_or_none()
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
