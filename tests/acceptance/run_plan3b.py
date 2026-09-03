from __future__ import annotations

import hashlib
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

from pillarmesh_contract_model import ArtifactModel, digest
from pillarmesh_evidence import package_fulfillment_receipts
from pillarmesh_request_management import (
    AccessScopePreview,
    DataAccessRequest,
    DecisionKind,
    DenialDispositionReceipt,
    FreshnessDisposition,
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentNotVisible,
    FulfillmentPolicyCompiler,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    InboxRequest,
    RequestDependency,
    RequestManagementService,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)
from pillarmesh_semantic_registry import (
    FulfillmentAuthorityObservation,
    SemanticFulfillmentSnapshotAdapter,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)

_SEMANTIC_SUPPORT = runpy.run_path(
    str(Path(__file__).parents[2] / "services/semantic-registry/tests/test_fulfillment_adapter.py")
)
published_repository = _SEMANTIC_SUPPORT["published_repository"]
base_authority = _SEMANTIC_SUPPORT["authority"]


class Plan3BAcceptanceResult(ArtifactModel):
    schema_version: Literal["1"] = "1"
    authorized_answer_status: Literal["ready_for_execution"]
    missing_data_dependency: Literal["data_product_change"]
    unauthorized_answer_status: Literal["denied"]
    access_status: Literal["ready_for_execution"]
    clarified_outcome_bound: Literal[True]
    plan2_decision_separate: Literal[True]
    access_scope_narrowed: Literal[True]
    distinct_access_authorities: Literal[True]
    requester_candidate_hidden: Literal[True]
    cross_tenant_denied: Literal[True]
    unrelated_actor_denied: Literal[True]
    external_effects: tuple[str, ...]
    evidence_package_digest: str


class Plan3BCommandResult(ArtifactModel):
    schema_version: Literal["1"] = "1"
    status: Literal["complete"] = "complete"
    evidence_digest: str


class ScenarioAuthorityResolver:
    def __init__(self, publication_id: str, integration_contract: object) -> None:
        self._publication_id = publication_id
        self._integration_contract = integration_contract

    def resolve(self, *, tenant_id: str, request: InboxRequest) -> FulfillmentAuthorityObservation:
        observation: FulfillmentAuthorityObservation = base_authority(
            self._publication_id, self._integration_contract
        )
        purpose = request.payload.purpose
        updates: dict[str, object] = {
            "requester_id": request.requester_id,
            "requester_principal_ref": f"principal:{request.requester_id}",
            "purpose_digest": digest(purpose),
        }
        if purpose in ("semantic definition", "missing governed data", "finance access"):
            updates |= {
                "freshness_observation_ref": None,
                "data_observation_refs": (),
            }
        if purpose == "unauthorized factual answer":
            updates["permitted_data_product_refs"] = ()
        return observation.model_copy(update=updates)


class ScenarioAnswerProvider:
    def propose(
        self, *, request: InboxRequest, grounding: FulfillmentGroundingSnapshot
    ) -> StakeholderAnswerDraft:
        factual = request.payload.purpose != "semantic definition"
        classified = request.payload.purpose == "unauthorized factual answer"
        return StakeholderAnswerDraft(
            answer_text="Net revenue is gross revenue less approved refunds.",
            governed_dataset_refs=grounding.governed_dataset_refs,
            metric_refs=grounding.metric_refs[:1],
            as_of=grounding.as_of,
            freshness_disposition="current" if factual else "not_applicable",
            material_quality_limitations=(),
            lineage_refs=grounding.lineage_refs[:1],
            disclosure_classifications=(grounding.classification_refs[:1] if classified else ()),
        )


class ScenarioAccessProvider:
    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessScopePreview:
        del policy
        payload = request.payload
        if not isinstance(payload, DataAccessRequest):
            raise ValueError("access preview requires a data access request")
        requested_fields = payload.requested_fields
        return AccessScopePreview(
            requester_principal_ref=f"principal:{request.requester_id}",
            data_product_ref=grounding.governed_dataset_refs[0],
            access_mode=payload.access_mode,
            requested_fields=requested_fields,
            effective_object_refs=grounding.governed_dataset_refs,
            effective_fields=(requested_fields[0],),
            excluded_scopes=tuple(requested_fields[1:]),
            classifications=grounding.classification_refs[:1],
            expires_at=NOW + timedelta(days=1),
        )


class ScenarioOwnerResolver:
    def resolve(self, *, tenant_id: str, data_product_ref: object) -> str:
        return "owner:product-revenue"


class ScenarioRoleResolver:
    _roles: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {
            ("requester-a", "principal:requester-a"),
            ("architect-a", "role:data_engineering_architect"),
            ("owner-a", "owner:product-revenue"),
            ("policy-a", "role:policy_authority"),
        }
    )

    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == "tenant-a" and (actor_id, authority_ref) in self._roles


