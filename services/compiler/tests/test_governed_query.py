from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pillarmesh_compiler import (
    GovernedQueryInput,
    GovernedQueryPlan,
    GovernedQueryPlanNotRequired,
    NoValidPlan,
    QueryCeilings,
    QueryConsumptionObject,
    QueryDimension,
    QueryEstimateRequest,
    QueryEstimator,
    QueryFilter,
    QueryMetric,
    QueryOrder,
    QueryReference,
    QueryScan,
    QueryScanEstimate,
    QueryTimeWindow,
    compile_governed_query,
)
from pillarmesh_contract_model import digest
from pydantic import ValidationError

_DIGEST = "a" * 64
_START = datetime(2026, 9, 1, tzinfo=UTC)
_END = datetime(2026, 9, 8, tzinfo=UTC)


def _reference(artifact_id: str) -> QueryReference:
    return QueryReference(artifact_id=artifact_id, version=1, digest=_DIGEST)


def _query_input(**updates: object) -> GovernedQueryInput:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "validation_digest": _DIGEST,
        "intent_kind": "metric_value",
        "engine_kind": "postgresql",
        "consumption_object": QueryConsumptionObject(
            object_ref=_reference("product:orders"),
            namespace="analytics",
            relation_name="orders_current",
        ),
        "product_generation_refs": (
            {"product_ref": _reference("product:orders"), "generation": 42},
        ),
        "metrics": (
            QueryMetric(
                metric_ref=_reference("metric:revenue"),
                aggregate="sum",
                column_name="amount",
                output_name="revenue",
            ),
        ),
        "dimensions": (
            QueryDimension(
                dimension_ref=_reference("dimension:region"),
                column_name="region",
                output_name="region",
            ),
        ),
        "filters": (
            QueryFilter(
                filter_ref=_reference("dimension:status"),
                column_name="status",
                operator="equal",
                value_type="string",
                value="settled' OR TRUE; --",
            ),
        ),
        "time_window": QueryTimeWindow(
            time_dimension_ref=_reference("dimension:occurred-at"),
            column_name="occurred_at",
            start=_START,
            end=_END,
        ),
        "ordering": (QueryOrder(output_name="revenue", direction="descending"),),
        "row_limit": 100,
        "disclosure_entity_column": "customer_id",
        "minimum_group_size": 5,
        "estimated_scan": QueryScanEstimate(
            rows=2_000,
            bytes=80_000,
            estimator_version="postgresql-explain-v1",
        ),
        "period_scan_consumed": QueryScan(rows=10_000, bytes=400_000),
        "ceilings": QueryCeilings(
            row_limit=500,
            scan=QueryScan(rows=5_000, bytes=200_000),
            period_scan=QueryScan(rows=50_000, bytes=2_000_000),
        ),
    }
    values.update(updates)
    return GovernedQueryInput.model_validate(values)


class _DigestSigner:
    def sign(self, plan_digest: str) -> str:
        return f"test-signature:{plan_digest}"


def _compile(
    query_input: GovernedQueryInput,
    *,
    estimator: QueryEstimator | None = None,
) -> GovernedQueryPlan | GovernedQueryPlanNotRequired | NoValidPlan:
    return compile_governed_query(query_input, signer=_DigestSigner(), estimator=estimator)


def test_postgresql_query_quotes_identifiers_binds_values_and_suppresses_small_groups() -> None:
    query_input = _query_input()

    result = _compile(query_input)

    assert isinstance(result, GovernedQueryPlan)
    assert result.statement == (
        'SELECT "source"."region" AS "region", '
        'SUM("source"."amount") AS "revenue" '
        'FROM "analytics"."orders_current" AS "source" '
        'WHERE ("source"."status" = %s) '
        'AND ("source"."occurred_at" >= %s) '
        'AND ("source"."occurred_at" < %s) '
        'GROUP BY "source"."region" '
        'HAVING COUNT(DISTINCT "source"."customer_id") >= %s '
        'ORDER BY "revenue" DESC LIMIT 100'
    )
    assert "settled' OR TRUE; --" not in result.statement
    assert tuple(parameter.value for parameter in result.parameters) == (
        "settled' OR TRUE; --",
        _START,
        _END,
        5,
    )
    assert result.product_generation_refs[0].generation == 42
    assert result.routing == "policy_admitted"


def test_clickhouse_query_uses_typed_bind_parameters() -> None:
    result = _compile(_query_input(engine_kind="clickhouse"))

    assert isinstance(result, GovernedQueryPlan)
    assert "{p0:String}" in result.statement
    assert "{p1:DateTime64(6, 'UTC')}" in result.statement
    assert "{p2:DateTime64(6, 'UTC')}" in result.statement
    assert "{p3:Int64}" in result.statement


