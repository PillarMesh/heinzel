from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import TypeAdapter

from .models import CatalogBinding, CatalogBindingState, CatalogValidationEvidence

type CatalogResourceCreationState = Literal["planned", "created", "validated", "failed"]
type CatalogResourceCleanupStatus = Literal[
    "not_started", "in_progress", "complete", "failed", "unknown"
]
type CatalogResourceCleanupFailureClassification = Literal[
    "transient",
    "throttled",
    "authentication",
    "authorization",
    "conflict",
    "invalid_request",
    "permanent",
    "unknown",
]

_CREATION_STATE_ADAPTER: TypeAdapter[CatalogResourceCreationState] = TypeAdapter(
    CatalogResourceCreationState
)
_CLEANUP_STATUS_ADAPTER: TypeAdapter[CatalogResourceCleanupStatus] = TypeAdapter(
    CatalogResourceCleanupStatus
)
_CLEANUP_FAILURE_CLASSIFICATION_ADAPTER: TypeAdapter[
    CatalogResourceCleanupFailureClassification
] = TypeAdapter(CatalogResourceCleanupFailureClassification)
_RESOURCE_CREATION_STATES: frozenset[str] = frozenset({"planned", "created", "validated", "failed"})
_RESOURCE_CLEANUP_STATUSES: frozenset[str] = frozenset(
    {"not_started", "in_progress", "complete", "failed", "unknown"}
)
_RESOURCE_CLEANUP_FAILURE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "transient",
        "throttled",
        "authentication",
        "authorization",
        "conflict",
        "invalid_request",
        "permanent",
        "unknown",
    }
)


class CatalogResourceKind(StrEnum):
    COMPOSE_PROJECT = "compose_project"
    COMPOSE_CONTAINER = "compose_container"
    COMPOSE_VOLUME = "compose_volume"
    COMPOSE_NETWORK = "compose_network"
    CATALOG_SERVICE = "catalog_service"
    CATALOG_DATABASE = "catalog_database"
    SEARCH_INDEX = "search_index"
    SERVICE_ACCOUNT = "service_account"
    TENANT_NAMESPACE = "tenant_namespace"
    CATALOG_GLOSSARY_TERM = "catalog_glossary_term"
    CATALOG_CLASSIFICATION = "catalog_classification"
    CATALOG_POLICY = "catalog_policy"
    CATALOG_ROLE = "catalog_role"
    CATALOG_TAG = "catalog_tag"
    CATALOG_LINEAGE = "catalog_lineage"
    BACKUP_ARTIFACT = "backup_artifact"


@dataclass(frozen=True, slots=True)
class PrivateCatalogResource:
    tenant_id: str
    binding_id: str
    resource_id: str
    resource_kind: CatalogResourceKind
    provider_ref: str
    creation_state: CatalogResourceCreationState
    retention_deadline: datetime
    cleanup_status: CatalogResourceCleanupStatus
    created_at: datetime
    cleaned_at: datetime | None
    cleanup_failure_classification: CatalogResourceCleanupFailureClassification | None = None


@dataclass(frozen=True, slots=True)
class PrivateCatalogOperation:
    tenant_id: str
    binding_id: str
    operation_id: str
    resource_handle: str
    project_name: str
    secret_reference: str
    created_at: datetime


class CatalogRepository(Protocol):
    def create_draft(self, tenant_id: str, created_at: datetime) -> CatalogBinding: ...

    def append_transition(self, binding: CatalogBinding, *, expected_revision: int) -> None: ...

    def record_validation(
        self,
        binding: CatalogBinding,
        evidence: CatalogValidationEvidence,
        *,
        expected_revision: int,
    ) -> None: ...

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding: ...

    def record_operation(
        self,
        operation: PrivateCatalogOperation,
        *,
        resources: tuple[PrivateCatalogResource, ...],
    ) -> None: ...

    def claim_operation(self, tenant_id: str, binding_id: str, operation_id: str) -> bool: ...

    def load_operation(
        self, tenant_id: str, binding_id: str, operation_id: str
    ) -> PrivateCatalogOperation: ...

    def load_operation_by_handle(self, resource_handle: str) -> PrivateCatalogOperation: ...

    def record_resource(self, resource: PrivateCatalogResource) -> None: ...

    def load_resource(self, tenant_id: str, resource_id: str) -> PrivateCatalogResource: ...

    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateCatalogResource, ...]: ...

    def mark_resource_created(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        provider_ref: str | None = None,
    ) -> PrivateCatalogResource: ...

    def begin_cleanup(self, tenant_id: str, resource_id: str) -> PrivateCatalogResource: ...

    def complete_cleanup(
        self, tenant_id: str, resource_id: str, *, cleaned_at: datetime
    ) -> PrivateCatalogResource: ...

    def fail_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        status: Literal["failed", "unknown"],
        failure_classification: CatalogResourceCleanupFailureClassification,
    ) -> PrivateCatalogResource: ...

    def load_validation(self, tenant_id: str, binding_id: str) -> CatalogValidationEvidence: ...


