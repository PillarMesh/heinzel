from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management.product_intent import (
    ApprovedProductIntent,
    DeliveryIntent,
    DimensionIntent,
    FilterIntent,
    FreshnessObjective,
    Grain,
    MeasureIntent,
    ProductIntent,
    ProductIntentApprovalService,
    ProductIntentAuthorityRefs,
    ProductIntentCandidate,
    ProductIntentCandidateConflictError,
    ProductIntentCandidateService,
    ProductIntentCandidateStaleRevisionError,
    ProductIntentConstraints,
    ProductIntentNoValidPlan,
    ProductIntentSourceCoverage,
)
from pillarmesh_request_management.repository import SQLiteRequestRepository
from pillarmesh_request_management.service import RequestManagementService
from pydantic import ValidationError

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _intent(*, maximum_age_seconds: int = 86_400) -> ProductIntent:
    return ProductIntent(
        request_id="request-placeholder",
        title="Daily net revenue",
        business_outcome="Give finance a governed daily revenue dataset.",
        source_refs=("source:orders",),
        grain=Grain(keys=("order_day",)),
        measures=(MeasureIntent(metric_ref="metric:net_revenue", aggregation="sum"),),
        dimensions=(DimensionIntent(dimension_ref="dimension:order_day"),),
        filters=(
            FilterIntent(
                dimension_ref="dimension:order_status",
                operator="equals",
                value="paid",
            ),
        ),
        freshness=FreshnessObjective(maximum_age_seconds=maximum_age_seconds),
        delivery=DeliveryIntent(outputs=("dataset", "table")),
    )


class _StubAuthority:
    """Stands in for the governed authority; records what approval asked it."""

    def __init__(self, constraints: ProductIntentConstraints | None) -> None:
        self.constraints = constraints
        self.calls: list[tuple[str, ProductIntentAuthorityRefs, datetime]] = []

    def resolve_constraints(
        self,
        *,
        tenant_id: str,
        authority_refs: ProductIntentAuthorityRefs,
        evaluated_at: datetime,
    ) -> ProductIntentConstraints | None:
        self.calls.append((tenant_id, authority_refs, evaluated_at))
        return self.constraints


def _refs(*, semantic_version: str = "semantic-version-1") -> ProductIntentAuthorityRefs:
    return ProductIntentAuthorityRefs(
        semantic_version=ArtifactReference(
            artifact_id=semantic_version, version=1, digest="a" * 64
        ),
        source_observations=(
            ArtifactReference(artifact_id="source-observation-orders", version=1, digest="b" * 64),
        ),
    )


def _services(
    authority: _StubAuthority | None = None,
) -> tuple[RequestManagementService, ProductIntentApprovalService]:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    requests = RequestManagementService(repository, clock=lambda: NOW)
    approvals = ProductIntentApprovalService(
        repository,
        clock=lambda: NOW,
        authority=authority if authority is not None else _StubAuthority(_constraints()),
    )
    return requests, approvals


def _constraints(*, minimum_interval_seconds: int = 86_400) -> ProductIntentConstraints:
    return ProductIntentConstraints(
        approved_source_refs=("source:orders",),
        approved_metric_refs=("metric:net_revenue",),
        approved_dimension_refs=("dimension:order_day", "dimension:order_status"),
        minimum_source_interval_seconds=minimum_interval_seconds,
    )


def _candidate_service(
    repository: SQLiteRequestRepository | None = None,
) -> tuple[RequestManagementService, ProductIntentCandidateService]:
    repository = repository or SQLiteRequestRepository(sqlite3.connect(":memory:"))
    return (
        RequestManagementService(repository, clock=lambda: NOW),
        ProductIntentCandidateService(repository, clock=lambda: NOW),
    )


def _propose_candidate(
    candidates: ProductIntentCandidateService,
    *,
    tenant_id: str,
    request_id: str,
    request_revision: int,
    idempotency_key: str = "candidate-proposal-1",
    intent: ProductIntent | None = None,
) -> ProductIntentCandidate:
    product_intent = intent or _intent().model_copy(update={"request_id": request_id})
    return candidates.propose(
        tenant_id=tenant_id,
        request_id=request_id,
        request_revision=request_revision,
        idempotency_key=idempotency_key,
        proposed_by="interpreter-a",
        intent=product_intent,
        constraints=_constraints(),
        source_coverage=(
            ProductIntentSourceCoverage(
                source_ref="source:orders",
                covered_fields=("order_day", "metric:net_revenue"),
                authorized=True,
            ),
        ),
        unresolved_constraints=(),
    )


def test_product_intent_candidate_is_durable_and_replays_exactly() -> None:
    requests, candidates = _candidate_service()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )

    first = _propose_candidate(
        candidates,
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
    )
    replay = _propose_candidate(
        candidates,
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
    )

    assert replay == first
    assert replay.model_dump_json() == first.model_dump_json()
    assert candidates.current_candidate("tenant-a", request.request_id) == first


