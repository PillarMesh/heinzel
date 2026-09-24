"""Every restricted-shape refusal must name the ground it was actually refused on.

A refusal reason is governed evidence: it is what `NoValidPlan.smallest_changes` reports
back to a requester. A focused mutation run over `restricted_sql.py` showed that the first
check only asserted `project is not None`, so a product carrying no aggregate at all was
refused with the *grain* message ("make the aggregate group exactly equal the product
grain"), which names a constraint the product never reached. These tests pin the ground of
each structural refusal so that widening one check cannot silently borrow another's reason.
"""

from __future__ import annotations

from heinzel_compiler.restricted_sql import evaluate_project_sum_shape
from heinzel_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)

_SHAPE_REASON = "use exactly one direct projection followed by one aggregate"
_GRAIN_REASON = "make the aggregate group exactly equal the product grain"

_REGION = ColumnReference(relation_alias="revenue_events", column_name="region")
_REVENUE = ColumnReference(relation_alias="revenue_events", column_name="revenue")

_PROJECT = ProjectOperation(
    expressions=(
        NamedExpression(output_name="region", expression=_REGION),
        NamedExpression(output_name="revenue", expression=_REVENUE),
    )
)
_AGGREGATE = AggregateOperation(
    group_by=(_REGION,),
    measures=(AggregateMeasure(function="sum", argument=_REVENUE, output_name="total_revenue"),),
)


def _product(operations: tuple[object, ...]) -> ProductIntentIR:
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
        operations=operations,  # type: ignore[arg-type]
        grain=(_REGION,),
        freshness_seconds=3600,
    )


def _unsatisfied_reasons(operations: tuple[object, ...]) -> frozenset[str]:
    return frozenset(
        check.reason
        for check in evaluate_project_sum_shape(_product(operations))
        if not check.satisfied
    )


def test_the_baseline_shape_satisfies_every_check() -> None:
    assert _unsatisfied_reasons((_PROJECT, _AGGREGATE)) == frozenset()


def test_a_product_without_an_aggregate_is_refused_on_the_shape_ground() -> None:
    reasons = _unsatisfied_reasons((_PROJECT, _PROJECT))

    assert _SHAPE_REASON in reasons


def test_a_product_without_a_projection_is_refused_on_the_shape_ground() -> None:
    reasons = _unsatisfied_reasons((_AGGREGATE, _AGGREGATE))

    assert _SHAPE_REASON in reasons


def test_a_grain_mismatch_is_the_only_structural_ground_for_the_grain_reason() -> None:
    """The grain reason must not double as the catch-all for a missing aggregate."""
    other = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    mismatched = AggregateOperation(
        group_by=(other,),
        measures=(
            AggregateMeasure(function="sum", argument=_REVENUE, output_name="total_revenue"),
        ),
    )

    assert _GRAIN_REASON in _unsatisfied_reasons((_PROJECT, mismatched))
    assert _SHAPE_REASON not in _unsatisfied_reasons((_PROJECT, mismatched))
