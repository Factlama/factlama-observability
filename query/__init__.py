"""FactLama Observability query API (tenant-scoped reads/aggregates).

OBS-01 scope: package layout and dependency boundary only. The
`/v0.1/ai/*` routes described in LOW_LEVEL_IMPLEMENTATION.md are OBS-07
work and do not exist yet. `query` may depend on `storage`; it must not
depend on `collector` or `sdk` (see the import-linter contracts in
pyproject.toml) -- it is an independent consumer of storage, not part of
the ingestion path.
"""
