from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel
from heinzel_provider_sdk.acquisition_models import AcquisitionMode
from pydantic import Field, field_validator, model_validator

type AcquisitionEvidenceOutcome = Literal[
    "prepared",
    "acknowledged",
    "no_valid_plan",
    "resynchronization_required",
    "failed",
]
type AcquisitionPublicReasonCode = Literal[
    "acquisition_mode_not_admitted",
    "authorization_denied",
    "contract_invalid",
    "contract_not_activated",
    "encoded_byte_ceiling_exceeded",
    "encoded_byte_ceiling_not_admitted",
    "integrity_failure",
    "logical_object_not_admitted",
    "physical_delete_capture_unsupported",
    "provider_unavailable",
    "rate_limited",
    "record_ceiling_exceeded",
    "record_ceiling_not_admitted",
    "source_binding_authority_stale",
    "source_binding_not_admitted",
    "source_drift",
    "source_observation_not_admitted",
    "stale_checkpoint",
    "stripe_event_cursor_expired",
    "stripe_event_overlap_gap",
]

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class AcquisitionEvidenceReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    run_intent_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_ref: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    acquisition_mode: AcquisitionMode
    logical_object_refs: tuple[str, ...] = Field(min_length=1)
    prepared_receipt_ref: str | None
    checkpoint_receipt_ref: str | None
    prior_checkpoint_revision: int = Field(ge=0)
    resulting_checkpoint_revision: int | None = Field(default=None, ge=1)
    reason_codes: tuple[AcquisitionPublicReasonCode, ...]
    outcome: AcquisitionEvidenceOutcome
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @field_validator("evidence_id", "prepared_receipt_ref", "checkpoint_receipt_ref")
    @classmethod
    def requires_opaque_receipt_reference(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST.fullmatch(value):
            raise ValueError("receipt references must be independently allocated opaque values")
        return value

    @model_validator(mode="after")
    def requires_canonical_outcome_shape(self) -> Self:
        if self.logical_object_refs != tuple(sorted(set(self.logical_object_refs))):
            raise ValueError("logical_object_refs must be unique and in canonical order")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("reason_codes must be unique and in canonical order")
        if self.outcome == "prepared":
            if (
                self.prepared_receipt_ref is None
                or self.checkpoint_receipt_ref is not None
                or self.resulting_checkpoint_revision is not None
                or self.reason_codes
            ):
                raise ValueError("prepared receipt has contradictory outcome fields")
        elif self.outcome == "acknowledged":
            if (
                self.prepared_receipt_ref is None
                or self.checkpoint_receipt_ref is None
                or self.resulting_checkpoint_revision != self.prior_checkpoint_revision + 1
                or self.reason_codes
            ):
                raise ValueError(
                    "acknowledged receipt must advance exactly one revision with both references"
                )
        elif (
            self.prepared_receipt_ref is not None
            or self.checkpoint_receipt_ref is not None
            or self.resulting_checkpoint_revision is not None
            or not self.reason_codes
        ):
            raise ValueError("failed and governed outcomes require only public reason_codes")
        return self
