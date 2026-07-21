"""Hospital reception CRM: appointment details + lab_results.

Revision ID: 0003_hospital_crm
Revises: 0002_campaign_bot_config
Create Date: 2026-07-21

Adds fields needed for a dummy hospital reception flow:
  - patients: MRN + outstanding balance
  - appointments: status, doctor, department, location
  - lab_results: readiness / delivery status for test-result inquiries
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_hospital_crm"
down_revision: str | None = "0002_campaign_bot_config"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("patient_mrn", sa.String(40), nullable=True))
    op.add_column(
        "customers",
        sa.Column(
            "outstanding_balance",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_customers_patient_mrn", "customers", ["patient_mrn"], unique=True)

    op.add_column(
        "appointments",
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="scheduled",
        ),
    )
    op.add_column("appointments", sa.Column("doctor", sa.String(120), nullable=True))
    op.add_column("appointments", sa.Column("department", sa.String(80), nullable=True))
    op.add_column("appointments", sa.Column("location", sa.String(120), nullable=True))
    op.create_index("ix_appointments_status", "appointments", ["status"])

    op.create_table(
        "lab_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("result_id", sa.String(40), nullable=False, unique=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("test_name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("result_summary", sa.String(255), nullable=True),
        sa.Column("eta_ready_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_via", sa.String(40), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("ordered_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_lab_results_customer_id", "lab_results", ["customer_id"])
    op.create_index("ix_lab_results_status", "lab_results", ["status"])


def downgrade() -> None:
    op.drop_index("ix_lab_results_status", table_name="lab_results")
    op.drop_index("ix_lab_results_customer_id", table_name="lab_results")
    op.drop_table("lab_results")

    op.drop_index("ix_appointments_status", table_name="appointments")
    op.drop_column("appointments", "location")
    op.drop_column("appointments", "department")
    op.drop_column("appointments", "doctor")
    op.drop_column("appointments", "status")

    op.drop_index("ix_customers_patient_mrn", table_name="customers")
    op.drop_column("customers", "outstanding_balance")
    op.drop_column("customers", "patient_mrn")
