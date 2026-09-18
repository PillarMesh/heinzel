from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import psycopg
import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import (
    AcquisitionCeilingExceeded,
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionSessionIncomplete,
    SourceObservationRequest,
    acquisition_intent_key,
)
from pydantic import SecretStr

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
type _Mode = Literal["snapshot", "incremental", "reconciliation"]
FIELDS = (
    AcquisitionField(name='order"id', value_type="integer", nullable=False),
    AcquisitionField(name="amount", value_type="decimal", nullable=False),
    AcquisitionField(name="active", value_type="boolean", nullable=False),
    AcquisitionField(name="label", value_type="string", nullable=True),
    AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
)


def schema(*, fields: tuple[AcquisitionField, ...] = FIELDS) -> AcquisitionObjectSchema:
    return AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=('order"id',),
        source_updated_at_field="updated_at",
    )


def intent(*, record_ceiling: int = 10, mode: _Mode = "snapshot") -> AcquisitionIntent:
    object_refs = ("orders",)
    run_intent_ref = "1" * 64
    contract_digest = "2" * 64
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref="source-binding-a",
            acquisition_mode=mode,
            object_refs=object_refs,
            prior_checkpoint_revision=0,
        ),
        tenant_id="tenant-a",
        run_intent_ref=run_intent_ref,
        contract_ref="contract-a",
        contract_digest=contract_digest,
        source_binding_ref="source-binding-a",
        source_observation_digest="3" * 64,
        acquisition_mode=mode,
        object_refs=object_refs,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=record_ceiling,
        encoded_byte_ceiling=100_000,
        admitted_at=NOW,
    )


def settings() -> PostgreSQLAcquisitionSettings:
    return PostgreSQLAcquisitionSettings(
        dsn=SecretStr("postgresql://private-secret@source/private"),
        connection_handle="opaque-source-capability",
        objects=(
            PostgreSQLSourceObjectDeclaration(
                logical_object_ref="orders",
                schema_name='sales"schema',
                table_name='order"table',
                field_names=tuple(field.name for field in FIELDS),
                key_name='order"id',
                source_updated_at_field="updated_at",
            ),
        ),
        unrelated_schema_name="private_unrelated",
        max_write_transaction_duration=timedelta(minutes=5),
    )


class FakeCursor:
    def __init__(self, connection: FakeConnection, *, name: str | None = None) -> None:
        self.connection = connection
        self.name = name
        self.rows: list[tuple[object, ...]] = []
        self.closed = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(self, query: object, params: object = None) -> None:
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.connection.calls.append((rendered, params, self.name))
        backend = self.connection.backend
        if self.name is not None:
            self.connection.named_cursor_reads += 1
            self.rows = list(backend.records)
        elif "pg_catalog.pg_constraint" in rendered:
            self.rows = [] if backend.key is None else [backend.key]
        elif "rolsuper" in rendered:
            self.rows = [(backend.evaluate_privilege_probe(rendered, params),)]
        elif "pg_catalog.pg_class" in rendered:
            self.rows = [("42", "fixture_db", backend.object_kind)]
        elif "information_schema.columns" in rendered:
            self.rows = list(backend.columns)
        elif "transaction_timestamp()" in rendered:
            self.rows = [(backend.snapshot_time,)]
        elif "snapshot_cursor_upper" in rendered:
            self.rows = [backend.snapshot_cursor_upper]
        elif "pg_current_snapshot" in rendered:
            self.rows = [(backend.snapshot_identity,)]
        elif "SELECT min" in rendered:
            self.rows = [backend.bounds]

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self.rows)

    def close(self) -> None:
        self.closed = True
        if self.connection.backend.cleanup_failure:
            raise RuntimeError("private-cursor-cleanup-canary")


