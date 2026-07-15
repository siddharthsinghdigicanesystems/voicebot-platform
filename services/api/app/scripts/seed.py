"""Idempotent seed: create admin + a few demo customers/appointments.

Safe to run on every container start. Existing rows are left alone.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.logging_setup import configure_logging, get_logger
from app.models import Appointment, Customer, User
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


_DEMO_CUSTOMERS = [
    {"name": "Priya Sharma", "phone": "+919812345678", "email": "priya@example.com"},
    {"name": "Rahul Kumar", "phone": "+919823456789", "email": "rahul@example.com"},
    {"name": "Anjali Patel", "phone": "+919834567890", "email": "anjali@example.com"},
    # Numbers the mock telephony page uses by default — make sure lookups succeed
    # for the out-of-the-box demo.
    {"name": "Demo Caller", "phone": "+911234567890", "email": "demo@example.com"},
]


async def _ensure_demo_customers() -> None:
    log = get_logger("seed")
    async with SessionLocal() as s:
        for c in _DEMO_CUSTOMERS:
            existing = (
                await s.execute(select(Customer).where(Customer.phone == c["phone"]))
            ).scalar_one_or_none()
            if existing:
                continue
            customer = Customer(name=c["name"], phone=c["phone"], email=c["email"])
            s.add(customer)
            await s.flush()
            # Give the demo caller an upcoming appointment to confirm.
            if c["phone"] == "+911234567890":
                s.add(
                    Appointment(
                        confirmation_id="APT-DEMO1",
                        customer_id=customer.id,
                        service="consultation",
                        scheduled_for=datetime.utcnow() + timedelta(days=2, hours=4),
                        notes="Demo appointment seeded for first-run experience.",
                    )
                )
        await s.commit()
        log.info("seed.demo_customers_ok", count=len(_DEMO_CUSTOMERS))


async def main() -> None:
    configure_logging(settings.log_level)
    await _ensure_admin()
    await _ensure_demo_customers()


if __name__ == "__main__":
    asyncio.run(main())
