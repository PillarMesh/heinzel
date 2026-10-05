from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from heinzel_contract_model import digest
from heinzel_dbt_adapter import DbtDecimalMagnitudeCheck
from heinzel_provider_postgresql import (
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
    compose_postgresql_answer_query_provider,
)
from heinzel_provider_postgresql.answer_query import PostgreSQLAnswerGenerationBinding
from heinzel_provider_postgresql.product_materialization import (
    _postgresql_product_generation_commit_reference,
)
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import (
    AnswerProductGenerationReference,
    AnswerQueryParameter,
    AnswerQueryReference,
    AnswerQueryTimedOut,
    ReadOnlyAnswerQuery,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from pydantic import SecretStr, ValidationError

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@dataclass(frozen=True)
class _Column:
    name: str
    type_code: int


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...] | None] | None = None) -> None:
        self.description: tuple[_Column, ...] = (
            _Column("region", 25),
            _Column("revenue", 1700),
        )
        self.rows: list[tuple[object, ...] | None] = rows or [("west", Decimal("12.50"))]
        self.executed: tuple[object, tuple[object, ...]] | None = None
        self.itersize = 0
        self.closed = False
        self.failure: psycopg.Error | None = None

    def execute(self, statement: object, params: tuple[object, ...]) -> None:
        if self.failure is not None:
            raise self.failure
        self.executed = (statement, params)

    def fetchone(self) -> tuple[object, ...] | None:
        if self.failure is not None:
            raise self.failure
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = [row for row in self.rows if row is not None]
        self.rows = []
        return rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self.query_cursor = _Cursor()
        self.control: list[tuple[object, tuple[object, ...]]] = []
        self.cursor_name: str | None = None
        self.cancelled = False
        self.rolled_back = False
        self.closed = False
        self.read_only = "on"
        self.role_identity_row: tuple[object, ...] = ("answer_runtime", "answer_runtime")
        self.session_role_row: tuple[object, ...] = (
            True,
            False,
            False,
            False,
            False,
            False,
        )
        self.relation_row: tuple[object, ...] = (
            4242,
            9001,
            "r",
            "p",
            False,
            False,
            False,
            False,
            NOW,
            False,
        )
        self.generation_columns: object = ["region", "revenue", "total_revenue", "tax"]
        self.magnitude_rows: list[tuple[object, ...] | None] = []
        self.magnitude_error: psycopg.Error | None = None

    def execute(self, statement: object, params: tuple[object, ...] = ()) -> _Cursor:
        self.control.append((statement, params))
        if statement == "SHOW transaction_read_only":
            return _Cursor([(self.read_only,)])
        if statement == "SELECT SESSION_USER, CURRENT_USER":
            return _Cursor([self.role_identity_row])
        if isinstance(statement, str) and statement.startswith("SELECT rolcanlogin"):
            return _Cursor([self.session_role_row])
        if isinstance(statement, str) and statement.startswith("SELECT c.oid::bigint"):
            return _Cursor([self.relation_row])
        if isinstance(statement, str) and statement.startswith("SELECT array_agg"):
            return _Cursor([(self.generation_columns,)])
        if "SELECT count(*)" in repr(statement):
            if self.magnitude_error is not None:
                raise self.magnitude_error
            return _Cursor([self.magnitude_rows.pop(0)])
        return _Cursor()

    def cursor(self, name: str) -> _Cursor:
        self.cursor_name = name
        return self.query_cursor

    def cancel(self) -> None:
        self.cancelled = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _request(**updates: object) -> ReadOnlyAnswerQuery:
    values: dict[str, object] = {
        "engine_kind": "postgresql",
        "tenant_id": "tenant-a",
        "statement": (
            'SELECT "source"."region" AS "region", '
            'SUM("source"."revenue") AS "revenue" '
            'FROM "product"."sales_v1_g7" AS "source" GROUP BY "source"."region" '
            'HAVING COUNT(DISTINCT "source"."customer_id") >= %s LIMIT 10'
        ),
        "parameters": (AnswerQueryParameter(name="p0", value_type="integer", value=5),),
        "statement_timeout_seconds": 3,
        "row_ceiling": 10,
        "byte_ceiling": 10_000,
        "consumption_object_refs": (
            AnswerQueryReference(artifact_id="consumption-1", version=7, digest="b" * 64),
        ),
        "product_generation_refs": (
            AnswerProductGenerationReference(
                product_ref=AnswerQueryReference(
                    artifact_id="product-1", version=1, digest="a" * 64
                ),
                generation=7,
            ),
        ),
    }
    values.update(updates)
    return ReadOnlyAnswerQuery.model_validate(values)


