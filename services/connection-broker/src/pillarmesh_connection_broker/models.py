from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind
from pydantic import Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type SourceAccountMode = Literal["not_applicable", "test", "live"]


class SourceConnectionBindingState(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    READY = "ready"
    SUSPENDED = "suspended"
    FAILED = "failed"
    RETIRED = "retired"


SOURCE_BINDING_TRANSITIONS: dict[
    SourceConnectionBindingState,
    frozenset[SourceConnectionBindingState],
] = {
    SourceConnectionBindingState.DRAFT: frozenset(
        {SourceConnectionBindingState.VALIDATING, SourceConnectionBindingState.RETIRED}
    ),
    SourceConnectionBindingState.VALIDATING: frozenset(
        {
            SourceConnectionBindingState.READY,
            SourceConnectionBindingState.FAILED,
            SourceConnectionBindingState.RETIRED,
        }
    ),
    SourceConnectionBindingState.READY: frozenset(
        {
            SourceConnectionBindingState.VALIDATING,
            SourceConnectionBindingState.SUSPENDED,
            SourceConnectionBindingState.RETIRED,
        }
    ),
    SourceConnectionBindingState.SUSPENDED: frozenset(
        {SourceConnectionBindingState.VALIDATING, SourceConnectionBindingState.RETIRED}
    ),
    SourceConnectionBindingState.FAILED: frozenset(
        {SourceConnectionBindingState.VALIDATING, SourceConnectionBindingState.RETIRED}
    ),
    SourceConnectionBindingState.RETIRED: frozenset(),
}


class SourceConnectionBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    connection_handle: str = Field(min_length=1)
    account_mode: SourceAccountMode
    lifecycle_state: SourceConnectionBindingState
    approved_object_refs: tuple[str, ...] = Field(min_length=1)
    capability_profile_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    source_observation_ref: str | None = Field(default=None, min_length=1)
    credential_revision: int = Field(ge=1)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def requires_canonical_objects_and_ready_authority(self) -> Self:
        if self.approved_object_refs != tuple(sorted(set(self.approved_object_refs))):
            raise ValueError("approved_object_refs must be unique and canonically ordered")
        if (self.capability_profile_digest is None) != (self.source_observation_ref is None):
            raise ValueError("capability and observation authority must be set together")
        if self.lifecycle_state is SourceConnectionBindingState.READY:
            if self.capability_profile_digest is None:
                raise ValueError("ready binding requires validated capability authority")
        elif self.capability_profile_digest is not None:
            raise ValueError("only ready binding may carry validated capability authority")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class SourceBindingValidationEvidence(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    credential_revision: int = Field(ge=1)
    provider_kind: AcquisitionProviderKind
    positive_probe_succeeded: Literal[True]
    positive_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    denial_probe_succeeded: Literal[True]
    denial_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_observation_ref: str = Field(min_length=1)
    capability_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)