class FakeBackend:
    def __init__(self) -> None:
        self.object_kind = "r"
        self.columns: list[tuple[object, ...]] = [
            ('order"id', "bigint", "int8", "NO"),
            ("amount", "numeric", "numeric", "NO"),
            ("active", "boolean", "bool", "NO"),
            ("label", "text", "text", "YES"),
            ("updated_at", "timestamp with time zone", "timestamptz", "NO"),
        ]
        self.key: tuple[object, ...] | None = ('order"id', "primary_key", True, "btree", 1)
        self.table_select = False
        self.approved_columns_select = True
        self.unapproved_column_select = False
        self.mutation_privilege = False
        self.maintain_privilege = False
        self.administration_privilege = False
        self.unrelated_schema_access = False
        self.undeclared_relation_access = False
        self.sequence_privilege = False
        self.reachable_role_unsafe = False
        self.membership_admin = False
        self.unrelated_schema_exists = True
        self.snapshot_identity = "100:200:"
        self.snapshot_time = NOW
        self.snapshot_cursor_upper: tuple[object, object] = (NOW, 8)
        self.bounds: tuple[object, ...] = (7, 8, 2)
        self.records: list[tuple[object, ...]] = [
            (7, Decimal("10.50"), True, None, NOW),
            (8, Decimal("11.25"), False, "renewal", NOW),
        ]
        self.cleanup_failure = False

    def evaluate_privilege_probe(self, statement: str, params: object) -> bool:
        normalized_statement = " ".join(statement.split())
        # Pinning the complete policy catches removed or inverted predicates that a fake database
        # cannot evaluate; the live acquisition journeys under tests/integration run the same
        # statement against real PostgreSQL grant semantics.
        statement_is_exact = (
            hashlib.sha256(normalized_statement.encode()).hexdigest()
            == "9241746875e7f5d202b4919687451c987339fb7f850964d2c75089ebca1b219d"
        )
        parameters_are_exact = (
            isinstance(params, tuple)
            and len(params) == 5
            and params[0] == "42"
            and params[1] == list(field.name for field in FIELDS)
            and params[2] == ['sales"schema']
            and params[3] == ['order"table']
            and params[4] == "private_unrelated"
        )
        return (
            statement_is_exact
            and parameters_are_exact
            and not self.table_select
            and self.approved_columns_select
            and not self.unapproved_column_select
            and not self.mutation_privilege
            and not self.maintain_privilege
            and not self.administration_privilege
            and not self.unrelated_schema_access
            and not self.undeclared_relation_access
            and not self.sequence_privilege
            and not self.reachable_role_unsafe
            and not self.membership_admin
            and self.unrelated_schema_exists
        )


class FakeConnection:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.calls: list[tuple[str, object, str | None]] = []
        self.named_cursor_reads = 0
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, name: str | None = None) -> FakeCursor:
        return FakeCursor(self, name=name)

    def execute(self, query: str) -> None:
        self.calls.append((query, None, None))

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True
        if self.backend.cleanup_failure:
            raise RuntimeError("private-rollback-cleanup-canary")

    def close(self) -> None:
        self.closed = True
        if self.backend.cleanup_failure:
            raise RuntimeError("private-connection-cleanup-canary")


def provider(
    backend: FakeBackend,
) -> tuple[PostgreSQLAcquisitionProvider, FakeConnection, dict[str, bytes]]:
    connection = FakeConnection(backend)
    private_boundaries: dict[str, bytes] = {}

    def write_private_boundary(tenant_id: str, reference: str, payload: bytes) -> None:
        assert tenant_id == "tenant-a"
        private_boundaries[reference] = payload

    acquisition_provider = PostgreSQLAcquisitionProvider(
        settings(),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=write_private_boundary,
    )
    return acquisition_provider, connection, private_boundaries


def test_settings_are_strict_and_require_positive_write_duration() -> None:
    with pytest.raises(ValueError):
        PostgreSQLAcquisitionSettings(
            **{
                **settings().model_dump(),
                "max_write_transaction_duration": timedelta(0),
            }
        )
    with pytest.raises(ValueError, match="unique"):
        PostgreSQLAcquisitionSettings(
            **{
                **settings().model_dump(),
                "objects": settings().objects * 2,
            }
        )
    duplicate_relation = (
        settings().objects[0].model_copy(update={"logical_object_ref": "orders-copy"})
    )
    with pytest.raises(ValueError, match="physical object declarations"):
        PostgreSQLAcquisitionSettings(
            **{
                **settings().model_dump(),
                "objects": (*settings().objects, duplicate_relation),
            }
        )
    with pytest.raises(ValueError, match="NUL"):
        PostgreSQLSourceObjectDeclaration.model_validate(
            {
                **settings().objects[0].model_dump(),
                "table_name": "orders\x00private",
            }
        )