class ScenarioFreshness:
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition:
        return "current" if grounding.freshness_observation_ref is not None else "not_applicable"


def _clarify(
    fulfillment: FulfillmentService,
    requests: RequestManagementService,
    request: InboxRequest,
) -> InboxRequest:
    fulfillment.clarify_outcome(
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        actor_id="architect-a",
        restated_request="Provide the governed requested outcome.",
        in_scope_summary="Approved semantic and product scope.",
        out_of_scope_summary="Raw rows and wider access.",
        expected_revision=request.revision,
    )
    return requests.get(request.tenant_id, request.request_id)


def _approve_all(
    fulfillment: FulfillmentService,
    proposal: FulfillmentProposal,
    awaiting_revision: int,
) -> None:
    actors = {
        "principal:requester-a": "requester-a",
        "role:data_engineering_architect": "architect-a",
        "owner:product-revenue": "owner-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting_revision,
        )


def _submit_for_review(
    fulfillment: FulfillmentService,
    proposal: FulfillmentProposal,
) -> InboxRequest:
    return fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )


def _held(claim: str, observed: bool) -> Literal[True]:
    """Assert an acceptance invariant, and give the checker the `Literal[True]` it is.

    `Plan3BAcceptanceResult` declares these fields `Literal[True]` because the run
    has no result to report unless every one of them held. Raising here names which
    invariant failed, rather than leaving the reader a field-level validation error.
    """
    if not observed:
        raise AssertionError(f"plan 3B acceptance did not hold: {claim}")
    return True


