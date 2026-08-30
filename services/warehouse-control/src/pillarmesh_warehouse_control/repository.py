from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes, digest

from .errors import (
    WarehouseOperationConflictError,
    WarehousePersistenceError,
    WarehouseValidationConflictError,
)
from .evidence import (
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
)
from .models import (
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
)
from .private_state import (
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
)
from .retirement import canonical_retirement_resource_snapshot, retirement_evidence_mismatch

_SCHEMA_VERSION = 2
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS warehouse_schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS warehouse_bindings_tenant_revision
    ON warehouse_bindings (tenant_id, binding_id, revision);

CREATE TABLE IF NOT EXISTS warehouse_operation_sequences (
    tenant_id TEXT PRIMARY KEY,
    next_sequence INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS private_warehouse_operations (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('claimed', 'running', 'reconciling', 'succeeded', 'failed')
    ),
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, binding_id, binding_revision, operation_id),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_warehouse_operation_per_binding
    ON private_warehouse_operations (tenant_id, binding_id)
    WHERE status IN ('claimed', 'running', 'reconciling');

CREATE TABLE IF NOT EXISTS private_warehouse_operation_claims (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, binding_id, binding_revision, operation_id),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision, operation_id)
        REFERENCES private_warehouse_operations (
            tenant_id, binding_id, binding_revision, operation_id
        )
);

CREATE TABLE IF NOT EXISTS private_warehouse_resources (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, binding_id, binding_revision, operation_id, resource_id),
    UNIQUE (tenant_id, resource_id),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision, operation_id)
        REFERENCES private_warehouse_operations (
            tenant_id, binding_id, binding_revision, operation_id
        )
);

CREATE TABLE IF NOT EXISTS warehouse_validation_evidence (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id),
    UNIQUE (tenant_id, binding_id, binding_revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision)
);

CREATE TABLE IF NOT EXISTS warehouse_resume_validation_evidence (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id),
    UNIQUE (tenant_id, binding_id, binding_revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision)
);

CREATE TABLE IF NOT EXISTS warehouse_restore_verifications (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    verification_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, verification_id),
    UNIQUE (tenant_id, binding_id, binding_revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision)
);

