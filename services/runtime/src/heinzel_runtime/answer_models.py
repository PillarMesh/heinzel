from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Self

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk.errors import ProviderErrorClassification
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
type AnswerQueryValue = str | int | float | Decimal | bool | datetime | None
type AnswerQueryValueType = Literal["boolean", "decimal", "integer", "string", "timestamp"]
type AnswerExecutionOutcome = Literal[
    "succeeded",
    "ceiling_exceeded",
    "timed_out",
    "aborted",
    "generation_unavailable",
    "provider_failed",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AnswerQueryScan(_StrictModel):
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class AnswerQueryScanEstimate(AnswerQueryScan):
    estimator_version: str = Field(min_length=1)


class AnswerQueryReference(_StrictModel):
    artifact_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    digest: str = Field(pattern=_DIGEST_PATTERN)


class AnswerProductGenerationReference(_StrictModel):
    product_ref: AnswerQueryReference
    generation: int = Field(ge=1)


class AnswerQueryCeilings(_StrictModel):
    row_limit: int = Field(gt=0)
    scan: AnswerQueryScan
    period_scan: AnswerQueryScan


class AnswerQueryParameter(_StrictModel):
    name: str = Field(pattern=r"^p[0-9]+$")
    value_type: AnswerQueryValueType
    value: AnswerQueryValue


class AnswerQueryPlan(_StrictModel):
    schema_version: Literal["1"] = "1"
    plan_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=_DIGEST_PATTERN)
    engine_kind: Literal["postgresql", "clickhouse"]
    compiler_version: str = Field(min_length=1)
    allowlist_version: Literal["governed-query-v1"]
    consumption_object_refs: tuple[AnswerQueryReference, ...] = Field(min_length=1)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    minimum_group_size: int = Field(gt=0)
    statement: str = Field(min_length=1)
    parameters: tuple[AnswerQueryParameter, ...]
    statement_digest: str = Field(pattern=_DIGEST_PATTERN)
    parameter_digest: str = Field(pattern=_DIGEST_PATTERN)
    estimated_scan: AnswerQueryScanEstimate | None
    ceilings: AnswerQueryCeilings
    routing: Literal["policy_admitted", "per_question_review", "policy_revision_dependency"]
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def digests_and_suppression_match(self) -> Self:
        if self.statement_digest != digest(self.statement):
            raise ValueError("statement digest does not match statement")
        if self.parameter_digest != digest(self.parameters):
            raise ValueError("parameter digest does not match parameters")
        body = self.model_dump(mode="python", exclude={"plan_digest", "signature"})
        if self.plan_digest != digest(body):
            raise ValueError("plan digest does not match plan")
        if " HAVING COUNT(DISTINCT " not in self.statement:
            raise ValueError("query plan does not carry SQL-level suppression")
        return self


class AnswerExecutionAuthorization(_StrictModel):
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_revision: int = Field(ge=1)
    entitlement_digest: str = Field(pattern=_DIGEST_PATTERN)
    row_ceiling: int = Field(gt=0)
    byte_ceiling: int = Field(gt=0)
    statement_timeout_seconds: int = Field(gt=0)
    result_retention_seconds: int = Field(gt=0)
    freshness_observation_ref: str = Field(min_length=1)
    quality_observation_ref: str = Field(min_length=1)


class QueryGenerationState(_StrictModel):
    addressable: bool
    current_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def pointer_state_is_complete(self) -> Self:
        if not self.addressable and self.current_generation is None:
            raise ValueError("a publication pointer must name its current generation")
        return self


class AnswerQueryColumn(_StrictModel):
    name: str = Field(min_length=1)
    value_type: AnswerQueryValueType


class ReadOnlyAnswerQuery(_StrictModel):
    principal_class: Literal["answer_runtime"] = "answer_runtime"
    read_only: Literal[True] = True
    tenant_id: str = Field(min_length=1)
    engine_kind: Literal["postgresql", "clickhouse"]
    statement: str = Field(min_length=1)
    parameters: tuple[AnswerQueryParameter, ...]
    statement_timeout_seconds: int = Field(gt=0)
    row_ceiling: int = Field(gt=0)
    byte_ceiling: int = Field(gt=0)
    consumption_object_refs: tuple[AnswerQueryReference, ...] = Field(min_length=1)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)


class AnswerResultSnapshot(_StrictModel):
    schema_version: Literal["1"] = "1"
    result_ref: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    columns: tuple[AnswerQueryColumn, ...] = Field(min_length=1)
    rows: tuple[tuple[AnswerQueryValue, ...], ...]
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    result_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def contents_match_evidence(self) -> Self:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("result row width does not match schema")
        if self.row_count != len(self.rows):
            raise ValueError("result row count does not match rows")
        if self.byte_count != len(canonical_bytes(self.rows)):
            raise ValueError("result byte count does not match rows")
        if self.result_schema_digest != digest(self.columns):
            raise ValueError("result schema digest does not match schema")
        if self.result_digest != digest({"columns": self.columns, "rows": self.rows}):
            raise ValueError("result digest does not match result")
        if self.expires_at <= self.created_at:
            raise ValueError("result expiry must follow creation")
        return self

    @field_validator("created_at", "expires_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("result timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class AnswerExecutionReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    principal_class: Literal["answer_runtime"] = "answer_runtime"
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    attempt: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime
    outcome: AnswerExecutionOutcome
    provider_error_classification: ProviderErrorClassification | None = None
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    suppressed_group_count: int = Field(ge=0)
    result_schema_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    result_ref: str | None = None
    freshness_observation_ref: str = Field(min_length=1)
    quality_observation_ref: str = Field(min_length=1)

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("execution timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def outcome_fields_are_consistent(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("execution completion precedes its start")
        result_fields = (self.result_schema_digest, self.result_digest, self.result_ref)
        if self.outcome == "succeeded" and any(value is None for value in result_fields):
            raise ValueError("successful execution requires result evidence")
        if self.outcome != "succeeded" and any(value is not None for value in result_fields):
            raise ValueError("failed execution cannot reference a result")
        if self.outcome == "provider_failed" and self.provider_error_classification is None:
            raise ValueError("provider failure requires a classification")
        if self.outcome != "provider_failed" and self.provider_error_classification is not None:
            raise ValueError("provider classification is only valid for provider failure")
        return self