def run_offline_plan3b() -> Plan3BAcceptanceResult:
    publication_repository, receipt, integration_contract = published_repository()
    snapshot_resolver = SemanticFulfillmentSnapshotAdapter(
        publication_repository=publication_repository,
        authority_resolver=ScenarioAuthorityResolver(receipt.publication_id, integration_contract),
        clock=lambda: NOW,
    )
    request_repository = SQLiteRequestRepository.open(":memory:")
    requests = RequestManagementService(request_repository, clock=lambda: NOW)
    repository = SQLiteFulfillmentRepository(request_repository)
    roles = ScenarioRoleResolver()
    fulfillment = FulfillmentService(
        request_service=requests,
        repository=repository,
        snapshot_resolver=snapshot_resolver,
        answer_candidate_provider=ScenarioAnswerProvider(),
        access_candidate_provider=ScenarioAccessProvider(),
        data_product_owner_resolver=ScenarioOwnerResolver(),
        authority_role_resolver=roles,
        policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=ScenarioFreshness()),
        clock=lambda: NOW,
    )

    answer_request = _clarify(
        fulfillment,
        requests,
        requests.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            purpose="semantic definition",
            question="What does net revenue mean?",
        ),
    )
    answer_proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=answer_request.request_id,
        actor_id="architect-a",
        expected_revision=answer_request.revision,
    )
    if not isinstance(answer_proposal, FulfillmentProposal):
        raise AssertionError("authorized answer did not compile to a proposal")
    clarified_outcome = repository.list_clarified_outcomes("tenant-a", answer_request.request_id)[
        -1
    ]
    requester_requirement = next(
        requirement
        for requirement in answer_proposal.required_approvals
        if requirement.authority_ref == "principal:requester-a"
    )
    clarified_outcome_bound = requester_requirement.subject_digest == digest(clarified_outcome)
    try:
        requests.record_decision(
            tenant_id="tenant-a",
            request_id=answer_proposal.request_id,
            request_revision=answer_proposal.request_revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest=digest(answer_proposal.subject),
        )
    except ValueError as error:
        plan2_decision_separate = "fulfillment proposal" in str(error)
    else:
        plan2_decision_separate = False
    answer_awaiting = _submit_for_review(fulfillment, answer_proposal)
    _approve_all(fulfillment, answer_proposal, answer_awaiting.revision)
    answer_admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=answer_request.request_id,
        actor_id="architect-a",
        expected_revision=answer_awaiting.revision,
    )
    if not isinstance(answer_admission, FulfillmentAdmissionReceipt):
        raise AssertionError("authorized answer did not reach execution-ready admission")

    missing_request = _clarify(
        fulfillment,
        requests,
        requests.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            purpose="missing governed data",
            question="What was net revenue yesterday?",
        ),
    )
    dependency = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=missing_request.request_id,
        actor_id="architect-a",
        expected_revision=missing_request.revision,
    )
    if not isinstance(dependency, RequestDependency):
        raise AssertionError("missing factual data did not create a dependency")
    missing_data_dependency = dependency.kind
    if missing_data_dependency != "data_product_change":
        raise AssertionError(
            "missing governed data raised a semantic dependency, not a data product one"
        )

    denied_request = _clarify(
        fulfillment,
        requests,
        requests.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            purpose="unauthorized factual answer",
            question="Disclose restricted net revenue.",
        ),
    )
    denial_proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=denied_request.request_id,
        actor_id="architect-a",
        expected_revision=denied_request.revision,
    )
    if not isinstance(denial_proposal, FulfillmentProposal):
        raise AssertionError("unauthorized answer did not compile to a denial proposal")
    denial_awaiting = _submit_for_review(fulfillment, denial_proposal)
    _approve_all(fulfillment, denial_proposal, denial_awaiting.revision)
    denial = fulfillment.dispose_denial(
        tenant_id="tenant-a",
        request_id=denied_request.request_id,
        actor_id="architect-a",
        expected_revision=denial_awaiting.revision,
    )
    if not isinstance(denial, DenialDispositionReceipt):
        raise AssertionError("denial disposition was not recorded")

    access_request = _clarify(
        fulfillment,
        requests,
        requests.submit_access_request(
            tenant_id="tenant-a",
            requester_id="requester-a",
            purpose="finance access",
            data_product_id="product-revenue",
            requested_fields=("invoice_id", "refund_amount"),
            access_mode="query",
            expires_at=NOW + timedelta(days=2),
        ),
    )
    access_proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=access_request.request_id,
        actor_id="architect-a",
        expected_revision=access_request.revision,
    )
    if not isinstance(access_proposal, FulfillmentProposal):
        raise AssertionError("access request did not compile to a proposal")
    if not isinstance(access_proposal.subject, AccessScopePreview):
        raise AssertionError("access request did not produce an access scope preview")
    access_scope_narrowed = access_proposal.subject.effective_fields == (
        "invoice_id",
    ) and access_proposal.subject.excluded_scopes == ("refund_amount",)
    access_authorities = {
        requirement.authority_ref
        for requirement in access_proposal.required_approvals
        if requirement.authority_ref != "principal:requester-a"
    }
    distinct_access_authorities = access_authorities == {
        "owner:product-revenue",
        "role:policy_authority",
    }
    access_awaiting = _submit_for_review(fulfillment, access_proposal)
    _approve_all(fulfillment, access_proposal, access_awaiting.revision)
    access_admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=access_request.request_id,
        actor_id="architect-a",
        expected_revision=access_awaiting.revision,
    )
    if not isinstance(access_admission, FulfillmentAdmissionReceipt):
        raise AssertionError("access request did not reach execution-ready admission")

    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=roles,
    )
    requester_payload = reader.requester_view(
        tenant_id="tenant-a",
        request_id=answer_request.request_id,
        actor_id="requester-a",
    ).model_dump_json()
    try:
        reader.requester_view(
            tenant_id="tenant-a",
            request_id=answer_request.request_id,
            actor_id="unrelated-a",
        )
    except FulfillmentNotVisible:
        unrelated_denied = True
    else:
        unrelated_denied = False
    try:
        reader.requester_view(
            tenant_id="tenant-b",
            request_id=answer_request.request_id,
            actor_id="requester-a",
        )
    except FulfillmentNotVisible:
        cross_tenant_denied = True
    else:
        cross_tenant_denied = False

    all_evidence = tuple(
        evidence
        for request_id in (
            answer_request.request_id,
            missing_request.request_id,
            denied_request.request_id,
            access_request.request_id,
        )
        for evidence in repository.list_evidence("tenant-a", request_id)
    )
    evidence_payload = package_fulfillment_receipts(all_evidence)
    return Plan3BAcceptanceResult(
        authorized_answer_status=answer_admission.execution_status,
        missing_data_dependency=missing_data_dependency,
        unauthorized_answer_status="denied",
        access_status=access_admission.execution_status,
        clarified_outcome_bound=_held(
            "the clarified outcome bound the approval", clarified_outcome_bound
        ),
        plan2_decision_separate=_held(
            "the plan 2 decision stayed separate", plan2_decision_separate
        ),
        access_scope_narrowed=_held("the access scope was narrowed", access_scope_narrowed),
        distinct_access_authorities=_held(
            "access required distinct authorities", distinct_access_authorities
        ),
        requester_candidate_hidden=_held(
            "the candidate answer stayed hidden from the requester",
            "Net revenue is gross revenue" not in requester_payload,
        ),
        cross_tenant_denied=_held("a cross-tenant read was denied", cross_tenant_denied),
        unrelated_actor_denied=_held("an unrelated actor was denied", unrelated_denied),
        external_effects=(
            "credentials:0",
            "grants:0",
            "messages:0",
            "provider_calls:0",
            "sql:0",
        ),
        evidence_package_digest=hashlib.sha256(evidence_payload).hexdigest(),
    )


def main() -> int:
    result = run_offline_plan3b()
    command_result = Plan3BCommandResult(evidence_digest=result.evidence_package_digest)
    sys.stdout.write(command_result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
