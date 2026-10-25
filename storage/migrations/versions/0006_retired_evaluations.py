"""F1: retired-evaluations tombstone

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25

Mirrors `storage/postgres/tables.py` exactly -- see that module's
comment on `retired_evaluations` for the rationale
(review-evidence/g6-coverage-fix-review-2026-10-25).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retired_evaluations",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_id", sa.String(128), primary_key=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("retired_evaluations")
