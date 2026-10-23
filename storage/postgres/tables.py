"""SQLAlchemy Core table definitions for OBS-06's PostgreSQL storage
backend. Core (`Table`/`select`/`insert`), never the ORM/session layer --
mirrors `factlama-reliability/storage/postgres/tables.py`'s rationale: this
keeps `storage.backend`'s public signatures free of ORM sessions, and using
Core throughout removes the temptation to leak one. Only `storage/postgres/*`
may import this module (enforced by `pyproject.toml`'s import-linter
contracts); `storage/backend.py` knows nothing about these column names.

`reliability_events` is this pass's only table -- LOW_LEVEL_IMPLEMENTATION.md's
full column list, minus the fields (`interaction_id`/`trace_id`/`span_id`
joins against a `spans` table) that only matter once OBS-04's OTLP path and
OBS-06's `spans`/`evaluation_links` tables exist. Building those now would
mean guessing a shape ahead of a canonical `AITrace`/span contract, the same
reason OBS-03 already deferred that model.
"""

from sqlalchemy import Column, DateTime, Index, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

reliability_events = Table(
    "reliability_events",
    metadata,
    # contracts/v0.1: "Event identity is stable across retries, and
    # consumers deduplicate on (tenant_id,event_id)." The primary key alone
    # gives `record_reliability_event` its idempotent-replay guarantee.
    Column("tenant_id", String(128), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("evaluation_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=True),
    Column("application_id", String(128), nullable=True),
    Column("interaction_id", String(128), nullable=True),
    Column("trace_id", String(128), nullable=True),
    Column("span_id", String(128), nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("verdict", String(32), nullable=False),
    Column("scores_json", JSONB, nullable=False),
    Column("evaluator_version", String(64), nullable=False),
    Column("calibration_class", String(128), nullable=False),
    Column("calibration_classes_json", JSONB, nullable=True),
    Column("qualification_status", String(32), nullable=False),
    Column("usage_summary_json", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # storage-query.md: "Indexes include tenant and time before
    # high-cardinality IDs" -- supports a future evaluation-detail lookup
    # (OBS-07) without a full tenant scan.
    Index("ix_reliability_events_evaluation", "tenant_id", "evaluation_id"),
    # OBS-07's listing/aggregate routes (2026-10-23 G6 stage 3) filter and
    # keyset-paginate by (tenant_id, received_at); without this, every
    # bounded-time-window query would be a full per-tenant scan.
    Index("ix_reliability_events_tenant_received", "tenant_id", "received_at"),
)
