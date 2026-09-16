from __future__ import annotations

from typing import Protocol

from pillarmesh_contract_model import (
    ArtifactModel,
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactAuthoritySnapshot,
    ImpactSubject,
)
from pydantic import Field

from .fulfillment_models import (
    AccessGrantAdmissionBinding,
    AccessGrantDeliveryObservation,
    AccessScopePreview,
    FreshnessDisposition,
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    StakeholderAnswerDraft,
)
from .models import InboxRequest


class ResolutionFailure(ArtifactModel):
    reason_codes: tuple[str, ...] = Field(min_length=1)
    constraint_refs: tuple[ArtifactReference, ...]
    smallest_changes: tuple[str, ...] = Field(min_length=1)
    requester_safe_explanation: str | None = Field(default=None, min_length=1, max_length=1000)


class AuthorityRoleResolver(Protocol):
    def has_role(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> bool: ...


class FulfillmentSnapshotResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot] | ResolutionFailure: ...


class AnswerCandidateProvider(Protocol):
    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
    ) -> StakeholderAnswerDraft: ...


class AnswerExecutionProvider(Protocol):
    def execute(
        self,
        *,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        admission: FulfillmentAdmissionReceipt,
        grounding: FulfillmentGroundingSnapshot,
    ) -> tuple[StakeholderAnswerDraft, tuple[ArtifactReference, ...]]: ...


class AccessCandidateProvider(Protocol):
    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessScopePreview: ...


class DataProductOwnerResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        data_product_ref: ArtifactReference,
    ) -> str: ...


class AccessGrantAdmissionResolver(Protocol):
    def bind(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessGrantAdmissionBinding: ...


class AccessGrantActivationReader(Protocol):
    def read_active(
        self, *, tenant_id: str, request_id: str, grant_id: str
    ) -> AccessGrantDeliveryObservation | None: ...


class ImpactAdmissionResolver(Protocol):
    """Resolve impact authority or raise ``ImpactAdmissionResolutionError``."""

    def bind(
        self,
        *,
        tenant_id: str,
        proposal: FulfillmentProposal,
    ) -> ImpactAdmissionBinding | None: ...

    def rederive(
        self,
        *,
        tenant_id: str,
        subject: ImpactSubject,
        source_record_refs: tuple[ArtifactReference, ...],
    ) -> ImpactAuthoritySnapshot: ...


class FreshnessEvaluator(Protocol):
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition: ...
