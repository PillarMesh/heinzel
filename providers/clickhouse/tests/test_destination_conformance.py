from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from heinzel_provider_clickhouse.destination import (
    ClickHouseDestinationProvider,
    ClickHouseLandStore,
    ClickHouseLandStoreError,
    ClickHouseLandStoreSettings,
    clickhouse_insert_token,
    compose_clickhouse_destination_provider,
)
from heinzel_provider_sdk import LandReceipt, ProviderError
from heinzel_provider_sdk.destination_conformance import (
    destination_segment,
    destination_target,
    run_destination_conformance,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState


class _Store:
    def __init__(self) -> None:
        self.receipts: dict[str, LandReceipt] = {}
        self.insert_tokens: list[str] = []
        self.fail_transport = False
        self.fail_receipt_once = False
        self.classified_failure: str | None = None
        self.inspect_classified_failure: str | None = None

    def insert_segment(
        self,
        *,
        receipt: LandReceipt,
        rows: tuple[bytes, ...],
        insert_token: str,
    ) -> None:
        if self.fail_transport:
            raise OSError("network unavailable")
        if self.classified_failure is not None:
            raise ClickHouseLandStoreError(self.classified_failure)
        self.insert_tokens.append(insert_token)

    def record_receipt(self, *, receipt: LandReceipt) -> LandReceipt:
        if self.fail_receipt_once:
            self.fail_receipt_once = False
            raise TimeoutError("receipt response lost")
        existing = self.receipts.get(receipt.idempotency_key)
        if existing is not None:
            return existing
        self.receipts[receipt.idempotency_key] = receipt
        return receipt

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None:
        if self.inspect_classified_failure is not None:
            raise ClickHouseLandStoreError(self.inspect_classified_failure)
        return self.receipts.get(idempotency_key)

    def inspect_segment(self, *, receipt: LandReceipt, insert_token: str) -> bool:
        return insert_token in self.insert_tokens


def test_clickhouse_passes_the_shared_destination_contract() -> None:
    asyncio.run(run_destination_conformance(lambda: ClickHouseDestinationProvider(store=_Store())))


def test_clickhouse_insert_token_is_deterministic_and_segment_scoped() -> None:
    first = clickhouse_insert_token(idempotency_key="a" * 64, segment_digest="b" * 64)
    replay = clickhouse_insert_token(idempotency_key="a" * 64, segment_digest="b" * 64)
    changed = clickhouse_insert_token(idempotency_key="a" * 64, segment_digest="c" * 64)

    assert first == replay
    assert first != changed


def test_clickhouse_classifies_transport_failure() -> None:
    store = _Store()
    store.fail_transport = True
    provider = ClickHouseDestinationProvider(
        store=store,
        clock=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="e" * 64,
            )
        assert captured.value.classification == "transient_transport"

    asyncio.run(exercise())


class _Client:
    def __init__(self) -> None:
        self.receipt_payload: str | None = None
        self.inserted_generation: tuple[str, str, int, str] | None = None
        self.statements: list[str] = []
        self.inserted_payload: object | None = None

    def execute(self, statement: str) -> bytes:
        self.statements.append(statement)
        if "INSERT INTO `raw`.`raw_orders`" in statement:
            lines = statement.splitlines()
            payload = json.loads(lines[-1])
            self.inserted_generation = (
                payload["generation_id"],
                payload["segment_digest"],
                1,
                payload["insert_token"],
            )
            self.inserted_payload = payload["payload"]
        if "INSERT INTO `control`.`land_receipts`" in statement:
            payload = json.loads(statement.splitlines()[-1])
            self.receipt_payload = payload["receipt_payload"]
        return b""

    def query_lines(self, statement: str) -> tuple[str, ...]:
        self.statements.append(statement)
        if "receipt_payload" in statement:
            return () if self.receipt_payload is None else (self.receipt_payload,)
        if self.inserted_generation is None:
            return ("0\t0\t\\N\t\\N",)
        generation_id, segment_digest, count, insert_token = self.inserted_generation
        if generation_id not in statement:
            return ("0\t0\t\\N\t\\N",)
        return (f"{count}\t1\t{segment_digest}\t{insert_token}",)

    def close(self) -> None:
        return None


def test_concrete_clickhouse_store_inserts_with_token_and_records_receipt() -> None:
    client = _Client()
    store = ClickHouseLandStore(
        ClickHouseLandStoreSettings(
            endpoint="http://127.0.0.1:8123",
            username="ingestion",
            password="secret",
            raw_database_name="raw",
            ledger_database_name="control",
            ledger_table_name="land_receipts",
        ),
        client=client,
    )
    provider = ClickHouseDestinationProvider(store=store)

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
    raw_inserts = [item for item in client.statements if "INSERT INTO `raw`.`raw_orders`" in item]
    assert len(raw_inserts) == 1
    assert first.provider_commit_ref in raw_inserts[0]
    assert client.inserted_payload == destination_segment().rows[0].decode()


def test_concrete_clickhouse_store_reconciles_timeout_after_insert() -> None:
    client = _Client()
    fired = False

    def lose_response() -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise TimeoutError("response lost")

    store = ClickHouseLandStore(
        ClickHouseLandStoreSettings(
            endpoint="http://127.0.0.1:8123",
            username="ingestion",
            password="secret",
            raw_database_name="raw",
            ledger_database_name="control",
            ledger_table_name="land_receipts",
        ),
        client=client,
        after_insert_hook=lose_response,
    )

    receipt = asyncio.run(
        ClickHouseDestinationProvider(store=store).land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="8" * 64,
        )
    )

    assert client.receipt_payload == receipt.model_dump_json()
    raw_inserts = [item for item in client.statements if "INSERT INTO `raw`.`raw_orders`" in item]
    assert len(raw_inserts) == 1


