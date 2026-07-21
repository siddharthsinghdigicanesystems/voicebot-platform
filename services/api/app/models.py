"""SQLAlchemy ORM models.

Schema notes:

  - We use UUID primary keys everywhere (string column, app-generated). This
    keeps inserts ordered enough for B-tree indexes while letting the bridge,
    worker, and api all mint IDs without coordination.
  - `provider_call_id` (Tata's call SID) is uniquely indexed; we use it to
    deduplicate webhook events that may retry.
  - `transcript_segments` is append-only; each row carries `provider_item_id`
    so the bridge can be idempotent if it re-emits a segment.
  - `tool_invocations` are stored as JSONB so we don't lock in a schema for
    arguments / results — useful because we'll iterate on tool shapes often.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ---------------------------------------------------------------------------
# CRM (mock)
# ---------------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    account_status: Mapped[str] = mapped_column(String(40), default="active", nullable=False)
    # Hospital mock-CRM fields (optional; null/0 for non-patient records).
    patient_mrn: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="customer")
    lab_results: Mapped[list["LabResult"]] = relationship(back_populates="customer")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    confirmation_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    service: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # scheduled | confirmed | cancelled | completed | no_show
    status: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False, index=True)
    doctor: Mapped[str | None] = mapped_column(String(120))
    department: Mapped[str | None] = mapped_column(String(80))
    location: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    source_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="appointments")


class LabResult(Base):
    """Dummy lab / diagnostics status for hospital reception demos.

    The voice bot only reports readiness / delivery — never detailed clinical
    values. `result_summary` is a short non-diagnostic phrase for tools/UI.
    """

    __tablename__ = "lab_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    result_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    test_name: Mapped[str] = mapped_column(String(120), nullable=False)
    # pending | processing | ready | sent
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    result_summary: Mapped[str | None] = mapped_column(String(255))
    eta_ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    # email | sms | pickup | None
    delivered_via: Mapped[str | None] = mapped_column(String(40))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    ordered_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="lab_results")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider_call_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    direction: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    from_number: Mapped[str] = mapped_column(String(32), nullable=False)
    to_number: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    audio_format: Mapped[str] = mapped_column(String(24), default="g711_ulaw", nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, default=8000, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(32))  # completed | error | transferred ...
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    campaign_contact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("campaign_contacts.id", ondelete="SET NULL"), index=True
    )

    transcript: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="call",
        order_by="TranscriptSegment.created_at",
        cascade="all, delete-orphan",
    )
    tool_invocations: Mapped[list["ToolInvocation"]] = relationship(
        back_populates="call",
        order_by="ToolInvocation.created_at",
        cascade="all, delete-orphan",
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("call_id", "provider_item_id", name="uq_transcript_call_item"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("calls.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    text: Mapped[str] = mapped_column(Text, nullable=False)
    provider_item_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False, index=True)

    call: Mapped[Call] = relationship(back_populates="transcript")


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("calls.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    call: Mapped[Call] = relationship(back_populates="tool_invocations")


# ---------------------------------------------------------------------------
# Outbound campaigns
# ---------------------------------------------------------------------------


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    # draft | scheduled | running | paused | completed
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    retry_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    # Per-campaign bot configuration. All optional (NULL falls back to the
    # bridge-wide defaults). The bridge fetches these via
    # `/v1/campaigns/_contacts/{id}/bot_config` on outbound dial start.
    brand: Mapped[str | None] = mapped_column(String(120))
    system_prompt_override: Mapped[str | None] = mapped_column(Text)
    voice: Mapped[str | None] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    contacts: Mapped[list["CampaignContact"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignContact(Base):
    __tablename__ = "campaign_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL")
    )
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(160))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    # pending | dialing | succeeded | failed | abandoned
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))

    campaign: Mapped[Campaign] = relationship(back_populates="contacts")
