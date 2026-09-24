from __future__ import annotations

from heinzel_execution_graph import GenerationScopedProductSource
from heinzel_iir import LiteralValue, ProductIntentIR
from heinzel_iir.product_models import ScalarType

from .generation_sql import render_generation_scoped_select
from .sql_emitter import AggregateDialect, render_select
from .sql_models import SqlEmission, SqlParameter

_CLICKHOUSE_TYPES: dict[ScalarType, str] = {
    "boolean": "Bool",
    "integer": "Int64",
    "decimal": "Decimal(38, 9)",
    "string": "String",
    "timestamp": "DateTime64(6, 'UTC')",
}


def emit_clickhouse(product_iir: ProductIntentIR) -> SqlEmission:
    """Render syntax only; the unsigned result carries no admission or execution authority."""
    statement, parameters = render_select(
        product_iir,
        _bind_literal,
        aggregate_dialect=AggregateDialect(decimal_type="Decimal(57, 9)", sum_function="sum"),
    )
    return SqlEmission(engine="clickhouse", statement=statement, parameters=parameters)


def emit_generation_scoped_clickhouse(
    product_iir: ProductIntentIR,
    source: GenerationScopedProductSource,
) -> SqlEmission:
    """Render a restricted candidate over one immutable raw generation."""
    statement = render_generation_scoped_select(
        product_iir,
        source,
        engine="clickhouse",
        aggregate_dialect=AggregateDialect(decimal_type="Decimal(57, 9)", sum_function="sum"),
    )
    return SqlEmission(engine="clickhouse", statement=statement, parameters=())


def _bind_literal(literal: LiteralValue, index: int) -> tuple[str, SqlParameter]:
    name = f"p{index}"
    parameter = SqlParameter(name=name, value_type=literal.value_type, value=literal.value)
    return f"{{{name}:{_CLICKHOUSE_TYPES[literal.value_type]}}}", parameter
