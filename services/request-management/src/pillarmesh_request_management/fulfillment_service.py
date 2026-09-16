from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps

from pillarmesh_contract_model import (
    ArtifactModel,
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactAuthoritySnapshot,
    digest,
)
from pydantic import ValidationError

from .fulfillment_errors import (
    FulfillmentAuthorityError,
    FulfillmentError,
    FulfillmentGroundingError,
    FulfillmentIntegrityError,
    FulfillmentOwnershipError,
    FulfillmentPolicyError,
    FulfillmentStaleRevision,
    ImpactAdmissionResolutionError,
)
from .fulfillment_models import (
    AccessGrantAdmissionBinding,
    AccessGrantDeliveryObservation,
    AccessScopePreview,
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DenialDispositionReceipt,
    DisclosureDenial,
    FulfillmentAccessDeliveryReceipt,
    FulfillmentAdmissionReceipt,
    FulfillmentApprovalBinding,
    FulfillmentDecision,
    FulfillmentDeliveryReceipt,
    FulfillmentEvidenceReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    RequestDependency,
    RequestNoValidPlan,
    StakeholderAnswerDraft,
)
from .fulfillment_policy import (
    DenialCompilation,
    DependencyCompilation,
    FulfillmentPolicyCompiler,
    NoValidPlanCompilation,
    ProposalCompilation,
)
from .fulfillment_protocols import (
    AccessCandidateProvider,
    AccessGrantActivationReader,
    AccessGrantAdmissionResolver,
    AnswerCandidateProvider,
    AnswerExecutionProvider,
    AuthorityRoleResolver,
    DataProductOwnerResolver,
    FulfillmentSnapshotResolver,
    ImpactAdmissionResolver,
    ResolutionFailure,
)
from .fulfillment_repository import FulfillmentRepository
from .models import DataAccessRequest, InboxRequest, RequestState, StakeholderQuestion
from .repository import StaleRevisionError
from .service import RequestManagementService

type FulfillmentOutcomeResult = FulfillmentProposal | RequestDependency | RequestNoValidPlan


