from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel
from pydantic import Field, field_validator, model_validator

from .models import EncryptionAtRestDisposition, EngineKind, WarehouseValidationProfile

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ENGINE_VERSION_PATTERN = r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z]{1,32})?$"


def _requires_timezone_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


class WarehouseValidationEvidence(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str
    tenant_id: str
    binding_id: str
    binding_revision: int
    validation_profile: WarehouseValidationProfile
    engine_kind: EngineKind
    engine_version: str = Field(max_length=64, pattern=_ENGINE_VERSION_PATTERN)
    engine_build_digest: str = Field(pattern=_DIGEST_PATTERN)
    engine_image_digest: str = Field(pattern=_DIGEST_PATTERN)
    principal_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    namespace_grant_matrix_digest: str = Field(pattern=_DIGEST_PATTERN)
    tls_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    network_isolation_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    encryption_at_rest_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    encryption_at_rest_disposition: EncryptionAtRestDisposition
    positive_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    denial_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    ledger_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    monitoring_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    capacity_alert_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    backup_artifact_digest: str = Field(pattern=_DIGEST_PATTERN)
    restore_verification_digest: str = Field(pattern=_DIGEST_PATTERN)
    restore_cleanup_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _requires_timezone_aware_utc(value)

    @model_validator(mode="after")
    def requires_profile_specific_encryption_evidence(self) -> Self:
        permitted_pairs = {
            (WarehouseValidationProfile.PRODUCTION, EncryptionAtRestDisposition.PROVEN),
            (
                WarehouseValidationProfile.LOCAL_ACCEPTANCE,
                EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE,
            ),
        }
        if (self.validation_profile, self.encryption_at_rest_disposition) not in permitted_pairs:
            raise ValueError("validation profile and encryption disposition are incompatible")
        return self


class WarehouseResumeValidationEvidence(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str
    tenant_id: str
    binding_id: str
    binding_revision: int
    engine_kind: EngineKind
    engine_version: str = Field(max_length=64, pattern=_ENGINE_VERSION_PATTERN)
    engine_build_digest: str = Field(pattern=_DIGEST_PATTERN)
    engine_image_digest: str = Field(pattern=_DIGEST_PATTERN)
    tls_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    network_isolation_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    monitoring_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    positive_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    denial_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    storage_integrity_probe_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _requires_timezone_aware_utc(value)


class WarehouseRestoreVerification(ArtifactModel):
    schema_version: Literal["1"] = "1"
    verification_id: str
    tenant_id: str
    binding_id: str
    binding_revision: int
    engine_kind: EngineKind
    source_backup_artifact_digest: str = Field(pattern=_DIGEST_PATTERN)
    representative_data_digest: str = Field(pattern=_DIGEST_PATTERN)
    schema_metadata_digest: str = Field(pattern=_DIGEST_PATTERN)
    principal_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    integrity_marker_digest: str = Field(pattern=_DIGEST_PATTERN)
    query_behavior_digest: str = Field(pattern=_DIGEST_PATTERN)
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _requires_timezone_aware_utc(value)


class WarehouseRetirementEvidence(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str
    tenant_id: str
    binding_id: str
    binding_revision: int
    resource_inventory_digest: str = Field(pattern=_DIGEST_PATTERN)
    cleanup_disposition_digest: str = Field(pattern=_DIGEST_PATTERN)
    retention_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    completed_resource_count: int = Field(ge=0)
    retained_resource_count: int = Field(ge=0)
    cleanup_failed_resource_count: int = Field(ge=0)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _requires_timezone_aware_utc(value)
