"""OBS-07: index for event-time (occurred_at) listing/aggregate queries

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24

Mirrors `storage/postgres/tables.py` exactly -- see that module's comment
on `ix_reliability_events_tenant_occurred` for why this replaces
`ix_reliability_events_tenant_received` as the index the listing/
aggregate routes actually use (ADR-021: "Evaluation aggregate windows use
occurred_at, not received_at"). The received_at index from migration 0002
is kept, not dropped -- retention's own cutoff is still receipt-time.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_reliability_events_tenant_occurred",
        "reliability_events",
        ["tenant_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reliability_events_tenant_occurred", table_name="reliability_events")
