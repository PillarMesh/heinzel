from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_compiler import NoValidPlan, compile_product_iir
from pillarmesh_compiler.clickhouse_sql import emit_clickhouse
from pillarmesh_compiler.postgresql_sql import emit_postgresql
from pillarmesh_compiler.restricted_sql import evaluate_project_sum_shape
from pillarmesh_contract_model import digest
from pillarmesh_iir import (
    AggregateMeasure,
    AggregateOperation,
    ArithmeticExpression,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from pillarmesh_provider_sdk import (
    InvalidProductSqlProviderObservation,
    ProductSqlColumnObservation,
    ProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlProviderObservationVerifier,
    ProductSqlSumSemantics,
    SignedProductSqlProviderObservation,
)
from pydantic import ValidationError

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "legality" / "product-sql" / "fixtures"
_RULE_PATH = (
    Path(__file__).parents[1]
    / "legality"
    / "product-sql"
    / "rules"
    / "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.json"
)
_POSTGRESQL_VERSION = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_CLICKHOUSE_VERSION = (
    "clickhouse/clickhouse-server:25.8.32.4@sha256:"
    "7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
_CAPABILITIES = (
    "binary_collation",
    "decimal_38_9_sum",
    "group_by",
    "project",
    "quoted_identifiers",
    "utc_timezone",
)
_POSTGRESQL_IMAGE_DIGEST = "33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"


def test_revenue_by_region_candidate_stays_closed_after_the_static_bound_proof() -> None:
    result = _compile(_revenue_by_region_iir(), observation=_postgresql_observation())

    assert isinstance(result, NoValidPlan)
    assert result.rule_id == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert tuple(item.status for item in result.preconditions[:12]) == ("satisfied",) * 12
    assert tuple(item.number for item in result.preconditions) == tuple(range(1, 19))
    assert tuple(item.reason for item in result.preconditions[6:11]) == (
        "bind the provider observation to the expected tenant, warehouse, relation, and digest",
        "bind the pinned engine version, image, and build to the provider observation",
        "use a provider observation no older than ten minutes at evaluation time",
        "observe the exact non-null binary string and Decimal(38,9) physical columns",
        "observe the pinned engine-specific SUM input, accumulator, result, overflow, null, "
        "and empty-group semantics",
    )
    assert result.preconditions[11].reason == (
        "prove NUMERIC(38,9) sums fit exact Decimal(57,9) arithmetic at the signed ledger ceiling"
    )
    assert tuple(item.status for item in result.preconditions[12:]) == ("unsatisfied",) * 6
    assert result.smallest_changes == (
        "bind the physical source to exactly one authoritative generation and receipt digest",
        "bind the contributing row ceiling to owning-service cardinality evidence",
        "enforce checked Decimal(57,9) result magnitude on every engine at runtime",
        "authenticate the observation with provider-owned provenance authority",
        "review live checked SUM on the pinned PostgreSQL engine; "
        "cross-engine equivalence is not claimed",
        "independent legality review has not approved this rule",
    )
    assert result.execution_occurred is False


def test_valid_signed_provider_observation_satisfies_only_the_provenance_gate() -> None:
    observation = _postgresql_observation()
    signed, verifier = _signed_observation(observation)

    result = _compile_signed(signed, verifier=verifier)

    assert result.preconditions[15].status == "satisfied"
    assert tuple(item.status for item in result.preconditions[12:15]) == (
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
    )
    assert tuple(item.status for item in result.preconditions[16:]) == (
        "unsatisfied",
        "unsatisfied",
    )
    assert result.execution_occurred is False


def test_raw_provider_observation_remains_untrusted_for_provenance() -> None:
    result = _compile(_revenue_by_region_iir(), observation=_postgresql_observation())

    assert result.preconditions[15].status == "unsatisfied"


def test_raw_and_signed_observations_are_ambiguous_and_remain_untrusted() -> None:
    observation = _postgresql_observation()
    signed, verifier = _signed_observation(observation)

    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        provider_observation=observation,
        signed_provider_observation=signed,
        provider_observation_verifier=verifier,
        expected_provider_observation_digest=signed.observation_digest,
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=4,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="b" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
    )

    assert result.preconditions[15].status == "unsatisfied"
    assert result.execution_occurred is False


@pytest.mark.parametrize(
    "case_kind",
    (
        "tampered",
        "malformed-signature",
        "unknown-key",
        "stale",
        "future",
        "copied-extra",
        "constructed-invalid",
        "wrong-tenant",
        "wrong-binding",
        "wrong-provider",
        "no-verifier",
        "malformed-verifier",
    ),
)
def test_signed_provider_provenance_fails_closed_for_invalid_or_mismatched_envelopes(
    case_kind: str,
) -> None:
    observation = _postgresql_observation()
    evaluated_at = datetime(2026, 9, 15, 12, 5, tzinfo=UTC)
    if case_kind == "stale":
        observation = observation.model_copy(
            update={"observed_at": evaluated_at - timedelta(minutes=11)}
        )
    elif case_kind == "future":
        observation = observation.model_copy(
            update={"observed_at": evaluated_at + timedelta(microseconds=1)}
        )
    elif case_kind == "wrong-tenant":
        observation = observation.model_copy(update={"tenant_id": "tenant-b"})
    elif case_kind == "wrong-binding":
        observation = observation.model_copy(update={"warehouse_binding_id": "warehouse-b"})
    elif case_kind == "wrong-provider":
        observation = observation.model_copy(update={"engine": "clickhouse"})
    signed, actual_verifier = _signed_observation(observation)
    verifier: ProductSqlProviderObservationVerifier | None = actual_verifier
    expected_digest = digest(observation)
    if case_kind == "tampered":
        signed = signed.model_copy(
            update={"observation": signed.observation.model_copy(update={"engine_version": "18.7"})}
        )
    elif case_kind == "malformed-signature":
        signed = signed.model_copy(update={"signature": "not-base64!"})
    elif case_kind == "unknown-key":
        verifier = ProductSqlProviderObservationVerifier(
            {}, maximum_observation_age=timedelta(minutes=10)
        )
    elif case_kind == "copied-extra":
        signed = signed.model_copy(update={"undeclared": "sensitive"})
    elif case_kind == "constructed-invalid":
        signed = SignedProductSqlProviderObservation.model_construct(
            **signed.model_dump(mode="python") | {"observation_digest": "not-a-digest"}
        )
    elif case_kind == "no-verifier":
        verifier = None
    elif case_kind == "malformed-verifier":
        verifier = cast(ProductSqlProviderObservationVerifier, object())

    result = _compile_signed(
        signed,
        verifier=verifier,
        expected_observation_digest=expected_digest,
        evaluated_at=evaluated_at,
    )

    assert result.preconditions[15].status == "unsatisfied"
    assert result.preconditions[15].reason == (
        "authenticate the observation with provider-owned provenance authority"
    )
    assert "sensitive" not in result.preconditions[15].reason
    assert result.execution_occurred is False


def test_provider_verification_failure_does_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed, verifier = _signed_observation(_postgresql_observation())

    def fail_verification(*_args: object, **_kwargs: object) -> ProductSqlProviderObservation:
        raise InvalidProductSqlProviderObservation("secret provider signing detail")

    monkeypatch.setattr(verifier, "verify", fail_verification)

    result = _compile_signed(signed, verifier=verifier)

    assert result.preconditions[15].status == "unsatisfied"
    assert "secret" not in result.model_dump_json()


def test_postgresql_emits_revenue_by_region_sum() -> None:
    emitted = emit_postgresql(_revenue_by_region_iir())

    assert emitted.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(SUM(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS '
        'NUMERIC(57,9)) AS "total_revenue" '
        'FROM "raw"."revenue_events" AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert emitted.parameters == ()


def test_clickhouse_emits_checked_widening_syntax_for_the_restricted_revenue_shape() -> None:
    emitted = emit_clickhouse(_revenue_by_region_iir())

    assert emitted.statement == (
        'SELECT "revenue_events"."region" AS "region", '
        'CAST(sum(CAST("revenue_events"."revenue" AS Decimal(57, 9))) AS '
        'Decimal(57, 9)) AS "total_revenue" '
        'FROM "raw"."revenue_events" AS "revenue_events" '
        'GROUP BY "revenue_events"."region"'
    )
    assert emitted.parameters == ()


def test_restricted_sum_emits_multiple_group_columns_in_declared_order() -> None:
    product = _revenue_by_region_iir()
    country = ColumnReference(relation_alias="revenue_events", column_name="country")
    project = product.operations[0]
    aggregate = product.operations[1]
    assert isinstance(project, ProjectOperation)
    assert isinstance(aggregate, AggregateOperation)
    product = product.model_copy(
        update={
            "source": product.source.model_copy(
                update={
                    "columns": (
                        product.source.columns[0],
                        ColumnDeclaration(name="country", value_type="string", nullable=False),
                        product.source.columns[1],
                    )
                }
            ),
            "operations": (
                project.model_copy(
                    update={
                        "expressions": (
                            project.expressions[0],
                            NamedExpression(output_name="country", expression=country),
                            project.expressions[1],
                        )
                    }
                ),
                aggregate.model_copy(update={"group_by": (aggregate.group_by[0], country)}),
            ),
            "grain": (aggregate.group_by[0], country),
        }
    )

    emitted = emit_postgresql(product)

    assert '"region" AS "region", "revenue_events"."country" AS "country",' in emitted.statement
    assert emitted.statement.endswith(
        'GROUP BY "revenue_events"."region", "revenue_events"."country"'
    )


def test_emitters_return_unsigned_syntax_without_execution_authority() -> None:
    emitted = emit_postgresql(_revenue_by_region_iir())

    assert set(type(emitted).model_fields) == {"engine", "statement", "parameters"}
    assert "rule_id" not in type(emitted).model_fields
    assert "signature" not in type(emitted).model_fields
    assert "execution_graph" not in type(emitted).model_fields


@pytest.mark.parametrize(
    ("mutation", "precondition", "reason"),
    [
        pytest.param(
            lambda product: product.model_copy(
                update={"operations": tuple(reversed(product.operations))}
            ),
            1,
            "use exactly one direct projection followed by one aggregate",
            id="operation-order",
        ),
        pytest.param(
            lambda product: product.model_copy(
                update={
                    "operations": (
                        product.operations[0].model_copy(
                            update={
                                "expressions": (
                                    NamedExpression(
                                        output_name="region",
                                        expression=ArithmeticExpression(
                                            operator="add",
                                            left=ColumnReference(
                                                relation_alias="revenue_events",
                                                column_name="revenue",
                                            ),
                                            right=ColumnReference(
                                                relation_alias="revenue_events",
                                                column_name="revenue",
                                            ),
                                        ),
                                    ),
                                )
                            }
                        ),
                        product.operations[1],
                    )
                }
            ),
            2,
            "project only the aggregate's direct declared source inputs",
            id="computed-project",
        ),
        pytest.param(
            lambda product: product.model_copy(
                update={
                    "source": product.source.model_copy(
                        update={
                            "columns": tuple(
                                column.model_copy(update={"nullable": True})
                                if column.name == "revenue"
                                else column
                                for column in product.source.columns
                            )
                        }
                    )
                }
            ),
            4,
            "sum exactly one non-null decimal source column",
            id="nullable-measure",
        ),
        pytest.param(
            lambda product: product.model_copy(
                update={
                    "grain": (
                        ColumnReference(relation_alias="revenue_events", column_name="revenue"),
                    )
                }
            ),
            3,
            "make the aggregate group exactly equal the product grain",
            id="grain-mismatch",
        ),
        pytest.param(
            lambda product: product.model_copy(
                update={
                    "source": product.source.model_copy(
                        update={
                            "columns": tuple(
                                column.model_copy(update={"nullable": True})
                                if column.name == "region"
                                else column
                                for column in product.source.columns
                            )
                        }
                    )
                }
            ),
            5,
            "group only by non-null string source columns under binary collation",
            id="nullable-group",
        ),
        pytest.param(
            lambda product: product.model_copy(
                update={
                    "operations": (
                        product.operations[0].model_copy(update={"expressions": ()}),
                        product.operations[1].model_copy(update={"group_by": (), "measures": ()}),
                    ),
                    "grain": (),
                }
            ),
            2,
            "project only the aggregate's direct declared source inputs",
            id="empty-projection-and-aggregate",
        ),
    ],
)
def test_each_restricted_sum_precondition_fails_closed(
    mutation: Callable[[ProductIntentIR], ProductIntentIR], precondition: int, reason: str
) -> None:
    result = _compile(mutation(_revenue_by_region_iir()))

    failures = tuple(
        (item.number, item.reason) for item in result.preconditions if item.status != "satisfied"
    )
    assert (precondition, reason) in failures
    assert failures[-1] == (18, "independent legality review has not approved this rule")
    assert result.smallest_changes == tuple(item[1] for item in failures)
    assert result.execution_occurred is False


@pytest.mark.parametrize(
    ("engine_version", "capabilities"),
    [
        pytest.param("postgres:latest", _CAPABILITIES, id="unpinned-version"),
        *(
            pytest.param(
                _POSTGRESQL_VERSION,
                tuple(item for item in _CAPABILITIES if item != missing),
                id=f"missing-{missing}",
            )
            for missing in _CAPABILITIES
        ),
    ],
)
def test_caller_claimed_provider_semantics_cannot_replace_an_observation(
    engine_version: str, capabilities: tuple[str, ...]
) -> None:
    result = _compile(
        _revenue_by_region_iir(),
        engine_version=engine_version,
        capabilities=capabilities,
    )

    assert result.preconditions[6].status == "unsatisfied"
    assert result.preconditions[6].number == 7
    assert result.preconditions[6].reason == (
        "bind the provider observation to the expected tenant, warehouse, relation, and digest"
    )
    assert tuple(item.status for item in result.preconditions[6:11]) == ("unsatisfied",) * 5
    assert result.execution_occurred is False


@pytest.mark.parametrize(
    ("engine_version", "capabilities"),
    [
        pytest.param(_POSTGRESQL_VERSION, (), id="version-only"),
        pytest.param(None, _CAPABILITIES, id="capabilities-only"),
    ],
)
def test_partial_profile_input_uses_candidate_fail_closed_contract(
    engine_version: str | None, capabilities: tuple[str, ...]
) -> None:
    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        engine_version=engine_version,
        capabilities=capabilities,
    )

    assert result.rule_id == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert result.preconditions[6].status == "unsatisfied"


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        pytest.param(
            {"tenant_id": "tenant-b"},
            "bind the provider observation to the expected tenant, warehouse, relation, and digest",
            id="tenant",
        ),
        pytest.param(
            {"warehouse_binding_revision": 5},
            "bind the provider observation to the expected tenant, warehouse, relation, and digest",
            id="binding-revision",
        ),
        pytest.param(
            {"relation_ref": "relation-other-v1"},
            "bind the provider observation to the expected tenant, warehouse, relation, and digest",
            id="relation-ref",
        ),
        pytest.param(
            {"relation_namespace": "other"},
            "bind the provider observation to the expected tenant, warehouse, relation, and digest",
            id="relation-namespace",
        ),
        pytest.param(
            {"observed_at": datetime(2026, 9, 15, 11, 49, tzinfo=UTC)},
            "use a provider observation no older than ten minutes at evaluation time",
            id="stale",
        ),
    ],
)
def test_provider_observation_identity_and_freshness_fail_closed(
    changed: dict[str, object], reason: str
) -> None:
    observation = _postgresql_observation().model_copy(update=changed)

    result = _compile(_revenue_by_region_iir(), observation=observation)

    assert reason in result.smallest_changes
    assert result.execution_occurred is False


