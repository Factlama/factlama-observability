"""OBS-07: index for time-bounded listing/aggregate queries

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23

Mirrors `storage/postgres/tables.py` exactly -- see that module's comment
on `ix_reliability_events_tenant_received` for why this is needed now that
OBS-07's listing/aggregate routes filter and keyset-paginate by
(tenant_id, received_at).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_reliability_events_tenant_received",
        "reliability_events",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_reliability_events_tenant_received", table_name="reliability_events")
