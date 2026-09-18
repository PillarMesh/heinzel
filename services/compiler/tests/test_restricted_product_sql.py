from __future__ import annotations

import pytest
from heinzel_compiler import NoValidPlan, compile_product_iir
from heinzel_compiler.clickhouse_sql import emit_clickhouse
from heinzel_compiler.postgresql_sql import emit_postgresql
from heinzel_iir import (
    ColumnDeclaration,
    ColumnReference,
    ComparisonExpression,
    FilterOperation,
    LiteralValue,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from pydantic import ValidationError


def test_undeclared_column_returns_no_valid_plan_with_exact_constraint() -> None:
    product_iir = _product_iir(column_name="missing_column")

    result = compile_product_iir(product_iir, engine="postgresql")

    assert isinstance(result, NoValidPlan)
    assert result.execution_occurred is False
    assert result.preconditions[0].reason == ("undeclared column reference: orders.missing_column")


def test_valid_but_unapproved_product_sql_returns_no_valid_plan() -> None:
    result = compile_product_iir(_product_iir(), engine="postgresql")

    assert isinstance(result, NoValidPlan)
    assert result.rule_id == "PRODUCT-SQL-V2-UNAPPROVED"
    assert result.preconditions[0].reason == "restricted product SQL has no approved legality rule"
    assert result.preconditions[0].number == 1
    assert result.smallest_changes == (
        "approve a legality rule with proof, fixtures, mutation tests, and independent review",
    )
    assert result.execution_occurred is False


def test_postgresql_emitter_quotes_identifiers_and_binds_hostile_literal() -> None:
    hostile_literal = "paid' OR TRUE; -- /* % _ *"

    emitted = emit_postgresql(_product_iir(literal=hostile_literal))

    assert emitted.statement == (
        'SELECT "orders"."order_id" AS "order_id" FROM "raw"."orders" AS "orders" '
        'WHERE ("orders"."status" = %s)'
    )
    assert hostile_literal not in emitted.statement
    assert tuple(parameter.value for parameter in emitted.parameters) == (hostile_literal,)


def test_clickhouse_emitter_uses_typed_bound_parameters() -> None:
    hostile_literal = "paid' OR TRUE; -- /* % _ *"

    emitted = emit_clickhouse(_product_iir(literal=hostile_literal))

    assert emitted.statement == (
        'SELECT "orders"."order_id" AS "order_id" FROM "raw"."orders" AS "orders" '
        'WHERE ("orders"."status" = {p0:String})'
    )
    assert hostile_literal not in emitted.statement
    assert emitted.parameters[0].name == "p0"
    assert emitted.parameters[0].value == hostile_literal


def test_emitter_revalidates_identifiers_at_its_trust_boundary() -> None:
    product_iir = _product_iir()
    unsafe_source = product_iir.source.model_copy(
        update={"relation_name": 'orders"; DROP TABLE orders;--'}
    )
    bypassed_model = product_iir.model_copy(update={"source": unsafe_source})

    with pytest.raises(ValidationError):
        emit_postgresql(bypassed_model)


def _product_iir(
    *,
    column_name: str = "order_id",
    literal: str = "paid",
) -> ProductIntentIR:
    source = SourceRelation(
        relation_namespace="raw",
        relation_name="orders",
        alias="orders",
        columns=(
            ColumnDeclaration(name="order_id", value_type="integer", nullable=False),
            ColumnDeclaration(name="status", value_type="string", nullable=False),
            ColumnDeclaration(name="amount", value_type="decimal", nullable=False),
        ),
    )
    return ProductIntentIR(
        product_ref="product_orders_v1",
        source=source,
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(
                        output_name="order_id",
                        expression=ColumnReference(
                            relation_alias="orders",
                            column_name=column_name,
                        ),
                    ),
                )
            ),
            FilterOperation(
                predicate=ComparisonExpression(
                    operator="equal",
                    left=ColumnReference(relation_alias="orders", column_name="status"),
                    right=LiteralValue(value_type="string", value=literal),
                )
            ),
        ),
        grain=(ColumnReference(relation_alias="orders", column_name="order_id"),),
        freshness_seconds=3_600,
    )
