from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import snowflake.connector.errors
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import OrderRow, ProviderError, SegmentManifest
from heinzel_provider_snowflake import SnowflakeProvider, SnowflakeSettings
from pydantic import SecretStr

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.ledger: dict[str, tuple[str, datetime]] = {}
        self.visibility_row: tuple[Any, ...] | None = None
        self.lose_commit_response = False
        self.query_number = 0
        self.fail_with: BaseException | None = None
        self.fail_merge_with: BaseException | None = None
        self.object_types = {"ORDERS": "BASE TABLE", "COMMIT_LEDGER": "BASE TABLE"}

    def connect(self, **_kwargs: object) -> Connection:
        return Connection(self)


class Cursor:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.rows: list[tuple[Any, ...]] = []
        self.sfqid = ""
        self.pending: tuple[str, str] | None = None
        self.rowcount = 0

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> Cursor:
        self.backend.query_number += 1
        self.sfqid = f"query-{self.backend.query_number}"
        self.backend.calls.append((query, params))
        if self.backend.fail_with is not None:
            raise self.backend.fail_with
        normalized = " ".join(query.split()).upper()
        self.rows = []
        if normalized.startswith("SELECT MANIFEST_DIGEST"):
            batch_id = params[0]  # type: ignore[index]
            row = self.backend.ledger.get(batch_id)
            self.rows = [row] if row else []
        elif "FROM INFORMATION_SCHEMA.TABLES" in normalized:
            table_name = params[2]  # type: ignore[index]
            table_type = self.backend.object_types.get(table_name)
            self.rows = [(table_type,)] if table_type is not None else []
        elif "FROM INFORMATION_SCHEMA.COLUMNS" in normalized:
            table_name = params[2]  # type: ignore[index]
            if table_name == "ORDERS":
                self.rows = [
                    ("ORDER_ID", "NUMBER", "NO", 19, 0, None, None),
                    ("CUSTOMER_REF", "TEXT", "NO", None, None, 65_535, None),
                    ("AMOUNT", "NUMBER", "NO", 18, 2, None, None),
                    ("CURRENCY", "TEXT", "NO", None, None, 3, None),
                    ("ORDER_STATUS", "TEXT", "NO", None, None, 65_535, None),
                    ("UPDATED_AT", "TIMESTAMP_TZ", "NO", None, None, None, 6),
                ]
            else:
                self.rows = [
                    ("BATCH_ID", "TEXT", "NO", None, None, 16_777_216, None),
                    ("MANIFEST_DIGEST", "TEXT", "NO", None, None, 64, None),
                    ("COMMITTED_AT", "TIMESTAMP_TZ", "NO", None, None, None, 9),
                ]
        elif "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in normalized:
            table_name = params[2]  # type: ignore[index]
            self.rows = [("ORDER_ID" if table_name == "ORDERS" else "BATCH_ID", "PRIMARY KEY")]
        elif normalized.startswith("INSERT INTO"):
            batch_id, manifest_digest = params[:2]  # type: ignore[index]
            self.pending = (batch_id, manifest_digest)
        elif normalized == "COMMIT":
            if self.pending:
                self.backend.ledger[self.pending[0]] = (self.pending[1], NOW)
            if self.backend.lose_commit_response:
                self.backend.lose_commit_response = False
                raise RuntimeError("connection lost after commit")
        elif "WHERE ORDER_ID = %S" in normalized:
            self.rows = [self.backend.visibility_row] if self.backend.visibility_row else []
        elif normalized.startswith("MERGE INTO"):
            if self.backend.fail_merge_with is not None:
                raise self.backend.fail_merge_with
            self.rowcount = 1
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Connection:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def cursor(self) -> Cursor:
        return Cursor(self.backend)

    def close(self) -> None:
        return None


def settings() -> SnowflakeSettings:
    return SnowflakeSettings(
        account="account",
        user="runtime_user",
        password=SecretStr("secret-canary"),
        role="HEINZEL_SNAPSHOT_RUNTIME",
        warehouse="HEINZEL_SNAPSHOT_WH",
        database="HEINZEL_SNAPSHOT",
        schema_name="TRANSFER",
        stage="SNAPSHOT_STAGE",
        target_table="ORDERS",
        ledger_table="COMMIT_LEDGER",
        connection_handle="destination-account",
    )


