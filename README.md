# FactLama Observability

FactLama Observability is the AI-native telemetry and dashboard layer for LLM, RAG and agentic applications.

The system-level source of truth lives in `Factlama/factlama-architecture`. This repository implements the observability contracts and must not silently redefine system boundaries.

## Product responsibility

Observability answers:

- What AI requests happened?
- Which model/provider/prompt version handled them?
- How long did they take?
- How many tokens were consumed and what did they cost?
- Which retrieval/tool operations occurred?
- Which requests failed and why?
- What reliability result was produced?
- Which trace/span/evaluation belong to the same interaction?
- Which model/prompt/retrieval change introduced a regression?

## Positioning

FactLama Observability is a lightweight, self-hosted, AI-specific observability product for teams that may not already operate Grafana, Splunk, Datadog, ELK or similar stacks.

It should also integrate cleanly with enterprise observability environments.

Core principle: **simple by default, interoperable by design**.

## Startup experience

The target developer experience is close to:

```text
docker compose up
```

A local deployment should expose a collector/ingestion path, storage, query API and native dashboard without requiring Kafka, Kubernetes or a large external monitoring stack.

## Target module layout

```text
factlama-observability/
├── sdk/
├── collector/
├── telemetry/
├── ingestion/
├── processing/
├── storage/
├── query/
├── alerts/
├── exporters/
├── dashboard/
├── integrations/
├── tests/
└── docs/
```

This is a target structure. Create modules as vertical slices require them rather than generating empty architecture.

## Logical architecture

1. Applications emit OpenTelemetry-compatible data and/or FactLama SDK events.
2. Collector/ingestion validates, normalizes and enriches incoming data.
3. Processing derives AI semantic events and usage/reliability signals.
4. Storage abstractions persist traces, metrics/events and correlation metadata.
5. Query APIs expose dashboard-oriented models.
6. Reliability results are joined by stable interaction/trace/evaluation identifiers.
7. Native dashboard renders AI overview, LLM, reliability, RAG, agent/tool, cost and trace views.
8. Exporters can forward data/signals to existing enterprise observability systems.

## OpenTelemetry boundary

OpenTelemetry concepts are first-class:

- Trace
- Span
- Event
- Metric
- Attribute
- Resource

FactLama should use OTEL-compatible conventions where useful but must not make the dashboard directly query a specific OTEL backend. A FactLama query/storage boundary keeps implementations replaceable.

## Initial AI semantic model

Support concepts such as:

- `llm.model`
- `llm.provider`
- `llm.prompt_tokens`
- `llm.completion_tokens`
- `llm.total_tokens`
- `llm.cost`
- `llm.temperature`
- `ai.operation`
- request success/failure
- retrieval count/latency/reference metadata
- tool name/success/latency
- `ai.reliability.groundedness`
- `ai.reliability.hallucination_risk`
- `ai.reliability.citation_support`
- evaluation verdict
- evaluator version

Semantic naming should be versioned and reviewed against evolving OTEL conventions rather than assumed permanent.

## Shared correlation model

Where available, telemetry should preserve:

- tenant ID;
- project/application ID;
- interaction ID;
- trace ID;
- span ID;
- evaluation ID;
- model version;
- prompt version;
- evaluator version;
- policy version.

These IDs are the bridge between Observability and Reliability.

## Storage strategy

Do not build a custom time-series database.

Use storage interfaces so the MVP can choose a small operational footprint and enterprise deployments can substitute approved backends later.

Conceptual abstractions:

- `TelemetryStore`
- `TraceStore`
- `Metrics/EventStore`
- `QueryRepository`

Raw interaction content is not required for telemetry storage. Future optional content storage belongs behind the Interaction Store architecture and separate capture controls.

## Native dashboard

The dashboard is part of the product, not a demo.

### AI Overview

Show:

- request volume;
- success/error rate;
- latency distributions;
- token usage;
- estimated cost;
- model/provider usage;
- reliability summary;
- recent regressions/anomalies when available.

