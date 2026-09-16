from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel, digest
from pydantic import Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


class TriggerWindow(ArtifactModel):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def end_follows_start(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("trigger window must end after it starts")
        return self


class RunIntent(ArtifactModel):
    tenant_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    trigger_policy_version: str = Field(min_length=1)
    trigger_window: TriggerWindow
    reason: Literal["scheduled", "run_now", "backfill", "retry"]

    @property
    def intent_digest(self) -> str:
        return digest(self)


class RunRecord(ArtifactModel):
    run_id: str = Field(min_length=1)
    intent: RunIntent
    intent_digest: str = Field(pattern=_DIGEST_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def digest_matches_intent(self) -> Self:
        if self.intent_digest != self.intent.intent_digest:
            raise ValueError("run intent digest does not match intent")
        return self


class RunAttemptClaim(ArtifactModel):
    run_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    epoch: int = Field(ge=1)
    worker_id: str = Field(min_length=1)
    claimed_at: datetime
    lease_expires_at: datetime

    @field_validator("claimed_at", "lease_expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def lease_expires_after_claim(self) -> Self:
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease must expire after claim")
        return self


class RunAttemptCompletion(ArtifactModel):
    run_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    epoch: int = Field(ge=1)
    worker_id: str = Field(min_length=1)
    outcome: Literal["succeeded", "failed"]
    failure_classification: Literal["transient", "permanent"] | None = None
    durable_boundary_ref: str = Field(min_length=1)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def failure_requires_classification(self) -> Self:
        has_failure = self.failure_classification is not None
        if has_failure != (self.outcome == "failed"):
            raise ValueError("failure classification must be set only for a failed attempt")
        return self


class RunCancellation(ArtifactModel):
    run_id: str = Field(min_length=1)
    cancelled_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    cancelled_at: datetime

    @field_validator("cancelled_at")
    @classmethod
    def cancelled_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class RunRetryRequest(ArtifactModel):
    command_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    failed_attempt_number: int = Field(ge=1)
    failed_epoch: int = Field(ge=1)
    requested_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    incident_revision: int = Field(ge=1)
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)