def _binding(engine: EngineKind = EngineKind.POSTGRESQL) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="warehouse-1",
        tenant_id="tenant-a",
        engine_kind=engine,
        region="local",
        capability_profile_digest="b" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=3,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        updated_at=datetime(2026, 9, 11, tzinfo=UTC),
        provisioned_at=datetime(2026, 9, 11, tzinfo=UTC),
    )


def _settings() -> PostgreSQLAnswerQuerySettings:
    return PostgreSQLAnswerQuerySettings(
        dsn=SecretStr("postgresql://answer:private-password@warehouse/db")
    )


def _generation_binding() -> PostgreSQLAnswerGenerationBinding:
    provider_commit_reference = digest(
        {
            "domain": "heinzel-postgresql-product-generation-v1",
            "tenant_id": "tenant-a",
            "product_id": "product-1",
            "product_revision": 1,
            "product_generation": 7,
            "model_digest": "e" * 64,
            "relation_identity": ("4242", "9001"),
        }
    )
    return PostgreSQLAnswerGenerationBinding(
        tenant_id="tenant-a",
        product_ref=AnswerQueryReference(artifact_id="product-1", version=1, digest="a" * 64),
        generation=7,
        consumption_object_ref=AnswerQueryReference(
            artifact_id="consumption-1", version=7, digest="b" * 64
        ),
        materialization_receipt_ref=AnswerQueryReference(
            artifact_id="materialization-1", version=7, digest="c" * 64
        ),
        receipt_plan_digest="e" * 64,
        provider_commit_reference=provider_commit_reference,
        namespace="product",
        relation_name="sales_v1_g7",
        retained_until=NOW + timedelta(hours=1),
    )


def _magnitude_generation_binding(
    checks: tuple[DbtDecimalMagnitudeCheck, ...] = (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        DbtDecimalMagnitudeCheck(column_name="tax"),
    ),
) -> PostgreSQLAnswerGenerationBinding:
    binding = _generation_binding()
    provider_commit_reference = _postgresql_product_generation_commit_reference(
        tenant_id=binding.tenant_id,
        product_id=binding.product_ref.artifact_id,
        product_revision=binding.product_ref.version,
        product_generation=binding.generation,
        model_digest=binding.receipt_plan_digest,
        relation_identity=("4242", "9001"),
        magnitude_checks=tuple((check, 0) for check in checks),
    )
    return binding.model_copy(
        update={
            "provider_commit_reference": provider_commit_reference,
            "output_magnitude_checks": checks,
        }
    )


class _GenerationAuthority:
    def __init__(self, binding: PostgreSQLAnswerGenerationBinding | None = None) -> None:
        self.binding: PostgreSQLAnswerGenerationBinding | None = (
            _generation_binding() if binding is None else binding
        )

    def resolve(
        self,
        *,
        tenant_id: str,
        product_ref: AnswerQueryReference,
        generation: int,
        consumption_object_ref: AnswerQueryReference,
    ) -> PostgreSQLAnswerGenerationBinding | None:
        del tenant_id, product_ref, generation, consumption_object_ref
        return self.binding


def test_postgresql_uses_a_read_only_transaction_timeout_bindings_and_streaming_cursor() -> None:
    connection = _Connection()
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
        fetch_size=64,
    )

    cursor = provider.execute_read_only(_request())

    assert connection.control[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        (),
    )
    assert connection.control[1] == ("SHOW transaction_read_only", ())
    assert connection.control[2] == ("SELECT SESSION_USER, CURRENT_USER", ())
    assert connection.control[3] == (
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER",
        (),
    )
    assert connection.control[4] == (
        "SELECT set_config('statement_timeout', %s, true)",
        ("3000",),
    )
    assert len(connection.control) == 7
    assert connection.cursor_name == "heinzel_answer_query"
    assert connection.query_cursor.executed == (_request().statement, (5,))
    assert connection.query_cursor.itersize == 64
    assert tuple((column.name, column.value_type) for column in cursor.columns) == (
        ("region", "string"),
        ("revenue", "decimal"),
    )
    assert cursor.fetchone() == ("west", Decimal("12.50"))
    assert cursor.fetchone() is None

    cursor.close()
    assert connection.query_cursor.closed is True
    assert connection.rolled_back is True
    assert connection.closed is True


