from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    ClarifiedOutcomeStatement,
    DecisionKind,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    FulfillmentPolicySnapshot,
    FulfillmentService,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def snapshots(
    requester_id: str = "requester-a",
    *,
    with_data: bool = True,
    purpose: str = "monthly close",
) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]:
    grounding = FulfillmentGroundingSnapshot(
        snapshot_id="grounding-1",
        tenant_id="tenant-a",
        catalog_publication_id="publication-1",
        catalog_publication_intent_digest=DIGEST,
        catalog_round_trip_observation_digest=DIGEST,
        semantic_version_ref=reference("semantic-1"),
        integration_contract_ref=reference("contract-1"),
        process_package_ref=reference("process-1"),
        governed_dataset_refs=(reference("product-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        classification_refs=(),
        lineage_refs=(reference("lineage-revenue"),),
        freshness_observation_ref=reference("freshness-1") if with_data else None,
        quality_observation_refs=(),
        authorization_policy_ref=reference("policy-1"),
        data_observation_refs=(reference("observation-1"),) if with_data else (),
        as_of=NOW,
        created_at=NOW,
    )
    policy = FulfillmentPolicySnapshot(
        snapshot_id="policy-1",
        tenant_id="tenant-a",
        requester_id=requester_id,
        requester_principal_ref=f"principal:{requester_id}",
        purpose_digest=digest(purpose),
        approved_policy_refs=(reference("policy-1"),),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(),
        permitted_data_product_refs=(reference("product-revenue"),),
        permitted_access_modes=("query",),
        maximum_expiry=NOW + timedelta(days=30),
        policy_authority_classifications=(),
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )
    return grounding, policy


class StaticSnapshotResolver:
    def __init__(
        self, value: tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]
    ) -> None:
        self.value = value

    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]:
        if tenant_id != "tenant-a" or request.tenant_id != tenant_id:
            raise ValueError("snapshot resolution authority mismatch")
        return self.value


class StaticAnswerProvider:
    def __init__(self, answer: StakeholderAnswerDraft) -> None:
        self.answer = answer

    def propose(self, *, request: object, grounding: object) -> StakeholderAnswerDraft:
        return self.answer


class CurrentFreshness:
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> str:
        return "current" if grounding.freshness_observation_ref is not None else "unknown"


def answer(*, missing_metric: bool = False) -> StakeholderAnswerDraft:
    return StakeholderAnswerDraft(
        answer_text="Net revenue is gross revenue less approved refunds.",
        governed_dataset_refs=(reference("product-revenue"),),
        metric_refs=(reference("metric-missing" if missing_metric else "metric-net-revenue"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(),
    )


def service(
    *,
    missing_metric: bool = False,
    with_data: bool = True,
    policy_purpose: str = "monthly close",
    answer_provider: StaticAnswerProvider | None = None,
) -> tuple[
    FulfillmentService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
]:
    request_repository = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(request_repository, clock=lambda: NOW)
    fulfillment_repository = SQLiteFulfillmentRepository(request_repository)
    fulfillment_service = FulfillmentService(
        request_service=request_service,
        repository=fulfillment_repository,
        snapshot_resolver=StaticSnapshotResolver(
            snapshots(with_data=with_data, purpose=policy_purpose)
        ),
        answer_candidate_provider=answer_provider
        or StaticAnswerProvider(answer(missing_metric=missing_metric)),
        policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=CurrentFreshness()),
        clock=lambda: NOW,
    )
    return fulfillment_service, request_service, fulfillment_repository


def submit_and_clarify(fulfillment: FulfillmentService, requests: RequestManagementService):
    submitted = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="monthly close",
        question="What is net revenue?",
    )
    statement = fulfillment.clarify_outcome(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        restated_request="Provide governed net revenue.",
        in_scope_summary="Approved product and metric.",
        out_of_scope_summary="No raw rows.",
        expected_revision=submitted.revision,
    )
    return requests.get("tenant-a", submitted.request_id), statement


def test_direct_clarification_and_answer_proposal_are_persisted() -> None:
    fulfillment, requests, repository = service()
    investigating, statement = submit_and_clarify(fulfillment, requests)

    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert isinstance(statement, ClarifiedOutcomeStatement)
    assert investigating.state is RequestState.INVESTIGATING
    assert proposal.subject.subject_kind == "stakeholder_answer"
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.PROPOSED
    assert repository.list_proposals("tenant-a", investigating.request_id) == (proposal,)


def test_missing_metric_creates_semantic_dependency_without_proposal() -> None:
    fulfillment, requests, repository = service(missing_metric=True)
    investigating, _ = submit_and_clarify(fulfillment, requests)

    dependency = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert dependency.kind == "semantic_change"
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.INVESTIGATING
    assert repository.list_proposals("tenant-a", investigating.request_id) == ()
    child = requests.get("tenant-a", dependency.child_request_id)
    assert child.payload.request_type == "schema_semantic_change"


def test_missing_factual_observation_creates_data_product_dependency() -> None:
    fulfillment, requests, repository = service(with_data=False)
    investigating, _ = submit_and_clarify(fulfillment, requests)

    dependency = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert dependency.kind == "data_product_change"
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.INVESTIGATING
    assert repository.list_proposals("tenant-a", investigating.request_id) == ()
    child = requests.get("tenant-a", dependency.child_request_id)
    assert child.payload.request_type == "data_product_change"


def test_unadmitted_purpose_stores_no_valid_plan_atomically() -> None:
    fulfillment, requests, repository = service(policy_purpose="unrelated purpose")
    investigating, _ = submit_and_clarify(fulfillment, requests)

    result = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert result.reason_codes == ("purpose_authority_mismatch",)
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.NO_VALID_PLAN
    assert repository.list_no_valid_plans("tenant-a", investigating.request_id) == (result,)


def test_material_answer_edit_creates_a_linked_proposal_revision() -> None:
    provider = StaticAnswerProvider(answer())
    fulfillment, requests, _ = service(answer_provider=provider)
    investigating, _ = submit_and_clarify(fulfillment, requests)
    first = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    provider.answer = provider.answer.model_copy(
        update={"answer_text": "Net revenue after approved refund adjustments."}
    )

    second = fulfillment.revise_proposal(
        tenant_id="tenant-a",
        request_id=first.request_id,
        actor_id="architect-a",
        expected_revision=first.request_revision,
    )

    assert second.revision == 2
    assert second.prior_proposal_digest == digest(first)
    assert second.subject != first.subject


def test_no_proposal_can_be_created_while_request_is_clarifying() -> None:
    fulfillment, requests, _ = service()
    submitted = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="monthly close",
        question="What is net revenue?",
    )
    clarifying = requests.transition(
        "tenant-a",
        submitted.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=submitted.revision,
    )

    with pytest.raises(ValueError, match="investigating"):
        fulfillment.propose_answer(
            tenant_id="tenant-a",
            request_id=clarifying.request_id,
            actor_id="architect-a",
            expected_revision=clarifying.revision,
        )


def test_semantic_formation_decision_cannot_bind_a_fulfillment_proposal_revision() -> None:
    fulfillment, requests, _ = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    proposed = requests.get("tenant-a", investigating.request_id)

    with pytest.raises(ValueError, match="fulfillment proposal"):
        requests.record_decision(
            tenant_id="tenant-a",
            request_id=proposed.request_id,
            request_revision=proposed.revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest=DIGEST,
        )
