"""SQLAlchemy Core table definitions for OBS-06's PostgreSQL storage
backend. Core (`Table`/`select`/`insert`), never the ORM/session layer --
mirrors `factlama-reliability/storage/postgres/tables.py`'s rationale: this
keeps `storage.backend`'s public signatures free of ORM sessions, and using
Core throughout removes the temptation to leak one. Only `storage/postgres/*`
may import this module (enforced by `pyproject.toml`'s import-linter
contracts); `storage/backend.py` knows nothing about these column names.

`spans`/`evaluation_links` (2026-10-24, ADR-021) store the normalized span
and evaluation-correlation state LOW_LEVEL_IMPLEMENTATION.md's table list
already named; building them earlier would have meant guessing a shape
ahead of the now-canonical `AITrace`/span contract, the same reason OBS-03
originally deferred the model.
"""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, MetaData, String, Table
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
    # ADR-022: NULL means this event carried no claim projection at all --
    # read downstream as "claim data unavailable", which is a different
    # state from 0 ("the evaluation genuinely had no claims"). Nullable is
    # load-bearing here, not laziness: collapsing the two would report a
    # clean reliability picture for producers that simply do not publish
    # claims, the false-metric outcome ADR-022 rejects by name.
    Column("claim_total_count", Integer, nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # storage-query.md: "Indexes include tenant and time before
    # high-cardinality IDs" -- supports a future evaluation-detail lookup
    # (OBS-07) without a full tenant scan.
    Index("ix_reliability_events_evaluation", "tenant_id", "evaluation_id"),
    # Retention's own cutoff (storage/postgres/backend.py::delete_reliability_events)
    # is receipt-time by nature ("how long have we stored this"), not
    # event-time; kept for that query, even though the listing/aggregate
    # routes below moved off it.
    Index("ix_reliability_events_tenant_received", "tenant_id", "received_at"),
    # OBS-07's listing/aggregate routes filter and keyset-paginate by
    # (tenant_id, occurred_at) -- event time, corrected 2026-10-24 per
    # ADR-021 from the original (tenant_id, received_at) above. Without
    # this, every bounded-time-window query would be a full per-tenant
    # scan.
    Index("ix_reliability_events_tenant_occurred", "tenant_id", "occurred_at"),
)

