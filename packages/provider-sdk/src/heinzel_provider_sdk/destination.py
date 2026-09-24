from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from heinzel_contract_model import digest
from pydantic import Field, field_validator, model_validator

from .models import ProviderModel

type IdempotencyKey = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type DestinationProviderKind = Literal["postgresql", "clickhouse"]


def staged_segment_digest(rows: tuple[bytes, ...]) -> str:
    hasher = sha256()
    for row in rows:
        hasher.update(len(row).to_bytes(8, "big"))
        hasher.update(row)
    return hasher.hexdigest()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


class StagedSegment(ProviderModel):
    schema_version: Literal["1"] = "1"
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=0)
    rows: tuple[bytes, ...]

    @model_validator(mode="after")
    def requires_exact_manifest(self) -> Self:
        if self.record_count != len(self.rows):
            raise ValueError("record_count does not match staged rows")
        if self.segment_digest != staged_segment_digest(self.rows):
            raise ValueError("segment_digest does not match staged rows")
        return self


class RawGenerationTarget(ProviderModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    trigger_window: str = Field(min_length=1)
    destination_binding_ref: str = Field(min_length=1)
    logical_object_ref: str = Field(min_length=1)
    table_ref: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,62}$")
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def raw_generation_key(*, target: RawGenerationTarget, segment_digest: str) -> str:
    return digest(
        {
            "domain": "heinzel-raw-generation-v1",
            "tenant_id": target.tenant_id,
            "contract_ref": target.contract_ref,
            "contract_revision": target.contract_revision,
            "trigger_window": target.trigger_window,
            "segment_digest": segment_digest,
            "destination_binding_ref": target.destination_binding_ref,
            "logical_object_ref": target.logical_object_ref,
        }
    )


class LandReceipt(ProviderModel):
    schema_version: Literal["1"] = "1"
    receipt_id: str = Field(min_length=1)
    idempotency_key: IdempotencyKey
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    trigger_window: str = Field(min_length=1)
    destination_binding_ref: str = Field(min_length=1)
    logical_object_ref: str = Field(min_length=1)
    target_table_ref: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]{0,62}$")
    generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(gt=0)
    provider_commit_ref: str = Field(min_length=1)
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def requires_utc_commit_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_matching_generation_id(self) -> Self:
        target = RawGenerationTarget(
            tenant_id=self.tenant_id,
            contract_ref=self.contract_ref,
            contract_revision=self.contract_revision,
            trigger_window=self.trigger_window,
            destination_binding_ref=self.destination_binding_ref,
            logical_object_ref=self.logical_object_ref,
            table_ref=self.target_table_ref,
            schema_digest=self.schema_digest,
        )
        expected = raw_generation_key(target=target, segment_digest=self.segment_digest)
        if self.generation_id != expected:
            raise ValueError("generation_id does not match canonical destination authority")
        return self


class CommitObservation(ProviderModel):
    schema_version: Literal["1"] = "1"
    outcome: Literal["committed", "not_found"]
    receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def requires_utc_observation_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_commit_digest(self) -> Self:
        if (self.outcome == "committed") != (self.receipt_digest is not None):
            raise ValueError("only a committed observation carries a receipt digest")
        return self


@runtime_checkable
class DestinationProvider(Protocol):
    @property
    def provider_kind(self) -> DestinationProviderKind: ...

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
    ) -> LandReceipt: ...

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation: ...
