"""The SQLite stores the demonstration console owns, opened under one state directory."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from heinzel_catalog_control import SQLiteCatalogRepository
from heinzel_contract_service import (
    SQLiteAcquisitionContractLifecycleRepository,
    SQLiteSourceFreshnessObservationRepository,
)
from heinzel_evidence import SQLiteStore
from heinzel_request_management import SQLiteRequestRepository
from heinzel_runtime import (
    GenerationLedger,
    SQLiteProductMaterializationReceiptReader,
    opaque_reference_factory,
)
from heinzel_semantic_registry import (
    SQLiteApprovedProductVersionRepository,
    SQLiteCatalogPublicationRepository,
    SQLiteProductCatalogPublicationRepository,
    SQLiteProductQueryBindingRepository,
)
from heinzel_semantic_registry.repository import SQLiteSemanticRepository
from heinzel_state import (
    CursorCipher,
    LocalAcquisitionArtifactStore,
    SQLiteAcquisitionStateRepository,
)

from .model_authority import DemoSignedModelStore

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

    def __init__(
        self,
        state_dir: Path,
        *,
        cursor_cipher_factory: Callable[[Path], CursorCipher] | None = None,
    ) -> None:
        """Open every store under `state_dir`, which is created when it is absent.

        The acquisition state store encrypts the source cursors it holds, so it is the one
        store that needs a cipher, and the cipher is this demonstration's to compose rather
        than this class's to invent. It arrives as a factory of the state directory, because
        the key material a cipher keeps belongs beside the stores it protects and the
        directory exists only once this constructor has made it.

        Given no factory, `acquisition_state` stays `None` and the acquisition path reports
        itself as not delivered -- the same posture the console takes without a warehouse,
        and an honest one: a store whose cursors could not be encrypted is not that store.
        """
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
            # The product authority the governed answer reads. These are distinct from
            # `publications` above, which records the catalog publication intent and its
            # receipt: these three carry what the materialization established about the
            # product itself -- which generation is current, what its columns mean, and
            # which query over it is approved.
            self.product_publications = SQLiteProductCatalogPublicationRepository(
                str(state_dir / "product-publications.sqlite3"), check_same_thread=False
            )
            opened.append(self.product_publications)
            self.product_versions = SQLiteApprovedProductVersionRepository(
                str(state_dir / "approved-products.sqlite3"), check_same_thread=False
            )
            opened.append(self.product_versions)
            self.query_bindings = SQLiteProductQueryBindingRepository(
                str(state_dir / "approved-query-bindings.sqlite3"), check_same_thread=False
            )
            opened.append(self.query_bindings)
            # How current the landed source was, per generation. The governed answer refuses a
            # generation it can find no observation for, so this is part of the product
            # authority rather than a separate record of it.
            self.source_freshness = SQLiteSourceFreshnessObservationRepository(
                str(state_dir / "source-freshness.sqlite3"), check_same_thread=False
            )
            opened.append(self.source_freshness)
            # What was landed, and under which generation. The product's input cardinality is
            # resolved against this, so it is durable rather than per-run: a ledger that lived
            # only as long as the process could not answer for a generation it had committed.
            ledger_connection = self._connect(state_dir / "generations.sqlite3")
            opened.append(ledger_connection)
            self.generations = GenerationLedger(ledger_connection)
            # What the materialization committed, and so what the governed answer may answer
            # from. Durable rather than in memory: the receipt outlives the run that wrote it,
            # and a restart answers for a generation whose runner has long since ended.
            self.materialization_ledger = self._connect(state_dir / "materializations.sqlite3")
            opened.append(self.materialization_ledger)
            self.materialization_receipts = SQLiteProductMaterializationReceiptReader(
                self.materialization_ledger
            )
            # The compiler's signed model for the committed generation, and the public half of
            # the key that verifies it. The answer path re-verifies the model before answering,
            # and without this it would restart into enforcing nothing about output magnitudes.
            self.signed_models = DemoSignedModelStore(
                str(state_dir / "signed-models.sqlite3"), check_same_thread=False
            )
            opened.append(self.signed_models)
            # Which acquisition contracts the tenant activated, and where each one stands.
            # This repository opens its own connection and already leaves the thread check
            # off, so there is no connection of ours for it to borrow.
            self.acquisition_lifecycle = SQLiteAcquisitionContractLifecycleRepository(
                str(state_dir / "acquisition-lifecycle.sqlite3")
            )
            opened.append(self.acquisition_lifecycle)
            # How far each source has been read, as an encrypted cursor beside the authority
            # that admitted it. The connection is supplied rather than left to the
            # repository's default `sqlite3.connect`, which keeps the thread check on and
            # would make every read from the threadpool a `ProgrammingError`. References are
            # allocated by the product's own factory rather than a counter, because the
            # receipt model refuses a reference shaped like a digest.
            self.acquisition_state: SQLiteAcquisitionStateRepository | None = None
            if cursor_cipher_factory is not None:
                self.acquisition_state = SQLiteAcquisitionStateRepository(
                    str(state_dir / "acquisition-state.sqlite3"),
                    cipher=cursor_cipher_factory(state_dir),
                    reference_factory=opaque_reference_factory(),
                    connection_factory=lambda database_path: sqlite3.connect(
                        database_path, check_same_thread=False
                    ),
                )
                opened.append(self.acquisition_state)
            # The receipts the acquisition run appends, which are what it can later be held
            # to. `check_same_thread=False` is opt-in there for the reason it is the default
            # here: this store is read from the thread that serves a route, not the one that
            # opened it.
            self.acquisition_evidence = SQLiteStore.open(
                state_dir / "acquisition-evidence.sqlite3", check_same_thread=False
            )
            opened.append(self.acquisition_evidence)
            # The landed payloads the receipts attest to, as files under a private root of
            # their own rather than rows. This store holds no connection -- each operation
            # opens and closes its own descriptors -- so it has nothing to close and takes no
            # place in the clean-up order.
            self.acquisition_artifacts = LocalAcquisitionArtifactStore(
                state_dir / "acquisition-artifacts"
            )
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