class StaleRevisionError(Exception):
    pass


class CatalogValidationConflictError(Exception):
    pass


class CatalogOperationConflictError(Exception):
    pass


class CatalogPersistenceError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"catalog persistence failed during {operation}")


class SQLiteCatalogRepository:
    def __init__(
        self,
        database_path: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Open a database, or borrow a connection the composer already owns.

        The console runs its backend on a threadpool, and a connection carries
        SQLite's thread affinity, so a repository that opens its own here is bound to
        whichever thread constructed it and fails on the first request from a worker.
        Only the composer can decide that lifecycle, so it must be able to supply one.

        A borrowed connection is never closed by `close()`: the owner closes it.
        """
        if (database_path is None) == (connection is None):
            raise ValueError("supply exactly one of database_path or connection")
        self._owns_connection = connection is None
        with _translate_sqlite_errors("initialize catalog repository"):
            if connection is None:
                assert database_path is not None
                connection = sqlite3.connect(database_path)
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_sequences ("
                "tenant_id TEXT PRIMARY KEY, "
                "next_sequence INTEGER NOT NULL"
                ")"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_bindings ("
                "tenant_id TEXT NOT NULL, "
                "binding_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, "
                "payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, binding_id, revision)"
                ")"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS catalog_validation_evidence ("
                "tenant_id TEXT NOT NULL, "
                "evidence_id TEXT NOT NULL, "
                "binding_id TEXT NOT NULL, "
                "binding_revision INTEGER NOT NULL, "
                "payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, evidence_id), "
                "UNIQUE (tenant_id, binding_id, binding_revision), "
                "FOREIGN KEY (tenant_id, binding_id, binding_revision) "
                "REFERENCES catalog_bindings (tenant_id, binding_id, revision)"
                ")"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS private_catalog_resources ("
                "tenant_id TEXT NOT NULL, "
                "binding_id TEXT NOT NULL, "
                "resource_id TEXT NOT NULL, "
                "resource_kind TEXT NOT NULL, "
                "provider_ref TEXT NOT NULL, "
                "creation_state TEXT NOT NULL, "
                "retention_deadline TEXT NOT NULL, "
                "cleanup_status TEXT NOT NULL, "
                "cleanup_failure_classification TEXT, "
                "created_at TEXT NOT NULL, "
                "cleaned_at TEXT, "
                "PRIMARY KEY (tenant_id, resource_id)"
                ")"
            )
            resource_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info('private_catalog_resources')"
                ).fetchall()
            }
            if "cleanup_failure_classification" not in resource_columns:
                self._connection.execute(
                    "ALTER TABLE private_catalog_resources "
                    "ADD COLUMN cleanup_failure_classification TEXT"
                )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS private_catalog_operation_claims ("
                "tenant_id TEXT NOT NULL, "
                "binding_id TEXT NOT NULL, "
                "operation_id TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, binding_id), "
                "UNIQUE (tenant_id, binding_id, operation_id)"
                ")"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS private_catalog_operations ("
                "tenant_id TEXT NOT NULL, "
                "binding_id TEXT NOT NULL, "
                "operation_id TEXT NOT NULL, "
                "resource_handle TEXT NOT NULL UNIQUE, "
                "project_name TEXT NOT NULL, "
                "secret_reference TEXT NOT NULL, "
                "created_at TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, binding_id, operation_id), "
                "UNIQUE (tenant_id, binding_id)"
                ")"
            )
            self._connection.execute(
                "INSERT INTO private_catalog_operation_claims "
                "(tenant_id, binding_id, operation_id) "
                "SELECT tenant_id, binding_id, operation_id FROM private_catalog_operations "
                "WHERE 1 "
                "ON CONFLICT(tenant_id, binding_id) DO NOTHING"
            )
            self._connection.commit()

    def close(self) -> None:
        if not self._owns_connection:
            return
        self._connection.close()

    def create_draft(self, tenant_id: str, created_at: datetime) -> CatalogBinding:
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        with (
            _translate_sqlite_errors("create catalog draft"),
            _transaction(self._connection),
        ):
            row = self._connection.execute(
                "INSERT INTO catalog_sequences (tenant_id, next_sequence) VALUES (?, 2) "
                "ON CONFLICT(tenant_id) DO UPDATE "
                "SET next_sequence = catalog_sequences.next_sequence + 1 "
                "RETURNING next_sequence - 1",
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("catalog sequence allocation did not return a sequence")
            sequence = int(row[0])
            binding = CatalogBinding(
                binding_id=_binding_id(tenant_id, sequence),
                tenant_id=tenant_id,
                capability_profile_digest=digest(
                    {
                        "deployment_mode": "pillarmesh_managed",
                        "provider_kind": "openmetadata",
                    }
                ),
                lifecycle_state=CatalogBindingState.DRAFT,
                revision=1,
                created_at=created_at,
                updated_at=created_at,
            )
            self._connection.execute(
                "INSERT INTO catalog_bindings (tenant_id, binding_id, revision, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    binding.tenant_id,
                    binding.binding_id,
                    binding.revision,
                    canonical_bytes(binding),
                ),
            )
        return binding

    def append_transition(self, binding: CatalogBinding, *, expected_revision: int) -> None:
        with (
            _translate_sqlite_errors("append catalog binding revision"),
            _transaction(self._connection),
        ):
            self._insert_advanced_binding(binding, expected_revision=expected_revision)

    def record_validation(
        self,
        binding: CatalogBinding,
        evidence: CatalogValidationEvidence,
        *,
        expected_revision: int,
    ) -> None:
        try:
            with _transaction(self._connection):
                self._insert_advanced_binding(binding, expected_revision=expected_revision)
                result = self._connection.execute(
                    "INSERT INTO catalog_validation_evidence "
                    "(tenant_id, evidence_id, binding_id, binding_revision, payload) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (
                        evidence.tenant_id,
                        evidence.evidence_id,
                        evidence.binding_id,
                        evidence.binding_revision,
                        canonical_bytes(evidence),
                    ),
                )
                if result.rowcount != 1:
                    raise CatalogValidationConflictError(
                        "catalog validation evidence was not recorded"
                    )
                self._connection.execute(
                    "UPDATE private_catalog_resources SET creation_state = 'validated' "
                    "WHERE tenant_id = ? AND binding_id = ? AND creation_state = 'created'",
                    (binding.tenant_id, binding.binding_id),
                )
        except sqlite3.Error as error:
            raise CatalogPersistenceError(operation="record catalog validation") from error

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        with _translate_sqlite_errors("load catalog binding"):
            row = self._connection.execute(
                "SELECT payload FROM catalog_bindings "
                "WHERE tenant_id = ? AND binding_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (tenant_id, binding_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"binding {binding_id} belongs to another tenant")
        return CatalogBinding.model_validate_json(row[0])

    def record_operation(
        self,
        operation: PrivateCatalogOperation,
        *,
        resources: tuple[PrivateCatalogResource, ...],
    ) -> None:
        _validate_operation(operation)
        for resource in resources:
            _validate_resource(resource)
            if (
                resource.tenant_id != operation.tenant_id
                or resource.binding_id != operation.binding_id
            ):
                raise ValueError(
                    "private operation resources must belong to its tenant and binding"
                )
        with (
            _translate_sqlite_errors("record private catalog operation"),
            _transaction(self._connection),
        ):
            binding = self._connection.execute(
                "SELECT 1 FROM catalog_bindings WHERE tenant_id = ? AND binding_id = ? LIMIT 1",
                (operation.tenant_id, operation.binding_id),
            ).fetchone()
            if binding is None:
                raise KeyError(f"binding {operation.binding_id} belongs to another tenant")
            self._connection.execute(
                "INSERT INTO private_catalog_operation_claims "
                "(tenant_id, binding_id, operation_id) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, binding_id) DO NOTHING",
                (operation.tenant_id, operation.binding_id, operation.operation_id),
            )
            self._assert_operation_claim(
                operation.tenant_id,
                operation.binding_id,
                operation.operation_id,
            )
            self._connection.execute(
                "INSERT INTO private_catalog_operations "
                "(tenant_id, binding_id, operation_id, resource_handle, project_name, "
                "secret_reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                _operation_values(operation),
            )
            for resource in resources:
                self._connection.execute(
                    "INSERT INTO private_catalog_resources "
                    "(tenant_id, binding_id, resource_id, resource_kind, provider_ref, "
                    "creation_state, retention_deadline, cleanup_status, "
                    "cleanup_failure_classification, created_at, cleaned_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _resource_values(resource),
                )

    def claim_operation(self, tenant_id: str, binding_id: str, operation_id: str) -> bool:
        if not tenant_id or not binding_id or not operation_id:
            raise ValueError("private catalog operation identifiers must not be empty")
        with (
            _translate_sqlite_errors("claim private catalog operation"),
            _transaction(self._connection),
        ):
            binding = self._connection.execute(
                "SELECT 1 FROM catalog_bindings WHERE tenant_id = ? AND binding_id = ? LIMIT 1",
                (tenant_id, binding_id),
            ).fetchone()
            if binding is None:
                raise KeyError(f"binding {binding_id} belongs to another tenant")
            result = self._connection.execute(
                "INSERT INTO private_catalog_operation_claims "
                "(tenant_id, binding_id, operation_id) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, binding_id) DO NOTHING",
                (tenant_id, binding_id, operation_id),
            )
            self._assert_operation_claim(tenant_id, binding_id, operation_id)
            return result.rowcount == 1

    def load_operation(
        self, tenant_id: str, binding_id: str, operation_id: str
    ) -> PrivateCatalogOperation:
        with _translate_sqlite_errors("load private catalog operation"):
            row = self._connection.execute(
                "SELECT tenant_id, binding_id, operation_id, resource_handle, project_name, "
                "secret_reference, created_at FROM private_catalog_operations "
                "WHERE tenant_id = ? AND binding_id = ? AND operation_id = ?",
                (tenant_id, binding_id, operation_id),
            ).fetchone()
        if row is None:
            raise KeyError("private catalog operation was not recorded")
        return _operation_from_row(row)

    def load_operation_by_handle(self, resource_handle: str) -> PrivateCatalogOperation:
        with _translate_sqlite_errors("load private catalog operation by handle"):
            row = self._connection.execute(
                "SELECT tenant_id, binding_id, operation_id, resource_handle, project_name, "
                "secret_reference, created_at FROM private_catalog_operations "
                "WHERE resource_handle = ?",
                (resource_handle,),
            ).fetchone()
        if row is None:
            raise KeyError("private catalog operation was not recorded")
        return _operation_from_row(row)

    def record_resource(self, resource: PrivateCatalogResource) -> None:
        _validate_resource(resource)
        with (
            _translate_sqlite_errors("record private catalog resource"),
            _transaction(self._connection),
        ):
            binding = self._connection.execute(
                "SELECT 1 FROM catalog_bindings WHERE tenant_id = ? AND binding_id = ? LIMIT 1",
                (resource.tenant_id, resource.binding_id),
            ).fetchone()
            if binding is None:
                raise KeyError(f"binding {resource.binding_id} belongs to another tenant")
            self._connection.execute(
                "INSERT INTO private_catalog_resources "
                "(tenant_id, binding_id, resource_id, resource_kind, provider_ref, "
                "creation_state, retention_deadline, cleanup_status, "
                "cleanup_failure_classification, created_at, cleaned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _resource_values(resource),
            )

    def load_resource(self, tenant_id: str, resource_id: str) -> PrivateCatalogResource:
        with _translate_sqlite_errors("load private catalog resource"):
            row = self._connection.execute(
                "SELECT tenant_id, binding_id, resource_id, resource_kind, provider_ref, "
                "creation_state, retention_deadline, cleanup_status, "
                "cleanup_failure_classification, created_at, cleaned_at "
                "FROM private_catalog_resources WHERE tenant_id = ? AND resource_id = ?",
                (tenant_id, resource_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"no recorded resource {resource_id} for tenant")
        return _resource_from_row(row)

    def load_resources(self, tenant_id: str, binding_id: str) -> tuple[PrivateCatalogResource, ...]:
        self.load(tenant_id, binding_id)
        with _translate_sqlite_errors("load private catalog resources"):
            rows = self._connection.execute(
                "SELECT tenant_id, binding_id, resource_id, resource_kind, provider_ref, "
                "creation_state, retention_deadline, cleanup_status, "
                "cleanup_failure_classification, created_at, cleaned_at "
                "FROM private_catalog_resources WHERE tenant_id = ? AND binding_id = ? "
                "ORDER BY resource_id",
                (tenant_id, binding_id),
            ).fetchall()
        return tuple(_resource_from_row(row) for row in rows)

    def mark_resource_created(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        provider_ref: str | None = None,
    ) -> PrivateCatalogResource:
        if provider_ref is not None and not provider_ref:
            raise ValueError("provider_ref must not be empty")
        with (
            _translate_sqlite_errors("mark private catalog resource created"),
            _transaction(self._connection),
        ):
            resource = self._load_resource_for_update(tenant_id, resource_id)
            if resource.creation_state != "planned":
                raise ValueError("recorded resource creation is not planned")
            exact_provider_ref = provider_ref or resource.provider_ref
            self._connection.execute(
                "UPDATE private_catalog_resources "
                "SET creation_state = 'created', provider_ref = ? "
                "WHERE tenant_id = ? AND resource_id = ?",
                (exact_provider_ref, tenant_id, resource_id),
            )
            return replace(
                resource,
                provider_ref=exact_provider_ref,
                creation_state="created",
            )

    def begin_cleanup(self, tenant_id: str, resource_id: str) -> PrivateCatalogResource:
        with (
            _translate_sqlite_errors("begin private catalog resource cleanup"),
            _transaction(self._connection),
        ):
            resource = self._load_resource_for_update(tenant_id, resource_id)
            if resource.cleanup_status != "not_started":
                raise ValueError("recorded resource cleanup is already terminal or in progress")
            self._connection.execute(
                "UPDATE private_catalog_resources SET cleanup_status = 'in_progress', "
                "cleanup_failure_classification = NULL "
                "WHERE tenant_id = ? AND resource_id = ?",
                (tenant_id, resource_id),
            )
            return replace(
                resource,
                cleanup_status="in_progress",
                cleanup_failure_classification=None,
            )

    def complete_cleanup(
        self, tenant_id: str, resource_id: str, *, cleaned_at: datetime
    ) -> PrivateCatalogResource:
        _require_utc(cleaned_at)
        with (
            _translate_sqlite_errors("complete private catalog resource cleanup"),
            _transaction(self._connection),
        ):
            resource = self._load_resource_for_update(tenant_id, resource_id)
            if resource.cleanup_status != "in_progress":
                raise ValueError("recorded resource cleanup has not started")
            self._connection.execute(
                "UPDATE private_catalog_resources "
                "SET cleanup_status = 'complete', cleanup_failure_classification = NULL, "
                "cleaned_at = ? "
                "WHERE tenant_id = ? AND resource_id = ?",
                (_timestamp(cleaned_at), tenant_id, resource_id),
            )
            return replace(
                resource,
                cleanup_status="complete",
                cleaned_at=cleaned_at,
                cleanup_failure_classification=None,
            )

    def fail_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        status: Literal["failed", "unknown"],
        failure_classification: CatalogResourceCleanupFailureClassification,
    ) -> PrivateCatalogResource:
        if status not in {"failed", "unknown"}:
            raise ValueError("terminal cleanup status must be failed or unknown")
        if failure_classification not in _RESOURCE_CLEANUP_FAILURE_CLASSIFICATIONS:
            raise ValueError("cleanup failure classification is invalid")
        if status == "failed" and failure_classification == "unknown":
            raise ValueError("failed cleanup requires a known failure classification")
        if status == "unknown" and failure_classification != "unknown":
            raise ValueError("unknown cleanup requires an unknown failure classification")
        with (
            _translate_sqlite_errors("record private catalog cleanup failure"),
            _transaction(self._connection),
        ):
            resource = self._load_resource_for_update(tenant_id, resource_id)
            if resource.cleanup_status != "in_progress":
                raise ValueError("recorded resource cleanup has not started")
            self._connection.execute(
                "UPDATE private_catalog_resources SET cleanup_status = ?, "
                "cleanup_failure_classification = ?, cleaned_at = NULL "
                "WHERE tenant_id = ? AND resource_id = ?",
                (status, failure_classification, tenant_id, resource_id),
            )
            return replace(
                resource,
                cleanup_status=status,
                cleaned_at=None,
                cleanup_failure_classification=failure_classification,
            )

    def load_validation(self, tenant_id: str, binding_id: str) -> CatalogValidationEvidence:
        with _translate_sqlite_errors("load catalog validation evidence"):
            row = self._connection.execute(
                "SELECT payload FROM catalog_validation_evidence "
                "WHERE tenant_id = ? AND binding_id = ? "
                "ORDER BY binding_revision DESC LIMIT 1",
                (tenant_id, binding_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"validation evidence for {binding_id} was not recorded")
        return CatalogValidationEvidence.model_validate_json(row[0])

    def _insert_advanced_binding(self, binding: CatalogBinding, *, expected_revision: int) -> None:
        if binding.revision != expected_revision + 1:
            raise StaleRevisionError("catalog binding revision was not advanced")
        result = self._connection.execute(
            "INSERT INTO catalog_bindings (tenant_id, binding_id, revision, payload) "
            "SELECT ?, ?, ?, ? WHERE ("
            "SELECT MAX(revision) FROM catalog_bindings "
            "WHERE tenant_id = ? AND binding_id = ?"
            ") = ?",
            (
                binding.tenant_id,
                binding.binding_id,
                binding.revision,
                canonical_bytes(binding),
                binding.tenant_id,
                binding.binding_id,
                expected_revision,
            ),
        )
        if result.rowcount != 1:
            raise StaleRevisionError("catalog binding revision was not advanced")

    def _load_resource_for_update(self, tenant_id: str, resource_id: str) -> PrivateCatalogResource:
        row = self._connection.execute(
            "SELECT tenant_id, binding_id, resource_id, resource_kind, provider_ref, "
            "creation_state, retention_deadline, cleanup_status, "
            "cleanup_failure_classification, created_at, cleaned_at "
            "FROM private_catalog_resources WHERE tenant_id = ? AND resource_id = ?",
            (tenant_id, resource_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"no recorded resource {resource_id} for tenant")
        return _resource_from_row(row)

    def _assert_operation_claim(self, tenant_id: str, binding_id: str, operation_id: str) -> None:
        row = self._connection.execute(
            "SELECT operation_id FROM private_catalog_operation_claims "
            "WHERE tenant_id = ? AND binding_id = ?",
            (tenant_id, binding_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("private catalog operation claim was not recorded")
        if str(row[0]) != operation_id:
            raise CatalogOperationConflictError(
                "catalog binding is already claimed by another operation"
            )


def _binding_id(tenant_id: str, sequence: int) -> str:
    return (
        "cat-"
        + digest(
            {
                "domain": "pillarmesh-catalog-binding-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except BaseException:
        with suppress(Exception):
            connection.rollback()
        raise


@contextmanager
def _translate_sqlite_errors(operation: str) -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise CatalogPersistenceError(operation=operation) from error


def _validate_resource(resource: PrivateCatalogResource) -> None:
    if resource.creation_state not in _RESOURCE_CREATION_STATES:
        raise ValueError("private resource creation_state is invalid")
    if resource.cleanup_status not in _RESOURCE_CLEANUP_STATUSES:
        raise ValueError("private resource cleanup_status is invalid")
    if (
        resource.cleanup_failure_classification is not None
        and resource.cleanup_failure_classification not in _RESOURCE_CLEANUP_FAILURE_CLASSIFICATIONS
    ):
        raise ValueError("private resource cleanup failure classification is invalid")
    if resource.cleanup_status == "failed" and resource.cleanup_failure_classification in {
        None,
        "unknown",
    }:
        raise ValueError("failed private resource cleanup requires a known failure classification")
    if (
        resource.cleanup_status == "unknown"
        and resource.cleanup_failure_classification != "unknown"
    ):
        raise ValueError(
            "unknown private resource cleanup requires an unknown failure classification"
        )
    if resource.cleanup_status not in {"failed", "unknown"} and (
        resource.cleanup_failure_classification is not None
    ):
        raise ValueError(
            "non-failed private resource cleanup must not record a failure classification"
        )
    if not resource.tenant_id or not resource.binding_id or not resource.resource_id:
        raise ValueError("private resource identifiers must not be empty")
    if not resource.provider_ref:
        raise ValueError("private provider reference must not be empty")
    _require_utc(resource.retention_deadline)
    _require_utc(resource.created_at)
    if resource.cleaned_at is not None:
        _require_utc(resource.cleaned_at)
    if resource.cleanup_status == "complete" and resource.cleaned_at is None:
        raise ValueError("complete private resource cleanup requires a completion timestamp")
    if resource.cleanup_status != "complete" and resource.cleaned_at is not None:
        raise ValueError("incomplete private resource cleanup must not have a completion timestamp")


def _validate_operation(operation: PrivateCatalogOperation) -> None:
    if not all(
        (
            operation.tenant_id,
            operation.binding_id,
            operation.operation_id,
            operation.resource_handle,
            operation.project_name,
            operation.secret_reference,
        )
    ):
        raise ValueError("private catalog operation identifiers must not be empty")
    _require_utc(operation.created_at)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _resource_values(resource: PrivateCatalogResource) -> tuple[object, ...]:
    return (
        resource.tenant_id,
        resource.binding_id,
        resource.resource_id,
        resource.resource_kind.value,
        resource.provider_ref,
        resource.creation_state,
        _timestamp(resource.retention_deadline),
        resource.cleanup_status,
        resource.cleanup_failure_classification,
        _timestamp(resource.created_at),
        _timestamp(resource.cleaned_at) if resource.cleaned_at is not None else None,
    )


def _operation_values(operation: PrivateCatalogOperation) -> tuple[object, ...]:
    return (
        operation.tenant_id,
        operation.binding_id,
        operation.operation_id,
        operation.resource_handle,
        operation.project_name,
        operation.secret_reference,
        _timestamp(operation.created_at),
    )


def _operation_from_row(row: tuple[object, ...]) -> PrivateCatalogOperation:
    return PrivateCatalogOperation(
        tenant_id=str(row[0]),
        binding_id=str(row[1]),
        operation_id=str(row[2]),
        resource_handle=str(row[3]),
        project_name=str(row[4]),
        secret_reference=str(row[5]),
        created_at=datetime.fromisoformat(str(row[6])),
    )


def _resource_from_row(row: tuple[object, ...]) -> PrivateCatalogResource:
    cleanup_failure_classification = (
        _CLEANUP_FAILURE_CLASSIFICATION_ADAPTER.validate_python(row[8])
        if row[8] is not None
        else None
    )
    cleaned_at = str(row[10]) if row[10] is not None else None
    return PrivateCatalogResource(
        tenant_id=str(row[0]),
        binding_id=str(row[1]),
        resource_id=str(row[2]),
        resource_kind=CatalogResourceKind(str(row[3])),
        provider_ref=str(row[4]),
        creation_state=_CREATION_STATE_ADAPTER.validate_python(row[5]),
        retention_deadline=datetime.fromisoformat(str(row[6])),
        cleanup_status=_CLEANUP_STATUS_ADAPTER.validate_python(row[7]),
        created_at=datetime.fromisoformat(str(row[9])),
        cleaned_at=datetime.fromisoformat(cleaned_at) if cleaned_at is not None else None,
        cleanup_failure_classification=cleanup_failure_classification,
    )
