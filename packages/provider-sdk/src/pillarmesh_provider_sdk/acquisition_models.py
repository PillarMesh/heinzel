from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Self

from pillarmesh_contract_model import digest
from pydantic import Field, field_validator, model_validator

from .models import ProviderModel

type AcquisitionMode = Literal["snapshot", "incremental", "reconciliation"]
type AcquisitionValueType = Literal["null", "boolean", "integer", "decimal", "string", "timestamp"]
type AcquisitionScalar = bool | int | Decimal | str | datetime | None
type AcquisitionNoValidPlanReasonCode = Literal[
    "acquisition_mode_not_admitted",
    "contract_not_activated",
    "encoded_byte_ceiling_not_admitted",
    "logical_object_not_admitted",
    "physical_delete_capture_unsupported",
    "record_ceiling_not_admitted",
    "source_binding_authority_stale",
    "source_binding_not_admitted",
    "source_observation_not_admitted",
]
type ResynchronizationReasonCode = Literal[
    "stripe_event_cursor_expired",
    "stripe_event_overlap_gap",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _require_canonical_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")
    if values != tuple(sorted(values)):
        raise ValueError(f"{field_name} must be in canonical order")


def acquisition_intent_key(
    *,
    tenant_id: str,
    run_intent_ref: str,
    contract_digest: str,
    source_binding_ref: str,
    acquisition_mode: AcquisitionMode,
    object_refs: tuple[str, ...],
    prior_checkpoint_revision: int,
) -> str:
    return digest(
        {
            "domain": "pillarmesh-acquisition-intent-v1",
            "tenant_id": tenant_id,
            "run_intent_ref": run_intent_ref,
            "contract_digest": contract_digest,
            "source_binding_ref": source_binding_ref,
            "acquisition_mode": acquisition_mode,
            "object_refs": object_refs,
            "prior_checkpoint_revision": prior_checkpoint_revision,
        }
    )


class AcquisitionIntent(ProviderModel):
    schema_version: Literal["1"] = "1"
    intent_key: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    run_intent_ref: str = Field(pattern=_DIGEST_PATTERN)
    contract_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    source_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    acquisition_mode: AcquisitionMode
    object_refs: tuple[str, ...] = Field(min_length=1)
    prior_checkpoint_revision: int = Field(ge=0)
    prior_checkpoint_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    record_ceiling: int = Field(gt=0)
    encoded_byte_ceiling: int = Field(gt=0)
    admitted_at: datetime

    @field_validator("admitted_at")
    @classmethod
    def requires_utc_admission_time(cls, value: datetime) -> datetime:
        return _require_utc(value, "admitted_at")

    @model_validator(mode="after")
    def requires_matching_intent_key(self) -> Self:
        _require_canonical_unique(self.object_refs, "object_refs")
        if (self.prior_checkpoint_revision == 0) != (self.prior_checkpoint_digest is None):
            raise ValueError(
                "prior_checkpoint_digest must be absent only for checkpoint revision zero"
            )
        expected = acquisition_intent_key(
            tenant_id=self.tenant_id,
            run_intent_ref=self.run_intent_ref,
            contract_digest=self.contract_digest,
            source_binding_ref=self.source_binding_ref,
            acquisition_mode=self.acquisition_mode,
            object_refs=self.object_refs,
            prior_checkpoint_revision=self.prior_checkpoint_revision,
        )
        if self.intent_key != expected:
            raise ValueError("intent_key does not match canonical authority inputs")
        return self


class AcquisitionField(ProviderModel):
    name: str = Field(min_length=1)
    value_type: AcquisitionValueType
    nullable: bool


class AcquisitionObjectSchema(ProviderModel):
    logical_object_ref: str = Field(min_length=1)
    schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    fields: tuple[AcquisitionField, ...] = Field(min_length=1)
    record_key_fields: tuple[str, ...] = Field(min_length=1)
    source_updated_at_field: str | None
    operation_semantics: Literal["upsert_only"] = "upsert_only"

    @model_validator(mode="after")
    def requires_exact_field_references(self) -> Self:
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        if len(self.record_key_fields) != len(set(self.record_key_fields)) or not set(
            self.record_key_fields
        ).issubset(names):
            raise ValueError("record_key_fields must be unique declared fields")
        if self.source_updated_at_field is not None:
            updated_field = next(
                (field for field in self.fields if field.name == self.source_updated_at_field),
                None,
            )
            if updated_field is None or updated_field.value_type != "timestamp":
                raise ValueError("source_updated_at_field must name a timestamp field")
        if self.schema_digest != digest(self.fields):
            raise ValueError("schema_digest does not match the ordered fields")
        return self


class AcquisitionFieldValue(ProviderModel):
    name: str = Field(min_length=1)
    value: AcquisitionScalar

    @field_validator("value", mode="before")
    @classmethod
    def requires_scalar_value(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("floating-point acquisition values are forbidden")
        if isinstance(value, (Mapping, Sequence)) and not isinstance(value, str):
            raise ValueError("acquisition values must be scalar")
        if isinstance(value, (bytes, bytearray)):
            raise ValueError("acquisition values must be scalar")
        return value

    @field_validator("value")
    @classmethod
    def requires_utc_timestamp_value(cls, value: AcquisitionScalar) -> AcquisitionScalar:
        if isinstance(value, datetime):
            return _require_utc(value, "timestamp value")
        return value


class AcquisitionRecord(ProviderModel):
    schema_version: Literal["1"] = "1"
    logical_object_ref: str = Field(min_length=1)
    record_key: str = Field(min_length=1)
    source_created_at: datetime | None
    source_updated_at: datetime | None
    operation: Literal["upsert"] = "upsert"
    fields: tuple[AcquisitionFieldValue, ...] = Field(min_length=1)

    @field_validator("source_created_at", "source_updated_at")
    @classmethod
    def requires_utc_source_timestamps(
        cls, value: datetime | None, info: object
    ) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "source timestamp")
        return _require_utc(value, str(field_name))

    @model_validator(mode="after")
    def requires_unique_fields(self) -> Self:
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        return self


class AcquisitionBoundary(ProviderModel):
    schema_version: Literal["1"] = "1"
    logical_object_ref: str = Field(min_length=1)
    acquisition_mode: AcquisitionMode
    schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    lower_cursor_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    upper_cursor_digest: str = Field(pattern=_DIGEST_PATTERN)
    query_shape_digest: str = Field(pattern=_DIGEST_PATTERN)
    snapshot_identity_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    key_range_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    private_boundary_ref: str = Field(min_length=1)
    record_count: int = Field(ge=0)
    opened_at: datetime
    closed_at: datetime

    @field_validator("opened_at", "closed_at")
    @classmethod
    def requires_utc_boundary_times(cls, value: datetime, info: object) -> datetime:
        return _require_utc(value, str(getattr(info, "field_name", "boundary timestamp")))

    @model_validator(mode="after")
    def requires_completed_interval(self) -> Self:
        if self.closed_at < self.opened_at:
            raise ValueError("closed_at cannot precede opened_at")
        return self


class AcquisitionSegmentManifest(ProviderModel):
    schema_version: Literal["1"] = "1"
    segment_name: str = Field(min_length=1, pattern=r"^[0-9]{4}-[0-9a-f]{64}\.jsonl$")
    logical_object_ref: str = Field(min_length=1)
    record_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    boundary_digest: str = Field(pattern=_DIGEST_PATTERN)
    encoding: Literal["canonical_jsonl_v1"] = "canonical_jsonl_v1"
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    record_set_digest: str = Field(pattern=_DIGEST_PATTERN)
    record_count: int = Field(ge=0)
    encoded_bytes: int = Field(ge=0)


def acquisition_batch_id(
    *,
    intent_key: str,
    prior_checkpoint_revision: int,
    candidate_checkpoint_digest: str,
    segment_manifests: tuple[AcquisitionSegmentManifest, ...],
) -> str:
    return digest(
        {
            "domain": "pillarmesh-acquisition-batch-v1",
            "intent_key": intent_key,
            "prior_checkpoint_revision": prior_checkpoint_revision,
            "candidate_checkpoint_digest": candidate_checkpoint_digest,
            "segment_manifest_digests": tuple(digest(segment) for segment in segment_manifests),
        }
    )


class AcquisitionBatchManifest(ProviderModel):
    schema_version: Literal["1"] = "1"
    batch_id: str = Field(pattern=_DIGEST_PATTERN)
    intent_key: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    source_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    acquisition_mode: AcquisitionMode
    prior_checkpoint_revision: int = Field(ge=0)
    prior_checkpoint_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    candidate_checkpoint_digest: str = Field(pattern=_DIGEST_PATTERN)
    segment_manifests: tuple[AcquisitionSegmentManifest, ...]
    total_record_count: int = Field(ge=0)
    total_encoded_bytes: int = Field(ge=0)
    prepared_at: datetime

    @field_validator("prepared_at")
    @classmethod
    def requires_utc_prepared_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "prepared_at")

    @model_validator(mode="after")
    def requires_canonical_exact_manifest(self) -> Self:
        object_refs = tuple(segment.logical_object_ref for segment in self.segment_manifests)
        _require_canonical_unique(object_refs, "segment logical_object_refs")
        if (self.prior_checkpoint_revision == 0) != (self.prior_checkpoint_digest is None):
            raise ValueError(
                "prior_checkpoint_digest must be absent only for checkpoint revision zero"
            )
        if self.total_record_count != sum(
            segment.record_count for segment in self.segment_manifests
        ):
            raise ValueError("total_record_count does not match segment manifests")
        if self.total_encoded_bytes != sum(
            segment.encoded_bytes for segment in self.segment_manifests
        ):
            raise ValueError("total_encoded_bytes does not match segment manifests")
        expected = acquisition_batch_id(
            intent_key=self.intent_key,
            prior_checkpoint_revision=self.prior_checkpoint_revision,
            candidate_checkpoint_digest=self.candidate_checkpoint_digest,
            segment_manifests=self.segment_manifests,
        )
        if self.batch_id != expected:
            raise ValueError("batch_id does not match canonical manifest inputs")
        return self


