from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AccessScopePreview,
    ClarifiedOutcomeStatement,
    FreshnessDisposition,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    FulfillmentPolicySnapshot,
    InboxRequest,
    RequestState,
    StakeholderAnswerDraft,
)
from heinzel_request_management.models import DataAccessRequest, StakeholderQuestion

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def request(*, access: bool = False) -> InboxRequest:
    payload = (
        DataAccessRequest(
            purpose="finance analysis",
            data_product_id="product-revenue",
            requested_fields=("invoice_id", "refund_amount"),
            access_mode="query",
            expires_at=NOW + timedelta(days=7),
        )
        if access
        else StakeholderQuestion(purpose="monthly close", question="What is net revenue?")
    )
    return InboxRequest(
        request_id="request-1",
        tenant_id="tenant-a",
        requester_id="requester-a",
        payload=payload,
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def clarified(current: InboxRequest) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="outcome-1",
        tenant_id=current.tenant_id,
        request_id=current.request_id,
        request_revision=current.revision,
        restated_request="Provide governed revenue information.",
        purpose_digest=digest(current.payload.purpose),
        in_scope_summary="Approved product, metric, and lineage.",
        out_of_scope_summary="No raw rows.",
        created_at=NOW,
    )


def grounding(*, with_data: bool = True) -> FulfillmentGroundingSnapshot:
    return FulfillmentGroundingSnapshot(
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
        classification_refs=(reference("classification-finance"),),
        lineage_refs=(reference("lineage-revenue"),),
        freshness_observation_ref=reference("freshness-1") if with_data else None,
        quality_observation_refs=(),
        authorization_policy_ref=reference("policy-finance"),
        data_observation_refs=(reference("observation-revenue"),) if with_data else (),
        as_of=NOW,
        created_at=NOW,
    )


