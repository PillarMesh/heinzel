from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Collection
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import cast

from pillarmesh_contract_model import JsonValue, canonical_bytes, canonical_value, digest

from .migrations import (
    MIGRATION_CHECKSUM,
    MIGRATION_SQL,
    MIGRATION_VERSION,
)
from .models import EvidenceEvent, RunRecord, RunState
from .private_state import RunPrivateState

TERMINAL_STATES = {"succeeded", "failed", "non_conforming"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class MigrationError(RuntimeError):
    pass


class ActiveRunError(RuntimeError):
    pass


class InvalidStateTransition(RuntimeError):
    pass


class SQLiteStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path, *, check_same_thread: bool = True) -> SQLiteStore:
        """Open the store, optionally for use from more than one thread.

        SQLite connections carry thread affinity and a server route runs its work in
        a threadpool, so a caller that composes on one thread and reads on another
        must say so. It stays opt-in rather than becoming the default, because most
        callers own their connection on one thread and should keep the check.

        This does not make concurrent writers safe, and it does not pretend to. The
        write paths hold their own `BEGIN IMMEDIATE`, and a second one arriving on
        the same connection raises `cannot start a transaction within a transaction`
        rather than interleaving silently -- a loud failure, not a corrupt one. That
        is the same posture every other repository in this estate takes with a
        thread-tolerant connection; none of them holds a lock either.
        """
        connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=check_same_thread
        )
        connection.execute("PRAGMA foreign_keys = ON")
        store = cls(connection)
        try:
            store._migrate()
        except Exception:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self._connection.close()

    def _migrate(self) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
        ).fetchone()
        if exists:
            row = self._connection.execute(
                "SELECT version, checksum FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise MigrationError("schema metadata is missing")
            version = int(row["version"])
            checksum = str(row["checksum"])
            if version > MIGRATION_VERSION:
                raise MigrationError("database schema is newer than this runtime")
            if version == 1:
                raise MigrationError(
                    "database schema version 1 is unsupported; required version 2; "
                    "start with a fresh state path"
                )
            if version != MIGRATION_VERSION or checksum != MIGRATION_CHECKSUM:
                raise MigrationError("database migration checksum mismatch")
            return
        # The schema and its version row must land together: a database carrying the
        # tables without the metadata row is rejected by every later open(). Opening the
        # transaction inside the script keeps it pending once executescript returns, so
        # the DDL and the metadata insert commit as one unit.
        self._connection.executescript(f"BEGIN IMMEDIATE;\n{MIGRATION_SQL}")
        try:
            self._connection.execute(
                "INSERT INTO schema_metadata(singleton, version, checksum) VALUES (1, ?, ?)",
                (MIGRATION_VERSION, MIGRATION_CHECKSUM),
            )
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def save_artifact(self, kind: str, artifact_digest: str, payload: bytes) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._save_artifact(kind, artifact_digest, payload)
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _save_artifact(self, kind: str, artifact_digest: str, payload: bytes) -> None:
        existing = self._connection.execute(
            "SELECT payload FROM artifacts WHERE kind = ? AND digest = ?", (kind, artifact_digest)
        ).fetchone()
        if existing is not None and bytes(existing["payload"]) != payload:
            raise ValueError("artifact digest already exists with different payload")
        self._connection.execute(
            "INSERT OR IGNORE INTO artifacts(kind, digest, payload) VALUES (?, ?, ?)",
            (kind, artifact_digest, payload),
        )

    def publish_verification(
        self,
        artifacts: tuple[tuple[str, str, bytes], ...],
        summary_digest: str,
        summary_payload: bytes,
    ) -> None:
        if any(kind == "activation_summary" for kind, _digest, _payload in artifacts):
            raise ValueError("activation summary must be published last")
        expected_artifacts = (
            *artifacts,
            ("activation_summary", summary_digest, summary_payload),
        )
        for _kind, artifact_digest, payload in expected_artifacts:
            if hashlib.sha256(payload).hexdigest() != artifact_digest:
                raise ValueError("artifact digest does not match payload")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for kind, artifact_digest, payload in artifacts:
                self._save_artifact(kind, artifact_digest, payload)
            self._save_artifact("activation_summary", summary_digest, summary_payload)
            self.bind_artifact_ref("activation_summaries", summary_digest, summary_digest)
            for kind, artifact_digest, payload in expected_artifacts:
                if self.load_artifact(kind, artifact_digest) != payload:
                    raise RuntimeError("verification publication resolution failed")
            if self.resolve_artifact_ref("activation_summaries", summary_digest) != summary_digest:
                raise RuntimeError("verification publication resolution failed")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def load_artifact(self, kind: str, artifact_digest: str) -> bytes | None:
        row = self._connection.execute(
            "SELECT payload FROM artifacts WHERE kind = ? AND digest = ?", (kind, artifact_digest)
        ).fetchone()
        return None if row is None else bytes(row["payload"])

    def list_run_artifacts(self, run_id: str) -> tuple[tuple[str, str, bytes], ...]:
        record = self.get_run(run_id)
        referenced = {record.contract_digest, record.summary_digest}
        referenced.update(_digest_strings(json.loads(record.signed_graph_json)))
        for event in self.trace(run_id):
            referenced.update(_digest_strings(event.attributes))
        rows = self._connection.execute(
            "SELECT kind, digest, payload FROM artifacts ORDER BY kind, digest"
        ).fetchall()
        selected: dict[tuple[str, str], bytes] = {}
        changed = True
        while changed:
            changed = False
            for row in rows:
                artifact_digest = str(row["digest"])
                key = (str(row["kind"]), artifact_digest)
                if artifact_digest not in referenced or key in selected:
                    continue
                payload = bytes(row["payload"])
                selected[key] = payload
                changed = True
                with suppress(UnicodeDecodeError, json.JSONDecodeError):
                    referenced.update(_digest_strings(json.loads(payload)))
        return tuple(
            (kind, artifact_digest, payload)
            for (kind, artifact_digest), payload in selected.items()
        )

    def bind_artifact_ref(self, namespace: str, ref_key: str, artifact_digest: str) -> None:
        row = self._connection.execute(
            "SELECT artifact_digest FROM artifact_refs WHERE namespace = ? AND ref_key = ?",
            (namespace, ref_key),
        ).fetchone()
        if row is not None and row["artifact_digest"] != artifact_digest:
            raise ValueError("artifact reference is immutable")
        self._connection.execute(
            "INSERT OR IGNORE INTO artifact_refs(namespace, ref_key, artifact_digest) "
            "VALUES (?, ?, ?)",
            (namespace, ref_key, artifact_digest),
        )

    def resolve_artifact_ref(self, namespace: str, ref_key: str) -> str | None:
        row = self._connection.execute(
            "SELECT artifact_digest FROM artifact_refs WHERE namespace = ? AND ref_key = ?",
            (namespace, ref_key),
        ).fetchone()
        return None if row is None else str(row["artifact_digest"])

    def list_artifact_refs(self, namespace: str, prefix: str = "") -> tuple[tuple[str, str], ...]:
        rows = self._connection.execute(
            "SELECT ref_key, artifact_digest FROM artifact_refs "
            "WHERE namespace = ? AND ref_key LIKE ? ESCAPE '\\' ORDER BY ref_key",
            (namespace, f"{_like_literal(prefix)}%"),
        ).fetchall()
        return tuple((str(row["ref_key"]), str(row["artifact_digest"])) for row in rows)

    def get_run_by_activation(self, activation_key: str) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE activation_key = ?", (activation_key,)
        ).fetchone()
        return None if row is None else self._run_from_row(row)

    def create_run(
        self,
        run_id: str,
        activation_key: str,
        contract_digest: str,
        summary_digest: str,
        signed_graph_json: str,
        now: datetime,
    ) -> RunRecord:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            record = self._create_run(
                run_id,
                activation_key,
                contract_digest,
                summary_digest,
                signed_graph_json,
                now,
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return record

    def create_activated_run(
        self,
        run_id: str,
        activation_key: str,
        contract_digest: str,
        summary_digest: str,
        signed_graph_json: str,
        now: datetime,
        *,
        acceptance_key: int,
        lifecycle_events: tuple[tuple[str, dict[str, object]], ...],
        producer: str,
    ) -> RunRecord:
        """Create a run together with its acceptance key and lifecycle evidence.

        Activation is only replayable if these land as one unit: a run created without
        its lifecycle events is returned verbatim by :meth:`get_run_by_activation` on the
        next attempt, so the missing events could never be backfilled and the run would
        execute to terminal success yet fail evidence-package export forever.
        """
        normalized = tuple(
            (event_type, _attributes(attributes)) for event_type, attributes in lifecycle_events
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            repeated = self.get_run_by_activation(activation_key)
            if repeated is not None:
                self._connection.execute("COMMIT")
                return repeated
            record = self._create_run(
                run_id,
                activation_key,
                contract_digest,
                summary_digest,
                signed_graph_json,
                now,
            )
            self._set_acceptance_key(run_id, acceptance_key)
            for event_type, attributes in normalized:
                self._append_event(run_id, event_type, now, producer, attributes)
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return record

    def _create_run(
        self,
        run_id: str,
        activation_key: str,
        contract_digest: str,
        summary_digest: str,
        signed_graph_json: str,
        now: datetime,
    ) -> RunRecord:
        repeated = self._connection.execute(
            "SELECT * FROM runs WHERE activation_key = ?", (activation_key,)
        ).fetchone()
        if repeated is not None:
            return self._run_from_row(repeated)
        active = self._connection.execute(
            "SELECT run_id FROM runs WHERE state IN ('created','running')"
        ).fetchone()
        if active is not None:
            raise ActiveRunError(f"run {active['run_id']} is active")
        timestamp = _timestamp(now)
        self._connection.execute(
            """INSERT INTO runs(
                run_id, activation_key, contract_digest, summary_digest, signed_graph_json,
                state, checkpoint, batch_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'created', 'created', NULL, ?, ?)""",
            (
                run_id,
                activation_key,
                contract_digest,
                summary_digest,
                signed_graph_json,
                timestamp,
                timestamp,
            ),
        )
        return self.get_run(run_id)

    def list_runs_for_contracts(self, contract_digests: Collection[str]) -> tuple[RunRecord, ...]:
        """Every run witnessed under any of these contract digests, newest first.

        This is the evidence half of deriving a run's tenant. The caller resolves
        which contracts a tenant has activated and passes those digests here; the
        store never learns what a tenant is, which is why `RunRecord` needs no
        tenant and the append-only chain needs no migration.

        An empty digest set answers with no runs, because a tenant that has
        activated nothing must see nothing. SQLite would reach that answer anyway
        -- it reads `IN ()` as always false -- but `IN ()` is a SQLite extension
        rather than standard SQL, so the guard states the intent here instead of
        resting on one engine's tolerance.
        """
        if not contract_digests:
            return ()
        digests = tuple(contract_digests)
        placeholders = ", ".join("?" for _ in digests)
        rows = self._connection.execute(
            f"SELECT * FROM runs WHERE contract_digest IN ({placeholders}) "
            # `datetime()` normalises each stored offset to UTC. Ordering the ISO
            # text directly compared "12:00+05:30" against "09:00+00:00" as strings
            # and reversed two runs whose real instants are the other way round.
            "ORDER BY datetime(created_at) DESC, run_id",
            digests,
        ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def _run_from_row(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            activation_key=row["activation_key"],
            contract_digest=row["contract_digest"],
            summary_digest=row["summary_digest"],
            signed_graph_json=row["signed_graph_json"],
            state=row["state"],
            checkpoint=row["checkpoint"],
            batch_id=row["batch_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_private_state(self, run_id: str) -> RunPrivateState:
        self.get_run(run_id)
        row = self._connection.execute(
            "SELECT acceptance_key, segment_path FROM run_private_state WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return RunPrivateState(run_id=run_id, acceptance_key=None, segment_path=None)
        return RunPrivateState(
            run_id=run_id,
            acceptance_key=None if row["acceptance_key"] is None else int(row["acceptance_key"]),
            segment_path=None if row["segment_path"] is None else Path(str(row["segment_path"])),
        )

    def set_acceptance_key(self, run_id: str, acceptance_key: int) -> None:
        self.get_run(run_id)
        self._set_acceptance_key(run_id, acceptance_key)

    def _set_acceptance_key(self, run_id: str, acceptance_key: int) -> None:
        cursor = self._connection.execute(
            """INSERT INTO run_private_state(run_id, acceptance_key, segment_path)
               VALUES (?, ?, NULL)
               ON CONFLICT(run_id) DO UPDATE SET acceptance_key = excluded.acceptance_key
               WHERE run_private_state.acceptance_key IS NULL
                  OR run_private_state.acceptance_key = excluded.acceptance_key""",
            (run_id, acceptance_key),
        )
        if cursor.rowcount != 1:
            raise ValueError("run acceptance key cannot change")

    def set_segment_path(self, run_id: str, segment_path: Path) -> None:
        self.get_run(run_id)
        self._set_segment_path(run_id, segment_path)

    def _set_segment_path(self, run_id: str, segment_path: Path) -> None:
        cursor = self._connection.execute(
            """INSERT INTO run_private_state(run_id, acceptance_key, segment_path)
               VALUES (?, NULL, ?)
               ON CONFLICT(run_id) DO UPDATE SET segment_path = excluded.segment_path
               WHERE run_private_state.segment_path IS NULL
                  OR run_private_state.segment_path = excluded.segment_path""",
            (run_id, str(segment_path)),
        )
        if cursor.rowcount != 1:
            raise ValueError("run segment path cannot change")

    def record_extraction(
        self,
        run_id: str,
        *,
        source_boundary_digest: str,
        source_boundary_payload: bytes,
        manifest_digest: str,
        manifest_payload: bytes,
        segment_path: Path,
        batch_id: str,
        row_count: int,
        encoded_bytes: int,
        occurred_at: datetime,
        producer: str,
    ) -> EvidenceEvent:
        for artifact_digest, payload in (
            (source_boundary_digest, source_boundary_payload),
            (manifest_digest, manifest_payload),
        ):
            if hashlib.sha256(payload).hexdigest() != artifact_digest:
                raise ValueError("artifact digest does not match payload")
        timestamp = _timestamp(occurred_at)
        attributes = _attributes(
            {
                "row_count": row_count,
                "encoded_bytes": encoded_bytes,
                "manifest_digest": manifest_digest,
                "source_boundary_digest": source_boundary_digest,
            }
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self.get_run(run_id)
            self._save_artifact("source_boundary", source_boundary_digest, source_boundary_payload)
            self._save_artifact("segment_manifest", manifest_digest, manifest_payload)
            self._set_segment_path(run_id, segment_path)
            cursor = self._connection.execute(
                """UPDATE runs SET checkpoint = 'extraction_completed', state = 'running',
                   batch_id = ?, updated_at = ?
                   WHERE run_id = ? AND checkpoint = 'snapshot_opened'
                     AND state NOT IN ('succeeded','failed','non_conforming')""",
                (batch_id, timestamp, run_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition("checkpoint compare-and-set failed")
            event = self._append_event(
                run_id,
                "extraction_completed",
                occurred_at,
                producer,
                attributes,
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return event

    def transition_run(
        self,
        run_id: str,
        expected_state: RunState,
        state: RunState,
        checkpoint: str,
        now: datetime,
        *,
        batch_id: str | None = None,
    ) -> RunRecord:
        current = self.get_run(run_id)
        if current.state in TERMINAL_STATES:
            raise InvalidStateTransition("terminal run state cannot regress")
        if current.state != expected_state:
            raise InvalidStateTransition(f"expected state {expected_state}, found {current.state}")
        cursor = self._connection.execute(
            """UPDATE runs SET state = ?, checkpoint = ?, batch_id = COALESCE(?, batch_id),
               updated_at = ? WHERE run_id = ? AND state = ?""",
            (state, checkpoint, batch_id, _timestamp(now), run_id, expected_state),
        )
        if cursor.rowcount != 1:
            raise InvalidStateTransition("run state changed concurrently")
        return self.get_run(run_id)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        occurred_at: datetime,
        producer: str,
        attributes: dict[str, object],
    ) -> EvidenceEvent:
        normalized = _attributes(attributes)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            event = self._append_event(run_id, event_type, occurred_at, producer, normalized)
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return event

    def advance_checkpoint_with_event(
        self,
        run_id: str,
        *,
        expected_checkpoint: str,
        checkpoint: str,
        state: RunState,
        event_type: str,
        occurred_at: datetime,
        producer: str,
        attributes: dict[str, object],
        batch_id: str | None = None,
    ) -> EvidenceEvent:
        normalized = _attributes(attributes)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """UPDATE runs SET checkpoint = ?, state = ?, batch_id = COALESCE(?, batch_id),
                   updated_at = ? WHERE run_id = ? AND checkpoint = ?
                   AND state NOT IN ('succeeded','failed','non_conforming')""",
                (
                    checkpoint,
                    state,
                    batch_id,
                    _timestamp(occurred_at),
                    run_id,
                    expected_checkpoint,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition("checkpoint compare-and-set failed")
            event = self._append_event(run_id, event_type, occurred_at, producer, normalized)
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return event

    def _append_event(
        self,
        run_id: str,
        event_type: str,
        occurred_at: datetime,
        producer: str,
        attributes: dict[str, JsonValue],
    ) -> EvidenceEvent:
        previous = self._connection.execute(
            """SELECT sequence, event_digest FROM evidence_events
               WHERE run_id = ? ORDER BY sequence DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_digest = None if previous is None else str(previous["event_digest"])
        body = {
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "producer": producer,
            "attributes": attributes,
            "previous_digest": previous_digest,
        }
        event = EvidenceEvent(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            producer=producer,
            attributes=attributes,
            previous_digest=previous_digest,
            event_digest=digest(body),
        )
        self._connection.execute(
            """INSERT INTO evidence_events(
                run_id, sequence, event_type, occurred_at, producer, attributes_json,
                previous_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.run_id,
                event.sequence,
                event.event_type,
                _timestamp(event.occurred_at),
                event.producer,
                canonical_bytes(event.attributes).decode("utf-8"),
                event.previous_digest,
                event.event_digest,
            ),
        )
        return event

    def trace(self, run_id: str) -> tuple[EvidenceEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM evidence_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        return tuple(
            EvidenceEvent(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                producer=row["producer"],
                attributes=cast(dict[str, JsonValue], json.loads(row["attributes_json"])),
                previous_digest=row["previous_digest"],
                event_digest=row["event_digest"],
            )
            for row in rows
        )

    def verify_chain(self, run_id: str) -> bool:
        previous: str | None = None
        for event in self.trace(run_id):
            body = {
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "producer": event.producer,
                "attributes": event.attributes,
                "previous_digest": event.previous_digest,
            }
            if event.previous_digest != previous or event.event_digest != digest(body):
                return False
            previous = event.event_digest
        return True


def _like_literal(value: str) -> str:
    """Escape LIKE metacharacters so a prefix matches literally under ESCAPE '\\'."""
    for character in ("\\", "%", "_"):
        value = value.replace(character, f"\\{character}")
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def _attributes(values: dict[str, object]) -> dict[str, JsonValue]:
    normalized = canonical_value(values)
    if not isinstance(normalized, dict):
        raise TypeError("evidence attributes must be an object")
    return normalized


def _digest_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if _DIGEST.fullmatch(value) else set()
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_digest_strings(item))
        return result
    if isinstance(value, dict):
        result = set()
        for item in value.values():
            result.update(_digest_strings(item))
        return result
    return set()
