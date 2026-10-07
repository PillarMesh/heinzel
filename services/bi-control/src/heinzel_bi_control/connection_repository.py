from __future__ import annotations

import sqlite3
from contextlib import suppress
from typing import Literal

from heinzel_contract_model import ArtifactReference, canonical_bytes
from pydantic import ValidationError

from .models import DashboardDatasetConnectionBinding


class DashboardConnectionConflict(ValueError):
    pass


class DashboardConnectionAuthorityError(ValueError):
    pass


class SQLiteDashboardConnectionRepository:
    """Persist the exact warehouse connection authorized for a consumption object."""

    def __init__(self, database_path: str) -> None:
        #
        # A console serves its reads on a worker thread while composing its stores on the
        # thread that started it, so a connection bound to its creating thread refuses every
        # resolve as an unavailable authority. The sqlite3 module serializes access itself.
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_dataset_connections_v1 ("
            "tenant_id TEXT NOT NULL, engine_kind TEXT NOT NULL, "
            "consumption_object_id TEXT NOT NULL, consumption_object_version INTEGER NOT NULL, "
            "consumption_object_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, engine_kind, consumption_object_id, "
            "consumption_object_version, consumption_object_digest))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(
        self, binding: DashboardDatasetConnectionBinding
    ) -> DashboardDatasetConnectionBinding:
        binding = DashboardDatasetConnectionBinding.model_validate(
            binding.model_dump(mode="python"), strict=True
        )
        key = self._key(
            tenant_id=binding.tenant_id,
            engine_kind=binding.engine_kind,
            consumption_object_ref=binding.consumption_object_ref,
        )
        payload = canonical_bytes(binding)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT payload FROM dashboard_dataset_connections_v1 "
                "WHERE tenant_id = ? AND engine_kind = ? AND consumption_object_id = ? "
                "AND consumption_object_version = ? AND consumption_object_digest = ?",
                key,
            ).fetchone()
            if row is not None:
                if bytes(row[0]) != payload:
                    raise DashboardConnectionConflict("dashboard connection authority is immutable")
                stored = self._validate_row(row[0], key)
                self._connection.commit()
                return stored
            self._connection.execute(
                "INSERT INTO dashboard_dataset_connections_v1 VALUES (?, ?, ?, ?, ?, ?)",
                (*key, payload),
            )
            self._connection.commit()
            return binding
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def resolve(
        self,
        *,
        tenant_id: str,
        engine_kind: Literal["postgresql", "clickhouse"],
        consumption_object_ref: ArtifactReference,
    ) -> DashboardDatasetConnectionBinding | None:
        key = self._key(
            tenant_id=tenant_id,
            engine_kind=engine_kind,
            consumption_object_ref=consumption_object_ref,
        )
        try:
            row = self._connection.execute(
                "SELECT payload FROM dashboard_dataset_connections_v1 "
                "WHERE tenant_id = ? AND engine_kind = ? AND consumption_object_id = ? "
                "AND consumption_object_version = ? AND consumption_object_digest = ?",
                key,
            ).fetchone()
        except sqlite3.Error as error:
            raise DashboardConnectionAuthorityError(
                "dashboard connection authority is unavailable"
            ) from error
        if row is None:
            return None
        return self._validate_row(row[0], key)

    @staticmethod
    def _key(
        *,
        tenant_id: str,
        engine_kind: Literal["postgresql", "clickhouse"],
        consumption_object_ref: ArtifactReference,
    ) -> tuple[str, str, str, int, str]:
        return (
            tenant_id,
            engine_kind,
            consumption_object_ref.artifact_id,
            consumption_object_ref.version,
            consumption_object_ref.digest,
        )

    @staticmethod
    def _validate_row(
        payload: object, expected_key: tuple[str, str, str, int, str]
    ) -> DashboardDatasetConnectionBinding:
        if not isinstance(payload, bytes):
            raise DashboardConnectionAuthorityError(
                "stored dashboard connection authority is invalid"
            )
        try:
            binding = DashboardDatasetConnectionBinding.model_validate_json(payload, strict=True)
        except (ValidationError, ValueError, TypeError):
            raise DashboardConnectionAuthorityError(
                "stored dashboard connection authority is invalid"
            ) from None
        actual_key = SQLiteDashboardConnectionRepository._key(
            tenant_id=binding.tenant_id,
            engine_kind=binding.engine_kind,
            consumption_object_ref=binding.consumption_object_ref,
        )
        if actual_key != expected_key:
            raise DashboardConnectionAuthorityError(
                "stored dashboard connection authority index is invalid"
            )
        return binding