def test_provider_observation_digest_tampering_fails_closed() -> None:
    observation = _postgresql_observation()

    result = _compile(
        _revenue_by_region_iir(),
        observation=observation,
        expected_observation_digest="f" * 64,
    )

    assert result.preconditions[6].status == "unsatisfied"


def test_observed_namespace_must_match_the_iir_namespace() -> None:
    product = _revenue_by_region_iir()
    other_namespace = product.source.model_copy(update={"relation_namespace": "curated"})

    result = _compile(
        product.model_copy(update={"source": other_namespace}),
        observation=_postgresql_observation(),
    )

    assert result.preconditions[6].status == "satisfied"
    assert result.preconditions[9].status == "unsatisfied"


@pytest.mark.parametrize(
    "evaluated_at",
    (
        datetime(2026, 9, 15, 12, tzinfo=UTC),
        datetime(2026, 9, 15, 12, 10, tzinfo=UTC),
    ),
)
def test_observation_freshness_includes_zero_and_ten_minute_boundaries(
    evaluated_at: datetime,
) -> None:
    observation = _postgresql_observation()

    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=4,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="b" * 64,
        evaluated_at=evaluated_at,
    )

    assert result.preconditions[8].status == "satisfied"


def test_non_utc_evaluation_time_fails_closed() -> None:
    observation = _postgresql_observation()

    result = _compile_with_evaluated_at(observation, datetime(2026, 9, 15, 12))

    assert result.preconditions[8].status == "unsatisfied"