def test_postgresql_resolves_generation_authority_from_the_exact_query_scope() -> None:
    connection = _Connection()
    binding = _generation_binding()
    calls: list[tuple[object, ...]] = []

    class Authority:
        def resolve(
            self,
            *,
            tenant_id: str,
            product_ref: AnswerQueryReference,
            generation: int,
            consumption_object_ref: AnswerQueryReference,
        ) -> PostgreSQLAnswerGenerationBinding:
            calls.append((tenant_id, product_ref, generation, consumption_object_ref))
            return binding

    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=Authority(),
        connect=lambda _dsn: connection,
    )

    provider.execute_read_only(_request()).close()

    assert calls == [
        (
            "tenant-a",
            _request().product_generation_refs[0].product_ref,
            7,
            _request().consumption_object_refs[0],
        )
    ]


def test_postgresql_denies_missing_generation_authority_before_opening_credentials() -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    authority = _GenerationAuthority()
    authority.binding = None
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(), generation_authority=authority, connect=connect
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert opened is False


def test_postgresql_preserves_generation_authority_availability_failures() -> None:
    class Authority:
        def resolve(
            self,
            *,
            tenant_id: str,
            product_ref: AnswerQueryReference,
            generation: int,
            consumption_object_ref: AnswerQueryReference,
        ) -> PostgreSQLAnswerGenerationBinding | None:
            del tenant_id, product_ref, generation, consumption_object_ref
            raise ProviderError("generation authority unavailable", "transient_unavailable")

    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(), generation_authority=Authority(), connect=lambda _dsn: _Connection()
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "transient_unavailable"


def test_postgresql_rejects_a_malformed_generation_authority_response_as_integrity_failure() -> (
    None
):
    invalid_binding = _generation_binding().model_copy(update={"tenant_id": ""})
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(invalid_binding),
        connect=lambda _dsn: _Connection(),
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"


@pytest.mark.parametrize("failure", ("wrong_precision", "duplicate", "hidden_field"))
def test_postgresql_strictly_revalidates_generation_magnitude_declarations(
    failure: str,
) -> None:
    opened = False
    check = DbtDecimalMagnitudeCheck(column_name="total_revenue")
    checks: tuple[DbtDecimalMagnitudeCheck, ...]
    if failure == "wrong_precision":
        checks = (check.model_copy(update={"precision": 58}),)
    elif failure == "duplicate":
        checks = (check, check)
    else:
        checks = (check.model_copy(update={"undeclared": "private"}),)
    invalid_binding = _generation_binding().model_copy(update={"output_magnitude_checks": checks})

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(invalid_binding),
            connect=connect,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert opened is False


def test_postgresql_rejects_a_stable_view_that_can_switch_to_another_generation() -> None:
    connection = _Connection()
    connection.relation_row = (
        4242,
        9001,
        "v",
        "p",
        False,
        False,
        False,
        False,
        NOW,
        False,
    )
    binding = _generation_binding().model_copy(
        update={"namespace": "consumption", "relation_name": "sales"}
    )
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(binding),
        connect=lambda _dsn: connection,
    )
    request = _request(
        statement=_request().statement.replace('"product"."sales_v1_g7"', '"consumption"."sales"')
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(request)

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_plan_that_names_the_switchable_view_instead_of_its_generation() -> (
    None
):
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    request = _request(
        statement=_request().statement.replace('"product"."sales_v1_g7"', '"consumption"."sales"')
    )

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(), generation_authority=_GenerationAuthority(), connect=connect
        ).execute_read_only(request)

    assert captured.value.classification == "authorization_denied"
    assert opened is False


@pytest.mark.parametrize(
    "statement",
    (
        _request().statement.replace(" GROUP BY", ', "private"."sales_v1_g8" AS "other" GROUP BY'),
        _request().statement.replace(
            '"source"."region" AS "region"',
            '(select max("secret") from "private"."sales_v1_g8") AS "region"',
        ),
        _request().statement.replace(
            '"source"."region" AS "region"',
            'COALESCE("source"."region", "source"."region") AS "region"',
        ),
    ),
)
def test_postgresql_rejects_non_compiler_sql_before_credentials(statement: str) -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(), generation_authority=_GenerationAuthority(), connect=connect
        ).execute_read_only(_request(statement=statement))

    assert captured.value.classification == "statement_rejected"
    assert opened is False


@pytest.mark.parametrize("position", range(1, 6))
def test_postgresql_rejects_a_privileged_connected_role(position: int) -> None:
    connection = _Connection()
    role_values = list(connection.session_role_row)
    role_values[position] = True
    connection.session_role_row = tuple(role_values)

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_privileged_session_that_switches_to_the_runtime_role() -> None:
    connection = _Connection()
    connection.role_identity_row = ("privileged_operator", "answer_runtime")

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert connection.cursor_name is None


