from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from heinzel_contract_model import ArtifactModel
from pydantic import Field, field_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class CatalogBindingState(StrEnum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    VALIDATING = "validating"
    READY = "ready"
    SUSPENDED = "suspended"
    RETIRING = "retiring"
    RETIRED = "retired"
    FAILED = "failed"


class CatalogBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    provider_kind: Literal["openmetadata"] = "openmetadata"
    deployment_mode: Literal["heinzel_managed"] = "heinzel_managed"
    capability_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    lifecycle_state: CatalogBindingState
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    provisioned_at: datetime | None = None

    @field_validator("created_at", "updated_at", "provisioned_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class CatalogValidationEvidence(ArtifactModel):
    evidence_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    provider_version: str = Field(min_length=1)
    provider_build_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_image_set_digest: str = Field(pattern=_DIGEST_PATTERN)
    positive_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    denial_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    stable_identity_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    backup_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)
