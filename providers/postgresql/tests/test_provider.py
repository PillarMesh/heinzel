from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_postgresql import PostgresProvider, PostgresSettings, normalize_columns
from heinzel_provider_postgresql.provider import ColumnMetadata
from pydantic import SecretStr

_COLUMNS = [
    ("order_id", "bigint", "int8", "NO", None, None, None),
    ("customer_ref", "character varying", "varchar", "NO", None, None, 65_535),
    ("amount", "numeric", "numeric", "NO", 18, 2, None),
    ("currency", "character varying", "varchar", "NO", None, None, 3),
    ("status", "character varying", "varchar", "NO", None, None, 65_535),
    ("updated_at", "timestamp with time zone", "timestamptz", "NO", None, None, None),
]


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: Any, params: object = None) -> None:
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.connection.calls.append((rendered, params))
        if "pg_catalog.pg_constraint" in rendered:
            self.rows = [("order_id", "primary_key", True, "btree")]
        elif "has_table_privilege" in rendered:
            self.rows = (
                [(self.connection.read_only,)] if self.connection.read_only is not None else []
            )
        elif "pg_catalog.pg_class" in rendered:
            self.rows = [("42", "fixture_db")]
        elif "information_schema.columns" in rendered:
            self.rows = list(_COLUMNS)
        elif "pg_current_snapshot" in rendered:
            self.rows = [("10:20:",)]
        elif "SELECT min" in rendered:
            self.rows = [(7, 8, 2)]
        elif "SELECT order_id" in rendered:
            self.rows = [
                (7, "customer-7", Decimal("10.50"), "USD", "paid", NOW),
                (8, "customer-8", Decimal("11.25"), "EUR", "new", NOW),
            ]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.read_only: bool | None = True

    def cursor(self, name: str | None = None) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, query: str) -> None:
        self.calls.append((query, None))

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_postgres_columns_normalize_to_fixed_semantic_types() -> None:
    rows: list[ColumnMetadata] = [
        ("order_id", "bigint", "int8", "NO", None, None),
        ("customer_ref", "character varying", "varchar", "NO", None, None, 65_535),
        ("amount", "numeric", "numeric", "NO", 18, 2),
        ("currency", "character varying", "varchar", "NO", None, None),
        ("status", "character varying", "varchar", "NO", None, None, 65_535),
        ("updated_at", "timestamp with time zone", "timestamptz", "NO", None, None),
    ]

    columns = normalize_columns(rows)

    assert tuple(column.type_name for column in columns) == (
        "BIGINT",
        "VARCHAR(65535)",
        "NUMERIC(18,2)",
        "VARCHAR(3)",
        "VARCHAR(65535)",
        "TIMESTAMPTZ",
    )


def test_observation_age_fixture_is_timezone_aware() -> None:
    assert NOW - timedelta(minutes=10) < NOW


def test_observe_uses_bound_metadata_values_and_redacts_database_name() -> None:
    connection = FakeConnection()
    settings = PostgresSettings(
        dsn=SecretStr("postgresql://secret@host/fixture_db"),
        connection_handle="source-account",
        schema_name='fixture"schema',
        table_name="orders",
    )
    provider = PostgresProvider(settings, connect=lambda _dsn: connection, clock=lambda: NOW)

    observation = provider.observe()

    metadata_call = next(call for call in connection.calls if "pg_catalog.pg_class" in call[0])
    assert metadata_call[1] == ('fixture"schema', "orders")
    assert "fixture_db" not in observation.object_identity
    assert "secret" not in repr(settings)
    assert observation.key_type == "BIGINT"
    assert observation.key_nullable is False
    assert observation.key_constraint == "primary_key"
    assert observation.stable_key_order is True
    assert observation.read_only is True
    constraint_call = next(
        call for call in connection.calls if "pg_catalog.pg_constraint" in call[0]
    )
    assert constraint_call[1] == ("42", "order_id")
    privilege_call = next(call for call in connection.calls if "has_table_privilege" in call[0])
    assert privilege_call[1] == ("42",)
    assert "has_any_column_privilege" in privilege_call[0]
    assert connection.closed


