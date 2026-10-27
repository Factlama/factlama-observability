"""FactLama Observability SDK (client-side instrumentation).

G7's vertical slice: `sdk.client.FactLamaClient` with
`submit_verification`/`get_verification_job` (2026-10-26), plus
`llm_span`/`retrieval_span`/`tool_span` context managers, a bounded
explicit-flush export queue with sampling and a drop counter
(`sdk.queue`), and capture-mode suppression (2026-10-27). W3C-format
trace/span ID generation lives in `sdk.ids`; OTLP/HTTP export (single-span
and batch) lives in `sdk.otlp_export`.

`verify` (the synchronous direct-call path) and a periodic background
export thread remain OBS-13 scope not yet built -- see
`sdk/client.py`'s and `sdk/queue.py`'s own module docstrings for why each
is deferred, not merely unstarted. This package must stay independent of
`collector`, `storage`, and `query` (see the import-linter contracts in
pyproject.toml): it ships to a customer's process, those do not.
"""
