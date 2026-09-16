from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pillarmesh_contract_model import ArtifactModel, digest
from pydantic import ConfigDict, Field, field_validator, model_validator

type Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
type ScalarType = Literal["boolean", "integer", "decimal", "string", "timestamp"]
type LiteralScalar = bool | int | Decimal | str | datetime | None
type ApprovedFunctionName = Literal["coalesce", "lower", "upper"]


class ProductModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ColumnDeclaration(ProductModel):
    name: Identifier
    value_type: ScalarType
    nullable: bool


class SourceRelation(ProductModel):
    relation_namespace: Identifier
    relation_name: Identifier
    alias: Identifier
    columns: tuple[ColumnDeclaration, ...] = Field(min_length=1)

    @field_validator("columns")
    @classmethod
    def columns_are_unique(
        cls, value: tuple[ColumnDeclaration, ...]
    ) -> tuple[ColumnDeclaration, ...]:
        names = tuple(column.name for column in value)
        if len(names) != len(set(names)):
            raise ValueError("source column declarations must be unique")
        return value


class ColumnReference(ProductModel):
    kind: Literal["column"] = "column"
    relation_alias: Identifier
    column_name: Identifier


class LiteralValue(ProductModel):
    kind: Literal["literal"] = "literal"
    value_type: ScalarType
    value: LiteralScalar

    @model_validator(mode="before")
    @classmethod
    def decode_canonical_json_scalars(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        literal = value.get("value")
        if not isinstance(literal, str):
            return value
        normalized = dict(value)
        if value.get("value_type") == "decimal":
            normalized["value"] = Decimal(literal)
        elif value.get("value_type") == "timestamp":
            normalized["value"] = datetime.fromisoformat(literal.replace("Z", "+00:00"))
        else:
            return value
        return normalized

    @model_validator(mode="after")
    def value_matches_declared_type(self) -> Self:
        if self.value is None:
            return self
        matches = (
            (self.value_type == "boolean" and type(self.value) is bool)
            or (self.value_type == "integer" and type(self.value) is int)
            or (self.value_type == "decimal" and isinstance(self.value, Decimal))
            or (self.value_type == "string" and isinstance(self.value, str))
            or (self.value_type == "timestamp" and isinstance(self.value, datetime))
        )
        if not matches:
            raise ValueError("literal value does not match its declared scalar type")
        if isinstance(self.value, datetime):
            if self.value.tzinfo is None or self.value.utcoffset() is None:
                raise ValueError("timestamp literals must be timezone-aware")
            object.__setattr__(self, "value", self.value.astimezone(UTC))
        return self


class ArithmeticExpression(ProductModel):
    kind: Literal["arithmetic"] = "arithmetic"
    operator: Literal["add", "subtract", "multiply", "divide"]
    left: ScalarExpression
    right: ScalarExpression


class ComparisonExpression(ProductModel):
    kind: Literal["comparison"] = "comparison"
    operator: Literal[
        "equal",
        "not_equal",
        "greater_than",
        "greater_than_or_equal",
        "less_than",
        "less_than_or_equal",
    ]
    left: ScalarExpression
    right: ScalarExpression


class CaseBranch(ProductModel):
    when: ScalarExpression
    then: ScalarExpression


class CaseExpression(ProductModel):
    kind: Literal["case"] = "case"
    branches: tuple[CaseBranch, ...] = Field(min_length=1)
    otherwise: ScalarExpression


class ApprovedFunctionCall(ProductModel):
    kind: Literal["function"] = "function"
    function_name: ApprovedFunctionName
    arguments: tuple[ScalarExpression, ...] = Field(min_length=1)


type ScalarExpression = Annotated[
    ColumnReference
    | LiteralValue
    | ArithmeticExpression
    | ComparisonExpression
    | CaseExpression
    | ApprovedFunctionCall,
    Field(discriminator="kind"),
]


class NamedExpression(ProductModel):
    output_name: Identifier
    expression: ScalarExpression


class ProjectOperation(ProductModel):
    kind: Literal["project"] = "project"
    expressions: tuple[NamedExpression, ...] = Field(min_length=1)

    @field_validator("expressions")
    @classmethod
    def output_names_are_unique(
        cls, value: tuple[NamedExpression, ...]
    ) -> tuple[NamedExpression, ...]:
        names = tuple(expression.output_name for expression in value)
        if len(names) != len(set(names)):
            raise ValueError("project output names must be unique")
        return value


class FilterOperation(ProductModel):
    kind: Literal["filter"] = "filter"
    predicate: ScalarExpression


class JoinKeyPair(ProductModel):
    left: ColumnReference
    right: ColumnReference


class JoinOperation(ProductModel):
    kind: Literal["join"] = "join"
    right: SourceRelation
    join_type: Literal["inner", "left"]
    cardinality: Literal["one_to_one", "many_to_one"]
    key_pairs: tuple[JoinKeyPair, ...] = Field(min_length=1)


class AggregateMeasure(ProductModel):
    function: Literal["sum"]
    argument: ScalarExpression
    output_name: Identifier


class AggregateOperation(ProductModel):
    kind: Literal["aggregate"] = "aggregate"
    group_by: tuple[ColumnReference, ...]
    measures: tuple[AggregateMeasure, ...] = Field(min_length=1)


class SortKey(ProductModel):
    column: ColumnReference
    direction: Literal["ascending", "descending"]


class DeduplicateOperation(ProductModel):
    kind: Literal["deduplicate"] = "deduplicate"
    keys: tuple[ColumnReference, ...] = Field(min_length=1)
    order_by: tuple[SortKey, ...] = Field(min_length=1)


type RelationalOperation = Annotated[
    ProjectOperation | FilterOperation | JoinOperation | AggregateOperation | DeduplicateOperation,
    Field(discriminator="kind"),
]


class ProductIntentIR(ProductModel):
    schema_version: Literal["2"] = "2"
    product_ref: Identifier
    source: SourceRelation
    operations: tuple[RelationalOperation, ...] = Field(min_length=1)
    grain: tuple[ColumnReference, ...] = Field(min_length=1)
    freshness_seconds: int = Field(gt=0)

    @property
    def semantic_digest(self) -> str:
        return digest(self)


ArithmeticExpression.model_rebuild()
ComparisonExpression.model_rebuild()
CaseBranch.model_rebuild()
CaseExpression.model_rebuild()
ApprovedFunctionCall.model_rebuild()