### LLM Performance

Show requests by model/provider/version, latency, token counts, cost and failures.

### Reliability

Show groundedness, hallucination risk, contradiction/citation/instruction/tool signals, verdict distributions, evaluator versions and drill-down to claim-level results where exposed by Reliability.

### RAG

Foundation should support retrieval request count, retrieval latency, document/reference metadata, context quality metrics and answer-groundedness correlation.

### Agents and Tools

Foundation should support agent run status, tool-call count, tool success/failure, latency, cost and evaluation findings.

### Trace Explorer

A trace should make it possible to understand an interaction sequence such as request -> retrieval -> model -> tool -> evaluation -> response, with timing and correlation metadata.

## Multi-tenancy and data governance

P0 requirements:

- tenant-aware ingestion, storage and query;
- no cross-tenant dashboard/query leakage;
- configurable capture of interaction content;
- metadata-only operation;
- redaction before optional content persistence;
- configurable retention interfaces;
- no credentials/secrets in telemetry attributes;
- bounded attribute/event sizes;
- self-observability for dropped/rejected/failed telemetry.

## MVP end product

A developer starts the local stack, instruments a sample LLM/RAG app and sends traffic. The dashboard then shows:

- requests/traces;
- model/provider information;
- request latency and errors;
- token usage and estimated cost;
- retrieval/tool events when emitted;
- reliability verdict and score signals produced by FactLama Reliability;
- a trace view that correlates the interaction and evaluation.

The same information must be accessible through documented query APIs, not dashboard-only logic.

## Implementation plan and status

Status values: `NOT_STARTED`, `IN_PROGRESS`, `BLOCKED`, `COMPLETE`.

### OBS-01 Repository/Foundation - STATUS: NOT_STARTED

- [ ] Choose runtime/package layout.
- [ ] Add lint/type/test tooling.
- [ ] Add configuration conventions.
- [ ] Add CI.
- [ ] Document development commands.

Acceptance: clean checkout installs/builds/tests successfully.

### OBS-02 Telemetry Contract - STATUS: NOT_STARTED

- [ ] Define supported OTEL inputs and FactLama AI semantic attributes.
- [ ] Define tenant/application/interaction/evaluation correlation fields.
- [ ] Define normalization rules and versioning.
- [ ] Add contract tests.

Acceptance: representative LLM/retrieval/tool/evaluation events normalize deterministically.

### OBS-03 Collector and Ingestion Vertical Slice - STATUS: NOT_STARTED

- [ ] Accept supported telemetry input.
- [ ] Validate and normalize.
- [ ] Enforce tenant context.
- [ ] Record rejected/dropped event telemetry.
- [ ] Persist through storage abstraction.

Acceptance: a sample instrumented application produces queryable traces/events without direct database coupling in ingestion logic.

### OBS-04 Storage and Query API - STATUS: NOT_STARTED

- [ ] Define telemetry/trace storage interfaces.
- [ ] Implement MVP backing store.
- [ ] Implement request/trace/model/cost/reliability queries.
- [ ] Add pagination/filter/time-range behavior.
- [ ] Add tenant isolation tests.

Acceptance: dashboard-required queries work against stable APIs and tenant A cannot query tenant B data.

### OBS-05 Reliability Correlation - STATUS: NOT_STARTED

- [ ] Consume/accept reliability signals/results.
- [ ] Correlate evaluation ID to interaction/trace.
- [ ] Expose reliability summary/drill-down query models.

Acceptance: an evaluated interaction can be found from either trace or evaluation identifier.

### OBS-06 Native Dashboard MVP - STATUS: NOT_STARTED

- [ ] AI overview page.
- [ ] LLM requests/models view.
- [ ] token/cost view.
- [ ] reliability view.
- [ ] trace explorer.
- [ ] error states/loading/empty states.

