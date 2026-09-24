from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from heinzel_contract_model import ApprovedSemanticVersion, canonical_bytes, digest


class SQLiteSemanticVersionRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
        # SQLite ignores every declared foreign key unless this is set per connection,
        # which would leave the approval-binding reference below decorative.
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_version_sequences ("
            "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_versions ("
            "tenant_id TEXT NOT NULL, semantic_version_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "material_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, semantic_version_id, version), "
            "UNIQUE (tenant_id, material_digest), UNIQUE (tenant_id, version))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_version_approval_bindings ("
            "tenant_id TEXT NOT NULL, semantic_version_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "approval_id TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, semantic_version_id, version, approval_id), "
            "FOREIGN KEY (tenant_id, semantic_version_id, version) REFERENCES semantic_versions "
            "(tenant_id, semantic_version_id, version))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, semantic_version: ApprovedSemanticVersion) -> ApprovedSemanticVersion:
        material_digest = _material_digest(semantic_version)
        with _transaction(self._connection):
            replay = self._connection.execute(
                "SELECT payload FROM semantic_versions WHERE tenant_id = ? AND material_digest = ?",
                (semantic_version.tenant_id, material_digest),
            ).fetchone()
            if replay is not None:
                return ApprovedSemanticVersion.model_validate_json(replay[0])
            sequence = self._allocate(semantic_version.tenant_id)
            stored = semantic_version.model_copy(
                update={
                    "semantic_version_id": _semantic_version_id(
                        semantic_version.tenant_id, sequence
                    ),
                    "version": sequence,
                }
            )
            self._connection.execute(
                "INSERT INTO semantic_versions "
                "(tenant_id, semantic_version_id, version, material_digest, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    stored.tenant_id,
                    stored.semantic_version_id,
                    stored.version,
                    material_digest,
                    canonical_bytes(stored),
                ),
            )
            self._connection.executemany(
                "INSERT INTO semantic_version_approval_bindings "
                "(tenant_id, semantic_version_id, version, approval_id) VALUES (?, ?, ?, ?)",
                tuple(
                    (stored.tenant_id, stored.semantic_version_id, stored.version, approval_id)
                    for approval_id in sorted(stored.approval_ids)
                ),
            )
        return stored

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion:
        row = self._connection.execute(
            "SELECT payload FROM semantic_versions "
            "WHERE tenant_id = ? AND semantic_version_id = ? AND version = ?",
            (tenant_id, semantic_version_id, version),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, semantic_version_id, version))
        return ApprovedSemanticVersion.model_validate_json(row[0])

    def load_approval_ids(self, semantic_version: ApprovedSemanticVersion) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT approval_id FROM semantic_version_approval_bindings "
            "WHERE tenant_id = ? AND semantic_version_id = ? AND version = ? ORDER BY approval_id",
            (
                semantic_version.tenant_id,
                semantic_version.semantic_version_id,
                semantic_version.version,
            ),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _allocate(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "INSERT INTO semantic_version_sequences (tenant_id, next_sequence) VALUES (?, 2) "
            "ON CONFLICT(tenant_id) DO UPDATE SET next_sequence = "
            "semantic_version_sequences.next_sequence + 1 "
            "RETURNING next_sequence - 1",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("semantic version allocation did not return a version")
        return int(row[0])


def _material_digest(semantic_version: ApprovedSemanticVersion) -> str:
    value = semantic_version.model_dump(mode="python")
    value.pop("semantic_version_id")
    value.pop("version")
    return digest({"domain": "heinzel-approved-semantic-version-material-v1", "value": value})


def _semantic_version_id(tenant_id: str, sequence: int) -> str:
    return (
        "semantic-"
        + digest(
            {
                "domain": "heinzel-approved-semantic-version-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
