"""OBS-06 foundation: reliability_events

Revision ID: 0001
Revises:
Create Date: 2026-09-29

Mirrors `storage/postgres/tables.py` exactly; keep the two in lockstep by
hand (no autogenerate was run against a live database for this initial
revision, matching factlama-reliability's own 0001 migration).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reliability_events",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("application_id", sa.String(128), nullable=True),
        sa.Column("interaction_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("span_id", sa.String(128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("scores_json", JSONB, nullable=False),
        sa.Column("evaluator_version", sa.String(64), nullable=False),
        sa.Column("calibration_class", sa.String(128), nullable=False),
        sa.Column("calibration_classes_json", JSONB, nullable=True),
        sa.Column("qualification_status", sa.String(32), nullable=False),
        sa.Column("usage_summary_json", JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_reliability_events_evaluation",
        "reliability_events",
        ["tenant_id", "evaluation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_reliability_events_evaluation", table_name="reliability_events")
    op.drop_table("reliability_events")
