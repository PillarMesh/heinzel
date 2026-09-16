"""D4 (locale) and D5 (timezone) rejection evidence for PRODUCT-SQL-V2-PROJECT-SUM-001.

The proof note argues that no locale-sensitive function and no timestamp expression can
appear in an admitted product. The IIR itself can express both -- `ApprovedFunctionName`
includes the case-conversion functions `lower` and `upper`, `ScalarType` includes
`timestamp`, and `LiteralScalar` includes `datetime`. The claim therefore rests entirely on
the restricted project-sum shape predicate, so these tests pin that predicate rather than the
IIR's vocabulary.
"""

from __future__ import annotations

from pillarmesh_compiler.restricted_sql import evaluate_project_sum_shape
from pillarmesh_iir import (
    AggregateMeasure,
    AggregateOperation,
    ApprovedFunctionCall,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)

_PROJECTION_REASON = "project only the aggregate's direct declared source inputs"
_GROUP_REASON = "group only by non-null string source columns under binary collation"
_MEASURE_REASON = "sum exactly one non-null decimal source column"


def _unsatisfied_reasons(product_iir: ProductIntentIR) -> frozenset[str]:
    return frozenset(
        check.reason for check in evaluate_project_sum_shape(product_iir) if not check.satisfied
    )


def _product(
    *,
    group_type: str = "string",
    group_expression: object | None = None,
    measure_type: str = "decimal",
) -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="logical",
            relation_name="revenue_events",
            alias="revenue_events",
            columns=(
                ColumnDeclaration(name="region", value_type=group_type, nullable=False),
                ColumnDeclaration(name="revenue", value_type=measure_type, nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(
                        output_name="region",
                        expression=group_expression if group_expression is not None else region,
                    ),
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


def test_the_baseline_product_satisfies_every_shape_check() -> None:
    assert _unsatisfied_reasons(_product()) == frozenset()


def test_d4_case_conversion_in_the_projection_is_not_a_direct_input() -> None:
    lowered = ApprovedFunctionCall(
        function_name="lower",
        arguments=(ColumnReference(relation_alias="revenue_events", column_name="region"),),
    )

    assert _PROJECTION_REASON in _unsatisfied_reasons(_product(group_expression=lowered))


def test_d4_upper_case_conversion_is_rejected_on_the_same_ground() -> None:
    uppered = ApprovedFunctionCall(
        function_name="upper",
        arguments=(ColumnReference(relation_alias="revenue_events", column_name="region"),),
    )

    assert _PROJECTION_REASON in _unsatisfied_reasons(_product(group_expression=uppered))


def test_d5_a_timestamp_group_column_cannot_be_grouped() -> None:
    assert _GROUP_REASON in _unsatisfied_reasons(_product(group_type="timestamp"))


def test_d5_a_timestamp_measure_cannot_be_summed() -> None:
    assert _MEASURE_REASON in _unsatisfied_reasons(_product(measure_type="timestamp"))
