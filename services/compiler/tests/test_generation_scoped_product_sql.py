from __future__ import annotations

import pytest
from pillarmesh_compiler.clickhouse_sql import emit_generation_scoped_clickhouse
from pillarmesh_compiler.postgresql_sql import emit_generation_scoped_postgresql
from pillarmesh_execution_graph import GenerationScopedProductSource, ProductJsonFieldBinding
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
from pydantic import ValidationError


def _product() -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="logical",
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
        freshness_seconds=3600,
    )


def _source() -> GenerationScopedProductSource:
    return GenerationScopedProductSource(
        namespace="raw",
        relation_name="raw_sales",
        generation_column="generation_id",
        payload_column="payload",
        generation_id="a" * 64,
        landing_receipt_digest="b" * 64,
        observed_source_schema_digest="c" * 64,
        field_bindings=(
            ProductJsonFieldBinding(
                logical_field="region", json_field="region", scalar_type="string"
            ),
            ProductJsonFieldBinding(
                logical_field="revenue", json_field="revenue", scalar_type="decimal"
            ),
        ),
    )


def test_postgresql_emits_one_generation_scoped_json_source() -> None:
    emitted = emit_generation_scoped_postgresql(_product(), _source())

    assert emitted.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(SUM(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS '
        'NUMERIC(57,9)) AS "total_revenue" FROM '
        '(SELECT "payload" ->> \'region\' AS "region", '
        'CAST("payload" ->> \'revenue\' AS NUMERIC(38,9)) AS "revenue" '
        'FROM "raw"."raw_sales" WHERE "generation_id" = \'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\') AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert emitted.parameters == ()


def test_clickhouse_emits_one_generation_scoped_json_source() -> None:
    emitted = emit_generation_scoped_clickhouse(_product(), _source())

    assert emitted.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(sum(CAST("revenue_events"."revenue" AS Decimal(57, 9))) AS '
        'Decimal(57, 9)) AS "total_revenue" FROM '
        '(SELECT JSONExtractString("payload", \'region\') AS "region", '
        'CAST(JSONExtractString("payload", \'revenue\') AS Decimal(38, 9)) AS "revenue" '
        'FROM "raw"."raw_sales" WHERE "generation_id" = \'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\') AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert emitted.parameters == ()


@pytest.mark.parametrize(
    "source",
    (
        _source().model_copy(update={"field_bindings": (_source().field_bindings[0],)}),
        _source().model_copy(
            update={
                "field_bindings": (
                    _source().field_bindings[0],
                    _source().field_bindings[1].model_copy(update={"scalar_type": "string"}),
                )
            }
        ),
        _source().model_copy(update={"undeclared": "hidden"}),
    ),
)
def test_generation_emission_rejects_incomplete_or_mutated_source_authority(
    source: GenerationScopedProductSource,
) -> None:
    with pytest.raises(ValueError, match="generation-scoped source"):
        emit_generation_scoped_postgresql(_product(), source)


def test_generation_emission_rejects_coercible_generation_identity() -> None:
    source = _source().model_copy(update={"generation_id": b"a" * 64})

    with pytest.raises(ValueError, match="generation-scoped source authority"):
        emit_generation_scoped_postgresql(_product(), source)


def test_generation_source_rejects_hostile_physical_and_json_identifiers() -> None:
    for update in (
        {"relation_name": 'raw_sales"; DROP TABLE raw_sales;--'},
        {
            "field_bindings": (
                ProductJsonFieldBinding(
                    logical_field="region", json_field="region", scalar_type="string"
                ),
                {"logical_field": "revenue", "json_field": "revenue' OR '1'='1"},
            )
        },
    ):
        with pytest.raises(ValidationError):
            GenerationScopedProductSource(**(_source().model_dump() | update))
