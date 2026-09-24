from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import digest
from heinzel_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    DbtFailureClassification,
    DbtInvocationAuthority,
    DbtInvocationError,
    DbtInvocationReceipt,
    DbtInvoker,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from heinzel_provider_clickhouse import (
    ClickHouseMaterializationSettings,
    ClickHouseMaterializationWarehouse,
    ClickHouseMaterializedColumn,
    ClickHouseProductSqlObservationSettings,
    clickhouse_materialized_schema_digest,
)
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import MaterializationRequest
from pydantic import SecretStr

_NOW = datetime(2026, 9, 15, 23, tzinfo=UTC)
_UUID = "22222222-2222-4222-8222-222222222222"
_CREATE_QUERY = (
    "CREATE TABLE product.product_revenue_g3 (`region` String, `total_revenue` Decimal(57, 9)) "
    "ENGINE = MergeTree ORDER BY region"
)
_COLUMNS = (
    ClickHouseMaterializedColumn(ordinal=1, name="region", data_type="String"),
    ClickHouseMaterializedColumn(ordinal=2, name="total_revenue", data_type="Decimal(57, 9)"),
)
_COMPILER_PRIVATE_KEY = Ed25519PrivateKey.generate()


class _Response:
    status_code = 200
    exception_code: int | None = None

    def __init__(self, lines: tuple[str, ...] = ()) -> None:
        self.lines = lines


class _Transport:
    def __init__(self, *, engine: str = "MergeTree", lose_seal_response: bool = False) -> None:
        self.engine = engine
        self.lose_seal_response = lose_seal_response
        self.sealed = False
        self.calls: list[str] = []

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> _Response:
        statement = content.decode()
        self.calls.append(statement)
        if statement.startswith("SELECT count() FROM system.tables"):
            return _Response(("0",))
        if statement.startswith("SELECT uuid, engine, create_table_query FROM system.tables"):
            return _Response((f"{_UUID}\t{self.engine}\t{_CREATE_QUERY}",))
        if statement.startswith("SELECT position, name, type FROM system.columns"):
            return _Response(("1\tregion\tString", "2\ttotal_revenue\tDecimal(57, 9)"))
        if statement.startswith("SELECT count() FROM `product`.`product_revenue_g3`"):
            return _Response(("2",))
        if statement.startswith("SELECT name FROM system.columns"):
            return _Response(("total_revenue",))
        if statement.startswith("SELECT countIf"):
            return _Response(("0",))
        if statement.startswith("REVOKE"):
            self.sealed = True
            if self.lose_seal_response:
                self.lose_seal_response = False
                raise TimeoutError("response lost after ClickHouse committed the revoke")
            return _Response()
        if statement.startswith("CHECK GRANT SELECT"):
            return _Response(("1",))
        if statement.startswith("CHECK GRANT"):
            return _Response(("0" if self.sealed else "1",))
        if statement.startswith("SELECT access_type, database, table, is_partial_revoke"):
            if not self.sealed:
                return _Response()
            return _Response(
                (
                    "INSERT\tproduct\tproduct_revenue_g3\t1",
                    "ALTER TABLE\tproduct\tproduct_revenue_g3\t1",
                    "ALTER VIEW\tproduct\tproduct_revenue_g3\t1",
                    "DROP TABLE\tproduct\tproduct_revenue_g3\t1",
                )
            )
        raise AssertionError(f"unexpected statement: {statement}")


class _Invoker:
    def __init__(self, *, error: DbtInvocationError | None = None) -> None:
        self.error = error
        self.calls: list[DbtInvocationAuthority] = []

    def invoke(
        self, *, signed_model: SignedCompiledDbtModel, authority: DbtInvocationAuthority
    ) -> DbtInvocationReceipt:
        self.calls.append(authority)
        if self.error is not None:
            raise self.error
        return _dbt_receipt(signed_model)


def _signed_model() -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="product_revenue_g3",
        contract_digest="1" * 64,
        provider="clickhouse",
        input_generation_digests=("2" * 64,),
        target_schema="product",
        output_columns=("region", "total_revenue"),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT region, total_revenue FROM raw.revenue",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-1",
        signature=base64.b64encode(
            _COMPILER_PRIVATE_KEY.sign(compiled_dbt_model_signing_bytes(model))
        ).decode(),
    )