def test_product_intent_candidate_survives_repository_reopen(tmp_path) -> None:
    database_path = str(tmp_path / "requests.sqlite3")
    repository = SQLiteRequestRepository.open(database_path)
    requests, candidates = _candidate_service(repository)
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    candidate = _propose_candidate(
        candidates,
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
    )
    repository.close()

    reopened = SQLiteRequestRepository.open(database_path)
    try:
        restored = ProductIntentCandidateService(reopened, clock=lambda: NOW).current_candidate(
            "tenant-a", request.request_id
        )
    finally:
        reopened.close()

    assert restored == candidate


def test_product_intent_candidate_rejects_a_conflicting_replay() -> None:
    requests, candidates = _candidate_service()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    _propose_candidate(
        candidates,
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
    )

    with pytest.raises(ProductIntentCandidateConflictError):
        _propose_candidate(
            candidates,
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            intent=_intent().model_copy(
                update={"request_id": request.request_id, "title": "Changed title"}
            ),
        )


def test_candidate_denies_stale_and_cross_tenant_requests_without_enumeration() -> None:
    requests, candidates = _candidate_service()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )

    with pytest.raises(ProductIntentCandidateStaleRevisionError):
        _propose_candidate(
            candidates,
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision + 1,
        )
    with pytest.raises(KeyError, match="request is unavailable") as foreign:
        candidates.current_candidate("tenant-b", request.request_id)
    with pytest.raises(KeyError, match="request is unavailable") as missing:
        candidates.current_candidate("tenant-b", "request-missing")
    with pytest.raises(KeyError, match="request is unavailable") as foreign_proposal:
        _propose_candidate(
            candidates,
            tenant_id="tenant-b",
            request_id=request.request_id,
            request_revision=request.revision,
        )
    with pytest.raises(KeyError, match="request is unavailable") as missing_proposal:
        _propose_candidate(
            candidates,
            tenant_id="tenant-b",
            request_id="request-missing",
            request_revision=request.revision,
        )

    assert foreign.value.args == missing.value.args
    assert foreign_proposal.value.args == missing_proposal.value.args


def test_answerable_request_records_exact_typed_intent_and_replays() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    intent = _intent().model_copy(update={"request_id": request.request_id})

    first = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )
    replay = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    assert isinstance(first, ApprovedProductIntent)
    assert replay == first
    assert first.intent == intent
    assert first.intent_digest == intent.canonical_digest()
    assert first.artifact_reference == ArtifactReference(
        artifact_id=first.approval_id,
        version=first.intent_revision,
        digest=digest(first),
    )
    assert approvals.list_for_request("tenant-a", request.request_id) == (first,)


def test_missing_grain_is_rejected_by_the_strict_contract() -> None:
    payload = _intent().model_dump(mode="json")
    payload.pop("grain")

    with pytest.raises(ValidationError, match="grain"):
        ProductIntent.model_validate(payload)


def test_unknown_metric_returns_attributable_no_valid_plan() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Finance reporting",
        question="What is gross margin?",
    )
    intent = _intent().model_copy(
        update={
            "request_id": request.request_id,
            "measures": (MeasureIntent(metric_ref="metric:gross_margin", aggregation="sum"),),
        }
    )

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == ("metric metric:gross_margin is not approved",)


def test_unapproved_source_prevents_intent_approval() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Finance reporting",
        question="What is daily net revenue?",
    )
    intent = _intent().model_copy(
        update={"request_id": request.request_id, "source_refs": ("source:private-ledger",)}
    )

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == ("source source:private-ledger is not approved",)
    assert approvals.list_for_request("tenant-a", request.request_id) == ()


def test_hourly_freshness_is_refused_for_a_daily_source() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Intraday finance reporting",
        question="What is hourly net revenue?",
    )
    intent = _intent(maximum_age_seconds=3_600).model_copy(
        update={"request_id": request.request_id}
    )

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == (
        "requested freshness 3600s is below the source minimum interval 86400s",
    )
    assert approvals.list_for_request("tenant-a", request.request_id) == ()


def test_cross_tenant_approval_is_denied_without_disclosing_request_ownership() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    intent = _intent().model_copy(update={"request_id": request.request_id})

    with pytest.raises(KeyError, match="request is unavailable"):
        approvals.approve(
            tenant_id="tenant-b",
            request_id=request.request_id,
            request_revision=request.revision,
            approved_by="architect-b",
            intent=intent,
            authority_refs=_refs(),
        )

    assert approvals.list_for_request("tenant-a", request.request_id) == ()


def test_new_request_revision_requires_a_new_intent_revision() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    intent = _intent().model_copy(update={"request_id": request.request_id})
    first = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )
    requests.append_conversation(
        "tenant-a",
        request.request_id,
        "requester-a",
        "Please include the finance table.",
        expected_revision=request.revision,
        author_role="requester",
    )

    with pytest.raises(ValueError, match="stale"):
        approvals.approve(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            approved_by="architect-a",
            intent=intent,
            authority_refs=_refs(),
        )

    current = requests.get("tenant-a", request.request_id)
    revised_intent = intent.model_copy(update={"business_outcome": "Include the finance table."})
    second = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=current.revision,
        approved_by="architect-a",
        intent=revised_intent,
        authority_refs=_refs(),
    )

    assert isinstance(first, ApprovedProductIntent)
    assert isinstance(second, ApprovedProductIntent)
    assert (first.intent_revision, second.intent_revision) == (1, 2)
    assert approvals.list_for_request("tenant-a", request.request_id) == (first, second)


