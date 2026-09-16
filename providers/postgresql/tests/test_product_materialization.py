from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import psycopg
import pytest
from pillarmesh_contract_model import digest
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    DbtInvocationReceipt,
    DbtInvoker,
    SignedCompiledDbtModel,
)
from pillarmesh_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from pillarmesh_provider_postgresql import (
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLProductGenerationAuthority,
)
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_runtime import (
    AnswerProductGenerationReference,
    AnswerQueryReference,
    MaterializationRequest,
    ProductInputCardinalityEvidence,
    ProductInputReceiptCardinality,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from pydantic import SecretStr, ValidationError

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _input_cardinality_evidence_digest(
    *,
    contract_digest: str,
    plan_digest: str,
    input_generation_digests: tuple[str, ...],
) -> str:
    receipts = tuple(
        ProductInputReceiptCardinality(
            generation_id=digest(
                {
                    "domain": "postgresql-materialization-test-generation-v1",
                    "ordinal": ordinal,
                }
            ),
            receipt_digest=input_generation_digest,
            record_count=1,
        )
        for ordinal, input_generation_digest in enumerate(input_generation_digests)
    )
    total = sum(receipt.record_count for receipt in receipts)
    evidence = ProductInputCardinalityEvidence(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=1,
        contract_digest=contract_digest,
        product_plan_digest=plan_digest,
        relation_ref="orders",
        generation_ids=tuple(receipt.generation_id for receipt in receipts),
        receipts=receipts,
        total_contributing_row_ceiling=total,
        policy_maximum_contributing_rows=100,
        maximum_scaled_sum=total * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=_NOW,
    )
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    evidence_digest = repository.record(evidence)
    assert (
        repository.read(
            tenant_id=evidence.tenant_id,
            evidence_digest=evidence_digest,
        )
        == evidence
    )
    return evidence_digest


class _PreflightCursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...]:
        return self._row

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _PreflightConnection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _PreflightCursor:
        del query, params
        return _PreflightCursor(self._row)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _DbtMustNotRun:
    def invoke(self, **_kwargs: object) -> object:
        raise AssertionError("dbt must not overwrite a retained product generation")


