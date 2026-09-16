from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import psycopg
import pytest
from pillarmesh_provider_postgresql.destination import (
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreError,
    PostgreSQLLandStoreSettings,
    compose_postgresql_destination_provider,
)
from pillarmesh_provider_sdk import LandReceipt, ProviderError
from pillarmesh_provider_sdk.destination_conformance import (
    destination_segment,
    destination_target,
    run_destination_conformance,
)
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState


class _Store:
    def __init__(self) -> None:
        self.receipts: dict[str, LandReceipt] = {}
        self.fail_transport = False
        self.fail_after_commit = False
        self.classified_failure: str | None = None
        self.inspect_classified_failure: str | None = None

    def land_transactionally(self, *, receipt: LandReceipt, rows: tuple[bytes, ...]) -> LandReceipt:
        if self.classified_failure is not None:
            raise PostgreSQLLandStoreError(self.classified_failure)
        if self.fail_transport:
            raise OSError("network unavailable")
        existing = self.receipts.get(receipt.idempotency_key)
        if existing is not None:
            return existing
        self.receipts[receipt.idempotency_key] = receipt
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise TimeoutError("response lost")
        return receipt

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None:
        if self.inspect_classified_failure is not None:
            raise PostgreSQLLandStoreError(self.inspect_classified_failure)
        return self.receipts.get(idempotency_key)


def test_postgresql_passes_the_shared_destination_contract() -> None:
    asyncio.run(run_destination_conformance(lambda: PostgreSQLDestinationProvider(store=_Store())))


def test_postgresql_classifies_transport_failure() -> None:
    store = _Store()
    store.fail_transport = True
    provider = PostgreSQLDestinationProvider(store=store)

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="e" * 64,
            )
        assert captured.value.classification == "transient_transport"

    asyncio.run(exercise())


def test_postgresql_recovers_a_response_lost_after_atomic_commit() -> None:
    store = _Store()
    store.fail_after_commit = True
    provider = PostgreSQLDestinationProvider(
        store=store,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )

    receipt = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="f" * 64,
        )
    )

    assert receipt == store.receipts["f" * 64]
    assert len(store.receipts) == 1


@pytest.mark.parametrize(
    "classification",
    ("transient_unavailable", "throttled", "authorization_denied", "statement_rejected"),
)
def test_postgresql_preserves_driver_failure_classification(classification: str) -> None:
    store = _Store()
    store.classified_failure = classification
    provider = PostgreSQLDestinationProvider(store=store)

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="9" * 64,
            )
        assert captured.value.classification == classification

    asyncio.run(exercise())


def test_postgresql_commit_inspection_preserves_driver_failure_classification() -> None:
    store = _Store()
    provider = PostgreSQLDestinationProvider(store=store)
    receipt = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="6" * 64,
        )
    )
    store.inspect_classified_failure = "transient_unavailable"

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.inspect_commit(receipt=receipt)
        assert captured.value.classification == "transient_unavailable"

    asyncio.run(exercise())


def test_postgresql_timeout_reconciliation_preserves_driver_failure_classification() -> None:
    store = _Store()
    store.fail_after_commit = True
    store.inspect_classified_failure = "transient_unavailable"
    provider = PostgreSQLDestinationProvider(store=store)

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="5" * 64,
            )
        assert captured.value.classification == "transient_unavailable"

    asyncio.run(exercise())


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.receipt_payload: str | None = None
        self.pending_receipt_payload: str | None = None
        self.raw_inserts: list[tuple[object, ...]] = []
        self.statements: list[str] = []
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, query: object, params: tuple[object, ...] = ()) -> _Cursor:
        statement = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.statements.append(statement)
        if statement.startswith("SELECT"):
            row = None if self.receipt_payload is None else (self.receipt_payload,)
            return _Cursor(row)
        if 'INSERT INTO "raw"."raw_orders"' in statement:
            self.raw_inserts.append(params)
        if 'INSERT INTO "control"."land_receipts"' in statement:
            self.pending_receipt_payload = str(params[-1])
        return _Cursor()

    def commit(self) -> None:
        self.commit_count += 1
        self.receipt_payload = self.pending_receipt_payload

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        return None


def test_concrete_postgresql_store_commits_rows_and_receipt_once() -> None:
    connection = _Connection()
    store = PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn="postgresql://ingestion:secret@127.0.0.1/pillarmesh",
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        ),
        connect=lambda _dsn: connection,
    )
    provider = PostgreSQLDestinationProvider(store=store)

    first = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="7" * 64,
        )
    )
    replay = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="7" * 64,
        )
    )

    assert replay == first
    assert len(connection.raw_inserts) == 1
    assert connection.commit_count == 2
    assert connection.rollback_count == 0
    assert connection.receipt_payload == first.model_dump_json()


def test_concrete_postgresql_store_reconciles_timeout_after_commit() -> None:
    connection = _Connection()
    fired = False

    def lose_response() -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise TimeoutError("response lost")

    store = PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn="postgresql://ingestion:secret@127.0.0.1/pillarmesh",
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        ),
        connect=lambda _dsn: connection,
        after_commit_hook=lose_response,
    )

    receipt = asyncio.run(
        PostgreSQLDestinationProvider(store=store).land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="8" * 64,
        )
    )

    assert connection.receipt_payload == receipt.model_dump_json()
    assert len(connection.raw_inserts) == 1


def test_concrete_postgresql_store_classifies_connection_failure() -> None:
    def unavailable(_dsn: str) -> _Connection:
        raise psycopg.OperationalError("database unavailable")

    store = PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn="postgresql://ingestion:secret@127.0.0.1/pillarmesh",
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        ),
        connect=unavailable,
    )

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await PostgreSQLDestinationProvider(store=store).land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="4" * 64,
            )
        assert captured.value.classification == "transient_transport"

    asyncio.run(exercise())


def test_postgresql_binding_composition_resolves_private_settings_without_receipt_leakage() -> None:
    connection = _Connection()
    secret_dsn = "postgresql://ingestion:private-password@127.0.0.1/pillarmesh"
    resolved: list[tuple[str, str, int]] = []

    class SettingsAuthority:
        def resolve(
            self,
            *,
            tenant_id: str,
            binding_id: str,
            binding_revision: int,
        ) -> PostgreSQLLandStoreSettings:
            resolved.append((tenant_id, binding_id, binding_revision))
            return PostgreSQLLandStoreSettings(
                dsn=secret_dsn,
                raw_schema_name="raw",
                ledger_schema_name="control",
                ledger_table_name="land_receipts",
            )

    binding = WarehouseBinding(
        binding_id="warehouse-a",
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capability_profile_digest="1" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=4,
        created_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        updated_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        provisioned_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
    )
    provider = compose_postgresql_destination_provider(
        binding=binding,
        settings_authority=SettingsAuthority(),
        connect=lambda _dsn: connection,
    )

    receipt = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="4" * 64,
        )
    )

    assert resolved == [("tenant-a", "warehouse-a", 4)]
    assert "private-password" not in receipt.model_dump_json()
