# Observability Architecture

FactLama Observability is the AI-native telemetry, query, and dashboard layer for LLM, RAG, and agentic applications.

## Responsibilities
- OTEL-compatible ingestion
- AI semantic telemetry normalization
- request/model/provider/token/cost/latency/error data
- retrieval and tool-call telemetry
- reliability signal correlation
- storage abstraction
- query API
- native dashboard
- alerts and exporters

## Boundaries
The dashboard reads the FactLama Query API, not databases directly. Storage engines remain replaceable behind interfaces. Reliability owns semantic evaluation decisions; Observability stores and presents their signals and provenance references.

## Enterprise requirements
Tenant isolation, backpressure, bounded payloads, non-blocking customer instrumentation, horizontal scale, retention controls, query pagination, schema evolution, self-observability, and external exporter support.
