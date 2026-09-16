from __future__ import annotations

from pillarmesh_compiler.clickhouse_sql import emit_clickhouse
from pillarmesh_compiler.postgresql_sql import emit_postgresql
from pillarmesh_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)


def test_postgresql_and_clickhouse_preserve_semantics_with_dialect_specific_widening() -> None:
    product = _product()

    postgresql = emit_postgresql(product)
    clickhouse = emit_clickhouse(product)

    assert postgresql.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(SUM(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS '
        'NUMERIC(57,9)) AS "total_revenue" '
        'FROM "raw"."revenue_events" AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert clickhouse.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(sum(CAST("revenue_events"."revenue" AS Decimal(57, 9))) AS '
        'Decimal(57, 9)) AS "total_revenue" '
        'FROM "raw"."revenue_events" AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert postgresql.parameters == clickhouse.parameters == ()
    assert product.semantic_digest == _product().semantic_digest


def _product() -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="raw",
            relation_name="revenue_events",
            alias="revenue_events",
            columns=(
                ColumnDeclaration(name="region", value_type="string", nullable=False),
                ColumnDeclaration(name="revenue", value_type="decimal", nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(output_name="region", expression=region),
                    NamedExpression(output_name="revenue", expression=revenue),
                )
            ),
            AggregateOperation(
                group_by=(region,),
                measures=(
                    AggregateMeasure(function="sum", argument=revenue, output_name="total_revenue"),
                ),
            ),
        ),
        grain=(region,),
        freshness_seconds=3_600,
    )
