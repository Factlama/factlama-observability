"""Cross-cutting foundations shared by sdk/collector/storage/query.

OBS-01 scope: logging convention, schema/API versioning constant, health
aggregation primitives, and env-based settings resolution shared by every
other package. Collector/query readiness endpoints and self-metrics
(OBS-04/07/11) are not implemented here -- this only gives them a common
`HealthCheck` shape and a place to compose checks once storage exists.
"""
