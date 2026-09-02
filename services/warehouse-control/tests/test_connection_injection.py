"""The composer must be able to own the connection's lifecycle.

Starlette runs sync work on a threadpool worker, so a connection bound to the
thread that happened to construct the repository raises ProgrammingError on the
next request. Which connection to use, and how it is shared across threads, is a
decision for whoever composes the application - not something this repository can
make by connecting for itself.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from threading import Thread

from pillarmesh_warehouse_control import EngineKind
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _draft(repository: SQLiteWarehouseRepository) -> str:
    from pillarmesh_warehouse_control import WarehouseControlService

    service = WarehouseControlService(repository, clock=lambda: NOW)
    binding = service.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    return binding.binding_id


def test_a_path_still_opens_its_own_connection() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    try:
        assert _draft(repository).startswith("whb-")
    finally:
        repository.close()


def test_an_injected_connection_is_used_as_given() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteWarehouseRepository(connection=connection)
        binding_id = _draft(repository)

        stored = connection.execute(
            "SELECT COUNT(*) FROM warehouse_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        assert stored[0] == 1
    finally:
        connection.close()


def test_closing_the_repository_leaves_a_borrowed_connection_open() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        repository = SQLiteWarehouseRepository(connection=connection)
        repository.close()

        assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        connection.close()


def test_an_injected_connection_serves_a_threadpool_worker() -> None:
    """This is the case that fails in the console today."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        repository = SQLiteWarehouseRepository(connection=connection)
        results: list[object] = []

        def work() -> None:
            try:
                results.append(_draft(repository))
            except BaseException as error:  # recorded so the assertion can report it
                results.append(error)

        worker = Thread(target=work)
        worker.start()
        worker.join(timeout=10)

        assert len(results) == 1
        assert isinstance(results[0], str), results[0]
    finally:
        connection.close()


def test_supplying_neither_or_both_is_refused() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for arguments in ({}, {"database_path": ":memory:", "connection": connection}):
            try:
                SQLiteWarehouseRepository(**arguments)  # type: ignore[arg-type]
            except ValueError:
                continue
            raise AssertionError(f"expected a refusal for {sorted(arguments)}")
    finally:
        connection.close()
