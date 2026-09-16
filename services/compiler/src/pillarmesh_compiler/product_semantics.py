from __future__ import annotations

from dataclasses import dataclass

from pillarmesh_iir import (
    AggregateOperation,
    ApprovedFunctionCall,
    ArithmeticExpression,
    CaseExpression,
    ColumnReference,
    ComparisonExpression,
    DeduplicateOperation,
    FilterOperation,
    JoinOperation,
    LiteralValue,
    ProductIntentIR,
    ProjectOperation,
    ScalarExpression,
    SourceRelation,
)
from pillarmesh_iir.product_models import ScalarType


@dataclass(frozen=True)
class ProductSemanticError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def validate_product_semantics(product_iir: ProductIntentIR) -> None:
    relations = {product_iir.source.alias: product_iir.source}
    for operation in product_iir.operations:
        if isinstance(operation, JoinOperation):
            if operation.right.alias in relations:
                raise ProductSemanticError(f"duplicate relation alias: {operation.right.alias}")
            _validate_join(operation, relations)
            relations[operation.right.alias] = operation.right
        elif isinstance(operation, ProjectOperation):
            for named_expression in operation.expressions:
                _expression_type(named_expression.expression, relations)
        elif isinstance(operation, FilterOperation):
            if _expression_type(operation.predicate, relations) != "boolean":
                raise ProductSemanticError("filter predicate must have boolean type")
        elif isinstance(operation, AggregateOperation):
            for column in operation.group_by:
                _column_type(column, relations)
            for measure in operation.measures:
                argument_type = _expression_type(measure.argument, relations)
                if measure.function == "sum" and argument_type not in {"integer", "decimal"}:
                    raise ProductSemanticError("sum argument must have numeric type")
        elif isinstance(operation, DeduplicateOperation):
            for column in operation.keys:
                _column_type(column, relations)
            for sort_key in operation.order_by:
                _column_type(sort_key.column, relations)
    for grain_column in product_iir.grain:
        _column_type(grain_column, relations)


def _validate_join(
    operation: JoinOperation,
    relations: dict[str, SourceRelation],
) -> None:
    available = {**relations, operation.right.alias: operation.right}
    for key_pair in operation.key_pairs:
        left_type = _column_type(key_pair.left, available)
        right_type = _column_type(key_pair.right, available)
        if key_pair.left.relation_alias not in relations:
            raise ProductSemanticError("join left key must reference an existing relation")
        if key_pair.right.relation_alias != operation.right.alias:
            raise ProductSemanticError("join right key must reference the joined relation")
        if left_type != right_type:
            raise ProductSemanticError("join key types must match")


def _column_type(
    reference: ColumnReference,
    relations: dict[str, SourceRelation],
) -> ScalarType:
    relation = relations.get(reference.relation_alias)
    if relation is None:
        raise ProductSemanticError(f"undeclared relation alias: {reference.relation_alias}")
    for column in relation.columns:
        if column.name == reference.column_name:
            return column.value_type
    raise ProductSemanticError(
        f"undeclared column reference: {reference.relation_alias}.{reference.column_name}"
    )


def _expression_type(
    expression: ScalarExpression,
    relations: dict[str, SourceRelation],
) -> ScalarType:
    if isinstance(expression, ColumnReference):
        return _column_type(expression, relations)
    if isinstance(expression, LiteralValue):
        return expression.value_type
    if isinstance(expression, ComparisonExpression):
        left_type = _expression_type(expression.left, relations)
        right_type = _expression_type(expression.right, relations)
        if left_type != right_type:
            raise ProductSemanticError("comparison operand types must match")
        return "boolean"
    if isinstance(expression, ArithmeticExpression):
        left_type = _expression_type(expression.left, relations)
        right_type = _expression_type(expression.right, relations)
        if left_type not in {"integer", "decimal"} or right_type not in {
            "integer",
            "decimal",
        }:
            raise ProductSemanticError("arithmetic operands must be numeric")
        return "decimal" if "decimal" in {left_type, right_type} else "integer"
    if isinstance(expression, CaseExpression):
        result_type = _expression_type(expression.otherwise, relations)
        for branch in expression.branches:
            if _expression_type(branch.when, relations) != "boolean":
                raise ProductSemanticError("case conditions must have boolean type")
            if _expression_type(branch.then, relations) != result_type:
                raise ProductSemanticError("case result types must match")
        return result_type
    if isinstance(expression, ApprovedFunctionCall):
        argument_types = tuple(
            _expression_type(argument, relations) for argument in expression.arguments
        )
        if expression.function_name in {"lower", "upper"}:
            if argument_types != ("string",):
                raise ProductSemanticError(
                    f"{expression.function_name} requires exactly one string argument"
                )
            return "string"
        if len(set(argument_types)) != 1:
            raise ProductSemanticError("coalesce argument types must match")
        return argument_types[0]
    raise ProductSemanticError("unsupported scalar expression")