def test_generic_snapshot_uses_exact_quoted_projection_and_returns_digest_only_boundary() -> None:
    backend = FakeBackend()
    acquisition_provider, connection, private_boundaries = provider(backend)

    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)
    records = tuple(session)
    completed = session.complete()

    assert connection.calls[0][0] == "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    read_call = next(call for call in connection.calls if call[2] is not None)
    assert (
        'SELECT "order""id", "amount", "active", "label", "updated_at" '
        'FROM "sales""schema"."order""table" WHERE ("updated_at", "order""id") '
        '<= (%s, %s) ORDER BY "order""id"'
    ) in read_call[0]
    assert read_call[1] == (NOW, 8)
    assert [record.record_key for record in records] == [
        digest({"logical_object_ref": "orders", "key": 7}),
        digest({"logical_object_ref": "orders", "key": 8}),
    ]
    assert records[0].fields[1].value == Decimal("10.50")
    assert records[0].source_updated_at == NOW
    assert connection.committed and connection.closed and not connection.rolled_back

    boundary = completed.boundaries[0]
    expected_snapshot_digest = digest({"snapshot_identity": backend.snapshot_identity})
    expected_key_range_digest = digest({"key_min": 7, "key_max": 8})
    expected_query_shape_digest = digest(
        {
            "columns": tuple(field.name for field in FIELDS),
            "order_by": 'order"id',
            "cursor_position": "row",
            "row_predicate": {
                "upper_columns": ("updated_at", 'order"id'),
                "upper_operator": "<=",
            },
        }
    )
    assert boundary.snapshot_identity_digest == expected_snapshot_digest
    assert boundary.key_range_digest == expected_key_range_digest
    assert boundary.query_shape_digest == expected_query_shape_digest
    assert boundary.record_count == 2
    assert boundary.private_boundary_ref == "private:tenant-a:orders"
    assert completed.cursor_version == "postgresql-incremental-v1"
    public_payload = canonical_bytes((boundary, completed.cursor_version))
    assert backend.snapshot_identity.encode() not in public_payload
    assert b'"key_min"' not in public_payload
    assert b'"key_max"' not in public_payload
    assert private_boundaries["private:tenant-a:orders"]

    count_position = next(
        index for index, call in enumerate(connection.calls) if "SELECT min" in call[0]
    )
    read_position = next(
        index for index, call in enumerate(connection.calls) if call[2] is not None
    )
    assert count_position < read_position


def test_snapshot_observation_uses_acquisition_schema_digest_and_declared_capability() -> None:
    backend = FakeBackend()
    acquisition_provider, connection, _private_boundaries = provider(backend)

    observation = acquisition_provider.observe_source(
        SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=("orders",),
        )
    )

    provider_observation = observation.object_observations[0].provider_observation
    assert provider_observation.schema_digest == schema().schema_digest
    assert provider_observation.capabilities == ("incremental", "reconciliation", "snapshot")
    assert provider_observation.object_kind == "base_table"
    assert provider_observation.read_only is True
    assert connection.committed and connection.closed


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    (
        ("view", "permanent_configuration"),
        ("missing_column", "permanent_configuration"),
        ("wrong_type", "permanent_configuration"),
        ("nullable_key", "permanent_configuration"),
        ("composite_key", "permanent_configuration"),
        ("hash_key", "permanent_configuration"),
        ("least_privilege", "authorization_denied"),
    ),
)
def test_snapshot_refuses_schema_key_table_and_privilege_drift(
    mutation: str,
    reason_code: str,
) -> None:
    backend = FakeBackend()
    if mutation == "view":
        backend.object_kind = "v"
    elif mutation == "missing_column":
        backend.columns.pop()
    elif mutation == "wrong_type":
        backend.columns[1] = ("amount", "double precision", "float8", "NO")
    elif mutation == "nullable_key":
        backend.key = ('order"id', "primary_key", False, "btree", 1)
    elif mutation == "composite_key":
        backend.key = ('order"id', "primary_key", True, "btree", 2)
    elif mutation == "hash_key":
        backend.key = ('order"id', "primary_key", True, "hash", 1)
    else:
        backend.table_select = True
    acquisition_provider, connection, _private_boundaries = provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(intent(), (schema(),), None)

    assert captured.value.reason_code == reason_code
    assert connection.rolled_back and connection.closed
    assert connection.named_cursor_reads == 0


