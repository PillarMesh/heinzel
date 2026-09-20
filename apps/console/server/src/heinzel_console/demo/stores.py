"""The SQLite stores the demonstration console owns, opened under one state directory."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Protocol

from heinzel_catalog_control import SQLiteCatalogRepository
from heinzel_request_management import SQLiteRequestRepository
from heinzel_semantic_registry import SQLiteCatalogPublicationRepository
from heinzel_semantic_registry.repository import SQLiteSemanticRepository

__all__ = ["DemoStores", "default_state_directory"]


class _Closeable(Protocol):
    def close(self) -> None: ...


def default_state_directory() -> Path:
    """The per-user directory the demonstration keeps its stores in.

    This is deliberately a per-user location and never a shared temporary directory. A
    shared `/tmp` name is predictable, so the second user to run the demonstration meets a
    private directory owned by the first. The secret store's owner check then fails, and the
    orchestrator reports `invalid_provider_response` — a permanent failure that names no
    cause, leaving the second user with nothing to act on.
    """
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "heinzel"
    return Path.home() / ".local/state/heinzel"


class DemoStores:
    """Every store the demonstration console reads and writes, under one directory.

    Connections are opened with `check_same_thread=False` because the console runs its
    backend on a threadpool, and a SQLite connection otherwise carries the affinity of the
    thread that opened it. This class owns those connections; the repositories that borrow
    one do not close it.
    """

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir
        requests_connection = self._connect(state_dir / "requests.sqlite3")
        semantic_connection = self._connect(state_dir / "semantic.sqlite3")
        catalog_connection = self._connect(state_dir / "catalog.sqlite3")
        self.requests = SQLiteRequestRepository(requests_connection)
        self.semantic = SQLiteSemanticRepository(connection=semantic_connection)
        self.catalog = SQLiteCatalogRepository(connection=catalog_connection)
        self.publications = SQLiteCatalogPublicationRepository(
            str(state_dir / "publications.sqlite3"), check_same_thread=False
        )
        # Reverse of the order the stores were opened in, each borrowed connection closed
        # after the repository that borrowed it.
        self._closings: tuple[_Closeable, ...] = (
            self.publications,
            self.catalog,
            catalog_connection,
            self.semantic,
            semantic_connection,
            self.requests,
            requests_connection,
        )
        self._closed = False

    @staticmethod
    def _connect(database_path: Path) -> sqlite3.Connection:
        return sqlite3.connect(str(database_path), check_same_thread=False)

    def close(self) -> None:
        """Close every store, then raise the first failure.

        Every close is attempted even when an earlier one raises, so that no handle is
        abandoned and the state directory stays reusable. A second call does nothing.
        """
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        for closing in self._closings:
            try:
                closing.close()
            # Every close is attempted; the first failure is re-raised below.
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure
