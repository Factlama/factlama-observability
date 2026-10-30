"""ADR-022: claim-level observability projection

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29

Mirrors `storage/postgres/tables.py` exactly -- see that module's
comments on `reliability_events.claim_total_count` and
`evaluation_claims` for the rationale.

`claim_total_count` is added nullable, which is also what makes this
migration safe on an existing database: every already-stored event
becomes NULL, i.e. "this event carried no claim projection", which is
precisely true of events written before ADR-022. Backfilling a 0 instead
would retroactively assert those evaluations had zero claims --
ADR-022's named false-metric failure.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reliability_events",
        sa.Column("claim_total_count", sa.Integer(), nullable=True),
    )
    op.create_table(
        "evaluation_claims",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_id", sa.String(128), primary_key=True),
        sa.Column("claim_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("application_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("rationale_code", sa.String(64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evaluation_claims_tenant_received",
        "evaluation_claims",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_claims_tenant_received", table_name="evaluation_claims")
    op.drop_table("evaluation_claims")
    op.drop_column("reliability_events", "claim_total_count")