def test_privilege_probe_covers_mutation_administration_and_unrelated_schema_denials() -> None:
    backend = FakeBackend()
    acquisition_provider, connection, _private_boundaries = provider(backend)

    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)
    tuple(session)

    statement, params, _name = next(call for call in connection.calls if "rolsuper" in call[0])
    for privilege in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    ):
        assert privilege in statement
    for role_flag in (
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        assert role_flag in statement
    assert "has_database_privilege" in statement
    assert "has_schema_privilege" in statement
    assert "pg_has_role" in statement
    assert "unnest(a.approved_relation_schemas, a.approved_relation_names)" in statement
    assert "undeclared_relations" in statement
    assert "has_sequence_privilege" in statement
    assert statement.count("current_setting('server_version_num')::integer >= 170000") == 4
    assert isinstance(params, tuple) and "private_unrelated" in params


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("table_select", True),
        ("approved_columns_select", False),
        ("unapproved_column_select", True),
        ("mutation_privilege", True),
        ("maintain_privilege", True),
        ("administration_privilege", True),
        ("unrelated_schema_access", True),
        ("undeclared_relation_access", True),
        ("sequence_privilege", True),
        ("reachable_role_unsafe", True),
        ("membership_admin", True),
        ("unrelated_schema_exists", False),
    ),
)
def test_each_privilege_escalation_or_missing_denial_witness_is_rejected(
    attribute: str,
    value: bool,
) -> None:
    backend = FakeBackend()
    setattr(backend, attribute, value)
    acquisition_provider, connection, _private_boundaries = provider(backend)

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(intent(), (schema(),), None)

    assert captured.value.reason_code == "authorization_denied"
    assert connection.rolled_back and connection.closed


def test_count_ceiling_is_refused_before_opening_a_row_cursor() -> None:
    backend = FakeBackend()
    backend.bounds = (1, 11, 11)
    acquisition_provider, connection, _private_boundaries = provider(backend)

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        acquisition_provider.open_acquisition(intent(record_ceiling=10), (schema(),), None)

    assert captured.value.logical_object_ref == "orders"
    assert captured.value.limit_kind == "records"
    assert connection.named_cursor_reads == 0
    assert connection.rolled_back and connection.closed


def test_captured_count_must_match_the_completely_consumed_snapshot() -> None:
    backend = FakeBackend()
    backend.bounds = (7, 8, 3)
    acquisition_provider, connection, _private_boundaries = provider(backend)
    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)

    with pytest.raises(AcquisitionProviderError) as captured:
        tuple(session)

    assert captured.value.reason_code == "invalid_provider_response"
    assert connection.rolled_back and connection.closed and not connection.committed


@pytest.mark.parametrize(
    ("driver_error", "classification", "reason_code"),
    (
        (psycopg.errors.ConnectionFailure(), "transient_transport", "transport_failure"),
        (psycopg.OperationalError(), "transient_unavailable", "provider_unavailable"),
        (psycopg.errors.AdminShutdown(), "transient_unavailable", "provider_unavailable"),
        (psycopg.errors.TooManyConnections(), "throttled", "rate_limited"),
        (psycopg.errors.InvalidPassword(), "authorization_denied", "authorization_denied"),
        (psycopg.errors.SyntaxError(), "statement_rejected", "statement_rejected"),
    ),
)
def test_driver_failures_are_classified_without_leaking_private_dsn(
    driver_error: psycopg.Error,
    classification: str,
    reason_code: str,
) -> None:
    def fail_connect(_dsn: str) -> FakeConnection:
        raise driver_error

    acquisition_provider = PostgreSQLAcquisitionProvider(
        settings(),
        connect=fail_connect,
        clock=lambda: NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda _tenant_id, _reference, _payload: None,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        acquisition_provider.open_acquisition(intent(), (schema(),), None)

    assert captured.value.classification == classification
    assert captured.value.reason_code == reason_code
    assert "private" not in str(captured.value)


def test_snapshot_rejects_non_snapshot_mode_and_private_cursor_without_connecting() -> None:
    backend = FakeBackend()
    acquisition_provider, connection, _private_boundaries = provider(backend)

    with pytest.raises(AcquisitionProviderError, match="permanent_configuration"):
        acquisition_provider.open_acquisition(intent(mode="incremental"), (schema(),), None)
    with pytest.raises(AcquisitionProviderError, match="permanent_configuration"):
        acquisition_provider.open_acquisition(intent(), (schema(),), b"private-cursor")

    assert connection.calls == []


def test_partial_consumption_rolls_back_and_cannot_complete() -> None:
    backend = FakeBackend()
    acquisition_provider, connection, _private_boundaries = provider(backend)
    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)

    next(iter(session))
    session.abort()

    assert connection.rolled_back and connection.closed and not connection.committed
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()


