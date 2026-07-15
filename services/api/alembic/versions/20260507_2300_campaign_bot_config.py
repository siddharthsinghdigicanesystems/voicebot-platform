"""Add per-campaign bot config (brand, system_prompt_override, voice, language).

Revision ID: 0002_campaign_bot_config
Revises: 0001_initial
Create Date: 2026-05-07

Why these specific columns:
  - `brand` lets `agent.build_system_prompt(brand=...)` keep working when
    multiple business units share one deploy. Optional; falls back to the
    bridge-wide default.
  - `system_prompt_override` is a full-text takeover for the system prompt.
    For most campaigns the structured prompt is fine; this is the escape
    hatch for "we need a totally bespoke survey script" without code changes.
  - `voice` selects the OpenAI Realtime voice per campaign — different
    audiences get different voices. Constrained at the API to the supported
    set, not at the DB level (so swapping models doesn't require a migration).
  - `language` drives prompt selection (English / Hinglish / Hindi / ...).
    Free-form short string; the agent module picks a flow per language.

All four are nullable / default-empty so existing campaigns keep working
exactly as before.
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_campaign_bot_config"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("brand", sa.String(120), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column("system_prompt_override", sa.Text(), nullable=True),
    )
    op.add_column("campaigns", sa.Column("voice", sa.String(40), nullable=True))
    op.add_column(
        "campaigns",
        sa.Column("language", sa.String(8), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "language")
    op.drop_column("campaigns", "voice")
    op.drop_column("campaigns", "system_prompt_override")
    op.drop_column("campaigns", "brand")
