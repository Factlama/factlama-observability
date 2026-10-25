"""G6: interaction-only correlation

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25

Mirrors `storage/postgres/tables.py` exactly -- see that module's
comments on `evaluation_links.interaction_id` and the two new indexes
for the rationale (G6_FINAL_REVIEW_2026-10-24.md: "Interaction-only
unique/ambiguous matching ... [is] not established by the current
evaluation-ID-centric matching").
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evaluation_links", sa.Column("interaction_id", sa.String(128), nullable=True))
    op.create_index(
        "ix_evaluation_links_tenant_interaction",
        "evaluation_links",
        ["tenant_id", "interaction_id"],
    )
    op.create_index("ix_spans_tenant_interaction", "spans", ["tenant_id", "interaction_id"])


def downgrade() -> None:
    op.drop_index("ix_spans_tenant_interaction", table_name="spans")
    op.drop_index("ix_evaluation_links_tenant_interaction", table_name="evaluation_links")
    op.drop_column("evaluation_links", "interaction_id")
