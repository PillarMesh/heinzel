import sqlite3
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes

from .models import WarehouseBinding


class WarehouseRepository(Protocol):
    def next_sequence(self, tenant_id: str) -> int: ...

    def save(self, binding: WarehouseBinding) -> None: ...

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None: ...


class StaleRevisionError(Exception):
    pass


class SQLiteWarehouseRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS warehouse_bindings ("
            "binding_id TEXT NOT NULL, "
            "revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "PRIMARY KEY (binding_id, revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS warehouse_sequences ("
            "tenant_id TEXT PRIMARY KEY, "
            "next_sequence INTEGER NOT NULL"
            ")"
        )
        self._connection.commit()

    def next_sequence(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "INSERT INTO warehouse_sequences (tenant_id, next_sequence) VALUES (?, 2) "
            "ON CONFLICT(tenant_id) DO UPDATE "
            "SET next_sequence = warehouse_sequences.next_sequence + 1 "
            "RETURNING next_sequence - 1",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("warehouse sequence allocation did not return a sequence")
        self._connection.commit()
        return int(row[0])

    def save(self, binding: WarehouseBinding) -> None:
        payload = canonical_bytes(binding)
        try:
            if binding.revision == 1:
                self._connection.execute(
                    "INSERT INTO warehouse_bindings (binding_id, revision, tenant_id, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (binding.binding_id, binding.revision, binding.tenant_id, payload),
                )
            else:
                result = self._connection.execute(
                    "INSERT INTO warehouse_bindings (binding_id, revision, tenant_id, payload) "
                    "SELECT ?, ?, ?, ? "
                    "WHERE EXISTS ("
                    "SELECT 1 FROM warehouse_bindings "
                    "WHERE binding_id = ? AND tenant_id = ? AND revision = ?"
                    ") AND NOT EXISTS ("
                    "SELECT 1 FROM warehouse_bindings WHERE binding_id = ? AND revision = ?"
                    ")",
                    (
                        binding.binding_id,
                        binding.revision,
                        binding.tenant_id,
                        payload,
                        binding.binding_id,
                        binding.tenant_id,
                        binding.revision - 1,
                        binding.binding_id,
                        binding.revision,
                    ),
                )
                if result.rowcount != 1:
                    self._connection.rollback()
                    raise StaleRevisionError("warehouse binding revision was not advanced")
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise StaleRevisionError("warehouse binding revision was not advanced") from error
        except BaseException:
            # Any other failure must not leave the implicit transaction open: the next
            # successful call's commit would publish this partial write.
            self._connection.rollback()
            raise
        self._connection.commit()

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        row = self._connection.execute(
            "SELECT payload FROM warehouse_bindings "
            "WHERE tenant_id = ? AND binding_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (tenant_id, binding_id),
        ).fetchone()
        if row is None:
            return None
        return WarehouseBinding.model_validate_json(row[0])
