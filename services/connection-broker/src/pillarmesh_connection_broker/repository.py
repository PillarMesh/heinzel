from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import BaseModel, ValidationError

from .models import (
    SOURCE_BINDING_TRANSITIONS,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from .private_state import PrivateSourceCapability


class SourceBindingNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__("source binding not found")


class StaleSourceBindingRevisionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("source binding revision is stale")


class SourceBindingConflictError(RuntimeError):
    pass


class SourceBindingPersistenceError(RuntimeError):
    def __init__(self, *, operation: str, detail: str | None = None) -> None:
        self.operation = operation
        message = f"source binding persistence failed during {operation}"
        super().__init__(f"{message}: {detail}" if detail else message)


class SourceBindingIntegrityError(SourceBindingPersistenceError):
    """The stored row is wrong, rather than the store being unreachable.

    A driver failure is transient and a caller may retry it; corruption is
    permanent and retrying only repeats it. Both arrived as
    `SourceBindingPersistenceError`, which forced any consumer classifying the
    failure to pick one verdict for both -- the acquisition runtime picked
    `authorization_denied` and wrote it into durable evidence for what was a
    momentary database failure.

    It stays a subclass so every existing consumer keeps failing closed exactly as
    it did; only a caller that asks for the distinction sees one.
    """


_SCHEMA_VERSION = 1
_SCHEMA_DEFINITIONS = (
    (
        "source_binding_schema_metadata",
        "CREATE TABLE source_binding_schema_metadata ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "version INTEGER NOT NULL, checksum TEXT NOT NULL)",
    ),
    (
        "source_bindings",
        "CREATE TABLE source_bindings ("
        "tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL, revision INTEGER NOT NULL, "
        "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, binding_id, revision))",
    ),
    (
        "private_source_capabilities",
        "CREATE TABLE private_source_capabilities ("
        "tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL, "
        "credential_revision INTEGER NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, binding_id, credential_revision))",
    ),
    (
        "source_binding_validation_evidence",
        "CREATE TABLE source_binding_validation_evidence ("
        "tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL, "
        "evidence_id TEXT NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, binding_id, binding_revision), "
        "UNIQUE (tenant_id, evidence_id), "
        "FOREIGN KEY (tenant_id, binding_id, binding_revision) "
        "REFERENCES source_bindings (tenant_id, binding_id, revision))",
    ),
)
_SCHEMA_CHECKSUM = digest(
    {
        "component": "pillarmesh-connection-broker",
        "schema_version": _SCHEMA_VERSION,
        "definitions": _SCHEMA_DEFINITIONS,
    }
)


class SourceBindingRepository(Protocol):
    def create(
        self, binding: SourceConnectionBinding, capability: PrivateSourceCapability
    ) -> None: ...

    def append(self, binding: SourceConnectionBinding, *, expected_revision: int) -> None: ...

    def rotate(
        self,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        *,
        expected_revision: int,
    ) -> None: ...

    def record_validation(
        self,
        binding: SourceConnectionBinding,
        evidence: SourceBindingValidationEvidence,
        *,
        expected_revision: int,
    ) -> None: ...

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding: ...

    def load_capability(
        self, tenant_id: str, binding_id: str, credential_revision: int
    ) -> PrivateSourceCapability: ...

    def load_validation(
        self, tenant_id: str, binding_id: str, binding_revision: int
    ) -> SourceBindingValidationEvidence: ...


