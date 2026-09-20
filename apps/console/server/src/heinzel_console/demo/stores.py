"""The SQLite stores the demonstration console owns, opened under one state directory."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from heinzel_catalog_control import SQLiteCatalogRepository
from heinzel_request_management import SQLiteRequestRepository
from heinzel_semantic_registry import SQLiteCatalogPublicationRepository
from heinzel_semantic_registry.repository import SQLiteSemanticRepository

__all__ = ["DemoStores", "default_state_directory"]


class _Closeable(Protocol):
    def close(self) -> None: ...


def _close_each(closings: Sequence[_Closeable]) -> BaseException | None:
    """Close every handle in order and return the first failure, if any.

    Every close is attempted even when an earlier one raises, so that no handle is
    abandoned by an early failure.
    """
    failure: BaseException | None = None
    for closing in closings:
        try:
            closing.close()
        except BaseException as error:
            failure = failure or error
    return failure


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
        # The XDG base directory specification requires a relative value to be ignored.
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate / "heinzel"
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
        # Each handle joins this list as it opens, so that a store which fails to open
        # leaves nothing behind it: a list assembled only after the last store opened
        # would abandon every connection already made.
        opened: list[_Closeable] = []
        try:
            requests_connection = self._connect(state_dir / "requests.sqlite3")
            opened.append(requests_connection)
            self.requests = SQLiteRequestRepository(requests_connection)
            opened.append(self.requests)
            semantic_connection = self._connect(state_dir / "semantic.sqlite3")
            opened.append(semantic_connection)
            self.semantic = SQLiteSemanticRepository(connection=semantic_connection)
            opened.append(self.semantic)
            catalog_connection = self._connect(state_dir / "catalog.sqlite3")
            opened.append(catalog_connection)
            self.catalog = SQLiteCatalogRepository(connection=catalog_connection)
            opened.append(self.catalog)
            self.publications = SQLiteCatalogPublicationRepository(
                str(state_dir / "publications.sqlite3"), check_same_thread=False
            )
            opened.append(self.publications)
        except BaseException:
            # Best-effort clean-up: a failure to close must not replace the failure to open.
            _close_each(tuple(reversed(opened)))
            raise
        # Reverse of the order the stores were opened in, so each borrowed connection is
        # closed after the repository that borrowed it.
        self._closings: tuple[_Closeable, ...] = tuple(reversed(opened))
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
        failure = _close_each(self._closings)
        if failure is not None:
            raise failure
