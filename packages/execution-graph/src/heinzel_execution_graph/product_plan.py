from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from heinzel_contract_model import ArtifactModel, digest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

type ProductIdentifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
type ProductScalarType = Literal["boolean", "integer", "decimal", "string", "timestamp"]
type ProductProvider = Literal["postgresql", "clickhouse"]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class InvalidProductPhysicalPlan(RuntimeError):
    pass


class _ProductPlanModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProductJsonFieldBinding(_ProductPlanModel):
    logical_field: ProductIdentifier
    json_field: ProductIdentifier
    scalar_type: ProductScalarType


class GenerationScopedProductSource(_ProductPlanModel):
    namespace: ProductIdentifier
    relation_name: ProductIdentifier
    generation_column: ProductIdentifier
    payload_column: ProductIdentifier
    generation_id: str = Field(pattern=_DIGEST_PATTERN)
    landing_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_source_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    field_bindings: tuple[ProductJsonFieldBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def field_binding_names_are_unique(self) -> Self:
        if self.generation_column == self.payload_column:
            raise ValueError("product source generation and payload columns must be distinct")
        logical_fields = tuple(binding.logical_field for binding in self.field_bindings)
        json_fields = tuple(binding.json_field for binding in self.field_bindings)
        if len(logical_fields) != len(set(logical_fields)) or len(json_fields) != len(
            set(json_fields)
        ):
            raise ValueError("product source field bindings must be unique")
        return self


class ProductTarget(_ProductPlanModel):
    namespace: ProductIdentifier
    relation_name: ProductIdentifier


class Decimal57OutputCheck(_ProductPlanModel):
    column_name: ProductIdentifier
    precision: Literal[57] = 57
    scale: Literal[9] = 9
    nullable: Literal[False] = False


class ProductPhysicalPlan(_ProductPlanModel):
    schema_version: Literal["1"] = "1"
    compiler_version: str = Field(min_length=1)
    legality_rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9-]+$")
    legality_rule_version: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    contract_ref: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    iir_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider: ProductProvider
    warehouse_binding_id: str = Field(min_length=1)
    warehouse_binding_revision: int = Field(ge=1)
    provider_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    source: GenerationScopedProductSource
    target: ProductTarget
    emitted_statement: str = Field(min_length=1)
    statement_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_columns: tuple[ProductIdentifier, ...] = Field(min_length=1)
    expected_output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    decimal_output_checks: tuple[Decimal57OutputCheck, ...] = Field(min_length=1)

    @field_validator("compiler_version", "legality_rule_version", "tenant_id", "product_id")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product physical plan text fields cannot be blank")
        return value

    @field_validator("contract_ref", "warehouse_binding_id", "emitted_statement")
    @classmethod
    def bound_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product physical plan bound text cannot be blank")
        return value

    @model_validator(mode="after")
    def statement_and_output_checks_are_bound(self) -> Self:
        if self.statement_digest != digest(self.emitted_statement):
            raise ValueError("product physical plan statement digest does not match")
        check_names = tuple(check.column_name for check in self.decimal_output_checks)
        if len(self.output_columns) != len(set(self.output_columns)):
            raise ValueError("product physical plan output columns must be unique")
        if len(check_names) != len(set(check_names)):
            raise ValueError("product physical plan output checks must be unique")
        if any(check_name not in self.output_columns for check_name in check_names):
            raise ValueError("product physical plan output check requires an output column")
        return self


def revalidate_product_physical_plan(plan: ProductPhysicalPlan) -> ProductPhysicalPlan:
    try:
        _require_declared_product_plan_tree(plan)
        payload = plan.model_dump(mode="python")
        return ProductPhysicalPlan.model_validate(payload, strict=True)
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise InvalidProductPhysicalPlan("product physical plan is invalid") from error


def _utc_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class ProductExecutionAuthorization(_ProductPlanModel):
    schema_version: Literal["1"] = "1"
    physical_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    legality_decision_digest: str = Field(pattern=_DIGEST_PATTERN)
    cardinality_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc_timestamp(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("product execution authorization expiry must follow issue time")
        return self


class SignedProductExecutionAuthorization(_ProductPlanModel):
    schema_version: Literal["1"] = "1"
    authorization: ProductExecutionAuthorization
    authorization_digest: str = Field(pattern=_DIGEST_PATTERN)
    key_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def digest_matches_authorization(self) -> Self:
        if self.authorization_digest != digest(self.authorization):
            raise ValueError("product execution authorization digest does not match")
        return self


def _require_only_declared_fields(value: object, model_type: type[BaseModel]) -> None:
    if type(value) is not model_type:
        raise TypeError("artifact has an unexpected model type")
    undeclared_fields = set(vars(value)) - set(model_type.model_fields)
    pydantic_extra = getattr(value, "__pydantic_extra__", None)
    if undeclared_fields or pydantic_extra:
        raise ValueError("artifact contains undeclared fields")


def _require_declared_product_plan_tree(plan: ProductPhysicalPlan) -> None:
    _require_only_declared_fields(plan, ProductPhysicalPlan)
    _require_only_declared_fields(plan.source, GenerationScopedProductSource)
    if type(plan.source.field_bindings) is not tuple:
        raise TypeError("product source field bindings must be an immutable tuple")
    for binding in plan.source.field_bindings:
        _require_only_declared_fields(binding, ProductJsonFieldBinding)
    _require_only_declared_fields(plan.target, ProductTarget)
    if type(plan.decimal_output_checks) is not tuple:
        raise TypeError("product output checks must be an immutable tuple")
    for check in plan.decimal_output_checks:
        _require_only_declared_fields(check, Decimal57OutputCheck)