def _public_operation[**Parameters, Result](
    operation: Callable[Parameters, Result],
) -> Callable[Parameters, Result]:
    @wraps(operation)
    def guarded(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        try:
            return operation(*args, **kwargs)
        except FulfillmentError:
            raise
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None
        except KeyError:
            raise FulfillmentOwnershipError(
                "fulfillment request is not owned by this tenant"
            ) from None
        except Exception:
            raise FulfillmentIntegrityError(
                "fulfillment operation failed integrity validation"
            ) from None

    return guarded


def _guard_public_operations[Service: type](service_class: Service) -> Service:
    operation_names = (
        "clarify_outcome",
        "propose_answer",
        "submit_proposal",
        "propose_access",
        "revise_proposal",
        "record_approval",
        "admit",
        "execute_access",
        "execute_answer",
        "deliver_answer",
        "cancel",
        "dispose_denial",
    )
    for operation_name in operation_names:
        operation = getattr(service_class, operation_name)
        setattr(service_class, operation_name, _public_operation(operation))
    return service_class


class FulfillmentService:
    def __init__(
        self,
        *,
        request_service: RequestManagementService,
        repository: FulfillmentRepository,
        snapshot_resolver: FulfillmentSnapshotResolver,
        answer_candidate_provider: AnswerCandidateProvider,
        answer_execution_provider: AnswerExecutionProvider | None = None,
        policy_compiler: FulfillmentPolicyCompiler,
        clock: Callable[[], datetime],
        access_candidate_provider: AccessCandidateProvider | None = None,
        data_product_owner_resolver: DataProductOwnerResolver | None = None,
        authority_role_resolver: AuthorityRoleResolver | None = None,
        impact_admission_resolver: ImpactAdmissionResolver | None = None,
        access_grant_admission_resolver: AccessGrantAdmissionResolver | None = None,
        access_grant_activation_reader: AccessGrantActivationReader | None = None,
    ) -> None:
        self._request_service = request_service
        self._repository = repository
        self._snapshot_resolver = snapshot_resolver
        self._answer_candidate_provider = answer_candidate_provider
        self._answer_execution_provider = answer_execution_provider
        self._policy_compiler = policy_compiler
        self._clock = clock
        self._access_candidate_provider = access_candidate_provider
        self._data_product_owner_resolver = data_product_owner_resolver
        self._authority_role_resolver = authority_role_resolver
        self._impact_admission_resolver = impact_admission_resolver
        self._access_grant_admission_resolver = access_grant_admission_resolver
        self._access_grant_activation_reader = access_grant_activation_reader

    def clarify_outcome(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        restated_request: str,
        in_scope_summary: str,
        out_of_scope_summary: str,
        expected_revision: int,
    ) -> ClarifiedOutcomeStatement:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state in (RequestState.SUBMITTED, RequestState.CLARIFYING):
            request = self._transition_request(
                tenant_id,
                request_id,
                RequestState.INVESTIGATING,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        elif request.state is not RequestState.INVESTIGATING:
            raise FulfillmentIntegrityError("clarified outcome requires an investigating request")
        statement = ClarifiedOutcomeStatement(
            statement_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=request.revision,
            restated_request=restated_request,
            purpose_digest=digest(request.payload.purpose),
            in_scope_summary=in_scope_summary,
            out_of_scope_summary=out_of_scope_summary,
            created_at=self._now(),
        )
        try:
            return self._repository.store_clarified_outcome(
                statement,
                expected_revision=request.revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def propose_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentOutcomeResult:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.INVESTIGATING:
            raise FulfillmentIntegrityError("answer proposal requires an investigating request")
        if not isinstance(request.payload, StakeholderQuestion):
            raise FulfillmentGroundingError("answer proposal requires a stakeholder question")
        clarified_outcomes = self._repository.list_clarified_outcomes(tenant_id, request_id)
        if not clarified_outcomes:
            raise FulfillmentGroundingError("answer proposal requires a clarified outcome")
        clarified_outcome = clarified_outcomes[-1]

        resolution = self._resolve_snapshots(tenant_id=tenant_id, request=request)
        if isinstance(resolution, ResolutionFailure):
            return self._store_no_valid_plan(
                request=request,
                compilation=NoValidPlanCompilation(
                    reason_codes=resolution.reason_codes,
                    constraint_refs=resolution.constraint_refs,
                    smallest_changes=resolution.smallest_changes,
                    requester_safe_explanation=resolution.requester_safe_explanation,
                ),
                actor_id=actor_id,
                grounding_snapshot_digest=None,
                policy_snapshot_digest=None,
            )
        grounding, policy = resolution
        candidate = self._propose_answer_candidate(request=request, grounding=grounding)
        proposals = self._repository.list_proposals(tenant_id, request_id)
        prior = proposals[-1] if proposals else None
        compilation = self._policy_compiler.compile_answer(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            candidate=candidate,
            proposal_revision=1 if prior is None else prior.revision + 1,
            prior_proposal_digest=None if prior is None else digest(prior),
            created_at=self._now(),
        )
        if isinstance(compilation, (ProposalCompilation, DenialCompilation)):
            proposal = self._bind_impact(compilation.proposal)
            try:
                return self._repository.store_proposal(
                    grounding=grounding,
                    policy=policy,
                    proposal=proposal,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                )
            except StaleRevisionError:
                raise FulfillmentStaleRevision("fulfillment request revision is stale") from None
        if isinstance(compilation, DependencyCompilation):
            return self._store_dependency(
                request=request,
                compilation=compilation,
            )
        return self._store_no_valid_plan(
            request=request,
            compilation=compilation,
            actor_id=actor_id,
            grounding_snapshot_digest=digest(grounding),
            policy_snapshot_digest=digest(policy),
        )

    def submit_proposal(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> InboxRequest:
        self._load_current(tenant_id, request_id, expected_revision)
        try:
            return self._repository.submit_proposal(
                tenant_id=tenant_id,
                request_id=request_id,
                actor_id=actor_id,
                expected_revision=expected_revision,
                created_at=self._now(),
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def propose_access(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentOutcomeResult:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.INVESTIGATING:
            raise FulfillmentIntegrityError("access proposal requires an investigating request")
        if not isinstance(request.payload, DataAccessRequest):
            raise FulfillmentGroundingError("access proposal requires a data access request")
        preview_provider = self._access_candidate_provider
        owner_resolver = self._data_product_owner_resolver
        if preview_provider is None or owner_resolver is None:
            raise FulfillmentGroundingError("access proposal boundaries are not configured")
        clarified_outcomes = self._repository.list_clarified_outcomes(tenant_id, request_id)
        if not clarified_outcomes:
            raise FulfillmentGroundingError("access proposal requires a clarified outcome")
        clarified_outcome = clarified_outcomes[-1]
        resolution = self._resolve_snapshots(tenant_id=tenant_id, request=request)
        if isinstance(resolution, ResolutionFailure):
            return self._store_no_valid_plan(
                request=request,
                compilation=NoValidPlanCompilation(
                    reason_codes=resolution.reason_codes,
                    constraint_refs=resolution.constraint_refs,
                    smallest_changes=resolution.smallest_changes,
                    requester_safe_explanation=resolution.requester_safe_explanation,
                ),
                actor_id=actor_id,
                grounding_snapshot_digest=None,
                policy_snapshot_digest=None,
            )
        grounding, policy = resolution
        preview = self._propose_access_candidate(
            provider=preview_provider,
            request=request,
            grounding=grounding,
            policy=policy,
        )
        owner_authority_ref = self._resolve_owner_authority(
            resolver=owner_resolver,
            tenant_id=tenant_id,
            preview=preview,
        )
        proposals = self._repository.list_proposals(tenant_id, request_id)
        prior = proposals[-1] if proposals else None
        compilation = self._policy_compiler.compile_access(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            preview=preview,
            data_product_owner_authority_ref=owner_authority_ref,
            proposal_revision=1 if prior is None else prior.revision + 1,
            prior_proposal_digest=None if prior is None else digest(prior),
            created_at=self._now(),
        )
        if isinstance(compilation, (ProposalCompilation, DenialCompilation)):
            proposal = self._bind_impact(compilation.proposal)
            try:
                return self._repository.store_proposal(
                    grounding=grounding,
                    policy=policy,
                    proposal=proposal,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                )
            except StaleRevisionError:
                raise FulfillmentStaleRevision("fulfillment request revision is stale") from None
        if isinstance(compilation, DependencyCompilation):
            return self._store_dependency(request=request, compilation=compilation)
        return self._store_no_valid_plan(
            request=request,
            compilation=compilation,
            actor_id=actor_id,
            grounding_snapshot_digest=digest(grounding),
            policy_snapshot_digest=digest(policy),
        )

    def revise_proposal(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentProposal:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state not in (RequestState.PROPOSED, RequestState.AWAITING_APPROVAL):
            raise FulfillmentIntegrityError("only an open proposal can be revised")
        investigating = self._transition_request(
            tenant_id,
            request_id,
            RequestState.INVESTIGATING,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        result = self.propose_answer(
            tenant_id=tenant_id,
            request_id=request_id,
            actor_id=actor_id,
            expected_revision=investigating.revision,
        )
        if not isinstance(result, FulfillmentProposal):
            raise FulfillmentGroundingError("revised candidate did not compile to a proposal")
        return result

    def record_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
        subject_digest: str,
        decision: FulfillmentDecision,
        expected_revision: int,
    ) -> FulfillmentApprovalBinding:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.AWAITING_APPROVAL:
            raise FulfillmentIntegrityError("approval requires a request awaiting approval")
        resolver = self._authority_role_resolver
        if resolver is None or not self._has_role(
            resolver=resolver,
            tenant_id=tenant_id,
            actor_id=actor_id,
            authority_ref=authority_ref,
        ):
            raise FulfillmentAuthorityError("actor does not hold the required authority")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        if not proposals:
            raise FulfillmentIntegrityError("approval requires a current proposal")
        proposal = proposals[-1]
        if not any(
            requirement.authority_ref == authority_ref
            and requirement.subject_digest == subject_digest
            for requirement in proposal.required_approvals
        ):
            raise FulfillmentAuthorityError("approval does not match an exact proposal requirement")
        binding = FulfillmentApprovalBinding(
            approval_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=digest(proposal),
            subject_digest=subject_digest,
            actor_id=actor_id,
            authority_ref=authority_ref,
            decision=decision,
            created_at=self._now(),
        )
        try:
            return self._repository.store_approval(
                binding,
                transition_actor_id=actor_id,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def admit(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentAdmissionReceipt | FulfillmentOutcomeResult:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.AWAITING_APPROVAL:
            raise FulfillmentIntegrityError("admission requires a request awaiting approval")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        if not proposals:
            raise FulfillmentIntegrityError("admission requires a current proposal")
        proposal = proposals[-1]
        if self._impact_authority_changed(proposal):
            return self._supersede_proposal(
                request=request,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        policy = self._repository.load_policy_snapshot(tenant_id, proposal.policy_snapshot_digest)
        now = self._now()
        current_policy = None
        if policy.valid_until <= now:
            resolution = self._resolve_snapshots(tenant_id=tenant_id, request=request)
            if isinstance(resolution, ResolutionFailure):
                investigating = self._transition_request(
                    tenant_id,
                    request_id,
                    RequestState.INVESTIGATING,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                )
                return self._store_no_valid_plan(
                    request=investigating,
                    compilation=NoValidPlanCompilation(
                        reason_codes=resolution.reason_codes,
                        constraint_refs=resolution.constraint_refs,
                        smallest_changes=resolution.smallest_changes,
                        requester_safe_explanation=resolution.requester_safe_explanation,
                    ),
                    actor_id=actor_id,
                    grounding_snapshot_digest=None,
                    policy_snapshot_digest=None,
                )
            current_grounding, resolved_policy = resolution
            stored_grounding = self._repository.load_grounding_snapshot(
                tenant_id, proposal.grounding_snapshot_digest
            )
            if not self._equivalent_artifact(
                stored_grounding,
                current_grounding,
                excluded_fields={"snapshot_id", "created_at"},
            ):
                return self._supersede_proposal(
                    request=request,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                )
            if not self._equivalent_artifact(
                policy,
                resolved_policy,
                excluded_fields={"snapshot_id", "observed_at", "valid_until"},
            ):
                return self._supersede_proposal(
                    request=request,
                    actor_id=actor_id,
                    expected_revision=expected_revision,
                )
            if resolved_policy.valid_until <= now:
                raise FulfillmentPolicyError("re-resolved policy is not current")
            current_policy = resolved_policy
        approvals = self._repository.list_approvals(tenant_id, request_id)
        selected: list[FulfillmentApprovalBinding] = []
        resolver = self._authority_role_resolver
        if resolver is None:
            raise FulfillmentAuthorityError("authority role resolver is not configured")
        for requirement in proposal.required_approvals:
            matches = tuple(
                approval
                for approval in approvals
                if approval.request_revision == expected_revision
                and approval.proposal_id == proposal.proposal_id
                and approval.proposal_revision == proposal.revision
                and approval.proposal_digest == digest(proposal)
                and approval.authority_ref == requirement.authority_ref
                and approval.subject_digest == requirement.subject_digest
                and approval.decision == "approve"
                and self._has_role(
                    resolver=resolver,
                    tenant_id=tenant_id,
                    actor_id=approval.actor_id,
                    authority_ref=approval.authority_ref,
                )
            )
            if len(matches) != 1:
                raise FulfillmentAuthorityError(
                    "admission requires one current approval per requirement"
                )
            selected.append(matches[0])
        approval_ids = tuple(item.approval_id for item in selected)
        access_grant_binding = self._bind_access_grant_admission(
            tenant_id=tenant_id,
            request=request,
            proposal=proposal,
            policy=policy if current_policy is None else current_policy,
        )
        admission = FulfillmentAdmissionReceipt(
            admission_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            source_request_revision=expected_revision,
            resulting_request_revision=expected_revision + 1,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=digest(proposal),
            grounding_snapshot_digest=proposal.grounding_snapshot_digest,
            policy_snapshot_digest=(
                proposal.policy_snapshot_digest
                if current_policy is None
                else digest(current_policy)
            ),
            approval_ids=approval_ids,
            access_grant_binding=access_grant_binding,
            admitted_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision + 1,
            outcome="execution_ready",
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            authority_refs=tuple(item.authority_ref for item in proposal.required_approvals),
            approval_ids=approval_ids,
            reason_codes=(),
            resulting_state=RequestState.EXECUTING,
            created_at=now,
        )
        try:
            return self._repository.admit(
                admission=admission,
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=expected_revision,
                current_policy=current_policy,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _bind_access_grant_admission(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessGrantAdmissionBinding | None:
        subject = proposal.subject
        if not isinstance(subject, AccessScopePreview):
            return None
        resolver = self._access_grant_admission_resolver
        if resolver is None:
            raise FulfillmentAuthorityError("access grant admission resolver is not configured")
        binding = AccessGrantAdmissionBinding.model_validate(
            resolver.bind(
                tenant_id=tenant_id,
                request=request,
                proposal=proposal,
                policy=policy,
            ).model_dump(mode="python"),
            strict=True,
        )
        required_permissions = {
            "query": {"query", "view"},
            "dashboard": {"dashboard", "view"},
            "export": {"download", "view"},
        }[subject.access_mode]
        surfaces = {target.surface for target in binding.targets}
        if (
            binding.proposal_digest != digest(proposal)
            or binding.effective_at > self._now()
            or not required_permissions.issubset(binding.permissions)
            or not {"result", "warehouse"}.issubset(surfaces)
            or (subject.access_mode == "dashboard" and "superset" not in surfaces)
        ):
            raise FulfillmentAuthorityError("access grant admission authority does not match")
        return binding

    def execute_access(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentAccessDeliveryReceipt:
        recorded = self._recorded_access_delivery(tenant_id, request_id, expected_revision)
        if recorded is not None:
            return recorded
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.EXECUTING or not isinstance(
            request.payload, DataAccessRequest
        ):
            raise FulfillmentIntegrityError("access execution requires an executing access request")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        admissions = self._repository.list_admissions(tenant_id, request_id)
        if not proposals or not admissions:
            raise FulfillmentIntegrityError("access execution requires an admitted proposal")
        proposal = proposals[-1]
        admission = admissions[-1]
        binding = admission.access_grant_binding
        if not isinstance(proposal.subject, AccessScopePreview) or binding is None:
            raise FulfillmentIntegrityError("access execution requires admitted access authority")
        reader = self._access_grant_activation_reader
        if reader is None:
            raise FulfillmentGroundingError("access activation boundary is not configured")
        try:
            observation = reader.read_active(
                tenant_id=tenant_id,
                request_id=request_id,
                grant_id=binding.grant_id,
            )
        except FulfillmentError:
            raise
        except Exception:
            raise FulfillmentGroundingError(
                "active access authority could not be verified"
            ) from None
        if not isinstance(observation, AccessGrantDeliveryObservation):
            raise FulfillmentGroundingError("active access authority could not be verified")
        expected_authority = (
            binding.grant_id,
            tenant_id,
            request_id,
            digest(proposal),
            binding.entitlement_snapshot_digest,
            binding.policy_revision,
            binding.effective_at,
            binding.permissions,
            binding.targets,
        )
        observed_authority = (
            observation.grant_id,
            observation.tenant_id,
            observation.request_id,
            observation.proposal_digest,
            observation.entitlement_snapshot_digest,
            observation.policy_revision,
            observation.effective_at,
            observation.permissions,
            observation.targets,
        )
        if observed_authority != expected_authority or (
            observation.expires_at != proposal.subject.expires_at
            or len(observation.effect_receipt_refs) != len(binding.targets)
        ):
            raise FulfillmentAuthorityError("active access authority does not match admission")
        now = self._now()
        if not observation.effective_at <= now < observation.expires_at:
            raise FulfillmentAuthorityError("active access authority is not currently effective")
        delivery = FulfillmentAccessDeliveryReceipt(
            delivery_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            source_request_revision=expected_revision,
            resulting_request_revision=expected_revision + 2,
            admission_id=admission.admission_id,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=digest(proposal),
            access_mode=proposal.subject.access_mode,
            fields=proposal.subject.effective_fields,
            effective_at=observation.effective_at,
            expires_at=observation.expires_at,
            permissions=observation.permissions,
            verification_refs=observation.effect_receipt_refs,
            delivered_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=delivery.resulting_request_revision,
            outcome="delivered",
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            authority_refs=tuple(item.authority_ref for item in proposal.required_approvals),
            approval_ids=admission.approval_ids,
            reason_codes=(),
            resulting_state=RequestState.DELIVERED,
            created_at=now,
        )
        try:
            return self._repository.record_access_delivery(
                delivery=delivery,
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _recorded_access_delivery(
        self, tenant_id: str, request_id: str, expected_revision: int
    ) -> FulfillmentAccessDeliveryReceipt | None:
        try:
            request = self._request_service.get(tenant_id, request_id)
        except KeyError:
            raise FulfillmentOwnershipError(
                "fulfillment request is not owned by this tenant"
            ) from None
        if request.state is not RequestState.DELIVERED:
            return None
        return next(
            (
                delivery
                for delivery in self._repository.list_access_deliveries(tenant_id, request_id)
                if delivery.source_request_revision == expected_revision
            ),
            None,
        )

    def execute_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentDeliveryReceipt:
        recorded = self._recorded_delivery(tenant_id, request_id, expected_revision)
        if recorded is not None:
            return recorded
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.EXECUTING:
            raise FulfillmentIntegrityError("answer execution requires an executing request")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        admissions = self._repository.list_admissions(tenant_id, request_id)
        if not proposals or not admissions:
            raise FulfillmentIntegrityError("answer execution requires an admitted proposal")
        proposal = proposals[-1]
        admission = admissions[-1]
        grounding = self._repository.load_grounding_snapshot(
            tenant_id, proposal.grounding_snapshot_digest
        )
        provider = self._answer_execution_provider
        if provider is None:
            raise FulfillmentGroundingError("answer execution boundary is not configured")
        try:
            executed_answer, verification_refs = provider.execute(
                request=request,
                proposal=proposal,
                admission=admission,
                grounding=grounding,
            )
        except FulfillmentError:
            raise
        except Exception:
            raise FulfillmentGroundingError("answer execution could not be verified") from None
        if not isinstance(executed_answer, StakeholderAnswerDraft) or not isinstance(
            verification_refs, tuple
        ):
            raise FulfillmentGroundingError("answer execution could not be verified")
        return self.deliver_answer(
            tenant_id=tenant_id,
            request_id=request_id,
            actor_id=actor_id,
            expected_revision=expected_revision,
            executed_answer=executed_answer,
            verification_refs=verification_refs,
        )

    def _recorded_delivery(
        self, tenant_id: str, request_id: str, expected_revision: int
    ) -> FulfillmentDeliveryReceipt | None:
        """The delivery an earlier attempt from this revision already recorded, if any.

        Delivery advances the request two revisions, so a retry still carrying the revision
        its caller read before delivering would otherwise be refused as stale for a request
        that was delivered.
        """
        try:
            request = self._request_service.get(tenant_id, request_id)
        except KeyError:
            raise FulfillmentOwnershipError(
                "fulfillment request is not owned by this tenant"
            ) from None
        if request.state is not RequestState.DELIVERED:
            return None
        return next(
            (
                delivery
                for delivery in reversed(self._repository.list_deliveries(tenant_id, request_id))
                if delivery.source_request_revision == expected_revision
            ),
            None,
        )

    def deliver_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
        executed_answer: StakeholderAnswerDraft,
        verification_refs: tuple[ArtifactReference, ...],
    ) -> FulfillmentDeliveryReceipt:
        recorded = self._recorded_delivery(tenant_id, request_id, expected_revision)
        if recorded is not None:
            return recorded
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.EXECUTING:
            raise FulfillmentIntegrityError("answer delivery requires an executing request")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        admissions = self._repository.list_admissions(tenant_id, request_id)
        if not proposals or not admissions:
            raise FulfillmentIntegrityError("answer delivery requires an admitted proposal")
        proposal = proposals[-1]
        admission = admissions[-1]
        if not isinstance(proposal.subject, StakeholderAnswerDraft):
            raise FulfillmentGroundingError("answer delivery requires a stakeholder answer")
        if executed_answer != proposal.subject:
            raise FulfillmentIntegrityError("executed answer differs from the approved proposal")
        now = self._now()
        delivery = FulfillmentDeliveryReceipt(
            delivery_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            source_request_revision=expected_revision,
            resulting_request_revision=expected_revision + 2,
            admission_id=admission.admission_id,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=digest(proposal),
            answer=executed_answer,
            verification_refs=verification_refs,
            delivered_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision + 2,
            outcome="delivered",
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            authority_refs=tuple(item.authority_ref for item in proposal.required_approvals),
            approval_ids=admission.approval_ids,
            reason_codes=(),
            resulting_state=RequestState.DELIVERED,
            created_at=now,
        )
        try:
            return self._repository.record_delivery(
                delivery=delivery,
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _supersede_proposal(
        self,
        *,
        request: InboxRequest,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentOutcomeResult:
        investigating = self._transition_request(
            request.tenant_id,
            request.request_id,
            RequestState.INVESTIGATING,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        if isinstance(request.payload, StakeholderQuestion):
            return self.propose_answer(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                actor_id=actor_id,
                expected_revision=investigating.revision,
            )
        if isinstance(request.payload, DataAccessRequest):
            return self.propose_access(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                actor_id=actor_id,
                expected_revision=investigating.revision,
            )
        raise FulfillmentIntegrityError("proposal cannot be superseded for this request type")

    def _bind_impact(self, proposal: FulfillmentProposal) -> FulfillmentProposal:
        resolver = self._impact_admission_resolver
        if resolver is None:
            return proposal
        try:
            binding = resolver.bind(tenant_id=proposal.tenant_id, proposal=proposal)
        except ImpactAdmissionResolutionError:
            raise FulfillmentGroundingError("impact authority could not be resolved") from None
        if binding is None:
            return proposal
        if not isinstance(binding, ImpactAdmissionBinding):
            raise FulfillmentGroundingError("impact authority could not be resolved")
        subject_digest = digest(proposal.subject)
        requirements_by_authority = {
            requirement.authority_ref: requirement for requirement in proposal.required_approvals
        }
        for requirement in binding.authority_snapshot.derived_approval_requirements:
            requirements_by_authority.setdefault(
                requirement.authority_ref,
                ApprovalRequirement(
                    authority_ref=requirement.authority_ref,
                    reason_code=requirement.reason_code,
                    subject_digest=subject_digest,
                ),
            )
        payload = proposal.model_dump(mode="python")
        payload.update(
            {
                "impact_admission_binding": binding,
                "required_approvals": tuple(
                    sorted(
                        requirements_by_authority.values(),
                        key=lambda item: item.authority_ref,
                    )
                ),
            }
        )
        try:
            return FulfillmentProposal.model_validate(payload)
        except ValidationError:
            raise FulfillmentGroundingError("impact authority could not be resolved") from None

    def _impact_authority_changed(self, proposal: FulfillmentProposal) -> bool:
        binding = proposal.impact_admission_binding
        if binding is None:
            return False
        resolver = self._impact_admission_resolver
        if resolver is None:
            raise FulfillmentGroundingError("impact authority is not configured")
        try:
            current = resolver.rederive(
                tenant_id=proposal.tenant_id,
                subject=binding.subject,
                source_record_refs=binding.authority_snapshot.source_record_refs,
            )
        except ImpactAdmissionResolutionError:
            raise FulfillmentGroundingError("impact authority could not be re-derived") from None
        if not isinstance(current, ImpactAuthoritySnapshot):
            raise FulfillmentGroundingError("impact authority could not be re-derived")
        if current.tenant_id != proposal.tenant_id or current.subject != binding.subject:
            raise FulfillmentGroundingError("impact authority could not be re-derived")
        # This digest includes canonical source record versions and derived requirements. The
        # graph digest is intentionally excluded so rebuilding an equivalent projection cannot
        # alter an owning service's admission decision.
        return current.authority_digest != binding.authority_snapshot_digest

    def cancel(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentEvidenceReceipt:
        self._load_current(tenant_id, request_id, expected_revision)
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision + 1,
            outcome="cancelled",
            authority_refs=(),
            approval_ids=(),
            reason_codes=(),
            resulting_state=RequestState.CANCELLED,
            created_at=self._now(),
        )
        try:
            return self._repository.record_cancellation(
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def dispose_denial(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> DenialDispositionReceipt:
        request = self._load_current(tenant_id, request_id, expected_revision)
        if request.state is not RequestState.AWAITING_APPROVAL:
            raise FulfillmentIntegrityError("denial requires a request awaiting approval")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        if not proposals:
            raise FulfillmentIntegrityError("denial disposition requires a current denial proposal")
        proposal = proposals[-1]
        subject = proposal.subject
        if not isinstance(subject, DisclosureDenial):
            raise FulfillmentIntegrityError("denial disposition requires a current denial proposal")
        policy = self._repository.load_policy_snapshot(tenant_id, proposal.policy_snapshot_digest)
        now = self._now()
        if policy.valid_until <= now:
            raise FulfillmentPolicyError("denial policy snapshot expired and must be re-resolved")
        resolver = self._authority_role_resolver
        if resolver is None:
            raise FulfillmentAuthorityError("authority role resolver is not configured")
        approvals = self._repository.list_approvals(tenant_id, request_id)
        selected: list[FulfillmentApprovalBinding] = []
        for requirement in proposal.required_approvals:
            matches = tuple(
                approval
                for approval in approvals
                if approval.request_revision == expected_revision
                and approval.proposal_id == proposal.proposal_id
                and approval.proposal_revision == proposal.revision
                and approval.proposal_digest == digest(proposal)
                and approval.authority_ref == requirement.authority_ref
                and approval.subject_digest == requirement.subject_digest
                and approval.decision == "approve"
                and self._has_role(
                    resolver=resolver,
                    tenant_id=tenant_id,
                    actor_id=approval.actor_id,
                    authority_ref=approval.authority_ref,
                )
            )
            if len(matches) != 1:
                raise FulfillmentAuthorityError(
                    "denial requires one current approval per requirement"
                )
            selected.append(matches[0])
        approval_ids = tuple(item.approval_id for item in selected)
        disposition = DenialDispositionReceipt(
            disposition_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            source_request_revision=expected_revision,
            resulting_request_revision=expected_revision + 1,
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            proposal_digest=digest(proposal),
            policy_snapshot_digest=proposal.policy_snapshot_digest,
            approval_ids=approval_ids,
            requester_safe_explanation=subject.requester_safe_explanation,
            recorded_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision + 1,
            outcome="denial",
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            authority_refs=tuple(item.authority_ref for item in proposal.required_approvals),
            approval_ids=approval_ids,
            reason_codes=(subject.reason_code,),
            resulting_state=RequestState.REJECTED,
            created_at=now,
        )
        try:
            return self._repository.record_denial(
                disposition=disposition,
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _store_dependency(
        self,
        *,
        request: InboxRequest,
        compilation: DependencyCompilation,
    ) -> RequestDependency:
        now = self._now()
        missing_digest = digest(compilation.missing_capability_refs)
        if compilation.dependency_kind == "semantic_change":
            child = self._request_service.prepare_schema_semantic_change(
                tenant_id=request.tenant_id,
                requester_id="pillarmesh-fulfillment-service",
                purpose="Resolve missing approved semantics for a stakeholder request.",
                review_bundle_id="missing-semantics-" + missing_digest[:24],
                review_bundle_digest=missing_digest,
                required_authority_refs=("role:data_engineering_architect",),
                affected_semantic_ref=compilation.missing_capability_refs[0],
            )
        else:
            child = self._request_service.prepare_data_product_change(
                tenant_id=request.tenant_id,
                requester_id="pillarmesh-fulfillment-service",
                purpose="Resolve missing governed data capability for a stakeholder request.",
                requested_outcome="Provide the capabilities named by the parent request.",
                missing_capability_refs=compilation.missing_capability_refs,
                source_request_id=request.request_id,
                source_request_revision=request.revision,
            )
        dependency = RequestDependency(
            dependency_id="pending",
            tenant_id=request.tenant_id,
            parent_request_id=request.request_id,
            parent_request_revision=request.revision,
            child_request_id=child.request_id,
            child_request_revision=child.revision,
            kind=compilation.dependency_kind,
            reason_code=compilation.reason_code,
            created_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            request_revision=request.revision,
            outcome="dependency",
            dependency_id="pending",
            authority_refs=(),
            approval_ids=(),
            reason_codes=(compilation.reason_code,),
            resulting_state=RequestState.INVESTIGATING,
            created_at=now,
        )
        try:
            return self._repository.store_dependency(
                child=child,
                dependency=dependency,
                evidence=evidence,
                expected_revision=request.revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _store_no_valid_plan(
        self,
        *,
        request: InboxRequest,
        compilation: NoValidPlanCompilation,
        actor_id: str,
        grounding_snapshot_digest: str | None,
        policy_snapshot_digest: str | None,
    ) -> RequestNoValidPlan:
        now = self._now()
        record = RequestNoValidPlan(
            record_id="pending",
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            source_request_revision=request.revision,
            resulting_request_revision=request.revision + 1,
            reason_codes=compilation.reason_codes,
            constraint_refs=compilation.constraint_refs,
            smallest_changes=compilation.smallest_changes,
            requester_safe_explanation=compilation.requester_safe_explanation,
            grounding_snapshot_digest=grounding_snapshot_digest,
            policy_snapshot_digest=policy_snapshot_digest,
            created_at=now,
        )
        evidence = FulfillmentEvidenceReceipt(
            evidence_id="pending",
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            request_revision=request.revision + 1,
            outcome="no_valid_plan",
            authority_refs=(),
            approval_ids=(),
            reason_codes=compilation.reason_codes,
            resulting_state=RequestState.NO_VALID_PLAN,
            created_at=now,
        )
        try:
            return self._repository.store_no_valid_plan(
                record=record,
                evidence=evidence,
                actor_id=actor_id,
                expected_revision=request.revision,
            )
        except StaleRevisionError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _transition_request(
        self,
        tenant_id: str,
        request_id: str,
        target_state: RequestState,
        *,
        actor_id: str,
        expected_revision: int,
    ) -> InboxRequest:
        try:
            return self._request_service.transition(
                tenant_id,
                request_id,
                target_state,
                actor_id=actor_id,
                expected_revision=expected_revision,
            )
        except ValueError:
            raise FulfillmentStaleRevision("fulfillment request revision is stale") from None

    def _resolve_snapshots(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot] | ResolutionFailure:
        try:
            resolution = self._snapshot_resolver.resolve(
                tenant_id=tenant_id,
                request=request,
            )
        except Exception:
            raise FulfillmentGroundingError("fulfillment grounding could not be resolved") from None
        if isinstance(resolution, ResolutionFailure):
            return resolution
        if (
            not isinstance(resolution, tuple)
            or len(resolution) != 2
            or not isinstance(resolution[0], FulfillmentGroundingSnapshot)
            or not isinstance(resolution[1], FulfillmentPolicySnapshot)
        ):
            raise FulfillmentGroundingError("fulfillment grounding could not be resolved")
        return resolution

    def _propose_answer_candidate(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
    ) -> StakeholderAnswerDraft:
        try:
            candidate = self._answer_candidate_provider.propose(
                request=request,
                grounding=grounding,
            )
        except Exception:
            raise FulfillmentGroundingError("answer candidate could not be grounded") from None
        if not isinstance(candidate, StakeholderAnswerDraft):
            raise FulfillmentGroundingError("answer candidate could not be grounded")
        return candidate

    @staticmethod
    def _propose_access_candidate(
        *,
        provider: AccessCandidateProvider,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessScopePreview:
        try:
            preview = provider.propose(
                request=request,
                grounding=grounding,
                policy=policy,
            )
        except Exception:
            raise FulfillmentGroundingError("access candidate could not be grounded") from None
        if not isinstance(preview, AccessScopePreview):
            raise FulfillmentGroundingError("access candidate could not be grounded")
        return preview

    @staticmethod
    def _resolve_owner_authority(
        *,
        resolver: DataProductOwnerResolver,
        tenant_id: str,
        preview: AccessScopePreview,
    ) -> str:
        try:
            authority_ref = resolver.resolve(
                tenant_id=tenant_id,
                data_product_ref=preview.data_product_ref,
            )
        except Exception:
            raise FulfillmentAuthorityError(
                "data product owner authority could not be resolved"
            ) from None
        if not isinstance(authority_ref, str):
            raise FulfillmentAuthorityError("data product owner authority could not be resolved")
        return authority_ref

    @staticmethod
    def _has_role(
        *,
        resolver: AuthorityRoleResolver,
        tenant_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> bool:
        try:
            result = resolver.has_role(
                tenant_id=tenant_id,
                actor_id=actor_id,
                authority_ref=authority_ref,
            )
        except Exception:
            raise FulfillmentAuthorityError("fulfillment authority could not be verified") from None
        if not isinstance(result, bool):
            raise FulfillmentAuthorityError("fulfillment authority could not be verified")
        return result

    def _load_current(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
    ) -> InboxRequest:
        try:
            request = self._request_service.get(tenant_id, request_id)
        except KeyError:
            raise FulfillmentOwnershipError(
                "fulfillment request is not owned by this tenant"
            ) from None
        if request.revision != expected_revision:
            raise FulfillmentStaleRevision("fulfillment request revision is stale")
        return request

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise FulfillmentIntegrityError("fulfillment operation failed integrity validation")
        return now.astimezone(UTC)

    @staticmethod
    def _equivalent_artifact(
        first: ArtifactModel,
        second: ArtifactModel,
        *,
        excluded_fields: set[str],
    ) -> bool:
        return first.model_dump(exclude=excluded_fields) == second.model_dump(
            exclude=excluded_fields
        )


_guard_public_operations(FulfillmentService)
