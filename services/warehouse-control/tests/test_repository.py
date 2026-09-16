import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseAdmissionError,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseFailureClassification,
    WarehouseOperationConflictError,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehousePersistenceError,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationConflictError,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pillarmesh_warehouse_control import repository as repository_module
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository, StaleRevisionError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


def draft(control: WarehouseControlService) -> WarehouseBinding:
    return control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )


def advance_binding(
    binding: WarehouseBinding, lifecycle_state: WarehouseBindingState
) -> WarehouseBinding:
    values = binding.model_dump()
    values.update(
        lifecycle_state=lifecycle_state,
        revision=binding.revision + 1,
        updated_at=LATER,
    )
    return WarehouseBinding.model_validate(values)


def operation(binding: WarehouseBinding, **updates: object) -> PrivateWarehouseOperation:
    values: dict[str, object] = {
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "operation_id": "wop-test",
        "operation_kind": WarehouseOperationKind.PROVISION,
        "engine_kind": binding.engine_kind,
        "status": WarehouseOperationStatus.CLAIMED,
        "phase": WarehouseOperationPhase.CLAIMED,
        "provider_resource_handle": None,
        "failure_classification": None,
        "started_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return PrivateWarehouseOperation.model_validate(values)


def _record_operation_state(
    repository: SQLiteWarehouseRepository,
    durable: PrivateWarehouseOperation,
) -> None:
    initial = PrivateWarehouseOperation.model_validate(
        {
            **durable.model_dump(),
            "status": WarehouseOperationStatus.CLAIMED,
            "phase": WarehouseOperationPhase.CLAIMED,
            "provider_resource_handle": None,
            "failure_classification": None,
            "updated_at": durable.started_at,
        }
    )
    repository.claim_operation(initial)
    if durable == initial:
        return
    current = initial.model_copy(
        update={
            "status": WarehouseOperationStatus.RUNNING,
            "updated_at": durable.updated_at,
        }
    )
    repository.save_operation(initial, current)
    phase_paths = {
        WarehouseOperationKind.PROVISION: (
            WarehouseOperationPhase.PROVIDER_CREATED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationPhase.VALIDATED,
        ),
        WarehouseOperationKind.SUSPEND: (WarehouseOperationPhase.SUSPENDED,),
        WarehouseOperationKind.RESUME: (
            WarehouseOperationPhase.RESUMED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationPhase.VALIDATED,
        ),
        WarehouseOperationKind.RETIRE: (WarehouseOperationPhase.RETIRED,),
    }
    for phase in phase_paths[durable.operation_kind]:
        if durable.phase is WarehouseOperationPhase.CLAIMED:
            break
        updated = current.model_copy(
            update={
                "phase": phase,
                "provider_resource_handle": (
                    durable.provider_resource_handle
                    if phase is WarehouseOperationPhase.PROVIDER_CREATED
                    else current.provider_resource_handle
                ),
            }
        )
        repository.save_operation(current, updated)
        current = updated
        if phase is durable.phase:
            break
    if current != durable:
        repository.save_operation(current, durable)


@dataclass(frozen=True, slots=True)
class StableAdmissionFixture:
    source: WarehouseBinding
    terminal: WarehouseBinding
    expected_operation: PrivateWarehouseOperation
    succeeded_operation: PrivateWarehouseOperation


def stable_admission_fixture(
    repository: SQLiteWarehouseRepository,
    kind: str,
) -> StableAdmissionFixture:
    initial = draft(WarehouseControlService(repository, clock=lambda: NOW))
    if kind == "initial":
        operation_binding = advance_binding(initial, WarehouseBindingState.PROVISIONING)
        repository.save(operation_binding)
        source = advance_binding(operation_binding, WarehouseBindingState.VALIDATING)
        terminal = WarehouseBinding.model_validate(
            {
                **advance_binding(source, WarehouseBindingState.READY).model_dump(),
                "provisioned_at": LATER,
            }
        )
        operation_kind = WarehouseOperationKind.PROVISION
        phase = WarehouseOperationPhase.VALIDATING
        provider_resource_handle = "provider-resource"
    elif kind == "resume":
        source = WarehouseBinding.model_validate(
            {
                **advance_binding(initial, WarehouseBindingState.SUSPENDED).model_dump(),
                "provisioned_at": NOW,
            }
        )
        operation_binding = source
        terminal = advance_binding(source, WarehouseBindingState.READY)
        operation_kind = WarehouseOperationKind.RESUME
        phase = WarehouseOperationPhase.VALIDATING
        provider_resource_handle = None
    elif kind == "retirement":
        source = WarehouseBinding.model_validate(
            {
                **advance_binding(initial, WarehouseBindingState.RETIRING).model_dump(),
                "provisioned_at": NOW,
            }
        )
        operation_binding = source
        terminal = advance_binding(source, WarehouseBindingState.RETIRED)
        operation_kind = WarehouseOperationKind.RETIRE
        phase = WarehouseOperationPhase.CLAIMED
        provider_resource_handle = None
    else:
        raise AssertionError("unknown stable admission fixture kind")
    if kind != "initial":
        repository.save(source)
    expected_operation = operation(
        operation_binding,
        operation_id=f"wop-admission-{kind}",
        operation_kind=operation_kind,
        status=WarehouseOperationStatus.RUNNING,
        phase=phase,
        provider_resource_handle=provider_resource_handle,
    )
    _record_operation_state(repository, expected_operation)
    if kind == "initial":
        repository.save(source)
    succeeded_operation = expected_operation.model_copy(
        update={
            "status": WarehouseOperationStatus.SUCCEEDED,
            "phase": {
                "initial": WarehouseOperationPhase.VALIDATED,
                "resume": WarehouseOperationPhase.VALIDATED,
                "retirement": WarehouseOperationPhase.RETIRED,
            }[kind],
        }
    )
    return StableAdmissionFixture(
        source=source,
        terminal=terminal,
        expected_operation=expected_operation,
        succeeded_operation=succeeded_operation,
    )


def resource(
    warehouse_operation: PrivateWarehouseOperation, **updates: object
) -> PrivateWarehouseResource:
    values: dict[str, object] = {
        "tenant_id": warehouse_operation.tenant_id,
        "binding_id": warehouse_operation.binding_id,
        "binding_revision": warehouse_operation.binding_revision,
        "operation_id": warehouse_operation.operation_id,
        "resource_id": "whr-test",
        "resource_kind": WarehouseResourceKind.RESTORE_CONTAINER,
        "provider_resource_handle": "restore-container-test",
        "parent_resource_handle": "restore-project-test",
        "creation_state": WarehouseResourceCreationState.PLANNED,
        "retention_deadline": NOW + timedelta(days=30),
        "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
        "cleanup_failure_classification": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return PrivateWarehouseResource.model_validate(values)


def restore_verification(
    binding: WarehouseBinding, **updates: object
) -> WarehouseRestoreVerification:
    values: dict[str, object] = {
        "verification_id": "wrv-test",
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "engine_kind": binding.engine_kind,
        "source_backup_artifact_digest": "a" * 64,
        "representative_data_digest": "b" * 64,
        "schema_metadata_digest": "c" * 64,
        "principal_profile_digest": "d" * 64,
        "integrity_marker_digest": "e" * 64,
        "query_behavior_digest": "f" * 64,
        "verified_at": NOW,
    }
    values.update(updates)
    return WarehouseRestoreVerification.model_validate(values)


def validation_evidence(
    binding: WarehouseBinding,
    restore: WarehouseRestoreVerification,
    **updates: object,
) -> WarehouseValidationEvidence:
    values: dict[str, object] = {
        "evidence_id": "wev-test",
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "validation_profile": WarehouseValidationProfile.PRODUCTION,
        "engine_kind": binding.engine_kind,
        "engine_version": "18.6",
        "engine_build_digest": "0" * 64,
        "engine_image_digest": "1" * 64,
        "principal_profile_digest": restore.principal_profile_digest,
        "namespace_grant_matrix_digest": "3" * 64,
        "tls_probe_digest": "4" * 64,
        "network_isolation_probe_digest": "5" * 64,
        "encryption_at_rest_evidence_digest": "6" * 64,
        "encryption_at_rest_disposition": EncryptionAtRestDisposition.PROVEN,
        "positive_probe_digest": "7" * 64,
        "denial_probe_digest": "8" * 64,
        "ledger_probe_digest": "9" * 64,
        "monitoring_probe_digest": "a" * 64,
        "capacity_alert_probe_digest": "b" * 64,
        "backup_artifact_digest": restore.source_backup_artifact_digest,
        "restore_verification_digest": digest(restore),
        "restore_cleanup_digest": "c" * 64,
        "observed_at": NOW,
    }
    values.update(updates)
    return WarehouseValidationEvidence.model_validate(values)


def resume_validation_evidence(
    binding: WarehouseBinding, **updates: object
) -> WarehouseResumeValidationEvidence:
    values: dict[str, object] = {
        "evidence_id": "wrev-test",
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "engine_kind": binding.engine_kind,
        "engine_version": "18.6",
        "engine_build_digest": "0" * 64,
        "engine_image_digest": "1" * 64,
        "tls_probe_digest": "2" * 64,
        "network_isolation_probe_digest": "3" * 64,
        "monitoring_probe_digest": "4" * 64,
        "positive_probe_digest": "5" * 64,
        "denial_probe_digest": "6" * 64,
        "storage_integrity_probe_digest": "7" * 64,
        "observed_at": NOW,
    }
    values.update(updates)
    return WarehouseResumeValidationEvidence.model_validate(values)


def retirement_evidence(
    binding: WarehouseBinding,
    resources: tuple[PrivateWarehouseResource, ...] = (),
    **updates: object,
) -> WarehouseRetirementEvidence:
    ordered_resources = sorted(
        resources,
        key=lambda resource: (
            resource.binding_revision,
            resource.operation_id,
            resource.resource_id,
        ),
    )
    values: dict[str, object] = {
        "evidence_id": "wret-test",
        "tenant_id": binding.tenant_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.revision,
        "resource_inventory_digest": digest(
            {
                "domain": "warehouse_retirement_resource_inventory_v1",
                "resources": [
                    {
                        "binding_revision": resource.binding_revision,
                        "operation_id": resource.operation_id,
                        "resource_id": resource.resource_id,
                        "resource_kind": resource.resource_kind,
                        "provider_resource_handle": resource.provider_resource_handle,
                        "parent_resource_handle": resource.parent_resource_handle,
                        "creation_state": resource.creation_state,
                        "created_at": resource.created_at,
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        "cleanup_disposition_digest": digest(
            {
                "domain": "warehouse_retirement_cleanup_disposition_v1",
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "cleanup_status": resource.cleanup_status,
                        "cleanup_failure_classification": (resource.cleanup_failure_classification),
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        "retention_policy_digest": digest(
            {
                "domain": "warehouse_retirement_retention_policy_v1",
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "retention_deadline": resource.retention_deadline,
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        "completed_resource_count": sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in resources
        ),
        "retained_resource_count": sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
            for resource in resources
        ),
        "cleanup_failed_resource_count": sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.FAILED
            for resource in resources
        ),
        "observed_at": NOW,
    }
    values.update(updates)
    return WarehouseRetirementEvidence.model_validate(values)


def recorded_terminal_resources(
    repository: SQLiteWarehouseRepository,
    binding: WarehouseBinding,
    warehouse_operation: PrivateWarehouseOperation | None = None,
) -> tuple[PrivateWarehouseResource, ...]:
    if warehouse_operation is None:
        warehouse_operation = operation(
            binding,
            status=WarehouseOperationStatus.SUCCEEDED,
            phase=WarehouseOperationPhase.VALIDATED,
        )
        _record_operation_state(repository, warehouse_operation)
    resources = tuple(
        resource(
            warehouse_operation,
            resource_id=f"whr-terminal-{index}",
            provider_resource_handle=f"terminal-{index}",
            cleanup_status=status,
            cleanup_failure_classification=(
                WarehouseFailureClassification.INTEGRITY_FAILURE
                if status is WarehouseResourceCleanupStatus.FAILED
                else None
            ),
            creation_state=WarehouseResourceCreationState.CREATED,
            updated_at=LATER,
        )
        for index, status in enumerate(
            (
                WarehouseResourceCleanupStatus.COMPLETE,
                WarehouseResourceCleanupStatus.RETAINED,
                WarehouseResourceCleanupStatus.FAILED,
            )
        )
    )
    repository.record_resources(resources)
    return resources


class BarrierConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        barrier: Barrier,
        atomic_allocations: Queue[None],
    ) -> None:
        self._connection = connection
        self._barrier = barrier
        self._atomic_allocations = atomic_allocations

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("SELECT next_sequence FROM warehouse_sequences"):
            raise AssertionError("warehouse sequence allocation must use one atomic statement")
        if sql.startswith("INSERT INTO warehouse_sequences"):
            self._atomic_allocations.put(None)
            self._barrier.wait(timeout=5)
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def close(self) -> None:
        self._connection.close()


class FailingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fail_after_prefix: str,
        rollback_error: sqlite3.Error | None = None,
    ) -> None:
        self._connection = connection
        self._fail_after_prefix = fail_after_prefix
        self._rollback_error = rollback_error

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if sql.lstrip().startswith(self._fail_after_prefix):
            raise sqlite3.OperationalError("sensitive primary sqlite detail")
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()
        if self._rollback_error is not None:
            raise self._rollback_error

    def close(self) -> None:
        self._connection.close()


class InitializationFailureConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql == "PRAGMA foreign_keys = ON":
            raise sqlite3.OperationalError("sensitive initialization detail")
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        self._connection.close()
        raise sqlite3.OperationalError("close must not replace initialization failure")


class TenantScopeConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        normalized_sql = " ".join(sql.split())
        if (
            normalized_sql.startswith("INSERT")
            and "INTO warehouse_bindings" in normalized_sql
            and "SELECT 1 FROM warehouse_bindings WHERE binding_id" in normalized_sql
        ):
            raise AssertionError("binding existence probes must be tenant-qualified")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def close(self) -> None:
        self._connection.close()


def test_repository_load_filters_tenant_before_returning_binding(tmp_path: Path) -> None:
    repository = SQLiteWarehouseRepository(str(tmp_path / "warehouse.db"))
    control = WarehouseControlService(repository, clock=lambda: NOW)
    warehouse_binding = draft(control)

    assert repository.load("tenant-b", warehouse_binding.binding_id) is None


def test_binding_writes_never_probe_for_another_tenants_identifier() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    repository._connection = TenantScopeConnection(repository._connection)
    other_tenant_binding = WarehouseBinding.model_validate(
        {**binding.model_dump(), "tenant_id": "tenant-b"}
    )

    failures: list[tuple[object, ...]] = []
    for replay in (binding, other_tenant_binding):
        with pytest.raises(StaleRevisionError) as failure:
            repository.save(replay)
        failures.append(failure.value.args)

    assert failures == [
        ("warehouse binding revision was not advanced",),
        ("warehouse binding revision was not advanced",),
    ]


def test_two_connections_allocate_distinct_sequences_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "warehouse.db"
    SQLiteWarehouseRepository(str(database_path))
    barrier = Barrier(2)
    atomic_allocations: Queue[None] = Queue()
    original_connect = sqlite3.connect
    outcomes: Queue[WarehouseBinding | Exception] = Queue()

    def connect(database: str, *args: object, **kwargs: object) -> BarrierConnection:
        return BarrierConnection(
            original_connect(database, *args, **kwargs), barrier, atomic_allocations
        )

    monkeypatch.setattr(repository_module.sqlite3, "connect", connect)

    def create_draft() -> None:
        control = WarehouseControlService(
            SQLiteWarehouseRepository(str(database_path)), clock=lambda: NOW
        )
        try:
            outcomes.put(draft(control))
        except Exception as error:
            outcomes.put(error)

    first = Thread(target=create_draft)
    second = Thread(target=create_draft)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert atomic_allocations.qsize() == 2
    results = [outcomes.get_nowait() for _ in range(2)]
    bindings = [result for result in results if isinstance(result, WarehouseBinding)]
    assert len(bindings) == 2
    assert bindings[0].binding_id != bindings[1].binding_id


def test_stored_payload_is_the_canonical_form_the_platform_digests() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))

    payload = repository._connection.execute("SELECT payload FROM warehouse_bindings").fetchone()[0]

    # Every artifact here is content-addressed by digest(); a row serialized any other
    # way hashes differently and reads as corruption to the first consumer that checks.
    assert bytes(payload) == canonical_bytes(binding)
    assert hashlib.sha256(payload).hexdigest() == digest(binding)


def test_private_schema_enables_foreign_keys_and_preserves_current_code_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    original = WarehouseBinding(
        binding_id="whb-existing",
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capability_profile_digest="a" * 64,
        lifecycle_state=WarehouseBindingState.PROVISIONING,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE warehouse_bindings ("
        "binding_id TEXT NOT NULL, revision INTEGER NOT NULL, tenant_id TEXT NOT NULL, "
        "payload BLOB NOT NULL, PRIMARY KEY (binding_id, revision))"
    )
    connection.execute(
        "CREATE TABLE warehouse_sequences ("
        "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO warehouse_bindings (binding_id, revision, tenant_id, payload) "
        "VALUES (?, ?, ?, ?)",
        (original.binding_id, original.revision, original.tenant_id, canonical_bytes(original)),
    )
    connection.commit()
    connection.close()

    repository = SQLiteWarehouseRepository(str(database_path))
    claimed = repository.claim_operation(operation(original))

    assert claimed is True
    assert repository.load("tenant-a", original.binding_id) == original
    assert repository._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        repository._connection.execute(
            "INSERT INTO private_warehouse_operation_claims "
            "(tenant_id, binding_id, binding_revision, operation_id) VALUES (?, ?, ?, ?)",
            ("tenant-a", "whb-missing", 1, "wop-missing"),
        )

    repository._connection.rollback()
    repository._connection.close()
    reopened = SQLiteWarehouseRepository(str(database_path))
    assert reopened.load("tenant-a", original.binding_id) == original


def test_private_schema_rejects_a_tampered_migration_checksum(tmp_path: Path) -> None:
    database_path = tmp_path / "warehouse.db"
    repository = SQLiteWarehouseRepository(str(database_path))
    repository._connection.execute(
        "UPDATE warehouse_schema_metadata SET checksum = 'tampered' WHERE singleton = 1"
    )
    repository._connection.commit()
    repository._connection.close()

    with pytest.raises(WarehousePersistenceError, match="checksum"):
        SQLiteWarehouseRepository(str(database_path))


def test_private_schema_migrates_legacy_hba_resources_to_the_approved_file_kind(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    legacy = resource(
        claimed_operation,
        resource_id="wrs-legacy-hba",
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle="/private/pg_hba.conf",
        parent_resource_handle="pgw-private",
    )
    legacy_payload = legacy.model_dump(mode="json")
    legacy_payload["resource_kind"] = "hba_configuration"
    repository._connection.execute(
        "INSERT INTO private_warehouse_resources "
        "(tenant_id, binding_id, binding_revision, operation_id, resource_id, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            legacy.tenant_id,
            legacy.binding_id,
            legacy.binding_revision,
            legacy.operation_id,
            legacy.resource_id,
            json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ),
    )
    repository._connection.execute(
        "UPDATE warehouse_schema_metadata SET version = 1, checksum = ? WHERE singleton = 1",
        (repository_module._SCHEMA_CHECKSUM,),
    )
    repository._connection.commit()
    repository.close()

    migrated = SQLiteWarehouseRepository(str(database_path))

    assert migrated.load_resources(binding.tenant_id, binding.binding_id) == (legacy,)


def test_operation_sequence_is_tenant_scoped_and_replay_safe() -> None:
    repository = SQLiteWarehouseRepository(":memory:")

    assert repository.next_operation_sequence("tenant-a") == 1
    assert repository.next_operation_sequence("tenant-a") == 2
    assert repository.next_operation_sequence("tenant-b") == 1


def test_claim_replay_returns_false_for_the_same_operation_and_revision() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)

    assert repository.claim_operation(claimed_operation) is True
    assert repository.claim_operation(claimed_operation) is False
    assert (
        repository.load_operation(
            "tenant-a", provisioning.binding_id, claimed_operation.operation_id
        )
        == claimed_operation
    )


@pytest.mark.parametrize(
    "updates",
    (
        {
            "status": WarehouseOperationStatus.RUNNING,
            "phase": WarehouseOperationPhase.CLAIMED,
        },
        {
            "status": WarehouseOperationStatus.CLAIMED,
            "phase": WarehouseOperationPhase.PROVIDER_CREATED,
        },
        {
            "status": WarehouseOperationStatus.SUCCEEDED,
            "phase": WarehouseOperationPhase.VALIDATED,
        },
        {
            "status": WarehouseOperationStatus.FAILED,
            "phase": WarehouseOperationPhase.CLAIMED,
            "failure_classification": (WarehouseFailureClassification.PERMANENT_CONFIGURATION),
        },
    ),
)
def test_operation_claim_rejects_every_noninitial_status_or_phase(
    updates: dict[str, object],
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    candidate = operation(binding, **updates)

    with pytest.raises(WarehouseOperationConflictError, match="initial claimed state"):
        repository.claim_operation(candidate)

    with pytest.raises(KeyError, match="not recorded"):
        repository.load_operation(
            candidate.tenant_id,
            candidate.binding_id,
            candidate.operation_id,
        )


def test_competing_live_claim_fails_closed() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    repository.claim_operation(operation(provisioning, operation_id="wop-first"))

    with pytest.raises(WarehouseOperationConflictError, match="already has a live operation"):
        repository.claim_operation(operation(provisioning, operation_id="wop-second"))


def test_live_operation_is_recovered_after_crash_without_allocating_another_sequence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    repository = SQLiteWarehouseRepository(str(database_path))
    control = WarehouseControlService(repository, clock=lambda: NOW)
    binding = draft(control)
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    assert repository.next_operation_sequence("tenant-a") == 1
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    repository._connection.close()

    recovered_repository = SQLiteWarehouseRepository(str(database_path))
    recovered = recovered_repository.load_live_operation("tenant-a", binding.binding_id)

    assert recovered == claimed_operation
    assert recovered_repository.next_operation_sequence("tenant-a") == 2


@pytest.mark.parametrize(
    ("status", "failure_classification"),
    (
        (WarehouseOperationStatus.SUCCEEDED, None),
        (
            WarehouseOperationStatus.FAILED,
            WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        ),
    ),
)
def test_terminal_operation_is_not_returned_as_live(
    status: WarehouseOperationStatus,
    failure_classification: WarehouseFailureClassification | None,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    running_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        phase=WarehouseOperationPhase.VALIDATING,
    )
    _record_operation_state(repository, running_operation)
    terminal = operation(
        provisioning,
        status=status,
        phase=(
            WarehouseOperationPhase.VALIDATED
            if status is WarehouseOperationStatus.SUCCEEDED
            else WarehouseOperationPhase.VALIDATING
        ),
        failure_classification=failure_classification,
        updated_at=LATER,
    )

    repository.save_operation(running_operation, terminal)

    assert repository.load_live_operation("tenant-a", binding.binding_id) is None


@pytest.mark.parametrize(
    ("operation_kind", "incomplete_phase"),
    (
        (WarehouseOperationKind.PROVISION, WarehouseOperationPhase.PROVIDER_CREATED),
        (WarehouseOperationKind.SUSPEND, WarehouseOperationPhase.CLAIMED),
        (WarehouseOperationKind.RESUME, WarehouseOperationPhase.RESUMED),
        (WarehouseOperationKind.RETIRE, WarehouseOperationPhase.CLAIMED),
    ),
)
def test_operation_success_requires_the_terminal_phase_for_its_kind(
    operation_kind: WarehouseOperationKind,
    incomplete_phase: WarehouseOperationPhase,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    running = operation(
        binding,
        operation_kind=operation_kind,
        status=WarehouseOperationStatus.RUNNING,
        phase=incomplete_phase,
        updated_at=LATER,
    )
    _record_operation_state(repository, running)
    succeeded = running.model_copy(
        update={
            "status": WarehouseOperationStatus.SUCCEEDED,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    with pytest.raises(WarehouseOperationConflictError, match="terminal phase"):
        repository.save_operation(running, succeeded)

    assert (
        repository.load_operation(binding.tenant_id, binding.binding_id, running.operation_id)
        == running
    )


@pytest.mark.parametrize(
    ("operation_kind", "terminal_phase"),
    (
        (WarehouseOperationKind.PROVISION, WarehouseOperationPhase.VALIDATED),
        (WarehouseOperationKind.SUSPEND, WarehouseOperationPhase.SUSPENDED),
        (WarehouseOperationKind.RESUME, WarehouseOperationPhase.VALIDATED),
        (WarehouseOperationKind.RETIRE, WarehouseOperationPhase.RETIRED),
    ),
)
def test_terminal_operation_success_replays_after_repository_reopen(
    operation_kind: WarehouseOperationKind,
    terminal_phase: WarehouseOperationPhase,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    succeeded = operation(
        binding,
        operation_kind=operation_kind,
        status=WarehouseOperationStatus.SUCCEEDED,
        phase=terminal_phase,
        updated_at=LATER,
    )
    _record_operation_state(repository, succeeded)
    repository.close()
    reopened = SQLiteWarehouseRepository(str(database_path))
    durable = reopened.load_operation(binding.tenant_id, binding.binding_id, succeeded.operation_id)

    reopened.save_operation(durable, durable)

    assert (
        reopened.load_operation(binding.tenant_id, binding.binding_id, succeeded.operation_id)
        == succeeded
    )


@pytest.mark.parametrize(
    ("operation_kind", "incomplete_phase"),
    (
        (WarehouseOperationKind.PROVISION, WarehouseOperationPhase.PROVIDER_CREATED),
        (WarehouseOperationKind.SUSPEND, WarehouseOperationPhase.CLAIMED),
        (WarehouseOperationKind.RESUME, WarehouseOperationPhase.RESUMED),
        (WarehouseOperationKind.RETIRE, WarehouseOperationPhase.CLAIMED),
    ),
)
def test_incomplete_operation_success_from_an_older_repository_fails_closed_on_replay(
    operation_kind: WarehouseOperationKind,
    incomplete_phase: WarehouseOperationPhase,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    impossible = operation(
        binding,
        operation_kind=operation_kind,
        status=WarehouseOperationStatus.SUCCEEDED,
        phase=incomplete_phase,
        updated_at=LATER,
    )
    repository._connection.execute(
        "INSERT INTO private_warehouse_operations "
        "(tenant_id, binding_id, binding_revision, operation_id, status, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            impossible.tenant_id,
            impossible.binding_id,
            impossible.binding_revision,
            impossible.operation_id,
            impossible.status.value,
            canonical_bytes(impossible),
        ),
    )
    repository._connection.commit()
    repository.close()
    reopened = SQLiteWarehouseRepository(str(database_path))

    with pytest.raises(WarehouseOperationConflictError, match="terminal phase"):
        reopened.load_operation(
            impossible.tenant_id,
            impossible.binding_id,
            impossible.operation_id,
        )


@pytest.mark.parametrize(
    (
        "stale_status",
        "stale_phase",
        "durable_status",
        "durable_phase",
        "failure_classification",
    ),
    (
        (
            WarehouseOperationStatus.CLAIMED,
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationStatus.RUNNING,
            WarehouseOperationPhase.CLAIMED,
            None,
        ),
        (
            WarehouseOperationStatus.RUNNING,
            WarehouseOperationPhase.VALIDATED,
            WarehouseOperationStatus.SUCCEEDED,
            WarehouseOperationPhase.VALIDATED,
            None,
        ),
        (
            WarehouseOperationStatus.RUNNING,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationStatus.FAILED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseFailureClassification.INTEGRITY_FAILURE,
        ),
    ),
)
def test_stale_operation_writer_cannot_regress_phase_or_reopen_terminal_state(
    tmp_path: Path,
    stale_status: WarehouseOperationStatus,
    stale_phase: WarehouseOperationPhase,
    durable_status: WarehouseOperationStatus,
    durable_phase: WarehouseOperationPhase,
    failure_classification: WarehouseFailureClassification | None,
) -> None:
    database_path = tmp_path / "warehouse.db"
    stale_repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(stale_repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    stale_repository.save(provisioning)
    stale_operation = operation(
        provisioning,
        status=stale_status,
        phase=stale_phase,
    )
    _record_operation_state(stale_repository, stale_operation)
    stale_snapshot = stale_repository.load_operation(
        "tenant-a", provisioning.binding_id, stale_operation.operation_id
    )
    current_repository = SQLiteWarehouseRepository(str(database_path))
    current_snapshot = current_repository.load_operation(
        "tenant-a", provisioning.binding_id, stale_operation.operation_id
    )
    durable_operation = PrivateWarehouseOperation.model_validate(
        {
            **current_snapshot.model_dump(),
            "status": durable_status,
            "phase": durable_phase,
            "failure_classification": failure_classification,
            "updated_at": LATER,
        }
    )
    current_repository.save_operation(current_snapshot, durable_operation)

    with pytest.raises(WarehouseOperationConflictError, match="durable state"):
        stale_repository.save_operation(stale_snapshot, stale_snapshot)

    assert (
        current_repository.load_operation(
            "tenant-a", provisioning.binding_id, stale_operation.operation_id
        )
        == durable_operation
    )


def test_re_reading_newer_operation_never_authorizes_stale_same_snapshot_replay() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    stale_snapshot = operation(provisioning)
    repository.claim_operation(stale_snapshot)
    running = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        updated_at=LATER,
    )
    repository.save_operation(stale_snapshot, running)
    assert (
        repository.load_operation(
            running.tenant_id,
            running.binding_id,
            running.operation_id,
        )
        == running
    )

    with pytest.raises(WarehouseOperationConflictError, match="durable state"):
        repository.save_operation(stale_snapshot, stale_snapshot)

    assert (
        repository.load_operation(
            running.tenant_id,
            running.binding_id,
            running.operation_id,
        )
        == running
    )


@pytest.mark.parametrize(
    ("durable_phase", "regressed_status", "regressed_phase"),
    (
        (
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationStatus.CLAIMED,
            WarehouseOperationPhase.CLAIMED,
        ),
        (
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationStatus.RUNNING,
            WarehouseOperationPhase.PROVIDER_CREATED,
        ),
    ),
)
def test_live_operation_update_rejects_status_or_phase_regression(
    durable_phase: WarehouseOperationPhase,
    regressed_status: WarehouseOperationStatus,
    regressed_phase: WarehouseOperationPhase,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    durable_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        phase=durable_phase,
        updated_at=LATER,
    )
    _record_operation_state(repository, durable_operation)
    regressed_operation = PrivateWarehouseOperation.model_validate(
        {
            **durable_operation.model_dump(),
            "status": regressed_status,
            "phase": regressed_phase,
        }
    )

    with pytest.raises(WarehouseOperationConflictError, match="operation update"):
        repository.save_operation(durable_operation, regressed_operation)

    assert (
        repository.load_operation(
            durable_operation.tenant_id,
            durable_operation.binding_id,
            durable_operation.operation_id,
        )
        == durable_operation
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"provider_resource_handle": "provider-replaced"},
        {
            "failure_classification": WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        },
        {"updated_at": NOW},
    ),
)
def test_live_operation_update_rejects_arbitrary_mutable_field_changes(
    updates: dict[str, object],
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    durable_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        phase=WarehouseOperationPhase.PROVIDER_CREATED,
        provider_resource_handle="provider-original",
        updated_at=LATER,
    )
    _record_operation_state(repository, durable_operation)
    changed_operation = durable_operation.model_copy(update=updates)

    with pytest.raises(WarehouseOperationConflictError, match="operation update"):
        repository.save_operation(durable_operation, changed_operation)

    assert (
        repository.load_operation(
            durable_operation.tenant_id,
            durable_operation.binding_id,
            durable_operation.operation_id,
        )
        == durable_operation
    )


def test_terminal_operation_and_binding_failure_commit_atomically() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    running = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        updated_at=LATER,
    )
    _record_operation_state(repository, running)
    failed_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.FAILED,
        failure_classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        updated_at=LATER + timedelta(minutes=1),
    )
    failed_binding = advance_binding(provisioning, WarehouseBindingState.FAILED)

    repository.record_terminal_operation_failure(
        running,
        failed_operation,
        failed_binding,
        expected_revision=provisioning.revision,
    )

    assert repository.load("tenant-a", binding.binding_id) == failed_binding
    assert (
        repository.load_operation("tenant-a", binding.binding_id, running.operation_id)
        == failed_operation
    )


def test_terminal_operation_and_binding_failure_roll_back_together() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    running = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        updated_at=LATER,
    )
    _record_operation_state(repository, running)
    failed_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.FAILED,
        failure_classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        updated_at=LATER + timedelta(minutes=1),
    )
    failed_binding = advance_binding(provisioning, WarehouseBindingState.FAILED)
    connection = repository._connection
    repository._connection = FailingConnection(
        connection,
        fail_after_prefix="INSERT INTO warehouse_bindings",
    )

    with pytest.raises(WarehousePersistenceError, match="terminal operation failure") as failure:
        repository.record_terminal_operation_failure(
            running,
            failed_operation,
            failed_binding,
            expected_revision=provisioning.revision,
        )

    repository._connection = connection
    assert "sensitive primary sqlite detail" not in str(failure.value)
    assert repository.load("tenant-a", binding.binding_id) == provisioning
    assert (
        repository.load_operation("tenant-a", binding.binding_id, running.operation_id) == running
    )


@pytest.mark.parametrize(
    ("expected_update", "failed_update"),
    (
        ({"status": WarehouseOperationStatus.CLAIMED}, {}),
        ({"operation_id": "wop-other"}, {"operation_id": "wop-other"}),
        (
            {},
            {
                "failure_classification": WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
            },
        ),
    ),
)
def test_atomic_terminal_failure_rejects_stale_identity_status_and_classification(
    expected_update: dict[str, object],
    failed_update: dict[str, object],
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    running = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        updated_at=LATER,
    )
    _record_operation_state(repository, running)
    expected = operation(
        provisioning,
        **{
            "status": WarehouseOperationStatus.RUNNING,
            "updated_at": LATER,
            **expected_update,
        },
    )
    failed_operation = operation(
        provisioning,
        **{
            "status": WarehouseOperationStatus.FAILED,
            "failure_classification": WarehouseFailureClassification.INTEGRITY_FAILURE,
            "updated_at": LATER + timedelta(minutes=1),
            **failed_update,
        },
    )
    failed_binding = advance_binding(provisioning, WarehouseBindingState.FAILED)

    with pytest.raises(WarehouseOperationConflictError):
        repository.record_terminal_operation_failure(
            expected,
            failed_operation,
            failed_binding,
            expected_revision=provisioning.revision,
        )

    assert repository.load("tenant-a", binding.binding_id) == provisioning
    assert (
        repository.load_operation("tenant-a", binding.binding_id, running.operation_id) == running
    )


def test_atomic_terminal_failure_rejects_a_stale_binding_revision() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    running = operation(
        provisioning,
        status=WarehouseOperationStatus.RUNNING,
        updated_at=LATER,
    )
    _record_operation_state(repository, running)
    failed_operation = operation(
        provisioning,
        status=WarehouseOperationStatus.FAILED,
        failure_classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        updated_at=LATER + timedelta(minutes=1),
    )
    failed_binding = advance_binding(provisioning, WarehouseBindingState.FAILED)

    with pytest.raises(WarehouseOperationConflictError):
        repository.record_terminal_operation_failure(
            running,
            failed_operation,
            failed_binding,
            expected_revision=provisioning.revision + 1,
        )

    assert repository.load("tenant-a", binding.binding_id) == provisioning
    assert (
        repository.load_operation("tenant-a", binding.binding_id, running.operation_id) == running
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"operation_kind": WarehouseOperationKind.RETIRE},
        {"engine_kind": EngineKind.CLICKHOUSE},
        {"started_at": NOW + timedelta(seconds=1), "updated_at": LATER},
    ),
)
def test_operation_update_refuses_changes_to_claimed_identity(
    updates: dict[str, object],
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    changed_values: dict[str, object] = {
        "status": WarehouseOperationStatus.RUNNING,
        "updated_at": LATER,
    }
    changed_values.update(updates)
    changed = operation(provisioning, **changed_values)

    with pytest.raises(WarehouseOperationConflictError, match="claimed identity"):
        repository.save_operation(claimed_operation, changed)

    assert (
        repository.load_operation("tenant-a", binding.binding_id, claimed_operation.operation_id)
        == claimed_operation
    )


def test_partial_unique_index_rejects_a_second_live_operation() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    first = operation(provisioning, operation_id="wop-first")
    repository.claim_operation(first)
    second = operation(provisioning, operation_id="wop-second")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        repository._connection.execute(
            "INSERT INTO private_warehouse_operations "
            "(tenant_id, binding_id, binding_revision, operation_id, status, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                second.tenant_id,
                second.binding_id,
                second.binding_revision,
                second.operation_id,
                second.status.value,
                canonical_bytes(second),
            ),
        )