def test_postgresql_rejects_an_existing_relation_with_the_wrong_identity() -> None:
    connection = _Connection()
    connection.relation_row = (
        9999,
        8008,
        "r",
        "p",
        False,
        False,
        False,
        False,
        NOW,
        False,
    )

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_generation_with_an_unverified_commit_reference() -> None:
    connection = _Connection()
    binding = _generation_binding().model_copy(update={"provider_commit_reference": "f" * 64})

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


def test_postgresql_rechecks_signed_magnitudes_before_executing_the_governed_query() -> None:
    connection = _Connection()
    connection.magnitude_rows = [(0,), (0,)]
    binding = _magnitude_generation_binding()

    cursor = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(binding),
        connect=lambda _dsn: connection,
    ).execute_read_only(_request())

    magnitude_queries = [
        (statement, parameters)
        for statement, parameters in connection.control
        if "SELECT count(*)" in repr(statement)
    ]
    assert len(magnitude_queries) == 2
    assert "Identifier('total_revenue')" in repr(magnitude_queries[0][0])
    assert "Identifier('tax')" in repr(magnitude_queries[1][0])
    assert tuple(parameters for _, parameters in magnitude_queries) == (
        (Decimal("-1e48"), Decimal("1e48")),
        (Decimal("-1e48"), Decimal("1e48")),
    )
    assert connection.cursor_name == "heinzel_answer_query"
    cursor.close()


def test_postgresql_rejects_a_magnitude_receipt_without_verified_declarations() -> None:
    connection = _Connection()
    binding = _magnitude_generation_binding().model_copy(update={"output_magnitude_checks": ()})

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None
    assert not any("SELECT count(*)" in repr(statement) for statement, _ in connection.control)


@pytest.mark.parametrize("mutation", ("reordered", "substituted"))
def test_postgresql_rejects_magnitude_declarations_not_bound_to_the_commit(
    mutation: str,
) -> None:
    connection = _Connection()
    connection.magnitude_rows = [(0,), (0,)]
    binding = _magnitude_generation_binding()
    if mutation == "reordered":
        checks = tuple(reversed(binding.output_magnitude_checks))
    else:
        checks = (
            DbtDecimalMagnitudeCheck(column_name="revenue"),
            binding.output_magnitude_checks[1],
        )
    binding = binding.model_copy(update={"output_magnitude_checks": checks})

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_magnitude_check_for_an_absent_generation_column() -> None:
    connection = _Connection()
    connection.generation_columns = ["region", "revenue", "tax"]
    binding = _magnitude_generation_binding()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_nonzero_magnitude_violation_count() -> None:
    connection = _Connection()
    connection.magnitude_rows = [(1,)]
    binding = _magnitude_generation_binding()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


@pytest.mark.parametrize("magnitude_row", [None, (), (True,), (-1,), (0, 1)])
def test_postgresql_rejects_a_malformed_magnitude_violation_count(
    magnitude_row: tuple[object, ...] | None,
) -> None:
    connection = _Connection()
    connection.magnitude_rows = [magnitude_row]
    binding = _magnitude_generation_binding()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert connection.cursor_name is None


def test_postgresql_classifies_a_driver_failure_during_the_magnitude_recheck() -> None:
    connection = _Connection()
    connection.magnitude_error = psycopg.OperationalError("private-password SELECT secret")
    binding = _magnitude_generation_binding()

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "transient_transport"
    assert "private-password" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert connection.cursor_name is None


def test_postgresql_rejects_an_expired_generation_using_server_time() -> None:
    connection = _Connection()
    binding = _generation_binding().model_copy(update={"retained_until": NOW})

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(binding),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert connection.cursor_name is None


def test_postgresql_refuses_execution_when_server_read_only_is_not_active() -> None:
    connection = _Connection()
    connection.read_only = "off"

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert connection.cursor_name is None


def test_postgresql_settings_reject_legacy_caller_supplied_generation_bindings() -> None:
    with pytest.raises(ValidationError):
        PostgreSQLAnswerQuerySettings.model_validate(
            {
                "dsn": SecretStr("postgresql://answer:private-password@warehouse/db"),
                "generation_bindings": (_generation_binding(),),
            },
            strict=True,
        )


@pytest.mark.parametrize(
    ("position", "unsafe_value"),
    [(4, True), (5, True), (6, True), (7, True), (9, True)],
)
def test_postgresql_rejects_mutable_or_row_secured_generation_relations(
    position: int, unsafe_value: object
) -> None:
    connection = _Connection()
    values = list(connection.relation_row)
    values[position] = unsafe_value
    connection.relation_row = tuple(values)

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        ).execute_read_only(_request())

    assert captured.value.classification == "integrity_failure"
    assert connection.cursor_name is None


