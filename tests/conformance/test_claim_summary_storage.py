"""ADR-022 acceptance evidence 3 and 6, against real PostgreSQL:
"Observability persists, deduplicates, scopes and aggregates summaries by
tenant/project/application" and "Cross-tenant and cross-project isolation
tests pass, including reused trace IDs".

Mock-only tests cannot establish any of this -- the dedup guarantee is a
primary key, the conflict rejection is a read-after-insert inside one
transaction, and the scoping is an indexed SQL predicate. Same `store`
fixture and conventions as `test_reliability_events_storage.py`.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest

from schemas.reliability_event import ClaimFinding, ClaimFindingStatus, ClaimSummary
from schemas.tenancy import TenantContext
from storage.errors import ClaimSummaryConflictError
from storage.postgres.backend import PostgresTelemetryStore
from tests.conformance.conftest import make_event

NOW = datetime.now(timezone.utc)
WINDOW = {"since": NOW - timedelta(hours=1), "until": NOW + timedelta(hours=1)}
TENANT_A = TenantContext(tenant_id="tenant-a")


def _summary(
    findings: Mapping[str, tuple[ClaimFindingStatus, str | None]], total: int | None = None
) -> ClaimSummary:
    return ClaimSummary(
        total_claim_count=total if total is not None else len(findings),
        findings={
            claim_id: ClaimFinding(status=status, rationale_code=code)
            for claim_id, (status, code) in findings.items()
        },
    )


SUPPORTED = (ClaimFindingStatus.SUPPORTED, "DIRECT_SUPPORT")
UNSUPPORTED = (ClaimFindingStatus.UNSUPPORTED, "NO_SUPPORT")
CONTRADICTED = (ClaimFindingStatus.CONTRADICTED, "CONTRADICTION_DETECTED")


async def test_claim_findings_persist_and_read_back_per_evaluation(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(
            event_id="evt-1",
            evaluation_id="eval-1",
            claim_summary=_summary({"c1": SUPPORTED, "c2": CONTRADICTED}),
        )
    )

    claims = await store.get_evaluation_claims(TENANT_A, "eval-1")

    assert claims is not None
    assert claims.projection_available is True
    assert claims.total_claim_count == 2
    assert {f.claim_id: f.status for f in claims.findings} == {
        "c1": "SUPPORTED",
        "c2": "CONTRADICTED",
    }
    assert claims.counts.supported_count == 1
    assert claims.counts.contradicted_count == 1
    assert claims.counts.no_finding_count == 0


async def test_absent_projection_is_unavailable_not_zero(
    store: PostgresTelemetryStore,
) -> None:
    """ADR-022's central rule: a producer that sent no projection must not
    look identical to one reporting zero claims."""
    await store.record_reliability_event(
        make_event(event_id="evt-none", evaluation_id="eval-none", claim_summary=None)
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-zero", evaluation_id="eval-zero", claim_summary=_summary({}, total=0)
        )
    )

    absent = await store.get_evaluation_claims(TENANT_A, "eval-none")
    zero = await store.get_evaluation_claims(TENANT_A, "eval-zero")

    assert absent is not None and zero is not None
    # Both have an empty findings list -- the flag is the only thing that
    # distinguishes them, which is exactly why it exists.
    assert absent.findings == [] and zero.findings == []
    assert absent.projection_available is False
    assert zero.projection_available is True


async def test_claims_with_no_projectable_finding_are_counted_not_lost(
    store: PostgresTelemetryStore,
) -> None:
    """A NOT_APPLICABLE/DISPUTED claim never reaches `findings`, but
    `total_claim_count` still includes it -- so the derived no-finding
    count is recoverable rather than the claim vanishing."""
    await store.record_reliability_event(
        make_event(
            event_id="evt-na",
            evaluation_id="eval-na",
            claim_summary=_summary({"c1": SUPPORTED}, total=3),
        )
    )

    claims = await store.get_evaluation_claims(TENANT_A, "eval-na")

    assert claims is not None
    assert claims.total_claim_count == 3
    assert len(claims.findings) == 1
    assert claims.counts.no_finding_count == 2


async def test_redelivered_identical_event_is_idempotent(
    store: PostgresTelemetryStore,
) -> None:
    """At-least-once delivery means the same event arrives again; ADR-022
    requires that to be silently accepted, not a conflict."""
    event = make_event(
        event_id="evt-dup",
        evaluation_id="eval-dup",
        claim_summary=_summary({"c1": SUPPORTED, "c2": UNSUPPORTED}),
    )
    assert await store.record_reliability_event(event) is True
    assert await store.record_reliability_event(event) is False

    claims = await store.get_evaluation_claims(TENANT_A, "eval-dup")
    assert claims is not None
    assert len(claims.findings) == 2
    assert claims.counts.supported_count == 1
    assert claims.counts.unsupported_count == 1


async def test_a_second_event_agreeing_on_the_same_claims_is_accepted(
    store: PostgresTelemetryStore,
) -> None:
    """A different event_id for the same evaluation that asserts the *same*
    findings is agreement, not conflict -- only a differing assertion is
    rejected."""
    await store.record_reliability_event(
        make_event(
            event_id="evt-a", evaluation_id="eval-agree", claim_summary=_summary({"c1": SUPPORTED})
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-b", evaluation_id="eval-agree", claim_summary=_summary({"c1": SUPPORTED})
        )
    )

    claims = await store.get_evaluation_claims(TENANT_A, "eval-agree")
    assert claims is not None
    assert len(claims.findings) == 1


async def test_conflicting_claim_status_is_rejected_with_bounded_diagnostics(
    store: PostgresTelemetryStore,
) -> None:
    """ADR-022: "conflicting claim summaries for the same evaluation/claim
    identity must be rejected with bounded diagnostics." """
    await store.record_reliability_event(
        make_event(
            event_id="evt-first",
            evaluation_id="eval-conflict",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )

    with pytest.raises(ClaimSummaryConflictError) as caught:
        await store.record_reliability_event(
            make_event(
                event_id="evt-second",
                evaluation_id="eval-conflict",
                claim_summary=_summary({"c1": CONTRADICTED}),
            )
        )

    details = caught.value.details
    assert details["evaluation_id"] == "eval-conflict"
    assert details["conflicts"] == [
        {
            "claim_id": "c1",
            "stored_status": "SUPPORTED",
            "stored_rationale_code": "DIRECT_SUPPORT",
            "incoming_status": "CONTRADICTED",
            "incoming_rationale_code": "CONTRADICTION_DETECTED",
        }
    ]

    # The first finding survives: the rejected write rolled back rather
    # than half-applying, and never overwrote the original assertion.
    claims = await store.get_evaluation_claims(TENANT_A, "eval-conflict")
    assert claims is not None
    assert [f.status for f in claims.findings] == ["SUPPORTED"]


async def test_conflicting_rationale_code_alone_is_also_a_conflict(
    store: PostgresTelemetryStore,
) -> None:
    """Same status, different bounded rationale: still two producers
    disagreeing about why, which is not something to silently pick from."""
    await store.record_reliability_event(
        make_event(
            event_id="evt-r1",
            evaluation_id="eval-rationale",
            claim_summary=_summary({"c1": (ClaimFindingStatus.UNSUPPORTED, "NO_SUPPORT")}),
        )
    )
    with pytest.raises(ClaimSummaryConflictError):
        await store.record_reliability_event(
            make_event(
                event_id="evt-r2",
                evaluation_id="eval-rationale",
                claim_summary=_summary(
                    {"c1": (ClaimFindingStatus.UNSUPPORTED, "NO_EVIDENCE_SUPPLIED")}
                ),
            )
        )


async def test_conflict_diagnostics_are_bounded_for_a_wholly_disagreeing_event(
    store: PostgresTelemetryStore,
) -> None:
    """ "Bounded diagnostics" means a producer disagreeing about 50 claims
    cannot turn one rejection into a 50-entry error body."""
    from storage.errors import MAX_REPORTED_CLAIM_CONFLICTS

    many_supported = {f"c{i}": SUPPORTED for i in range(50)}
    many_contradicted = {f"c{i}": CONTRADICTED for i in range(50)}
    await store.record_reliability_event(
        make_event(
            event_id="evt-many-1",
            evaluation_id="eval-many",
            claim_summary=_summary(many_supported),
        )
    )
    with pytest.raises(ClaimSummaryConflictError) as caught:
        await store.record_reliability_event(
            make_event(
                event_id="evt-many-2",
                evaluation_id="eval-many",
                claim_summary=_summary(many_contradicted),
            )
        )
    assert len(caught.value.details["conflicts"]) == MAX_REPORTED_CLAIM_CONFLICTS
    assert caught.value.details["reported_conflict_limit"] == MAX_REPORTED_CLAIM_CONFLICTS


async def test_claims_are_scoped_by_tenant(store: PostgresTelemetryStore) -> None:
    await store.record_reliability_event(
        make_event(
            event_id="evt-a",
            tenant_id="tenant-a",
            evaluation_id="eval-shared",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-b",
            tenant_id="tenant-b",
            evaluation_id="eval-shared",
            claim_summary=_summary({"c1": CONTRADICTED}),
        )
    )

    a = await store.get_evaluation_claims(TENANT_A, "eval-shared")
    b = await store.get_evaluation_claims(TenantContext(tenant_id="tenant-b"), "eval-shared")

    assert a is not None and b is not None
    assert [f.status for f in a.findings] == ["SUPPORTED"]
    assert [f.status for f in b.findings] == ["CONTRADICTED"]
    # The same evaluation_id under two tenants is not a conflict -- claim
    # identity is (tenant, evaluation, claim), so neither rejected the other.


async def test_claims_are_scoped_by_project_for_a_reused_evaluation_id(
    store: PostgresTelemetryStore,
) -> None:
    """The reused-identity leak class ADR-021's scoping exists to prevent,
    applied to `evaluation_id`: one tenant may legitimately use the same
    evaluation_id under two projects, and a project-scoped read must see
    only its own claims."""
    await store.record_reliability_event(
        make_event(
            event_id="evt-pa",
            evaluation_id="eval-reused",
            project_id="proj-a",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-pb",
            evaluation_id="eval-reused",
            project_id="proj-b",
            claim_summary=_summary({"c2": CONTRADICTED}),
        )
    )

    proj_a = await store.get_evaluation_claims(
        TenantContext(tenant_id="tenant-a", project_id="proj-a"), "eval-reused"
    )
    proj_b = await store.get_evaluation_claims(
        TenantContext(tenant_id="tenant-a", project_id="proj-b"), "eval-reused"
    )

    assert proj_a is not None and proj_b is not None
    assert [f.claim_id for f in proj_a.findings] == ["c1"]
    assert [f.claim_id for f in proj_b.findings] == ["c2"]


async def test_get_evaluation_claims_hides_existence_across_tenants(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(
            event_id="evt-x",
            tenant_id="tenant-a",
            evaluation_id="eval-private",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )
    assert (
        await store.get_evaluation_claims(TenantContext(tenant_id="tenant-b"), "eval-private")
        is None
    )


async def test_aggregate_claim_findings_sums_in_scope_evaluations(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(
            event_id="evt-1",
            evaluation_id="eval-1",
            claim_summary=_summary({"c1": SUPPORTED, "c2": UNSUPPORTED}),
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-2",
            evaluation_id="eval-2",
            claim_summary=_summary({"c1": CONTRADICTED}, total=2),
        )
    )

    counts = await store.aggregate_claim_findings(TENANT_A, **WINDOW)

    assert counts.projection_available is True
    assert counts.supported_count == 1
    assert counts.unsupported_count == 1
    assert counts.contradicted_count == 1
    assert counts.total_claim_count == 4
    assert counts.no_finding_count == 1
    assert counts.evaluations_with_projection == 2
    assert counts.evaluations_without_projection == 0


async def test_aggregate_reports_unavailable_when_no_producer_sent_claims(
    store: PostgresTelemetryStore,
) -> None:
    """Every count is 0 here, but `projection_available` is False -- the
    dashboard must say "unavailable", not "no findings"."""
    await store.record_reliability_event(
        make_event(event_id="evt-1", evaluation_id="eval-1", claim_summary=None)
    )

    counts = await store.aggregate_claim_findings(TENANT_A, **WINDOW)

    assert counts.projection_available is False
    assert counts.evaluations_without_projection == 1
    assert counts.total_claim_count == 0


async def test_aggregate_distinguishes_partial_producer_coverage(
    store: PostgresTelemetryStore,
) -> None:
    """One producer publishes claims, another does not. The counts are real
    but cover only part of the window, which both per-evaluation counters
    make visible."""
    await store.record_reliability_event(
        make_event(
            event_id="evt-with",
            evaluation_id="eval-with",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )
    await store.record_reliability_event(
        make_event(event_id="evt-without", evaluation_id="eval-without", claim_summary=None)
    )

    counts = await store.aggregate_claim_findings(TENANT_A, **WINDOW)

    assert counts.projection_available is True
    assert counts.evaluations_with_projection == 1
    assert counts.evaluations_without_projection == 1
    assert counts.supported_count == 1


async def test_aggregate_is_scoped_by_tenant_and_project(
    store: PostgresTelemetryStore,
) -> None:
    await store.record_reliability_event(
        make_event(
            event_id="evt-a",
            evaluation_id="eval-a",
            project_id="proj-a",
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-b",
            evaluation_id="eval-b",
            project_id="proj-b",
            claim_summary=_summary({"c1": CONTRADICTED}),
        )
    )
    await store.record_reliability_event(
        make_event(
            event_id="evt-other-tenant",
            tenant_id="tenant-b",
            evaluation_id="eval-c",
            claim_summary=_summary({"c1": UNSUPPORTED}),
        )
    )

    proj_a = await store.aggregate_claim_findings(
        TenantContext(tenant_id="tenant-a", project_id="proj-a"), **WINDOW
    )
    assert proj_a.supported_count == 1
    assert proj_a.contradicted_count == 0
    assert proj_a.unsupported_count == 0

    tenant_wide = await store.aggregate_claim_findings(TENANT_A, **WINDOW)
    assert tenant_wide.supported_count == 1
    assert tenant_wide.contradicted_count == 1
    # tenant-b's claim never appears in tenant-a's aggregate.
    assert tenant_wide.unsupported_count == 0


async def test_aggregate_excludes_evaluations_outside_the_window(
    store: PostgresTelemetryStore,
) -> None:
    old = (NOW - timedelta(days=3)).isoformat()
    await store.record_reliability_event(
        make_event(
            event_id="evt-old",
            evaluation_id="eval-old",
            occurred_at=old,
            claim_summary=_summary({"c1": SUPPORTED}),
        )
    )

    counts = await store.aggregate_claim_findings(TENANT_A, **WINDOW)

    assert counts.supported_count == 0
    assert counts.evaluations_with_projection == 0
    assert counts.projection_available is False