def _dbt_receipt(signed_model: SignedCompiledDbtModel) -> DbtInvocationReceipt:
    return DbtInvocationReceipt(
        model_digest=signed_model.model_digest,
        contract_digest=signed_model.model.contract_digest,
        provider="clickhouse",
        input_generation_digests=signed_model.model.input_generation_digests,
        dbt_version="1.10.13",
        manifest_digest="3" * 64,
        run_results_digest="4" * 64,
        lineage_digest="5" * 64,
        quality_assertion_count=1,
        quality_disposition="passed",
    )


def _settings() -> ClickHouseMaterializationSettings:
    return ClickHouseMaterializationSettings(
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        endpoint="https://clickhouse.test",
        administration_username="administrator",
        administration_password=SecretStr("private-admin"),
        transformation_username="transformer",
        transformation_password=SecretStr("private-transform"),
        transformation_role="transformation_runtime",
    )


def _physical_plan() -> ProductPhysicalPlan:
    statement = "SELECT region, total_revenue FROM raw.revenue"
    return ProductPhysicalPlan(
        compiler_version="compiler-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        contract_ref="contract-a",
        contract_revision=1,
        contract_digest="1" * 64,
        iir_digest="7" * 64,
        provider="clickhouse",
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        provider_observation_digest="8" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="revenue",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="9" * 64,
            landing_receipt_digest="a" * 64,
            observed_source_schema_digest="b" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="total_revenue",
                    json_field="total_revenue",
                    scalar_type="decimal",
                ),
            ),
        ),
        target=ProductTarget(namespace="product", relation_name="product_revenue_g3"),
        emitted_statement=statement,
        statement_digest=digest(statement),
        output_columns=("region", "total_revenue"),
        expected_output_schema_digest=clickhouse_materialized_schema_digest(_COLUMNS),
        decimal_output_checks=(Decimal57OutputCheck(column_name="total_revenue"),),
    )


def _request(signed_model: SignedCompiledDbtModel, **updates: object) -> MaterializationRequest:
    physical_plan = _physical_plan()
    values: dict[str, object] = {
        "run_id": "run-a",
        "tenant_id": "tenant-a",
        "product_id": "product-revenue",
        "product_revision": 1,
        "product_generation": 3,
        "retention_seconds": 3600,
        "contract_digest": signed_model.model.contract_digest,
        "physical_plan": physical_plan,
        "physical_plan_digest": digest(physical_plan),
        "compiled_model_digest": signed_model.model_digest,
        "input_generation_digests": signed_model.model.input_generation_digests,
        "input_cardinality_evidence_digest": "6" * 64,
        "expected_output_schema_digest": clickhouse_materialized_schema_digest(_COLUMNS),
    }
    values.update(updates)
    return MaterializationRequest.model_validate(values)


def _warehouse(
    signed_model: SignedCompiledDbtModel,
    transport: _Transport,
    invoker: _Invoker,
) -> ClickHouseMaterializationWarehouse:
    observation_settings = ClickHouseProductSqlObservationSettings(
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        endpoint="https://clickhouse.test",
        username="observer",
        password=SecretStr("private-observer"),
    )
    return ClickHouseMaterializationWarehouse(
        settings=_settings(),
        observation_settings=observation_settings,
        signed_model=signed_model,
        trusted_compiler_keys={"compiler-1": _COMPILER_PRIVATE_KEY.public_key()},
        invoker=cast(DbtInvoker, invoker),
        transport=transport,
        clock=lambda: _NOW,
    )


def test_execute_binds_physical_identity_schema_dbt_magnitude_and_seal() -> None:
    signed_model = _signed_model()
    transport = _Transport()
    invoker = _Invoker()
    warehouse = _warehouse(signed_model, transport, invoker)

    commit = warehouse.materialize(_request(signed_model))
    observation = commit.runtime_observation()

    assert commit.relation_uuid == _UUID
    assert commit.normalized_create_query == _CREATE_QUERY.replace("`", "")
    assert commit.columns == _COLUMNS
    assert commit.dbt_receipt == _dbt_receipt(signed_model)
    assert commit.magnitude_observation.results[0].declaration == DbtDecimalMagnitudeCheck(
        column_name="total_revenue"
    )
    assert commit.sealed_write_privileges == ("ALTER", "DROP TABLE", "INSERT")
    assert commit.exact_partial_revokes == (
        "INSERT",
        "ALTER TABLE",
        "ALTER VIEW",
        "DROP TABLE",
    )
    assert commit.commit_reference == digest(
        commit.model_dump(exclude={"commit_reference"}, mode="python")
    )
    assert observation.output_schema_digest == clickhouse_materialized_schema_digest(_COLUMNS)
    assert observation.output_row_count == 2
    assert observation.dbt_manifest_digest == "3" * 64
    assert observation.dbt_run_results_digest == "4" * 64
    assert observation.lineage_digest == "5" * 64
    assert observation.quality_assertion_count == 1
    assert observation.quality_disposition == "passed"
    assert len(invoker.calls) == 1
    assert invoker.calls[0] == DbtInvocationAuthority(
        contract_digest="1" * 64,
        provider="clickhouse",
        input_generation_digests=("2" * 64,),
    )
    assert transport.sealed is True
    revoke_index = next(
        index for index, statement in enumerate(transport.calls) if statement.startswith("REVOKE")
    )
    magnitude_index = next(
        index
        for index, statement in enumerate(transport.calls)
        if statement.startswith("SELECT countIf")
    )
    assert magnitude_index < revoke_index