def test_aware_non_utc_evaluation_time_fails_closed() -> None:
    observation = _postgresql_observation()
    non_utc = datetime.fromisoformat("2026-09-15T05:10:00-07:00")

    result = _compile_with_evaluated_at(observation, non_utc)

    assert result.preconditions[8].status == "unsatisfied"


def test_invalid_constructed_observation_returns_attributable_no_valid_plan() -> None:
    observation = _postgresql_observation()
    invalid_sum = observation.sum_semantics.model_copy(update={"overflow_behavior": "truncate"})
    invalid = observation.model_copy(update={"sum_semantics": invalid_sum})

    result = _compile(_revenue_by_region_iir(), observation=invalid)

    assert result.preconditions[6].status == "unsatisfied"
    assert result.preconditions[6].reason == (
        "provider observation is invalid at sum_semantics.overflow_behavior: "
        "Input should be 'error', 'promote' or 'wrap'"
    )
    assert result.execution_occurred is False


def test_constructed_observation_cannot_coerce_a_binding_revision() -> None:
    observation = _postgresql_observation().model_copy(update={"warehouse_binding_revision": "4"})

    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        result = _compile(_revenue_by_region_iir(), observation=observation)

    assert result.preconditions[6].status == "unsatisfied"
    assert result.preconditions[6].reason.startswith(
        "provider observation is invalid at warehouse_binding_revision:"
    )


