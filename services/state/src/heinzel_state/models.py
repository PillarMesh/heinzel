from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from heinzel_provider_sdk.errors import AcquisitionProviderKind
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class StateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class PreparedAcquisitionStateStatus(StrEnum):
    PREPARED = "prepared"
    ACKNOWLEDGED = "acknowledged"


class SourceCheckpointState(StateModel):
    tenant_id: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    cursor_version: str = Field(min_length=1)
    revision: int = Field(ge=0)
    encrypted_cursor_payload: bytes = Field(repr=False)
    cursor_digest: str = Field(pattern=_DIGEST_PATTERN)
    last_batch_id: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_ordered_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class PreparedAcquisitionState(StateModel):
    tenant_id: str = Field(min_length=1)
    intent_key: str = Field(pattern=_DIGEST_PATTERN)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    source_binding_revision: int = Field(ge=1)
    credential_revision: int = Field(ge=1)
    binding_authority_epoch: int = Field(ge=0)
    contract_authority_epoch: int = Field(ge=0)
    acknowledgement_consumer_ref: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    prior_checkpoint_revision: int = Field(ge=0)
    batch_id: str = Field(pattern=_DIGEST_PATTERN)
    batch_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    candidate_cursor_ciphertext: bytes = Field(repr=False)
    candidate_checkpoint_digest: str = Field(pattern=_DIGEST_PATTERN)
    cursor_version: str = Field(min_length=1, strict=True)
    state: PreparedAcquisitionStateStatus
    acknowledgement_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_state_authority_and_ordered_timestamps(self) -> Self:
        has_acknowledgement = self.acknowledgement_digest is not None
        if has_acknowledgement != (self.state is PreparedAcquisitionStateStatus.ACKNOWLEDGED):
            raise ValueError("acknowledgement_digest must be set only for acknowledged state")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class GovernedAcquisitionOutcomeState(StateModel):
    schema_version: Literal["1"] = "1"
    outcome_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    outcome_kind: Literal["no_valid_plan", "resynchronization_required"]
    payload: bytes = Field(repr=False)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)
