from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from heinzel_console.demo import stores as demo_stores
from heinzel_console.demo.stores import DemoStores, default_state_directory
from heinzel_contract_model import ArtifactReference


class _RefusingRepository:
    """Stands in for the last store opened, and refuses to open."""

    def __init__(self, database_path: str, *, check_same_thread: bool = True) -> None:
        raise RuntimeError("the publication store refused to open")

    def close(self) -> None:  # pragma: no cover - never constructed successfully
        raise AssertionError("a store that never opened must never be closed")


def test_the_state_directory_is_created_when_absent(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        assert (tmp_path / "state").is_dir()
    finally:
        stores.close()


def test_every_store_opens_under_the_state_directory(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        names = {path.name for path in (tmp_path / "state").iterdir()}
    finally:
        stores.close()
    assert {"requests.sqlite3", "semantic.sqlite3", "catalog.sqlite3"} <= names


def test_every_store_can_be_read_from_another_thread(tmp_path: Path) -> None:
    """The console serves its backend on a threadpool, so a thread-affine store is a 500.

    A SQLite connection opened with `check_same_thread` left on raises
    `sqlite3.ProgrammingError` for any thread but the one that opened it. Every test that
    reads these stores on the main thread passes either way, which is why this one does not.
    """
    stores = DemoStores(tmp_path / "state")
    product_ref = ArtifactReference(artifact_id="orders_daily", version=1, digest="a" * 64)

    def read() -> tuple[object, ...]:
        return (
            stores.requests.connection.execute("SELECT 1").fetchone(),
            stores.publications.list_publications(tenant_id="tenant-demo"),
            stores.product_publications.read_for_product_generation(
                tenant_id="tenant-demo", product_ref=product_ref, generation=1
            ),
            stores.product_versions.read_current(
                tenant_id="tenant-demo", product_ref=product_ref, generation=1
            ),
            stores.query_bindings.read_current(
                tenant_id="tenant-demo", product_ref=product_ref, generation=1
            ),
            stores.source_freshness.read_for_generation(
                tenant_id="tenant-demo", input_generation_digest="b" * 64
            ),
            stores.generations.load("absent-generation"),
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read).result() == ((1,), (), None, None, None, None, None)
    finally:
        stores.close()


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    stores.close()
    stores.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_parent_fails_before_any_store_opens(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        # The failure must come from the directory, not from a store: opening one first
        # raises `sqlite3.OperationalError`, which is not an `OSError` and fails here.
        with pytest.raises(PermissionError):
            DemoStores(blocked / "state")
    finally:
        blocked.chmod(0o700)


def test_a_store_that_fails_to_open_closes_the_connections_already_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    connect = DemoStores._connect

    # Patched on the class rather than on `sqlite3.connect`, so that a parallel run of this
    # suite cannot see a connect function belonging to this test.
    def recording_connect(database_path: Path) -> sqlite3.Connection:
        connection = connect(database_path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(DemoStores, "_connect", staticmethod(recording_connect))
    monkeypatch.setattr(demo_stores, "SQLiteCatalogPublicationRepository", _RefusingRepository)

    with pytest.raises(RuntimeError, match="refused to open"):
        DemoStores(tmp_path / "state")

    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_the_default_state_directory_follows_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-example")
    assert default_state_directory() == Path("/tmp/xdg-example/heinzel")


def test_a_relative_xdg_state_home_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The XDG base directory specification says a relative value must be ignored."""
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    monkeypatch.setenv("HOME", "/tmp/home-example")
    assert default_state_directory() == Path("/tmp/home-example/.local/state/heinzel")


def test_the_default_state_directory_falls_back_to_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/home-example")
    assert default_state_directory() == Path("/tmp/home-example/.local/state/heinzel")