spans = Table(
    "spans",
    metadata,
    # ADR-021: (tenant_id, trace_id, span_id) is span identity.
    Column("tenant_id", String(128), primary_key=True),
    Column("trace_id", String(32), primary_key=True),
    Column("span_id", String(16), primary_key=True),
    Column("parent_span_id", String(16), nullable=True),
    Column("project_id", String(128), nullable=True),
    Column("application_id", String(128), nullable=True),
    Column("operation_type", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("interaction_id", String(128), nullable=True),
    Column("evaluation_id", String(128), nullable=True),
    Column("provider_id", String(128), nullable=True),
    Column("model_id", String(128), nullable=True),
    Column("model_version", String(128), nullable=True),
    Column("usage_json", JSONB, nullable=False),
    Column("resource_attributes_json", JSONB, nullable=False),
    Column("extension_attributes_json", JSONB, nullable=False),
    Column("mapping_version", String(64), nullable=False),
    Column("semconv_version", String(32), nullable=False),
    Column("schema_version", String(16), nullable=False),
    # ADR-021: "Duplicate detection compares canonical normalized content."
    # A SHA-256 hex digest of every persisted semantic field (never receipt
    # time/processing counters/link state) lets an exact replay be detected
    # as a cheap equality check instead of re-comparing every field on
    # every insert attempt.
    Column("content_hash", String(64), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Index("ix_spans_tenant_trace", "tenant_id", "trace_id"),
    Index("ix_spans_tenant_evaluation", "tenant_id", "evaluation_id"),
    # G6_FINAL_REVIEW_2026-10-24.md: interaction-only correlation needs to
    # find every span sharing one (tenant_id, interaction_id) to decide
    # whether exactly one distinct trace_id is scope-compatible.
    Index("ix_spans_tenant_interaction", "tenant_id", "interaction_id"),
)

evaluation_links = Table(
    "evaluation_links",
    metadata,
    # One link row per evaluation_id this process has any awareness of,
    # from either arrival order (ADR-021: "Persist first-receipt time and
    # absolute deadline for each unmatched evaluation event and each span
    # with explicit evaluation intent").
    Column("tenant_id", String(128), primary_key=True),
    Column("evaluation_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=True),
    Column("application_id", String(128), nullable=True),
    Column("trace_id", String(32), nullable=True),
    Column("span_id", String(16), nullable=True),
    # ADR-021: "Interaction-only matches remain pending when ambiguous;
    # do not guess a trace." Fixed forever at whatever the first toucher
    # supplied (`touch_link`'s same "scope fixed at first touch" rule),
    # so a link resolved via this interaction_id to one trace_id does not
    # silently re-resolve to a different trace later.
    Column("interaction_id", String(128), nullable=True),
    Column("state", String(32), nullable=False),
    Column("first_received_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("event_received", Boolean, nullable=False, server_default="false"),
    Column("span_received", Boolean, nullable=False, server_default="false"),
    Index("ix_evaluation_links_tenant_trace", "tenant_id", "trace_id"),
    Index("ix_evaluation_links_tenant_interaction", "tenant_id", "interaction_id"),
)

retired_evaluations = Table(
    "retired_evaluations",
    metadata,
    # F1 (review-evidence/g6-coverage-fix-review-2026-10-25): a bounded
    # *deletion* floor on the full `evaluation_links` row (above) is not
    # itself a replay admission boundary -- once that row is actually
    # gone, nothing stopped a replay of the same identity from creating
    # a brand-new PENDING link and completing normally, reviving a
    # correlation ADR-021 requires to stay permanently expired. This is
    # the "durable mechanism to reject retired identities" the review
    # names as the alternative to retaining the full marker forever: a
    # minimal (tenant_id, evaluation_id, retired_at) tombstone, written
    # the moment `delete_reliability_events` is about to remove a
    # RECONCILIATION_EXPIRED marker, and never deleted itself -- its
    # footprint (one identity pair plus a timestamp, no trace/span/scope
    # content) is negligible compared to the full row it replaces, so
    # retaining it permanently does not reopen the storage-growth problem
    # bounding the full marker's own retention was meant to solve.
    Column("tenant_id", String(128), primary_key=True),
    Column("evaluation_id", String(128), primary_key=True),
    Column("retired_at", DateTime(timezone=True), nullable=False),
)


evaluation_claims = Table(
    "evaluation_claims",
    metadata,
    # ADR-022: claim identity is (tenant, evaluation, claim) -- "a stable
    # claim identifier scoped to the evaluation". The primary key is what
    # makes a replayed event idempotent; `record_reliability_event`
    # compares an already-stored row's own status/rationale before
    # accepting, so a *conflicting* second assertion for this identity is
    # rejected rather than silently overwriting the first.
    #
    # Deliberately its own table rather than a JSONB blob on
    # `reliability_events`: the claim counts are aggregated across many
    # evaluations in one window (the Reliability view), and tenant/project
    # scoping has to be a real indexed predicate, not a JSON traversal.
    Column("tenant_id", String(128), primary_key=True),
    Column("evaluation_id", String(128), primary_key=True),
    Column("claim_id", String(128), primary_key=True),
    # Copied from the owning event so a scoped aggregate never has to join
    # back to `reliability_events` -- the same denormalization
    # `evaluation_links` already makes, and for the same reason: scope must
    # be filterable in SQL. Nullable exactly as the event's own are.
    Column("project_id", String(128), nullable=True),
    Column("application_id", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("rationale_code", String(64), nullable=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # The Reliability view aggregates every claim for a tenant (optionally
    # narrowed by project/application) in one window.
    Index("ix_evaluation_claims_tenant_received", "tenant_id", "received_at"),
)