def test_clickhouse_reconciles_an_insert_committed_before_the_receipt() -> None:
    store = _Store()
    store.fail_receipt_once = True
    provider = ClickHouseDestinationProvider(
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

    assert store.receipts["f" * 64] == receipt
    assert len(store.insert_tokens) == 1


@pytest.mark.parametrize(
    "classification",
    ("transient_unavailable", "throttled", "authorization_denied", "statement_rejected"),
)
def test_clickhouse_preserves_driver_failure_classification(classification: str) -> None:
    store = _Store()
    store.classified_failure = classification
    provider = ClickHouseDestinationProvider(store=store)

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="9" * 64,
            )
        assert captured.value.classification == classification

    asyncio.run(exercise())


def test_clickhouse_commit_inspection_preserves_driver_failure_classification() -> None:
    store = _Store()
    provider = ClickHouseDestinationProvider(store=store)
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


def test_clickhouse_timeout_reconciliation_preserves_driver_failure_classification() -> None:
    class ReconciliationFailureStore(_Store):
        def __init__(self) -> None:
            super().__init__()
            self.inspect_calls = 0

        def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None:
            self.inspect_calls += 1
            if self.inspect_calls > 1:
                raise ClickHouseLandStoreError("transient_unavailable")
            return super().inspect_receipt(idempotency_key=idempotency_key)

    store = ReconciliationFailureStore()
    store.fail_receipt_once = True
    provider = ClickHouseDestinationProvider(store=store)

    async def exercise() -> None:
        with pytest.raises(ProviderError) as captured:
            await provider.land(
                segment=destination_segment(),
                target=destination_target(),
                idempotency_key="5" * 64,
            )
        assert captured.value.classification == "transient_unavailable"

    asyncio.run(exercise())


def test_clickhouse_binding_composition_resolves_private_settings_without_receipt_leakage() -> None:
    client = _Client()
    secret_password = "private-password"
    resolved: list[tuple[str, str, int]] = []

    class SettingsAuthority:
        def resolve(
            self,
            *,
            tenant_id: str,
            binding_id: str,
            binding_revision: int,
        ) -> ClickHouseLandStoreSettings:
            resolved.append((tenant_id, binding_id, binding_revision))
            return ClickHouseLandStoreSettings(
                endpoint="http://127.0.0.1:8123",
                username="ingestion",
                password=secret_password,
                raw_database_name="raw",
                ledger_database_name="control",
                ledger_table_name="land_receipts",
            )

    binding = WarehouseBinding(
        binding_id="warehouse-a",
        tenant_id="tenant-a",
        engine_kind=EngineKind.CLICKHOUSE,
        region="local",
        capability_profile_digest="1" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=4,
        created_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        updated_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        provisioned_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
    )
    provider = compose_clickhouse_destination_provider(
        binding=binding,
        settings_authority=SettingsAuthority(),
        client=client,
    )

    receipt = asyncio.run(
        provider.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="4" * 64,
        )
    )

    assert resolved == [("tenant-a", "warehouse-a", 4)]
    assert secret_password not in receipt.model_dump_json()
