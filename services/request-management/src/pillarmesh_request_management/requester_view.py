from __future__ import annotations

from datetime import datetime
from typing import Literal

from pillarmesh_contract_model import ArtifactModel, digest

from .fulfillment_errors import FulfillmentNotVisible
from .fulfillment_models import (
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DenialDispositionReceipt,
    FulfillmentAccessDeliveryReceipt,
    FulfillmentAdmissionReceipt,
    FulfillmentApprovalBinding,
    FulfillmentDeliveryReceipt,
    FulfillmentEvidenceReceipt,
    FulfillmentProposal,
    FulfillmentSubject,
    RequestDependency,
    RequestNoValidPlan,
    StakeholderAnswerDraft,
)
from .fulfillment_protocols import AuthorityRoleResolver
from .fulfillment_repository import FulfillmentRepository
from .models import InboxRequest, RequestState
from .service import RequestManagementService


class OwnDecisionView(ArtifactModel):
    lifecycle: Literal["semantic_review", "fulfillment"]
    decision: str
    authority_ref: str | None
    created_at: datetime


class RequesterAccessDeliveryView(ArtifactModel):
    access_mode: Literal["query", "dashboard", "export"]
    fields: tuple[str, ...]
    effective_at: datetime
    expires_at: datetime
    permissions: tuple[Literal["dashboard", "download", "query", "view"], ...]


class RequesterRequestView(ArtifactModel):
    request_id: str
    state: RequestState
    revision: int
    clarified_outcomes: tuple[ClarifiedOutcomeStatement, ...]
    own_decisions: tuple[OwnDecisionView, ...]
    fulfillment_status: Literal[
        "investigating", "in_review", "ready_for_execution", "denied", "closed"
    ]
    denial_explanation: str | None
    no_valid_plan_explanation: str | None
    delivered_answer: StakeholderAnswerDraft | None = None
    delivered_access: RequesterAccessDeliveryView | None = None
    delivery_id: str | None = None


class ReviewerRequestView(ArtifactModel):
    request_id: str
    state: RequestState
    revision: int
    clarified_outcomes: tuple[ClarifiedOutcomeStatement, ...]
    proposal_id: str
    proposal_revision: int
    # None whenever the held requirement binds something other than the candidate,
    # as the requester's clarified-outcome acceptance does.
    subject: FulfillmentSubject | None
    requirement: ApprovalRequirement
    own_decisions: tuple[OwnDecisionView, ...]


class ArchitectRequestView(ArtifactModel):
    request: InboxRequest
    clarified_outcomes: tuple[ClarifiedOutcomeStatement, ...]
    proposals: tuple[FulfillmentProposal, ...]
    approvals: tuple[FulfillmentApprovalBinding, ...]
    admissions: tuple[FulfillmentAdmissionReceipt, ...]
    deliveries: tuple[FulfillmentDeliveryReceipt, ...] = ()
    access_deliveries: tuple[FulfillmentAccessDeliveryReceipt, ...] = ()
    dependencies: tuple[RequestDependency, ...]
    no_valid_plans: tuple[RequestNoValidPlan, ...]
    denials: tuple[DenialDispositionReceipt, ...]
    evidence: tuple[FulfillmentEvidenceReceipt, ...]


