from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from heinzel_contract_model import canonical_bytes
from pydantic import ValidationError

from .incident_models import IncidentRecord, RecoveryActionEvidence


class IncidentNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__("incident is unavailable")


class IncidentConflictError(RuntimeError):
    pass


class StaleIncidentRevisionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("incident revision is stale")


class IncidentIntegrityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("stored incident is invalid")


class IncidentPersistenceError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"incident persistence failed during {operation}")


class SQLiteIncidentRepository:
    def __init__(self, database_path: str | Path, *, check_same_thread: bool = True) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(database_path), timeout=5, check_same_thread=check_same_thread
            )
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                "CREATE TABLE IF NOT EXISTS incident_revisions ("
                "tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, incident_id, revision));"
                "CREATE TABLE IF NOT EXISTS incident_current ("
                "tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, "
                "current_revision INTEGER NOT NULL, "
                "PRIMARY KEY (tenant_id, incident_id), "
                "FOREIGN KEY (tenant_id, incident_id, current_revision) "
                "REFERENCES incident_revisions(tenant_id, incident_id, revision));"
                "CREATE TABLE IF NOT EXISTS incident_recovery_evidence ("
                "tenant_id TEXT NOT NULL, command_id TEXT NOT NULL, incident_id TEXT NOT NULL, "
                "incident_revision INTEGER NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, command_id), "
                "UNIQUE (tenant_id, incident_id, incident_revision), "
                "FOREIGN KEY (tenant_id, incident_id, incident_revision) "
                "REFERENCES incident_revisions(tenant_id, incident_id, revision));"
            )
        except sqlite3.Error:
            if connection is not None:
                with suppress(BaseException):
                    connection.close()
            raise IncidentPersistenceError(operation="initialize incident repository") from None

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="close incident repository") from None

    def append(
        self,
        incident: IncidentRecord,
        *,
        expected_current_revision: int,
    ) -> IncidentRecord:
        if expected_current_revision < 0 or incident.revision != expected_current_revision + 1:
            raise StaleIncidentRevisionError
        payload = canonical_bytes(incident)
        try:
            with _transaction(self._connection):
                replay_row = self._connection.execute(
                    "SELECT tenant_id, incident_id, revision, payload FROM incident_revisions "
                    "WHERE tenant_id = ? AND incident_id = ? AND revision = ?",
                    (incident.tenant_id, incident.incident_id, incident.revision),
                ).fetchone()
                if replay_row is not None:
                    replay = _decode_incident(replay_row)
                    if bytes(replay_row[3]) != payload:
                        raise IncidentConflictError(
                            "incident replay conflicts with stored revision"
                        )
                    return replay

                current_row = self._connection.execute(
                    "SELECT current_revision FROM incident_current "
                    "WHERE tenant_id = ? AND incident_id = ?",
                    (incident.tenant_id, incident.incident_id),
                ).fetchone()
                if current_row is None:
                    if expected_current_revision != 0:
                        raise StaleIncidentRevisionError
                    self._connection.execute(
                        "INSERT INTO incident_revisions "
                        "(tenant_id, incident_id, revision, payload) VALUES (?, ?, ?, ?)",
                        (
                            incident.tenant_id,
                            incident.incident_id,
                            incident.revision,
                            payload,
                        ),
                    )
                    self._connection.execute(
                        "INSERT INTO incident_current "
                        "(tenant_id, incident_id, current_revision) VALUES (?, ?, ?)",
                        (incident.tenant_id, incident.incident_id, incident.revision),
                    )
                    return incident

                if int(current_row[0]) != expected_current_revision:
                    raise StaleIncidentRevisionError
                self._connection.execute(
                    "INSERT INTO incident_revisions "
                    "(tenant_id, incident_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (
                        incident.tenant_id,
                        incident.incident_id,
                        incident.revision,
                        payload,
                    ),
                )
                updated = self._connection.execute(
                    "UPDATE incident_current SET current_revision = ? "
                    "WHERE tenant_id = ? AND incident_id = ? AND current_revision = ?",
                    (
                        incident.revision,
                        incident.tenant_id,
                        incident.incident_id,
                        expected_current_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleIncidentRevisionError
                return incident
        except (IncidentConflictError, IncidentIntegrityError, StaleIncidentRevisionError):
            raise
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="append incident revision") from None

    def load_current(self, tenant_id: str, incident_id: str) -> IncidentRecord:
        try:
            row = self._connection.execute(
                "SELECT current.tenant_id, current.incident_id, current.current_revision, "
                "revisions.payload FROM incident_current AS current "
                "LEFT JOIN incident_revisions AS revisions "
                "ON revisions.tenant_id = current.tenant_id "
                "AND revisions.incident_id = current.incident_id "
                "AND revisions.revision = current.current_revision "
                "WHERE current.tenant_id = ? AND current.incident_id = ?",
                (tenant_id, incident_id),
            ).fetchone()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="load current incident") from None
        if row is None:
            raise IncidentNotFoundError
        return _decode_incident(row)

    def load_revision(self, tenant_id: str, incident_id: str, revision: int) -> IncidentRecord:
        try:
            row = self._connection.execute(
                "SELECT tenant_id, incident_id, revision, payload FROM incident_revisions "
                "WHERE tenant_id = ? AND incident_id = ? AND revision = ?",
                (tenant_id, incident_id, revision),
            ).fetchone()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="load incident revision") from None
        if row is None:
            raise IncidentNotFoundError
        return _decode_incident(row)

    def list_current(self, tenant_id: str) -> tuple[IncidentRecord, ...]:
        try:
            rows = self._connection.execute(
                "SELECT current.tenant_id, current.incident_id, current.current_revision, "
                "revisions.payload FROM incident_current AS current "
                "LEFT JOIN incident_revisions AS revisions "
                "ON revisions.tenant_id = current.tenant_id "
                "AND revisions.incident_id = current.incident_id "
                "AND revisions.revision = current.current_revision "
                "WHERE current.tenant_id = ? "
                "ORDER BY current.incident_id COLLATE BINARY",
                (tenant_id,),
            ).fetchall()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="list current incidents") from None
        return tuple(_decode_incident(row) for row in rows)

    def append_recovery_evidence(self, evidence: RecoveryActionEvidence) -> RecoveryActionEvidence:
        payload = canonical_bytes(evidence)
        try:
            with _transaction(self._connection):
                replay_row = self._connection.execute(
                    "SELECT tenant_id, command_id, incident_id, incident_revision, payload "
                    "FROM incident_recovery_evidence "
                    "WHERE tenant_id = ? AND command_id = ?",
                    (evidence.tenant_id, evidence.command_id),
                ).fetchone()
                if replay_row is not None:
                    replay = _decode_recovery_evidence(replay_row)
                    if bytes(replay_row[4]) != payload:
                        raise IncidentConflictError(
                            "recovery command conflicts with recorded evidence"
                        )
                    return replay
                existing = self._connection.execute(
                    "SELECT command_id FROM incident_recovery_evidence "
                    "WHERE tenant_id = ? AND incident_id = ? AND incident_revision = ?",
                    (
                        evidence.tenant_id,
                        evidence.incident_id,
                        evidence.incident_revision,
                    ),
                ).fetchone()
                if existing is not None:
                    raise IncidentConflictError("incident revision already has recovery evidence")
                self._connection.execute(
                    "INSERT INTO incident_recovery_evidence "
                    "(tenant_id, command_id, incident_id, incident_revision, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        evidence.tenant_id,
                        evidence.command_id,
                        evidence.incident_id,
                        evidence.incident_revision,
                        payload,
                    ),
                )
        except (IncidentConflictError, IncidentIntegrityError):
            raise
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="append recovery evidence") from None
        return evidence

    def list_recovery_evidence(
        self, tenant_id: str, incident_id: str
    ) -> tuple[RecoveryActionEvidence, ...]:
        try:
            rows = self._connection.execute(
                "SELECT tenant_id, command_id, incident_id, incident_revision, payload "
                "FROM incident_recovery_evidence "
                "WHERE tenant_id = ? AND incident_id = ? ORDER BY incident_revision",
                (tenant_id, incident_id),
            ).fetchall()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="list recovery evidence") from None
        return tuple(_decode_recovery_evidence(row) for row in rows)

    def load_recovery_evidence(
        self, tenant_id: str, command_id: str
    ) -> RecoveryActionEvidence | None:
        try:
            row = self._connection.execute(
                "SELECT tenant_id, command_id, incident_id, incident_revision, payload "
                "FROM incident_recovery_evidence WHERE tenant_id = ? AND command_id = ?",
                (tenant_id, command_id),
            ).fetchone()
        except sqlite3.Error:
            raise IncidentPersistenceError(operation="load recovery evidence") from None
        if row is None:
            return None
        return _decode_recovery_evidence(row)