def test_claim_refuses_an_operation_owned_by_an_old_binding_revision() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    validating = advance_binding(provisioning, WarehouseBindingState.VALIDATING)
    repository.save(validating)

    with pytest.raises(WarehouseOperationConflictError, match="binding revision"):
        repository.claim_operation(operation(provisioning))


def test_operation_load_is_tenant_qualified() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)

    assert repository.load_live_operation("tenant-b", binding.binding_id) is None
    with pytest.raises(KeyError, match="not recorded"):
        repository.load_operation("tenant-b", binding.binding_id, claimed_operation.operation_id)


def test_resources_round_trip_immutably_and_are_tenant_qualified() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)

    repository.record_resources((planned,))

    assert repository.load_resources("tenant-a", binding.binding_id) == (planned,)
    with pytest.raises(KeyError, match="warehouse binding was not found"):
        repository.load_resources("tenant-b", binding.binding_id)


def test_missing_and_foreign_bindings_have_the_same_non_disclosing_result() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)

    failures: list[tuple[object, ...]] = []
    for tenant_id, binding_id in (
        ("tenant-b", binding.binding_id),
        ("tenant-a", "whb-missing"),
    ):
        with pytest.raises(KeyError) as resource_failure:
            repository.load_resources(tenant_id, binding_id)
        failures.append(resource_failure.value.args)

        with pytest.raises(KeyError) as current_binding_failure:
            repository.claim_operation(
                operation(provisioning, tenant_id=tenant_id, binding_id=binding_id)
            )
        failures.append(current_binding_failure.value.args)

    assert failures == [
        ("warehouse binding was not found",),
        ("warehouse binding was not found",),
        ("warehouse binding was not found",),
        ("warehouse binding was not found",),
    ]


