from __future__ import annotations

from typing import Protocol

from pillarmesh_contract_model import digest
from pillarmesh_iir.product_models import Identifier, LiteralScalar, ScalarType
from pydantic import TypeAdapter

from .models import NoValidPlan, PreconditionResult
from .query_models import (
    GovernedQueryInput,
    GovernedQueryPlan,
    GovernedQueryPlanNotRequired,
    QueryEstimateRequest,
    QueryRouting,
    QueryScanEstimate,
)
from .sql_models import SqlParameter

_RULE_ID = "GOVERNED-QUERY-V1"
_AGGREGATES = {
    "average": "AVG",
    "count": "COUNT",
    "maximum": "MAX",
    "minimum": "MIN",
    "sum": "SUM",
}
_OPERATORS = {
    "equal": "=",
    "greater_than": ">",
    "greater_than_or_equal": ">=",
    "less_than": "<",
    "less_than_or_equal": "<=",
    "not_equal": "<>",
}
_CLICKHOUSE_TYPES: dict[ScalarType, str] = {
    "boolean": "Bool",
    "decimal": "Decimal(38, 9)",
    "integer": "Int64",
    "string": "String",
    "timestamp": "DateTime64(6, 'UTC')",
}
_IDENTIFIER_ADAPTER: TypeAdapter[str] = TypeAdapter(Identifier)


class QueryEstimator(Protocol):
    def estimate(self, request: QueryEstimateRequest) -> QueryScanEstimate | None: ...


class QueryPlanSigner(Protocol):
    def sign(self, plan_digest: str) -> str: ...


