from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from heinzel_console.demo import stores as demo_stores
from heinzel_console.demo.stores import DemoStores, default_state_directory
from heinzel_contract_model import ArtifactReference
from heinzel_contract_service import SQLiteAcquisitionContractLifecycleRepository
from heinzel_evidence import SQLiteStore
from heinzel_state import AcquisitionStateNotFoundError, CursorCipher


class _CursorCipher:
    """Stands in for the demonstration's cursor cipher, with the properties it must have.

    Reversible and tenant-bound, because the state repository checks both: it refuses a
    ciphertext that still contains its plaintext, and it decrypts under the tenant that
    stored the cursor. A cipher that returned its input would pass neither.
    """

    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        return b"cipher:" + tenant_id.encode() + b":" + plaintext[::-1]

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        prefix = b"cipher:" + tenant_id.encode() + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("cursor tenant mismatch")
        return ciphertext.removeprefix(prefix)[::-1]


def _cursor_cipher(_state_dir: Path) -> CursorCipher:
    return _CursorCipher()


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
    assert {
        "requests.sqlite3",
        "semantic.sqlite3",
        "catalog.sqlite3",
        "acquisition-lifecycle.sqlite3",
        "acquisition-evidence.sqlite3",
        "acquisition-artifacts",
    } <= names


def test_every_store_can_be_read_from_another_thread(tmp_path: Path) -> None:
    """The console serves its backend on a threadpool, so a thread-affine store is a 500.

    A SQLite connection opened with `check_same_thread` left on raises
    `sqlite3.ProgrammingError` for any thread but the one that opened it. Every test that
    reads these stores on the main thread passes either way, which is why this one does not.
    """
    stores = DemoStores(tmp_path / "state", cursor_cipher_factory=_cursor_cipher)
    product_ref = ArtifactReference(artifact_id="orders_daily", version=1, digest="a" * 64)

    def read_absent_checkpoint() -> object:
        """A checkpoint store with nothing in it answers by refusing, so that is the read.

        The refusal is specific: the repository translates `sqlite3.ProgrammingError` --
        which a thread-affine connection raises, and which is a `sqlite3.Error` -- into
        `AcquisitionStatePersistenceError`. Catching only the not-found case therefore still
        fails if this connection carries the affinity of the thread that opened it.
        """
        assert stores.acquisition_state is not None
        with pytest.raises(AcquisitionStateNotFoundError):
            stores.acquisition_state.load_checkpoint("tenant-demo", "c" * 64, "source-binding:demo")
        return None

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
            stores.acquisition_lifecycle.list_activated("tenant-demo"),
            read_absent_checkpoint(),
            stores.acquisition_evidence.list_acquisition_receipts("tenant-demo"),
            stores.acquisition_artifacts.exists_verified(
                tenant_id="tenant-demo", artifact_digest="a" * 64
            ),
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(read).result() == (
                (1,),
                (),
                None,
                None,
                None,
                None,
                None,
                (),
                None,
                (),
                False,
            )
    finally:
        stores.close()


def test_the_acquisition_state_store_is_absent_without_a_cursor_cipher(tmp_path: Path) -> None:
    """Cursors are stored encrypted, so no cipher means no store rather than a store.

    Opening it under a cipher this class invented would be the one outcome worse than not
    opening it: cursors written under a stand-in cannot be read back by the real one.
    """
    stores = DemoStores(tmp_path / "state")
    try:
        assert stores.acquisition_state is None
        assert not (tmp_path / "state/acquisition-state.sqlite3").exists()
    finally:
        stores.close()


def test_the_cursor_cipher_is_composed_over_an_existing_state_directory(tmp_path: Path) -> None:
    """The cipher's key material belongs beside the stores, so the directory must be there.

    A factory called before the directory was made could only create it a second time, with
    whichever permissions it chose rather than the ones this class established.
    """
    seen: list[Path] = []

    def recording_factory(state_dir: Path) -> CursorCipher:
        seen.append(state_dir)
        assert state_dir.is_dir()
        return _CursorCipher()

    stores = DemoStores(tmp_path / "state", cursor_cipher_factory=recording_factory)
    try:
        assert seen == [tmp_path / "state"]
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


def test_a_late_failure_closes_the_acquisition_stores_already_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acquisition handles join the clean-up list, so a later failure closes them too.

    The artifact store is the last store opened and the only one holding no connection,
    which makes it the one place a failure can arrive with every acquisition handle open.
    The recorded order is the reverse of the opening order, so each borrowed connection is
    closed after whatever borrowed it.
    """
    closed: list[str] = []

    class _RecordingLifecycleRepository(SQLiteAcquisitionContractLifecycleRepository):
        def close(self) -> None:
            closed.append("lifecycle")
            super().close()

    class _RecordingEvidenceStore(SQLiteStore):
        def close(self) -> None:
            closed.append("evidence")
            super().close()

    class _RefusingArtifactStore:
        def __init__(self, root: Path) -> None:
            raise RuntimeError("the artifact store refused to open")

    monkeypatch.setattr(
        demo_stores, "SQLiteAcquisitionContractLifecycleRepository", _RecordingLifecycleRepository
    )
    monkeypatch.setattr(demo_stores, "SQLiteStore", _RecordingEvidenceStore)
    monkeypatch.setattr(demo_stores, "LocalAcquisitionArtifactStore", _RefusingArtifactStore)

    with pytest.raises(RuntimeError, match="refused to open"):
        DemoStores(tmp_path / "state", cursor_cipher_factory=_cursor_cipher)

    assert closed == ["evidence", "lifecycle"]


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
