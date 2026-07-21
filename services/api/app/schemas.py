"""Pydantic request/response schemas.

Conventions:
  - Inputs end in `In`; outputs end in `Out`. Update payloads end in `Update`.
  - All schemas use ConfigDict(from_attributes=True) so we can return
    SQLAlchemy models directly from endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------


class CustomerOut(BaseModel):
    id: str
    name: str
    phone: str
    email: str | None = None
    account_status: str
    patient_mrn: str | None = None
    outstanding_balance: float = 0.0
    next_appointment: dict[str, Any] | None = None
    appointments: list[dict[str, Any]] = Field(default_factory=list)
    lab_results: list[dict[str, Any]] = Field(default_factory=list)
    recent_orders: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AppointmentIn(BaseModel):
    customer_id: str
    service: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM (24h)
    doctor: str | None = None
    department: str | None = None
    location: str | None = None
    notes: str | None = None
    source_call_id: str | None = None


class AppointmentRescheduleIn(BaseModel):
    date: str  # YYYY-MM-DD
    time: str  # HH:MM (24h)
    notes: str | None = None
    source_call_id: str | None = None


class AppointmentOut(BaseModel):
    id: str
    confirmation_id: str
    customer_id: str
    service: str
    scheduled_for: datetime
    status: str = "scheduled"
    doctor: str | None = None
    department: str | None = None
    location: str | None = None
    notes: str | None = None
    source_call_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class LabResultOut(BaseModel):
    id: str
    result_id: str
    customer_id: str
    test_name: str
    status: str
    result_summary: str | None = None
    eta_ready_at: datetime | None = None
    delivered_via: str | None = None
    delivered_at: datetime | None = None
    ordered_at: datetime
    notes: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


class CallStartIn(BaseModel):
    provider_call_id: str
    direction: Literal["inbound", "outbound"]
    from_number: str
    to_number: str
    audio_format: str = "g711_ulaw"
    sample_rate: int = 8000
    metadata: dict[str, Any] = Field(default_factory=dict)


class CallEndIn(BaseModel):
    outcome: str
    duration_seconds: float
    facts: dict[str, Any] = Field(default_factory=dict)


class TranscriptIn(BaseModel):
    role: Literal["user", "assistant", "system"]
    text: str
    provider_item_id: str | None = None


class TranscriptOut(BaseModel):
    role: str
    text: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ToolInvocationIn(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: Any


class ToolInvocationOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: Any
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CallSummaryOut(BaseModel):
    id: str
    provider_call_id: str
    direction: str
    from_number: str
    to_number: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    outcome: str | None
    facts: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class CallDetailOut(CallSummaryOut):
    transcript: list[TranscriptOut]
    tool_invocations: list[ToolInvocationOut]


class CallListOut(BaseModel):
    items: list[CallSummaryOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class CampaignContactIn(BaseModel):
    phone: str
    name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


SUPPORTED_VOICES = {"alloy", "echo", "shimmer", "verse", "ballad", "coral", "sage"}
SUPPORTED_LANGUAGES = {"en", "hi", "hinglish"}


class CampaignIn(BaseModel):
    name: str
    scheduled_at: datetime | None = None
    max_concurrency: int = 5
    retry_attempts: int = 2
    contacts: list[CampaignContactIn] = Field(default_factory=list)
    # Bot config overrides — all optional.
    brand: str | None = None
    system_prompt_override: str | None = None
    voice: str | None = None
    language: str = "en"


class CampaignContactOut(BaseModel):
    id: str
    phone: str
    name: str | None
    status: str
    attempts: int
    last_attempt_at: datetime | None
    last_error: str | None

    model_config = ConfigDict(from_attributes=True)


class CampaignOut(BaseModel):
    id: str
    name: str
    status: str
    scheduled_at: datetime | None
    max_concurrency: int
    retry_attempts: int
    created_at: datetime
    brand: str | None = None
    system_prompt_override: str | None = None
    voice: str | None = None
    language: str = "en"
    contacts_count: int = 0
    pending_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class CampaignBotConfigOut(BaseModel):
    """Sent to the bridge on outbound call start.

    `system_prompt_override` is a full text takeover; if NULL the bridge
    builds its prompt from the structured agent module using `brand` and
    `language`.
    """

    campaign_id: str
    brand: str | None = None
    system_prompt_override: str | None = None
    voice: str | None = None
    language: str = "en"
