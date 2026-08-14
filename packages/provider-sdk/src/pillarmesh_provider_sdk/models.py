from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnObservation(ProviderModel):
    name: str
    type_name: str
    nullable: bool


class ProviderObservation(ProviderModel):
    schema_version: Literal["1"] = "1"
    provider: Literal["postgresql", "snowflake"]
    connection_handle: str
    object_identity: str
    object_kind: Literal["base_table", "view", "unknown"]
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    columns: tuple[ColumnObservation, ...]
    key_name: str | None
    key_type: str | None
    key_nullable: bool | None
    key_constraint: Literal["primary_key", "unique", "none"] | None
    stable_key_order: bool | None
    read_only: bool | None
    capabilities: tuple[str, ...] | None
    observed_at: datetime | None
    snapshot_semantics: Literal["snapshot", "unknown"]
    commit_ledger_object_kind: Literal["base_table", "view", "unknown"] | None
    commit_ledger_columns: tuple[ColumnObservation, ...] | None
    commit_ledger_key_name: str | None
    commit_ledger_key_constraint: Literal["primary_key", "unique", "none"] | None
    evidence_safe: bool | None

    @field_validator("observed_at")
    @classmethod
    def aware_observation(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def ordered_capabilities(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ValueError("capabilities must be strings")
        return tuple(sorted(set(value)))


class OrderRow(ProviderModel):
    order_id: int
    customer_ref: str = Field(max_length=65_535)
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)
    status: str = Field(max_length=65_535)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return value


class DriftProbe(ProviderModel):
    object_identity: str
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceBoundary(ProviderModel):
    schema_version: Literal["2"] = "2"
    object_identity: str
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_identity: str
    key_range_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    query_shape_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    opened_at: datetime
    closed_at: datetime


class SegmentManifest(ProviderModel):
    schema_version: Literal["2"] = "2"
    batch_id: str
    segment_name: Literal["segment.csv"] = "segment.csv"
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int
    encoded_bytes: int
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_boundary_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CommitReceipt(ProviderModel):
    schema_version: Literal["1"] = "1"
    batch_id: str
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_ids: tuple[str, ...]
    affected_rows: int = Field(ge=0)
    ledger_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_at: datetime
    replayed: bool = False


class VisibilityProof(ProviderModel):
    schema_version: Literal["2"] = "2"
    batch_id: str
    value_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_id: str
    verified_at: datetime