def _decode_incident(row: tuple[object, ...]) -> IncidentRecord:
    try:
        tenant_id = str(row[0])
        incident_id = str(row[1])
        revision = int(str(row[2]))
        raw_payload = row[3]
        if not isinstance(raw_payload, bytes | bytearray | memoryview):
            raise TypeError
        payload = bytes(raw_payload)
        incident = IncidentRecord.model_validate_json(payload)
    except (TypeError, ValueError, ValidationError):
        raise IncidentIntegrityError from None
    if (
        incident.tenant_id != tenant_id
        or incident.incident_id != incident_id
        or incident.revision != revision
        or canonical_bytes(incident) != payload
    ):
        raise IncidentIntegrityError
    return incident


def _decode_recovery_evidence(row: tuple[object, ...]) -> RecoveryActionEvidence:
    try:
        tenant_id = str(row[0])
        command_id = str(row[1])
        incident_id = str(row[2])
        incident_revision = int(str(row[3]))
        raw_payload = row[4]
        if not isinstance(raw_payload, bytes | bytearray | memoryview):
            raise TypeError
        payload = bytes(raw_payload)
        evidence = RecoveryActionEvidence.model_validate_json(payload)
    except (TypeError, ValueError, ValidationError):
        raise IncidentIntegrityError from None
    if (
        evidence.tenant_id != tenant_id
        or evidence.command_id != command_id
        or evidence.incident_id != incident_id
        or evidence.incident_revision != incident_revision
        or canonical_bytes(evidence) != payload
    ):
        raise IncidentIntegrityError
    return evidence


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise
    else:
        connection.commit()
