from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pillarmesh_iir import (
    AggregateOperation,
    ApprovedFunctionCall,
    ArithmeticExpression,
    CaseExpression,
    ColumnReference,
    ComparisonExpression,
    FilterOperation,
    LiteralValue,
    ProductIntentIR,
    ProjectOperation,
    ScalarExpression,
)
from pillarmesh_iir.product_models import Identifier
from pydantic import TypeAdapter

from .product_semantics import validate_product_semantics
from .restricted_sql import require_project_sum_shape
from .sql_models import SqlParameter

type BindLiteral = Callable[[LiteralValue, int], tuple[str, SqlParameter]]


@dataclass(frozen=True, slots=True)
class AggregateDialect:
    decimal_type: str
    sum_function: str


_ARITHMETIC_OPERATORS = {
    "add": "+",
    "subtract": "-",
    "multiply": "*",
    "divide": "/",
}
_COMPARISON_OPERATORS = {
    "equal": "=",
    "not_equal": "<>",
    "greater_than": ">",
    "greater_than_or_equal": ">=",
    "less_than": "<",
    "less_than_or_equal": "<=",
}
_IDENTIFIER_ADAPTER: TypeAdapter[str] = TypeAdapter(Identifier)


def render_select(
    product_iir: ProductIntentIR,
    bind_literal: BindLiteral,
    *,
    aggregate_dialect: AggregateDialect,
) -> tuple[str, tuple[SqlParameter, ...]]:
    validate_product_semantics(product_iir)
    if (
        len(product_iir.operations) == 2
        and isinstance(product_iir.operations[0], ProjectOperation)
        and isinstance(product_iir.operations[1], AggregateOperation)
    ):
        return _render_project_sum(product_iir, aggregate_dialect=aggregate_dialect), ()
    project = tuple(
        operation for operation in product_iir.operations if isinstance(operation, ProjectOperation)
    )
    filters = tuple(
        operation for operation in product_iir.operations if isinstance(operation, FilterOperation)
    )
    if (
        len(project) != 1
        or len(filters) > 1
        or len(product_iir.operations) != len(project) + len(filters)
    ):
        raise ValueError("emitter supports exactly one project and at most one filter")

    parameters: list[SqlParameter] = []

    def render(expression: ScalarExpression) -> str:
        if isinstance(expression, ColumnReference):
            return f"{_quote(expression.relation_alias)}.{_quote(expression.column_name)}"
        if isinstance(expression, LiteralValue):
            placeholder, parameter = bind_literal(expression, len(parameters))
            parameters.append(parameter)
            return placeholder
        if isinstance(expression, ArithmeticExpression):
            return (
                f"({render(expression.left)} {_ARITHMETIC_OPERATORS[expression.operator]} "
                f"{render(expression.right)})"
            )
        if isinstance(expression, ComparisonExpression):
            return (
                f"({render(expression.left)} {_COMPARISON_OPERATORS[expression.operator]} "
                f"{render(expression.right)})"
            )
        if isinstance(expression, CaseExpression):
            branches = " ".join(
                f"WHEN {render(branch.when)} THEN {render(branch.then)}"
                for branch in expression.branches
            )
            return f"(CASE {branches} ELSE {render(expression.otherwise)} END)"
        if isinstance(expression, ApprovedFunctionCall):
            arguments = ", ".join(render(argument) for argument in expression.arguments)
            return f"{expression.function_name.upper()}({arguments})"
        raise TypeError("unsupported scalar expression")

    expressions = ", ".join(
        f"{render(named.expression)} AS {_quote(named.output_name)}"
        for named in project[0].expressions
    )
    source = product_iir.source
    statement = (
        f"SELECT {expressions} FROM {_quote(source.relation_namespace)}."
        f"{_quote(source.relation_name)} AS {_quote(source.alias)}"
    )
    if filters:
        statement = f"{statement} WHERE {render(filters[0].predicate)}"
    return statement, tuple(parameters)


def _render_project_sum(
    product_iir: ProductIntentIR,
    *,
    aggregate_dialect: AggregateDialect,
    source_expression: str | None = None,
) -> str:
    project, aggregate = require_project_sum_shape(product_iir)

    def render_column(reference: ColumnReference) -> str:
        return f"{_quote(reference.relation_alias)}.{_quote(reference.column_name)}"

    group_expressions = ", ".join(
        f"{render_column(reference)} AS {_quote(named.output_name)}"
        for reference, named in zip(
            aggregate.group_by,
            project.expressions[: len(aggregate.group_by)],
            strict=True,
        )
    )
    measure_expressions: list[str] = []
    for measure in aggregate.measures:
        if measure.function != "sum" or not isinstance(measure.argument, ColumnReference):
            raise ValueError("restricted sum emitter requires direct-column measures")
        argument = render_column(measure.argument)
        decimal_type = aggregate_dialect.decimal_type
        measure_expressions.append(
            f"CAST({aggregate_dialect.sum_function}(CAST({argument} AS {decimal_type})) AS "
            f"{decimal_type}) AS {_quote(measure.output_name)}"
        )
    expressions = ", ".join((group_expressions, *measure_expressions))
    source = product_iir.source
    source_relation = (
        f"{_quote(source.relation_namespace)}.{_quote(source.relation_name)}"
        if source_expression is None
        else source_expression
    )
    groups = ", ".join(render_column(reference) for reference in aggregate.group_by)
    return (
        f"SELECT {expressions} FROM {source_relation} AS {_quote(source.alias)} GROUP BY {groups}"
    )


def _quote(identifier: str) -> str:
    validated = _IDENTIFIER_ADAPTER.validate_python(identifier, strict=True)
    return f'"{validated}"'
