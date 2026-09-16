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


def _services() -> tuple[RequestManagementService, ProductIntentApprovalService]:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    requests = RequestManagementService(repository, clock=lambda: NOW)
    approvals = ProductIntentApprovalService(repository, clock=lambda: NOW)
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
        constraints=_constraints(),
    )
    replay = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        approved_by="architect-a",
        intent=intent,
        constraints=_constraints(),
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
        constraints=_constraints(),
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
        constraints=_constraints(),
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
        constraints=_constraints(minimum_interval_seconds=86_400),
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
            constraints=_constraints(),
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
        constraints=_constraints(),
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
            constraints=_constraints(),
        )

    current = requests.get("tenant-a", request.request_id)
    revised_intent = intent.model_copy(update={"business_outcome": "Include the finance table."})
    second = approvals.approve(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=current.revision,
        approved_by="architect-a",
        intent=revised_intent,
        constraints=_constraints(),
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
        constraints=_constraints(),
    )

    with pytest.raises(ValueError, match="different approved intent"):
        approvals.approve(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            approved_by="architect-a",
            intent=intent.model_copy(update={"title": "Changed title"}),
            constraints=_constraints(),
        )

    assert len(approvals.list_for_request("tenant-a", request.request_id)) == 1