def compile_governed_query(
    query_input: GovernedQueryInput,
    *,
    signer: QueryPlanSigner,
    estimator: QueryEstimator | None = None,
) -> GovernedQueryPlan | GovernedQueryPlanNotRequired | NoValidPlan:
    if query_input.intent_kind == "definition":
        return GovernedQueryPlanNotRequired(
            tenant_id=query_input.tenant_id,
            validation_digest=query_input.validation_digest,
        )
    if not query_input.metrics:
        return _no_valid_plan("query construct is not allowlisted: metrics:empty")
    for metric in query_input.metrics:
        if metric.aggregate not in _AGGREGATES:
            return _no_valid_plan(
                f"query construct is not allowlisted: aggregate:{metric.aggregate}"
            )
    for query_filter in query_input.filters:
        if query_filter.operator not in _OPERATORS:
            return _no_valid_plan(
                f"query construct is not allowlisted: filter:{query_filter.operator}"
            )
    output_names = {
        *(dimension.output_name for dimension in query_input.dimensions),
        *(metric.output_name for metric in query_input.metrics),
    }
    if len(output_names) != len(query_input.dimensions) + len(query_input.metrics):
        return _no_valid_plan("query output names must be unique")
    if any(order.output_name not in output_names for order in query_input.ordering):
        return _no_valid_plan("query ordering references an unknown output")
    if query_input.row_limit > query_input.ceilings.row_limit:
        return _no_valid_plan("row limit exceeds the validated ceiling")

    parameters: list[SqlParameter] = []

    def bind(value_type: ScalarType, value: LiteralScalar) -> str:
        name = f"p{len(parameters)}"
        parameters.append(SqlParameter(name=name, value_type=value_type, value=value))
        if query_input.engine_kind == "postgresql":
            return "%s"
        return f"{{{name}:{_CLICKHOUSE_TYPES[value_type]}}}"

    source = query_input.consumption_object
    source_alias = _quote("source")
    projections = [
        f"{source_alias}.{_quote(dimension.column_name)} AS {_quote(dimension.output_name)}"
        for dimension in query_input.dimensions
    ]
    projections.extend(
        f"{_AGGREGATES[metric.aggregate]}("
        f"{source_alias}.{_quote(metric.column_name)}) AS {_quote(metric.output_name)}"
        for metric in query_input.metrics
    )
    predicates = [
        f"({source_alias}.{_quote(query_filter.column_name)} "
        f"{_OPERATORS[query_filter.operator]} "
        f"{bind(query_filter.value_type, query_filter.value)})"
        for query_filter in query_input.filters
    ]
    if query_input.time_window is not None:
        time_column = f"{source_alias}.{_quote(query_input.time_window.column_name)}"
        predicates.extend(
            (
                f"({time_column} >= {bind('timestamp', query_input.time_window.start)})",
                f"({time_column} < {bind('timestamp', query_input.time_window.end)})",
            )
        )

    statement = (
        f"SELECT {', '.join(projections)} "
        f"FROM {_quote(source.namespace)}.{_quote(source.relation_name)} AS {source_alias}"
    )
    if predicates:
        statement += f" WHERE {' AND '.join(predicates)}"
    if query_input.dimensions:
        statement += " GROUP BY " + ", ".join(
            f"{source_alias}.{_quote(dimension.column_name)}"
            for dimension in query_input.dimensions
        )
    suppression_parameter = bind("integer", query_input.minimum_group_size)
    statement += (
        f" HAVING COUNT(DISTINCT {source_alias}."
        f"{_quote(query_input.disclosure_entity_column)}) >= {suppression_parameter}"
    )
    if query_input.ordering:
        statement += " ORDER BY " + ", ".join(
            f"{_quote(order.output_name)} {'ASC' if order.direction == 'ascending' else 'DESC'}"
            for order in query_input.ordering
        )
    statement += f" LIMIT {query_input.row_limit}"

    parameter_tuple = tuple(parameters)
    estimate = query_input.estimated_scan
    if estimator is not None:
        estimate = estimator.estimate(
            QueryEstimateRequest(
                engine_kind=query_input.engine_kind,
                statement=statement,
                parameters=parameter_tuple,
                product_generation_refs=query_input.product_generation_refs,
            )
        )
    statement_digest = digest(statement)
    parameter_digest = digest(parameter_tuple)
    plan_body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "query-plan-"
        + digest(
            {
                "validation_digest": query_input.validation_digest,
                "engine_kind": query_input.engine_kind,
                "product_generation_refs": query_input.product_generation_refs,
            }
        )[:24],
        "tenant_id": query_input.tenant_id,
        "validation_digest": query_input.validation_digest,
        "engine_kind": query_input.engine_kind,
        "compiler_version": query_input.compiler_version,
        "allowlist_version": query_input.allowlist_version,
        "consumption_object_refs": (source.object_ref,),
        "product_generation_refs": query_input.product_generation_refs,
        "minimum_group_size": query_input.minimum_group_size,
        "statement": statement,
        "parameters": parameter_tuple,
        "statement_digest": statement_digest,
        "parameter_digest": parameter_digest,
        "estimated_scan": estimate,
        "ceilings": query_input.ceilings,
        "routing": _route(query_input, estimate),
    }
    plan_digest = digest(plan_body)
    return GovernedQueryPlan.model_validate(
        {
            **plan_body,
            "plan_digest": plan_digest,
            "signature": signer.sign(plan_digest),
        }
    )


def _route(query_input: GovernedQueryInput, estimate: QueryScanEstimate | None) -> QueryRouting:
    if estimate is not None and (
        query_input.period_scan_consumed.rows + estimate.rows
        > query_input.ceilings.period_scan.rows
        or query_input.period_scan_consumed.bytes + estimate.bytes
        > query_input.ceilings.period_scan.bytes
    ):
        return "policy_revision_dependency"
    if (
        query_input.validation_outcome == "review_required"
        or query_input.statement_ceiling_breached
        or estimate is None
        or estimate.rows > query_input.ceilings.scan.rows
        or estimate.bytes > query_input.ceilings.scan.bytes
    ):
        return "per_question_review"
    return "policy_admitted"


def _quote(identifier: str) -> str:
    validated = _IDENTIFIER_ADAPTER.validate_python(identifier, strict=True)
    return f'"{validated}"'


def _no_valid_plan(reason: str) -> NoValidPlan:
    return NoValidPlan(
        rule_id=_RULE_ID,
        preconditions=(PreconditionResult(number=1, status="unsatisfied", reason=reason),),
        smallest_changes=("use only governed query allowlist constructs",),
    )
