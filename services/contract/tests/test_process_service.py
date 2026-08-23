import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread

import pytest
from pillarmesh_contract_model import canonical_bytes
from pillarmesh_contract_service import (
    BusinessProcessManifest,
    ProcessPackageReceipt,
    ProcessPackageService,
)
from pillarmesh_contract_service.process_service import SQLiteProcessPackageRepository
from pydantic import ValidationError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
MARKDOWN = "text/markdown; charset=utf-8"


def service() -> ProcessPackageService:
    return ProcessPackageService(SQLiteProcessPackageRepository(":memory:"), clock=lambda: NOW)


def manifest() -> BusinessProcessManifest:
    return BusinessProcessManifest(
        process_name="revenue-to-cash",
        owner="finance-data-owner",
        participants=("customer", "finance"),
        outcomes=("recognized-revenue",),
        entities=("Customer", "Invoice", "Payment", "Refund"),
        events=("invoice-issued", "payment-settled", "refund-issued"),
        states=("invoice-open", "invoice-paid", "invoice-refunded"),
        rules=("refund does not exceed settled payment",),
        source_references=("postgresql.billing", "stripe"),
        unresolved_questions=("refund exception owner",),
    )


def receipt(**updates: object) -> ProcessPackageReceipt:
    values: dict[str, object] = {
        "package_id": "bpp-test",
        "tenant_id": "tenant-a",
        "version": 1,
        "media_type": MARKDOWN,
        "original_digest": "a" * 64,
        "manifest_digest": "b" * 64,
        "manifest_source_digest": "c" * 64,
        "uploader_id": "architect-a",
        "received_at": NOW,
    }
    values.update(updates)
    return ProcessPackageReceipt.model_validate(values)


def test_upload_preserves_original_and_creates_new_version() -> None:
    packages = service()
    first = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "architect-a")
    second = packages.upload(
        "tenant-a", b"# Revenue to cash v2\n", MARKDOWN, manifest(), "architect-a"
    )

    assert first.version == 1
    assert second.version == 2
    assert first.original_digest != second.original_digest
    assert first.media_type == MARKDOWN
    assert packages.get_original("tenant-a", first.package_id, 1) == b"# Revenue to cash\n"


def test_upload_rejects_non_utf8_narrative() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        service().upload("tenant-a", b"\xff", MARKDOWN, manifest(), "architect-a")


def test_upload_rejects_an_unsupported_media_type() -> None:
    with pytest.raises(ValueError, match="media type"):
        service().upload("tenant-a", b"# ok\n", "application/pdf", manifest(), "architect-a")


def test_upload_manifest_bytes_preserves_and_digests_the_exact_input() -> None:
    packages = service()
    manifest_bytes = canonical_bytes(manifest()) + b"\n"

    uploaded = packages.upload_manifest_bytes(
        "tenant-a",
        b"# Revenue to cash\n",
        MARKDOWN,
        manifest_bytes,
        "architect-a",
    )

    assert (
        packages.get_manifest("tenant-a", uploaded.package_id, uploaded.version) == manifest_bytes
    )
    assert uploaded.manifest_source_digest != uploaded.manifest_digest


def test_upload_manifest_bytes_rejects_unknown_fields() -> None:
    manifest_bytes = canonical_bytes(manifest()).replace(b"}", b',"unknown":"value"}', 1)

    with pytest.raises(ValueError, match="manifest JSON"):
        service().upload_manifest_bytes(
            "tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest_bytes, "architect-a"
        )


def test_upload_manifest_bytes_rejects_invalid_utf8() -> None:
    with pytest.raises(ValueError, match="manifest JSON"):
        service().upload_manifest_bytes(
            "tenant-a", b"# Revenue to cash\n", MARKDOWN, b"\xff", "architect-a"
        )


def test_one_tenant_cannot_read_another_tenants_original() -> None:
    packages = service()
    receipt = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")

    with pytest.raises(KeyError, match="belongs to another tenant"):
        packages.get_original("tenant-b", receipt.package_id, receipt.version)


def test_foreign_tenant_lookup_does_not_select_original_payload() -> None:
    repository = SQLiteProcessPackageRepository(":memory:")
    packages = ProcessPackageService(repository, clock=lambda: NOW)
    uploaded = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")

    class PayloadQueryRecorder:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.payload_queries: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if "original.original" in sql:
                self.payload_queries.append((sql, parameters))
                if "package.tenant_id = ?" not in sql or parameters[0] != "tenant-b":
                    raise AssertionError("foreign original selected without tenant predicate")
            return self._connection.execute(sql, parameters)

    recorder = PayloadQueryRecorder(repository._connection)
    repository._connection = recorder

    with pytest.raises(KeyError, match="belongs to another tenant"):
        packages.get_original("tenant-b", uploaded.package_id, uploaded.version)

    assert len(recorder.payload_queries) == 1


