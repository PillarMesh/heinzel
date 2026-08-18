import sqlite3
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import Literal, Protocol

from pillarmesh_contract_model import canonical_bytes, digest

from .process_models import BusinessProcessManifest, ProcessPackageReceipt

_MARKDOWN_MEDIA_TYPE: Literal["text/markdown; charset=utf-8"] = "text/markdown; charset=utf-8"


def _validated_media_type(media_type: str) -> Literal["text/markdown; charset=utf-8"]:
    if media_type != _MARKDOWN_MEDIA_TYPE:
        raise ValueError(f"unsupported media type: {media_type}")
    return _MARKDOWN_MEDIA_TYPE


class ProcessPackageRepository(Protocol):
    def store(
        self,
        *,
        tenant_id: str,
        package_id: str,
        media_type: Literal["text/markdown; charset=utf-8"],
        original: bytes,
        original_digest: str,
        manifest: bytes,
        manifest_digest: str,
        uploader_id: str,
        received_at: datetime,
    ) -> ProcessPackageReceipt: ...

    def load_original(self, tenant_id: str, package_id: str, version: int) -> bytes | None: ...


class SQLiteProcessPackageRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        # SQLite ignores every declared foreign key unless this is set per connection,
        # which would leave the package -> artifact references below decorative.
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS process_package_versions ("
            "tenant_id TEXT NOT NULL, "
            "package_id TEXT NOT NULL, "
            "next_version INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, package_id)"
            ")"
        )
        self._connection.execute(
            # Artifact bytes are keyed by tenant as well as digest. Deduplicating on
            # digest alone makes two tenants who upload identical narratives share one
            # row, so later cleanup of one tenant's package destroys the other's.
            "CREATE TABLE IF NOT EXISTS process_package_originals ("
            "tenant_id TEXT NOT NULL, "
            "original_digest TEXT NOT NULL, "
            "original BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, original_digest)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS process_package_manifests ("
            "tenant_id TEXT NOT NULL, "
            "manifest_digest TEXT NOT NULL, "
            "manifest BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, manifest_digest)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS process_packages ("
            "package_id TEXT NOT NULL, "
            "version INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "media_type TEXT NOT NULL, "
            "original_digest TEXT NOT NULL, "
            "manifest_digest TEXT NOT NULL, "
            "uploader_id TEXT NOT NULL, "
            "received_at TEXT NOT NULL, "
            "PRIMARY KEY (package_id, version), "
            "FOREIGN KEY (tenant_id, original_digest) "
            "REFERENCES process_package_originals(tenant_id, original_digest), "
            "FOREIGN KEY (tenant_id, manifest_digest) "
            "REFERENCES process_package_manifests(tenant_id, manifest_digest)"
            ")"
        )
        self._connection.commit()

    def store(
        self,
        *,
        tenant_id: str,
        package_id: str,
        media_type: Literal["text/markdown; charset=utf-8"],
        original: bytes,
        original_digest: str,
        manifest: bytes,
        manifest_digest: str,
        uploader_id: str,
        received_at: datetime,
    ) -> ProcessPackageReceipt:
        """Allocate the next version and persist the package as one unit.

        Allocating in a prior committed statement burned a version whenever the write
        that followed failed, leaving a hole no consumer can tell from a deletion.
        """
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                receipt = ProcessPackageReceipt(
                    package_id=package_id,
                    tenant_id=tenant_id,
                    version=self._allocate_version(tenant_id, package_id),
                    media_type=media_type,
                    original_digest=original_digest,
                    manifest_digest=manifest_digest,
                    uploader_id=uploader_id,
                    received_at=received_at,
                )
                self._save_artifact(
                    "process_package_originals",
                    "original_digest",
                    "original",
                    tenant_id,
                    receipt.original_digest,
                    original,
                )
                self._save_artifact(
                    "process_package_manifests",
                    "manifest_digest",
                    "manifest",
                    tenant_id,
                    receipt.manifest_digest,
                    manifest,
                )
                self._connection.execute(
                    "INSERT INTO process_packages ("
                    "package_id, version, tenant_id, media_type, original_digest, manifest_digest, "
                    "uploader_id, received_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.package_id,
                        receipt.version,
                        receipt.tenant_id,
                        receipt.media_type,
                        receipt.original_digest,
                        receipt.manifest_digest,
                        receipt.uploader_id,
                        receipt.received_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                self._connection.rollback()
                raise ValueError("process package version is immutable") from error
            except BaseException:
                # Any other failure must not leave the implicit transaction open: the
                # next successful call's commit would publish this partial write.
                self._connection.rollback()
                raise
            self._connection.commit()
            return receipt

    def _allocate_version(self, tenant_id: str, package_id: str) -> int:
        row = self._connection.execute(
            "INSERT INTO process_package_versions (tenant_id, package_id, next_version) "
            "VALUES (?, ?, 2) "
            "ON CONFLICT(tenant_id, package_id) DO UPDATE "
            "SET next_version = process_package_versions.next_version + 1 "
            "RETURNING next_version - 1",
            (tenant_id, package_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("process package version allocation did not return a version")
        return int(row[0])

    def load_original(self, tenant_id: str, package_id: str, version: int) -> bytes | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT original.original "
                "FROM process_packages AS package "
                "INNER JOIN process_package_originals AS original "
                "ON original.tenant_id = package.tenant_id "
                "AND original.original_digest = package.original_digest "
                "WHERE package.tenant_id = ? AND package.package_id = ? AND package.version = ?",
                (tenant_id, package_id, version),
            ).fetchone()
            if row is not None:
                return bytes(row[0])
            owner = self._connection.execute(
                "SELECT tenant_id FROM process_packages WHERE package_id = ? AND version = ?",
                (package_id, version),
            ).fetchone()
        if owner is None:
            return None
        if owner[0] != tenant_id:
            raise KeyError(f"package {package_id} belongs to another tenant")
        raise RuntimeError("process package receipt points to a missing original")

    def _save_artifact(
        self,
        table: str,
        digest_column: str,
        payload_column: str,
        tenant_id: str,
        artifact_digest: str,
        payload: bytes,
    ) -> None:
        self._connection.execute(
            f"INSERT INTO {table} (tenant_id, {digest_column}, {payload_column}) VALUES (?, ?, ?) "
            f"ON CONFLICT(tenant_id, {digest_column}) DO NOTHING",
            (tenant_id, artifact_digest, payload),
        )
        row = self._connection.execute(
            f"SELECT {payload_column} FROM {table} WHERE tenant_id = ? AND {digest_column} = ?",
            (tenant_id, artifact_digest),
        ).fetchone()
        if row is None or bytes(row[0]) != payload:
            raise ValueError("artifact digest points to different bytes")


class ProcessPackageService:
    def __init__(
        self, repository: ProcessPackageRepository, *, clock: Callable[[], datetime]
    ) -> None:
        self._repository = repository
        self._clock = clock

    def upload(
        self,
        tenant_id: str,
        original: bytes,
        media_type: str,
        manifest: BusinessProcessManifest,
        uploader_id: str,
    ) -> ProcessPackageReceipt:
        validated_media_type = _validated_media_type(media_type)
        try:
            original.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("original narrative must be valid UTF-8") from error

        package_id = (
            "bpp-"
            + digest(
                {
                    "domain": "pillarmesh-business-process-package-v1",
                    "tenant_id": tenant_id,
                    "process_name": manifest.process_name,
                }
            )[:24]
        )
        return self._repository.store(
            tenant_id=tenant_id,
            package_id=package_id,
            media_type=validated_media_type,
            original=original,
            original_digest=sha256(original).hexdigest(),
            manifest=canonical_bytes(manifest),
            manifest_digest=digest(manifest),
            uploader_id=uploader_id,
            received_at=self._clock(),
        )

    def get_original(self, tenant_id: str, package_id: str, version: int) -> bytes:
        original = self._repository.load_original(tenant_id, package_id, version)
        if original is None:
            raise KeyError((package_id, version))
        return original
