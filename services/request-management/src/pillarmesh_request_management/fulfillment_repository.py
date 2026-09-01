from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest

from .fulfillment_errors import FulfillmentIntegrityError
from .fulfillment_models import (
    ClarifiedOutcomeStatement,
    DenialDispositionReceipt,
    DisclosureDenial,
    FulfillmentAdmissionReceipt,
    FulfillmentApprovalBinding,
    FulfillmentEvidenceReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    RequestDependency,
    RequestNoValidPlan,
)
from .models import (
    DataProductChangeRequest,
    InboxRequest,
    RequestState,
    SchemaSemanticChangeRequest,
)
from .repository import SQLiteRequestRepository, StaleRevisionError, _transaction


def _validate_persisted_artifact[ArtifactType: ArtifactModel](
    model: type[ArtifactType], payload: bytes
) -> ArtifactType:
    try:
        return model.model_validate_json(payload)
    except (TypeError, ValueError):
        raise FulfillmentIntegrityError("stored fulfillment artifact is invalid") from None


class FulfillmentRepository(Protocol):
    def store_clarified_outcome(
        self,
        statement: ClarifiedOutcomeStatement,
        *,
        expected_revision: int,
    ) -> ClarifiedOutcomeStatement: ...

    def list_clarified_outcomes(
        self, tenant_id: str, request_id: str
    ) -> tuple[ClarifiedOutcomeStatement, ...]: ...

    def list_proposals(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentProposal, ...]: ...

    def list_approvals(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentApprovalBinding, ...]: ...

    def load_policy_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentPolicySnapshot: ...

    def load_grounding_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentGroundingSnapshot: ...

    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentAdmissionReceipt, ...]: ...

    def list_dependencies(
        self, tenant_id: str, parent_request_id: str
    ) -> tuple[RequestDependency, ...]: ...

    def list_no_valid_plans(
        self, tenant_id: str, request_id: str
    ) -> tuple[RequestNoValidPlan, ...]: ...

    def list_denials(
        self, tenant_id: str, request_id: str
    ) -> tuple[DenialDispositionReceipt, ...]: ...

    def list_evidence(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentEvidenceReceipt, ...]: ...

    def store_proposal(
        self,
        *,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        proposal: FulfillmentProposal,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentProposal: ...

    def submit_proposal(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
        created_at: datetime,
    ) -> InboxRequest: ...

    def store_approval(
        self,
        binding: FulfillmentApprovalBinding,
        *,
        transition_actor_id: str | None = None,
    ) -> FulfillmentApprovalBinding: ...

    def admit(
        self,
        *,
        admission: FulfillmentAdmissionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
        current_policy: FulfillmentPolicySnapshot | None = None,
    ) -> FulfillmentAdmissionReceipt: ...

    def store_no_valid_plan(
        self,
        *,
        record: RequestNoValidPlan,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> RequestNoValidPlan: ...

    def record_cancellation(
        self,
        *,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentEvidenceReceipt: ...

    def store_dependency(
        self,
        *,
        child: InboxRequest,
        dependency: RequestDependency,
        evidence: FulfillmentEvidenceReceipt,
        expected_revision: int,
    ) -> RequestDependency: ...

    def record_denial(
        self,
        *,
        disposition: DenialDispositionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> DenialDispositionReceipt: ...


class SQLiteFulfillmentRepository:
    def __init__(self, request_repository: SQLiteRequestRepository) -> None:
        self._requests = request_repository
        self._connection = request_repository.connection
        self._initialize_schema()

    def store_clarified_outcome(
        self,
        statement: ClarifiedOutcomeStatement,
        *,
        expected_revision: int,
    ) -> ClarifiedOutcomeStatement:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(statement.tenant_id, statement.request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("request revision is stale")
            if request.state is not RequestState.INVESTIGATING:
                raise ValueError("clarified outcome requires an investigating request")
            if statement.request_revision != request.revision:
                raise StaleRevisionError("clarified outcome revision is stale")

            if statement.statement_id != "pending":
                payload = self._load_payload(
                    table="clarified_outcomes",
                    identity_column="statement_id",
                    identity=statement.statement_id,
                    tenant_id=statement.tenant_id,
                )
                if payload is None:
                    raise ValueError("clarified outcome identity was not allocated here")
                existing = _validate_persisted_artifact(ClarifiedOutcomeStatement, payload)
                if canonical_bytes(existing) != canonical_bytes(statement):
                    raise ValueError("clarified outcome replay does not match stored artifact")
                return existing

            sequence = self._requests.allocate_artifact_sequence_in_transaction(
                statement.tenant_id, "clarified_outcome"
            )
            stored = statement.model_copy(
                update={
                    "statement_id": self._artifact_id(
                        "clarified_outcome", statement.tenant_id, sequence
                    )
                }
            )
            self._connection.execute(
                "INSERT INTO clarified_outcomes "
                "(statement_id, tenant_id, request_id, request_revision, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stored.statement_id,
                    stored.tenant_id,
                    stored.request_id,
                    stored.request_revision,
                    stored.created_at.isoformat(),
                    canonical_bytes(stored),
                ),
            )
            return stored

    def store_proposal(
        self,
        *,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        proposal: FulfillmentProposal,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentProposal:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(proposal.tenant_id, proposal.request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("request revision is stale")
            if request.state is not RequestState.INVESTIGATING:
                raise ValueError("proposal requires an investigating request")
            if proposal.request_revision != request.revision + 1:
                raise StaleRevisionError("proposal request revision is stale")
            if grounding.tenant_id != request.tenant_id or policy.tenant_id != request.tenant_id:
                raise ValueError("proposal snapshots must belong to the request tenant")
            statements = self.list_clarified_outcomes(request.tenant_id, request.request_id)
            if not any(
                digest(statement) == proposal.clarified_outcome_digest for statement in statements
            ):
                raise ValueError("proposal does not bind a stored clarified outcome")

            stored_grounding = self._allocate_grounding_snapshot(grounding)
            stored_policy = self._allocate_policy_snapshot(policy)
            proposal_sequence = self._requests.allocate_artifact_sequence_in_transaction(
                proposal.tenant_id, "fulfillment_proposal"
            )
            stored_proposal = FulfillmentProposal.model_validate(
                {
                    **proposal.model_dump(mode="python"),
                    "proposal_id": self._artifact_id(
                        "fulfillment_proposal", proposal.tenant_id, proposal_sequence
                    ),
                    "grounding_snapshot_digest": digest(stored_grounding),
                    "policy_snapshot_digest": digest(stored_policy),
                }
            )
            transitioned = self._requests.transition_in_transaction(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.PROPOSED,
                created_at=proposal.created_at,
            )
            if transitioned.revision != stored_proposal.request_revision:
                raise StaleRevisionError("proposal request revision is stale")

            self._insert_snapshot(
                table="fulfillment_grounding_snapshots",
                identity_column="snapshot_id",
                identity=stored_grounding.snapshot_id,
                tenant_id=stored_grounding.tenant_id,
                artifact_digest=digest(stored_grounding),
                created_at=stored_grounding.created_at.isoformat(),
                payload=canonical_bytes(stored_grounding),
            )
            self._insert_snapshot(
                table="fulfillment_policy_snapshots",
                identity_column="snapshot_id",
                identity=stored_policy.snapshot_id,
                tenant_id=stored_policy.tenant_id,
                artifact_digest=digest(stored_policy),
                created_at=stored_policy.observed_at.isoformat(),
                payload=canonical_bytes(stored_policy),
            )
            self._connection.execute(
                "INSERT INTO fulfillment_proposals "
                "(proposal_id, tenant_id, request_id, request_revision, revision, "
                "proposal_digest, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_proposal.proposal_id,
                    stored_proposal.tenant_id,
                    stored_proposal.request_id,
                    stored_proposal.request_revision,
                    stored_proposal.revision,
                    digest(stored_proposal),
                    stored_proposal.created_at.isoformat(),
                    canonical_bytes(stored_proposal),
                ),
            )
            return stored_proposal

    def submit_proposal(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
        created_at: datetime,
    ) -> InboxRequest:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(tenant_id, request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("request revision is stale")
            if request.state is not RequestState.PROPOSED:
                raise ValueError("only a proposed request can await approval")
            proposal = self._load_latest_proposal(tenant_id, request_id)
            if proposal.request_revision != request.revision:
                raise StaleRevisionError("proposal request revision is stale")
            return self._requests.transition_in_transaction(
                tenant_id=tenant_id,
                request_id=request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.AWAITING_APPROVAL,
                created_at=created_at,
            )

    def store_approval(
        self,
        binding: FulfillmentApprovalBinding,
        *,
        transition_actor_id: str | None = None,
    ) -> FulfillmentApprovalBinding:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(binding.tenant_id, binding.request_id)
            if request.state is not RequestState.AWAITING_APPROVAL:
                raise ValueError("approval requires a request awaiting approval")
            if request.revision != binding.request_revision:
                raise StaleRevisionError("approval request revision is stale")
            proposal = self._load_latest_proposal(binding.tenant_id, binding.request_id)
            self._validate_approval_binding(binding, proposal)

            if binding.approval_id != "pending":
                payload = self._load_payload(
                    table="fulfillment_approvals",
                    identity_column="approval_id",
                    identity=binding.approval_id,
                    tenant_id=binding.tenant_id,
                )
                if payload is None:
                    raise ValueError("approval identity was not allocated here")
                existing = _validate_persisted_artifact(FulfillmentApprovalBinding, payload)
                if canonical_bytes(existing) != canonical_bytes(binding):
                    raise ValueError("approval replay does not match stored artifact")
                return existing

            sequence = self._requests.allocate_artifact_sequence_in_transaction(
                binding.tenant_id, "fulfillment_approval"
            )
            stored = binding.model_copy(
                update={
                    "approval_id": self._artifact_id(
                        "fulfillment_approval", binding.tenant_id, sequence
                    )
                }
            )
            self._connection.execute(
                "INSERT INTO fulfillment_approvals "
                "(approval_id, tenant_id, request_id, request_revision, proposal_id, "
                "proposal_revision, authority_ref, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.approval_id,
                    stored.tenant_id,
                    stored.request_id,
                    stored.request_revision,
                    stored.proposal_id,
                    stored.proposal_revision,
                    stored.authority_ref,
                    stored.created_at.isoformat(),
                    canonical_bytes(stored),
                ),
            )
            if binding.decision in ("reject", "request_changes"):
                if transition_actor_id is None:
                    raise ValueError("negative approval decision requires a transition actor")
                target_state = (
                    RequestState.REJECTED
                    if binding.decision == "reject"
                    else RequestState.INVESTIGATING
                )
                self._requests.transition_in_transaction(
                    tenant_id=binding.tenant_id,
                    request_id=binding.request_id,
                    expected_revision=binding.request_revision,
                    actor_id=transition_actor_id,
                    to_state=target_state,
                    created_at=binding.created_at,
                )
            return stored

    def admit(
        self,
        *,
        admission: FulfillmentAdmissionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
        current_policy: FulfillmentPolicySnapshot | None = None,
    ) -> FulfillmentAdmissionReceipt:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(admission.tenant_id, admission.request_id)
            existing = self._load_admission_by_source_revision(
                admission.tenant_id,
                admission.request_id,
                admission.source_request_revision,
            )
            if existing is not None:
                existing_evidence = self._load_evidence_for_outcome(
                    admission.tenant_id,
                    admission.request_id,
                    admission.resulting_request_revision,
                    "execution_ready",
                )
                replay_admission = admission.model_copy(
                    update={
                        "admission_id": existing.admission_id,
                        "policy_snapshot_digest": existing.policy_snapshot_digest,
                    }
                )
                replay_evidence = evidence.model_copy(
                    update={"evidence_id": existing_evidence.evidence_id}
                )
                if canonical_bytes(replay_admission) != canonical_bytes(
                    existing
                ) or canonical_bytes(replay_evidence) != canonical_bytes(existing_evidence):
                    raise ValueError("admission replay does not match stored outcome")
                return existing
            if request.revision != expected_revision:
                raise StaleRevisionError("admission request revision is stale")
            if request.state is not RequestState.AWAITING_APPROVAL:
                raise ValueError("admission requires a request awaiting approval")
            proposal = self._load_latest_proposal(admission.tenant_id, admission.request_id)
            approvals = self.list_approvals(admission.tenant_id, admission.request_id)
            effective_policy_digest = proposal.policy_snapshot_digest
            stored_current_policy = None
            if current_policy is not None:
                if current_policy.tenant_id != admission.tenant_id:
                    raise ValueError("current policy belongs to another tenant")
                stored_current_policy = self._allocate_policy_snapshot(current_policy)
                effective_policy_digest = digest(stored_current_policy)
                admission = admission.model_copy(
                    update={"policy_snapshot_digest": effective_policy_digest}
                )
            self._validate_admission(
                admission=admission,
                evidence=evidence,
                proposal=proposal,
                approvals=approvals,
                expected_revision=expected_revision,
                effective_policy_digest=effective_policy_digest,
            )

            admission_sequence = self._requests.allocate_artifact_sequence_in_transaction(
                admission.tenant_id, "fulfillment_admission"
            )
            stored_admission = admission.model_copy(
                update={
                    "admission_id": self._artifact_id(
                        "fulfillment_admission", admission.tenant_id, admission_sequence
                    )
                }
            )
            stored_evidence = self._allocate_evidence(evidence)
            transitioned = self._requests.transition_in_transaction(
                tenant_id=admission.tenant_id,
                request_id=admission.request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.EXECUTING,
                created_at=admission.admitted_at,
            )
            if transitioned.revision != stored_admission.resulting_request_revision:
                raise StaleRevisionError("admission resulting revision is stale")
            self._connection.execute(
                "INSERT INTO fulfillment_admissions "
                "(admission_id, tenant_id, request_id, source_request_revision, "
                "resulting_request_revision, proposal_id, proposal_revision, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_admission.admission_id,
                    stored_admission.tenant_id,
                    stored_admission.request_id,
                    stored_admission.source_request_revision,
                    stored_admission.resulting_request_revision,
                    stored_admission.proposal_id,
                    stored_admission.proposal_revision,
                    stored_admission.admitted_at.isoformat(),
                    canonical_bytes(stored_admission),
                ),
            )
            if stored_current_policy is not None:
                self._insert_snapshot(
                    table="fulfillment_policy_snapshots",
                    identity_column="snapshot_id",
                    identity=stored_current_policy.snapshot_id,
                    tenant_id=stored_current_policy.tenant_id,
                    artifact_digest=digest(stored_current_policy),
                    created_at=stored_current_policy.observed_at.isoformat(),
                    payload=canonical_bytes(stored_current_policy),
                )
            self._insert_evidence(stored_evidence)
            return stored_admission

    def store_no_valid_plan(
        self,
        *,
        record: RequestNoValidPlan,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> RequestNoValidPlan:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(record.tenant_id, record.request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("no-valid-plan request revision is stale")
            if request.state is not RequestState.INVESTIGATING:
                raise ValueError("No Valid Plan requires an investigating request")
            if (
                record.source_request_revision != expected_revision
                or record.resulting_request_revision != expected_revision + 1
            ):
                raise StaleRevisionError("no-valid-plan revisions are stale")
            if (
                evidence.tenant_id != record.tenant_id
                or evidence.request_id != record.request_id
                or evidence.request_revision != record.resulting_request_revision
                or evidence.outcome != "no_valid_plan"
                or evidence.reason_codes != record.reason_codes
                or evidence.resulting_state is not RequestState.NO_VALID_PLAN
            ):
                raise ValueError("No Valid Plan evidence does not match the outcome")

            record_sequence = self._requests.allocate_artifact_sequence_in_transaction(
                record.tenant_id, "request_no_valid_plan"
            )
            stored_record = record.model_copy(
                update={
                    "record_id": self._artifact_id(
                        "request_no_valid_plan", record.tenant_id, record_sequence
                    )
                }
            )
            stored_evidence = self._allocate_evidence(evidence)
            transitioned = self._requests.transition_in_transaction(
                tenant_id=record.tenant_id,
                request_id=record.request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.NO_VALID_PLAN,
                created_at=record.created_at,
            )
            if transitioned.revision != stored_record.resulting_request_revision:
                raise StaleRevisionError("no-valid-plan resulting revision is stale")
            self._connection.execute(
                "INSERT INTO request_no_valid_plans "
                "(record_id, tenant_id, request_id, source_request_revision, "
                "resulting_request_revision, created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_record.record_id,
                    stored_record.tenant_id,
                    stored_record.request_id,
                    stored_record.source_request_revision,
                    stored_record.resulting_request_revision,
                    stored_record.created_at.isoformat(),
                    canonical_bytes(stored_record),
                ),
            )
            self._insert_evidence(stored_evidence)
            return stored_record

    def record_cancellation(
        self,
        *,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentEvidenceReceipt:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(evidence.tenant_id, evidence.request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("cancellation request revision is stale")
            if request.state in {
                RequestState.REJECTED,
                RequestState.NO_VALID_PLAN,
                RequestState.CANCELLED,
                RequestState.FAILED,
                RequestState.RETIRED,
            }:
                raise ValueError("terminal request cannot be cancelled")
            if (
                evidence.request_revision != expected_revision + 1
                or evidence.outcome != "cancelled"
                or evidence.proposal_id is not None
                or evidence.proposal_revision is not None
                or evidence.dependency_id is not None
                or evidence.resulting_state is not RequestState.CANCELLED
            ):
                raise ValueError("cancellation evidence does not match the outcome")

            stored_evidence = self._allocate_evidence(evidence)
            transitioned = self._requests.transition_in_transaction(
                tenant_id=evidence.tenant_id,
                request_id=evidence.request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.CANCELLED,
                created_at=evidence.created_at,
            )
            if transitioned.revision != evidence.request_revision:
                raise StaleRevisionError("cancellation resulting revision is stale")
            self._insert_evidence(stored_evidence)
            return stored_evidence

    def store_dependency(
        self,
        *,
        child: InboxRequest,
        dependency: RequestDependency,
        evidence: FulfillmentEvidenceReceipt,
        expected_revision: int,
    ) -> RequestDependency:
        with _transaction(self._connection):
            parent = self._requests.load_owned_request(
                dependency.tenant_id, dependency.parent_request_id
            )
            if parent.revision != expected_revision:
                raise StaleRevisionError("dependency parent revision is stale")
            if parent.state is not RequestState.INVESTIGATING:
                raise ValueError("dependency requires an investigating parent")
            if (
                child.tenant_id != parent.tenant_id
                or child.request_id == parent.request_id
                or child.revision != 1
                or child.state is not RequestState.SUBMITTED
                or dependency.parent_request_revision != parent.revision
                or dependency.child_request_id != child.request_id
                or dependency.child_request_revision != child.revision
            ):
                raise ValueError("dependency child does not match the current parent")
            self._validate_dependency_payload(child, dependency, parent)
            if (
                evidence.tenant_id != parent.tenant_id
                or evidence.request_id != parent.request_id
                or evidence.request_revision != parent.revision
                or evidence.outcome != "dependency"
                or evidence.dependency_id != "pending"
                or evidence.reason_codes != (dependency.reason_code,)
                or evidence.resulting_state is not RequestState.INVESTIGATING
            ):
                raise ValueError("dependency evidence does not match the outcome")

            prepared_sequence = int(child.request_id.split("-", 2)[1])
            if self._requests.peek_next_sequence(child.tenant_id) != prepared_sequence:
                raise StaleRevisionError("prepared dependency request sequence is stale")
            self._connection.execute(
                "INSERT INTO request_sequences (tenant_id, next_sequence) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET next_sequence = excluded.next_sequence",
                (child.tenant_id, prepared_sequence + 1),
            )
            self._requests.save_request_revision_in_transaction(child)

            dependency_sequence = self._requests.allocate_artifact_sequence_in_transaction(
                dependency.tenant_id, "request_dependency"
            )
            stored_dependency = dependency.model_copy(
                update={
                    "dependency_id": self._artifact_id(
                        "request_dependency", dependency.tenant_id, dependency_sequence
                    )
                }
            )
            stored_evidence = self._allocate_evidence(
                evidence.model_copy(update={"dependency_id": stored_dependency.dependency_id})
            )
            self._connection.execute(
                "INSERT INTO request_dependencies "
                "(dependency_id, tenant_id, parent_request_id, parent_request_revision, "
                "child_request_id, child_request_revision, kind, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_dependency.dependency_id,
                    stored_dependency.tenant_id,
                    stored_dependency.parent_request_id,
                    stored_dependency.parent_request_revision,
                    stored_dependency.child_request_id,
                    stored_dependency.child_request_revision,
                    stored_dependency.kind,
                    stored_dependency.created_at.isoformat(),
                    canonical_bytes(stored_dependency),
                ),
            )
            self._insert_evidence(stored_evidence)
            return stored_dependency

    def record_denial(
        self,
        *,
        disposition: DenialDispositionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        actor_id: str,
        expected_revision: int,
    ) -> DenialDispositionReceipt:
        with _transaction(self._connection):
            request = self._requests.load_owned_request(
                disposition.tenant_id, disposition.request_id
            )
            if request.revision != expected_revision:
                raise StaleRevisionError("denial request revision is stale")
            if request.state is not RequestState.AWAITING_APPROVAL:
                raise ValueError("denial requires a request awaiting approval")
            proposal = self._load_latest_proposal(disposition.tenant_id, disposition.request_id)
            if not isinstance(proposal.subject, DisclosureDenial):
                raise ValueError("denial disposition requires a denial proposal")
            approvals = self.list_approvals(disposition.tenant_id, disposition.request_id)
            self._validate_denial(
                disposition=disposition,
                evidence=evidence,
                proposal=proposal,
                approvals=approvals,
                expected_revision=expected_revision,
            )

            sequence = self._requests.allocate_artifact_sequence_in_transaction(
                disposition.tenant_id, "denial_disposition"
            )
            stored_disposition = disposition.model_copy(
                update={
                    "disposition_id": self._artifact_id(
                        "denial_disposition", disposition.tenant_id, sequence
                    )
                }
            )
            stored_evidence = self._allocate_evidence(evidence)
            transitioned = self._requests.transition_in_transaction(
                tenant_id=disposition.tenant_id,
                request_id=disposition.request_id,
                expected_revision=expected_revision,
                actor_id=actor_id,
                to_state=RequestState.REJECTED,
                created_at=disposition.recorded_at,
            )
            if transitioned.revision != disposition.resulting_request_revision:
                raise StaleRevisionError("denial resulting revision is stale")
            self._connection.execute(
                "INSERT INTO denial_dispositions "
                "(disposition_id, tenant_id, request_id, source_request_revision, "
                "resulting_request_revision, proposal_id, proposal_revision, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_disposition.disposition_id,
                    stored_disposition.tenant_id,
                    stored_disposition.request_id,
                    stored_disposition.source_request_revision,
                    stored_disposition.resulting_request_revision,
                    stored_disposition.proposal_id,
                    stored_disposition.proposal_revision,
                    stored_disposition.recorded_at.isoformat(),
                    canonical_bytes(stored_disposition),
                ),
            )
            self._insert_evidence(stored_evidence)
            return stored_disposition

    def list_clarified_outcomes(
        self, tenant_id: str, request_id: str
    ) -> tuple[ClarifiedOutcomeStatement, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM clarified_outcomes "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY request_revision, statement_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(
            _validate_persisted_artifact(ClarifiedOutcomeStatement, row[0]) for row in rows
        )

    def list_proposals(self, tenant_id: str, request_id: str) -> tuple[FulfillmentProposal, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM fulfillment_proposals "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY revision, proposal_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(_validate_persisted_artifact(FulfillmentProposal, row[0]) for row in rows)

    def list_approvals(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentApprovalBinding, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM fulfillment_approvals "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY created_at, approval_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(
            _validate_persisted_artifact(FulfillmentApprovalBinding, row[0]) for row in rows
        )

    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentAdmissionReceipt, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM fulfillment_admissions "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY recorded_at, admission_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(
            _validate_persisted_artifact(FulfillmentAdmissionReceipt, row[0]) for row in rows
        )

    def list_evidence(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentEvidenceReceipt, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM fulfillment_evidence_receipts "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY created_at, evidence_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(
            _validate_persisted_artifact(FulfillmentEvidenceReceipt, row[0]) for row in rows
        )

    def list_no_valid_plans(
        self, tenant_id: str, request_id: str
    ) -> tuple[RequestNoValidPlan, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM request_no_valid_plans "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY created_at, record_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(_validate_persisted_artifact(RequestNoValidPlan, row[0]) for row in rows)

    def list_dependencies(
        self, tenant_id: str, parent_request_id: str
    ) -> tuple[RequestDependency, ...]:
        self._requests.load_owned_request(tenant_id, parent_request_id)
        rows = self._connection.execute(
            "SELECT payload FROM request_dependencies "
            "WHERE tenant_id = ? AND parent_request_id = ? ORDER BY created_at, dependency_id",
            (tenant_id, parent_request_id),
        ).fetchall()
        return tuple(_validate_persisted_artifact(RequestDependency, row[0]) for row in rows)

    def list_denials(self, tenant_id: str, request_id: str) -> tuple[DenialDispositionReceipt, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM denial_dispositions "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY recorded_at, disposition_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(_validate_persisted_artifact(DenialDispositionReceipt, row[0]) for row in rows)

    def load_grounding_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentGroundingSnapshot:
        payload = self._load_payload(
            table="fulfillment_grounding_snapshots",
            identity_column="artifact_digest",
            identity=snapshot_digest,
            tenant_id=tenant_id,
        )
        if payload is None:
            raise KeyError("grounding snapshot is unavailable to the tenant")
        return _validate_persisted_artifact(FulfillmentGroundingSnapshot, payload)

    def load_policy_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentPolicySnapshot:
        payload = self._load_payload(
            table="fulfillment_policy_snapshots",
            identity_column="artifact_digest",
            identity=snapshot_digest,
            tenant_id=tenant_id,
        )
        if payload is None:
            raise KeyError("policy snapshot is unavailable to the tenant")
        return _validate_persisted_artifact(FulfillmentPolicySnapshot, payload)

    def _initialize_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS clarified_outcomes ("
            "statement_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_grounding_snapshots ("
            "snapshot_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "artifact_digest TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, artifact_digest)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_policy_snapshots ("
            "snapshot_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "artifact_digest TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, artifact_digest)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_proposals ("
            "proposal_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, "
            "proposal_digest TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, proposal_id, revision), "
            "UNIQUE (tenant_id, request_id, request_revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_approvals ("
            "approval_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "proposal_id TEXT NOT NULL, "
            "proposal_revision INTEGER NOT NULL, "
            "authority_ref TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, proposal_id, proposal_revision, authority_ref)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_admissions ("
            "admission_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "source_request_revision INTEGER NOT NULL, "
            "resulting_request_revision INTEGER NOT NULL, "
            "proposal_id TEXT NOT NULL, "
            "proposal_revision INTEGER NOT NULL, "
            "recorded_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, request_id, source_request_revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_evidence_receipts ("
            "evidence_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "outcome TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS request_no_valid_plans ("
            "record_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "source_request_revision INTEGER NOT NULL, "
            "resulting_request_revision INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, request_id, source_request_revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS request_dependencies ("
            "dependency_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "parent_request_id TEXT NOT NULL, "
            "parent_request_revision INTEGER NOT NULL, "
            "child_request_id TEXT NOT NULL, "
            "child_request_revision INTEGER NOT NULL, "
            "kind TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, parent_request_id, child_request_id)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS denial_dispositions ("
            "disposition_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "source_request_revision INTEGER NOT NULL, "
            "resulting_request_revision INTEGER NOT NULL, "
            "proposal_id TEXT NOT NULL, "
            "proposal_revision INTEGER NOT NULL, "
            "recorded_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, request_id, source_request_revision)"
            ")"
        )
        self._connection.commit()

    def _load_payload(
        self,
        *,
        table: str,
        identity_column: str,
        identity: str,
        tenant_id: str,
    ) -> bytes | None:
        allowed_lookups = {
            ("clarified_outcomes", "statement_id"),
            ("fulfillment_grounding_snapshots", "artifact_digest"),
            ("fulfillment_policy_snapshots", "artifact_digest"),
            ("fulfillment_approvals", "approval_id"),
        }
        if (table, identity_column) not in allowed_lookups:
            raise ValueError("unsupported artifact table")
        row = self._connection.execute(
            f"SELECT payload FROM {table} WHERE {identity_column} = ? AND tenant_id = ?",
            (identity, tenant_id),
        ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def _allocate_grounding_snapshot(
        self, snapshot: FulfillmentGroundingSnapshot
    ) -> FulfillmentGroundingSnapshot:
        sequence = self._requests.allocate_artifact_sequence_in_transaction(
            snapshot.tenant_id, "grounding_snapshot"
        )
        return snapshot.model_copy(
            update={
                "snapshot_id": self._artifact_id("grounding_snapshot", snapshot.tenant_id, sequence)
            }
        )

    def _allocate_policy_snapshot(
        self, snapshot: FulfillmentPolicySnapshot
    ) -> FulfillmentPolicySnapshot:
        sequence = self._requests.allocate_artifact_sequence_in_transaction(
            snapshot.tenant_id, "policy_snapshot"
        )
        return snapshot.model_copy(
            update={
                "snapshot_id": self._artifact_id("policy_snapshot", snapshot.tenant_id, sequence)
            }
        )

    def _insert_snapshot(
        self,
        *,
        table: str,
        identity_column: str,
        identity: str,
        tenant_id: str,
        artifact_digest: str,
        created_at: str,
        payload: bytes,
    ) -> None:
        allowed_tables = {
            ("fulfillment_grounding_snapshots", "snapshot_id"),
            ("fulfillment_policy_snapshots", "snapshot_id"),
        }
        if (table, identity_column) not in allowed_tables:
            raise ValueError("unsupported snapshot table")
        self._connection.execute(
            f"INSERT INTO {table} "
            f"({identity_column}, tenant_id, artifact_digest, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (identity, tenant_id, artifact_digest, created_at, payload),
        )

    def _load_latest_proposal(self, tenant_id: str, request_id: str) -> FulfillmentProposal:
        row = self._connection.execute(
            "SELECT payload FROM fulfillment_proposals "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, request_id),
        ).fetchone()
        if row is None:
            raise ValueError("request has no fulfillment proposal")
        return _validate_persisted_artifact(FulfillmentProposal, row[0])

    def _load_admission_by_source_revision(
        self, tenant_id: str, request_id: str, source_request_revision: int
    ) -> FulfillmentAdmissionReceipt | None:
        row = self._connection.execute(
            "SELECT payload FROM fulfillment_admissions "
            "WHERE tenant_id = ? AND request_id = ? AND source_request_revision = ?",
            (tenant_id, request_id, source_request_revision),
        ).fetchone()
        if row is None:
            return None
        return _validate_persisted_artifact(FulfillmentAdmissionReceipt, row[0])

    def _load_evidence_for_outcome(
        self,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        outcome: str,
    ) -> FulfillmentEvidenceReceipt:
        rows = self._connection.execute(
            "SELECT payload FROM fulfillment_evidence_receipts "
            "WHERE tenant_id = ? AND request_id = ? AND request_revision = ? AND outcome = ?",
            (tenant_id, request_id, request_revision, outcome),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("stored outcome evidence is incomplete")
        return _validate_persisted_artifact(FulfillmentEvidenceReceipt, rows[0][0])

    @staticmethod
    def _validate_approval_binding(
        binding: FulfillmentApprovalBinding, proposal: FulfillmentProposal
    ) -> None:
        if (
            binding.proposal_id != proposal.proposal_id
            or binding.proposal_revision != proposal.revision
            or binding.proposal_digest != digest(proposal)
        ):
            raise ValueError("approval does not bind the latest proposal")
        if not any(
            requirement.authority_ref == binding.authority_ref
            and requirement.subject_digest == binding.subject_digest
            for requirement in proposal.required_approvals
        ):
            raise ValueError("approval does not bind an exact requirement")

    @staticmethod
    def _validate_admission(
        *,
        admission: FulfillmentAdmissionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        proposal: FulfillmentProposal,
        approvals: tuple[FulfillmentApprovalBinding, ...],
        expected_revision: int,
        effective_policy_digest: str,
    ) -> None:
        if (
            admission.source_request_revision != expected_revision
            or admission.resulting_request_revision != expected_revision + 1
            or admission.proposal_id != proposal.proposal_id
            or admission.proposal_revision != proposal.revision
            or admission.proposal_digest != digest(proposal)
            or admission.grounding_snapshot_digest != proposal.grounding_snapshot_digest
            or admission.policy_snapshot_digest != effective_policy_digest
        ):
            raise ValueError("admission does not bind the current proposal")
        selected = tuple(
            approval for approval in approvals if approval.approval_id in admission.approval_ids
        )
        if len(selected) != len(admission.approval_ids):
            raise ValueError("admission names an unknown or duplicate approval")
        for requirement in proposal.required_approvals:
            matches = tuple(
                approval
                for approval in selected
                if approval.authority_ref == requirement.authority_ref
                and approval.subject_digest == requirement.subject_digest
                and approval.proposal_digest == admission.proposal_digest
                and approval.proposal_revision == admission.proposal_revision
                and approval.decision == "approve"
            )
            if len(matches) != 1:
                raise ValueError("admission does not have one exact approval per requirement")
        if any(approval.decision != "approve" for approval in selected):
            raise ValueError("admission contains a negative decision")
        if (
            evidence.tenant_id != admission.tenant_id
            or evidence.request_id != admission.request_id
            or evidence.request_revision != admission.resulting_request_revision
            or evidence.outcome != "execution_ready"
            or evidence.proposal_id != admission.proposal_id
            or evidence.proposal_revision != admission.proposal_revision
            or evidence.approval_ids != admission.approval_ids
            or evidence.resulting_state is not RequestState.EXECUTING
        ):
            raise ValueError("admission evidence does not match the outcome")

    @staticmethod
    def _validate_denial(
        *,
        disposition: DenialDispositionReceipt,
        evidence: FulfillmentEvidenceReceipt,
        proposal: FulfillmentProposal,
        approvals: tuple[FulfillmentApprovalBinding, ...],
        expected_revision: int,
    ) -> None:
        subject = proposal.subject
        if not isinstance(subject, DisclosureDenial):
            raise ValueError("denial disposition requires a denial proposal")
        if (
            disposition.source_request_revision != expected_revision
            or disposition.resulting_request_revision != expected_revision + 1
            or disposition.proposal_id != proposal.proposal_id
            or disposition.proposal_revision != proposal.revision
            or disposition.proposal_digest != digest(proposal)
            or disposition.policy_snapshot_digest != proposal.policy_snapshot_digest
            or disposition.requester_safe_explanation != subject.requester_safe_explanation
        ):
            raise ValueError("denial does not bind the current proposal")
        selected = tuple(
            approval for approval in approvals if approval.approval_id in disposition.approval_ids
        )
        if len(selected) != len(disposition.approval_ids):
            raise ValueError("denial names an unknown or duplicate approval")
        for requirement in proposal.required_approvals:
            matches = tuple(
                approval
                for approval in selected
                if approval.authority_ref == requirement.authority_ref
                and approval.subject_digest == requirement.subject_digest
                and approval.proposal_digest == disposition.proposal_digest
                and approval.proposal_revision == disposition.proposal_revision
                and approval.decision == "approve"
            )
            if len(matches) != 1:
                raise ValueError("denial does not have one exact approval per requirement")
        if (
            evidence.tenant_id != disposition.tenant_id
            or evidence.request_id != disposition.request_id
            or evidence.request_revision != disposition.resulting_request_revision
            or evidence.outcome != "denial"
            or evidence.proposal_id != disposition.proposal_id
            or evidence.proposal_revision != disposition.proposal_revision
            or evidence.approval_ids != disposition.approval_ids
            or evidence.resulting_state is not RequestState.REJECTED
        ):
            raise ValueError("denial evidence does not match the outcome")

    def _allocate_evidence(
        self, evidence: FulfillmentEvidenceReceipt
    ) -> FulfillmentEvidenceReceipt:
        sequence = self._requests.allocate_artifact_sequence_in_transaction(
            evidence.tenant_id, "fulfillment_evidence"
        )
        return evidence.model_copy(
            update={
                "evidence_id": self._artifact_id(
                    "fulfillment_evidence", evidence.tenant_id, sequence
                )
            }
        )

    @staticmethod
    def _validate_dependency_payload(
        child: InboxRequest, dependency: RequestDependency, parent: InboxRequest
    ) -> None:
        payload = child.payload
        if dependency.kind == "data_product_change":
            if not isinstance(payload, DataProductChangeRequest):
                raise ValueError("data-product dependency requires its typed child payload")
            source_request_id = payload.source_request_id
            source_request_revision = payload.source_request_revision
        else:
            if not isinstance(payload, SchemaSemanticChangeRequest):
                raise ValueError("semantic dependency requires its typed child payload")
            source_request_id = parent.request_id
            source_request_revision = parent.revision
        if source_request_id != parent.request_id or source_request_revision != parent.revision:
            raise ValueError("dependency payload does not bind the parent revision")

    def _insert_evidence(self, evidence: FulfillmentEvidenceReceipt) -> None:
        self._connection.execute(
            "INSERT INTO fulfillment_evidence_receipts "
            "(evidence_id, tenant_id, request_id, request_revision, outcome, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.request_id,
                evidence.request_revision,
                evidence.outcome,
                evidence.created_at.isoformat(),
                canonical_bytes(evidence),
            ),
        )

    @staticmethod
    def _artifact_id(artifact_kind: str, tenant_id: str, sequence: int) -> str:
        prefix = {
            "clarified_outcome": "out",
            "denial_disposition": "dny",
            "fulfillment_admission": "adm",
            "fulfillment_approval": "apr",
            "fulfillment_evidence": "evd",
            "fulfillment_proposal": "prp",
            "grounding_snapshot": "grd",
            "policy_snapshot": "pol",
            "request_dependency": "dep",
            "request_no_valid_plan": "nvp",
        }[artifact_kind]
        identity_digest = digest(
            {
                "domain": f"pillarmesh-{artifact_kind}-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
        return f"{prefix}-{sequence:020d}-{identity_digest}"
