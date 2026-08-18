from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pillarmesh_contract_model import ArtifactModel
from pydantic import Field, field_validator


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


class InboxRequest(ArtifactModel):
    request_id: str
    tenant_id: str
    requester_id: str
    payload: Annotated[StakeholderQuestion | DataAccessRequest, Field(discriminator="request_type")]
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


class ConversationEntry(ArtifactModel):
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
