"""ADR-022 acceptance evidence 4: "Query responses distinguish zero,
unavailable and pending data."

HTTP layer only -- auth, envelope shape, the unavailable/zero distinction,
scope narrowing and storage-outage passthrough. Real storage correctness
(dedup, conflict rejection, tenant/project scoping) is proven against live
PostgreSQL by `tests/conformance/test_claim_summary_storage.py`.

A separate minimal fake rather than extending `tests/test_query_api.py`'s
`FakeTelemetryStore`, same reason `test_query_api_span_aggregates.py` keeps
its own: that double has no claim storage, and this file needs none of its
other machinery.
"""

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from operations.tenant_credentials import TenantCredentialStore
from query.api.app import app, get_storage_readiness, get_telemetry_store
from query.api.settings import Settings, get_settings
from schemas.tenancy import TenantContext
from storage.aggregates import ClaimFindingCounts, ClaimFindingRow, EvaluationClaims
from storage.errors import StorageUnavailableError

TENANT_KEY = "tenant-claims-key"
TENANT_KEY_SCOPED_TO_PROJECT_A = "tenant-claims-key-proj-a"


def _counts(
    *,
    supported: int = 0,
    unsupported: int = 0,
    contradicted: int = 0,
    insufficient: int = 0,
    total: int = 0,
    no_finding: int = 0,
    with_projection: int = 1,
    without_projection: int = 0,
) -> ClaimFindingCounts:
    return ClaimFindingCounts(
        supported_count=supported,
        unsupported_count=unsupported,
        contradicted_count=contradicted,
        insufficient_evidence_count=insufficient,
        total_claim_count=total,
        no_finding_count=no_finding,
        evaluations_with_projection=with_projection,
        evaluations_without_projection=without_projection,
    )


class _FakeClaimStore:
    def __init__(self) -> None:
        self.fail_next = False
        self.evaluation_claims: EvaluationClaims | None = EvaluationClaims(
            evaluation_id="eval-1",
            projection_available=True,
            total_claim_count=3,
            findings=[
                ClaimFindingRow(claim_id="c1", status="SUPPORTED", rationale_code="DIRECT_SUPPORT"),
                ClaimFindingRow(claim_id="c2", status="CONTRADICTED", rationale_code=None),
            ],
            counts=_counts(supported=1, contradicted=1, total=3, no_finding=1),
        )
        self.aggregate = _counts(supported=4, unsupported=2, contradicted=1, total=8, no_finding=1)

    async def get_evaluation_claims(
        self, tenant: TenantContext, evaluation_id: str
    ) -> EvaluationClaims | None:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.evaluation_claims

    async def aggregate_claim_findings(
        self, tenant: TenantContext, *, since: datetime, until: datetime
    ) -> ClaimFindingCounts:
        if self.fail_next:
            self.fail_next = False
            raise StorageUnavailableError("simulated storage outage")
        return self.aggregate


def _test_settings() -> Settings:
    settings = Settings.__new__(Settings)
    settings.tenant_store = TenantCredentialStore(
        {
            TENANT_KEY: TenantContext(tenant_id="tenant-a"),
            TENANT_KEY_SCOPED_TO_PROJECT_A: TenantContext(
                tenant_id="tenant-a", project_id="proj-a"
            ),
        }
    )
    settings.database_url = None
    settings.query_cursor_secret = "test-cursor-secret"
    settings.admin_token = None
    return settings


@pytest.fixture
def store() -> _FakeClaimStore:
    return _FakeClaimStore()


