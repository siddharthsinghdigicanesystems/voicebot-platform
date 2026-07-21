"""Idempotent seed: admin + hospital reception demo patients / appointments / labs.

Safe to run on every container start. Existing rows are left alone (matched by
phone, confirmation_id, or result_id).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.logging_setup import configure_logging, get_logger
from app.models import Appointment, Customer, LabResult, User
from app.security import hash_password


async def _ensure_admin() -> None:
    log = get_logger("seed")
    async with SessionLocal() as s:
        existing = (
            await s.execute(select(User).where(User.username == settings.admin_username))
        ).scalar_one_or_none()
        if existing:
            return
        s.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
            )
        )
        await s.commit()
        log.info("seed.admin_created", username=settings.admin_username)


# Demo patients for CityCare Hospital reception scenarios.
# Phone +911234567890 is the mock-telephony default caller.
_DEMO_CUSTOMERS = [
    {
        "name": "Priya Sharma",
        "phone": "+919812345678",
        "email": "priya@example.com",
        "patient_mrn": "MRN-1001",
        "outstanding_balance": 0.0,
    },
    {
        "name": "Rahul Kumar",
        "phone": "+919823456789",
        "email": "rahul@example.com",
        "patient_mrn": "MRN-1002",
        "outstanding_balance": 2500.0,
    },
    {
        "name": "Anjali Patel",
        "phone": "+919834567890",
        "email": "anjali@example.com",
        "patient_mrn": "MRN-1003",
        "outstanding_balance": 0.0,
    },
    {
        "name": "Demo Caller",
        "phone": "+911234567890",
        "email": "demo@example.com",
        "patient_mrn": "MRN-DEMO1",
        "outstanding_balance": 750.0,
    },
]


async def _ensure_demo_customers() -> dict[str, str]:
    """Return phone -> customer_id for seeded patients."""
    log = get_logger("seed")
    phone_to_id: dict[str, str] = {}
    async with SessionLocal() as s:
        for c in _DEMO_CUSTOMERS:
            existing = (
                await s.execute(select(Customer).where(Customer.phone == c["phone"]))
            ).scalar_one_or_none()
            if existing:
                # Keep demo hospital fields in sync for known seed phones.
                existing.patient_mrn = c["patient_mrn"]
                existing.outstanding_balance = c["outstanding_balance"]
                if c.get("email") and not existing.email:
                    existing.email = c["email"]
                phone_to_id[c["phone"]] = existing.id
                continue
            customer = Customer(
                name=c["name"],
                phone=c["phone"],
                email=c["email"],
                patient_mrn=c["patient_mrn"],
                outstanding_balance=c["outstanding_balance"],
            )
            s.add(customer)
            await s.flush()
            phone_to_id[c["phone"]] = customer.id
        await s.commit()
        log.info("seed.demo_customers_ok", count=len(_DEMO_CUSTOMERS))
    return phone_to_id


def _demo_appointments(now: datetime) -> list[dict]:
    return [
        # Demo Caller — main inbound "confirm my appointment" scenario
        {
            "phone": "+911234567890",
            "confirmation_id": "APT-DEMO1",
            "service": "cardiology consultation",
            "scheduled_for": now + timedelta(days=2, hours=4),
            "status": "scheduled",
            "doctor": "Dr. Mehta",
            "department": "Cardiology",
            "location": "OPD Wing B, Floor 2, Room 204",
            "notes": "Bring prior ECG reports if available.",
        },
        # Demo Caller — past completed visit (for history)
        {
            "phone": "+911234567890",
            "confirmation_id": "APT-DEMO0",
            "service": "general checkup",
            "scheduled_for": now - timedelta(days=14),
            "status": "completed",
            "doctor": "Dr. Rao",
            "department": "General Medicine",
            "location": "OPD Wing A, Floor 1",
            "notes": None,
        },
        # Priya — already confirmed follow-up
        {
            "phone": "+919812345678",
            "confirmation_id": "APT-PRIYA1",
            "service": "follow-up visit",
            "scheduled_for": now + timedelta(days=1, hours=3),
            "status": "confirmed",
            "doctor": "Dr. Kapoor",
            "department": "Orthopedics",
            "location": "OPD Wing C, Floor 3",
            "notes": "Post-physiotherapy review.",
        },
        # Rahul — needs reschedule / cancel demo; has dues
        {
            "phone": "+919823456789",
            "confirmation_id": "APT-RAHUL1",
            "service": "ENT consultation",
            "scheduled_for": now + timedelta(days=3, hours=2),
            "status": "scheduled",
            "doctor": "Dr. Singh",
            "department": "ENT",
            "location": "OPD Wing A, Floor 2",
            "notes": "Pending clearance of outstanding balance before visit.",
        },
        # Anjali — future booking
        {
            "phone": "+919834567890",
            "confirmation_id": "APT-ANJALI1",
            "service": "dermatology consultation",
            "scheduled_for": now + timedelta(days=7, hours=5),
            "status": "scheduled",
            "doctor": "Dr. Nair",
            "department": "Dermatology",
            "location": "OPD Wing D, Floor 1",
            "notes": None,
        },
    ]


def _demo_lab_results(now: datetime) -> list[dict]:
    return [
        # Demo Caller — pending CBC with ETA
        {
            "phone": "+911234567890",
            "result_id": "LAB-CBC1",
            "test_name": "Complete Blood Count (CBC)",
            "status": "pending",
            "result_summary": None,
            "eta_ready_at": now + timedelta(hours=18),
            "delivered_via": None,
            "delivered_at": None,
            "ordered_at": now - timedelta(hours=6),
            "notes": "Sample collected at CityCare Lab Desk.",
        },
        # Demo Caller — X-ray ready and emailed
        {
            "phone": "+911234567890",
            "result_id": "LAB-XRAY1",
            "test_name": "Chest X-Ray",
            "status": "sent",
            "result_summary": "Report available; sent to registered email.",
            "eta_ready_at": None,
            "delivered_via": "email",
            "delivered_at": now - timedelta(hours=2),
            "ordered_at": now - timedelta(days=1),
            "notes": "Sent to demo@example.com",
        },
        # Demo Caller — lipid profile processing
        {
            "phone": "+911234567890",
            "result_id": "LAB-LIPID1",
            "test_name": "Lipid Profile",
            "status": "processing",
            "result_summary": None,
            "eta_ready_at": now + timedelta(days=1, hours=4),
            "delivered_via": None,
            "delivered_at": None,
            "ordered_at": now - timedelta(hours=20),
            "notes": "Fasting sample.",
        },
        # Priya — ready for hospital pickup
        {
            "phone": "+919812345678",
            "result_id": "LAB-PRIYA1",
            "test_name": "Vitamin D",
            "status": "ready",
            "result_summary": "Report ready for pickup at Lab Desk.",
            "eta_ready_at": None,
            "delivered_via": "pickup",
            "delivered_at": None,
            "ordered_at": now - timedelta(days=2),
            "notes": "Collect from Lab Desk, Ground Floor, with ID proof.",
        },
        # Rahul — sent via SMS
        {
            "phone": "+919823456789",
            "result_id": "LAB-RAHUL1",
            "test_name": "Blood Sugar (FBS)",
            "status": "sent",
            "result_summary": "Report available; sent via SMS link.",
            "eta_ready_at": None,
            "delivered_via": "sms",
            "delivered_at": now - timedelta(hours=10),
            "ordered_at": now - timedelta(days=1, hours=6),
            "notes": None,
        },
        # Anjali — still pending
        {
            "phone": "+919834567890",
            "result_id": "LAB-ANJALI1",
            "test_name": "Thyroid Panel (TSH/T3/T4)",
            "status": "pending",
            "result_summary": None,
            "eta_ready_at": now + timedelta(days=2),
            "delivered_via": None,
            "delivered_at": None,
            "ordered_at": now - timedelta(hours=4),
            "notes": "ETA two working days.",
        },
    ]


async def _ensure_demo_appointments(phone_to_id: dict[str, str]) -> None:
    log = get_logger("seed")
    now = datetime.utcnow()
    async with SessionLocal() as s:
        created = 0
        updated = 0
        for row in _demo_appointments(now):
            existing = (
                await s.execute(
                    select(Appointment).where(
                        Appointment.confirmation_id == row["confirmation_id"]
                    )
                )
            ).scalar_one_or_none()
            if existing:
                # Refresh demo metadata without clobbering a live confirm/cancel.
                existing.service = row["service"]
                existing.doctor = row["doctor"]
                existing.department = row["department"]
                existing.location = row["location"]
                if existing.status not in {"confirmed", "cancelled", "completed"}:
                    existing.status = row["status"]
                    existing.scheduled_for = row["scheduled_for"]
                updated += 1
                continue
            customer_id = phone_to_id.get(row["phone"])
            if not customer_id:
                continue
            s.add(
                Appointment(
                    confirmation_id=row["confirmation_id"],
                    customer_id=customer_id,
                    service=row["service"],
                    scheduled_for=row["scheduled_for"],
                    status=row["status"],
                    doctor=row["doctor"],
                    department=row["department"],
                    location=row["location"],
                    notes=row["notes"],
                )
            )
            created += 1
        await s.commit()
        log.info("seed.demo_appointments_ok", created=created, updated=updated)


async def _ensure_demo_lab_results(phone_to_id: dict[str, str]) -> None:
    log = get_logger("seed")
    now = datetime.utcnow()
    async with SessionLocal() as s:
        created = 0
        for row in _demo_lab_results(now):
            existing = (
                await s.execute(
                    select(LabResult).where(LabResult.result_id == row["result_id"])
                )
            ).scalar_one_or_none()
            if existing:
                continue
            customer_id = phone_to_id.get(row["phone"])
            if not customer_id:
                continue
            s.add(
                LabResult(
                    result_id=row["result_id"],
                    customer_id=customer_id,
                    test_name=row["test_name"],
                    status=row["status"],
                    result_summary=row["result_summary"],
                    eta_ready_at=row["eta_ready_at"],
                    delivered_via=row["delivered_via"],
                    delivered_at=row["delivered_at"],
                    ordered_at=row["ordered_at"],
                    notes=row["notes"],
                )
            )
            created += 1
        await s.commit()
        log.info("seed.demo_lab_results_ok", created=created)


async def main() -> None:
    configure_logging(settings.log_level)
    await _ensure_admin()
    phone_to_id = await _ensure_demo_customers()
    await _ensure_demo_appointments(phone_to_id)
    await _ensure_demo_lab_results(phone_to_id)


if __name__ == "__main__":
    asyncio.run(main())