def test_observation_serialization_failure_returns_non_sensitive_no_valid_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _postgresql_observation()

    def fail_serialization(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("secret provider detail")

    monkeypatch.setattr(ProductSqlProviderObservation, "model_dump", fail_serialization)
    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest="f" * 64,
    )

    assert result.preconditions[6].reason == (
        "provider observation could not be serialized for strict validation"
    )
    assert "secret" not in result.preconditions[6].reason
    assert result.execution_occurred is False


def test_arbitrary_nonempty_sum_facts_do_not_satisfy_the_engine_profile() -> None:
    observation = _postgresql_observation()
    unsupported = observation.sum_semantics.model_copy(
        update={
            "input_physical_type": "DECIMAL",
            "accumulator_physical_type": "DECIMAL",
            "result_physical_type": "DECIMAL",
        }
    )

    result = _compile(
        _revenue_by_region_iir(),
        observation=observation.model_copy(update={"sum_semantics": unsupported}),
    )

    assert result.preconditions[10].status == "unsatisfied"


@pytest.mark.parametrize(
    "changed",
    (
        {"null_input_behavior": "include"},
        {"empty_group_behavior": "zero"},
    ),
)
def test_each_sum_null_semantic_is_independently_required(changed: dict[str, object]) -> None:
    observation = _postgresql_observation()
    unsafe = observation.sum_semantics.model_copy(update=changed)

    result = _compile(
        _revenue_by_region_iir(),
        observation=observation.model_copy(update={"sum_semantics": unsafe}),
    )

    assert result.preconditions[10].status == "unsatisfied"


