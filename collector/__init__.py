"""FactLama Observability collector (ingress + processing).

OBS-01 scope: package layout and dependency boundary only. OTLP/HTTP and
internal ReliabilityEvent ingress (`collector.ingress`) and the
authenticate -> authorize -> validate -> normalize -> persist pipeline
(`collector.processing`) described in LOW_LEVEL_IMPLEMENTATION.md are
OBS-04/05 work and do not exist yet.
"""