def policy(
    *,
    permitted: bool = True,
    classified: bool = False,
    purpose: str = "monthly close",
) -> FulfillmentPolicySnapshot:
    return FulfillmentPolicySnapshot(
        snapshot_id="policy-1",
        tenant_id="tenant-a",
        requester_id="requester-a",
        requester_principal_ref="principal:requester-a",
        purpose_digest=digest(purpose),
        approved_policy_refs=(reference("policy-finance"),),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(reference("classification-rule-1"),),
        permitted_data_product_refs=(reference("product-revenue"),) if permitted else (),
        permitted_access_modes=("query",) if permitted else (),
        maximum_expiry=NOW + timedelta(days=30),
        policy_authority_classifications=("classification-finance",) if classified else (),
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def answer(*, classified: bool = False) -> StakeholderAnswerDraft:
    return StakeholderAnswerDraft(
        answer_text="Net revenue is gross revenue minus approved refunds.",
        governed_dataset_refs=(reference("product-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(reference("classification-finance"),) if classified else (),
    )


class CurrentFreshnessEvaluator:
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition:
        return "current" if grounding.freshness_observation_ref is not None else "unknown"


def compiler() -> FulfillmentPolicyCompiler:
    return FulfillmentPolicyCompiler(freshness_evaluator=CurrentFreshnessEvaluator())


def test_answer_requires_requester_and_architect_but_not_product_owner() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(),
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "proposal"
    assert tuple(item.authority_ref for item in result.proposal.required_approvals) == (
        "principal:requester-a",
        "role:data_engineering_architect",
    )
    assert "role:data_product_owner" not in {
        item.authority_ref for item in result.proposal.required_approvals
    }


def test_classified_answer_adds_policy_authority() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(classified=True),
        candidate=answer(classified=True),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "proposal"
    assert tuple(item.authority_ref for item in result.proposal.required_approvals) == (
        "principal:requester-a",
        "role:data_engineering_architect",
        "role:policy_authority",
    )


def test_missing_factual_observation_creates_data_product_dependency() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(),
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "dependency"
    assert result.dependency_kind == "data_product_change"
    assert result.missing_capability_refs == ("freshness:data_observation",)


@pytest.mark.parametrize(
    ("candidate_update", "expected_capability"),
    (
        ({"metric_refs": (reference("metric-missing"),)}, "semantic:metric-missing"),
        ({"lineage_refs": (reference("lineage-missing"),)}, "semantic:lineage-missing"),
    ),
)
def test_each_missing_semantic_reference_creates_a_dependency(
    candidate_update: dict[str, object], expected_capability: str
) -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(),
        candidate=answer().model_copy(update=candidate_update),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "dependency"
    assert result.dependency_kind == "semantic_change"
    assert result.reason_code == "missing_approved_semantics"
    assert result.missing_capability_refs == (expected_capability,)


def test_unentitled_answer_compiles_to_denial_not_dependency() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(permitted=False),
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "denial"
    assert result.proposal.subject.subject_kind == "disclosure_denial"
    assert result.proposal.subject.requester_safe_explanation == (
        "The requested disclosure is not available for this purpose."
    )


def test_access_requires_exact_owner_and_conditional_policy_authority() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id",),
        excluded_scopes=("refund_amount",),
        classifications=(reference("classification-finance"),),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(classified=True, purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="owner:product-revenue",
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "proposal"
    assert tuple(item.authority_ref for item in result.proposal.required_approvals) == (
        "owner:product-revenue",
        "principal:requester-a",
        "role:policy_authority",
    )
    assert "role:data_engineering_architect" not in {
        item.authority_ref for item in result.proposal.required_approvals
    }


def test_access_compilation_refuses_cross_tenant_grounding() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id",),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id",),
        excluded_scopes=(),
        classifications=(),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False).model_copy(update={"tenant_id": "tenant-b"}),
        policy=policy(purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="owner:product-revenue",
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("tenant_authority_mismatch",)


def test_missing_access_owner_returns_no_valid_plan() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id",),
        excluded_scopes=("refund_amount",),
        classifications=(),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="",
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("data_product_owner_unresolved",)


def test_expired_policy_snapshot_returns_no_valid_plan() -> None:
    current = request()
    expired_policy = policy().model_copy(
        update={
            "observed_at": NOW - timedelta(hours=2),
            "valid_until": NOW - timedelta(hours=1),
        }
    )

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=expired_policy,
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("policy_snapshot_expired",)


def test_policy_snapshot_expiring_at_compilation_time_is_expired() -> None:
    current = request()
    boundary_policy = policy().model_copy(update={"valid_until": NOW})

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=boundary_policy,
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("policy_snapshot_expired",)


def test_unadmitted_quality_reference_is_a_data_capability_dependency() -> None:
    current = request()
    candidate = answer().model_copy(
        update={"material_quality_limitations": (reference("quality-missing"),)}
    )

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(),
        candidate=candidate,
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "dependency"
    assert result.dependency_kind == "data_product_change"
    assert result.missing_capability_refs == ("quality:quality-missing",)


def test_access_scope_widening_returns_no_valid_plan() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id", "bank_account"),
        excluded_scopes=("refund_amount",),
        classifications=(),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="owner:product-revenue",
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("access_scope_invalid",)


def test_colliding_requester_and_owner_authorities_return_no_valid_plan() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id",),
        excluded_scopes=("refund_amount",),
        classifications=(),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="principal:requester-a",
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"


def test_reordered_candidate_references_compile_identically() -> None:
    current = request()
    first_metric = reference("metric-net-revenue")
    second_metric = reference("metric-refunds")
    admitted = grounding().model_copy(update={"metric_refs": (first_metric, second_metric)})
    forward = answer().model_copy(update={"metric_refs": (first_metric, second_metric)})
    reverse = answer().model_copy(update={"metric_refs": (second_metric, first_metric)})

    first = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=admitted,
        policy=policy(),
        candidate=forward,
        proposal_revision=1,
        created_at=NOW,
    )
    second = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=admitted,
        policy=policy(),
        candidate=reverse,
        proposal_revision=1,
        created_at=NOW,
    )

    assert first.outcome_kind == second.outcome_kind == "proposal"
    assert first.proposal.subject == second.proposal.subject
    assert first.proposal.required_approvals == second.proposal.required_approvals


@pytest.mark.parametrize("mismatched_input", ["clarified", "grounding", "policy"])
def test_each_cross_tenant_authority_input_returns_no_valid_plan(mismatched_input: str) -> None:
    current = request()
    outcome = clarified(current)
    admitted = grounding()
    entitlement = policy()
    if mismatched_input == "clarified":
        outcome = outcome.model_copy(update={"tenant_id": "tenant-b"})
    elif mismatched_input == "grounding":
        admitted = admitted.model_copy(update={"tenant_id": "tenant-b"})
    else:
        entitlement = entitlement.model_copy(update={"tenant_id": "tenant-b"})

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=outcome,
        grounding=admitted,
        policy=entitlement,
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("tenant_authority_mismatch",)


def test_purpose_mismatch_returns_no_valid_plan() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(purpose="unrelated purpose"),
        candidate=answer(),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "no_valid_plan"
    assert result.reason_codes == ("purpose_authority_mismatch",)


def test_classified_denial_requires_architect_and_policy_authority() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(permitted=False, classified=True),
        candidate=answer(classified=True),
        proposal_revision=1,
        created_at=NOW,
    )

    assert result.outcome_kind == "denial"
    assert tuple(item.authority_ref for item in result.proposal.required_approvals) == (
        "principal:requester-a",
        "role:data_engineering_architect",
        "role:policy_authority",
    )


def test_revised_proposal_preserves_prior_digest() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(),
        candidate=answer(),
        proposal_revision=2,
        prior_proposal_digest=DIGEST,
        created_at=NOW,
    )

    assert result.outcome_kind == "proposal"
    assert result.proposal.revision == 2
    assert result.proposal.prior_proposal_digest == DIGEST


def test_revised_denial_preserves_prior_digest() -> None:
    current = request()

    result = compiler().compile_answer(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(),
        policy=policy(permitted=False),
        candidate=answer(),
        proposal_revision=2,
        prior_proposal_digest=DIGEST,
        created_at=NOW,
    )

    assert result.outcome_kind == "denial"
    assert result.proposal.revision == 2
    assert result.proposal.prior_proposal_digest == DIGEST


def test_revised_access_proposal_preserves_prior_digest() -> None:
    current = request(access=True)
    preview = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("product-revenue"),),
        effective_fields=("invoice_id",),
        excluded_scopes=("refund_amount",),
        classifications=(),
        expires_at=NOW + timedelta(days=1),
    )

    result = compiler().compile_access(
        request=current,
        clarified_outcome=clarified(current),
        grounding=grounding(with_data=False),
        policy=policy(purpose="finance analysis"),
        preview=preview,
        data_product_owner_authority_ref="owner:product-revenue",
        proposal_revision=2,
        prior_proposal_digest=DIGEST,
        created_at=NOW,
    )

    assert result.outcome_kind == "proposal"
    assert result.proposal.revision == 2
    assert result.proposal.prior_proposal_digest == DIGEST
