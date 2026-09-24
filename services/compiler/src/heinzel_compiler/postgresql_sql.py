from __future__ import annotations

from heinzel_execution_graph import GenerationScopedProductSource
from heinzel_iir import LiteralValue, ProductIntentIR

from .generation_sql import render_generation_scoped_select
from .sql_emitter import AggregateDialect, render_select
from .sql_models import SqlEmission, SqlParameter


def emit_postgresql(product_iir: ProductIntentIR) -> SqlEmission:
    """Render syntax only; the unsigned result carries no admission or execution authority."""
    statement, parameters = render_select(
        product_iir,
        _bind_literal,
        aggregate_dialect=AggregateDialect(decimal_type="NUMERIC(57,9)", sum_function="SUM"),
    )
    return SqlEmission(engine="postgresql", statement=statement, parameters=parameters)


def emit_generation_scoped_postgresql(
    product_iir: ProductIntentIR,
    source: GenerationScopedProductSource,
) -> SqlEmission:
    """Render a restricted candidate over one immutable raw generation."""
    # The admitted statement must not depend on session state: every function is qualified to
    # pg_catalog so no search_path entry can shadow it.
    statement = render_generation_scoped_select(
        product_iir,
        source,
        engine="postgresql",
        aggregate_dialect=AggregateDialect(
            decimal_type="NUMERIC(57,9)", sum_function="pg_catalog.sum"
        ),
    )
    return SqlEmission(engine="postgresql", statement=statement, parameters=())


def _bind_literal(literal: LiteralValue, index: int) -> tuple[str, SqlParameter]:
    return "%s", SqlParameter(
        name=f"p{index}",
        value_type=literal.value_type,
        value=literal.value,
    )