class _TargetLockConnection:
    def __init__(self) -> None:
        self.calls = 0
        self.locked = False

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _PreflightCursor:
        del query, params
        self.calls += 1
        if self.calls == 1:
            self.locked = True
        elif self.calls == 4:
            assert self.locked
            self.locked = False
        return _PreflightCursor((False, False) if self.calls == 3 else ())

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _InspectCursor:
    def __init__(
        self, *, row: tuple[object, ...] | None = None, rows: list[tuple[object, ...]] | None = None
    ) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _InspectConnection:
    def __init__(
        self,
        lock_connection: _TargetLockConnection,
        *,
        magnitude_row: tuple[object, ...] | None = (0,),
        magnitude_error: psycopg.Error | None = None,
        magnitude_values: tuple[Decimal, ...] | None = None,
        columns: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.calls = 0
        self._lock_connection = lock_connection
        self._magnitude_row = magnitude_row
        self._magnitude_error = magnitude_error
        self._magnitude_values = magnitude_values
        self._columns = columns or [(1, "region", "text", "NO")]
        self.statements: list[tuple[object, tuple[object, ...]]] = []

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _InspectCursor:
        assert self._lock_connection.locked
        self.statements.append((query, params))
        self.calls += 1
        if self.calls == 1:
            return _InspectCursor(rows=self._columns)
        if self.calls == 2:
            return _InspectCursor(row=(1,))
        if self.calls == 3:
            return _InspectCursor(row=("16425", "16425"))
        if self._magnitude_error is not None:
            raise self._magnitude_error
        if self._magnitude_values is not None:
            lower_bound, upper_bound = params
            assert isinstance(lower_bound, Decimal)
            assert isinstance(upper_bound, Decimal)
            return _InspectCursor(
                row=(
                    sum(
                        value <= lower_bound or value >= upper_bound
                        for value in self._magnitude_values
                    ),
                )
            )
        return _InspectCursor(row=self._magnitude_row)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _GenerationAuthorityConnection:
    def __init__(
        self,
        *,
        generation_row: tuple[object, ...] | None,
        pointer_row: tuple[object, ...] | None,
    ) -> None:
        self._rows = iter((generation_row, pointer_row))

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _InspectCursor:
        del query, params
        return _InspectCursor(row=next(self._rows))

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _LockAssertingInvoker:
    def __init__(self, connection: _TargetLockConnection) -> None:
        self._connection = connection

    def invoke(self, **_kwargs: object) -> DbtInvocationReceipt:
        assert self._connection.locked
        return DbtInvocationReceipt(
            model_digest="3" * 64,
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            dbt_version="1.10.13",
            manifest_digest="4" * 64,
            run_results_digest="5" * 64,
            lineage_digest="6" * 64,
            quality_assertion_count=0,
            quality_disposition="not_asserted",
        )


class _SwitchMutationConnection:
    def __init__(self) -> None:
        self.calls = 0
        self.statements: list[tuple[object, tuple[object, ...]]] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _InspectCursor:
        self.statements.append((query, params))
        self.calls += 1
        if self.calls <= 3:
            return _InspectCursor(row=())
        if self.calls == 4:
            return _InspectCursor(rows=[(1, "total_revenue", "numeric", "NO")])
        if self.calls == 5:
            return _InspectCursor(row=(1,))
        if self.calls == 6:
            return _InspectCursor(row=("16425", "16425"))
        if self.calls == 7:
            return _InspectCursor(row=(1,))
        raise AssertionError("publication effect must not run after magnitude mutation")

    def commit(self) -> None:
        raise AssertionError("mutated output must not commit")

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_postgresql_materialization_boundary_is_concrete() -> None:
    assert PostgreSQLMaterializationSettings is not None
    assert PostgreSQLMaterializationWarehouse is not None


def test_generation_authority_classifies_driver_failure_without_leaking_details() -> None:
    settings = PostgreSQLMaterializationSettings(
        tenant_id="tenant-a",
        dsn=SecretStr("postgresql://materialization:private@localhost/database"),
        consumption_schema_name="consumption",
        consumption_view_name="product_revenue",
        control_schema_name="product_control",
        generation_table_name="product_generations",
        generation_pointer_table_name="product_generation_pointers",
    )
    authority = PostgreSQLProductGenerationAuthority(
        settings,
        connect=lambda _dsn: (_ for _ in ()).throw(psycopg.OperationalError("private")),
    )
    reference = AnswerProductGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product-revenue", version=1, digest="1" * 64),
        generation=1,
    )

    with pytest.raises(ProviderError) as caught:
        authority.observe(reference)

    assert caught.value.classification == "transient_transport"
    assert "private" not in str(caught.value)


def _generation_reference() -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product-revenue", version=1, digest="1" * 64),
        generation=7,
    )


def _generation_authority(
    connection: _GenerationAuthorityConnection,
) -> PostgreSQLProductGenerationAuthority:
    return PostgreSQLProductGenerationAuthority(
        PostgreSQLMaterializationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr("postgresql://unused"),
            consumption_schema_name="consumption",
            consumption_view_name="product_revenue",
            control_schema_name="product_control",
            generation_table_name="product_generations",
            generation_pointer_table_name="product_generation_pointers",
        ),
        connect=lambda _dsn: connection,
    )


def test_generation_authority_does_not_invent_a_missing_pointer() -> None:
    state = _generation_authority(
        _GenerationAuthorityConnection(generation_row=(True,), pointer_row=None)
    ).observe(_generation_reference())

    assert state.addressable is True
    assert state.current_generation is None


def test_generation_authority_rejects_missing_pointer_for_unaddressable_generation() -> None:
    authority = _generation_authority(
        _GenerationAuthorityConnection(generation_row=(False,), pointer_row=None)
    )

    with pytest.raises(ProviderError) as caught:
        authority.observe(_generation_reference())

    assert caught.value.classification == "integrity_failure"


