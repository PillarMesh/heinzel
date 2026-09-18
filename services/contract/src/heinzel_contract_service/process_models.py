from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from heinzel_contract_model import ArtifactModel, ArtifactReference, digest
from heinzel_provider_sdk import AcquisitionObjectSchema
from heinzel_provider_sdk.acquisition_models import AcquisitionMode
from pydantic import Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class BusinessProcessManifest(ArtifactModel):
    schema_version: Literal["1"] = "1"
    process_name: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    participants: tuple[str, ...]
    outcomes: tuple[str, ...]
    entities: tuple[str, ...]
    events: tuple[str, ...]
    states: tuple[str, ...]
    rules: tuple[str, ...]
    source_references: tuple[str, ...]
    unresolved_questions: tuple[str, ...]


class ProcessPackageReceipt(ArtifactModel):
    package_id: str
    tenant_id: str
    version: int = Field(ge=1)
    media_type: Literal["text/markdown; charset=utf-8"]
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    uploader_id: str
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class ProcessPackageSnapshot(ArtifactModel):
    receipt: ProcessPackageReceipt
    manifest: BusinessProcessManifest


class AcquisitionActivationApproval(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    process_package_ref: ArtifactReference
    product_intent_ref: ArtifactReference
    destination_product_ref: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("approval timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class ActivatedAcquisitionContract(ArtifactModel):
    schema_version: Literal["2"] = "2"
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    process_package_ref: ArtifactReference
    product_intent_ref: ArtifactReference
    destination_product_ref: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    source_binding_revision: int = Field(ge=1)
    credential_revision: int = Field(ge=1)
    acknowledgement_consumer_ref: str = Field(min_length=1)
    capability_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_observation_ref: str = Field(min_length=1)
    source_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    lifecycle_state: Literal["activated", "inactive"]
    acquisition_modes: tuple[AcquisitionMode, ...] = Field(min_length=1)
    object_schemas: tuple[AcquisitionObjectSchema, ...] = Field(min_length=1)
    record_ceiling: int = Field(gt=0)
    encoded_byte_ceiling: int = Field(gt=0)
    activated_by: str = Field(min_length=1)
    activated_at: datetime

    @field_validator("activated_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("activation timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def requires_canonical_unique_authority(self) -> ActivatedAcquisitionContract:
        if self.acquisition_modes != tuple(sorted(set(self.acquisition_modes))):
            raise ValueError("acquisition_modes must be unique and in canonical order")
        object_refs = tuple(schema.logical_object_ref for schema in self.object_schemas)
        if object_refs != tuple(sorted(set(object_refs))):
            raise ValueError("object_schemas must be unique and in canonical order")
        return self


class ActivatedAcquisitionContractRecord(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    contract_artifact_ref: ArtifactReference
    activated_by: str = Field(min_length=1)
    activated_at: datetime
    contract: ActivatedAcquisitionContract

    @model_validator(mode="after")
    def binds_exact_contract_revision(self) -> ActivatedAcquisitionContractRecord:
        expected_reference = ArtifactReference(
            artifact_id=self.contract_ref,
            version=self.revision,
            digest=digest(self.contract),
        )
        if (
            self.tenant_id != self.contract.tenant_id
            or self.contract_ref != self.contract.contract_ref
            or self.contract_digest != self.contract.contract_digest
            or self.contract_artifact_ref != expected_reference
            or self.activated_by != self.contract.activated_by
            or self.activated_at != self.contract.activated_at
        ):
            raise ValueError("activation record does not bind the exact contract revision")
        return self
