from __future__ import annotations

import sqlite3
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from .models import AcquisitionContractLifecycleState


class AcquisitionContractLifecycleNotFoundError(LookupError):
    pass


class StaleAcquisitionContractLifecycleError(RuntimeError):
    pass


class AcquisitionContractLifecycleRepository(Protocol):
    def activate(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        activated_at: datetime,
    ) -> AcquisitionContractLifecycleState: ...

    def get(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState: ...

    def list_activated(
        self,
        tenant_id: str,
    ) -> tuple[AcquisitionContractLifecycleState, ...]: ...

    def retire(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        expected_revision: int,
        retired_at: datetime,
    ) -> AcquisitionContractLifecycleState: ...


class SQLiteAcquisitionContractLifecycleRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS acquisition_contract_lifecycles ("
            "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, revision INTEGER NOT NULL, "
            "lifecycle_state TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, contract_digest))"
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def activate(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        activated_at: datetime,
    ) -> AcquisitionContractLifecycleState:
        candidate = AcquisitionContractLifecycleState(
            tenant_id=tenant_id,
            contract_digest=contract_digest,
            revision=1,
            lifecycle_state="activated",
            activated_at=activated_at,
            retired_at=None,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._load_optional(tenant_id, contract_digest)
                if existing is not None:
                    if existing.lifecycle_state != "activated":
                        raise StaleAcquisitionContractLifecycleError(
                            "retired acquisition contract cannot reactivate"
                        )
                    self._connection.commit()
                    return existing
                self._connection.execute(
                    "INSERT INTO acquisition_contract_lifecycles "
                    "(tenant_id, contract_digest, revision, lifecycle_state, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        candidate.tenant_id,
                        candidate.contract_digest,
                        candidate.revision,
                        candidate.lifecycle_state,
                        candidate.model_dump_json().encode(),
                    ),
                )
                self._connection.commit()
                return candidate
            except BaseException:
                self._connection.rollback()
                raise

    def get(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState:
        with self._lock:
            state = self._load_optional(tenant_id, contract_digest)
        if state is None:
            raise AcquisitionContractLifecycleNotFoundError((tenant_id, contract_digest))
        return state

    def retire(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        expected_revision: int,
        retired_at: datetime,
    ) -> AcquisitionContractLifecycleState:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._load_optional(tenant_id, contract_digest)
                if current is None:
                    raise AcquisitionContractLifecycleNotFoundError((tenant_id, contract_digest))
                if current.lifecycle_state == "retired":
                    if current.revision == expected_revision + 1:
                        self._connection.commit()
                        return current
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                if current.revision != expected_revision:
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                retired = current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "lifecycle_state": "retired",
                        "retired_at": retired_at,
                    }
                )
                retired = AcquisitionContractLifecycleState.model_validate(
                    retired.model_dump(),
                    strict=True,
                )
                result = self._connection.execute(
                    "UPDATE acquisition_contract_lifecycles "
                    "SET revision = ?, lifecycle_state = ?, payload = ? "
                    "WHERE tenant_id = ? AND contract_digest = ? AND revision = ? "
                    "AND lifecycle_state = 'activated'",
                    (
                        retired.revision,
                        retired.lifecycle_state,
                        retired.model_dump_json().encode(),
                        tenant_id,
                        contract_digest,
                        expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise StaleAcquisitionContractLifecycleError(
                        "acquisition contract lifecycle revision is stale"
                    )
                self._connection.commit()
                return retired
            except BaseException:
                self._connection.rollback()
                raise

    def list_activated(
        self,
        tenant_id: str,
    ) -> tuple[AcquisitionContractLifecycleState, ...]:
        """Every contract currently activated for one tenant.

        This is the tenant half of deriving a run's tenant: a run carries a contract
        digest, and a contract digest is tenant-qualified only here. The filter is
        `activated` rather than "every row", because a retired contract is no longer
        a live contract of this tenant.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT revision, lifecycle_state, payload "
                "FROM acquisition_contract_lifecycles "
                "WHERE tenant_id = ? AND lifecycle_state = 'activated' "
                "ORDER BY contract_digest",
                (tenant_id,),
            ).fetchall()
        return tuple(self._state_from_row(row) for row in rows)

    def _state_from_row(self, row: tuple[Any, ...]) -> AcquisitionContractLifecycleState:
        state = AcquisitionContractLifecycleState.model_validate_json(row[2], strict=True)
        if state.revision != int(row[0]) or state.lifecycle_state != str(row[1]):
            raise RuntimeError("stored acquisition contract lifecycle authority mismatch")
        return state

    def _load_optional(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState | None:
        row = self._connection.execute(
            "SELECT revision, lifecycle_state, payload "
            "FROM acquisition_contract_lifecycles "
            "WHERE tenant_id = ? AND contract_digest = ?",
            (tenant_id, contract_digest),
        ).fetchone()
        if row is None:
            return None
        return self._state_from_row(row)
