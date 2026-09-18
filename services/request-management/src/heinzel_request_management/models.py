from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from heinzel_contract_model import ArtifactModel, digest
from pydantic import Field, field_validator, model_validator


class DelegatedRequestProvenance(ArtifactModel):
    schema_version: Literal["1"] = "1"
    delegation_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    agent_client_ref: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    delegation_authority_ref: str = Field(min_length=1, max_length=512)
    delegation_authority_revision: int = Field(gt=0)
    entitlement_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=512)
    policy_revision: int = Field(gt=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    invoked_at: datetime

    @field_validator("invoked_at")
    @classmethod
    def invoked_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("invoked_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class RequestState(StrEnum):
    SUBMITTED = "submitted"
    CLARIFYING = "clarifying"
    INVESTIGATING = "investigating"
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    DELIVERED = "delivered"
    MONITORING = "monitoring"
    REJECTED = "rejected"
    NO_VALID_PLAN = "no_valid_plan"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RETIRED = "retired"


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class StakeholderQuestion(ArtifactModel):
    request_type: Literal["stakeholder_question"] = "stakeholder_question"
    purpose: str = Field(min_length=1, max_length=512)
    question: str = Field(min_length=1, max_length=4000)


class DataAccessRequest(ArtifactModel):
    request_type: Literal["data_access"] = "data_access"
    purpose: str = Field(min_length=1, max_length=512)
    data_product_id: str
    requested_fields: tuple[str, ...]
    access_mode: Literal["query", "dashboard", "export"]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("expires_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class SchemaSemanticChangeRequest(ArtifactModel):
    request_type: Literal["schema_semantic_change"] = "schema_semantic_change"
    purpose: str = Field(min_length=1, max_length=512)
    review_bundle_id: str = Field(min_length=1)
    review_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_authority_refs: tuple[str, ...]
    before_observation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_observation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    affected_semantic_ref: str | None = Field(default=None, min_length=1)
    affected_contract_ref: str | None = Field(default=None, min_length=1)


class DataProductChangeRequest(ArtifactModel):
    request_type: Literal["data_product_change"] = "data_product_change"
    purpose: str = Field(min_length=1, max_length=512)
    requested_outcome: str = Field(min_length=1, max_length=4000)
    missing_capability_refs: tuple[str, ...] = Field(min_length=1)
    source_request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)


class InboxRequest(ArtifactModel):
    title: str | None = Field(
        default=None, min_length=1, max_length=16_000, exclude_if=lambda value: value is None
    )
    request_id: str
    tenant_id: str
    requester_id: str
    delegated_agent: DelegatedRequestProvenance | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    payload: Annotated[
        StakeholderQuestion
        | DataAccessRequest
        | SchemaSemanticChangeRequest
        | DataProductChangeRequest,
        Field(discriminator="request_type"),
    ]
    state: RequestState
    revision: int = Field(ge=1)
    submitted_at: datetime
    updated_at: datetime

    @field_validator("submitted_at", "updated_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def delegated_agent_matches_requester_and_purpose(self) -> Self:
        delegated_agent = self.delegated_agent
        if delegated_agent is None:
            return self
        if (
            delegated_agent.principal_ref != self.requester_id
            or delegated_agent.purpose_digest != digest(self.payload.purpose)
        ):
            raise ValueError("delegated agent provenance does not match the request")
        return self


type ConversationAuthorRole = Literal[
    "requester", "data_architect", "data_owner", "policy_approver", "budget_approver", "heinzel"
]


class ConversationEntry(ArtifactModel):
    author_role: ConversationAuthorRole | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entry_id: str
    request_id: str
    request_revision: int = Field(ge=1)
    actor_id: str
    body: str = Field(min_length=1, max_length=8000)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class TransitionEvent(ArtifactModel):
    event_id: str
    request_id: str
    request_revision: int = Field(ge=2)
    actor_id: str
    from_state: RequestState
    to_state: RequestState
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class DecisionBinding(ArtifactModel):
    decision_id: str
    request_id: str
    request_revision: int = Field(ge=1)
    actor_id: str
    kind: DecisionKind
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)