Acceptance: a clean demo dataset or live sample clearly shows the end-to-end FactLama value without requiring another observability UI.

### OBS-07 SDK Instrumentation - STATUS: NOT_STARTED

- [ ] Minimal app initialization.
- [ ] LLM request metadata instrumentation.
- [ ] retrieval/tool event hooks.
- [ ] correlation ID propagation.
- [ ] examples.

Acceptance: sample application requires minimal code changes and emits valid telemetry.

### OBS-08 Cost and Usage - STATUS: NOT_STARTED

- [ ] Token normalization.
- [ ] Provider/model pricing configuration interface.
- [ ] Estimated cost calculation with provenance/version.
- [ ] Aggregate queries.

Acceptance: cost numbers show source/configuration and are not silently treated as provider billing truth.

### OBS-09 Alerts/Regression Foundation - STATUS: NOT_STARTED

- [ ] Alert-rule model.
- [ ] reliability/error/latency threshold foundations.
- [ ] regression comparison query foundations.

### OBS-10 Exporters/Integrations - STATUS: NOT_STARTED

- [ ] Exporter interface.
- [ ] OTEL/Prometheus-compatible export path where appropriate.
- [ ] document integration patterns for enterprise observability stacks.

### OBS-11 Interaction Store Integration - STATUS: NOT_STARTED

Future scope. Do not block MVP on this.

- [ ] optional content-store interface;
- [ ] capture-mode integration;
- [ ] replay/query hooks;
- [ ] retention/redaction behavior.

### OBS-12 Enterprise Hardening - STATUS: NOT_STARTED

- [ ] RBAC/SSO integration points;
- [ ] deployment scaling patterns;
- [ ] audit/data controls;
- [ ] performance/load validation.

## Claude implementation instructions

For every task:

1. read the relevant architecture source of truth;
2. inspect existing code and contracts;
3. set task status to `IN_PROGRESS`;
4. implement one end-to-end vertical slice rather than isolated layers;
5. add unit, integration and contract tests;
6. verify tenant isolation and data-governance behavior;
7. verify FactLama's own telemetry and failure visibility;
8. run lint/type/test commands;
9. review coupling and architectural drift;
10. update docs/status;
11. mark `COMPLETE` only after acceptance criteria pass.

Claude must not build a generic Grafana clone, couple dashboard components directly to database-specific schemas, require Kafka/Kubernetes for MVP, store prompt/response content by default, omit tenant context, put business logic only in the dashboard, or create a custom telemetry database.

## Required test scenarios

1. Valid LLM telemetry -> normalized and queryable.
2. Invalid/malformed telemetry -> rejected predictably and rejection is observable.
3. Tenant A data -> inaccessible from tenant B query context.
4. Trace/span hierarchy -> preserved through ingestion/storage/query.
5. Evaluation signal -> correlated to the correct interaction and trace.
6. Content capture disabled -> telemetry/dashboard remain useful.
7. Model/token/cost aggregation -> deterministic from stored normalized events.
8. Missing pricing -> cost is unknown/explicit, never silently fabricated.
9. High-cardinality/oversized attributes -> bounded according to configured limits.
10. Storage failure -> surfaced through self-observability and safe retry/error behavior.
11. Dashboard empty state -> usable and non-failing.
12. Docker Compose clean start -> collector, storage/query and dashboard become usable.
13. Sample application -> appears in dashboard with trace and reliability correlation.
14. Query pagination/time filtering -> stable and repeatable.

## Performance expectations

Establish baseline measurements before adding distributed infrastructure. Track ingestion throughput, processing latency, query latency, dashboard load time, storage growth and resource footprint. Optimize from evidence, not architecture fashion.

## Definition of done

An Observability task is complete only when implementation, tests, failure paths, tenant/security checks, telemetry/self-observability, lint/type checks, API/documentation changes and status updates are complete.

## License

MIT. Third-party SDKs, exporters and storage dependencies retain their own licenses and must be reviewed independently.