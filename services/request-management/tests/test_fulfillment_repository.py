import sqlite3
from datetime import UTC, datetime

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DataProductChangeRequest,
    DenialDispositionReceipt,
    DisclosureDenial,
    FulfillmentAdmissionReceipt,
    FulfillmentApprovalBinding,
    FulfillmentEvidenceReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    InboxRequest,
    RequestDependency,
    RequestManagementService,
    RequestNoValidPlan,
    RequestState,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)
from pillarmesh_request_management.repository import StaleRevisionError

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def repositories() -> tuple[
    SQLiteRequestRepository,
    SQLiteFulfillmentRepository,
    RequestManagementService,
]:
    connection = sqlite3.connect(":memory:")
    request_repository = SQLiteRequestRepository(connection)
    fulfillment_repository = SQLiteFulfillmentRepository(request_repository)
    service = RequestManagementService(request_repository, clock=lambda: NOW)
    return request_repository, fulfillment_repository, service


def investigating_question(
    service: RequestManagementService,
) -> tuple[str, int]:
    request = service.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="monthly close",
        question="What is net revenue?",
    )
    investigated = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=request.revision,
    )
    return investigated.request_id, investigated.revision


def clarified_statement(request_id: str, request_revision: int) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=request_revision,
        restated_request="Provide the governed net revenue definition.",
        purpose_digest=digest("monthly close"),
        in_scope_summary="Approved metric definition and lineage.",
        out_of_scope_summary="No raw rows.",
        created_at=NOW,
    )


