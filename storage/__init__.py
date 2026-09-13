"""FactLama Observability storage (tenant-keyed spans/events/projections).

OBS-01 scope: package layout, migration tooling, and dependency boundary
only. The `TelemetryStore` interfaces and PostgreSQL schema
(`spans`, `reliability_events`, `evaluation_links`, pricing/config tables)
described in LOW_LEVEL_IMPLEMENTATION.md are OBS-06 work and do not exist
yet. `storage/migrations/` is wired to Alembic against `DATABASE_URL` with
zero revisions -- `alembic upgrade head` succeeds as a no-op until OBS-06
adds the first real revision.
"""
