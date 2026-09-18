from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_iir import (
    AggregateMeasure,
    AggregateOperation,
    ApprovedFunctionCall,
    ColumnDeclaration,
    ColumnReference,
    JoinOperation,
    LiteralValue,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    "unsafe_identifier",
    (
        'orders"; DROP TABLE raw.orders;--',
        "orders/*comment*/",
        "orders--comment",
        "orders*",
        "orders%",
        "\u043erders",
    ),
)
@pytest.mark.parametrize(
    "factory",
    (
        lambda value: SourceRelation(
            relation_namespace="raw", relation_name=value, alias="orders", columns=(_column(),)
        ),
        lambda value: SourceRelation(
            relation_namespace="raw", relation_name="orders", alias=value, columns=(_column(),)
        ),
        lambda value: ColumnDeclaration(name=value, value_type="integer", nullable=False),
        lambda value: ColumnReference(relation_alias="orders", column_name=value),
        lambda value: NamedExpression(
            output_name=value,
            expression=ColumnReference(relation_alias="orders", column_name="order_id"),
        ),
    ),
)
def test_every_identifier_boundary_rejects_sql_tokens_and_unicode_confusables(
    unsafe_identifier: str,
    factory: Callable[[str], object],
) -> None:
    with pytest.raises(ValidationError):
        factory(unsafe_identifier)


@pytest.mark.parametrize("function_name", ("random", "now", "uuid", "unknown_function"))
def test_unknown_and_nondeterministic_functions_are_not_in_the_closed_ast(
    function_name: str,
) -> None:
    with pytest.raises(ValidationError):
        ApprovedFunctionCall.model_validate(
            {
                "function_name": function_name,
                "arguments": (ColumnReference(relation_alias="orders", column_name="status"),),
            }
        )


def test_join_requires_at_least_one_declared_key_pair() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        JoinOperation(
            right=SourceRelation(
                relation_namespace="raw",
                relation_name="customers",
                alias="customers",
                columns=(
                    ColumnDeclaration(name="customer_id", value_type="string", nullable=False),
                ),
            ),
            join_type="inner",
            cardinality="many_to_one",
            key_pairs=(),
        )


def test_product_iir_has_stable_canonical_bytes_and_digest() -> None:
    left = _product_iir()
    right = ProductIntentIR.model_validate_json(left.model_dump_json())

    assert left.schema_version == "2"
    assert canonical_bytes(left) == canonical_bytes(right)
    assert left.semantic_digest == right.semantic_digest == digest(left)
    assert b"2026-09-11T12:00:00.000000Z" in canonical_bytes(left)
    assert b"10.50" in canonical_bytes(left)


def test_aggregate_sum_and_relation_namespace_are_canonical_iir_semantics() -> None:
    region = ColumnReference(relation_alias="orders", column_name="status")
    amount = ColumnReference(relation_alias="orders", column_name="amount")
    product = _product_iir().model_copy(
        update={
            "operations": (
                AggregateOperation(
                    group_by=(region,),
                    measures=(
                        AggregateMeasure(
                            function="sum",
                            argument=amount,
                            output_name="total_amount",
                        ),
                    ),
                ),
            ),
            "grain": (region,),
        }
    )

    payload = product.model_dump(mode="json")

    assert payload["source"]["relation_namespace"] == "raw"
    assert payload["operations"][0]["measures"][0] == {
        "function": "sum",
        "argument": {
            "kind": "column",
            "relation_alias": "orders",
            "column_name": "amount",
        },
        "output_name": "total_amount",
    }


def _column() -> ColumnDeclaration:
    return ColumnDeclaration(name="order_id", value_type="integer", nullable=False)


def _product_iir() -> ProductIntentIR:
    source = SourceRelation(
        relation_namespace="raw",
        relation_name="orders",
        alias="orders",
        columns=(
            _column(),
            ColumnDeclaration(name="amount", value_type="decimal", nullable=False),
            ColumnDeclaration(name="status", value_type="string", nullable=False),
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
                        expression=ColumnReference(relation_alias="orders", column_name="order_id"),
                    ),
                    NamedExpression(
                        output_name="amount",
                        expression=LiteralValue(value_type="decimal", value=Decimal("10.50")),
                    ),
                    NamedExpression(
                        output_name="observed_at",
                        expression=LiteralValue(
                            value_type="timestamp",
                            value=datetime(2026, 9, 11, 12, tzinfo=UTC),
                        ),
                    ),
                )
            ),
        ),
        grain=(ColumnReference(relation_alias="orders", column_name="order_id"),),
        freshness_seconds=3_600,
    )
