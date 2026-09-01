from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AccessScopePreview,
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DataAccessRequest,
    DataProductChangeRequest,
    FulfillmentEvidenceReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    InboxRequest,
    RequestState,
    StakeholderAnswerDraft,
    StakeholderQuestion,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def question_request() -> InboxRequest:
    return InboxRequest(
        request_id="request-1",
        tenant_id="tenant-a",
        requester_id="requester-a",
        payload=StakeholderQuestion(purpose="monthly close", question="What is net revenue?"),
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def access_request() -> InboxRequest:
    return InboxRequest(
        request_id="request-2",
        tenant_id="tenant-a",
        requester_id="requester-a",
        payload=DataAccessRequest(
            purpose="refund analysis",
            data_product_id="product-revenue",
            requested_fields=("invoice_id", "refund_amount"),
            access_mode="query",
            expires_at=NOW + timedelta(days=7),
        ),
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def grounding(*, with_observation: bool) -> FulfillmentGroundingSnapshot:
    freshness_reference = reference("freshness-1") if with_observation else None
    return FulfillmentGroundingSnapshot(
        snapshot_id="grounding-1",
        tenant_id="tenant-a",
        catalog_publication_id="publication-1",
        catalog_publication_intent_digest=DIGEST,
        catalog_round_trip_observation_digest=DIGEST,
        semantic_version_ref=reference("semantic-1"),
        integration_contract_ref=reference("contract-1"),
        process_package_ref=reference("process-1"),
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        classification_refs=(reference("classification-finance"),),
        lineage_refs=(reference("lineage-revenue"),),
        freshness_observation_ref=freshness_reference,
        quality_observation_refs=(reference("quality-revenue"),),
        authorization_policy_ref=reference("policy-finance"),
        data_observation_refs=(reference("observation-revenue"),) if with_observation else (),
        as_of=NOW,
        created_at=NOW,
    )


def policy(*, tenant_id: str = "tenant-a") -> FulfillmentPolicySnapshot:
    return FulfillmentPolicySnapshot(
        snapshot_id="policy-snapshot-1",
        tenant_id=tenant_id,
        requester_id="requester-a",
        requester_principal_ref="principal:requester-a",
        purpose_digest=DIGEST,
        approved_policy_refs=(reference("policy-finance"),),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(reference("classification-rule-1"),),
        permitted_data_product_refs=(reference("product-revenue"),),
        permitted_access_modes=("query",),
        maximum_expiry=NOW + timedelta(days=30),
        policy_authority_classifications=("finance",),
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )


def clarified(request: InboxRequest) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="clarified-1",
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        restated_request="Provide the governed net revenue definition.",
        purpose_digest=DIGEST,
        in_scope_summary="Approved metric definition and lineage.",
        out_of_scope_summary="No raw rows or unrestricted query output.",
        created_at=NOW,
    )


def requirements(
    statement: ClarifiedOutcomeStatement,
    subject: StakeholderAnswerDraft | AccessScopePreview,
) -> tuple[ApprovalRequirement, ...]:
    return (
        ApprovalRequirement(
            authority_ref="principal:requester-a",
            reason_code="clarified_outcome_acceptance",
            subject_digest=digest(statement),
        ),
        ApprovalRequirement(
            authority_ref="role:data_engineering_architect",
            reason_code="architect_review",
            subject_digest=digest(subject),
        ),
    )


def test_data_product_change_is_a_strict_inbox_payload() -> None:
    request = InboxRequest(
        request_id="request-3",
        tenant_id="tenant-a",
        requester_id="architect-a",
        payload=DataProductChangeRequest(
            purpose="answer blocked",
            requested_outcome="Publish governed net revenue observations.",
            missing_capability_refs=("metric:net-revenue",),
            source_request_id="request-1",
            source_request_revision=2,
        ),
        state=RequestState.SUBMITTED,
        revision=1,
        submitted_at=NOW,
        updated_at=NOW,
    )

    assert request.payload.request_type == "data_product_change"

    with pytest.raises(ValidationError):
        DataProductChangeRequest.model_validate(
            {
                **request.payload.model_dump(mode="json"),
                "warehouse_role": "must-not-be-accepted",
            }
        )


def test_current_answer_requires_freshness_and_data_observations() -> None:
    request = question_request()
    statement = clarified(request)
    subject = StakeholderAnswerDraft(
        answer_text="Net revenue is gross revenue minus approved refunds.",
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(reference("classification-finance"),),
    )

    with pytest.raises(ValueError, match="freshness observation"):
        FulfillmentProposal.create(
            proposal_id="proposal-1",
            request=request,
            clarified_outcome=statement,
            grounding=grounding(with_observation=False),
            policy=policy(),
            subject=subject,
            required_approvals=requirements(statement, subject),
            revision=1,
            created_at=NOW,
        )


def test_answer_citations_must_come_from_the_exact_grounding_snapshot() -> None:
    request = question_request()
    statement = clarified(request)
    subject = StakeholderAnswerDraft(
        answer_text="A candidate with an unapproved metric.",
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-unapproved"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(reference("classification-finance"),),
    )

    with pytest.raises(ValueError, match="metric citation"):
        FulfillmentProposal.create(
            proposal_id="proposal-1",
            request=request,
            clarified_outcome=statement,
            grounding=grounding(with_observation=True),
            policy=policy(),
            subject=subject,
            required_approvals=requirements(statement, subject),
            revision=1,
            created_at=NOW,
        )


def test_access_preview_can_narrow_but_never_widen_requested_fields() -> None:
    request = access_request()
    statement = clarified(request)
    subject = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=reference("product-revenue"),
        access_mode="query",
        requested_fields=("invoice_id", "refund_amount"),
        effective_object_refs=(reference("dataset-revenue"),),
        effective_fields=("invoice_id", "customer_email"),
        excluded_scopes=("raw_customer_identity",),
        classifications=(reference("classification-finance"),),
        expires_at=NOW + timedelta(days=1),
    )

    with pytest.raises(ValueError, match="effective fields"):
        FulfillmentProposal.create(
            proposal_id="proposal-2",
            request=request,
            clarified_outcome=statement,
            grounding=grounding(with_observation=False),
            policy=policy(),
            subject=subject,
            required_approvals=requirements(statement, subject),
            revision=1,
            created_at=NOW,
        )