@pytest.mark.parametrize(
    "case_kind",
    (
        "relation-name",
        "column-count",
        "column-name",
        "logical-type",
        "nullability",
        "physical-type",
        "collation",
        "decimal-scale",
    ),
)
def test_physical_relation_observation_must_match_every_declared_property(
    case_kind: str,
) -> None:
    observation = _postgresql_observation()
    changes: dict[str, dict[str, object]] = {
        "relation-name": {"relation_name": "other_events"},
        "column-count": {"columns": (observation.columns[0],)},
        "column-name": {
            "columns": (
                observation.columns[0].model_copy(update={"name": "territory"}),
                observation.columns[1],
            )
        },
        "logical-type": {
            "columns": (
                observation.columns[0].model_copy(update={"logical_type": "decimal"}),
                observation.columns[1],
            )
        },
        "nullability": {
            "columns": (
                observation.columns[0].model_copy(update={"nullable": True}),
                observation.columns[1],
            )
        },
        "physical-type": {
            "columns": (
                observation.columns[0].model_copy(update={"physical_type": "VARCHAR"}),
                observation.columns[1],
            )
        },
        "collation": {
            "columns": (
                observation.columns[0].model_copy(update={"collation": "en_US"}),
                observation.columns[1],
            )
        },
        "decimal-scale": {
            "columns": (
                observation.columns[0],
                observation.columns[1].model_copy(update={"decimal_scale": 8}),
            )
        },
    }
    observation = observation.model_copy(update=changes[case_kind])

    result = _compile(_revenue_by_region_iir(), observation=observation)

    assert result.preconditions[9].status == "unsatisfied"


def test_physical_decimal_and_sum_overflow_semantics_fail_closed() -> None:
    observation = _postgresql_observation()
    unsafe_column = observation.columns[1].model_copy(
        update={"decimal_precision": 18, "physical_type": "NUMERIC(18,2)"}
    )
    unsafe_sum = observation.sum_semantics.model_copy(update={"overflow_behavior": "truncate"})
    changed = observation.model_copy(
        update={"columns": (observation.columns[0], unsafe_column), "sum_semantics": unsafe_sum}
    )

    result = _compile(_revenue_by_region_iir(), observation=changed)

    assert result.preconditions[9].status == "unsatisfied"
    assert result.preconditions[10].status == "unsatisfied"
    assert result.execution_occurred is False