def test_service_missing_and_foreign_bindings_have_the_same_non_disclosing_result() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=lambda: NOW)
    binding = draft(control)

    failures: list[tuple[object, ...]] = []
    for tenant_id, binding_id in (
        ("tenant-b", binding.binding_id),
        ("tenant-a", "whb-missing"),
    ):
        with pytest.raises(KeyError) as failure:
            control.get(tenant_id, binding_id)
        failures.append(failure.value.args)

    assert failures == [
        ("warehouse binding was not found",),
        ("warehouse binding was not found",),
    ]


def test_replaying_the_same_resource_record_does_not_duplicate_it() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)

    repository.record_resources((planned,))
    repository.record_resources((planned,))

    assert repository.load_resources("tenant-a", binding.binding_id) == (planned,)


def test_resource_save_requires_the_exact_recorded_parent_operation() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    orphan_operation = operation(provisioning)
    orphan_restore = resource(orphan_operation)

    with pytest.raises(WarehousePersistenceError, match="parent operation"):
        repository.record_resources((orphan_restore,))

    assert repository.load_resources("tenant-a", binding.binding_id) == ()


def test_resource_updates_preserve_identity_and_replace_the_payload() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)
    repository.record_resources((planned,))
    created = resource(
        claimed_operation,
        creation_state=WarehouseResourceCreationState.CREATED,
        updated_at=LATER,
    )

    created = repository.save_resource(created)

    assert repository.load_resources("tenant-a", binding.binding_id) == (created,)