class AcquisitionPreparedReceipt(ProviderModel):
    schema_version: Literal["1"] = "1"
    prepared_receipt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    intent_key: str = Field(pattern=_DIGEST_PATTERN)
    batch_id: str = Field(pattern=_DIGEST_PATTERN)
    batch_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    prior_checkpoint_revision: int = Field(ge=0)
    candidate_checkpoint_digest: str = Field(pattern=_DIGEST_PATTERN)
    cursor_version: str = Field(min_length=1, strict=True)
    prepared_at: datetime

    @field_validator("prepared_at")
    @classmethod
    def requires_utc_prepared_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "prepared_at")


class AcquisitionAcknowledgement(ProviderModel):
    schema_version: Literal["1"] = "1"
    acknowledgement_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    consumer_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    batch_id: str = Field(pattern=_DIGEST_PATTERN)
    batch_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    prior_checkpoint_revision: int = Field(ge=0)
    candidate_checkpoint_digest: str = Field(pattern=_DIGEST_PATTERN)
    consumer_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    acknowledged_at: datetime

    @field_validator("acknowledged_at")
    @classmethod
    def requires_utc_acknowledged_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "acknowledged_at")


class AcquisitionCheckpointReceipt(ProviderModel):
    schema_version: Literal["1"] = "1"
    checkpoint_receipt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_binding_ref: str = Field(min_length=1)
    previous_revision: int = Field(ge=0)
    committed_revision: int = Field(ge=1)
    cursor_digest: str = Field(pattern=_DIGEST_PATTERN)
    batch_id: str = Field(pattern=_DIGEST_PATTERN)
    acknowledgement_id: str = Field(min_length=1)
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def requires_utc_committed_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "committed_at")

    @model_validator(mode="after")
    def advances_exactly_one_revision(self) -> Self:
        if self.committed_revision != self.previous_revision + 1:
            raise ValueError("committed_revision must advance exactly one revision")
        return self


class AcquisitionNoValidPlan(ProviderModel):
    schema_version: Literal["1"] = "1"
    reason_codes: tuple[AcquisitionNoValidPlanReasonCode, ...] = Field(min_length=1)
    failed_constraints: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def requires_canonical_reasons(self) -> Self:
        _require_canonical_unique(self.reason_codes, "reason_codes")
        _require_canonical_unique(self.failed_constraints, "failed_constraints")
        return self


class ResynchronizationRequired(ProviderModel):
    schema_version: Literal["1"] = "1"
    reason_code: ResynchronizationReasonCode
    source_binding_ref: str = Field(min_length=1)
    affected_object_refs: tuple[str, ...] = Field(min_length=1)
    last_proven_checkpoint_digest: str = Field(pattern=_DIGEST_PATTERN)
    required_scope: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_utc_created_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @model_validator(mode="after")
    def requires_canonical_objects(self) -> Self:
        _require_canonical_unique(self.affected_object_refs, "affected_object_refs")
        return self
