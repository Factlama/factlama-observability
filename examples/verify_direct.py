"""G7's direct-call sample app: one real `sdk.client.FactLamaClient.verify()`
call against a live Reliability API, exporting one best-effort
`VERIFICATION` span to a live Observability collector.

Unlike `examples/verify_and_correlate.py` (the async path), this span is
**permanently, architecturally uncorrelated** -- `factlama-reliability`'s
synchronous `POST /v0.1/verifications` never produces a `ReliabilityEvent`,
so there is no mechanism by which this call's trace_id/span_id could ever
show up linked to an evaluation in Observability's own trace query API.
That is not a limitation of this script; it is true of `verify()` itself
(see `sdk/client.py`'s own module docstring). This script exists to
exercise and demonstrate the *direct* half of `docs/LOW_LEVEL_IMPLEMENTATION.md`'s
"Python `verify`/async client ... direct and async API examples pass"
acceptance line -- the async half is `verify_and_correlate.py`.

Run standalone against an already-running Reliability API and collector:
    FACTLAMA_SDK_COLLECTOR_URL=http://localhost:4318 \\
    FACTLAMA_SDK_RELIABILITY_API_URL=http://localhost:8000 \\
    FACTLAMA_SDK_API_KEY=<a key the Reliability API's tenant credentials recognize> \\
    FACTLAMA_SDK_PROJECT_ID=p-demo \\
    FACTLAMA_SDK_APPLICATION_ID=a-demo \\
    python -m examples.verify_direct

With no piped stdin, submits a small built-in demo verification request
(no `policy`/`execution_storage` needed -- the synchronous endpoint never
persists anything, so ADR-020's durable-storage authorization does not
apply here). Piping a JSON object of the shape `{"request": {...}}` on
stdin verifies that request instead.

Prints exactly one JSON object to stdout: `{"trace_id", "span_id",
"interaction_id", "result"}` on success, or `{"error": "..."}` with a
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
    return {
        "schema_version": "0.1",
        "request_id": "g7-direct-sample-app-demo",
        "project_id": config.project_id,
        "application_id": config.application_id,
        "answer": "Paris is the capital of France.",
        "claims": [{"claim_id": "c1", "text": "Paris is the capital of France."}],
        "evidence": [{"evidence_id": "e1", "content": "Paris is the capital of France."}],
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
    request = payload["request"] if payload is not None else _demo_request(config)

    client = FactLamaClient(config)
    try:
        handle = client.verify(request)
    except VerificationSubmitError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    print(
        json.dumps(
            {
                "trace_id": handle.trace_id,
                "span_id": handle.span_id,
                "interaction_id": handle.interaction_id,
                "result": handle.result,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