def test_compiler_obtains_estimate_from_the_pinned_estimator_port() -> None:
    requests: list[QueryEstimateRequest] = []

    class Estimator:
        def estimate(self, request: QueryEstimateRequest) -> QueryScanEstimate:
            requests.append(request)
            return QueryScanEstimate(rows=2_000, bytes=80_000, estimator_version="estimate-v1")

    result = _compile(_query_input(estimated_scan=None), estimator=Estimator())

    assert isinstance(result, GovernedQueryPlan)
    assert requests[0].statement == result.statement
    assert requests[0].product_generation_refs == result.product_generation_refs
    assert result.estimated_scan is not None
    assert result.estimated_scan.estimator_version == "estimate-v1"


def test_compiler_binds_the_plan_digest_to_an_injected_signature() -> None:
    signed_digests: list[str] = []

    class Signer:
        def sign(self, plan_digest: str) -> str:
            signed_digests.append(plan_digest)
            return f"signature:{plan_digest}"

    result = compile_governed_query(_query_input(), signer=Signer())

    assert isinstance(result, GovernedQueryPlan)
    assert signed_digests == [result.plan_digest]
    assert result.signature == f"signature:{result.plan_digest}"


def test_query_input_rejects_identifier_injection_and_raw_statement_input() -> None:
    with pytest.raises(ValidationError, match="namespace"):
        _query_input(
            consumption_object={
                "object_ref": _reference("product:orders"),
                "namespace": 'analytics"; DROP TABLE users; --',
                "relation_name": "orders_current",
            }
        )

    with pytest.raises(ValidationError, match="statement"):
        GovernedQueryInput.model_validate(
            {**_query_input().model_dump(mode="python"), "statement": "SELECT * FROM secrets"}
        )


def test_generation_and_estimator_references_must_be_structurally_pinned() -> None:
    with pytest.raises(ValidationError, match="product_generation_refs"):
        _query_input(product_generation_refs=("generation:orders:42",))

    with pytest.raises(ValidationError, match="estimator_version"):
        _query_input(estimated_scan={"rows": 2_000, "bytes": 80_000})


def test_unknown_query_construct_returns_no_valid_plan() -> None:
    unsupported = QueryMetric(
        metric_ref=_reference("metric:median-revenue"),
        aggregate="median",
        column_name="amount",
        output_name="median_revenue",
    )

    result = _compile(_query_input(metrics=(unsupported,)))

    assert isinstance(result, NoValidPlan)
    assert result.rule_id == "GOVERNED-QUERY-V1"
    assert result.preconditions[0].reason == "query construct is not allowlisted: aggregate:median"
    assert result.execution_occurred is False


@pytest.mark.parametrize(
    ("estimate", "routing"),
    (
        (None, "per_question_review"),
        (
            QueryScanEstimate(rows=5_001, bytes=80_000, estimator_version="estimate-v1"),
            "per_question_review",
        ),
        (
            QueryScanEstimate(rows=2_000, bytes=200_001, estimator_version="estimate-v1"),
            "per_question_review",
        ),
    ),
)
def test_missing_or_over_ceiling_estimate_routes_to_review(
    estimate: QueryScanEstimate | None,
    routing: str,
) -> None:
    result = _compile(_query_input(estimated_scan=estimate))

    assert isinstance(result, GovernedQueryPlan)
    assert result.routing == routing


def test_period_scan_budget_routes_to_policy_revision_dependency() -> None:
    result = _compile(_query_input(period_scan_consumed=QueryScan(rows=49_000, bytes=1_950_000)))

    assert isinstance(result, GovernedQueryPlan)
    assert result.routing == "policy_revision_dependency"


def test_row_limit_above_policy_ceiling_returns_no_valid_plan() -> None:
    result = _compile(_query_input(row_limit=501))

    assert isinstance(result, NoValidPlan)
    assert result.preconditions[0].reason == "row limit exceeds the validated ceiling"


def test_plan_and_statement_digests_are_deterministic() -> None:
    first = _compile(_query_input())
    second = _compile(_query_input())

    assert isinstance(first, GovernedQueryPlan)
    assert isinstance(second, GovernedQueryPlan)
    assert first == second
    assert first.plan_digest == second.plan_digest
    assert first.statement_digest == digest(first.statement)
    assert first.parameter_digest == digest(first.parameters)


def test_definition_intent_requires_no_query_plan() -> None:
    result = _compile(
        _query_input(
            intent_kind="definition",
            metrics=(),
            dimensions=(),
            filters=(),
            time_window=None,
            ordering=(),
            estimated_scan=None,
        )
    )

    assert result == GovernedQueryPlanNotRequired(
        tenant_id="tenant-a",
        validation_digest=_DIGEST,
    )