class FulfillmentReadService:
    def __init__(
        self,
        *,
        request_service: RequestManagementService,
        repository: FulfillmentRepository,
        authority_role_resolver: AuthorityRoleResolver | None,
    ) -> None:
        self._request_service = request_service
        self._repository = repository
        self._authority_role_resolver = authority_role_resolver

    def requester_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
    ) -> RequesterRequestView:
        request = self._visible_request(tenant_id, request_id)
        if request.requester_id != actor_id:
            raise FulfillmentNotVisible("request is not visible to this actor")
        approvals = self._repository.list_approvals(tenant_id, request_id)
        current_approvals = approvals
        if request.state is RequestState.AWAITING_APPROVAL:
            proposals = self._repository.list_proposals(tenant_id, request_id)
            proposal = proposals[-1] if proposals else None
            current_approvals = tuple(
                approval
                for approval in approvals
                if proposal is not None
                and approval.request_revision == request.revision
                and approval.proposal_id == proposal.proposal_id
                and approval.proposal_revision == proposal.revision
                and approval.proposal_digest == digest(proposal)
                and any(
                    requirement.authority_ref == approval.authority_ref
                    and requirement.subject_digest == approval.subject_digest
                    for requirement in proposal.required_approvals
                )
            )
        own_decisions = tuple(
            OwnDecisionView(
                lifecycle="semantic_review",
                decision=decision.kind.value,
                authority_ref=None,
                created_at=decision.created_at,
            )
            for decision in self._request_service.list_decisions(tenant_id, request_id)
            if decision.actor_id == actor_id
        ) + tuple(
            OwnDecisionView(
                lifecycle="fulfillment",
                decision=approval.decision,
                authority_ref=approval.authority_ref,
                created_at=approval.created_at,
            )
            for approval in current_approvals
            if approval.actor_id == actor_id
        )
        denials = self._repository.list_denials(tenant_id, request_id)
        denial_explanation = (
            denials[-1].requester_safe_explanation
            if request.state is RequestState.REJECTED and denials
            else None
        )
        no_valid_plans = self._repository.list_no_valid_plans(tenant_id, request_id)
        current_refusal = no_valid_plans[-1] if no_valid_plans else None
        no_valid_plan_explanation = (
            current_refusal.requester_safe_explanation
            if request.state is RequestState.NO_VALID_PLAN
            and current_refusal is not None
            and current_refusal.resulting_request_revision == request.revision
            else None
        )
        deliveries = self._repository.list_deliveries(tenant_id, request_id)
        access_deliveries = self._repository.list_access_deliveries(tenant_id, request_id)
        current_delivery = deliveries[-1] if deliveries else None
        current_access_delivery = access_deliveries[-1] if access_deliveries else None
        delivered_answer = (
            current_delivery.answer
            if request.state is RequestState.DELIVERED
            and current_delivery is not None
            and current_delivery.resulting_request_revision == request.revision
            else None
        )
        return RequesterRequestView(
            request_id=request.request_id,
            state=request.state,
            revision=request.revision,
            clarified_outcomes=self._repository.list_clarified_outcomes(tenant_id, request_id),
            own_decisions=tuple(sorted(own_decisions, key=lambda item: item.created_at)),
            fulfillment_status=self._requester_status(request),
            denial_explanation=denial_explanation,
            no_valid_plan_explanation=no_valid_plan_explanation,
            delivered_answer=delivered_answer,
            delivered_access=(
                RequesterAccessDeliveryView(
                    access_mode=current_access_delivery.access_mode,
                    fields=current_access_delivery.fields,
                    effective_at=current_access_delivery.effective_at,
                    expires_at=current_access_delivery.expires_at,
                    permissions=current_access_delivery.permissions,
                )
                if request.state is RequestState.DELIVERED
                and current_access_delivery is not None
                and current_access_delivery.resulting_request_revision == request.revision
                else None
            ),
            delivery_id=(
                current_delivery.delivery_id
                if current_delivery is not None
                else (
                    current_access_delivery.delivery_id
                    if current_access_delivery is not None
                    else None
                )
            ),
        )

    def reviewer_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> ReviewerRequestView:
        request = self._visible_request(tenant_id, request_id)
        resolver = self._authority_role_resolver
        if resolver is None or not resolver.has_role(
            tenant_id=tenant_id,
            actor_id=actor_id,
            authority_ref=authority_ref,
        ):
            raise FulfillmentNotVisible("request is not visible to this actor")
        proposals = self._repository.list_proposals(tenant_id, request_id)
        if not proposals:
            raise FulfillmentNotVisible("request is not visible to this actor")
        proposal = proposals[-1]
        requirements = tuple(
            item for item in proposal.required_approvals if item.authority_ref == authority_ref
        )
        if len(requirements) != 1:
            raise FulfillmentNotVisible("request is not visible to this actor")
        own_decisions = tuple(
            OwnDecisionView(
                lifecycle="fulfillment",
                decision=approval.decision,
                authority_ref=approval.authority_ref,
                created_at=approval.created_at,
            )
            for approval in self._repository.list_approvals(tenant_id, request_id)
            if approval.actor_id == actor_id
            and approval.authority_ref == authority_ref
            and approval.request_revision == request.revision
            and approval.proposal_id == proposal.proposal_id
            and approval.proposal_revision == proposal.revision
            and approval.proposal_digest == digest(proposal)
            and approval.subject_digest == requirements[0].subject_digest
        )
        requirement = requirements[0]
        # Holding a requirement is not authority over the candidate. The requester's
        # acceptance requirement names the clarified outcome, so projecting the
        # subject for any held authority would disclose the unapproved candidate to
        # the requester.
        subject = (
            proposal.subject if requirement.subject_digest == digest(proposal.subject) else None
        )
        return ReviewerRequestView(
            request_id=request.request_id,
            state=request.state,
            revision=request.revision,
            clarified_outcomes=self._repository.list_clarified_outcomes(tenant_id, request_id),
            proposal_id=proposal.proposal_id,
            proposal_revision=proposal.revision,
            subject=subject,
            requirement=requirement,
            own_decisions=own_decisions,
        )

    def architect_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
    ) -> ArchitectRequestView:
        request = self._visible_request(tenant_id, request_id)
        resolver = self._authority_role_resolver
        if resolver is None or not resolver.has_role(
            tenant_id=tenant_id,
            actor_id=actor_id,
            authority_ref="role:data_engineering_architect",
        ):
            raise FulfillmentNotVisible("request is not visible to this actor")
        return ArchitectRequestView(
            request=request,
            clarified_outcomes=self._repository.list_clarified_outcomes(tenant_id, request_id),
            proposals=self._repository.list_proposals(tenant_id, request_id),
            approvals=self._repository.list_approvals(tenant_id, request_id),
            admissions=self._repository.list_admissions(tenant_id, request_id),
            deliveries=self._repository.list_deliveries(tenant_id, request_id),
            access_deliveries=self._repository.list_access_deliveries(tenant_id, request_id),
            dependencies=self._repository.list_dependencies(tenant_id, request_id),
            no_valid_plans=self._repository.list_no_valid_plans(tenant_id, request_id),
            denials=self._repository.list_denials(tenant_id, request_id),
            evidence=self._repository.list_evidence(tenant_id, request_id),
        )

    def _visible_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        try:
            return self._request_service.get(tenant_id, request_id)
        except KeyError as error:
            raise FulfillmentNotVisible("request is not visible to this actor") from error

    @staticmethod
    def _requester_status(
        request: InboxRequest,
    ) -> Literal["investigating", "in_review", "ready_for_execution", "denied", "closed"]:
        if request.state is RequestState.EXECUTING:
            return "ready_for_execution"
        if request.state is RequestState.DELIVERED:
            return "closed"
        if request.state is RequestState.REJECTED:
            return "denied"
        if request.state in (RequestState.PROPOSED, RequestState.AWAITING_APPROVAL):
            return "in_review"
        if request.state in (
            RequestState.NO_VALID_PLAN,
            RequestState.CANCELLED,
            RequestState.FAILED,
            RequestState.RETIRED,
        ):
            return "closed"
        return "investigating"