def test_malformed_scalar_aborts_and_cleanup_failure_never_replaces_primary_error() -> None:
    backend = FakeBackend()
    backend.records[0] = (7, 10.5, True, None, NOW)
    acquisition_provider, connection, _private_boundaries = provider(backend)
    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)
    backend.cleanup_failure = True

    with pytest.raises(AcquisitionProviderError) as captured:
        next(iter(session))

    assert captured.value.reason_code == "invalid_provider_response"
    assert "private" not in str(captured.value)
    assert connection.rolled_back and connection.closed and not connection.committed


def test_naive_updated_timestamp_is_invalid_provider_response() -> None:
    backend = FakeBackend()
    backend.records[0] = (
        7,
        Decimal("10.50"),
        True,
        None,
        datetime(2026, 9, 1, 12, 0),
    )
    acquisition_provider, connection, _private_boundaries = provider(backend)
    session = acquisition_provider.open_acquisition(intent(), (schema(),), None)

    with pytest.raises(AcquisitionProviderError) as captured:
        tuple(session)

    assert captured.value.reason_code == "invalid_provider_response"
    assert connection.rolled_back and connection.closed


_PASSWORD_DSN = (
    "host=source.internal dbname=private user=reader password=private-secret "
    "sslmode=disable gssencmode=disable"
)


def rejected_at_startup(
    probe_outcome: Exception,
) -> tuple[PostgreSQLAcquisitionProvider, list[dict[str, object]]]:
    probe_calls: list[dict[str, object]] = []

    def fail_connect(_dsn: str) -> FakeConnection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> FakeConnection:
        probe_calls.append(parameters)
        raise probe_outcome

    acquisition_provider = PostgreSQLAcquisitionProvider(
        settings().model_copy(update={"dsn": SecretStr(_PASSWORD_DSN)}),
        connect=fail_connect,
        startup_denial_probe=probe,
        clock=lambda: NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda _tenant_id, _reference, _payload: None,
    )
    return acquisition_provider, probe_calls


def _observe(acquisition_provider: PostgreSQLAcquisitionProvider) -> object:
    return acquisition_provider.observe_source(
        SourceObservationRequest(
            tenant_id="tenant-a", source_binding_ref="source-binding-a", object_refs=("orders",)
        )
    )


def _open_snapshot(acquisition_provider: PostgreSQLAcquisitionProvider) -> object:
    return acquisition_provider.open_acquisition(intent(), (schema(),), None)


@pytest.mark.parametrize("entry_point", (_observe, _open_snapshot))
def test_rejected_credentials_at_startup_are_authorization_denied(
    entry_point: Callable[[PostgreSQLAcquisitionProvider], object],
) -> None:
    acquisition_provider, probe_calls = rejected_at_startup(psycopg.errors.InvalidPassword())

    with pytest.raises(AcquisitionProviderError) as captured:
        entry_point(acquisition_provider)

    assert captured.value.classification == "authorization_denied"
    assert captured.value.reason_code == "authorization_denied"
    assert len(probe_calls) == 1
    assert "private" not in str(captured.value)


def test_startup_failure_the_probe_cannot_attribute_to_credentials_stays_transient() -> None:
    acquisition_provider, probe_calls = rejected_at_startup(
        psycopg.OperationalError("PostgreSQL denial probe transport failed")
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _observe(acquisition_provider)

    assert captured.value.classification == "transient_unavailable"
    assert captured.value.reason_code == "provider_unavailable"
    assert len(probe_calls) == 1
