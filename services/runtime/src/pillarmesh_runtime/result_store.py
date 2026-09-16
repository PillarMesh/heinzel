from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pillarmesh_contract_model import canonical_bytes

from .answer_errors import AnswerExecutionConflict, QueryResultNotFound
from .answer_models import AnswerExecutionReceipt, AnswerResultSnapshot


class SQLiteAnswerResultStore:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS answer_executions ("
            "tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, input_digest TEXT NOT NULL, "
            "latest_attempt INTEGER NOT NULL, latest_receipt BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, request_id));"
            "CREATE TABLE IF NOT EXISTS answer_execution_attempts ("
            "tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
            "receipt BLOB NOT NULL, PRIMARY KEY (tenant_id, request_id, attempt), "
            "FOREIGN KEY (tenant_id, request_id) "
            "REFERENCES answer_executions(tenant_id, request_id));"
            "CREATE TABLE IF NOT EXISTS answer_results ("
            "result_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, payload BLOB NOT NULL);"
        )

    @classmethod
    def in_memory(cls, *, clock: Callable[[], datetime] | None = None) -> SQLiteAnswerResultStore:
        return cls(sqlite3.connect(":memory:"), clock=clock)

    @classmethod
    def open(
        cls, path: Path, *, clock: Callable[[], datetime] | None = None
    ) -> SQLiteAnswerResultStore:
        return cls(sqlite3.connect(path), clock=clock)

    def load_execution(
        self, tenant_id: str, request_id: str
    ) -> tuple[str, AnswerExecutionReceipt] | None:
        row = self._connection.execute(
            "SELECT input_digest, latest_receipt FROM answer_executions "
            "WHERE tenant_id = ? AND request_id = ?",
            (tenant_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), AnswerExecutionReceipt.model_validate_json(bytes(row[1]), strict=True)

    def record_attempt(
        self,
        *,
        input_digest: str,
        receipt: AnswerExecutionReceipt,
        snapshot: AnswerResultSnapshot | None,
    ) -> None:
        if (snapshot is None) != (receipt.outcome != "succeeded"):
            raise AnswerExecutionConflict("execution result does not match its receipt")
        if snapshot is not None and (
            snapshot.tenant_id != receipt.tenant_id
            or snapshot.request_id != receipt.request_id
            or snapshot.plan_digest != receipt.plan_digest
            or snapshot.result_ref != receipt.result_ref
            or snapshot.result_digest != receipt.result_digest
        ):
            raise AnswerExecutionConflict("execution result authority does not match its receipt")
        with self._connection:
            existing = self.load_execution(receipt.tenant_id, receipt.request_id)
            if existing is None:
                self._connection.execute(
                    "INSERT INTO answer_executions "
                    "(tenant_id, request_id, input_digest, latest_attempt, latest_receipt) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        receipt.tenant_id,
                        receipt.request_id,
                        input_digest,
                        receipt.attempt,
                        canonical_bytes(receipt),
                    ),
                )
            else:
                recorded_digest, prior_receipt = existing
                if recorded_digest != input_digest:
                    raise AnswerExecutionConflict(
                        "request replay conflicts with the recorded execution authority"
                    )
                if receipt.attempt != prior_receipt.attempt + 1:
                    raise AnswerExecutionConflict("execution attempt is not the next attempt")
                self._connection.execute(
                    "UPDATE answer_executions SET latest_attempt = ?, latest_receipt = ? "
                    "WHERE tenant_id = ? AND request_id = ?",
                    (
                        receipt.attempt,
                        canonical_bytes(receipt),
                        receipt.tenant_id,
                        receipt.request_id,
                    ),
                )
            self._connection.execute(
                "INSERT INTO answer_execution_attempts "
                "(tenant_id, request_id, attempt, receipt) VALUES (?, ?, ?, ?)",
                (
                    receipt.tenant_id,
                    receipt.request_id,
                    receipt.attempt,
                    canonical_bytes(receipt),
                ),
            )
            if snapshot is not None:
                self._connection.execute(
                    "INSERT INTO answer_results "
                    "(result_ref, tenant_id, request_id, expires_at, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot.result_ref,
                        snapshot.tenant_id,
                        snapshot.request_id,
                        snapshot.expires_at.isoformat(),
                        canonical_bytes(snapshot),
                    ),
                )

    def list_attempts(self, tenant_id: str, request_id: str) -> tuple[AnswerExecutionReceipt, ...]:
        rows = self._connection.execute(
            "SELECT receipt FROM answer_execution_attempts "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY attempt",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(
            AnswerExecutionReceipt.model_validate_json(bytes(row[0]), strict=True) for row in rows
        )

    def list_for_tenant(self, *, tenant_id: str) -> tuple[AnswerExecutionReceipt, ...]:
        rows = self._connection.execute(
            "SELECT receipt FROM answer_execution_attempts "
            "WHERE tenant_id = ? ORDER BY request_id, attempt",
            (tenant_id,),
        ).fetchall()
        return tuple(
            AnswerExecutionReceipt.model_validate_json(bytes(row[0]), strict=True) for row in rows
        )

    def read_result(self, tenant_id: str, result_ref: str) -> AnswerResultSnapshot:
        row = self._connection.execute(
            "SELECT payload FROM answer_results WHERE tenant_id = ? AND result_ref = ? "
            "AND expires_at > ?",
            (tenant_id, result_ref, self._clock().isoformat()),
        ).fetchone()
        if row is None:
            raise QueryResultNotFound("result is not available")
        return AnswerResultSnapshot.model_validate_json(bytes(row[0]), strict=True)