def test_proposal_binds_clarified_outcome_and_private_subject_digests() -> None:
    request = question_request()
    statement = clarified(request)
    subject = StakeholderAnswerDraft(
        answer_text="Net revenue is gross revenue minus approved refunds.",
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(reference("quality-revenue"),),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(reference("classification-finance"),),
    )

    proposal = FulfillmentProposal.create(
        proposal_id="proposal-1",
        request=request,
        clarified_outcome=statement,
        grounding=grounding(with_observation=True),
        policy=policy(),
        subject=subject,
        required_approvals=requirements(statement, subject),
        revision=1,
        created_at=NOW,
    )

    assert proposal.request_revision == request.revision + 1
    assert proposal.clarified_outcome_digest == digest(statement)
    assert {item.subject_digest for item in proposal.required_approvals} == {
        digest(statement),
        digest(subject),
    }
    revised_payload = {**proposal.model_dump(mode="json"), "revision": 2}

    with pytest.raises(ValidationError):
        FulfillmentProposal.model_validate(revised_payload)


def test_fulfillment_evidence_rejects_private_candidate_content() -> None:
    receipt = FulfillmentEvidenceReceipt(
        evidence_id="evidence-1",
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=3,
        outcome="execution_ready",
        proposal_id="proposal-1",
        proposal_revision=1,
        dependency_id=None,
        authority_refs=("role:data_engineering_architect",),
        approval_ids=("approval-1",),
        reason_codes=(),
        resulting_state=RequestState.EXECUTING,
        created_at=NOW,
    )
    payload = receipt.model_dump(mode="json")
    payload["answer_text"] = "must not enter evidence"

    with pytest.raises(ValidationError):
        FulfillmentEvidenceReceipt.model_validate(payload)


def test_policy_snapshot_requires_a_positive_utc_observation_window() -> None:
    payload = policy().model_dump(mode="python")
    payload["valid_until"] = NOW - timedelta(seconds=1)

    with pytest.raises(ValidationError, match="valid_until"):
        FulfillmentPolicySnapshot.model_validate(payload)
