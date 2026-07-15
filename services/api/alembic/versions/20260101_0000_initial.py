"""Initial schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01

"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("account_status", sa.String(40), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_customers_phone", "customers", ["phone"], unique=True)

    op.create_table(
        "appointments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("confirmation_id", sa.String(40), nullable=False, unique=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("service", sa.String(80), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source_call_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_appointments_customer_id", "appointments", ["customer_id"])
    op.create_index("ix_appointments_source_call_id", "appointments", ["source_call_id"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "campaign_contacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("name", sa.String(160), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
    )
    op.create_index("ix_campaign_contacts_campaign_id", "campaign_contacts", ["campaign_id"])
    op.create_index("ix_campaign_contacts_phone", "campaign_contacts", ["phone"])
    op.create_index("ix_campaign_contacts_status", "campaign_contacts", ["status"])

    op.create_table(
        "calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_call_id", sa.String(64), nullable=False, unique=True),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("from_number", sa.String(32), nullable=False),
        sa.Column("to_number", sa.String(32), nullable=False),
        sa.Column("audio_format", sa.String(24), nullable=False, server_default="g711_ulaw"),
        sa.Column("sample_rate", sa.Integer(), nullable=False, server_default="8000"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("extra_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column(
            "campaign_contact_id",
            sa.String(36),
            sa.ForeignKey("campaign_contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_calls_provider_call_id", "calls", ["provider_call_id"], unique=True)
    op.create_index("ix_calls_direction", "calls", ["direction"])
    op.create_index("ix_calls_to_number", "calls", ["to_number"])
    op.create_index("ix_calls_started_at", "calls", ["started_at"])
    op.create_index("ix_calls_campaign_contact_id", "calls", ["campaign_contact_id"])

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "call_id",
            sa.String(36),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("provider_item_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("call_id", "provider_item_id", name="uq_transcript_call_item"),
    )
    op.create_index("ix_transcript_segments_call_id", "transcript_segments", ["call_id"])
    op.create_index("ix_transcript_segments_created_at", "transcript_segments", ["created_at"])

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "call_id",
            sa.String(36),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_tool_invocations_call_id", "tool_invocations", ["call_id"])
    op.create_index("ix_tool_invocations_name", "tool_invocations", ["name"])


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("transcript_segments")
    op.drop_table("calls")
    op.drop_table("campaign_contacts")
    op.drop_table("campaigns")
    op.drop_table("appointments")
    op.drop_table("customers")
    op.drop_table("users")
