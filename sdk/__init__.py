"""FactLama Observability SDK (client-side instrumentation).

OBS-01 scope: package layout and dependency boundary only. The public
surface described in ../factlama-architecture/factlama-observability's
LOW_LEVEL_IMPLEMENTATION.md (`FactLamaClient`, `verify`, `llm_span`,
`retrieval_span`, `tool_span`, the bounded best-effort export queue) is
OBS-13 work and does not exist yet. This package must stay independent of
`collector`, `storage`, and `query` (see the import-linter contracts in
pyproject.toml): it ships to a customer's process, those do not.
"""