@pytest.mark.parametrize(
    ("generation_row", "pointer_row"),
    [
        ((1,), (7,)),
        ((True,), ("7",)),
        ((True,), (True,)),
        ((True,), (7, "unexpected")),
    ],
)
def test_generation_authority_rejects_malformed_provider_state(
    generation_row: tuple[object, ...], pointer_row: tuple[object, ...]
) -> None:
    authority = _generation_authority(
        _GenerationAuthorityConnection(
            generation_row=generation_row,
            pointer_row=pointer_row,
        )
    )

    with pytest.raises(ProviderError) as caught:
        authority.observe(_generation_reference())

    assert caught.value.classification == "invalid_provider_response"


def _physical_plan(
    signed_model: SignedCompiledDbtModel,
    *,
    expected_output_schema_digest: str,
) -> ProductPhysicalPlan:
    model = signed_model.model
    return ProductPhysicalPlan(
        compiler_version="compiler-test-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id="tenant-a",
        product_id="orders",
        product_revision=1,
        contract_ref="contract-a",
        contract_revision=1,
        contract_digest=model.contract_digest,
        iir_digest="8" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=1,
        provider_observation_digest="9" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="raw_orders",
            generation_column="generation_id",
            payload_column="payload",
            generation_id=digest("postgresql-materialization-test-source-generation"),
            landing_receipt_digest=model.input_generation_digests[0],
            observed_source_schema_digest="3" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="total_revenue", json_field="revenue", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(
            namespace=model.target_schema,
            relation_name=model.model_name,
        ),
        emitted_statement=model.compiled_sql,
        statement_digest=digest(model.compiled_sql),
        output_columns=model.output_columns,
        expected_output_schema_digest=expected_output_schema_digest,
        decimal_output_checks=tuple(
            Decimal57OutputCheck(column_name=check.column_name)
            for check in model.output_magnitude_checks
        ),
    )


def _materialization_request(
    signed_model: SignedCompiledDbtModel,
    *,
    compiled_model_digest: str,
    expected_output_schema_digest: str,
) -> MaterializationRequest:
    model = signed_model.model
    physical_plan = _physical_plan(
        signed_model, expected_output_schema_digest=expected_output_schema_digest
    )
    return MaterializationRequest(
        run_id="run-a",
        tenant_id="tenant-a",
        product_id="orders",
        product_revision=1,
        product_generation=1,
        retention_seconds=60,
        contract_digest=model.contract_digest,
        physical_plan=physical_plan,
        physical_plan_digest=digest(physical_plan),
        compiled_model_digest=compiled_model_digest,
        input_generation_digests=model.input_generation_digests,
        input_cardinality_evidence_digest=_input_cardinality_evidence_digest(
            contract_digest=model.contract_digest,
            plan_digest=digest(physical_plan),
            input_generation_digests=model.input_generation_digests,
        ),
        expected_output_schema_digest=expected_output_schema_digest,
    )


@pytest.mark.parametrize("preflight_row", [(True, True), (False, True)])
def test_materialization_refuses_an_existing_target_before_dbt_runs(
    preflight_row: tuple[object, ...],
) -> None:
    model = CompiledDbtModel(
        model_name="orders",
        contract_digest="1" * 64,
        provider="postgresql",
        input_generation_digests=("2" * 64,),
        target_schema="contract_" + "1" * 54,
        output_columns=("total_revenue",),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT 1 AS total_revenue",
    )
    signed = SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler",
        signature="unused-before-dbt",
    )
    warehouse = PostgreSQLMaterializationWarehouse(
        settings=PostgreSQLMaterializationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr("postgresql://unused"),
            consumption_schema_name="consumption",
            consumption_view_name="orders",
            control_schema_name="control",
            generation_table_name="generations",
            generation_pointer_table_name="pointers",
        ),
        signed_model=signed,
        invoker=cast(DbtInvoker, _DbtMustNotRun()),
        connect=lambda _dsn: _PreflightConnection(preflight_row),
    )
    request = _materialization_request(
        signed,
        compiled_model_digest=digest(model),
        expected_output_schema_digest="3" * 64,
    )

    with pytest.raises(ProviderError) as caught:
        warehouse.execute(request)

    assert caught.value.classification == "integrity_failure"