def test_stale_resource_snapshot_cannot_regress_created_and_complete_state() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)
    repository.record_resources((planned,))
    terminal = planned.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "cleanup_status": WarehouseResourceCleanupStatus.COMPLETE,
            "updated_at": LATER,
        }
    )
    terminal = repository.save_resource(terminal)
    stale = planned.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.AMBIGUOUS,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    with pytest.raises(WarehousePersistenceError, match="transition"):
        repository.save_resource(stale)

    assert repository.load_resources(binding.tenant_id, binding.binding_id) == (terminal,)


def test_resource_batch_rolls_back_a_valid_update_when_a_stale_snapshot_regresses() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    first = resource(claimed_operation)
    second = resource(
        claimed_operation,
        resource_id="whr-second",
        provider_resource_handle="restore-container-second",
    )
    repository.record_resources((first, second))
    terminal_second = second.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "cleanup_status": WarehouseResourceCleanupStatus.COMPLETE,
            "updated_at": LATER,
        }
    )
    terminal_second = repository.save_resource(terminal_second)
    created_first = first.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )
    stale_second = second.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.AMBIGUOUS,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    with pytest.raises(WarehousePersistenceError, match="transition"):
        repository.save_resources((created_first, stale_second))

    durable_by_id = {
        item.resource_id: item
        for item in repository.load_resources(binding.tenant_id, binding.binding_id)
    }
    assert durable_by_id == {
        first.resource_id: first,
        terminal_second.resource_id: terminal_second,
    }