def test_candidate_semantic_failure_preserves_attributable_counterfactual() -> None:
    product = _revenue_by_region_iir()
    aggregate = product.operations[1]
    assert isinstance(aggregate, AggregateOperation)
    invalid = aggregate.model_copy(
        update={
            "measures": (
                AggregateMeasure(
                    function="sum",
                    output_name="total_revenue",
                    argument=ColumnReference(
                        relation_alias="revenue_events", column_name="missing"
                    ),
                ),
            )
        }
    )

    result = compile_product_iir(
        product.model_copy(update={"operations": (product.operations[0], invalid)}),
        engine="postgresql",
        engine_version=_POSTGRESQL_VERSION,
        capabilities=_CAPABILITIES,
    )

    assert len(result.preconditions) == 1
    assert result.preconditions[0].number == 1
    assert result.preconditions[0].reason == ("undeclared column reference: revenue_events.missing")
    assert result.smallest_changes == (
        "declare the referenced relation and column with its scalar type",
    )


@pytest.mark.parametrize(
    "hostile_identifier",
    (
        'region";drop_table',
        "region--comment",
        "region/*comment*/",
        "region%wildcard",
        "region_confus\u0430ble",
    ),
)
def test_identifier_security_rejects_sql_syntax_wildcards_and_confusables(
    hostile_identifier: str,
) -> None:
    with pytest.raises(ValidationError):
        ColumnReference(
            relation_alias="revenue_events",
            column_name=hostile_identifier,
        )


def test_emitter_revalidates_identifiers_after_unsafe_model_copy() -> None:
    product = _revenue_by_region_iir()
    unsafe_source = product.source.model_copy(
        update={"relation_name": 'events"; DROP TABLE events;--'}
    )

    with pytest.raises(ValidationError):
        emit_postgresql(product.model_copy(update={"source": unsafe_source}))


