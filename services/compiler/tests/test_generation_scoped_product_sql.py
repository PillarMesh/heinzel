from __future__ import annotations

import re

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
        'CAST(pg_catalog.sum(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS '
        'NUMERIC(57,9)) AS "total_revenue" FROM (SELECT (CASE WHEN '
        "pg_catalog.jsonb_typeof(\"payload\" OPERATOR(pg_catalog.->) 'region') "
        "OPERATOR(pg_catalog.=) 'string' THEN \"payload\" OPERATOR(pg_catalog.->>) 'region' "
        "ELSE CAST(CAST('pillarmesh refused a string landing value in generation ' "
        'OPERATOR(pg_catalog.||) "generation_id" AS NUMERIC) AS pg_catalog.text) END) COLLATE '
        'pg_catalog."C" AS "region", CAST(CASE WHEN pg_catalog.jsonb_typeof("payload" '
        "OPERATOR(pg_catalog.->) 'revenue') OPERATOR(pg_catalog.=) 'string' AND \"payload\" "
        "OPERATOR(pg_catalog.->>) 'revenue' OPERATOR(pg_catalog.~) "
        "'^-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?$' THEN \"payload\" OPERATOR(pg_catalog.->>) "
        "'revenue' ELSE 'pillarmesh refused a decimal landing value in generation ' "
        'OPERATOR(pg_catalog.||) "generation_id" END AS NUMERIC(38,9)) AS "revenue" FROM '
        '"raw"."raw_sales" WHERE "generation_id" OPERATOR(pg_catalog.=) '
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') AS "
        '"revenue_events" GROUP BY "revenue_events"."region"'
    )
    assert emitted.parameters == ()


def test_postgresql_generation_statement_resolves_nothing_through_search_path() -> None:
    """Every function, operator, type and collation in the admitted statement is pg_catalog.

    An unqualified name resolves through the session search_path, so a hostile schema listed before
    pg_catalog could replace jsonb_typeof or `~` and defeat the decode guard (verified live). This
    test fails if any of those names appears unqualified.
    """
    statement = emit_generation_scoped_postgresql(_product(), _source()).statement

    for unqualified in (
        " jsonb_typeof(",
        "(jsonb_typeof(",
        " ->> ",
        " -> ",
        " ~ ",
        " || ",
        " = ",
        "SUM(",
        " AS TEXT)",
        'COLLATE "C"',
    ):
        assert unqualified not in statement, unqualified
    for qualified in (
        "pg_catalog.jsonb_typeof(",
        "OPERATOR(pg_catalog.->>)",
        "OPERATOR(pg_catalog.->)",
        "OPERATOR(pg_catalog.~)",
        "OPERATOR(pg_catalog.||)",
        "OPERATOR(pg_catalog.=)",
        "pg_catalog.sum(",
        "AS pg_catalog.text)",
        'COLLATE pg_catalog."C"',
    ):
        assert qualified in statement, qualified


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


@pytest.mark.parametrize(
    "value",
    (
        "0",
        "-0",
        "7",
        "-42.125",
        "0.123456789",
        "99999999999999999999999999999.999999999",
        "-99999999999999999999999999999.999999999",
    ),
)
def test_postgresql_canonical_decimal_pattern_admits_exact_decimal38_9_literals(value: str) -> None:
    from pillarmesh_compiler.generation_sql import _POSTGRESQL_CANONICAL_DECIMAL

    assert re.fullmatch(_POSTGRESQL_CANONICAL_DECIMAL, value) is not None


@pytest.mark.parametrize(
    "value",
    (
        "NaN",
        "Infinity",
        "-Infinity",
        "0x10",
        "1_000",
        "1e3",
        "1E+28",
        " 7",
        "7 ",
        "+5",
        "007",
        ".5",
        "5.",
        "-",
        "",
        "1.1234567895",
        "100000000000000000000000000000",
        "\uff11",
        "1\n",
    ),
)
def test_postgresql_canonical_decimal_pattern_refuses_every_permissive_numeric_form(
    value: str,
) -> None:
    """PostgreSQL's numeric input accepts each of these; the statement must refuse them first.

    Live against the pinned image the guarded statement refuses every one with SQLSTATE 22P02,
    recorded in the checked SUM evidence. This test pins the pattern itself.
    """
    from pillarmesh_compiler.generation_sql import _POSTGRESQL_CANONICAL_DECIMAL

    assert re.fullmatch(_POSTGRESQL_CANONICAL_DECIMAL, value) is None


@pytest.mark.parametrize(
    "value",
    (
        "0",
        "0E-9",
        "-0.00",
        "10.00",
        "1E+3",
        "0.000000001",
        "-42.125",
        "99999999999999999999999999999.999999999",
        "-99999999999999999999999999999.999999999",
    ),
)
def test_every_in_range_decimal_the_landing_encoder_writes_passes_the_decode_guard(
    value: str,
) -> None:
    """The landing encoder and the decode guard are two halves of one contract.

    Acquired rows are landed as canonical JSON, which encodes a Decimal with format(value, "f").
    If that encoder could ever emit exponent notation or another form the guard refuses, every
    genuinely landed row with that value would refuse the product statement.
    """
    from decimal import Decimal

    from pillarmesh_compiler.generation_sql import _POSTGRESQL_CANONICAL_DECIMAL
    from pillarmesh_contract_model import canonical_value

    encoded = canonical_value({"revenue": Decimal(value)})
    assert isinstance(encoded, dict)
    landed = encoded["revenue"]

    assert isinstance(landed, str)
    assert re.fullmatch(_POSTGRESQL_CANONICAL_DECIMAL, landed) is not None, landed


@pytest.mark.parametrize(
    "value",
    ("NaN", "Infinity", "-Infinity", "0.0000000001", "100000000000000000000000000000"),
)
def test_the_decode_guard_refuses_what_the_landing_encoder_writes_for_inadmissible_decimals(
    value: str,
) -> None:
    from decimal import Decimal

    from pillarmesh_compiler.generation_sql import _POSTGRESQL_CANONICAL_DECIMAL
    from pillarmesh_contract_model import canonical_value

    encoded = canonical_value({"revenue": Decimal(value)})
    assert isinstance(encoded, dict)

    assert re.fullmatch(_POSTGRESQL_CANONICAL_DECIMAL, str(encoded["revenue"])) is None
