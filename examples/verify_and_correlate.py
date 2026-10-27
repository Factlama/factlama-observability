"""G7's end-to-end sample application: one real `sdk.client.FactLamaClient`
call, submitting a verification job to a live Reliability API and
exporting one `VERIFICATION` span to a live Observability collector, with
the two correlated by a shared trace_id/span_id -- exactly the slice
`EXECUTION_PLAN.md`'s G7 row asks for ("Sample app emits one interaction/
trace linked to one evaluation").

This is also `tests/conformance/test_sdk_correlation_e2e.py` (in the
sibling `factlama-reliability` repo)'s own SDK driver: that test invokes
this script as a subprocess from this repo's own venv (where the `sdk`
extra's dependencies -- `httpx`, `opentelemetry-proto` -- are installed),
since it runs in Reliability's venv and must not import `sdk` directly
(no cross-repo Python import; see `sdk/client.py`'s module docstring).
One real script serves both purposes rather than a test-only duplicate.

Run standalone against two already-running services:
    FACTLAMA_SDK_COLLECTOR_URL=http://localhost:4318 \\
    FACTLAMA_SDK_RELIABILITY_API_URL=http://localhost:8000 \\
    FACTLAMA_SDK_API_KEY=<a key both services' tenant credentials recognize> \\
    FACTLAMA_SDK_PROJECT_ID=p-demo \\
    FACTLAMA_SDK_APPLICATION_ID=a-demo \\
    python -m examples.verify_and_correlate

With no piped stdin, submits a small built-in demo verification request.
Piping a JSON object of the shape `{"request": {...}, "idempotency_key":
"..."}` on stdin submits that request instead (what the cross-repo test
does, so it controls project/application IDs and policy precisely).

Prints exactly one JSON object to stdout: `{"job_id", "state", "trace_id",
"span_id", "interaction_id"}` on success, or `{"error": "..."}` with a
non-zero exit code on failure.
"""

import json
import os
import sys
from typing import Any

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


def _demo_request(config: FactLamaConfig) -> dict[str, Any]:
    """A minimal, self-contained verification request -- durable async
    submission needs explicit `DURABLE_ENCRYPTED` authorization
    (`factlama-reliability`'s ADR-020), the same shape that repo's own
    `examples/api_usage.py` demo uses for its async section."""
    return {
        "schema_version": "0.1",
        "request_id": "g7-sample-app-demo",
        "project_id": config.project_id,
        "application_id": config.application_id,
        "answer": "Paris is the capital of France.",
        "claims": [{"claim_id": "c1", "text": "Paris is the capital of France."}],
        "evidence": [{"evidence_id": "e1", "content": "Paris is the capital of France."}],
        "policy": {"id": "g7-sample-app", "execution_storage": {"mode": "DURABLE_ENCRYPTED"}},
    }


def _read_stdin_payload() -> dict[str, Any] | None:
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    return json.loads(raw)


def main() -> int:
    try:
        config = _config_from_env()
    except FactLamaConfigError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    payload = _read_stdin_payload()
    if payload is not None:
        request = payload["request"]
        idempotency_key = payload["idempotency_key"]
    else:
        request = _demo_request(config)
        idempotency_key = "g7-sample-app-demo-key"

    client = FactLamaClient(config)
    try:
        handle = client.submit_verification(request, idempotency_key=idempotency_key)
    except VerificationSubmitError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    print(
        json.dumps(
            {
                "job_id": handle.job_id,
                "state": handle.state,
                "trace_id": handle.trace_id,
                "span_id": handle.span_id,
                "interaction_id": handle.interaction_id,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
