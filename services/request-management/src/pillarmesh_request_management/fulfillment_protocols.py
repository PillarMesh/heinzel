from __future__ import annotations

from typing import Protocol

from pillarmesh_contract_model import ArtifactModel, ArtifactReference
from pydantic import Field

from .fulfillment_models import (
    AccessScopePreview,
    FreshnessDisposition,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    StakeholderAnswerDraft,
)
from .models import InboxRequest


class ResolutionFailure(ArtifactModel):
    reason_codes: tuple[str, ...] = Field(min_length=1)
    constraint_refs: tuple[ArtifactReference, ...]
    smallest_changes: tuple[str, ...] = Field(min_length=1)


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


class FreshnessEvaluator(Protocol):
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> FreshnessDisposition: ...