def test_emitter_rejects_a_non_decimal_measure_even_when_model_validation_was_bypassed() -> None:
    product = _revenue_by_region_iir()
    aggregate = product.operations[1]
    assert isinstance(aggregate, AggregateOperation)
    unsafe_aggregate = aggregate.model_copy(
        update={
            "measures": (
                AggregateMeasure(
                    function="sum",
                    output_name="total_revenue",
                    argument=ColumnReference(relation_alias="revenue_events", column_name="region"),
                ),
            )
        }
    )
    project = product.operations[0]
    assert isinstance(project, ProjectOperation)
    unsafe_project = project.model_copy(
        update={
            "expressions": (
                project.expressions[0],
                project.expressions[1].model_copy(
                    update={
                        "expression": ColumnReference(
                            relation_alias="revenue_events", column_name="region"
                        )
                    }
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="sum argument must have numeric type"):
        emit_clickhouse(
            product.model_copy(update={"operations": (unsafe_project, unsafe_aggregate)})
        )


def test_emitter_rejects_a_measure_output_that_collides_with_a_group_output() -> None:
    product = _revenue_by_region_iir()
    aggregate = product.operations[1]
    assert isinstance(aggregate, AggregateOperation)
    colliding = aggregate.model_copy(
        update={"measures": (aggregate.measures[0].model_copy(update={"output_name": "region"}),)}
    )

    with pytest.raises(ValueError, match="keep grouped and measure output names distinct"):
        emit_postgresql(
            product.model_copy(update={"operations": (product.operations[0], colliding)})
        )

    result = _compile(product.model_copy(update={"operations": (product.operations[0], colliding)}))
    assert result.preconditions[5].reason == "keep grouped and measure output names distinct"
    assert result.preconditions[5].status == "unsatisfied"


def test_emitter_never_invents_an_aggregate_function() -> None:
    product = _revenue_by_region_iir()
    aggregate = product.operations[1]
    assert isinstance(aggregate, AggregateOperation)
    unsupported = aggregate.measures[0].model_copy(update={"function": "average"})
    changed = product.model_copy(
        update={
            "operations": (
                product.operations[0],
                aggregate.model_copy(update={"measures": (unsupported,)}),
            )
        }
    )

    with pytest.raises(ValueError, match="project only the aggregate"):
        emit_postgresql(changed)


def test_projected_measure_alias_cannot_be_silently_ignored() -> None:
    product = _revenue_by_region_iir()
    project = product.operations[0]
    assert isinstance(project, ProjectOperation)
    renamed = project.model_copy(
        update={
            "expressions": (
                project.expressions[0],
                project.expressions[1].model_copy(update={"output_name": "net_revenue"}),
            )
        }
    )
    changed = product.model_copy(update={"operations": (renamed, product.operations[1])})

    result = _compile(changed)

    assert result.preconditions[1].status == "unsatisfied"
    with pytest.raises(ValueError, match="project only the aggregate"):
        emit_postgresql(changed)


def test_shape_evaluation_rejects_a_reference_outside_the_declared_source_alias() -> None:
    product = _revenue_by_region_iir()
    foreign_region = ColumnReference(relation_alias="other_events", column_name="region")
    project = product.operations[0]
    aggregate = product.operations[1]
    assert isinstance(project, ProjectOperation)
    assert isinstance(aggregate, AggregateOperation)
    changed_project = project.model_copy(
        update={
            "expressions": (
                project.expressions[0].model_copy(update={"expression": foreign_region}),
                project.expressions[1],
            )
        }
    )
    changed_aggregate = aggregate.model_copy(update={"group_by": (foreign_region,)})
    changed = product.model_copy(
        update={
            "operations": (changed_project, changed_aggregate),
            "grain": (foreign_region,),
        }
    )

    checks = evaluate_project_sum_shape(changed)

    assert checks[4].satisfied is False


def test_engine_fixtures_cover_positive_and_negative_provider_pairs() -> None:
    paths = sorted(_FIXTURE_DIRECTORY.glob("*-project-sum-*.json"))

    assert len(paths) == 4
    observed: set[tuple[str, str]] = set()
    for path in paths:
        payload = json.loads(path.read_bytes())
        engine = payload["engine"]
        product = ProductIntentIR.model_validate_json(
            json.dumps(payload["product_iir"]), strict=True
        )
        observation = ProductSqlProviderObservation.model_validate_json(
            json.dumps(payload["provider_observation"]), strict=True
        )
        result = compile_product_iir(
            product,
            engine=engine,
            provider_observation=observation,
            expected_provider_observation_digest=payload["provider_observation_digest"],
            expected_tenant_id=payload["expected_tenant_id"],
            expected_warehouse_binding_id=payload["expected_warehouse_binding_id"],
            expected_warehouse_binding_revision=payload["expected_warehouse_binding_revision"],
            expected_relation_ref=payload["expected_relation_ref"],
            expected_relation_namespace=payload["expected_relation_namespace"],
            expected_engine_image_digest=payload["expected_engine_image_digest"],
            expected_engine_build_digest=payload["expected_engine_build_digest"],
            evaluated_at=datetime.fromisoformat(payload["evaluated_at"]),
        )

        assert isinstance(result, NoValidPlan)
        assert [
            item.number for item in result.preconditions if item.status != "satisfied"
        ] == payload["expected_failed_preconditions"]
        observed.add((engine, payload["case_kind"]))
        if payload["case_kind"] == "positive_candidate":
            emitted = (
                emit_postgresql(product) if engine == "postgresql" else emit_clickhouse(product)
            )
            assert emitted.statement == payload["expected_statement"]

    assert observed == {
        ("postgresql", "positive_candidate"),
        ("postgresql", "negative_nullable"),
        ("clickhouse", "positive_candidate"),
        ("clickhouse", "negative_nullable"),
    }


def test_rule_declaration_pins_narrow_constructs_and_review_gate() -> None:
    rule = json.loads(_RULE_PATH.read_bytes())

    assert rule["rule_id"] == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert rule["review_status"] == "changes_requested"
    assert rule["observation_schema_version"] == "1"
    assert rule["unsatisfied_gates"] == [
        "authoritative_generation_addressing",
        "authority_bound_cardinality",
        "runtime_result_magnitude_enforcement",
        "provider_owned_provenance",
        "live_cross_engine_checked_sum_review",
        "independent_review",
    ]
    assert rule["physical_source_requirements"] == {
        "authoritative_generation_count": 1,
        "generation_identifier_format": "sha256",
        "landing_receipt_digest_required": True,
        "status": "candidate_implemented_not_admitted",
    }
    assert rule["decimal_sum_bound"]["maximum_contributing_rows"] == (2**63) - 1
    assert rule["engine_profiles"]["postgresql"]["checked_sum_result"] == "NUMERIC"
    assert rule["engine_profiles"]["clickhouse"]["checked_sum_accumulator"] == ("Decimal(76, 9)")
    assert rule["engine_profiles"]["clickhouse"]["outer_cast_enforces_magnitude"] is False
    assert rule["constructs"] == ["direct_column_project", "explicit_single_decimal_sum_group"]
    assert rule["excluded_constructs"] == [
        "filter",
        "join",
        "deduplicate",
        "arithmetic",
        "case",
        "function",
        "multiple_measures",
    ]


def _compile(
    product: ProductIntentIR,
    *,
    engine_version: str = _POSTGRESQL_VERSION,
    capabilities: tuple[str, ...] = _CAPABILITIES,
    observation: ProductSqlProviderObservation | None = None,
    expected_observation_digest: str | None = None,
) -> NoValidPlan:
    expected_digest = (
        expected_observation_digest
        if expected_observation_digest is not None
        else digest(observation)
        if observation is not None
        else None
    )
    result = compile_product_iir(
        product,
        engine="postgresql",
        engine_version=engine_version,
        capabilities=capabilities,
        provider_observation=observation,
        expected_provider_observation_digest=expected_digest,
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=4,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="b" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 10, tzinfo=UTC),
    )
    assert isinstance(result, NoValidPlan)
    return result


def _compile_with_evaluated_at(
    observation: ProductSqlProviderObservation, evaluated_at: datetime
) -> NoValidPlan:
    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=4,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="b" * 64,
        evaluated_at=evaluated_at,
    )
    assert isinstance(result, NoValidPlan)
    return result


def _signed_observation(
    observation: ProductSqlProviderObservation,
) -> tuple[SignedProductSqlProviderObservation, ProductSqlProviderObservationVerifier]:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(observation)
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=10),
    )
    return signed, verifier