@pytest.fixture
def client(store: _FakeClaimStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_telemetry_store] = lambda: store
    app.dependency_overrides[get_storage_readiness] = lambda: True
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class TestEvaluationClaims:
    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/v0.1/ai/evaluations/eval-1/claims").status_code == 401

    def test_returns_claims_with_counts(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/evaluations/eval-1/claims", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema_version"] == "0.1"
        assert body["evaluation_id"] == "eval-1"
        assert body["claims"] == [
            {"claim_id": "c1", "status": "SUPPORTED", "rationale_code": "DIRECT_SUPPORT"},
            {"claim_id": "c2", "status": "CONTRADICTED", "rationale_code": None},
        ]
        metrics = body["metrics"]
        assert metrics["projection_available"] is True
        assert metrics["supported_count"] == 1
        assert metrics["contradicted_count"] == 1
        assert metrics["total_claim_count"] == 3
        assert metrics["no_finding_count"] == 1

    def test_response_carries_no_claim_text_field_at_all(self, client: TestClient) -> None:
        """ADR-022's privacy boundary at the read boundary: there is no
        field in this response that could carry claim text, a prompt, a
        response, evidence or free-form rationale."""
        body = client.get("/v0.1/ai/evaluations/eval-1/claims", headers=_auth(TENANT_KEY)).json()
        for claim in body["claims"]:
            assert set(claim) == {"claim_id", "status", "rationale_code"}

    def test_unavailable_projection_is_distinguishable_from_zero_claims(
        self, client: TestClient, store: _FakeClaimStore
    ) -> None:
        """Both responses carry `claims: []`; only `projection_available`
        separates "the producer published nothing" from "there were no
        claims". ADR-022 requires the two to stay distinct."""
        store.evaluation_claims = EvaluationClaims(
            evaluation_id="eval-none",
            projection_available=False,
            total_claim_count=0,
            findings=[],
            counts=_counts(with_projection=0, without_projection=1),
        )
        unavailable = client.get(
            "/v0.1/ai/evaluations/eval-none/claims", headers=_auth(TENANT_KEY)
        ).json()
        assert unavailable["claims"] == []
        assert unavailable["metrics"]["projection_available"] is False

        store.evaluation_claims = EvaluationClaims(
            evaluation_id="eval-zero",
            projection_available=True,
            total_claim_count=0,
            findings=[],
            counts=_counts(with_projection=1),
        )
        zero = client.get("/v0.1/ai/evaluations/eval-zero/claims", headers=_auth(TENANT_KEY)).json()
        assert zero["claims"] == []
        assert zero["metrics"]["projection_available"] is True

    def test_missing_or_inaccessible_evaluation_is_404(
        self, client: TestClient, store: _FakeClaimStore
    ) -> None:
        store.evaluation_claims = None
        response = client.get("/v0.1/ai/evaluations/nope/claims", headers=_auth(TENANT_KEY))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_storage_outage_returns_retryable_503(
        self, client: TestClient, store: _FakeClaimStore
    ) -> None:
        store.fail_next = True
        response = client.get("/v0.1/ai/evaluations/eval-1/claims", headers=_auth(TENANT_KEY))
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True

    def test_claims_path_is_not_shadowed_by_the_evaluation_detail_route(
        self, client: TestClient
    ) -> None:
        """A real routing-order risk: `/v0.1/ai/evaluations/{evaluation_id}`
        must not swallow `/{evaluation_id}/claims`."""
        body = client.get("/v0.1/ai/evaluations/eval-1/claims", headers=_auth(TENANT_KEY)).json()
        assert "claims" in body
        assert "verdict" not in body


class TestClaimAggregates:
    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/v0.1/ai/claims/aggregates").status_code == 401

    def test_returns_the_documented_aggregate_envelope(self, client: TestClient) -> None:
        response = client.get("/v0.1/ai/claims/aggregates", headers=_auth(TENANT_KEY))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schema_version"] == "0.1"
        assert {"since", "until"} <= body["window"].keys()
        assert "as_of" in body
        assert body["sample_count"] == 1
        metrics = body["metrics"]
        assert metrics["supported_count"] == 4
        assert metrics["unsupported_count"] == 2
        assert metrics["contradicted_count"] == 1
        assert metrics["no_finding_count"] == 1
        assert metrics["projection_available"] is True

    def test_reports_unavailable_rather_than_zero_findings(
        self, client: TestClient, store: _FakeClaimStore
    ) -> None:
        store.aggregate = _counts(with_projection=0, without_projection=5)
        metrics = client.get("/v0.1/ai/claims/aggregates", headers=_auth(TENANT_KEY)).json()[
            "metrics"
        ]
        assert metrics["projection_available"] is False
        assert metrics["supported_count"] == 0
        assert metrics["evaluations_without_projection"] == 5

    def test_echoes_resolved_scope_filters(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/claims/aggregates",
            params={"project_id": "proj-a"},
            headers=_auth(TENANT_KEY),
        )
        assert response.status_code == 200, response.text
        assert response.json()["filters"] == {"project_id": "proj-a", "application_id": None}

    def test_scoped_key_cannot_widen_to_another_project(self, client: TestClient) -> None:
        response = client.get(
            "/v0.1/ai/claims/aggregates",
            params={"project_id": "proj-b"},
            headers=_auth(TENANT_KEY_SCOPED_TO_PROJECT_A),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_storage_outage_returns_retryable_503(
        self, client: TestClient, store: _FakeClaimStore
    ) -> None:
        store.fail_next = True
        response = client.get("/v0.1/ai/claims/aggregates", headers=_auth(TENANT_KEY))
        assert response.status_code == 503
        assert response.json()["error"]["retryable"] is True
