import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseControlService
from pillarmesh_warehouse_control import repository as repository_module
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def draft(control: WarehouseControlService) -> WarehouseBinding:
    return control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )


class BarrierConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        barrier: Barrier,
        atomic_allocations: Queue[None],
    ) -> None:
        self._connection = connection
        self._barrier = barrier
        self._atomic_allocations = atomic_allocations

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("SELECT next_sequence FROM warehouse_sequences"):
            raise AssertionError("warehouse sequence allocation must use one atomic statement")
        if sql.startswith("INSERT INTO warehouse_sequences"):
            self._atomic_allocations.put(None)
            self._barrier.wait(timeout=5)
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def test_repository_load_filters_tenant_before_returning_binding(tmp_path: Path) -> None:
    repository = SQLiteWarehouseRepository(str(tmp_path / "warehouse.db"))
    control = WarehouseControlService(repository, clock=lambda: NOW)
    warehouse_binding = draft(control)

    assert repository.load("tenant-b", warehouse_binding.binding_id) is None


def test_two_connections_allocate_distinct_sequences_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "warehouse.db"
    SQLiteWarehouseRepository(str(database_path))
    barrier = Barrier(2)
    atomic_allocations: Queue[None] = Queue()
    original_connect = sqlite3.connect
    outcomes: Queue[WarehouseBinding | Exception] = Queue()

    def connect(database: str, *args: object, **kwargs: object) -> BarrierConnection:
        return BarrierConnection(
            original_connect(database, *args, **kwargs), barrier, atomic_allocations
        )

    monkeypatch.setattr(repository_module.sqlite3, "connect", connect)

    def create_draft() -> None:
        control = WarehouseControlService(
            SQLiteWarehouseRepository(str(database_path)), clock=lambda: NOW
        )
        try:
            outcomes.put(draft(control))
        except Exception as error:
            outcomes.put(error)

    first = Thread(target=create_draft)
    second = Thread(target=create_draft)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert atomic_allocations.qsize() == 2
    results = [outcomes.get_nowait() for _ in range(2)]
    bindings = [result for result in results if isinstance(result, WarehouseBinding)]
    assert len(bindings) == 2
    assert bindings[0].binding_id != bindings[1].binding_id


def test_stored_payload_is_the_canonical_form_the_platform_digests() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))

    payload = repository._connection.execute("SELECT payload FROM warehouse_bindings").fetchone()[0]

    # Every artifact here is content-addressed by digest(); a row serialized any other
    # way hashes differently and reads as corruption to the first consumer that checks.
    assert bytes(payload) == canonical_bytes(binding)
    assert hashlib.sha256(payload).hexdigest() == digest(binding)
