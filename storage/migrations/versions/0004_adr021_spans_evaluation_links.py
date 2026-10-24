"""ADR-021: spans and evaluation_links

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24

Mirrors `storage/postgres/tables.py` exactly -- see that module's
comments on `spans`/`evaluation_links` for the rationale.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spans",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("trace_id", sa.String(32), primary_key=True),
        sa.Column("span_id", sa.String(16), primary_key=True),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("application_id", sa.String(128), nullable=True),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("interaction_id", sa.String(128), nullable=True),
        sa.Column("evaluation_id", sa.String(128), nullable=True),
        sa.Column("provider_id", sa.String(128), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("usage_json", JSONB, nullable=False),
        sa.Column("resource_attributes_json", JSONB, nullable=False),
        sa.Column("extension_attributes_json", JSONB, nullable=False),
        sa.Column("mapping_version", sa.String(64), nullable=False),
        sa.Column("semconv_version", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_spans_tenant_trace", "spans", ["tenant_id", "trace_id"])
    op.create_index("ix_spans_tenant_evaluation", "spans", ["tenant_id", "evaluation_id"])

    op.create_table(
        "evaluation_links",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("evaluation_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("application_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("span_id", sa.String(16), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_received", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("span_received", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index(
        "ix_evaluation_links_tenant_trace", "evaluation_links", ["tenant_id", "trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_links_tenant_trace", table_name="evaluation_links")
    op.drop_table("evaluation_links")
    op.drop_index("ix_spans_tenant_evaluation", table_name="spans")
    op.drop_index("ix_spans_tenant_trace", table_name="spans")
    op.drop_table("spans")
