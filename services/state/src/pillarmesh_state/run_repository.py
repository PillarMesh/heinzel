from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from pillarmesh_contract_model import canonical_bytes

from .run_models import (
    RunAttemptClaim,
    RunAttemptCompletion,
    RunCancellation,
    RunRecord,
    RunRetryRequest,
)


class SQLiteRunRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(str(database_path), timeout=5)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, intent_digest TEXT NOT NULL UNIQUE, "
            "payload BLOB NOT NULL);"
            "CREATE TABLE IF NOT EXISTS run_attempt_claims ("
            "run_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, epoch INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (run_id, attempt_number), "
            "UNIQUE (run_id, epoch), FOREIGN KEY (run_id) REFERENCES runs(run_id));"
            "CREATE TABLE IF NOT EXISTS run_attempt_completions ("
            "run_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (run_id, attempt_number), "
            "FOREIGN KEY (run_id, attempt_number) "
            "REFERENCES run_attempt_claims(run_id, attempt_number));"
            "CREATE TABLE IF NOT EXISTS run_cancellations ("
            "run_id TEXT PRIMARY KEY, payload BLOB NOT NULL, "
            "FOREIGN KEY (run_id) REFERENCES runs(run_id));"
            "CREATE TABLE IF NOT EXISTS run_retry_requests ("
            "command_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
            "failed_attempt_number INTEGER NOT NULL, "
            "payload BLOB NOT NULL, UNIQUE (run_id, failed_attempt_number), "
            "FOREIGN KEY (run_id) REFERENCES runs(run_id));"
        )

    def close(self) -> None:
        self._connection.close()

    def load_owned(self, tenant_id: str, run_id: str) -> RunRecord:
        row = self._connection.execute(
            "SELECT payload FROM runs WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError("run is unavailable")
        return RunRecord.model_validate_json(bytes(row[0]))

    def load_by_intent(self, tenant_id: str, intent_digest: str) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT payload FROM runs WHERE tenant_id = ? AND intent_digest = ?",
            (tenant_id, intent_digest),
        ).fetchone()
        if row is None:
            return None
        return RunRecord.model_validate_json(bytes(row[0]))

    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM runs WHERE tenant_id = ? ORDER BY run_id",
            (tenant_id,),
        ).fetchall()
        return tuple(RunRecord.model_validate_json(bytes(row[0])) for row in rows)

    def insert_run(self, run: RunRecord) -> RunRecord:
        with self.transaction():
            existing = self.load_by_intent(run.intent.tenant_id, run.intent_digest)
            if existing is not None:
                return existing
            self._connection.execute(
                "INSERT INTO runs (run_id, tenant_id, intent_digest, payload) VALUES (?, ?, ?, ?)",
                (
                    run.run_id,
                    run.intent.tenant_id,
                    run.intent_digest,
                    canonical_bytes(run),
                ),
            )
        return run

    def latest_claim(self, run_id: str) -> RunAttemptClaim | None:
        row = self._connection.execute(
            "SELECT payload FROM run_attempt_claims WHERE run_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunAttemptClaim.model_validate_json(bytes(row[0]))

    def load_completion(self, run_id: str, attempt_number: int) -> RunAttemptCompletion | None:
        row = self._connection.execute(
            "SELECT payload FROM run_attempt_completions WHERE run_id = ? AND attempt_number = ?",
            (run_id, attempt_number),
        ).fetchone()
        if row is None:
            return None
        return RunAttemptCompletion.model_validate_json(bytes(row[0]))

    def insert_claim(self, claim: RunAttemptClaim) -> None:
        self._connection.execute(
            "INSERT INTO run_attempt_claims (run_id, attempt_number, epoch, payload) "
            "VALUES (?, ?, ?, ?)",
            (claim.run_id, claim.attempt_number, claim.epoch, canonical_bytes(claim)),
        )

    def insert_completion(self, completion: RunAttemptCompletion) -> None:
        self._connection.execute(
            "INSERT INTO run_attempt_completions (run_id, attempt_number, payload) "
            "VALUES (?, ?, ?)",
            (completion.run_id, completion.attempt_number, canonical_bytes(completion)),
        )

    def load_cancellation(self, run_id: str) -> RunCancellation | None:
        row = self._connection.execute(
            "SELECT payload FROM run_cancellations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunCancellation.model_validate_json(bytes(row[0]))

    def insert_cancellation(self, cancellation: RunCancellation) -> None:
        self._connection.execute(
            "INSERT INTO run_cancellations (run_id, payload) VALUES (?, ?)",
            (cancellation.run_id, canonical_bytes(cancellation)),
        )

    def load_retry_request_by_command(self, run_id: str, command_id: str) -> RunRetryRequest | None:
        row = self._connection.execute(
            "SELECT payload FROM run_retry_requests WHERE run_id = ? AND command_id = ?",
            (run_id, command_id),
        ).fetchone()
        if row is None:
            return None
        return RunRetryRequest.model_validate_json(bytes(row[0]))

    def load_retry_request_for_attempt(
        self, run_id: str, failed_attempt_number: int
    ) -> RunRetryRequest | None:
        row = self._connection.execute(
            "SELECT payload FROM run_retry_requests WHERE run_id = ? AND failed_attempt_number = ?",
            (run_id, failed_attempt_number),
        ).fetchone()
        if row is None:
            return None
        return RunRetryRequest.model_validate_json(bytes(row[0]))

    def list_retry_requests(self, run_id: str) -> tuple[RunRetryRequest, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM run_retry_requests WHERE run_id = ? "
            "ORDER BY failed_attempt_number",
            (run_id,),
        ).fetchall()
        return tuple(RunRetryRequest.model_validate_json(bytes(row[0])) for row in rows)

    def insert_retry_request(self, request: RunRetryRequest) -> None:
        self._connection.execute(
            "INSERT INTO run_retry_requests "
            "(command_id, run_id, failed_attempt_number, payload) VALUES (?, ?, ?, ?)",
            (
                request.command_id,
                request.run_id,
                request.failed_attempt_number,
                canonical_bytes(request),
            ),
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        else:
            self._connection.commit()