def test_postgresql_rejects_a_generation_without_exact_tenant_authority() -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    request = _request(tenant_id="tenant-b")

    with pytest.raises(ProviderError) as captured:
        PostgreSQLAnswerQueryProvider(
            settings=_settings(), generation_authority=_GenerationAuthority(), connect=connect
        ).execute_read_only(request)

    assert captured.value.classification == "authorization_denied"
    assert opened is False


def test_postgresql_cursor_cancellation_reaches_the_connection() -> None:
    connection = _Connection()
    cursor = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    ).execute_read_only(_request())

    cursor.cancel()
    cursor.close()

    assert connection.cancelled is True


def test_postgresql_query_timeout_is_typed_and_sanitized() -> None:
    connection = _Connection()
    connection.query_cursor.failure = psycopg.errors.QueryCanceled("private-password SELECT secret")
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    )

    with pytest.raises(AnswerQueryTimedOut) as captured:
        provider.execute_read_only(_request())

    assert "private-password" not in str(captured.value)
    assert connection.rolled_back is True
    assert connection.closed is True


def test_postgresql_driver_error_is_classified_without_secret_or_statement() -> None:
    connection = _Connection()
    connection.query_cursor.failure = psycopg.errors.InsufficientPrivilege(
        "private-password SELECT secret"
    )
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert "private-password" not in str(captured.value)
    assert "SELECT" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("failure", "classification"),
    [
        (psycopg.OperationalError("private-password SELECT secret"), "transient_transport"),
        (psycopg.errors.TooManyConnections("private-password"), "transient_unavailable"),
        (psycopg.errors.SyntaxError("SELECT secret"), "statement_rejected"),
    ],
)
def test_postgresql_preserves_driver_failure_classification(
    failure: psycopg.Error, classification: str
) -> None:
    connection = _Connection()
    connection.query_cursor.failure = failure
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == classification
    assert "private-password" not in str(captured.value)
    assert "SELECT" not in str(captured.value)


def test_postgresql_rejects_noncanonical_statement_before_opening_credentials() -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(), generation_authority=_GenerationAuthority(), connect=connect
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request(statement="SELECT 1; DELETE FROM private_table"))

    assert captured.value.classification == "statement_rejected"
    assert opened is False


def test_postgresql_rejects_unknown_driver_column_type() -> None:
    connection = _Connection()
    connection.query_cursor.description = (_Column("opaque", 999999),)
    provider = PostgreSQLAnswerQueryProvider(
        settings=_settings(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert connection.closed is True


def test_postgresql_composition_resolves_only_ready_matching_answer_credentials() -> None:
    connection = _Connection()

    class Authority:
        def resolve_answer_runtime(
            self, *, tenant_id: str, binding_id: str, binding_revision: int
        ) -> PostgreSQLAnswerQuerySettings:
            assert (tenant_id, binding_id, binding_revision) == ("tenant-a", "warehouse-1", 3)
            return _settings()

    provider = compose_postgresql_answer_query_provider(
        binding=_binding(),
        settings_authority=Authority(),
        generation_authority=_GenerationAuthority(),
        connect=lambda _dsn: connection,
    )
    assert provider.engine_kind == "postgresql"

    with pytest.raises(ProviderError) as captured:
        compose_postgresql_answer_query_provider(
            binding=_binding(EngineKind.CLICKHOUSE),
            settings_authority=Authority(),
            generation_authority=_GenerationAuthority(),
            connect=lambda _dsn: connection,
        )
    assert captured.value.classification == "authorization_denied"


@pytest.mark.parametrize(
    ("probe_outcome", "classification"),
    (
        (psycopg.errors.InvalidPassword(), "authorization_denied"),
        (psycopg.OperationalError("probe transport failed"), "transient_transport"),
    ),
)
def test_answer_query_attributes_only_structured_startup_rejections(
    probe_outcome: Exception, classification: str
) -> None:
    probe_calls: list[dict[str, object]] = []

    def rejected(_dsn: str) -> _Connection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> _Connection:
        probe_calls.append(parameters)
        raise probe_outcome

    provider = PostgreSQLAnswerQueryProvider(
        settings=PostgreSQLAnswerQuerySettings(
            dsn=SecretStr(
                "host=warehouse.internal dbname=db user=answer "
                "password=private-password sslmode=disable gssencmode=disable"
            )
        ),
        generation_authority=_GenerationAuthority(),
        connect=rejected,
        startup_denial_probe=probe,
    )

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == classification
    assert len(probe_calls) == 1
    assert "private" not in str(captured.value)
