from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from heinzel_semantic_registry.repository import SQLiteSemanticRepository


def test_a_borrowed_connection_serves_a_worker_thread(tmp_path: Path) -> None:
    """The console runs its backend on a threadpool.

    A repository that opens its own connection is bound to whichever thread built
    it, so the first read from a worker fails. Only the composer can decide that
    lifecycle, so it must be able to supply the connection.
    """
    connection = sqlite3.connect(str(tmp_path / "semantic.sqlite3"), check_same_thread=False)
    repository = SQLiteSemanticRepository(connection=connection)
    failures: list[BaseException] = []

    def read_from_worker() -> None:
        try:
            repository.has_authority_role(
                tenant_id="tenant-a", actor_id="actor-a", authority_ref="role:reviewer"
            )
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=read_from_worker)
    worker.start()
    worker.join()

    assert failures == []
    repository.close()
    # The borrowed connection outlives the repository, because its owner closes it.
    connection.execute("SELECT 1")
    connection.close()


def test_supplying_both_a_path_and_a_connection_is_refused(tmp_path: Path) -> None:
    connection = sqlite3.connect(str(tmp_path / "semantic.sqlite3"))
    try:
        with pytest.raises(ValueError, match="exactly one"):
            SQLiteSemanticRepository(str(tmp_path / "other.sqlite3"), connection=connection)
        with pytest.raises(ValueError, match="exactly one"):
            SQLiteSemanticRepository()
    finally:
        connection.close()