@pytest.mark.parametrize(
    ("constraint_row", "expected_constraint", "expected_stable"),
    [
        (("order_id", "unique", True, "btree"), "unique", True),
        (("order_id", "primary_key", False, "btree"), "primary_key", False),
        (("order_id", "primary_key", True, "hash"), "primary_key", False),
        (None, "none", False),
    ],
)
def test_observe_reports_actual_key_constraint_and_stable_order(
    constraint_row: tuple[str, str, bool, str] | None,
    expected_constraint: str,
    expected_stable: bool,
) -> None:
    connection = FakeConnection()
    original_cursor = connection.cursor

    def metadata_cursor(name: str | None = None) -> FakeCursor:
        cursor = original_cursor(name)
        original_execute = cursor.execute

        def execute(query: Any, params: object = None) -> None:
            original_execute(query, params)
            rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
            if "pg_catalog.pg_constraint" in rendered:
                cursor.rows = [] if constraint_row is None else [constraint_row]

        cursor.execute = execute  # type: ignore[method-assign]
        return cursor

    connection.cursor = metadata_cursor  # type: ignore[method-assign]
    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr("postgresql://ignored"),
            connection_handle="source-account",
            schema_name="fixture",
            table_name="orders",
        ),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
    )

    observation = provider.observe()

    assert observation.key_constraint == expected_constraint
    assert observation.stable_key_order is expected_stable
    assert observation.key_name == ("order_id" if constraint_row else None)


@pytest.mark.parametrize("read_only", [False, None])
def test_observe_reports_non_read_only_or_unknown_access(read_only: bool | None) -> None:
    connection = FakeConnection()
    connection.read_only = read_only
    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr("postgresql://ignored"),
            connection_handle="source-account",
            schema_name="fixture",
            table_name="orders",
        ),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
    )

    observation = provider.observe()

    assert observation.read_only is read_only


def test_snapshot_is_read_only_repeatable_and_ordered_by_quoted_key() -> None:
    connection = FakeConnection()
    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr("postgresql://ignored"),
            connection_handle="source-account",
            schema_name="fixture-schema",
            table_name="order-table",
            key_name='order"id',
        ),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
    )

    boundary, rows = provider.read_snapshot()

    assert connection.calls[0][0] == ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    read_call = next(call[0] for call in connection.calls if "SELECT order_id" in call[0])
    assert 'FROM "fixture-schema"."order-table" ORDER BY "order""id"' in read_call
    assert [row.order_id for row in rows] == [7, 8]
    assert boundary.snapshot_identity == "10:20:"
    assert boundary.row_count == 2
    assert boundary.key_range_digest == digest({"key_min": 7, "key_max": 8})
    assert b'"key_min"' not in canonical_bytes(boundary)
    assert b'"key_max"' not in canonical_bytes(boundary)
    assert connection.committed and connection.closed
    assert rows.final_boundary().closed_at == NOW  # type: ignore[attr-defined]


def test_empty_snapshot_hashes_an_ordered_pair_of_null_bounds() -> None:
    connection = FakeConnection()
    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr("postgresql://ignored"),
            connection_handle="source-account",
            schema_name="fixture",
            table_name="orders",
        ),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
    )
    original_cursor = connection.cursor

    def empty_cursor(name: str | None = None) -> FakeCursor:
        cursor = original_cursor(name)
        original_execute = cursor.execute

        def execute(query: Any, params: object = None) -> None:
            original_execute(query, params)
            rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
            if "SELECT min" in rendered:
                cursor.rows = [(None, None, 0)]
            elif "SELECT order_id" in rendered:
                cursor.rows = []

        cursor.execute = execute  # type: ignore[method-assign]
        return cursor

    connection.cursor = empty_cursor  # type: ignore[method-assign]

    boundary, rows = provider.read_snapshot()
    assert list(rows) == []
    assert boundary.key_range_digest == digest({"key_min": None, "key_max": None})


def test_abandoned_snapshot_rolls_back_and_cannot_claim_a_closed_boundary() -> None:
    connection = FakeConnection()
    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr("postgresql://ignored"),
            connection_handle="source-account",
            schema_name="fixture",
            table_name="orders",
        ),
        connect=lambda _dsn: connection,
        clock=lambda: NOW,
    )
    _boundary, rows = provider.read_snapshot()

    next(iter(rows))
    rows.abort()  # type: ignore[attr-defined]

    assert connection.rolled_back and connection.closed
    with pytest.raises(RuntimeError, match="not been consumed"):
        rows.final_boundary()  # type: ignore[attr-defined]


def test_rejected_credentials_surface_the_structured_authorization_rejection() -> None:
    probe_calls: list[dict[str, object]] = []

    def rejected(_dsn: str) -> FakeConnection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> FakeConnection:
        probe_calls.append(parameters)
        raise psycopg.errors.InvalidPassword()

    provider = PostgresProvider(
        PostgresSettings(
            dsn=SecretStr(
                "host=source.internal dbname=source user=reader password=private-secret "
                "sslmode=disable gssencmode=disable"
            ),
            connection_handle="source-account",
            schema_name="fixture",
            table_name="orders",
        ),
        connect=rejected,
        startup_denial_probe=probe,
        clock=lambda: NOW,
    )

    with pytest.raises(psycopg.errors.InvalidPassword):
        provider.observe()

    assert len(probe_calls) == 1