class SQLiteSourceBindingRepository:
    def __init__(self, database_path: str) -> None:
        try:
            connection = sqlite3.connect(database_path)
        except sqlite3.Error as error:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository"
            ) from error
        self._connection = connection
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            _initialize_schema(self._connection)
        except SourceBindingPersistenceError:
            with suppress(BaseException):
                self._connection.close()
            raise
        except sqlite3.Error as error:
            with suppress(BaseException):
                self._connection.close()
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository"
            ) from error

    def close(self) -> None:
        with _translate_sqlite_errors("close source binding repository"):
            self._connection.close()

    def create(self, binding: SourceConnectionBinding, capability: PrivateSourceCapability) -> None:
        if binding.revision != 1 or binding.credential_revision != 1:
            raise ValueError("new source binding must start at revision one")
        if binding.lifecycle_state is not SourceConnectionBindingState.DRAFT:
            raise SourceBindingConflictError("new source binding must start in draft")
        _assert_capability_matches(binding, capability)
        try:
            with _transaction(self._connection):
                self._insert_binding(binding)
                self._insert_capability(capability)
        except sqlite3.IntegrityError as error:
            raise SourceBindingConflictError("source binding already exists") from error
        except sqlite3.Error as error:
            raise SourceBindingPersistenceError(operation="create source binding") from error

    def append(self, binding: SourceConnectionBinding, *, expected_revision: int) -> None:
        with (
            _translate_sqlite_errors("append source binding revision"),
            _transaction(self._connection),
        ):
            current = self._load_current(binding.tenant_id, binding.binding_id)
            _assert_advance(current, binding, expected_revision=expected_revision)
            if binding.credential_revision != current.credential_revision:
                raise StaleSourceBindingRevisionError()
            if binding.lifecycle_state is SourceConnectionBindingState.READY:
                raise SourceBindingConflictError(
                    "ready source binding requires validation evidence"
                )
            if binding.lifecycle_state not in SOURCE_BINDING_TRANSITIONS[current.lifecycle_state]:
                raise SourceBindingConflictError("source binding transition is not allowed")
            self._insert_binding(binding)

    def rotate(
        self,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        *,
        expected_revision: int,
    ) -> None:
        _assert_capability_matches(binding, capability)
        with (
            _translate_sqlite_errors("rotate source binding capability"),
            _transaction(self._connection),
        ):
            current = self._load_current(binding.tenant_id, binding.binding_id)
            _assert_advance(current, binding, expected_revision=expected_revision)
            if current.lifecycle_state in {
                SourceConnectionBindingState.DRAFT,
                SourceConnectionBindingState.RETIRED,
            }:
                raise SourceBindingConflictError("source binding rotation is not allowed")
            if binding.credential_revision != current.credential_revision + 1:
                raise StaleSourceBindingRevisionError()
            if (
                binding.lifecycle_state is not SourceConnectionBindingState.VALIDATING
                or binding.capability_profile_digest is not None
                or binding.source_observation_ref is not None
            ):
                raise SourceBindingConflictError(
                    "source binding rotation must require fresh validation"
                )
            self._insert_binding(binding)
            self._insert_capability(capability)

    def record_validation(
        self,
        binding: SourceConnectionBinding,
        evidence: SourceBindingValidationEvidence,
        *,
        expected_revision: int,
    ) -> None:
        try:
            with _transaction(self._connection):
                self._record_validation_in_transaction(
                    binding,
                    evidence,
                    expected_revision=expected_revision,
                )
        except sqlite3.IntegrityError as error:
            raise SourceBindingConflictError("source validation evidence already exists") from error
        except sqlite3.Error as error:
            raise SourceBindingPersistenceError(
                operation="record source binding validation"
            ) from error

    def _record_validation_in_transaction(
        self,
        binding: SourceConnectionBinding,
        evidence: SourceBindingValidationEvidence,
        *,
        expected_revision: int,
    ) -> None:
        current = self._load_current(binding.tenant_id, binding.binding_id)
        _assert_advance(current, binding, expected_revision=expected_revision)
        if current.lifecycle_state is not SourceConnectionBindingState.VALIDATING:
            raise SourceBindingConflictError("source validation requires a validating binding")
        if (
            evidence.tenant_id != current.tenant_id
            or evidence.binding_id != current.binding_id
            or evidence.binding_revision != current.revision
            or evidence.credential_revision != current.credential_revision
            or evidence.provider_kind != current.provider_kind
        ):
            raise SourceBindingConflictError("source validation evidence does not match binding")
        if (
            binding.lifecycle_state is not SourceConnectionBindingState.READY
            or binding.credential_revision != current.credential_revision
            or binding.capability_profile_digest != evidence.capability_profile_digest
            or binding.source_observation_ref != evidence.source_observation_ref
            or evidence.observed_at < current.updated_at
            or binding.updated_at != evidence.observed_at
        ):
            raise SourceBindingConflictError(
                "source validation authority does not match ready binding"
            )
        self._insert_binding(binding)
        result = self._connection.execute(
            "INSERT INTO source_binding_validation_evidence "
            "(tenant_id, binding_id, binding_revision, evidence_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                evidence.tenant_id,
                evidence.binding_id,
                evidence.binding_revision,
                evidence.evidence_id,
                canonical_bytes(evidence),
            ),
        )
        if result.rowcount != 1:
            raise SourceBindingConflictError("source validation evidence was not recorded")

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        with _translate_sqlite_errors("load source binding"):
            return self._load_current(tenant_id, binding_id)

    def load_capability(
        self, tenant_id: str, binding_id: str, credential_revision: int
    ) -> PrivateSourceCapability:
        with _translate_sqlite_errors("load private source capability"):
            row = self._connection.execute(
                "SELECT tenant_id, binding_id, credential_revision, payload "
                "FROM private_source_capabilities "
                "WHERE tenant_id = ? AND binding_id = ? AND credential_revision = ?",
                (tenant_id, binding_id, credential_revision),
            ).fetchone()
        if row is None:
            raise SourceBindingNotFoundError()
        capability = _revalidate_payload(
            PrivateSourceCapability,
            row[3],
            operation="load private source capability",
        )
        if (
            capability.tenant_id != str(row[0])
            or capability.binding_id != str(row[1])
            or capability.credential_revision != int(row[2])
            or capability.tenant_id != tenant_id
            or capability.binding_id != binding_id
            or capability.credential_revision != credential_revision
        ):
            raise SourceBindingIntegrityError(
                operation="load private source capability",
                detail="stored row identity mismatch",
            )
        return capability

    def load_validation(
        self, tenant_id: str, binding_id: str, binding_revision: int
    ) -> SourceBindingValidationEvidence:
        with _translate_sqlite_errors("load source binding validation"):
            row = self._connection.execute(
                "SELECT tenant_id, binding_id, binding_revision, evidence_id, payload "
                "FROM source_binding_validation_evidence "
                "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ?",
                (tenant_id, binding_id, binding_revision),
            ).fetchone()
        if row is None:
            raise SourceBindingNotFoundError()
        evidence = _revalidate_payload(
            SourceBindingValidationEvidence,
            row[4],
            operation="load source binding validation",
        )
        if (
            evidence.tenant_id != str(row[0])
            or evidence.binding_id != str(row[1])
            or evidence.binding_revision != int(row[2])
            or evidence.evidence_id != str(row[3])
            or evidence.tenant_id != tenant_id
            or evidence.binding_id != binding_id
            or evidence.binding_revision != binding_revision
        ):
            raise SourceBindingIntegrityError(
                operation="load source binding validation",
                detail="stored row identity mismatch",
            )
        return evidence

    def _load_current(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        row = self._connection.execute(
            "SELECT tenant_id, binding_id, revision, payload FROM source_bindings "
            "WHERE tenant_id = ? AND binding_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (tenant_id, binding_id),
        ).fetchone()
        if row is None:
            raise SourceBindingNotFoundError()
        binding = _revalidate_payload(
            SourceConnectionBinding,
            row[3],
            operation="load source binding",
        )
        if (
            binding.tenant_id != str(row[0])
            or binding.binding_id != str(row[1])
            or binding.revision != int(row[2])
            or binding.tenant_id != tenant_id
            or binding.binding_id != binding_id
        ):
            raise SourceBindingIntegrityError(
                operation="load source binding",
                detail="stored row identity mismatch",
            )
        return binding

    def _insert_binding(self, binding: SourceConnectionBinding) -> None:
        self._connection.execute(
            "INSERT INTO source_bindings (tenant_id, binding_id, revision, payload) "
            "VALUES (?, ?, ?, ?)",
            (binding.tenant_id, binding.binding_id, binding.revision, canonical_bytes(binding)),
        )

    def _insert_capability(self, capability: PrivateSourceCapability) -> None:
        self._connection.execute(
            "INSERT INTO private_source_capabilities "
            "(tenant_id, binding_id, credential_revision, payload) VALUES (?, ?, ?, ?)",
            (
                capability.tenant_id,
                capability.binding_id,
                capability.credential_revision,
                canonical_bytes(capability),
            ),
        )


def _initialize_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    expected_tables = {name for name, _statement in _SCHEMA_DEFINITIONS}
    if "source_binding_schema_metadata" in tables:
        row = connection.execute(
            "SELECT version, checksum FROM source_binding_schema_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository",
                detail="schema metadata is missing",
            )
        version, checksum = int(row[0]), str(row[1])
        if version > _SCHEMA_VERSION:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository",
                detail="database schema is newer than runtime",
            )
        if version != _SCHEMA_VERSION or checksum != _SCHEMA_CHECKSUM:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository",
                detail="schema checksum mismatch",
            )
        if tables != expected_tables:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository",
                detail="schema table layout mismatch",
            )
        actual_definitions = {
            str(name): _normalize_sql(str(statement))
            for name, statement in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_definitions = {
            name: _normalize_sql(statement) for name, statement in _SCHEMA_DEFINITIONS
        }
        if actual_definitions != expected_definitions:
            raise SourceBindingPersistenceError(
                operation="initialize source binding repository",
                detail="schema definition mismatch",
            )
        return
    if tables:
        raise SourceBindingPersistenceError(
            operation="initialize source binding repository",
            detail="schema metadata is missing",
        )
    with _transaction(connection):
        for _name, statement in _SCHEMA_DEFINITIONS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO source_binding_schema_metadata (singleton, version, checksum) "
            "VALUES (1, ?, ?)",
            (_SCHEMA_VERSION, _SCHEMA_CHECKSUM),
        )