def test_two_repositories_cannot_commit_a_stale_resource_snapshot(tmp_path: Path) -> None:
    database_path = tmp_path / "warehouse.sqlite"
    winning_repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(winning_repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    winning_repository.save(provisioning)
    claimed_operation = operation(provisioning)
    winning_repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)
    winning_repository.record_resources((planned,))
    stale_repository = SQLiteWarehouseRepository(str(database_path))
    stale = stale_repository.load_resources(binding.tenant_id, binding.binding_id)[0]
    terminal = planned.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "cleanup_status": WarehouseResourceCleanupStatus.COMPLETE,
            "updated_at": LATER,
        }
    )
    terminal = winning_repository.save_resource(terminal)
    stale_update = stale.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.AMBIGUOUS,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    with pytest.raises(WarehousePersistenceError, match="transition"):
        stale_repository.save_resource(stale_update)

    stale_repository.close()
    winning_repository.close()
    reopened = SQLiteWarehouseRepository(str(database_path))
    assert reopened.load_resources(binding.tenant_id, binding.binding_id) == (terminal,)


def test_two_repositories_cannot_replace_a_cleanup_failure_from_a_stale_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.sqlite"
    winning_repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(winning_repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    winning_repository.save(provisioning)
    claimed_operation = operation(provisioning)
    winning_repository.claim_operation(claimed_operation)
    failed = resource(
        claimed_operation,
        creation_state=WarehouseResourceCreationState.CREATED,
        cleanup_status=WarehouseResourceCleanupStatus.FAILED,
        cleanup_failure_classification=(WarehouseFailureClassification.TRANSIENT_UNAVAILABLE),
    )
    winning_repository.record_resources((failed,))
    stale_repository = SQLiteWarehouseRepository(str(database_path))
    winning_snapshot = winning_repository.load_resources(binding.tenant_id, binding.binding_id)[0]
    stale_snapshot = stale_repository.load_resources(binding.tenant_id, binding.binding_id)[0]
    permanent_failure = winning_snapshot.model_copy(
        update={
            "cleanup_failure_classification": (
                WarehouseFailureClassification.PERMANENT_CONFIGURATION
            ),
            "updated_at": LATER,
        }
    )
    stale_failure = stale_snapshot.model_copy(
        update={
            "cleanup_failure_classification": (WarehouseFailureClassification.INTEGRITY_FAILURE),
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    permanent_failure = winning_repository.save_resource(permanent_failure)
    with pytest.raises(WarehousePersistenceError, match="expected snapshot"):
        stale_repository.save_resource(stale_failure)

    assert winning_repository.load_resources(binding.tenant_id, binding.binding_id) == (
        permanent_failure,
    )


def test_resource_batch_update_is_atomic_when_one_identity_is_invalid() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    first = resource(claimed_operation)
    second = resource(
        claimed_operation,
        resource_id="wrs-second",
        provider_resource_handle="provider-second",
    )
    repository.record_resources((first, second))
    created_first = first.model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "updated_at": LATER,
        }
    )
    invalid_second = second.model_copy(
        update={
            "provider_resource_handle": "changed-provider-second",
            "creation_state": WarehouseResourceCreationState.CREATED,
            "updated_at": LATER,
        }
    )

    with pytest.raises(WarehousePersistenceError, match="planned identity"):
        repository.save_resources((created_first, invalid_second))

    assert repository.load_resources("tenant-a", binding.binding_id) == (first, second)


def test_restore_resource_reopening_is_atomic_and_durable(tmp_path: Path) -> None:
    database_path = tmp_path / "warehouse.sqlite"
    repository = SQLiteWarehouseRepository(str(database_path))
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    completed = tuple(
        resource(
            claimed_operation,
            resource_id=f"whr-restore-{index}",
            resource_kind=resource_kind,
            provider_resource_handle=f"restore-handle-{index}",
            creation_state=WarehouseResourceCreationState.CREATED,
            cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
            updated_at=LATER,
        )
        for index, resource_kind in enumerate(
            (
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                WarehouseResourceKind.RESTORE_CONTAINER,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_DATA_VOLUME,
            )
        )
    )
    repository.record_resources(completed)
    reopened_at = LATER + timedelta(minutes=1)

    reopened = repository.reopen_resources_for_recreation(
        completed,
        reopened_at=reopened_at,
    )

    assert len(reopened) == 5
    assert all(
        item.creation_state is WarehouseResourceCreationState.PLANNED
        and item.cleanup_status is WarehouseResourceCleanupStatus.PENDING
        and item.cleanup_failure_classification is None
        and item.updated_at == reopened_at
        for item in reopened
    )
    repository.close()
    reopened_repository = SQLiteWarehouseRepository(str(database_path))
    assert reopened_repository.load_resources("tenant-a", binding.binding_id) == reopened

    with pytest.raises(WarehousePersistenceError, match="timestamp"):
        reopened_repository.save_resource(completed[0])

    assert reopened_repository.load_resources("tenant-a", binding.binding_id) == reopened


@pytest.mark.parametrize(
    "resource_kind",
    (
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.BACKUP_STAGING_FILE,
    ),
)
def test_ephemeral_resource_reopening_is_explicit_and_durable(
    resource_kind: WarehouseResourceKind,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    completed = resource(
        claimed_operation,
        resource_id="whr-ephemeral",
        resource_kind=resource_kind,
        provider_resource_handle="ephemeral-file",
        creation_state=WarehouseResourceCreationState.CREATED,
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
        updated_at=LATER,
    )
    repository.record_resources((completed,))
    reopened_at = LATER + timedelta(minutes=1)

    reopened = repository.reopen_resources_for_recreation(
        (completed,),
        reopened_at=reopened_at,
    )

    assert reopened == (
        completed.model_copy(
            update={
                "creation_state": WarehouseResourceCreationState.PLANNED,
                "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
                "state_revision": completed.state_revision + 1,
                "updated_at": reopened_at,
            }
        ),
    )
    assert repository.load_resources("tenant-a", binding.binding_id) == reopened


def test_restore_resource_reopening_rolls_back_every_record_for_a_stale_snapshot() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    first = resource(
        claimed_operation,
        resource_id="whr-restore-first",
        provider_resource_handle="restore-first",
        creation_state=WarehouseResourceCreationState.CREATED,
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
        updated_at=LATER,
    )
    second = resource(
        claimed_operation,
        resource_id="whr-restore-second",
        provider_resource_handle="restore-second",
        creation_state=WarehouseResourceCreationState.CREATED,
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
        updated_at=LATER,
    )
    repository.record_resources((first, second))
    stale_second = second.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )

    with pytest.raises(WarehousePersistenceError, match="durable state changed"):
        repository.reopen_resources_for_recreation(
            (first, stale_second),
            reopened_at=LATER + timedelta(minutes=1),
        )

    assert repository.load_resources("tenant-a", binding.binding_id) == (first, second)


def test_restore_resource_reopening_rejects_a_retained_resource() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    retained = resource(
        claimed_operation,
        resource_id="whr-retained-restore",
        creation_state=WarehouseResourceCreationState.CREATED,
        cleanup_status=WarehouseResourceCleanupStatus.RETAINED,
        updated_at=LATER,
    )
    repository.record_resources((retained,))

    with pytest.raises(WarehousePersistenceError, match="retained"):
        repository.reopen_resources_for_recreation(
            (retained,),
            reopened_at=LATER + timedelta(minutes=1),
        )

    assert repository.load_resources("tenant-a", binding.binding_id) == (retained,)


@pytest.mark.parametrize(
    "updates",
    (
        {"resource_kind": WarehouseResourceKind.BACKUP_ARTIFACT},
        {"provider_resource_handle": "different-provider-handle"},
        {"parent_resource_handle": "different-parent-handle"},
        {"created_at": NOW + timedelta(seconds=1), "updated_at": LATER},
        {"retention_deadline": NOW + timedelta(days=60)},
    ),
)
def test_resource_update_refuses_changes_to_planned_identity(
    updates: dict[str, object],
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    binding = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioning = advance_binding(binding, WarehouseBindingState.PROVISIONING)
    repository.save(provisioning)
    claimed_operation = operation(provisioning)
    repository.claim_operation(claimed_operation)
    planned = resource(claimed_operation)
    repository.record_resources((planned,))
    changed_values: dict[str, object] = {
        "creation_state": WarehouseResourceCreationState.CREATED,
        "updated_at": LATER,
    }
    changed_values.update(updates)
    changed = resource(claimed_operation, **changed_values)

    with pytest.raises(WarehousePersistenceError, match="planned identity"):
        repository.save_resource(changed)

    assert repository.load_resources("tenant-a", binding.binding_id) == (planned,)


def test_draft_abandonment_requires_a_null_provisioned_time() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioned_draft = WarehouseBinding.model_validate(
        {
            **source.model_dump(),
            "revision": source.revision + 1,
            "provisioned_at": NOW,
            "updated_at": LATER,
        }
    )
    repository.save(provisioned_draft)
    retired = advance_binding(provisioned_draft, WarehouseBindingState.RETIRED)

    with pytest.raises(WarehouseValidationConflictError, match="provisioned"):
        repository.abandon_draft(
            retired,
            expected_revision=provisioned_draft.revision,
        )

    assert repository.load("tenant-a", source.binding_id) == provisioned_draft


def test_draft_abandonment_checks_the_current_provisioned_time_not_only_the_candidate() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    provisioned_draft = WarehouseBinding.model_validate(
        {
            **source.model_dump(),
            "revision": source.revision + 1,
            "provisioned_at": NOW,
            "updated_at": LATER,
        }
    )
    repository.save(provisioned_draft)
    retired = WarehouseBinding.model_validate(
        {
            **provisioned_draft.model_dump(),
            "lifecycle_state": WarehouseBindingState.RETIRED,
            "revision": provisioned_draft.revision + 1,
            "provisioned_at": None,
        }
    )

    with pytest.raises(WarehouseValidationConflictError, match="provisioned"):
        repository.abandon_draft(retired, expected_revision=provisioned_draft.revision)

    assert repository.load("tenant-a", source.binding_id) == provisioned_draft


def test_draft_abandonment_refuses_a_candidate_that_changes_the_current_binding() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    retired = WarehouseBinding.model_validate(
        {
            **advance_binding(source, WarehouseBindingState.RETIRED).model_dump(),
            "region": "different-region",
        }
    )

    with pytest.raises(WarehouseValidationConflictError, match="current draft"):
        repository.abandon_draft(retired, expected_revision=source.revision)

    assert repository.load("tenant-a", source.binding_id) == source


@pytest.mark.parametrize(
    "status",
    (WarehouseOperationStatus.SUCCEEDED, WarehouseOperationStatus.FAILED),
)
def test_draft_abandonment_rejects_every_terminal_operation_status(
    status: WarehouseOperationStatus,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    terminal_operation = operation(
        source,
        status=status,
        phase=WarehouseOperationPhase.VALIDATED,
        failure_classification=(
            WarehouseFailureClassification.PERMANENT_CONFIGURATION
            if status is WarehouseOperationStatus.FAILED
            else None
        ),
    )
    _record_operation_state(repository, terminal_operation)
    retired = advance_binding(source, WarehouseBindingState.RETIRED)

    with pytest.raises(WarehouseValidationConflictError, match="operation"):
        repository.abandon_draft(retired, expected_revision=source.revision)

    assert repository.load("tenant-a", source.binding_id) == source


def test_draft_abandonment_rejects_any_recorded_resource() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    warehouse_operation = operation(
        source,
        status=WarehouseOperationStatus.SUCCEEDED,
        phase=WarehouseOperationPhase.VALIDATED,
    )
    _record_operation_state(repository, warehouse_operation)
    warehouse_resource = resource(
        warehouse_operation,
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
    )
    repository.record_resources((warehouse_resource,))
    retired = advance_binding(source, WarehouseBindingState.RETIRED)

    with pytest.raises(WarehouseValidationConflictError, match="resource"):
        repository.abandon_draft(retired, expected_revision=source.revision)

    assert repository.load_resources("tenant-a", source.binding_id) == (warehouse_resource,)
    assert repository.load("tenant-a", source.binding_id) == source


def test_draft_abandonment_rolls_back_an_insert_failure() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    source = draft(WarehouseControlService(repository, clock=lambda: NOW))
    retired = advance_binding(source, WarehouseBindingState.RETIRED)
    repository._connection = FailingConnection(
        repository._connection,
        fail_after_prefix="INSERT INTO warehouse_bindings",
    )

    with pytest.raises(WarehousePersistenceError, match="draft abandonment") as failure:
        repository.abandon_draft(retired, expected_revision=source.revision)

    assert "sensitive primary sqlite detail" not in str(failure.value)
    assert repository.load("tenant-a", source.binding_id) == source


def test_concurrent_draft_abandonment_and_operation_resource_claim_are_serialized(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "warehouse.db"
    setup_repository = SQLiteWarehouseRepository(str(database_path))
    source = draft(WarehouseControlService(setup_repository, clock=lambda: NOW))
    setup_repository.close()
    retired = advance_binding(source, WarehouseBindingState.RETIRED)
    barrier = Barrier(2)
    outcomes: Queue[tuple[str, object]] = Queue()

    def abandon() -> None:
        repository = SQLiteWarehouseRepository(str(database_path))
        barrier.wait(timeout=5)
        try:
            repository.abandon_draft(retired, expected_revision=source.revision)
        except Exception as error:
            outcomes.put(("abandon", error))
        else:
            outcomes.put(("abandon", None))
        finally:
            repository.close()

    def claim_and_record_resource() -> None:
        repository = SQLiteWarehouseRepository(str(database_path))
        warehouse_operation = operation(source)
        warehouse_resource = resource(warehouse_operation)
        barrier.wait(timeout=5)
        claim_error: Exception | None = None
        resource_error: Exception | None = None
        try:
            repository.claim_operation(warehouse_operation)
        except Exception as error:
            claim_error = error
        try:
            repository.record_resources((warehouse_resource,))
        except Exception as error:
            resource_error = error
        outcomes.put(("operation", (claim_error, resource_error)))
        repository.close()

    abandonment_thread = Thread(target=abandon)
    operation_thread = Thread(target=claim_and_record_resource)
    abandonment_thread.start()
    operation_thread.start()
    abandonment_thread.join(timeout=5)
    operation_thread.join(timeout=5)

    assert not abandonment_thread.is_alive()
    assert not operation_thread.is_alive()
    results = dict(outcomes.get(timeout=1) for _ in range(2))
    repository = SQLiteWarehouseRepository(str(database_path))
    current = repository.load(source.tenant_id, source.binding_id)
    claim_error, resource_error = results["operation"]
    if current == retired:
        assert results["abandon"] is None
        assert isinstance(claim_error, WarehouseOperationConflictError)
        assert isinstance(resource_error, WarehousePersistenceError)
        assert repository.load_resources(source.tenant_id, source.binding_id) == ()
    else:
        assert current == source
        assert isinstance(results["abandon"], WarehouseValidationConflictError)
        assert claim_error is None
        assert resource_error is None
        assert repository.load_resources(source.tenant_id, source.binding_id) == (
            resource(operation(source)),
        )


def test_initial_validation_records_binding_restore_and_evidence_atomically() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    evidence = validation_evidence(source, restore)

    repository.record_initial_validation_operation(
        ready,
        evidence,
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )

    assert repository.load("tenant-a", source.binding_id) == ready
    assert repository._connection.execute(
        "SELECT payload FROM warehouse_validation_evidence "
        "WHERE tenant_id = ? AND binding_id = ? AND evidence_id = ?",
        ("tenant-a", source.binding_id, evidence.evidence_id),
    ).fetchone() == (canonical_bytes(evidence),)
    assert repository._connection.execute(
        "SELECT payload FROM warehouse_restore_verifications "
        "WHERE tenant_id = ? AND binding_id = ? AND verification_id = ?",
        ("tenant-a", source.binding_id, restore.verification_id),
    ).fetchone() == (canonical_bytes(restore),)
    assert (
        repository._connection.execute(
            "SELECT payload FROM warehouse_validation_evidence "
            "WHERE tenant_id = ? AND binding_id = ?",
            ("tenant-b", source.binding_id),
        ).fetchone()
        is None
    )


def test_validated_engine_evidence_read_returns_the_recorded_authority() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    evidence = validation_evidence(source, restore)
    repository.record_initial_validation_operation(
        ready,
        evidence,
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    control = WarehouseControlService(repository, clock=lambda: NOW)

    observed = control.get_validated_engine_evidence(
        source.tenant_id,
        source.binding_id,
        expected_revision=ready.revision,
    )

    assert observed == evidence


@pytest.mark.parametrize(
    ("tenant_id", "binding_id"),
    (("tenant-b", "existing"), ("tenant-a", "missing")),
)
def test_validated_engine_evidence_read_hides_binding_existence(
    tenant_id: str,
    binding_id: str,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    repository.record_initial_validation_operation(
        ready,
        validation_evidence(source, restore),
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    control = WarehouseControlService(repository, clock=lambda: NOW)
    requested_binding_id = source.binding_id if binding_id == "existing" else binding_id

    with pytest.raises(KeyError, match="validated engine evidence was not found"):
        control.get_validated_engine_evidence(
            tenant_id,
            requested_binding_id,
            expected_revision=ready.revision,
        )


def test_validated_engine_evidence_read_rejects_a_stale_binding_revision() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    repository.record_initial_validation_operation(
        ready,
        validation_evidence(source, restore),
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    control = WarehouseControlService(repository, clock=lambda: NOW)

    with pytest.raises(ValueError, match="binding revision is stale"):
        control.get_validated_engine_evidence(
            source.tenant_id,
            source.binding_id,
            expected_revision=source.revision,
        )


def test_validated_engine_evidence_read_rejects_stale_recorded_evidence() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    repository.record_initial_validation_operation(
        ready,
        validation_evidence(source, restore),
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    suspended = advance_binding(ready, WarehouseBindingState.SUSPENDED)
    resumed = advance_binding(suspended, WarehouseBindingState.READY)
    repository.save(suspended)
    repository.save(resumed)
    control = WarehouseControlService(repository, clock=lambda: NOW)

    with pytest.raises(WarehouseAdmissionError, match="validated engine evidence is stale"):
        control.get_validated_engine_evidence(
            source.tenant_id,
            source.binding_id,
            expected_revision=resumed.revision,
        )


def test_validated_engine_evidence_read_requires_a_ready_binding() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    repository.record_initial_validation_operation(
        ready,
        validation_evidence(source, restore),
        restore,
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    suspended = advance_binding(ready, WarehouseBindingState.SUSPENDED)
    repository.save(suspended)
    control = WarehouseControlService(repository, clock=lambda: NOW)

    with pytest.raises(
        WarehouseAdmissionError,
        match="validated engine evidence requires a ready binding",
    ):
        control.get_validated_engine_evidence(
            source.tenant_id,
            source.binding_id,
            expected_revision=suspended.revision,
        )


@pytest.mark.parametrize("kind", ("resume", "retirement"))
def test_single_evidence_admission_records_binding_and_evidence_atomically(kind: str) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, kind)
    source = fixture.source
    terminal = fixture.terminal

    if kind == "resume":
        evidence = resume_validation_evidence(source)
        repository.record_resume_validation_operation(
            terminal,
            evidence,
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )
        table = "warehouse_resume_validation_evidence"
    else:
        evidence = retirement_evidence(source)
        repository.record_retirement_operation(
            terminal,
            evidence,
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )
        table = "warehouse_retirement_evidence"

    assert repository.load("tenant-a", source.binding_id) == terminal
    assert repository._connection.execute(
        f"SELECT payload FROM {table} WHERE tenant_id = ? AND binding_id = ?",
        ("tenant-a", source.binding_id),
    ).fetchone() == (canonical_bytes(evidence),)
    assert (
        repository._connection.execute(
            f"SELECT payload FROM {table} WHERE tenant_id = ? AND binding_id = ?",
            ("tenant-b", source.binding_id),
        ).fetchone()
        is None
    )


@pytest.mark.parametrize("kind", ("resume", "retirement"))
@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"evidence_id": ""}, "evidence_id"),
        ({"tenant_id": "tenant-b"}, "tenant"),
        ({"binding_id": "whb-other"}, "binding"),
        ({"binding_revision": 99}, "revision"),
    ),
)
def test_single_evidence_admission_rejects_identity_or_revision_mismatch(
    kind: str, updates: dict[str, object], message: str
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, kind)
    source = fixture.source
    terminal = fixture.terminal

    with pytest.raises(WarehouseValidationConflictError, match=message):
        if kind == "resume":
            repository.record_resume_validation_operation(
                terminal,
                resume_validation_evidence(source, **updates),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )
        else:
            repository.record_retirement_operation(
                terminal,
                retirement_evidence(source, **updates),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )

    assert repository.load("tenant-a", source.binding_id) == source


@pytest.mark.parametrize(
    ("evidence_updates", "restore_updates", "message"),
    (
        ({"evidence_id": ""}, {}, "evidence_id"),
        ({"tenant_id": "tenant-b"}, {}, "tenant"),
        ({"binding_id": "whb-other"}, {}, "binding"),
        ({"binding_revision": 2}, {}, "revision"),
        ({}, {"tenant_id": "tenant-b"}, "tenant"),
        ({}, {"binding_id": "whb-other"}, "binding"),
        ({}, {"binding_revision": 2}, "revision"),
        ({"backup_artifact_digest": "f" * 64}, {}, "backup"),
        ({"restore_verification_digest": "f" * 64}, {}, "restore"),
        ({"principal_profile_digest": "f" * 64}, {}, "principal"),
    ),
)
def test_initial_validation_rejects_identity_revision_and_digest_mismatch(
    evidence_updates: dict[str, object],
    restore_updates: dict[str, object],
    message: str,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source, **restore_updates)
    evidence = validation_evidence(source, restore, **evidence_updates)

    with pytest.raises(WarehouseValidationConflictError, match=message):
        repository.record_initial_validation_operation(
            ready,
            evidence,
            restore,
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert repository.load("tenant-a", source.binding_id) == source


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"resource_inventory_digest": "f" * 64}, "inventory"),
        ({"cleanup_disposition_digest": "f" * 64}, "cleanup disposition"),
        ({"retention_policy_digest": "f" * 64}, "retention policy"),
        ({"completed_resource_count": 2}, "count"),
        ({"retained_resource_count": 2}, "count"),
        ({"cleanup_failed_resource_count": 2}, "count"),
    ),
)
def test_retirement_admission_rejects_every_resource_snapshot_mismatch(
    updates: dict[str, object], message: str
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    resources = recorded_terminal_resources(repository, source, fixture.expected_operation)
    retired = fixture.terminal

    with pytest.raises(WarehouseValidationConflictError, match=message):
        repository.record_retirement_operation(
            retired,
            retirement_evidence(source, resources, **updates),
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert repository.load("tenant-a", source.binding_id) == source


@pytest.mark.parametrize("changed_field", ("retention_deadline", "cleanup_classification"))
def test_retirement_snapshot_binds_retention_deadlines_and_cleanup_classifications(
    changed_field: str,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    resources = recorded_terminal_resources(repository, source, fixture.expected_operation)
    changed_resources = list(resources)
    if changed_field == "retention_deadline":
        changed_resources[0] = PrivateWarehouseResource.model_validate(
            {
                **resources[0].model_dump(),
                "retention_deadline": resources[0].retention_deadline + timedelta(days=1),
            }
        )
        message = "retention policy"
    else:
        changed_resources[2] = PrivateWarehouseResource.model_validate(
            {
                **resources[2].model_dump(),
                "cleanup_failure_classification": (
                    WarehouseFailureClassification.PERMANENT_CONFIGURATION
                ),
            }
        )
        message = "cleanup disposition"
    retired = fixture.terminal

    with pytest.raises(WarehouseValidationConflictError, match=message):
        repository.record_retirement_operation(
            retired,
            retirement_evidence(source, tuple(changed_resources)),
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert repository.load("tenant-a", source.binding_id) == source


def test_retirement_admission_rejects_a_nonterminal_resource_inside_the_transaction() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    warehouse_operation = fixture.expected_operation
    pending = resource(warehouse_operation)
    repository.record_resources((pending,))
    retired = fixture.terminal

    with pytest.raises(WarehouseValidationConflictError, match="terminal"):
        repository.record_retirement_operation(
            retired,
            retirement_evidence(source, (pending,)),
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert repository.load("tenant-a", source.binding_id) == source


@pytest.mark.parametrize("write_kind", ("update", "insert"))
def test_retired_binding_refuses_late_resource_ledger_writes(write_kind: str) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    resources = recorded_terminal_resources(repository, source, fixture.expected_operation)
    retired = fixture.terminal
    repository.record_retirement_operation(
        retired,
        retirement_evidence(source, resources),
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )

    with pytest.raises(WarehousePersistenceError, match="retired"):
        if write_kind == "update":
            repository.save_resource(
                PrivateWarehouseResource.model_validate(
                    {
                        **resources[0].model_dump(),
                        "cleanup_status": WarehouseResourceCleanupStatus.RETAINED,
                        "updated_at": LATER + timedelta(seconds=1),
                    }
                )
            )
        else:
            repository.record_resources(
                (
                    resource(
                        fixture.succeeded_operation,
                        resource_id="whr-late",
                        provider_resource_handle="late-resource",
                    ),
                )
            )

    assert repository.load_resources("tenant-a", source.binding_id) == resources


def test_retired_retained_resource_records_truthful_deletion_failure_without_reopening() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    resources = recorded_terminal_resources(repository, source, fixture.expected_operation)
    retained = resources[1]
    retired = fixture.terminal
    repository.record_retirement_operation(
        retired,
        retirement_evidence(source, resources),
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    failed = retained.model_copy(
        update={
            "cleanup_status": WarehouseResourceCleanupStatus.FAILED,
            "cleanup_failure_classification": (
                WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
            ),
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    failed = repository.save_resource(failed)

    durable_by_id = {
        item.resource_id: item
        for item in repository.load_resources(source.tenant_id, source.binding_id)
    }
    assert durable_by_id[failed.resource_id] == failed
    assert durable_by_id[resources[0].resource_id] == resources[0]
    assert durable_by_id[resources[2].resource_id] == resources[2]

    reopened = failed.model_copy(
        update={
            "cleanup_status": WarehouseResourceCleanupStatus.RETAINED,
            "cleanup_failure_classification": None,
            "updated_at": failed.updated_at + timedelta(seconds=1),
        }
    )
    with pytest.raises(WarehousePersistenceError, match="retired"):
        repository.save_resource(reopened)

    assert {
        item.resource_id: item
        for item in repository.load_resources(source.tenant_id, source.binding_id)
    } == durable_by_id


def test_retired_resource_cleanup_retry_converges_from_failed_to_complete() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "retirement")
    source = fixture.source
    resources = recorded_terminal_resources(repository, source, fixture.expected_operation)
    retained = resources[1]
    retired = fixture.terminal
    repository.record_retirement_operation(
        retired,
        retirement_evidence(source, resources),
        fixture.expected_operation,
        fixture.succeeded_operation,
        expected_revision=source.revision,
    )
    failed = repository.save_resource(
        retained.model_copy(
            update={
                "cleanup_status": WarehouseResourceCleanupStatus.FAILED,
                "cleanup_failure_classification": (
                    WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
                ),
                "updated_at": LATER + timedelta(seconds=1),
            }
        )
    )
    completed = failed.model_copy(
        update={
            "cleanup_status": WarehouseResourceCleanupStatus.COMPLETE,
            "cleanup_failure_classification": None,
            "updated_at": failed.updated_at + timedelta(seconds=1),
        }
    )

    completed = repository.save_resource(completed)

    durable_by_id = {
        item.resource_id: item
        for item in repository.load_resources(source.tenant_id, source.binding_id)
    }
    assert durable_by_id[completed.resource_id] == completed
    assert durable_by_id[resources[0].resource_id] == resources[0]
    assert durable_by_id[resources[2].resource_id] == resources[2]


@pytest.mark.parametrize("kind", ("initial", "resume", "retirement"))
def test_evidence_admission_revision_races_use_the_validation_conflict_taxonomy(
    kind: str,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, kind)
    source = fixture.source
    terminal = fixture.terminal
    repository.save(terminal)

    with pytest.raises(WarehouseValidationConflictError, match="revision conflict"):
        if kind == "initial":
            restore = restore_verification(source)
            repository.record_initial_validation_operation(
                terminal,
                validation_evidence(source, restore),
                restore,
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )
        elif kind == "resume":
            repository.record_resume_validation_operation(
                terminal,
                resume_validation_evidence(source),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )
        else:
            repository.record_retirement_operation(
                terminal,
                retirement_evidence(source),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )


@pytest.mark.parametrize(
    "fail_after_prefix",
    (
        "INSERT INTO warehouse_bindings",
        "INSERT INTO warehouse_validation_evidence",
        "INSERT INTO warehouse_restore_verifications",
    ),
)
def test_initial_validation_rolls_back_after_every_inserted_row(
    fail_after_prefix: str,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    evidence = validation_evidence(source, restore)
    repository._connection = FailingConnection(
        repository._connection, fail_after_prefix=fail_after_prefix
    )

    with pytest.raises(WarehousePersistenceError, match="initial validation") as failure:
        repository.record_initial_validation_operation(
            ready,
            evidence,
            restore,
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert "sensitive primary sqlite detail" not in str(failure.value)
    assert repository.load("tenant-a", source.binding_id) == source
    assert (
        repository.load_live_operation(source.tenant_id, source.binding_id)
        == fixture.expected_operation
    )
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_validation_evidence"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_resume_validation_evidence"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_restore_verifications"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_retirement_evidence"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("kind", "fail_after_prefix"),
    (
        ("resume", "INSERT INTO warehouse_bindings"),
        ("resume", "INSERT INTO warehouse_resume_validation_evidence"),
        ("retirement", "INSERT INTO warehouse_bindings"),
        ("retirement", "INSERT INTO warehouse_retirement_evidence"),
    ),
)
def test_single_evidence_admission_rolls_back_after_every_inserted_row(
    kind: str, fail_after_prefix: str
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, kind)
    source = fixture.source
    terminal = fixture.terminal
    repository._connection = FailingConnection(
        repository._connection, fail_after_prefix=fail_after_prefix
    )

    with pytest.raises(WarehousePersistenceError, match=kind):
        if kind == "resume":
            repository.record_resume_validation_operation(
                terminal,
                resume_validation_evidence(source),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )
        else:
            repository.record_retirement_operation(
                terminal,
                retirement_evidence(source),
                fixture.expected_operation,
                fixture.succeeded_operation,
                expected_revision=source.revision,
            )

    assert repository.load("tenant-a", source.binding_id) == source
    assert (
        repository.load_live_operation(source.tenant_id, source.binding_id)
        == fixture.expected_operation
    )
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_validation_evidence"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_resume_validation_evidence"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_restore_verifications"
    ).fetchone() == (0,)
    assert repository._connection.execute(
        "SELECT COUNT(*) FROM warehouse_retirement_evidence"
    ).fetchone() == (0,)


def test_rollback_failure_does_not_replace_the_primary_persistence_error() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    fixture = stable_admission_fixture(repository, "initial")
    source = fixture.source
    ready = fixture.terminal
    restore = restore_verification(source)
    evidence = validation_evidence(source, restore)
    repository._connection = FailingConnection(
        repository._connection,
        fail_after_prefix="INSERT INTO warehouse_bindings",
        rollback_error=sqlite3.OperationalError("rollback must not replace primary"),
    )

    with pytest.raises(WarehousePersistenceError, match="initial validation") as failure:
        repository.record_initial_validation_operation(
            ready,
            evidence,
            restore,
            fixture.expected_operation,
            fixture.succeeded_operation,
            expected_revision=source.revision,
        )

    assert "rollback must not replace primary" not in str(failure.value)


def test_close_failure_does_not_replace_repository_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        repository_module.sqlite3,
        "connect",
        lambda _database: InitializationFailureConnection(connection),
    )

    with pytest.raises(WarehousePersistenceError, match="initialize") as failure:
        SQLiteWarehouseRepository(":memory:")

    assert "close must not replace" not in str(failure.value)
    assert "sensitive initialization detail" not in str(failure.value)


def test_connect_failure_is_wrapped_without_exposing_sqlite_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(_database: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError("sensitive connect detail")

    monkeypatch.setattr(repository_module.sqlite3, "connect", fail_connect)

    with pytest.raises(WarehousePersistenceError, match="initialize") as failure:
        SQLiteWarehouseRepository(":memory:")

    assert "sensitive connect detail" not in str(failure.value)