def proposal_artifacts(
    *,
    service: RequestManagementService,
    statement: ClarifiedOutcomeStatement,
) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot, FulfillmentProposal]:
    request = service.get("tenant-a", statement.request_id)
    grounding = FulfillmentGroundingSnapshot(
        snapshot_id="pending",
        tenant_id="tenant-a",
        catalog_publication_id="publication-1",
        catalog_publication_intent_digest=DIGEST,
        catalog_round_trip_observation_digest=DIGEST,
        semantic_version_ref=reference("semantic-1"),
        integration_contract_ref=reference("contract-1"),
        process_package_ref=reference("process-1"),
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        classification_refs=(),
        lineage_refs=(reference("lineage-revenue"),),
        freshness_observation_ref=reference("freshness-1"),
        quality_observation_refs=(),
        authorization_policy_ref=reference("policy-finance"),
        data_observation_refs=(reference("data-observation-1"),),
        as_of=NOW,
        created_at=NOW,
    )
    policy = FulfillmentPolicySnapshot(
        snapshot_id="pending",
        tenant_id="tenant-a",
        requester_id="requester-a",
        requester_principal_ref="principal:requester-a",
        purpose_digest=statement.purpose_digest,
        approved_policy_refs=(reference("policy-finance"),),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(),
        permitted_data_product_refs=(),
        permitted_access_modes=(),
        maximum_expiry=None,
        policy_authority_classifications=(),
        observed_at=NOW,
        valid_until=NOW.replace(hour=13),
    )
    subject = StakeholderAnswerDraft(
        answer_text="Net revenue is gross revenue minus approved refunds.",
        governed_dataset_refs=(reference("dataset-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        as_of=NOW,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(),
    )
    requirements = (
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
    proposal = FulfillmentProposal.create(
        proposal_id="pending",
        request=request,
        clarified_outcome=statement,
        grounding=grounding,
        policy=policy,
        subject=subject,
        required_approvals=requirements,
        revision=1,
        created_at=NOW,
    )
    return grounding, policy, proposal


def test_clarified_outcome_allocates_once_and_replays_exactly() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)

    stored = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    replayed = repository.store_clarified_outcome(stored, expected_revision=request_revision)

    assert stored.statement_id.startswith("out-")
    assert replayed == stored
    assert repository.list_clarified_outcomes("tenant-a", request_id) == (stored,)


def test_clarified_outcome_refuses_stale_and_cross_tenant_requests() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = clarified_statement(request_id, request_revision)

    with pytest.raises(StaleRevisionError, match="stale"):
        repository.store_clarified_outcome(statement, expected_revision=request_revision - 1)

    foreign = statement.model_copy(update={"tenant_id": "tenant-b"})
    with pytest.raises(KeyError, match="another tenant"):
        repository.store_clarified_outcome(foreign, expected_revision=request_revision)

    assert repository.list_clarified_outcomes("tenant-a", request_id) == ()


def test_clarified_outcome_failure_rolls_back_artifact_and_sequence() -> None:
    request_repository, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    request_repository._connection.execute(
        "CREATE TRIGGER fail_clarified_outcome BEFORE INSERT ON clarified_outcomes "
        "BEGIN SELECT RAISE(ABORT, 'forced clarified outcome failure'); END"
    )
    request_repository._connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced clarified outcome failure"):
        repository.store_clarified_outcome(
            clarified_statement(request_id, request_revision),
            expected_revision=request_revision,
        )

    sequence = request_repository._connection.execute(
        "SELECT next_sequence FROM artifact_sequences WHERE tenant_id = ? AND artifact_kind = ?",
        ("tenant-a", "clarified_outcome"),
    ).fetchone()
    assert sequence is None
    assert repository.list_clarified_outcomes("tenant-a", request_id) == ()


def test_proposal_and_request_transition_commit_together() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    grounding, policy, proposal = proposal_artifacts(service=service, statement=statement)

    stored = repository.store_proposal(
        grounding=grounding,
        policy=policy,
        proposal=proposal,
        actor_id="architect-a",
        expected_revision=request_revision,
    )

    current = service.get("tenant-a", request_id)
    assert current.state is RequestState.PROPOSED
    assert current.revision == request_revision + 1
    assert stored.proposal_id.startswith("prp-")
    assert stored.request_revision == current.revision
    assert repository.list_proposals("tenant-a", request_id) == (stored,)
    assert repository.load_grounding_snapshot(
        "tenant-a", stored.grounding_snapshot_digest
    ).snapshot_id.startswith("grd-")
    assert repository.load_policy_snapshot(
        "tenant-a", stored.policy_snapshot_digest
    ).snapshot_id.startswith("pol-")


def test_proposal_failure_rolls_back_request_transition_and_all_artifacts() -> None:
    request_repository, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    grounding, policy, proposal = proposal_artifacts(service=service, statement=statement)
    history_before = service.list_transition_history("tenant-a", request_id)
    request_repository._connection.execute(
        "CREATE TRIGGER fail_fulfillment_proposal BEFORE INSERT ON fulfillment_proposals "
        "BEGIN SELECT RAISE(ABORT, 'forced proposal failure'); END"
    )
    request_repository._connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced proposal failure"):
        repository.store_proposal(
            grounding=grounding,
            policy=policy,
            proposal=proposal,
            actor_id="architect-a",
            expected_revision=request_revision,
        )

    current = service.get("tenant-a", request_id)
    assert current.state is RequestState.INVESTIGATING
    assert current.revision == request_revision
    assert service.list_transition_history("tenant-a", request_id) == history_before
    assert repository.list_proposals("tenant-a", request_id) == ()
    assert request_repository._connection.execute(
        "SELECT COUNT(*) FROM fulfillment_grounding_snapshots"
    ).fetchone() == (0,)
    assert request_repository._connection.execute(
        "SELECT COUNT(*) FROM fulfillment_policy_snapshots"
    ).fetchone() == (0,)


def test_approval_and_admission_are_append_only_and_atomic() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    grounding, policy, proposal = proposal_artifacts(service=service, statement=statement)
    stored_proposal = repository.store_proposal(
        grounding=grounding,
        policy=policy,
        proposal=proposal,
        actor_id="architect-a",
        expected_revision=request_revision,
    )
    awaiting = repository.submit_proposal(
        tenant_id="tenant-a",
        request_id=request_id,
        actor_id="architect-a",
        expected_revision=stored_proposal.request_revision,
        created_at=NOW,
    )
    approvals = tuple(
        repository.store_approval(
            FulfillmentApprovalBinding(
                approval_id="pending",
                tenant_id="tenant-a",
                request_id=request_id,
                request_revision=awaiting.revision,
                proposal_id=stored_proposal.proposal_id,
                proposal_revision=stored_proposal.revision,
                proposal_digest=digest(stored_proposal),
                subject_digest=requirement.subject_digest,
                actor_id="requester-a" if index == 0 else "architect-a",
                authority_ref=requirement.authority_ref,
                decision="approve",
                created_at=NOW,
            )
        )
        for index, requirement in enumerate(stored_proposal.required_approvals)
    )
    admission = FulfillmentAdmissionReceipt(
        admission_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        source_request_revision=awaiting.revision,
        resulting_request_revision=awaiting.revision + 1,
        proposal_id=stored_proposal.proposal_id,
        proposal_revision=stored_proposal.revision,
        proposal_digest=digest(stored_proposal),
        grounding_snapshot_digest=stored_proposal.grounding_snapshot_digest,
        policy_snapshot_digest=stored_proposal.policy_snapshot_digest,
        approval_ids=tuple(approval.approval_id for approval in approvals),
        admitted_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=awaiting.revision + 1,
        outcome="execution_ready",
        proposal_id=stored_proposal.proposal_id,
        proposal_revision=stored_proposal.revision,
        dependency_id=None,
        authority_refs=tuple(approval.authority_ref for approval in approvals),
        approval_ids=tuple(approval.approval_id for approval in approvals),
        reason_codes=(),
        resulting_state=RequestState.EXECUTING,
        created_at=NOW,
    )

    stored_admission = repository.admit(
        admission=admission,
        evidence=evidence,
        actor_id="system:fulfillment",
        expected_revision=awaiting.revision,
    )
    replayed_admission = repository.admit(
        admission=admission,
        evidence=evidence,
        actor_id="system:fulfillment",
        expected_revision=awaiting.revision,
    )

    assert stored_admission.admission_id.startswith("adm-")
    assert replayed_admission == stored_admission
    assert service.get("tenant-a", request_id).state is RequestState.EXECUTING
    assert repository.list_approvals("tenant-a", request_id) == approvals
    assert repository.list_admissions("tenant-a", request_id) == (stored_admission,)
    receipts = repository.list_evidence("tenant-a", request_id)
    assert len(receipts) == 1
    assert receipts[0].evidence_id.startswith("evd-")
    assert receipts[0].outcome == "execution_ready"

    with pytest.raises(ValueError, match="replay"):
        repository.admit(
            admission=admission.model_copy(update={"proposal_digest": "1" * 64}),
            evidence=evidence,
            actor_id="system:fulfillment",
            expected_revision=awaiting.revision,
        )


def test_no_valid_plan_and_request_transition_commit_together() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    record = RequestNoValidPlan(
        record_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        source_request_revision=request_revision,
        resulting_request_revision=request_revision + 1,
        reason_codes=("authority_unverifiable",),
        constraint_refs=(),
        smallest_changes=("Publish an approved authority observation.",),
        grounding_snapshot_digest=None,
        policy_snapshot_digest=None,
        created_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=request_revision + 1,
        outcome="no_valid_plan",
        proposal_id=None,
        proposal_revision=None,
        dependency_id=None,
        authority_refs=(),
        approval_ids=(),
        reason_codes=record.reason_codes,
        resulting_state=RequestState.NO_VALID_PLAN,
        created_at=NOW,
    )

    stored = repository.store_no_valid_plan(
        record=record,
        evidence=evidence,
        actor_id="system:fulfillment",
        expected_revision=request_revision,
    )

    assert stored.record_id.startswith("nvp-")
    assert service.get("tenant-a", request_id).state is RequestState.NO_VALID_PLAN
    assert repository.list_no_valid_plans("tenant-a", request_id) == (stored,)
    assert repository.list_evidence("tenant-a", request_id)[0].outcome == "no_valid_plan"


def test_cancellation_abandons_an_open_proposal_without_admission() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    grounding, policy, proposal = proposal_artifacts(service=service, statement=statement)
    stored_proposal = repository.store_proposal(
        grounding=grounding,
        policy=policy,
        proposal=proposal,
        actor_id="architect-a",
        expected_revision=request_revision,
    )
    awaiting = repository.submit_proposal(
        tenant_id="tenant-a",
        request_id=request_id,
        actor_id="architect-a",
        expected_revision=stored_proposal.request_revision,
        created_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=awaiting.revision + 1,
        outcome="cancelled",
        proposal_id=None,
        proposal_revision=None,
        dependency_id=None,
        authority_refs=(),
        approval_ids=(),
        reason_codes=("requester_cancelled",),
        resulting_state=RequestState.CANCELLED,
        created_at=NOW,
    )

    receipt = repository.record_cancellation(
        evidence=evidence,
        actor_id="requester-a",
        expected_revision=awaiting.revision,
    )

    assert receipt.evidence_id.startswith("evd-")
    assert service.get("tenant-a", request_id).state is RequestState.CANCELLED
    assert repository.list_admissions("tenant-a", request_id) == ()
    assert repository.list_evidence("tenant-a", request_id) == (receipt,)


def test_dependency_stores_child_edge_and_evidence_without_advancing_parent() -> None:
    request_repository, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    child_sequence = request_repository.peek_next_sequence("tenant-a")
    child_id = service._request_id_for_sequence("tenant-a", child_sequence)
    child = InboxRequest(
        request_id=child_id,
        tenant_id="tenant-a",
        requester_id="system:fulfillment",
        payload=DataProductChangeRequest(
            purpose="answer blocked",
            requested_outcome="Publish governed net revenue observations.",
            missing_capability_refs=("metric:net-revenue",),
            source_request_id=request_id,
            source_request_revision=request_revision,
        ),
        state=RequestState.SUBMITTED,
        revision=1,
        submitted_at=NOW,
        updated_at=NOW,
    )
    dependency = RequestDependency(
        dependency_id="pending",
        tenant_id="tenant-a",
        parent_request_id=request_id,
        parent_request_revision=request_revision,
        child_request_id=child_id,
        child_request_revision=1,
        kind="data_product_change",
        reason_code="missing_governed_observation",
        created_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=request_revision,
        outcome="dependency",
        proposal_id=None,
        proposal_revision=None,
        dependency_id="pending",
        authority_refs=(),
        approval_ids=(),
        reason_codes=(dependency.reason_code,),
        resulting_state=RequestState.INVESTIGATING,
        created_at=NOW,
    )

    stored = repository.store_dependency(
        child=child,
        dependency=dependency,
        evidence=evidence,
        expected_revision=request_revision,
    )

    assert stored.dependency_id.startswith("dep-")
    parent = service.get("tenant-a", request_id)
    assert parent.revision == request_revision
    assert parent.state is RequestState.INVESTIGATING
    assert service.get("tenant-a", child_id) == child
    assert repository.list_dependencies("tenant-a", request_id) == (stored,)
    assert repository.list_evidence("tenant-a", request_id)[0].dependency_id == stored.dependency_id


def test_denial_disposition_transitions_to_rejected_without_admission() -> None:
    _, repository, service = repositories()
    request_id, request_revision = investigating_question(service)
    statement = repository.store_clarified_outcome(
        clarified_statement(request_id, request_revision),
        expected_revision=request_revision,
    )
    grounding, policy, _ = proposal_artifacts(service=service, statement=statement)
    request = service.get("tenant-a", request_id)
    subject = DisclosureDenial(
        reason_code="disclosure_not_entitled",
        requester_safe_explanation="The requested disclosure is not available for this purpose.",
        denied_scope_digest=DIGEST,
    )
    proposal = FulfillmentProposal.create(
        proposal_id="pending",
        request=request,
        clarified_outcome=statement,
        grounding=grounding,
        policy=policy,
        subject=subject,
        required_approvals=(
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
        ),
        revision=1,
        created_at=NOW,
    )
    stored_proposal = repository.store_proposal(
        grounding=grounding,
        policy=policy,
        proposal=proposal,
        actor_id="architect-a",
        expected_revision=request_revision,
    )
    awaiting = repository.submit_proposal(
        tenant_id="tenant-a",
        request_id=request_id,
        actor_id="architect-a",
        expected_revision=stored_proposal.request_revision,
        created_at=NOW,
    )
    approvals = tuple(
        repository.store_approval(
            FulfillmentApprovalBinding(
                approval_id="pending",
                tenant_id="tenant-a",
                request_id=request_id,
                request_revision=awaiting.revision,
                proposal_id=stored_proposal.proposal_id,
                proposal_revision=stored_proposal.revision,
                proposal_digest=digest(stored_proposal),
                subject_digest=requirement.subject_digest,
                actor_id="requester-a" if index == 0 else "architect-a",
                authority_ref=requirement.authority_ref,
                decision="approve",
                created_at=NOW,
            )
        )
        for index, requirement in enumerate(stored_proposal.required_approvals)
    )
    disposition = DenialDispositionReceipt(
        disposition_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        source_request_revision=awaiting.revision,
        resulting_request_revision=awaiting.revision + 1,
        proposal_id=stored_proposal.proposal_id,
        proposal_revision=stored_proposal.revision,
        proposal_digest=digest(stored_proposal),
        policy_snapshot_digest=stored_proposal.policy_snapshot_digest,
        approval_ids=tuple(approval.approval_id for approval in approvals),
        requester_safe_explanation=subject.requester_safe_explanation,
        recorded_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=request_id,
        request_revision=awaiting.revision + 1,
        outcome="denial",
        proposal_id=stored_proposal.proposal_id,
        proposal_revision=stored_proposal.revision,
        dependency_id=None,
        authority_refs=tuple(approval.authority_ref for approval in approvals),
        approval_ids=tuple(approval.approval_id for approval in approvals),
        reason_codes=(subject.reason_code,),
        resulting_state=RequestState.REJECTED,
        created_at=NOW,
    )

    stored = repository.record_denial(
        disposition=disposition,
        evidence=evidence,
        actor_id="system:fulfillment",
        expected_revision=awaiting.revision,
    )

    assert stored.disposition_id.startswith("dny-")
    assert service.get("tenant-a", request_id).state is RequestState.REJECTED
    assert repository.list_denials("tenant-a", request_id) == (stored,)
    assert repository.list_admissions("tenant-a", request_id) == ()


def test_in_transaction_surface_refuses_to_run_without_an_open_transaction() -> None:
    """The composing repository shares one connection and one transaction.

    The ordinary public methods each open `BEGIN IMMEDIATE`, which SQLite refuses
    inside an open transaction, so a composing repository must use this surface.
    Running it outside a transaction would autocommit a fragment of an outcome, so
    it fails loudly instead.
    """
    requests = SQLiteRequestRepository.open(":memory:")
    try:
        assert not requests.connection.in_transaction
        with pytest.raises(RuntimeError, match="inside an open transaction"):
            requests.allocate_artifact_sequence_in_transaction("tenant-a", "transition")
        with pytest.raises(RuntimeError, match="inside an open transaction"):
            requests.transition_in_transaction(
                tenant_id="tenant-a",
                request_id="req-1",
                expected_revision=1,
                actor_id="architect-a",
                to_state=RequestState.INVESTIGATING,
                created_at=NOW,
            )
    finally:
        requests.close()
