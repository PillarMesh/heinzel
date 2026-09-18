from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel, digest
from heinzel_iir.product_models import Identifier, LiteralScalar, ScalarType
from pydantic import ConfigDict, Field, model_validator

from .sql_models import SqlParameter

type QueryEngine = Literal["postgresql", "clickhouse"]
type QueryRouting = Literal["policy_admitted", "per_question_review", "policy_revision_dependency"]


class _StrictQueryModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QueryScan(_StrictQueryModel):
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class QueryScanEstimate(QueryScan):
    estimator_version: str = Field(min_length=1)


class QueryReference(_StrictQueryModel):
    artifact_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductGenerationReference(_StrictQueryModel):
    product_ref: QueryReference
    generation: int = Field(ge=1)


class QueryCeilings(_StrictQueryModel):
    row_limit: int = Field(gt=0)
    scan: QueryScan
    period_scan: QueryScan


class QueryConsumptionObject(_StrictQueryModel):
    object_ref: QueryReference
    namespace: Identifier
    relation_name: Identifier


class QueryMetric(_StrictQueryModel):
    metric_ref: QueryReference
    aggregate: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_name: Identifier
    output_name: Identifier


class QueryDimension(_StrictQueryModel):
    dimension_ref: QueryReference
    column_name: Identifier
    output_name: Identifier


class QueryFilter(_StrictQueryModel):
    filter_ref: QueryReference
    column_name: Identifier
    operator: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value_type: ScalarType
    value: LiteralScalar

    @model_validator(mode="after")
    def value_matches_declared_type(self) -> Self:
        from heinzel_iir import LiteralValue

        literal = LiteralValue(value_type=self.value_type, value=self.value)
        if literal.value != self.value:
            object.__setattr__(self, "value", literal.value)
        return self


class QueryTimeWindow(_StrictQueryModel):
    time_dimension_ref: QueryReference
    column_name: Identifier
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def is_bounded_utc_window(self) -> Self:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("time window start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("time window end must be timezone-aware")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        if end <= start:
            raise ValueError("time window end must follow its start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        return self


class QueryOrder(_StrictQueryModel):
    output_name: Identifier
    direction: Literal["ascending", "descending"]


class GovernedQueryInput(_StrictQueryModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_outcome: Literal["admitted", "review_required"] = "admitted"
    intent_kind: Literal["definition", "metric_value"]
    engine_kind: QueryEngine
    compiler_version: str = "1"
    allowlist_version: Literal["governed-query-v1"] = "governed-query-v1"
    consumption_object: QueryConsumptionObject
    product_generation_refs: tuple[ProductGenerationReference, ...] = Field(min_length=1)
    metrics: tuple[QueryMetric, ...]
    dimensions: tuple[QueryDimension, ...]
    filters: tuple[QueryFilter, ...]
    time_window: QueryTimeWindow | None
    ordering: tuple[QueryOrder, ...]
    row_limit: int = Field(gt=0)
    disclosure_entity_column: Identifier
    minimum_group_size: int = Field(gt=0)
    estimated_scan: QueryScanEstimate | None
    period_scan_consumed: QueryScan
    ceilings: QueryCeilings
    statement_ceiling_breached: bool = False


class GovernedQueryPlanNotRequired(_StrictQueryModel):
    result: Literal["not_applicable_for_definition"] = "not_applicable_for_definition"
    tenant_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class QueryEstimateRequest(_StrictQueryModel):
    engine_kind: QueryEngine
    statement: str = Field(min_length=1)
    parameters: tuple[SqlParameter, ...]
    product_generation_refs: tuple[ProductGenerationReference, ...] = Field(min_length=1)


class GovernedQueryPlan(_StrictQueryModel):
    schema_version: Literal["1"] = "1"
    plan_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_kind: QueryEngine
    compiler_version: str = Field(min_length=1)
    allowlist_version: Literal["governed-query-v1"]
    consumption_object_refs: tuple[QueryReference, ...] = Field(min_length=1)
    product_generation_refs: tuple[ProductGenerationReference, ...] = Field(min_length=1)
    minimum_group_size: int = Field(gt=0)
    statement: str = Field(min_length=1)
    parameters: tuple[SqlParameter, ...]
    statement_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_scan: QueryScanEstimate | None
    ceilings: QueryCeilings
    routing: QueryRouting
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def digests_match_contents(self) -> Self:
        if self.statement_digest != digest(self.statement):
            raise ValueError("statement digest does not match statement")
        if self.parameter_digest != digest(self.parameters):
            raise ValueError("parameter digest does not match parameters")
        body = self.model_dump(mode="python", exclude={"plan_digest", "signature"})
        if self.plan_digest != digest(body):
            raise ValueError("plan digest does not match plan")
        return self
