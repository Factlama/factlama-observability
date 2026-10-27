"""The instrumented LLM/RAG sample app `docs/LOW_LEVEL_IMPLEMENTATION.md`
illustrates: one `llm_span`, one nested `retrieval_span`, one nested
`tool_span`, then `submit_verification` linking the whole interaction to
one evaluation. Doubles as this slice's own "SDK reviewer test" fixture
("run the sample LLM/RAG wrapper and inspect a trace with one LLM span,
one retrieval span, one tool span and a linked evaluation_id").

A separate script from `examples/verify_and_correlate.py` on purpose:
that one is the cross-repo e2e test's own driver and stays untouched to
avoid an unreviewed behavior change to already-approved test
infrastructure; this one is purely additive.

Run standalone against two already-running services:
    FACTLAMA_SDK_COLLECTOR_URL=http://localhost:4318 \\
    FACTLAMA_SDK_RELIABILITY_API_URL=http://localhost:8000 \\
    FACTLAMA_SDK_API_KEY=<a key both services' tenant credentials recognize> \\
    FACTLAMA_SDK_PROJECT_ID=p-demo \\
    FACTLAMA_SDK_APPLICATION_ID=a-demo \\
    python -m examples.instrumented_llm_rag

Prints one JSON object: `{"job_id", "state", "trace_id"}` (all four spans
-- llm, retrieval, tool, and the verification span `submit_verification`
exports -- share this one `trace_id`) on success, or `{"error": "..."}`
with a non-zero exit code on failure. No prompt, answer, retrieved
document, or tool payload appears anywhere in this script's own state or
output -- metadata only, matching every span type's own fields
(`sdk/spans.py`).
"""

import json
import os
import sys

from sdk.client import FactLamaClient
from sdk.config import FactLamaConfig
from sdk.errors import FactLamaConfigError, VerificationSubmitError


def _config_from_env() -> FactLamaConfig:
    return FactLamaConfig(
        collector_url=os.environ.get("FACTLAMA_SDK_COLLECTOR_URL", ""),
        reliability_api_url=os.environ.get("FACTLAMA_SDK_RELIABILITY_API_URL", ""),
        api_key=os.environ.get("FACTLAMA_SDK_API_KEY", ""),
        project_id=os.environ.get("FACTLAMA_SDK_PROJECT_ID", ""),
        application_id=os.environ.get("FACTLAMA_SDK_APPLICATION_ID", ""),
    )


def _search(_query: str) -> str:
    """Stands in for a real retriever -- returns a fixed passage so this
    script has no external dependency to run standalone."""
    return "Paris is the capital of France."


def _generate(evidence: str) -> str:
    """Stands in for a real LLM call."""
    return evidence


def main() -> int:
    try:
        config = _config_from_env()
    except FactLamaConfigError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    client = FactLamaClient(config)
    try:
        with client.llm_span(provider="example", model="model-a") as llm:
            with client.retrieval_span(source="kb") as retrieval:
                evidence = _search("When was Paris named the capital of France?")
                retrieval.set_result(document_ids=["doc-1"], count=1)
            with client.tool_span(name="format_citation"):
                pass
            answer = _generate(evidence)
            llm.set_usage(input_tokens=120, output_tokens=18)

            handle = client.submit_verification(
                {
                    "schema_version": "0.1",
                    "request_id": "g7-instrumented-rag-demo",
                    "project_id": config.project_id,
                    "application_id": config.application_id,
                    "answer": answer,
                    "claims": [{"claim_id": "c1", "text": answer}],
                    "evidence": [{"evidence_id": "e1", "content": evidence}],
                    "policy": {
                        "id": "g7-instrumented-rag",
                        "execution_storage": {"mode": "DURABLE_ENCRYPTED"},
                    },
                    "trace_id": llm.trace_id,
                    "interaction_id": llm.interaction_id,
                },
                idempotency_key="g7-instrumented-rag-demo-key",
            )
    except VerificationSubmitError as exc:
        client.close(timeout=5.0)
        print(json.dumps({"error": str(exc)}))
        return 1

    client.close(timeout=5.0)
    print(json.dumps({"job_id": handle.job_id, "state": handle.state, "trace_id": handle.trace_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