CREATE TABLE IF NOT EXISTS warehouse_retirement_evidence (
    tenant_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (tenant_id, evidence_id),
    UNIQUE (tenant_id, binding_id, binding_revision),
    FOREIGN KEY (tenant_id, binding_id, binding_revision)
        REFERENCES warehouse_bindings (tenant_id, binding_id, revision)
);
""".strip()
_SCHEMA_CHECKSUM = hashlib.sha256(_SCHEMA_SQL.encode("utf-8")).hexdigest()
_SCHEMA_V1_CHECKSUM = _SCHEMA_CHECKSUM
_LEGACY_HBA_RESOURCE_KIND = b'"resource_kind":"hba_configuration"'
_CREDENTIAL_FILE_RESOURCE_KIND = b'"resource_kind":"credential_file"'
_LIVE_OPERATION_STATUSES = frozenset(
    {
        WarehouseOperationStatus.CLAIMED,
        WarehouseOperationStatus.RUNNING,
        WarehouseOperationStatus.RECONCILING,
    }
)
_ALLOWED_OPERATION_STATUS_TRANSITIONS = frozenset(
    {
        (WarehouseOperationStatus.CLAIMED, WarehouseOperationStatus.RUNNING),
        (WarehouseOperationStatus.CLAIMED, WarehouseOperationStatus.RECONCILING),
        (WarehouseOperationStatus.RUNNING, WarehouseOperationStatus.RUNNING),
        (WarehouseOperationStatus.RUNNING, WarehouseOperationStatus.RECONCILING),
        (WarehouseOperationStatus.RUNNING, WarehouseOperationStatus.SUCCEEDED),
        (WarehouseOperationStatus.RUNNING, WarehouseOperationStatus.FAILED),
        (WarehouseOperationStatus.RECONCILING, WarehouseOperationStatus.RUNNING),
        (WarehouseOperationStatus.RECONCILING, WarehouseOperationStatus.RECONCILING),
        (WarehouseOperationStatus.RECONCILING, WarehouseOperationStatus.SUCCEEDED),
        (WarehouseOperationStatus.RECONCILING, WarehouseOperationStatus.FAILED),
    }
)
_OPERATION_PHASES = {
    WarehouseOperationKind.PROVISION: frozenset(
        {
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationPhase.RESOURCES_PLANNED,
            WarehouseOperationPhase.PROVIDER_CREATED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationPhase.VALIDATED,
        }
    ),
    WarehouseOperationKind.SUSPEND: frozenset(
        {
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationPhase.SUSPENDED,
        }
    ),
    WarehouseOperationKind.RESUME: frozenset(
        {
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationPhase.RESUMED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationPhase.VALIDATED,
        }
    ),
    WarehouseOperationKind.RETIRE: frozenset(
        {
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationPhase.RETIREMENT_DISPOSITION_RECORDED,
            WarehouseOperationPhase.RETIRED,
        }
    ),
}
_TERMINAL_OPERATION_PHASES = {
    WarehouseOperationKind.PROVISION: WarehouseOperationPhase.VALIDATED,
    WarehouseOperationKind.SUSPEND: WarehouseOperationPhase.SUSPENDED,
    WarehouseOperationKind.RESUME: WarehouseOperationPhase.VALIDATED,
    WarehouseOperationKind.RETIRE: WarehouseOperationPhase.RETIRED,
}
_ALLOWED_OPERATION_PHASE_TRANSITIONS = {
    WarehouseOperationKind.PROVISION: frozenset(
        {
            (WarehouseOperationPhase.CLAIMED, WarehouseOperationPhase.PROVIDER_CREATED),
            (
                WarehouseOperationPhase.RESOURCES_PLANNED,
                WarehouseOperationPhase.PROVIDER_CREATED,
            ),
            (WarehouseOperationPhase.PROVIDER_CREATED, WarehouseOperationPhase.VALIDATING),
            (WarehouseOperationPhase.VALIDATING, WarehouseOperationPhase.VALIDATED),
        }
    ),
    WarehouseOperationKind.SUSPEND: frozenset(
        {(WarehouseOperationPhase.CLAIMED, WarehouseOperationPhase.SUSPENDED)}
    ),
    WarehouseOperationKind.RESUME: frozenset(
        {
            (WarehouseOperationPhase.CLAIMED, WarehouseOperationPhase.RESUMED),
            (WarehouseOperationPhase.RESUMED, WarehouseOperationPhase.VALIDATING),
            (WarehouseOperationPhase.VALIDATING, WarehouseOperationPhase.VALIDATED),
        }
    ),
    WarehouseOperationKind.RETIRE: frozenset(
        {
            (WarehouseOperationPhase.CLAIMED, WarehouseOperationPhase.RETIRED),
            (
                WarehouseOperationPhase.RETIREMENT_DISPOSITION_RECORDED,
                WarehouseOperationPhase.RETIRED,
            ),
        }
    ),
}
_TERMINAL_FAILURE_CLASSIFICATIONS = frozenset(
    {
        WarehouseFailureClassification.AUTHORIZATION_DENIED,
        WarehouseFailureClassification.STATEMENT_REJECTED,
        WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        WarehouseFailureClassification.INTEGRITY_FAILURE,
        WarehouseFailureClassification.PERMANENT_CONFIGURATION,
    }
)
_ALLOWED_RESOURCE_CREATION_TRANSITIONS = frozenset(
    {
        (
            WarehouseResourceCreationState.PLANNED,
            WarehouseResourceCreationState.CREATED,
        ),
        (
            WarehouseResourceCreationState.PLANNED,
            WarehouseResourceCreationState.AMBIGUOUS,
        ),
        (
            WarehouseResourceCreationState.PLANNED,
            WarehouseResourceCreationState.ABSENT,
        ),
        (
            WarehouseResourceCreationState.AMBIGUOUS,
            WarehouseResourceCreationState.CREATED,
        ),
        (
            WarehouseResourceCreationState.AMBIGUOUS,
            WarehouseResourceCreationState.ABSENT,
        ),
        (
            WarehouseResourceCreationState.ABSENT,
            WarehouseResourceCreationState.CREATED,
        ),
        (
            WarehouseResourceCreationState.ABSENT,
            WarehouseResourceCreationState.AMBIGUOUS,
        ),
    }
)
_ALLOWED_RESOURCE_CLEANUP_TRANSITIONS = frozenset(
    {
        (
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.RETAINED,
        ),
        (
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
        ),
        (
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.FAILED,
        ),
        (
            WarehouseResourceCleanupStatus.RETAINED,
            WarehouseResourceCleanupStatus.COMPLETE,
        ),
        (
            WarehouseResourceCleanupStatus.RETAINED,
            WarehouseResourceCleanupStatus.FAILED,
        ),
        (
            WarehouseResourceCleanupStatus.FAILED,
            WarehouseResourceCleanupStatus.COMPLETE,
        ),
    }
)


class _Connection(Protocol):
    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor: ...

    def executescript(self, sql: str) -> sqlite3.Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class WarehouseRepository(Protocol):
    def next_sequence(self, tenant_id: str) -> int: ...

    def save(self, binding: WarehouseBinding) -> None: ...

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None: ...

    def next_operation_sequence(self, tenant_id: str) -> int: ...

    def claim_operation(self, operation: PrivateWarehouseOperation) -> bool: ...

    def load_live_operation(
        self, tenant_id: str, binding_id: str
    ) -> PrivateWarehouseOperation | None: ...

    def load_operation(
        self, tenant_id: str, binding_id: str, operation_id: str
    ) -> PrivateWarehouseOperation: ...

    def save_operation(
        self,
        expected_operation: PrivateWarehouseOperation,
        updated_operation: PrivateWarehouseOperation,
    ) -> None: ...

    def record_terminal_operation_failure(
        self,
        expected_operation: PrivateWarehouseOperation,
        failed_operation: PrivateWarehouseOperation,
        failed_binding: WarehouseBinding,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_suspension(
        self,
        binding: WarehouseBinding,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_resources(self, resources: tuple[PrivateWarehouseResource, ...]) -> None: ...

    def save_resource(self, resource: PrivateWarehouseResource) -> PrivateWarehouseResource: ...

    def save_resources(
        self, resources: tuple[PrivateWarehouseResource, ...]
    ) -> tuple[PrivateWarehouseResource, ...]: ...

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]: ...

    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]: ...

    def abandon_draft(
        self,
        binding: WarehouseBinding,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_initial_validation_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseValidationEvidence,
        restore: WarehouseRestoreVerification,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_resume_validation_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseResumeValidationEvidence,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_retirement_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_retained_resource_deletion(
        self,
        evidence: WarehouseRetirementEvidence,
        *,
        expected_revision: int,
    ) -> None: ...

    def load_retirement_evidence(
        self,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> WarehouseRetirementEvidence: ...


class StaleRevisionError(Exception):
    pass


class SQLiteWarehouseRepository:
    def __init__(self, database_path: str) -> None:
        try:
            connection: _Connection = sqlite3.connect(database_path)
        except sqlite3.Error as error:
            raise WarehousePersistenceError(
                "warehouse persistence failed to initialize warehouse repository"
            ) from error
        self._connection = connection
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS warehouse_bindings ("
                "binding_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, "
                "tenant_id TEXT NOT NULL, "
                "payload BLOB NOT NULL, "
                "PRIMARY KEY (binding_id, revision)"
                ")"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS warehouse_sequences ("
                "tenant_id TEXT PRIMARY KEY, "
                "next_sequence INTEGER NOT NULL"
                ")"
            )
            connection.commit()
            self._migrate()
        except WarehousePersistenceError:
            with suppress(BaseException):
                connection.close()
            raise
        except sqlite3.Error as error:
            with suppress(BaseException):
                connection.rollback()
            with suppress(BaseException):
                connection.close()
            raise WarehousePersistenceError(
                "warehouse persistence failed to initialize warehouse repository"
            ) from error
        except BaseException:
            with suppress(BaseException):
                connection.rollback()
            with suppress(BaseException):
                connection.close()
            raise

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as error:
            raise WarehousePersistenceError(
                "warehouse persistence failed while closing repository"
            ) from error

    def next_sequence(self, tenant_id: str) -> int:
        return self._next_sequence("warehouse_sequences", tenant_id)

    def next_operation_sequence(self, tenant_id: str) -> int:
        return self._next_sequence("warehouse_operation_sequences", tenant_id)

    def save(self, binding: WarehouseBinding) -> None:
        with (
            _translate_sqlite_errors("save warehouse binding"),
            _transaction(self._connection),
        ):
            self._insert_binding(binding)

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        with _translate_sqlite_errors("load warehouse binding"):
            row = self._connection.execute(
                "SELECT payload FROM warehouse_bindings "
                "WHERE tenant_id = ? AND binding_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (tenant_id, binding_id),
            ).fetchone()
        if row is None:
            return None
        return WarehouseBinding.model_validate_json(row[0])

    def claim_operation(self, operation: PrivateWarehouseOperation) -> bool:
        if (
            operation.status is not WarehouseOperationStatus.CLAIMED
            or operation.phase is not WarehouseOperationPhase.CLAIMED
        ):
            raise WarehouseOperationConflictError(
                "warehouse operation claim requires initial claimed state and phase"
            )
        with (
            _translate_sqlite_errors("claim warehouse operation"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(operation.tenant_id, operation.binding_id)
            if current.revision != operation.binding_revision:
                raise WarehouseOperationConflictError(
                    "warehouse operation binding revision is stale"
                )
            if current.engine_kind is not operation.engine_kind:
                raise WarehouseOperationConflictError(
                    "warehouse operation engine does not match its binding"
                )
            existing = self._connection.execute(
                "SELECT payload FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                "AND operation_id = ?",
                (
                    operation.tenant_id,
                    operation.binding_id,
                    operation.binding_revision,
                    operation.operation_id,
                ),
            ).fetchone()
            if existing is not None:
                recorded = PrivateWarehouseOperation.model_validate_json(existing[0])
                if recorded == operation:
                    claimed = False
                else:
                    raise WarehouseOperationConflictError(
                        "warehouse operation identity already has different durable state"
                    )
            else:
                live = self._connection.execute(
                    "SELECT operation_id FROM private_warehouse_operations "
                    "WHERE tenant_id = ? AND binding_id = ? "
                    "AND status IN ('claimed', 'running', 'reconciling') LIMIT 1",
                    (operation.tenant_id, operation.binding_id),
                ).fetchone()
                if live is not None:
                    raise WarehouseOperationConflictError(
                        "warehouse binding already has a live operation"
                    )
                self._connection.execute(
                    "INSERT INTO private_warehouse_operations "
                    "(tenant_id, binding_id, binding_revision, operation_id, status, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    _operation_values(operation),
                )
                self._connection.execute(
                    "INSERT INTO private_warehouse_operation_claims "
                    "(tenant_id, binding_id, binding_revision, operation_id) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        operation.tenant_id,
                        operation.binding_id,
                        operation.binding_revision,
                        operation.operation_id,
                    ),
                )
                claimed = True
        return claimed

    def load_live_operation(
        self, tenant_id: str, binding_id: str
    ) -> PrivateWarehouseOperation | None:
        with _translate_sqlite_errors("load live warehouse operation"):
            row = self._connection.execute(
                "SELECT payload FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? "
                "AND status IN ('claimed', 'running', 'reconciling') LIMIT 1",
                (tenant_id, binding_id),
            ).fetchone()
        if row is None:
            return None
        return PrivateWarehouseOperation.model_validate_json(row[0])

    def load_operation(
        self, tenant_id: str, binding_id: str, operation_id: str
    ) -> PrivateWarehouseOperation:
        with _translate_sqlite_errors("load warehouse operation"):
            row = self._connection.execute(
                "SELECT payload FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? AND operation_id = ? "
                "ORDER BY binding_revision DESC LIMIT 1",
                (tenant_id, binding_id, operation_id),
            ).fetchone()
        if row is None:
            raise KeyError("private warehouse operation was not recorded")
        operation = PrivateWarehouseOperation.model_validate_json(row[0])
        _validate_operation_terminal_phase(operation)
        return operation

    def save_operation(
        self,
        expected_operation: PrivateWarehouseOperation,
        updated_operation: PrivateWarehouseOperation,
    ) -> None:
        _validate_operation_update(expected_operation, updated_operation)
        with (
            _translate_sqlite_errors("save warehouse operation"),
            _transaction(self._connection),
        ):
            self._update_operation_row(expected_operation, updated_operation)

    def _update_operation_row(
        self,
        expected_operation: PrivateWarehouseOperation,
        updated_operation: PrivateWarehouseOperation,
    ) -> None:
        expected_payload = canonical_bytes(expected_operation)
        row = self._connection.execute(
            "SELECT payload FROM private_warehouse_operations "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
            "AND operation_id = ?",
            (
                expected_operation.tenant_id,
                expected_operation.binding_id,
                expected_operation.binding_revision,
                expected_operation.operation_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError("private warehouse operation was not recorded")
        recorded_payload = bytes(row[0])
        if recorded_payload != expected_payload:
            raise WarehouseOperationConflictError(
                "warehouse operation durable state changed since the expected snapshot"
            )
        if expected_operation == updated_operation:
            return
        result = self._connection.execute(
            "UPDATE private_warehouse_operations SET status = ?, payload = ? "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
            "AND operation_id = ? AND status = ? AND payload = ?",
            (
                updated_operation.status.value,
                canonical_bytes(updated_operation),
                expected_operation.tenant_id,
                expected_operation.binding_id,
                expected_operation.binding_revision,
                expected_operation.operation_id,
                expected_operation.status.value,
                expected_payload,
            ),
        )
        if result.rowcount != 1:
            raise WarehouseOperationConflictError(
                "warehouse operation durable state changed during update"
            )

    def record_terminal_operation_failure(
        self,
        expected_operation: PrivateWarehouseOperation,
        failed_operation: PrivateWarehouseOperation,
        failed_binding: WarehouseBinding,
        *,
        expected_revision: int,
    ) -> None:
        _validate_terminal_operation_update(expected_operation, failed_operation)
        with (
            _translate_sqlite_errors("record terminal operation failure"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(
                expected_operation.tenant_id,
                expected_operation.binding_id,
            )
            _validate_terminal_binding_update(
                current,
                failed_binding,
                expected_operation,
                expected_revision=expected_revision,
            )
            row = self._connection.execute(
                "SELECT payload FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                "AND operation_id = ?",
                (
                    expected_operation.tenant_id,
                    expected_operation.binding_id,
                    expected_operation.binding_revision,
                    expected_operation.operation_id,
                ),
            ).fetchone()
            if row is None:
                raise WarehouseOperationConflictError(
                    "warehouse terminal operation was not recorded"
                )
            recorded = PrivateWarehouseOperation.model_validate_json(row[0])
            if recorded != expected_operation:
                raise WarehouseOperationConflictError(
                    "warehouse terminal operation durable state changed"
                )
            result = self._connection.execute(
                "UPDATE private_warehouse_operations SET status = ?, payload = ? "
                "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                "AND operation_id = ? AND status = ? AND payload = ?",
                (
                    failed_operation.status.value,
                    canonical_bytes(failed_operation),
                    expected_operation.tenant_id,
                    expected_operation.binding_id,
                    expected_operation.binding_revision,
                    expected_operation.operation_id,
                    expected_operation.status.value,
                    canonical_bytes(expected_operation),
                ),
            )
            if result.rowcount != 1:
                raise WarehouseOperationConflictError(
                    "warehouse terminal operation compare-and-swap failed"
                )
            self._insert_advanced_binding(failed_binding, expected_revision=expected_revision)

    def record_suspension(
        self,
        binding: WarehouseBinding,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None:
        _validate_operation_update(expected_operation, succeeded_operation)
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record warehouse suspension"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(binding.tenant_id, binding.binding_id)
            _validate_stable_binding_operation(
                current,
                binding,
                expected_operation,
                succeeded_operation,
                expected_revision=expected_revision,
            )
            self._update_operation_row(expected_operation, succeeded_operation)
            self._insert_advanced_binding(binding, expected_revision=expected_revision)

    def record_resources(self, resources: tuple[PrivateWarehouseResource, ...]) -> None:
        if not resources:
            return
        with (
            _translate_sqlite_errors("record warehouse resources"),
            _transaction(self._connection),
        ):
            for resource in resources:
                if resource.state_revision != 0:
                    raise WarehousePersistenceError(
                        "new warehouse resource requires initial state revision"
                    )
                self._assert_resource_parent(resource)
                existing = self._connection.execute(
                    "SELECT payload FROM private_warehouse_resources "
                    "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                    "AND operation_id = ? AND resource_id = ?",
                    _resource_identity(resource),
                ).fetchone()
                if existing is not None:
                    recorded = PrivateWarehouseResource.model_validate_json(existing[0])
                    if recorded == resource:
                        continue
                    raise WarehousePersistenceError(
                        "warehouse resource identity already has different durable state"
                    )
                self._connection.execute(
                    "INSERT INTO private_warehouse_resources "
                    "(tenant_id, binding_id, binding_revision, operation_id, resource_id, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (*_resource_identity(resource), canonical_bytes(resource)),
                )

    def save_resource(self, resource: PrivateWarehouseResource) -> PrivateWarehouseResource:
        return self.save_resources((resource,))[0]

    def save_resources(
        self, resources: tuple[PrivateWarehouseResource, ...]
    ) -> tuple[PrivateWarehouseResource, ...]:
        if not resources:
            return ()
        with (
            _translate_sqlite_errors("save warehouse resources"),
            _transaction(self._connection),
        ):
            return tuple(self._save_resource(resource) for resource in resources)

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]:
        if not expected_resources:
            return ()
        identities = tuple(_resource_identity(resource) for resource in expected_resources)
        if len(set(identities)) != len(identities):
            raise WarehousePersistenceError(
                "warehouse resource reopening requires unique resource identities"
            )
        recreatable_kinds = {
            WarehouseResourceKind.CREDENTIAL_FILE,
            WarehouseResourceKind.BACKUP_STAGING_FILE,
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
        if any(resource.resource_kind not in recreatable_kinds for resource in expected_resources):
            raise WarehousePersistenceError(
                "warehouse resource reopening is restricted to recreatable resources"
            )
        if any(
            resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
            for resource in expected_resources
        ):
            raise WarehousePersistenceError("retained warehouse resources cannot be recreated")
        reopened = tuple(
            PrivateWarehouseResource.model_validate(
                {
                    **resource.model_dump(),
                    "creation_state": WarehouseResourceCreationState.PLANNED,
                    "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
                    "cleanup_failure_classification": None,
                    "state_revision": resource.state_revision + 1,
                    "updated_at": reopened_at,
                }
            )
            for resource in expected_resources
        )
        with (
            _translate_sqlite_errors("reopen warehouse resources for recreation"),
            _transaction(self._connection),
        ):
            for expected, updated in zip(expected_resources, reopened, strict=True):
                self._assert_resource_parent(expected)
                row = self._connection.execute(
                    "SELECT payload FROM private_warehouse_resources "
                    "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                    "AND operation_id = ? AND resource_id = ?",
                    _resource_identity(expected),
                ).fetchone()
                if row is None:
                    raise KeyError("private warehouse resource was not recorded")
                recorded = PrivateWarehouseResource.model_validate_json(row[0])
                if recorded != expected:
                    raise WarehousePersistenceError(
                        "warehouse resource durable state changed before recreation"
                    )
                result = self._connection.execute(
                    "UPDATE private_warehouse_resources SET payload = ? "
                    "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                    "AND operation_id = ? AND resource_id = ? AND payload = ?",
                    (
                        canonical_bytes(updated),
                        *_resource_identity(expected),
                        canonical_bytes(expected),
                    ),
                )
                if result.rowcount != 1:
                    raise WarehousePersistenceError(
                        "warehouse resource recreation compare-and-swap failed"
                    )
        return reopened

    def _save_resource(self, resource: PrivateWarehouseResource) -> PrivateWarehouseResource:
        row = self._connection.execute(
            "SELECT payload FROM private_warehouse_resources "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
            "AND operation_id = ? AND resource_id = ?",
            _resource_identity(resource),
        ).fetchone()
        if row is None:
            raise KeyError("private warehouse resource was not recorded")
        recorded = PrivateWarehouseResource.model_validate_json(row[0])
        self._assert_resource_parent(resource, previous=recorded)
        _validate_resource_update(recorded, resource)
        if recorded == resource:
            return recorded
        if recorded.state_revision != resource.state_revision:
            raise WarehousePersistenceError(
                "warehouse resource durable state changed since the expected snapshot"
            )
        updated = resource.model_copy(update={"state_revision": resource.state_revision + 1})
        expected_payload = canonical_bytes(recorded)
        result = self._connection.execute(
            "UPDATE private_warehouse_resources SET payload = ? "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
            "AND operation_id = ? AND resource_id = ? AND payload = ?",
            (
                canonical_bytes(updated),
                *_resource_identity(resource),
                expected_payload,
            ),
        )
        if result.rowcount != 1:
            raise WarehousePersistenceError(
                "warehouse resource durable state changed during update"
            )
        return updated

    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]:
        if self.load(tenant_id, binding_id) is None:
            raise KeyError("warehouse binding was not found")
        with _translate_sqlite_errors("load warehouse resources"):
            return self._load_resources(tenant_id, binding_id)

    def abandon_draft(
        self,
        binding: WarehouseBinding,
        *,
        expected_revision: int,
    ) -> None:
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record warehouse draft abandonment"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(binding.tenant_id, binding.binding_id)
            if current.revision != expected_revision:
                raise StaleRevisionError("warehouse binding revision was not advanced")
            if current.lifecycle_state is not WarehouseBindingState.DRAFT:
                raise WarehouseValidationConflictError(
                    "warehouse draft abandonment requires draft binding state"
                )
            if current.provisioned_at is not None or binding.provisioned_at is not None:
                raise WarehouseValidationConflictError(
                    "warehouse provisioned binding cannot be abandoned as a draft"
                )
            expected_binding = WarehouseBinding.model_validate(
                {
                    **current.model_dump(),
                    "lifecycle_state": WarehouseBindingState.RETIRED,
                    "revision": current.revision + 1,
                    "updated_at": binding.updated_at,
                    "provisioned_at": None,
                }
            )
            if binding != expected_binding or binding.updated_at < current.updated_at:
                raise WarehouseValidationConflictError(
                    "warehouse draft abandonment binding does not match current draft"
                )
            resource = self._connection.execute(
                "SELECT 1 FROM private_warehouse_resources "
                "WHERE tenant_id = ? AND binding_id = ? LIMIT 1",
                (binding.tenant_id, binding.binding_id),
            ).fetchone()
            if resource is not None:
                raise WarehouseValidationConflictError(
                    "warehouse draft with a resource cannot be abandoned"
                )
            operation = self._connection.execute(
                "SELECT 1 FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? LIMIT 1",
                (binding.tenant_id, binding.binding_id),
            ).fetchone()
            if operation is not None:
                raise WarehouseValidationConflictError(
                    "warehouse draft with any operation, including a live operation, "
                    "cannot be abandoned"
                )
            self._insert_advanced_binding(binding, expected_revision=expected_revision)

    def record_initial_validation_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseValidationEvidence,
        restore: WarehouseRestoreVerification,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None:
        self._validate_initial_evidence(
            binding, evidence, restore, expected_revision=expected_revision
        )
        _validate_operation_update(expected_operation, succeeded_operation)
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record warehouse initial validation operation"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(binding.tenant_id, binding.binding_id)
            _validate_stable_binding_operation(
                current,
                binding,
                expected_operation,
                succeeded_operation,
                expected_revision=expected_revision,
            )
            self._update_operation_row(expected_operation, succeeded_operation)
            self._insert_advanced_binding(binding, expected_revision=expected_revision)
            self._insert_evidence(
                "warehouse_validation_evidence",
                "evidence_id",
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.binding_id,
                evidence.binding_revision,
                canonical_bytes(evidence),
            )
            self._insert_evidence(
                "warehouse_restore_verifications",
                "verification_id",
                restore.verification_id,
                restore.tenant_id,
                restore.binding_id,
                restore.binding_revision,
                canonical_bytes(restore),
            )

    def record_resume_validation_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseResumeValidationEvidence,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None:
        self._validate_evidence_ownership(
            binding,
            evidence.tenant_id,
            evidence.binding_id,
            evidence.binding_revision,
            evidence.evidence_id,
            expected_revision=expected_revision,
        )
        if evidence.engine_kind is not binding.engine_kind:
            raise WarehouseValidationConflictError(
                "warehouse resume evidence engine does not match binding"
            )
        _validate_operation_update(expected_operation, succeeded_operation)
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record warehouse resume validation operation"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(binding.tenant_id, binding.binding_id)
            _validate_stable_binding_operation(
                current,
                binding,
                expected_operation,
                succeeded_operation,
                expected_revision=expected_revision,
            )
            self._update_operation_row(expected_operation, succeeded_operation)
            self._insert_advanced_binding(binding, expected_revision=expected_revision)
            self._insert_evidence(
                "warehouse_resume_validation_evidence",
                "evidence_id",
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.binding_id,
                evidence.binding_revision,
                canonical_bytes(evidence),
            )

    def record_retirement_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None:
        self._validate_evidence_ownership(
            binding,
            evidence.tenant_id,
            evidence.binding_id,
            evidence.binding_revision,
            evidence.evidence_id,
            expected_revision=expected_revision,
        )
        _validate_operation_update(expected_operation, succeeded_operation)
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record warehouse retirement operation"),
            _transaction(self._connection),
        ):
            current = self._load_current_binding(binding.tenant_id, binding.binding_id)
            _validate_stable_binding_operation(
                current,
                binding,
                expected_operation,
                succeeded_operation,
                expected_revision=expected_revision,
            )
            resources = self._load_resources(binding.tenant_id, binding.binding_id)
            snapshot = canonical_retirement_resource_snapshot(resources)
            if snapshot.pending_resource_count:
                raise WarehouseValidationConflictError(
                    "warehouse retirement requires terminal resource cleanup dispositions"
                )
            mismatch = retirement_evidence_mismatch(evidence, snapshot)
            if mismatch is not None:
                raise WarehouseValidationConflictError(
                    f"warehouse retirement {mismatch} does not match resource snapshot"
                )
            self._update_operation_row(expected_operation, succeeded_operation)
            self._insert_advanced_binding(binding, expected_revision=expected_revision)
            self._insert_evidence(
                "warehouse_retirement_evidence",
                "evidence_id",
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.binding_id,
                evidence.binding_revision,
                canonical_bytes(evidence),
            )

    def record_retained_resource_deletion(
        self,
        evidence: WarehouseRetirementEvidence,
        *,
        expected_revision: int,
    ) -> None:
        with (
            _translate_validation_revision_conflict(),
            _translate_sqlite_errors("record retained warehouse resource deletion"),
            _transaction(self._connection),
        ):
            binding = self._load_current_binding(evidence.tenant_id, evidence.binding_id)
            if binding.lifecycle_state is not WarehouseBindingState.RETIRED:
                raise WarehouseValidationConflictError(
                    "retained warehouse resource deletion requires retired binding"
                )
            if (
                binding.revision != expected_revision
                or evidence.binding_revision != expected_revision
            ):
                raise WarehouseValidationConflictError(
                    "retained warehouse resource deletion revision does not match binding"
                )
            resources = self._load_resources(evidence.tenant_id, evidence.binding_id)
            snapshot = canonical_retirement_resource_snapshot(resources)
            if (
                snapshot.pending_resource_count
                or snapshot.retained_resource_count
                or snapshot.cleanup_failed_resource_count
            ):
                raise WarehouseValidationConflictError(
                    "retained warehouse resource deletion requires verified terminal cleanup"
                )
            mismatch = retirement_evidence_mismatch(evidence, snapshot)
            if mismatch is not None:
                raise WarehouseValidationConflictError(
                    "retained warehouse resource deletion "
                    f"{mismatch} does not match resource snapshot"
                )
            self._insert_evidence(
                "warehouse_retirement_evidence",
                "evidence_id",
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.binding_id,
                evidence.binding_revision,
                canonical_bytes(evidence),
            )

    def load_retirement_evidence(
        self,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> WarehouseRetirementEvidence:
        row = self._connection.execute(
            "SELECT payload FROM warehouse_retirement_evidence "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ?",
            (tenant_id, binding_id, binding_revision),
        ).fetchone()
        if row is None:
            raise KeyError("warehouse retirement evidence was not found")
        return WarehouseRetirementEvidence.model_validate_json(row[0])

    def _migrate(self) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'warehouse_schema_metadata'"
        ).fetchone()
        if exists is not None:
            row = self._connection.execute(
                "SELECT version, checksum FROM warehouse_schema_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise WarehousePersistenceError("warehouse schema metadata is missing")
            version = int(row[0])
            checksum = str(row[1])
            if version > _SCHEMA_VERSION:
                raise WarehousePersistenceError("warehouse database schema is newer than runtime")
            if version == _SCHEMA_VERSION and checksum == _SCHEMA_CHECKSUM:
                return
            if version == 1 and checksum == _SCHEMA_V1_CHECKSUM:
                self._migrate_v1_resources()
                return
            if version != _SCHEMA_VERSION or checksum != _SCHEMA_CHECKSUM:
                raise WarehousePersistenceError("warehouse database migration checksum mismatch")
        self._connection.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA_SQL}")
        try:
            self._connection.execute(
                "INSERT INTO warehouse_schema_metadata(singleton, version, checksum) "
                "VALUES (1, ?, ?)",
                (_SCHEMA_VERSION, _SCHEMA_CHECKSUM),
            )
            self._connection.commit()
        except BaseException:
            with suppress(BaseException):
                self._connection.rollback()
            raise

    def _migrate_v1_resources(self) -> None:
        with (
            _translate_sqlite_errors("migrate warehouse resource vocabulary"),
            _transaction(self._connection),
        ):
            rows = self._connection.execute(
                "SELECT tenant_id, binding_id, binding_revision, operation_id, "
                "resource_id, payload "
                "FROM private_warehouse_resources"
            ).fetchall()
            for row in rows:
                raw_payload = row[5]
                if not isinstance(raw_payload, (bytes, bytearray, memoryview)):
                    raise WarehousePersistenceError(
                        "warehouse database resource migration payload is invalid"
                    )
                payload = bytes(raw_payload)
                if _LEGACY_HBA_RESOURCE_KIND not in payload:
                    continue
                if payload.count(_LEGACY_HBA_RESOURCE_KIND) != 1:
                    raise WarehousePersistenceError(
                        "warehouse database resource migration payload is invalid"
                    )
                migrated_payload = payload.replace(
                    _LEGACY_HBA_RESOURCE_KIND,
                    _CREDENTIAL_FILE_RESOURCE_KIND,
                )
                try:
                    resource = PrivateWarehouseResource.model_validate_json(migrated_payload)
                except ValueError as error:
                    raise WarehousePersistenceError(
                        "warehouse database resource migration payload is invalid"
                    ) from error
                result = self._connection.execute(
                    "UPDATE private_warehouse_resources SET payload = ? "
                    "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
                    "AND operation_id = ? AND resource_id = ?",
                    (
                        canonical_bytes(resource),
                        str(row[0]),
                        str(row[1]),
                        int(row[2]),
                        str(row[3]),
                        str(row[4]),
                    ),
                )
                if result.rowcount != 1:
                    raise WarehousePersistenceError(
                        "warehouse database resource migration did not update one row"
                    )
            result = self._connection.execute(
                "UPDATE warehouse_schema_metadata SET version = ?, checksum = ? "
                "WHERE singleton = 1 AND version = 1 AND checksum = ?",
                (_SCHEMA_VERSION, _SCHEMA_CHECKSUM, _SCHEMA_V1_CHECKSUM),
            )
            if result.rowcount != 1:
                raise WarehousePersistenceError(
                    "warehouse database resource migration metadata changed"
                )

    def _next_sequence(self, table: str, tenant_id: str) -> int:
        _require_nonempty(tenant_id, "tenant_id")
        with _translate_sqlite_errors("allocate warehouse sequence"):
            try:
                row = self._connection.execute(
                    f"INSERT INTO {table} (tenant_id, next_sequence) VALUES (?, 2) "
                    f"ON CONFLICT(tenant_id) DO UPDATE "
                    f"SET next_sequence = {table}.next_sequence + 1 "
                    "RETURNING next_sequence - 1",
                    (tenant_id,),
                ).fetchone()
                if row is None:
                    raise WarehousePersistenceError(
                        "warehouse sequence allocation did not return a sequence"
                    )
                self._connection.commit()
                return int(row[0])
            except BaseException:
                with suppress(BaseException):
                    self._connection.rollback()
                raise

    def _insert_binding(self, binding: WarehouseBinding) -> None:
        if binding.revision == 1:
            result = self._connection.execute(
                "INSERT INTO warehouse_bindings "
                "(binding_id, revision, tenant_id, payload) "
                "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
                "SELECT 1 FROM warehouse_bindings "
                "WHERE tenant_id = ? AND binding_id = ? AND revision = ?) "
                "ON CONFLICT(binding_id, revision) DO NOTHING",
                (
                    binding.binding_id,
                    binding.revision,
                    binding.tenant_id,
                    canonical_bytes(binding),
                    binding.tenant_id,
                    binding.binding_id,
                    binding.revision,
                ),
            )
        else:
            result = self._connection.execute(
                "INSERT INTO warehouse_bindings "
                "(binding_id, revision, tenant_id, payload) "
                "SELECT ?, ?, ?, ? WHERE EXISTS ("
                "SELECT 1 FROM warehouse_bindings "
                "WHERE tenant_id = ? AND binding_id = ? AND revision = ?"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM warehouse_bindings "
                "WHERE tenant_id = ? AND binding_id = ? AND revision = ?"
                ") ON CONFLICT(binding_id, revision) DO NOTHING",
                (
                    binding.binding_id,
                    binding.revision,
                    binding.tenant_id,
                    canonical_bytes(binding),
                    binding.tenant_id,
                    binding.binding_id,
                    binding.revision - 1,
                    binding.tenant_id,
                    binding.binding_id,
                    binding.revision,
                ),
            )
        if result.rowcount != 1:
            raise StaleRevisionError("warehouse binding revision was not advanced")

    def _insert_advanced_binding(
        self, binding: WarehouseBinding, *, expected_revision: int
    ) -> None:
        if binding.revision != expected_revision + 1:
            raise StaleRevisionError("warehouse binding revision was not advanced")
        result = self._connection.execute(
            "INSERT INTO warehouse_bindings "
            "(binding_id, revision, tenant_id, payload) "
            "SELECT ?, ?, ?, ? WHERE ("
            "SELECT MAX(revision) FROM warehouse_bindings "
            "WHERE tenant_id = ? AND binding_id = ?"
            ") = ? AND NOT EXISTS ("
            "SELECT 1 FROM warehouse_bindings "
            "WHERE tenant_id = ? AND binding_id = ? AND revision = ?"
            ") ON CONFLICT(binding_id, revision) DO NOTHING",
            (
                binding.binding_id,
                binding.revision,
                binding.tenant_id,
                canonical_bytes(binding),
                binding.tenant_id,
                binding.binding_id,
                expected_revision,
                binding.tenant_id,
                binding.binding_id,
                binding.revision,
            ),
        )
        if result.rowcount != 1:
            raise StaleRevisionError("warehouse binding revision was not advanced")

    def _load_current_binding(self, tenant_id: str, binding_id: str) -> WarehouseBinding:
        row = self._connection.execute(
            "SELECT payload FROM warehouse_bindings "
            "WHERE tenant_id = ? AND binding_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, binding_id),
        ).fetchone()
        if row is None:
            raise KeyError("warehouse binding was not found")
        return WarehouseBinding.model_validate_json(row[0])

    def _load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM private_warehouse_resources "
            "WHERE tenant_id = ? AND binding_id = ? "
            "ORDER BY binding_revision, operation_id, resource_id",
            (tenant_id, binding_id),
        ).fetchall()
        return tuple(PrivateWarehouseResource.model_validate_json(row[0]) for row in rows)

    def _assert_resource_parent(
        self,
        resource: PrivateWarehouseResource,
        *,
        previous: PrivateWarehouseResource | None = None,
    ) -> None:
        current = self._load_current_binding(resource.tenant_id, resource.binding_id)
        if current.lifecycle_state is WarehouseBindingState.RETIRED and not (
            previous is not None
            and (
                resource == previous
                or (
                    (
                        previous.cleanup_status,
                        resource.cleanup_status,
                    )
                    in {
                        (
                            WarehouseResourceCleanupStatus.RETAINED,
                            WarehouseResourceCleanupStatus.COMPLETE,
                        ),
                        (
                            WarehouseResourceCleanupStatus.RETAINED,
                            WarehouseResourceCleanupStatus.FAILED,
                        ),
                        (
                            WarehouseResourceCleanupStatus.FAILED,
                            WarehouseResourceCleanupStatus.COMPLETE,
                        ),
                    }
                    and resource
                    == previous.model_copy(
                        update={
                            "cleanup_status": resource.cleanup_status,
                            "cleanup_failure_classification": (
                                resource.cleanup_failure_classification
                            ),
                            "updated_at": resource.updated_at,
                        }
                    )
                    and resource.updated_at >= previous.updated_at
                )
            )
        ):
            raise WarehousePersistenceError(
                "warehouse resource ledger cannot change after binding is retired"
            )
        binding = self._connection.execute(
            "SELECT 1 FROM warehouse_bindings "
            "WHERE tenant_id = ? AND binding_id = ? AND revision = ?",
            (resource.tenant_id, resource.binding_id, resource.binding_revision),
        ).fetchone()
        if binding is None:
            raise WarehousePersistenceError("warehouse resource binding revision was not recorded")
        operation = self._connection.execute(
            "SELECT 1 FROM private_warehouse_operations "
            "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
            "AND operation_id = ?",
            (
                resource.tenant_id,
                resource.binding_id,
                resource.binding_revision,
                resource.operation_id,
            ),
        ).fetchone()
        if operation is None:
            raise WarehousePersistenceError("warehouse resource parent operation was not recorded")

    def _validate_initial_evidence(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseValidationEvidence,
        restore: WarehouseRestoreVerification,
        *,
        expected_revision: int,
    ) -> None:
        self._validate_evidence_ownership(
            binding,
            evidence.tenant_id,
            evidence.binding_id,
            evidence.binding_revision,
            evidence.evidence_id,
            expected_revision=expected_revision,
        )
        self._validate_evidence_ownership(
            binding,
            restore.tenant_id,
            restore.binding_id,
            restore.binding_revision,
            restore.verification_id,
            expected_revision=expected_revision,
        )
        if evidence.engine_kind is not binding.engine_kind:
            raise WarehouseValidationConflictError(
                "warehouse validation evidence engine does not match binding"
            )
        if restore.engine_kind is not binding.engine_kind:
            raise WarehouseValidationConflictError(
                "warehouse restore verification engine does not match binding"
            )
        if evidence.backup_artifact_digest != restore.source_backup_artifact_digest:
            raise WarehouseValidationConflictError(
                "warehouse validation backup digest does not match restore"
            )
        if evidence.restore_verification_digest != digest(restore):
            raise WarehouseValidationConflictError(
                "warehouse validation restore digest does not match verification"
            )
        if evidence.principal_profile_digest != restore.principal_profile_digest:
            raise WarehouseValidationConflictError(
                "warehouse validation principal profile digest does not match restore"
            )

    @staticmethod
    def _validate_evidence_ownership(
        binding: WarehouseBinding,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
        evidence_id: str,
        *,
        expected_revision: int,
    ) -> None:
        if not evidence_id.strip():
            raise WarehouseValidationConflictError("warehouse evidence_id must not be empty")
        if tenant_id != binding.tenant_id:
            raise WarehouseValidationConflictError(
                "warehouse evidence tenant does not match binding"
            )
        if binding_id != binding.binding_id:
            raise WarehouseValidationConflictError(
                "warehouse evidence binding does not match binding"
            )
        if binding_revision != expected_revision:
            raise WarehouseValidationConflictError(
                "warehouse evidence revision does not match binding"
            )
        if binding.revision != expected_revision + 1:
            raise WarehouseValidationConflictError(
                "warehouse admitted binding revision was not advanced"
            )

    def _insert_evidence(
        self,
        table: str,
        identifier_column: str,
        identifier: str,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
        payload: bytes,
    ) -> None:
        existing = self._connection.execute(
            f"SELECT 1 FROM {table} WHERE tenant_id = ? "
            f"AND ({identifier_column} = ? OR (binding_id = ? AND binding_revision = ?))",
            (tenant_id, identifier, binding_id, binding_revision),
        ).fetchone()
        if existing is not None:
            raise WarehouseValidationConflictError(
                "warehouse evidence identity or binding revision was already recorded"
            )
        self._connection.execute(
            f"INSERT INTO {table} "
            f"(tenant_id, binding_id, binding_revision, {identifier_column}, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, binding_id, binding_revision, identifier, payload),
        )


@contextmanager
def _transaction(connection: _Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except BaseException:
        with suppress(BaseException):
            connection.rollback()
        raise


@contextmanager
def _translate_validation_revision_conflict() -> Iterator[None]:
    try:
        yield
    except StaleRevisionError as error:
        raise WarehouseValidationConflictError(
            "warehouse evidence admission revision conflict"
        ) from error


@contextmanager
def _translate_sqlite_errors(operation: str) -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise WarehousePersistenceError(
            f"warehouse persistence failed during {operation}"
        ) from error


def _require_nonempty(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _operation_values(operation: PrivateWarehouseOperation) -> tuple[object, ...]:
    return (
        operation.tenant_id,
        operation.binding_id,
        operation.binding_revision,
        operation.operation_id,
        operation.status.value,
        canonical_bytes(operation),
    )


def _resource_identity(resource: PrivateWarehouseResource) -> tuple[object, ...]:
    return (
        resource.tenant_id,
        resource.binding_id,
        resource.binding_revision,
        resource.operation_id,
        resource.resource_id,
    )


def _operation_claimed_identity(operation: PrivateWarehouseOperation) -> tuple[object, ...]:
    return (
        operation.tenant_id,
        operation.binding_id,
        operation.binding_revision,
        operation.operation_id,
        operation.operation_kind,
        operation.engine_kind,
        operation.started_at,
    )


def _validate_operation_update(
    expected: PrivateWarehouseOperation,
    updated: PrivateWarehouseOperation,
) -> None:
    _validate_operation_terminal_phase(expected)
    _validate_operation_terminal_phase(updated)
    if _operation_claimed_identity(expected) != _operation_claimed_identity(updated):
        raise WarehouseOperationConflictError(
            "warehouse operation update changed its claimed identity"
        )
    if expected == updated:
        return
    if expected.status not in _LIVE_OPERATION_STATUSES:
        raise WarehouseOperationConflictError(
            "warehouse operation update cannot reopen terminal state"
        )
    if (expected.status, updated.status) not in _ALLOWED_OPERATION_STATUS_TRANSITIONS:
        raise WarehouseOperationConflictError(
            "warehouse operation update has invalid status transition"
        )
    valid_phases = _OPERATION_PHASES[expected.operation_kind]
    if expected.phase not in valid_phases or updated.phase not in valid_phases:
        raise WarehouseOperationConflictError(
            "warehouse operation update has invalid phase for its kind"
        )
    if (
        expected.phase != updated.phase
        and (expected.phase, updated.phase)
        not in _ALLOWED_OPERATION_PHASE_TRANSITIONS[expected.operation_kind]
    ):
        raise WarehouseOperationConflictError(
            "warehouse operation update has invalid phase transition"
        )
    if updated.updated_at < expected.updated_at:
        raise WarehouseOperationConflictError("warehouse operation update timestamp cannot regress")
    if expected.provider_resource_handle != updated.provider_resource_handle:
        valid_provider_handle_assignment = (
            expected.operation_kind is WarehouseOperationKind.PROVISION
            and expected.provider_resource_handle is None
            and updated.provider_resource_handle is not None
            and updated.status is WarehouseOperationStatus.RUNNING
            and expected.phase
            in {
                WarehouseOperationPhase.CLAIMED,
                WarehouseOperationPhase.RESOURCES_PLANNED,
            }
            and updated.phase is WarehouseOperationPhase.PROVIDER_CREATED
        )
        if not valid_provider_handle_assignment:
            raise WarehouseOperationConflictError(
                "warehouse operation update changed its provider resource handle"
            )
    if updated.status is WarehouseOperationStatus.FAILED:
        if (
            updated.failure_classification not in _TERMINAL_FAILURE_CLASSIFICATIONS
            or updated.phase is not expected.phase
        ):
            raise WarehouseOperationConflictError(
                "warehouse operation update has invalid failure transition"
            )
    elif updated.failure_classification is not None:
        raise WarehouseOperationConflictError(
            "warehouse operation update set failure classification on live or successful state"
        )
    if (
        updated.status is WarehouseOperationStatus.RECONCILING
        and updated.phase is not expected.phase
    ):
        raise WarehouseOperationConflictError(
            "warehouse operation update cannot change phase while reconciling"
        )


def _validate_operation_terminal_phase(operation: PrivateWarehouseOperation) -> None:
    if (
        operation.status is WarehouseOperationStatus.SUCCEEDED
        and operation.phase is not _TERMINAL_OPERATION_PHASES[operation.operation_kind]
    ):
        raise WarehouseOperationConflictError(
            "warehouse operation success requires its operation-kind terminal phase"
        )


def _validate_terminal_operation_update(
    expected: PrivateWarehouseOperation,
    failed: PrivateWarehouseOperation,
) -> None:
    if expected.status not in _LIVE_OPERATION_STATUSES:
        raise WarehouseOperationConflictError(
            "warehouse terminal failure requires a live operation"
        )
    if (
        failed.status is not WarehouseOperationStatus.FAILED
        or failed.failure_classification not in _TERMINAL_FAILURE_CLASSIFICATIONS
    ):
        raise WarehouseOperationConflictError(
            "warehouse terminal operation classification is invalid"
        )
    expected_failed = PrivateWarehouseOperation.model_validate(
        {
            **expected.model_dump(),
            "status": WarehouseOperationStatus.FAILED,
            "failure_classification": failed.failure_classification,
            "updated_at": failed.updated_at,
        }
    )
    if failed != expected_failed or failed.updated_at < expected.updated_at:
        raise WarehouseOperationConflictError(
            "warehouse terminal operation update changed durable state"
        )


def _validate_terminal_binding_update(
    current: WarehouseBinding,
    failed: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    *,
    expected_revision: int,
) -> None:
    expected_operation_revision = {
        WarehouseBindingState.PROVISIONING: expected_revision,
        WarehouseBindingState.VALIDATING: expected_revision - 1,
    }.get(current.lifecycle_state)
    expected_failed = WarehouseBinding.model_validate(
        {
            **current.model_dump(),
            "lifecycle_state": WarehouseBindingState.FAILED,
            "revision": expected_revision + 1,
            "updated_at": failed.updated_at,
        }
    )
    if (
        current.revision != expected_revision
        or expected_operation_revision is None
        or operation.binding_revision != expected_operation_revision
        or operation.tenant_id != current.tenant_id
        or operation.binding_id != current.binding_id
        or operation.engine_kind is not current.engine_kind
        or failed != expected_failed
        or failed.updated_at < current.updated_at
    ):
        raise WarehouseOperationConflictError(
            "warehouse terminal binding revision or operation is stale"
        )


def _validate_stable_binding_operation(
    current: WarehouseBinding,
    stable: WarehouseBinding,
    expected_operation: PrivateWarehouseOperation,
    succeeded_operation: PrivateWarehouseOperation,
    *,
    expected_revision: int,
) -> None:
    if current.revision != expected_revision:
        raise StaleRevisionError("warehouse binding revision was not advanced")
    admission = {
        (WarehouseBindingState.VALIDATING, WarehouseBindingState.READY): (
            WarehouseOperationKind.PROVISION,
            expected_revision - 1,
        ),
        (WarehouseBindingState.READY, WarehouseBindingState.SUSPENDED): (
            WarehouseOperationKind.SUSPEND,
            expected_revision,
        ),
        (WarehouseBindingState.SUSPENDED, WarehouseBindingState.READY): (
            WarehouseOperationKind.RESUME,
            expected_revision,
        ),
        (WarehouseBindingState.RETIRING, WarehouseBindingState.RETIRED): (
            WarehouseOperationKind.RETIRE,
            expected_revision,
        ),
        (WarehouseBindingState.FAILED, WarehouseBindingState.RETIRED): (
            WarehouseOperationKind.RETIRE,
            expected_revision,
        ),
    }.get((current.lifecycle_state, stable.lifecycle_state))
    if admission is None:
        raise WarehouseOperationConflictError(
            "warehouse stable admission has an invalid binding transition"
        )
    operation_kind, operation_revision = admission
    expected_provisioned_at = (
        stable.updated_at
        if current.lifecycle_state is WarehouseBindingState.VALIDATING
        else current.provisioned_at
    )
    expected_stable = WarehouseBinding.model_validate(
        {
            **current.model_dump(),
            "lifecycle_state": stable.lifecycle_state,
            "revision": expected_revision + 1,
            "updated_at": stable.updated_at,
            "provisioned_at": expected_provisioned_at,
        }
    )
    if (
        stable != expected_stable
        or stable.updated_at < current.updated_at
        or expected_operation.tenant_id != current.tenant_id
        or expected_operation.binding_id != current.binding_id
        or expected_operation.binding_revision != operation_revision
        or expected_operation.operation_kind is not operation_kind
        or expected_operation.engine_kind is not current.engine_kind
        or succeeded_operation.status is not WarehouseOperationStatus.SUCCEEDED
        or succeeded_operation.phase is not _TERMINAL_OPERATION_PHASES[operation_kind]
    ):
        raise WarehouseOperationConflictError(
            "warehouse stable admission binding revision or operation is stale"
        )


def _resource_planned_identity(resource: PrivateWarehouseResource) -> tuple[object, ...]:
    return (
        resource.tenant_id,
        resource.binding_id,
        resource.binding_revision,
        resource.operation_id,
        resource.resource_id,
        resource.resource_kind,
        resource.provider_resource_handle,
        resource.parent_resource_handle,
        resource.retention_deadline,
        resource.created_at,
    )


def _validate_resource_update(
    expected: PrivateWarehouseResource,
    updated: PrivateWarehouseResource,
) -> None:
    if _resource_planned_identity(expected) != _resource_planned_identity(updated):
        raise WarehousePersistenceError("warehouse resource update changed its planned identity")
    if updated.updated_at < expected.updated_at:
        raise WarehousePersistenceError("warehouse resource update timestamp cannot regress")
    if (
        expected.creation_state is not updated.creation_state
        and (expected.creation_state, updated.creation_state)
        not in _ALLOWED_RESOURCE_CREATION_TRANSITIONS
    ):
        raise WarehousePersistenceError("warehouse resource update has invalid creation transition")
    if (
        expected.cleanup_status is not updated.cleanup_status
        and (expected.cleanup_status, updated.cleanup_status)
        not in _ALLOWED_RESOURCE_CLEANUP_TRANSITIONS
    ):
        raise WarehousePersistenceError("warehouse resource update has invalid cleanup transition")