def _compile_signed(
    signed_observation: SignedProductSqlProviderObservation,
    *,
    verifier: ProductSqlProviderObservationVerifier | None,
    expected_observation_digest: str | None = None,
    evaluated_at: datetime = datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
) -> NoValidPlan:
    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine="postgresql",
        signed_provider_observation=signed_observation,
        provider_observation_verifier=verifier,
        expected_provider_observation_digest=(
            expected_observation_digest
            if expected_observation_digest is not None
            else signed_observation.observation_digest
        ),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=4,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="b" * 64,
        evaluated_at=evaluated_at,
    )
    assert isinstance(result, NoValidPlan)
    return result


def _postgresql_observation() -> ProductSqlProviderObservation:
    return ProductSqlProviderObservation(
        observation_id="product-sql-observation-1",
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=4,
        relation_ref="relation-revenue-events-v1",
        relation_namespace="raw",
        relation_name="revenue_events",
        engine="postgresql",
        engine_version="18.6",
        engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        engine_build_digest="b" * 64,
        observed_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        columns=(
            ProductSqlColumnObservation(
                name="region",
                logical_type="string",
                physical_type="TEXT",
                nullable=False,
                decimal_precision=None,
                decimal_scale=None,
                collation="C",
                encoding="UTF8",
            ),
            ProductSqlColumnObservation(
                name="revenue",
                logical_type="decimal",
                physical_type="NUMERIC(38,9)",
                nullable=False,
                decimal_precision=38,
                decimal_scale=9,
                collation=None,
                encoding=None,
            ),
        ),
        sum_semantics=ProductSqlSumSemantics(
            input_physical_type="NUMERIC(38,9)",
            accumulator_physical_type="INTERNAL",
            result_physical_type="NUMERIC",
            overflow_behavior="promote",
            null_input_behavior="exclude",
            empty_group_behavior="no_row",
        ),
    )


def _revenue_by_region_iir() -> ProductIntentIR:
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


@pytest.mark.parametrize(
    ("engine", "expected_reason"),
    (
        (
            "postgresql",
            "review live checked SUM on the pinned PostgreSQL engine; "
            "cross-engine equivalence is not claimed",
        ),
        (
            "clickhouse",
            "activate this rule for ClickHouse with its own live checked SUM evidence and review",
        ),
    ),
)
def test_live_sum_review_is_worded_per_engine_and_stays_unsatisfied(
    engine: Literal["postgresql", "clickhouse"], expected_reason: str
) -> None:
    """PostgreSQL activation withholds the cross-engine claim and never admits ClickHouse.

    Precondition 17 stays unsatisfied on both engines: satisfying it is the independent
    reviewer's decision, not something live evidence or the compiler can grant.
    """
    result = compile_product_iir(
        _revenue_by_region_iir(),
        engine=engine,
        engine_version="0",
        capabilities=_CAPABILITIES,
    )

    review = next(item for item in result.preconditions if item.number == 17)
    assert review.reason == expected_reason
    assert review.status == "unsatisfied"
    assert "both pinned engines" not in review.reason