def test_execute_reconciles_an_ambiguous_seal_without_repeating_it() -> None:
    signed_model = _signed_model()
    transport = _Transport(lose_seal_response=True)
    warehouse = _warehouse(signed_model, transport, _Invoker())

    observation = warehouse.execute(_request(signed_model))

    assert len(observation.provider_commit_reference) == 64
    assert sum(statement.startswith("REVOKE") for statement in transport.calls) == 1
    assert transport.sealed is True


def test_execute_rejects_non_mergetree_output_before_sealing() -> None:
    signed_model = _signed_model()
    transport = _Transport(engine="Memory")
    warehouse = _warehouse(signed_model, transport, _Invoker())

    with pytest.raises(ProviderError) as captured:
        warehouse.execute(_request(signed_model))

    assert captured.value.classification == "integrity_failure"
    assert not any(statement.startswith("REVOKE") for statement in transport.calls)


def test_execute_rejects_mismatched_authority_before_dbt_or_network_access() -> None:
    signed_model = _signed_model()
    transport = _Transport()
    invoker = _Invoker()
    warehouse = _warehouse(signed_model, transport, invoker)

    with pytest.raises(ProviderError) as captured:
        warehouse.execute(_request(signed_model, tenant_id="tenant-b"))

    assert captured.value.classification == "authorization_denied"
    assert invoker.calls == []
    assert transport.calls == []


def test_execute_rejects_a_physical_plan_for_another_binding_before_dbt() -> None:
    signed_model = _signed_model()
    transport = _Transport()
    invoker = _Invoker()
    warehouse = _warehouse(signed_model, transport, invoker)
    other_plan = _physical_plan().model_copy(update={"warehouse_binding_id": "warehouse-2"})

    with pytest.raises(ProviderError) as captured:
        warehouse.execute(
            _request(
                signed_model,
                physical_plan=other_plan,
                physical_plan_digest=digest(other_plan),
            )
        )

    assert captured.value.classification == "authorization_denied"
    assert invoker.calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    ("dbt_classification", "expected"),
    [
        (DbtFailureClassification.INVALID_SIGNATURE, "integrity_failure"),
        (DbtFailureClassification.AUTHORITY_MISMATCH, "authorization_denied"),
        (DbtFailureClassification.MALFORMED_OUTPUT, "invalid_provider_response"),
        (DbtFailureClassification.INVOCATION_FAILED, "ambiguous_outcome"),
        (DbtFailureClassification.NONZERO_EXIT, "ambiguous_outcome"),
    ],
)
def test_execute_preserves_dbt_failure_classification(
    dbt_classification: DbtFailureClassification, expected: str
) -> None:
    signed_model = _signed_model()
    invoker = _Invoker(error=DbtInvocationError(dbt_classification, "private detail"))
    warehouse = _warehouse(signed_model, _Transport(), invoker)

    with pytest.raises(ProviderError) as captured:
        warehouse.execute(_request(signed_model))

    assert captured.value.classification == expected
    assert "private detail" not in str(captured.value)


def test_switch_fails_closed_because_runtime_observation_omits_exact_clickhouse_commit() -> None:
    signed_model = _signed_model()
    transport = _Transport()
    warehouse = _warehouse(signed_model, transport, _Invoker())
    request = _request(signed_model)
    observation = warehouse.execute(request)

    with pytest.raises(ProviderError) as captured:
        warehouse.switch_consumption_view(request, observation)

    assert captured.value.classification == "permanent_configuration"
    assert not any(statement.startswith("CREATE OR REPLACE VIEW") for statement in transport.calls)
