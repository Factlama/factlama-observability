"""OBS-07's first slice: the authenticated tenant-scoped `/v0.1/ai/*` query
service (`docs/storage-query.md`), as its own process/service boundary --
distinct from `collector.ingress`'s internal workload-authenticated
ingestion path (`EXECUTION_PLAN.md`'s handoff table lists "Collector ->
Query API" and "Query API -> Dashboard" as separate boxes). Only
`GET /v0.1/ai/evaluations/{evaluation_id}` exists so far; the rest of
`docs/storage-query.md`'s target route list needs the canonical
`AITrace`/span contract (trace detail, overview, models, reliability, rag,
tools) that does not exist yet.
"""
