from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_catalog_control.repository import SQLiteCatalogRepository


def test_a_borrowed_connection_serves_a_worker_thread(tmp_path: Path) -> None:
    """Console command routes run the backend on a threadpool.

    A repository that opens its own connection is bound to whichever thread
    constructed it, so the first request from a worker fails. The composer is the
    only party that can decide the connection's thread tolerance, so it must be able
    to supply one.
    """
    connection = sqlite3.connect(str(tmp_path / "catalog.sqlite3"), check_same_thread=False)
    repository = SQLiteCatalogRepository(connection=connection)
    failures: list[BaseException] = []
    created: list[str] = []

    def create_from_worker() -> None:
        try:
            created.append(repository.create_draft("tenant-a", _now()).binding_id)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=create_from_worker)
    worker.start()
    worker.join()

    assert failures == []
    assert len(created) == 1
    repository.close()
    # The borrowed connection outlives the repository, because its owner closes it.
    connection.execute("SELECT 1")
    connection.close()


def test_supplying_both_a_path_and_a_connection_is_refused(tmp_path: Path) -> None:
    connection = sqlite3.connect(str(tmp_path / "catalog.sqlite3"))
    try:
        with pytest.raises(ValueError, match="exactly one"):
            SQLiteCatalogRepository(str(tmp_path / "other.sqlite3"), connection=connection)
        with pytest.raises(ValueError, match="exactly one"):
            SQLiteCatalogRepository()
    finally:
        connection.close()


def _now() -> datetime:
    return datetime(2026, 9, 3, 12, tzinfo=UTC)
