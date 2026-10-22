"""FactLama Observability query API (tenant-scoped reads/aggregates).

OBS-07's first slice now lives in `query/api/`: an authenticated
`GET /v0.1/ai/evaluations/{evaluation_id}` route. Most of
`docs/storage-query.md`'s target route list (`/ai/requests`,
`/ai/traces/{trace_id}`, `/ai/overview`, `/ai/models`, `/ai/reliability`,
`/ai/rag`, `/ai/tools`) still does not exist -- those need the canonical
`AITrace`/span contract this repo's ingress doesn't have yet either.
`query` may depend on `storage`; it must not depend on `collector` or
`sdk` (see the import-linter contracts in pyproject.toml) -- it is an
independent consumer of storage, not part of the ingestion path.
"""
