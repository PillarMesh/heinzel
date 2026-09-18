from __future__ import annotations

from dataclasses import dataclass

from heinzel_iir import (
    AggregateOperation,
    ColumnReference,
    ProductIntentIR,
    ProjectOperation,
)


@dataclass(frozen=True, slots=True)
class RestrictedShapeCheck:
    satisfied: bool
    reason: str


def evaluate_project_sum_shape(product_iir: ProductIntentIR) -> tuple[RestrictedShapeCheck, ...]:
    project, aggregate = _restricted_operations(product_iir)
    projected_inputs = _direct_projected_inputs(project)
    group_by = aggregate.group_by if aggregate is not None else ()
    measure_inputs = _direct_measure_inputs(aggregate)
    group_output_names = (
        tuple(named.output_name for named in project.expressions[: len(group_by)])
        if project is not None
        else ()
    )
    measure_output_names = (
        tuple(named.output_name for named in aggregate.measures) if aggregate is not None else ()
    )
    return (
        RestrictedShapeCheck(
            satisfied=project is not None and aggregate is not None,
            reason="use exactly one direct projection followed by one aggregate",
        ),
        RestrictedShapeCheck(
            satisfied=bool(projected_inputs)
            and projected_inputs == (*group_by, *measure_inputs)
            and _projection_preserves_input_names(project),
            reason="project only the aggregate's direct declared source inputs",
        ),
        RestrictedShapeCheck(
            satisfied=bool(group_by) and group_by == product_iir.grain,
            reason="make the aggregate group exactly equal the product grain",
        ),
        RestrictedShapeCheck(
            satisfied=len(measure_inputs) == 1
            and _column_has_type(
                product_iir,
                measure_inputs[0],
                expected_type="decimal",
                require_non_null=True,
            ),
            reason="sum exactly one non-null decimal source column",
        ),
        RestrictedShapeCheck(
            satisfied=bool(group_by)
            and all(
                _column_has_type(
                    product_iir,
                    reference,
                    expected_type="string",
                    require_non_null=True,
                )
                for reference in group_by
            ),
            reason="group only by non-null string source columns under binary collation",
        ),
        RestrictedShapeCheck(
            satisfied=bool(group_output_names)
            and len((*group_output_names, *measure_output_names))
            == len(set((*group_output_names, *measure_output_names))),
            reason="keep grouped and measure output names distinct",
        ),
    )


def require_project_sum_shape(
    product_iir: ProductIntentIR,
) -> tuple[ProjectOperation, AggregateOperation]:
    for check in evaluate_project_sum_shape(product_iir):
        if not check.satisfied:
            raise ValueError(check.reason)
    project, aggregate = _restricted_operations(product_iir)
    if project is None or aggregate is None:
        raise ValueError("restricted project-sum shape was not established")
    return project, aggregate


def _restricted_operations(
    product_iir: ProductIntentIR,
) -> tuple[ProjectOperation | None, AggregateOperation | None]:
    if len(product_iir.operations) != 2:
        return None, None
    project, aggregate = product_iir.operations
    return (
        project if isinstance(project, ProjectOperation) else None,
        aggregate if isinstance(aggregate, AggregateOperation) else None,
    )


def _direct_projected_inputs(project: ProjectOperation | None) -> tuple[ColumnReference, ...]:
    if project is None or not all(
        isinstance(named.expression, ColumnReference) for named in project.expressions
    ):
        return ()
    return tuple(
        named.expression
        for named in project.expressions
        if isinstance(named.expression, ColumnReference)
    )


def _direct_measure_inputs(aggregate: AggregateOperation | None) -> tuple[ColumnReference, ...]:
    if aggregate is None or not all(
        measure.function == "sum" and isinstance(measure.argument, ColumnReference)
        for measure in aggregate.measures
    ):
        return ()
    return tuple(
        measure.argument
        for measure in aggregate.measures
        if isinstance(measure.argument, ColumnReference)
    )


def _projection_preserves_input_names(project: ProjectOperation | None) -> bool:
    return project is not None and all(
        isinstance(named.expression, ColumnReference)
        and named.output_name == named.expression.column_name
        for named in project.expressions
    )


def _column_has_type(
    product_iir: ProductIntentIR,
    reference: ColumnReference,
    *,
    expected_type: str,
    require_non_null: bool,
) -> bool:
    if reference.relation_alias != product_iir.source.alias:
        return False
    return any(
        column.name == reference.column_name
        and column.value_type == expected_type
        and (not require_non_null or not column.nullable)
        for column in product_iir.source.columns
    )