def test_materialization_holds_the_target_lock_through_dbt_and_output_inspection() -> None:
    model = CompiledDbtModel(
        model_name="orders",
        contract_digest="1" * 64,
        provider="postgresql",
        input_generation_digests=("2" * 64,),
        target_schema="contract_" + "1" * 54,
        output_columns=("total_revenue",),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT 1 AS total_revenue",
    )
    signed = SignedCompiledDbtModel(
        model=model,
        model_digest="3" * 64,
        key_id="compiler",
        signature="unused-by-fake-invoker",
    )
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection, columns=[(1, "total_revenue", "numeric", "NO")]
    )
    connections = iter((lock_connection, inspect_connection))
    warehouse = PostgreSQLMaterializationWarehouse(
        settings=PostgreSQLMaterializationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr("postgresql://unused"),
            consumption_schema_name="consumption",
            consumption_view_name="orders",
            control_schema_name="control",
            generation_table_name="generations",
            generation_pointer_table_name="pointers",
        ),
        signed_model=signed,
        invoker=cast(DbtInvoker, _LockAssertingInvoker(lock_connection)),
        connect=lambda _dsn: next(connections),
    )
    request = _materialization_request(
        signed,
        compiled_model_digest=signed.model_digest,
        expected_output_schema_digest="7" * 64,
    )

    observation = warehouse.execute(request)

    assert observation.output_row_count == 1
    assert inspect_connection.calls == 4
    assert not lock_connection.locked


def _magnitude_model() -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="orders",
        contract_digest="1" * 64,
        provider="postgresql",
        input_generation_digests=("2" * 64,),
        target_schema="contract_" + "1" * 54,
        output_columns=("total_revenue",),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT 1 AS total_revenue",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest="3" * 64,
        key_id="compiler",
        signature="verified-by-fake-invoker",
    )


def _magnitude_request(signed_model: SignedCompiledDbtModel) -> MaterializationRequest:
    return _materialization_request(
        signed_model,
        compiled_model_digest=signed_model.model_digest,
        expected_output_schema_digest="7" * 64,
    )


def _magnitude_warehouse(
    signed_model: SignedCompiledDbtModel,
    connections: tuple[
        _TargetLockConnection | _InspectConnection | _SwitchMutationConnection,
        ...,
    ],
) -> PostgreSQLMaterializationWarehouse:
    connection_iterator = iter(connections)
    return PostgreSQLMaterializationWarehouse(
        settings=PostgreSQLMaterializationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr("postgresql://unused"),
            consumption_schema_name="consumption",
            consumption_view_name="orders",
            control_schema_name="control",
            generation_table_name="generations",
            generation_pointer_table_name="pointers",
        ),
        signed_model=signed_model,
        invoker=cast(
            DbtInvoker, _LockAssertingInvoker(cast(_TargetLockConnection, connections[0]))
        ),
        connect=lambda _dsn: next(connection_iterator),
    )


def test_signed_decimal_magnitude_check_accepts_exact_boundary_inside_values() -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        magnitude_values=(
            Decimal(f"{'9' * 48}.{'9' * 9}"),
            Decimal(f"-{'9' * 48}.{'9' * 9}"),
        ),
        columns=[(1, "total_revenue", "numeric", "NO")],
    )
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection),
    )

    observation = warehouse.execute(_magnitude_request(signed_model))

    magnitude_statement, magnitude_parameters = inspect_connection.statements[3]
    assert "Identifier('total_revenue')" in repr(magnitude_statement)
    assert "IS NULL" in repr(magnitude_statement)
    assert " <= " in repr(magnitude_statement)
    assert " >= " in repr(magnitude_statement)
    assert magnitude_parameters == (Decimal("-1e48"), Decimal("1e48"))
    assert observation.provider_commit_reference == digest(
        {
            "domain": "pillarmesh-postgresql-product-generation-v1",
            "tenant_id": "tenant-a",
            "product_id": "orders",
            "product_revision": 1,
            "product_generation": 1,
            "model_digest": signed_model.model_digest,
            "relation_identity": ("16425", "16425"),
            "output_magnitude_checks": (
                {
                    "declaration": signed_model.model.output_magnitude_checks[0].model_dump(
                        mode="python"
                    ),
                    "violation_count": 0,
                },
            ),
        }
    )