@pytest.mark.parametrize(
    "timestamp",
    (
        datetime(2026, 8, 17, 12),
        datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_receipt_rejects_non_utc_received_at(timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        receipt(received_at=timestamp)


@pytest.mark.parametrize(
    "clock_value",
    (
        datetime(2026, 8, 17, 12),
        datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_upload_rejects_non_utc_clock(clock_value: datetime) -> None:
    packages = ProcessPackageService(
        SQLiteProcessPackageRepository(":memory:"), clock=lambda: clock_value
    )

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")


def test_receipt_canonicalizes_zero_offset_timestamp_to_utc() -> None:
    zero_offset = timezone(timedelta(0), "zero-offset")

    canonical = receipt(received_at=datetime(2026, 8, 17, 12, tzinfo=zero_offset))

    assert canonical.received_at == NOW
    assert canonical.received_at.tzinfo is UTC


def test_concurrent_uploads_allocate_unique_versions(tmp_path: Path) -> None:
    database_path = tmp_path / "process-packages.db"
    SQLiteProcessPackageRepository(str(database_path))
    barrier = Barrier(2)
    outcomes: Queue[ProcessPackageReceipt | Exception] = Queue()

    def upload() -> None:
        packages = ProcessPackageService(
            SQLiteProcessPackageRepository(str(database_path)), clock=lambda: NOW
        )
        barrier.wait(timeout=5)
        try:
            outcomes.put(
                packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")
            )
        except Exception as error:
            outcomes.put(error)

    first = Thread(target=upload)
    second = Thread(target=upload)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    results = [outcomes.get_nowait() for _ in range(2)]
    receipts = [result for result in results if isinstance(result, ProcessPackageReceipt)]
    assert sorted(receipt.version for receipt in receipts) == [1, 2]


def test_identical_narratives_are_not_shared_between_tenants() -> None:
    repository = SQLiteProcessPackageRepository(":memory:")
    packages = ProcessPackageService(repository, clock=lambda: NOW)
    narrative = b"# Revenue to cash\n"

    packages.upload("tenant-a", narrative, MARKDOWN, manifest(), "arch-a")
    packages.upload("tenant-b", narrative, MARKDOWN, manifest(), "arch-b")

    # Deduplicating on digest alone would leave one row co-owned by both tenants, so
    # cleaning up either package would destroy the other tenant's narrative.
    stored = repository._connection.execute(
        "SELECT tenant_id FROM process_package_originals ORDER BY tenant_id"
    ).fetchall()
    assert [row[0] for row in stored] == ["tenant-a", "tenant-b"]


def test_package_row_cannot_reference_a_missing_artifact() -> None:
    repository = SQLiteProcessPackageRepository(":memory:")

    with pytest.raises(sqlite3.IntegrityError):
        repository._connection.execute(
            "INSERT INTO process_packages ("
            "package_id, version, tenant_id, media_type, original_digest, manifest_digest, "
            "manifest_source_digest, uploader_id, received_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "bpp-orphan",
                1,
                "tenant-a",
                MARKDOWN,
                "f" * 64,
                "e" * 64,
                "d" * 64,
                "arch-a",
                NOW.isoformat(),
            ),
        )


def test_failed_store_publishes_nothing_and_burns_no_version(tmp_path: Path) -> None:
    database_path = str(tmp_path / "process-packages.db")
    repository = SQLiteProcessPackageRepository(database_path)
    packages = ProcessPackageService(repository, clock=lambda: NOW)

    class FailsPackageInsert:
        """Fails the final insert, as a locked database or a full disk would."""

        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if "INSERT INTO process_packages " in sql:
                raise sqlite3.OperationalError("database is locked")
            return self._wrapped.execute(sql, parameters)

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    healthy = repository._connection
    repository._connection = FailsPackageInsert(healthy)  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError):
        packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")
    repository._connection = healthy  # type: ignore[assignment]

    assert healthy.in_transaction is False
    surviving = packages.upload("tenant-a", b"# Revenue to cash\n", MARKDOWN, manifest(), "arch-a")

    # The failed attempt must leave neither an orphan artifact nor a version hole.
    assert surviving.version == 1
    committed = sqlite3.connect(database_path)
    assert committed.execute("SELECT COUNT(*) FROM process_package_originals").fetchone()[0] == 1
    assert committed.execute("SELECT COUNT(*) FROM process_packages").fetchone()[0] == 1