def manifest(path: Path, *, batch_id: str = "batch-1", acceptance_key: int = 7) -> SegmentManifest:
    content = path.read_bytes()
    return SegmentManifest(
        batch_id=batch_id,
        segment_name="segment.csv",
        segment_digest=hashlib.sha256(content).hexdigest(),
        row_set_digest="1" * 64,
        row_count=1,
        encoded_bytes=len(content),
        schema_digest="2" * 64,
        source_boundary_digest="3" * 64,
        acceptance_value_digest=digest(
            OrderRow(
                order_id=acceptance_key,
                customer_ref="customer-7",
                amount=Decimal("10.50"),
                currency="USD",
                status="paid",
                updated_at=NOW,
            )
        ),
    )


def test_stage_checks_bytes_and_uses_batch_scoped_prefix(tmp_path: Path) -> None:
    backend = Backend()
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)

    provider.stage(segment, expected)

    put = next(query for query, _params in backend.calls if query.startswith("PUT "))
    assert "@HEINZEL_SNAPSHOT.TRANSFER.SNAPSHOT_STAGE/runs/batch-1 " in put
    assert "secret-canary" not in repr(provider)

    segment.write_bytes(b"changed")
    with pytest.raises(ProviderError, match="digest"):
        provider.stage(segment, expected)

    unsafe = manifest(segment, batch_id="batch/escape").model_copy(
        update={"segment_digest": hashlib.sha256(b"changed").hexdigest()}
    )
    with pytest.raises(ProviderError, match="stage path"):
        provider.stage(segment, unsafe)


def test_observation_normalizes_exact_legal_schema_and_capabilities() -> None:
    backend = Backend()
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    observation = provider.observe()

    assert tuple(column.type_name for column in observation.columns) == (
        "NUMBER(19,0)",
        "VARCHAR(65535)",
        "NUMBER(18,2)",
        "VARCHAR(3)",
        "VARCHAR(65535)",
        "TIMESTAMP_TZ(6)",
    )
    assert observation.capabilities == (
        "commit_ledger",
        "idempotent_merge",
        "stage_write",
        "visibility_query",
    )
    assert observation.key_name == "order_id"
    assert observation.key_nullable is False
    assert observation.key_constraint == "primary_key"
    assert observation.object_kind == "base_table"
    assert tuple(column.type_name for column in observation.commit_ledger_columns or ()) == (
        "VARCHAR(16777216)",
        "VARCHAR(64)",
        "TIMESTAMP_TZ(9)",
    )
    assert observation.commit_ledger_key_name == "batch_id"
    assert observation.commit_ledger_key_constraint == "primary_key"
    assert observation.commit_ledger_object_kind == "base_table"
    assert sum("FROM information_schema.tables" in query for query, _params in backend.calls) == 2


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (("CUSTOMER_REF", "TEXT", "NO", None, None, 128, None), "VARCHAR(128)"),
        (("UPDATED_AT", "TIMESTAMP_TZ", "NO", None, None, None, 3), "TIMESTAMP_TZ(3)"),
    ],
)
def test_observation_preserves_actual_varchar_length_and_timestamp_precision(
    row: tuple[Any, ...], expected: str
) -> None:
    assert SnowflakeProvider._normalize_column(row).type_name == expected


@pytest.mark.parametrize(
    ("table", "table_type", "expected"),
    [
        ("ORDERS", "VIEW", "view"),
        ("ORDERS", None, "unknown"),
        ("COMMIT_LEDGER", "VIEW", "view"),
        ("COMMIT_LEDGER", None, "unknown"),
    ],
)
def test_observation_reports_actual_or_unknown_object_kinds(
    table: str, table_type: str | None, expected: str
) -> None:
    backend = Backend()
    if table_type is None:
        backend.object_types.pop(table)
    else:
        backend.object_types[table] = table_type
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    observation = provider.observe()

    actual = observation.object_kind if table == "ORDERS" else observation.commit_ledger_object_kind
    assert actual == expected


def test_existing_batch_replays_or_rejects_conflicting_manifest(tmp_path: Path) -> None:
    backend = Backend()
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    expected_digest = digest(expected)
    backend.ledger[expected.batch_id] = (expected_digest, NOW)
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    receipt = provider.commit_or_resolve(expected)

    assert receipt.replayed
    assert receipt.manifest_digest == expected_digest
    backend.ledger[expected.batch_id] = ("f" * 64, NOW)
    with pytest.raises(ProviderError, match="conflicting manifest") as error:
        provider.commit_or_resolve(expected)
    assert error.value.classification == "permanent"