@pytest.mark.parametrize("exclusive_boundary", [Decimal("-1e48"), Decimal("1e48")])
def test_signed_decimal_magnitude_check_rejects_exclusive_bounds(
    exclusive_boundary: Decimal,
) -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        magnitude_values=(exclusive_boundary,),
        columns=[(1, "total_revenue", "numeric", "NO")],
    )
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection),
    )

    with pytest.raises(ProviderError) as caught:
        warehouse.execute(_magnitude_request(signed_model))

    assert caught.value.classification == "integrity_failure"
    assert exclusive_boundary in inspect_connection.statements[3][1]


@pytest.mark.parametrize("magnitude_row", [None, (), (True,), (-1,), (0, 1)])
def test_signed_decimal_magnitude_check_rejects_malformed_violation_count(
    magnitude_row: tuple[object, ...] | None,
) -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        magnitude_row=magnitude_row,
        columns=[(1, "total_revenue", "numeric", "NO")],
    )
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection),
    )

    with pytest.raises(ProviderError) as caught:
        warehouse.execute(_magnitude_request(signed_model))

    assert caught.value.classification == "invalid_provider_response"


def test_signed_decimal_magnitude_check_classifies_driver_failure() -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        magnitude_error=psycopg.OperationalError("private"),
        columns=[(1, "total_revenue", "numeric", "NO")],
    )
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection),
    )

    with pytest.raises(ProviderError) as caught:
        warehouse.execute(_magnitude_request(signed_model))

    assert caught.value.classification == "transient_transport"
    assert "private" not in str(caught.value)


def test_signed_decimal_magnitude_check_rejects_missing_observed_column() -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        columns=[(1, "region", "text", "NO")],
    )
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection),
    )

    with pytest.raises(ProviderError) as caught:
        warehouse.execute(_magnitude_request(signed_model))

    assert caught.value.classification == "invalid_provider_response"
    assert inspect_connection.calls == 3


def test_signed_model_rejects_hostile_and_unknown_magnitude_check_columns() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        DbtDecimalMagnitudeCheck(column_name='total_revenue"; DROP TABLE orders; --')

    with pytest.raises(ValidationError, match="declared output column"):
        CompiledDbtModel(
            model_name="orders",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="contract_" + "1" * 54,
            output_columns=("region",),
            output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
            compiled_sql="SELECT 1",
        )


def test_switch_rejects_post_execute_magnitude_mutation_before_publication_effect() -> None:
    signed_model = _magnitude_model()
    lock_connection = _TargetLockConnection()
    inspect_connection = _InspectConnection(
        lock_connection,
        magnitude_row=(0,),
        columns=[(1, "total_revenue", "numeric", "NO")],
    )
    switch_connection = _SwitchMutationConnection()
    warehouse = _magnitude_warehouse(
        signed_model,
        (lock_connection, inspect_connection, switch_connection),
    )
    request = _magnitude_request(signed_model)
    observation = warehouse.execute(request)

    with pytest.raises(ProviderError) as caught:
        warehouse.switch_consumption_view(request, observation)

    assert caught.value.classification == "integrity_failure"
    assert switch_connection.rolled_back is True
    assert switch_connection.closed is True
    assert not any(
        "CREATE OR REPLACE VIEW" in repr(query) for query, _ in switch_connection.statements
    )
