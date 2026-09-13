# Observability MVP: low-level implementation and review checklist

**Status: specification only; no runtime code is claimed.** Read the architecture [implementation map](https://github.com/Factlama/factlama-architecture/blob/main/IMPLEMENTATION_MAP.md) and [wire contract](https://github.com/Factlama/factlama-architecture/blob/main/CONTRACTS.md) first. `docs/implementation.md` owns OBS task status. MVP is OBS-01–08, 11–14; alerts/exporters get a minimal interface but full delivery is later. This spec tells a reviewer what to inspect in SDK, collector, processing, storage/query and dashboard.

## Code ownership and dependency flow

```text
Python SDK -> OTLP/HTTP traces -> collector ingress -> validate/redact -> normalize -> PostgreSQL
           -> Reliability API (explicit verify call)             |                  |
Reliability outbox -> authenticated reliability-event ingress ----+                  v
                                                        tenant-scoped Query API -> TypeScript dashboard
```

| Unit | Implement | Must not do |
|---|---|---|
| `sdk/python` | Explicit verify client and context-managed LLM/retrieval/tool instrumentation | Block customer inference on telemetry outage or capture raw content by default |
| `collector/ingress` | OTLP/HTTP trace receiver and authenticated internal ReliabilityEvent receiver | Trust tenant attributes supplied by clients |
| `collector/processing` | Redact, validate, normalize, deduplicate, correlate, derive usage/cost | Recompute factual verdicts or treat similar text as truth |
| `storage` | Migrations, tenant-keyed spans/events/projections and retention | Mix raw interaction bodies into telemetry rows |
| `query` | Authorized, time-bounded, cursor-based reads and aggregates | Let dashboard access DB directly |
| `dashboard` | Overview, Requests/Trace, Reliability, Models and honest states | Render missing cost/evaluation as zero/PASS |
| `operations` | Health, metrics, readiness, seed/sample app and Compose smoke test | Claim readiness while storage is unavailable |

Use maintained OpenTelemetry libraries for W3C context and OTLP transport. Do not write a parallel trace protocol. Define a versioned mapping from recognized incoming OTel GenAI/SDK attributes to internal fields; retain bounded allowlisted extensions for debugging. Unknown attributes cannot become metric labels by default.

## Python SDK: public surface and exact behavior

The first installable package exposes `FactLamaClient(config)`, `client.verify(request)`, `client.submit_verification(request, idempotency_key)`, `client.get_verification/job`, and context managers `client.llm_span(...)`, `client.retrieval_span(...)`, `client.tool_span(...)`. `config` requires collector URL, Reliability API URL, credential reference/value, project/application IDs; optional capture mode defaults `METADATA_ONLY`, queue capacity, batch size, flush interval, request timeout, sampling rate and service name. Do not add framework auto-instrumentation until explicit wrappers work and have tests. Both APIs preserve caller W3C trace context; direct verification attaches current interaction/trace/span IDs.

`llm_span` starts a span with operation, provider, model, request ID and start time. The caller records output token counts, status and error class before closing. `retrieval_span` records source name, document count/reference IDs and latency, not document bodies. `tool_span` records tool name, success/error and latency, not arguments/results. Each top-level request gets one opaque `interaction_id`; nested spans inherit it. `verify` uses the shared request schema, not an SDK-specific result model. It may be synchronous; telemetry export is independently best effort. `submit_verification` must send the same idempotency key on retries.

The SDK queue is bounded in memory. When full or offline, it drops/samples telemetry according to documented policy, increments a local drop counter and returns control promptly; it never retries without limit or writes raw content to disk. `flush(timeout)` is explicit for short-lived processes, and shutdown waits only up to its bound. Capture controls filter prompts, answers, evidence, tool payloads, headers and credentials **before** enqueue. A requested capture mode more permissive than tenant policy is rejected at ingress. Never include auth tokens in span attributes or exception text.

SDK reviewer test: run the sample LLM/RAG wrapper and inspect a trace with one LLM span, one retrieval span, one tool span and a linked `evaluation_id`. Disable collector during a second request: application result remains unchanged, SDK queue stays bounded, drop counter rises, and no secret/content appears in debug output.

The published example should look approximately like this; actual constructor names must match the released SDK and generated API docs:

```python
client = FactLamaClient(project_id="p-demo", application_id="a-rag", capture_mode="METADATA_ONLY")
with client.llm_span(provider="example", model="model-a") as llm:
    with client.retrieval_span(source="kb") as retrieval:
        evidence = search("When was Acme founded?")
        retrieval.set_result(document_ids=["doc-1"], count=1)
    answer = generate(evidence)
    llm.set_usage(input_tokens=120, output_tokens=18)
    result = client.verify({"schema_version": "0.1", "request_id": "req-1", "project_id": "p-demo", "application_id": "a-rag", "answer": answer, "evidence": evidence})
```

The example is illustrative documentation, not implemented Python. `verify` should attach current interaction/trace/span IDs when absent, and reject a conflicting supplied ID rather than silently overwriting it.

## Collector: ingress and processing stages

Expose OTLP/HTTP `/v1/traces` using an OTel-compatible receiver, plus authenticated internal `POST /internal/v0.1/reliability-events` for the shared event JSON. The latter is accessible only to the Reliability workload identity and returns success for replay of the same `(tenant_id,event_id)`. The OTLP receiver authenticates an SDK credential before accepting payload; the credential binds tenant, project and allowed application. Reject a conflicting resource/body tenant or unauthorized project/application. Enforce configured compressed/uncompressed body size, span count, attribute count/value length and timestamp skew before durable writes. Return 400 for malformed data, 401/403 for identity/scope, 413 for size, 429 for tenant quota, 503 for unavailable durable acceptance; include retry hints only for transient conditions. Never return a success response for data that can be lost after a process crash unless the SDK contract explicitly labels it best-effort acceptance.

Processing order: authenticate -> authorize -> decode/limits -> strip or redact governed fields -> validate IDs/timestamps -> map semantic attributes -> assign stable ingest key -> persist span/event -> update projections -> emit self-metrics. No raw content enters logs or a durable queue before filtering. The mapping stores source attribute name, target field and mapping version; input and output token counts must be nonnegative integers, latency is computed from span times when valid, and missing counts remain unknown. Price calculation uses a versioned tenant-approved price table and currency; unknown model/price means cost `UNAVAILABLE`, not zero. Errors use normalized error class plus bounded sanitized message.

Use a unique span ingest key `(tenant_id,trace_id,span_id)` with an update/version rule for late span completion; duplicate identical delivery is a no-op. Preserve start/end timestamps and parent ID. Reject impossible negative duration or mark invalid timing with a reason; do not silently clamp. Reliability events use unique `(tenant_id,event_id)` and upsert/link by `evaluation_id`, plus interaction/trace/span IDs. Events may precede traces: store them as unmatched and reconcile when spans arrive. A bounded sweeper records stale unmatched events; it does not discard audit-relevant metadata silently. A trace without event is `NOT_EVALUATED` or `PENDING` only when an evaluation was explicitly requested. This is a join, not an evaluative inference.

Backpressure: bounded request concurrency, per-tenant quota and DB pool. On saturation, reject with 429/503 rather than growing an unbounded queue. Downstream exporter failures do not roll back accepted local data; retry with cap and report drops. Readiness fails when durable storage cannot accept data. Self-metrics include accepted/rejected spans by reason, redaction failures, processing latency, DB latency, unmatched events, event duplicates, queue/pool saturation and exporter retries.

## PostgreSQL tables and query projections

Migrations create at least: `spans(tenant_id,trace_id,span_id,parent_span_id,interaction_id,project_id,application_id,operation,start_at,end_at,status,provider,model,input_tokens,output_tokens,cost,currency,pricing_version,attributes_json)`, `reliability_events(tenant_id,event_id,evaluation_id,interaction_id,trace_id,span_id,occurred_at,status,verdict,scores_json,evaluator_version)`, `evaluation_links(tenant_id,evaluation_id,trace_id,interaction_id,link_state)`, and tenant-scoped pricing/config tables. Unique constraints enforce span/event dedup. Index `(tenant_id,start_at)`, `(tenant_id,trace_id)`, `(tenant_id,interaction_id)`, `(tenant_id,evaluation_id)` and due reconciliation work. Avoid indexing unbounded raw attributes. Project/application authorization applies on every read and write. Retention deletes source rows and derived projections by tenant/data class, with documented lag. No prompt, answer, evidence or tool body column exists in telemetry tables.

The API exposes the routes in [storage-query.md](storage-query.md). All list/aggregate routes require UTC `from`/`to`, a maximum window and bounded `limit`; cursor encodes tenant, filters, sort key and expiry and is authenticated, not client-editable. Order requests by `(start_at DESC, trace_id, span_id)` for stable pagination. Aggregates include denominator/sample count and units. Request rate is count per bucket divided by bucket duration; error rate is failed requests/known-status requests; latency uses completed valid-duration operations only; token totals use known counts and report unknown count; cost totals group currency/pricing version or explicitly state mixed prices, never add different currencies. Reliability coverage uses distinct requests with available evaluation / eligible requests and separately counts pending, abstained, failed and not evaluated. Model view groups by provider/model/version and retains unknown bucket. These formulas should be tested with late and duplicate spans/events.

| Query route | Minimum response projection |
|---|---|
| `GET /v0.1/ai/requests` | Paged top-level interactions with IDs, timestamp, operation, model/provider, status, latency, tokens, cost status and evaluation state |
| `GET /v0.1/ai/traces/{trace_id}` | Ordered parent/child spans, interaction ID, retrieval/tool metadata and evaluation links |
| `GET /v0.1/ai/evaluations/{evaluation_id}` | Link state plus delegated sanitized Reliability result or explicit unavailable state |
| `GET /v0.1/ai/overview` | Window/bucket request rate, errors, latency percentiles, tokens, cost and reliability coverage |
| `GET /v0.1/ai/models` | Provider/model/version groups with request, error, latency, token and cost aggregates |
| `GET /v0.1/ai/reliability` | Verdict/status counts, claim-label counts, measured score distributions and evaluator versions |
| `GET /v0.1/ai/rag`, `/v0.1/ai/tools` | Retrieval/tool counts, latency, error/status metadata; no unimplemented quality score |

Example request-list item (inside the list envelope):

```json
{"trace_id":"0123456789abcdef0123456789abcdef","interaction_id":"int-1","started_at":"2026-09-13T00:00:00Z","operation":"llm.chat","provider":"example","model":"model-a","status":"OK","latency_ms":420,"input_tokens":120,"output_tokens":18,"cost":{"status":"UNAVAILABLE"},"evaluation":{"state":"AVAILABLE","evaluation_id":"eval-1","verdict":"PARTIAL"}}
```

This item is a projection, not the original OTLP span. If tokens or latency are missing, the field is omitted or explicitly unavailable; zero is reserved for a measured zero.

Response envelopes: list `{schema_version,items,next_cursor,window,as_of}`; aggregate `{schema_version,window,as_of,filters,sample_count,metrics}`. Detail includes trace/span tree, retrieval/tool metadata, evaluation link state and authorized claim-level result projection. Missing/inaccessible trace or evaluation returns the same 404. For claim detail, Query API calls Reliability's `GET /v0.1/verifications/{evaluation_id}` with workload authentication and a delegated, validated tenant/project/content scope; Reliability rechecks authorization and returns the sanitized result. Query API caches no raw result body. Reliability outage yields an explicit detail-unavailable state while telemetry remains readable. Never expose raw content solely because an ID is known.

## Dashboard: routes, cells and states

Implement a TypeScript single-page UI with authenticated tenant/project context and these routes: `/overview`, `/requests`, `/requests/:traceId`, `/reliability`, `/models`. Filters (time window, project, application, provider, model, status/verdict) are represented in URL query state so links are reproducible. All views use the Query API, cancel stale requests on filter changes, show loading/empty/error separately, and display `as_of` freshness and active filters. A 401 prompts sign-in; a 403 shows access denied; a 404 on a detail route shows a resource-not-found state. No direct database, collector or provider call from the browser.

| View | Required visible cells | Reviewer interaction |
|---|---|---|
| Overview | request count/rate, error rate, latency p50/p95, known/unknown tokens, cost with currency/pricing status, reliability coverage | Change time/project; all cells update from same window and show denominator |
| Requests | paged timestamp, operation, provider/model, latency, tokens, cost status, error, evaluation state | Filter and open a row; cursor does not duplicate/skip rows |
| Trace detail | span tree/timeline, LLM/retrieval/tool metadata, correlation IDs, evaluation state and link | Follow evaluation to claim findings; absent/pending/abstained distinct |
| Reliability | PASS/PARTIAL/FAIL/ABSTAIN counts, unsupported/contradicted claim counts, measured-score distribution, evaluator version | Filter by model/version and inspect exact source evaluation |
| Models | provider/model/version request count, error rate, latency, tokens and cost provenance | Unknown cost stays visibly unknown; mixed currency not summed |

RAG/Agent panels in the initial navigation show only measured retrieval/tool metadata; advanced quality scores are hidden or labeled unavailable until Reliability implements their evaluators. Claim text is shown only when the result has `text_status=AVAILABLE` and the user has content permission; `NOT_STORED` shows a clear label. Use semantic HTML and keyboard-operable tables/filters. Chart tooltips expose unit, count and time window. Do not imply a tiny sample is a stable trend. Empty means a successful query with zero rows; error means query failed; pending means data is expected later.

## End-to-end reviewer script

1. Start Compose from a clean checkout; migrations and seeded nonproduction tenant succeed. `/health/live` is up and `/health/ready` becomes ready only after DB checks.
2. Run the sample instrumented request and explicit verification. Inspect the SDK event: no raw content in default mode, valid W3C trace context, stable interaction ID.
3. Inspect collector acceptance and DB rows: one LLM/retrieval/tool span each, tenant-keyed, no raw content, known token/latency fields, cost either versioned or unavailable.
4. Inspect query/API/dashboard: one request in Overview/Requests; Trace detail links the correct evaluation; Reliability shows `PARTIAL` and the two claim findings from the worked fixture. Refresh or replay the event; counts remain unchanged.
5. Send reliability event before trace, then trace: link resolves. Send a trace with no evaluation: UI reports not evaluated. Send a provider timeout result: UI reports abstained, not FAIL.
6. Use a second tenant credential and try the first tenant's trace ID, evaluation ID and cursor: no data or existence leaks. Disconnect DB: ingress fails ready/acceptance safely while SDK customer work continues. Restore DB and confirm recovery.

Passing a visual screenshot alone does not prove implementation. Reviewers should inspect API responses, persistence, duplicate behavior, content suppression and the automated tests corresponding to each step.