def test_absent_batch_merges_and_records_ledger_in_one_transaction(tmp_path: Path) -> None:
    backend = Backend()
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    receipt = provider.commit_or_resolve(expected)

    statements = [" ".join(query.split()).upper() for query, _params in backend.calls]
    assert "BEGIN" in statements
    assert any(statement.startswith("MERGE INTO") for statement in statements)
    assert any(statement.startswith("INSERT INTO") for statement in statements)
    assert "COMMIT" in statements
    assert backend.ledger[expected.batch_id][0] == digest(expected)
    assert not receipt.replayed


def test_commit_receipt_uses_opaque_ledger_identity(tmp_path: Path) -> None:
    backend = Backend()
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    provider_settings = settings()
    provider = SnowflakeProvider(provider_settings, connect=backend.connect, clock=lambda: NOW)
    qualified_ledger = (
        f"{provider_settings.database}.{provider_settings.schema_name}."
        f"{provider_settings.ledger_table}"
    )

    receipt = provider.commit_or_resolve(expected)

    assert receipt.ledger_identity == digest({"provider": "snowflake", "ledger": qualified_ledger})
    assert qualified_ledger.encode() not in canonical_bytes(receipt)


def test_lost_commit_response_is_resolved_from_ledger_without_second_merge(tmp_path: Path) -> None:
    backend = Backend()
    backend.lose_commit_response = True
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    receipt = provider.commit_or_resolve(expected)

    merge_count = sum(query.lstrip().upper().startswith("MERGE INTO") for query, _ in backend.calls)
    assert merge_count == 1
    assert receipt.replayed


def test_visibility_uses_fresh_query_and_requires_value_digest(tmp_path: Path) -> None:
    backend = Backend()
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    acceptance_key = 984201
    expected = manifest(segment, acceptance_key=acceptance_key)
    backend.visibility_row = (7, "customer-7", Decimal("10.50"), "USD", "paid", NOW)
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    backend.visibility_row = (
        acceptance_key,
        "customer-7",
        Decimal("10.50"),
        "USD",
        "paid",
        NOW,
    )

    proof = provider.verify_visibility(expected, acceptance_key)

    assert proof.value_digest == expected.acceptance_value_digest
    assert proof.query_id.startswith("query-")
    assert str(acceptance_key).encode() not in canonical_bytes(proof)
    visibility_call = next(
        (query, params) for query, params in backend.calls if "WHERE order_id = %s" in query
    )
    assert visibility_call[1] == (acceptance_key,)
    backend.visibility_row = (acceptance_key, "changed", Decimal("10.50"), "USD", "paid", NOW)
    with pytest.raises(ProviderError, match="digest"):
        provider.verify_visibility(expected, acceptance_key)


def test_driver_failures_surface_as_classified_provider_errors(tmp_path: Path) -> None:
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    transport = snowflake.connector.errors.OperationalError("connection reset")
    statement = snowflake.connector.errors.ProgrammingError("SQL compilation error")

    for error, classification in ((transport, "retryable"), (statement, "permanent")):
        backend = Backend()
        backend.fail_with = error
        provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

        # A raw driver exception here bypasses the runtime's bounded retry entirely.
        operations: tuple[Callable[[], object], ...] = (
            partial(provider.stage, segment, expected),
            partial(provider.commit_or_resolve, expected),
            partial(provider.verify_visibility, expected, 7),
            provider.observe,
        )
        for call in operations:
            with pytest.raises(ProviderError) as raised:
                call()
            assert raised.value.classification == classification
            assert raised.value.__cause__ is error


def test_transient_merge_failure_stays_retryable(tmp_path: Path) -> None:
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"header\nrow\n")
    expected = manifest(segment)
    backend = Backend()
    backend.fail_merge_with = snowflake.connector.errors.OperationalError("warehouse suspended")
    provider = SnowflakeProvider(settings(), connect=backend.connect, clock=lambda: NOW)

    with pytest.raises(ProviderError) as raised:
        provider.commit_or_resolve(expected)

    assert raised.value.classification == "retryable"