def _revalidate_payload[Model: BaseModel](
    model: type[Model], payload: str | bytes, *, operation: str
) -> Model:
    try:
        return model.model_validate_json(payload)
    except (TypeError, ValueError, ValidationError) as error:
        raise SourceBindingIntegrityError(
            operation=operation,
            detail="stored payload is invalid",
        ) from error


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.split())


def _immutable_identity(binding: SourceConnectionBinding) -> tuple[object, ...]:
    return (
        binding.tenant_id,
        binding.binding_id,
        binding.provider_kind,
        binding.connection_handle,
        binding.account_mode,
        binding.approved_object_refs,
        binding.created_at,
    )


def _assert_advance(
    current: SourceConnectionBinding,
    candidate: SourceConnectionBinding,
    *,
    expected_revision: int,
) -> None:
    if (
        current.revision != expected_revision
        or candidate.revision != expected_revision + 1
        or candidate.updated_at < current.updated_at
        or _immutable_identity(current) != _immutable_identity(candidate)
        or candidate.credential_revision
        not in {
            current.credential_revision,
            current.credential_revision + 1,
        }
    ):
        raise StaleSourceBindingRevisionError()


def _assert_capability_matches(
    binding: SourceConnectionBinding, capability: PrivateSourceCapability
) -> None:
    if (
        capability.tenant_id != binding.tenant_id
        or capability.binding_id != binding.binding_id
        or capability.provider_kind != binding.provider_kind
        or capability.connection_handle != binding.connection_handle
        or capability.account_mode != binding.account_mode
        or capability.credential_revision != binding.credential_revision
    ):
        raise SourceBindingConflictError("private source capability does not match binding")


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with suppress(BaseException):
            connection.rollback()
        raise
    else:
        try:
            connection.commit()
        except BaseException:
            with suppress(BaseException):
                connection.rollback()
            raise


@contextmanager
def _translate_sqlite_errors(operation: str) -> Iterator[None]:
    try:
        yield
    except sqlite3.Error as error:
        raise SourceBindingPersistenceError(operation=operation) from error