def test_same_request_revision_cannot_approve_different_intent() -> None:
    requests, approvals = _services()
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    intent = _intent().model_copy(update={"request_id": request.request_id})
    approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    with pytest.raises(ValueError, match="different approved intent"):
        approvals.approve(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            approved_by="architect-a",
            intent=intent.model_copy(update={"title": "Changed title"}),
            authority_refs=_refs(),
        )

    assert len(approvals.list_for_request("tenant-a", request.request_id)) == 1


def _submitted(requests: RequestManagementService) -> tuple[str, int]:
    request = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Daily finance reporting",
        question="What is daily net revenue?",
    )
    return request.request_id, request.revision


def test_approval_without_authority_references_is_refused() -> None:
    authority = _StubAuthority(_constraints())
    requests, approvals = _services(authority)
    request_id, revision = _submitted(requests)

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=_intent().model_copy(update={"request_id": request_id}),
        authority_refs=None,
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == ("governed authority references are required",)
    assert authority.calls == []
    assert approvals.list_for_request("tenant-a", request_id) == ()


def test_unusable_authority_references_are_refused() -> None:
    requests, approvals = _services(_StubAuthority(None))
    request_id, revision = _submitted(requests)

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=_intent().model_copy(update={"request_id": request_id}),
        authority_refs=_refs(),
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == (
        "governed authority references are unavailable, foreign, altered, or not current",
    )
    assert approvals.list_for_request("tenant-a", request_id) == ()


def test_approval_evaluates_against_authority_not_the_candidate_constraints() -> None:
    """A proposer's permissive constraints cannot make an intent approvable.

    The candidate below claims every source, metric and dimension is approved and that the source
    refreshes hourly. The authority says the metric is not approved. Approval must follow the
    authority.
    """
    narrow = _constraints().model_copy(update={"approved_metric_refs": ()})
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    requests = RequestManagementService(repository, clock=lambda: NOW)
    candidates = ProductIntentCandidateService(repository, clock=lambda: NOW)
    approvals = ProductIntentApprovalService(
        repository, clock=lambda: NOW, authority=_StubAuthority(narrow)
    )
    request_id, revision = _submitted(requests)
    intent = _intent(maximum_age_seconds=3_600).model_copy(update={"request_id": request_id})
    candidate = candidates.propose(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        idempotency_key="permissive-candidate",
        proposed_by="interpreter-a",
        intent=intent,
        constraints=_constraints(minimum_interval_seconds=60),
        source_coverage=(
            ProductIntentSourceCoverage(
                source_ref="source:orders", covered_fields=("order_day",), authorized=True
            ),
        ),
        unresolved_constraints=(),
        authority_refs=_refs(),
    )

    result = approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=candidate.intent,
        authority_refs=candidate.authority_refs,
    )

    assert isinstance(result, ProductIntentNoValidPlan)
    assert result.constraints == (
        "metric metric:net_revenue is not approved",
        "requested freshness 3600s is below the source minimum interval 86400s",
    )


def test_an_approval_records_the_authority_it_was_evaluated_against() -> None:
    authority = _StubAuthority(_constraints())
    requests, approvals = _services(authority)
    request_id, revision = _submitted(requests)

    approved = approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=_intent().model_copy(update={"request_id": request_id}),
        authority_refs=_refs(),
    )

    assert isinstance(approved, ApprovedProductIntent)
    assert approved.authority_refs == _refs()
    assert approved.constraints == _constraints()
    assert authority.calls == [("tenant-a", _refs(), NOW)]


def test_replaying_an_approval_against_different_authority_is_refused() -> None:
    requests, approvals = _services()
    request_id, revision = _submitted(requests)
    intent = _intent().model_copy(update={"request_id": request_id})
    approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=intent,
        authority_refs=_refs(),
    )

    with pytest.raises(ValueError, match="different authority"):
        approvals.approve(
            tenant_id="tenant-a",
            request_id=request_id,
            request_revision=revision,
            approved_by="architect-a",
            intent=intent,
            authority_refs=_refs(semantic_version="semantic-version-2"),
        )


def test_an_approval_resolves_only_by_its_exact_reference_and_tenant() -> None:
    requests, approvals = _services()
    request_id, revision = _submitted(requests)
    approved = approvals.approve(
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=revision,
        approved_by="architect-a",
        intent=_intent().model_copy(update={"request_id": request_id}),
        authority_refs=_refs(),
    )
    assert isinstance(approved, ApprovedProductIntent)
    reference = approved.artifact_reference

    assert approvals.resolve("tenant-a", reference) == approved
    assert approvals.resolve("tenant-b", reference) is None
    assert approvals.resolve("tenant-a", reference.model_copy(update={"version": 2})) is None
    assert approvals.resolve("tenant-a", reference.model_copy(update={"digest": "f" * 64})) is None
    assert (
        approvals.resolve("tenant-a", reference.model_copy(update={"artifact_id": "missing"}))
        is None
    )
