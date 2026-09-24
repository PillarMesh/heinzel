from __future__ import annotations

import sqlite3
from threading import Lock

from heinzel_contract_model import canonical_bytes
from pydantic import ValidationError

from .models import SignedDashboardContract


class DashboardContractAuthorityError(ValueError):
    pass


class SQLiteDashboardContractRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS signed_dashboard_contracts_v1 ("
            "tenant_id TEXT NOT NULL, dashboard_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, dashboard_id, version))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, signed: SignedDashboardContract) -> SignedDashboardContract:
        envelope = SignedDashboardContract.model_validate(
            signed.model_dump(mode="python"), strict=True
        )
        key = (
            envelope.tenant_id,
            envelope.contract.dashboard_id,
            envelope.contract.version,
        )
        payload = canonical_bytes(envelope)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT payload FROM signed_dashboard_contracts_v1 "
                    "WHERE tenant_id = ? AND dashboard_id = ? AND version = ?",
                    key,
                ).fetchone()
                if row is not None:
                    if bytes(row[0]) != payload:
                        raise ValueError("conflicting dashboard contract replay")
                    stored = self._validate_stored(row[0], key)
                    self._connection.commit()
                    return stored
                self._connection.execute(
                    "INSERT INTO signed_dashboard_contracts_v1 VALUES (?, ?, ?, ?)",
                    (*key, payload),
                )
                self._connection.commit()
                return envelope
            except BaseException:
                self._connection.rollback()
                raise

    def read_exact(
        self, *, tenant_id: str, dashboard_id: str, version: int
    ) -> SignedDashboardContract | None:
        key = (tenant_id, dashboard_id, version)
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload FROM signed_dashboard_contracts_v1 "
                    "WHERE tenant_id = ? AND dashboard_id = ? AND version = ?",
                    key,
                ).fetchone()
        except sqlite3.Error as error:
            raise DashboardContractAuthorityError(
                "dashboard contract authority is unavailable"
            ) from error
        if row is None:
            return None
        return self._validate_stored(row[0], key)

    @staticmethod
    def _validate_stored(
        payload: bytes | str, expected_key: tuple[str, str, int]
    ) -> SignedDashboardContract:
        try:
            envelope = SignedDashboardContract.model_validate_json(payload, strict=True)
        except (ValidationError, ValueError, TypeError):
            raise DashboardContractAuthorityError(
                "stored dashboard contract payload is invalid"
            ) from None
        actual_key = (
            envelope.tenant_id,
            envelope.contract.dashboard_id,
            envelope.contract.version,
        )
        if actual_key != expected_key:
            raise DashboardContractAuthorityError(
                "dashboard contract authority index does not match its payload"
            )
        return envelope
