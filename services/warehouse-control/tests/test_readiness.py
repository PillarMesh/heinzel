from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import heinzel_warehouse_control as warehouse_control
import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseAdmissionError,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseFailureClassification,
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
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
PROVISIONED_AT = NOW - timedelta(days=1)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FailingConnection:
    def __init__(self, connection: sqlite3.Connection, *, fail_after_prefix: str) -> None:
        self._connection = connection
        self._fail_after_prefix = fail_after_prefix

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        if sql.lstrip().startswith(self._fail_after_prefix):
            raise sqlite3.OperationalError("injected persistence failure")
        return cursor

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class ResourceMutationBeforeRetirementRepository(SQLiteWarehouseRepository):
    resource_update: PrivateWarehouseResource | None = None

    def record_retirement_operation(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        expected_operation: PrivateWarehouseOperation,
        succeeded_operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> None:
        if self.resource_update is None:
            raise AssertionError("retirement race requires a resource update")
        self.save_resource(self.resource_update)
        super().record_retirement_operation(
            binding,
            evidence,
            expected_operation,
            succeeded_operation,
            expected_revision=expected_revision,
        )


def new_control(
    *, local_acceptance: bool = False
) -> tuple[WarehouseControlService, SQLiteWarehouseRepository, Clock]:
    repository = SQLiteWarehouseRepository(":memory:")
    clock = Clock()
    if local_acceptance:
        control = WarehouseControlService(
            repository,
            clock=clock,
            readiness_policy=warehouse_control.LocalAcceptanceWarehouseReadinessPolicy(),
        )
    else:
        control = WarehouseControlService(repository, clock=clock)
    return control, repository, clock


def source_binding(
    control: WarehouseControlService,
    repository: SQLiteWarehouseRepository,
    lifecycle_state: WarehouseBindingState,
    *,
    provisioned_at: datetime | None = None,
    tenant_id: str = "tenant-a",
) -> WarehouseBinding:
    draft = control.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    if lifecycle_state is WarehouseBindingState.VALIDATING:
        sequence = repository.next_operation_sequence(draft.tenant_id)
        assert repository.claim_operation(
            PrivateWarehouseOperation(
                tenant_id=draft.tenant_id,
                binding_id=draft.binding_id,
                binding_revision=draft.revision,
                operation_id=f"wop-admission-provision-{sequence}",
                operation_kind=WarehouseOperationKind.PROVISION,
                engine_kind=draft.engine_kind,
                status=WarehouseOperationStatus.CLAIMED,
                phase=WarehouseOperationPhase.CLAIMED,
                started_at=NOW,
                updated_at=NOW,
            )
        )
    source = WarehouseBinding.model_validate(
        {
            **draft.model_dump(),
            "lifecycle_state": lifecycle_state,
            "revision": draft.revision + 1,
            "updated_at": NOW,
            "provisioned_at": provisioned_at,
        }
    )
    repository.save(source)
    return source


def admission_operation(
    repository: SQLiteWarehouseRepository,
    binding: WarehouseBinding,
    operation_kind: WarehouseOperationKind,
) -> PrivateWarehouseOperation:
    current = repository.load_live_operation(binding.tenant_id, binding.binding_id)
    if current is None:
        sequence = repository.next_operation_sequence(binding.tenant_id)
        current = PrivateWarehouseOperation(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation_id=f"wop-admission-{operation_kind.value}-{sequence}",
            operation_kind=operation_kind,
            engine_kind=binding.engine_kind,
            status=WarehouseOperationStatus.CLAIMED,
            phase=WarehouseOperationPhase.CLAIMED,
            started_at=NOW,
            updated_at=NOW,
        )
        assert repository.claim_operation(current)
    if current.operation_kind is not operation_kind:
        raise AssertionError("admission fixture has the wrong operation kind")
    if current.status is WarehouseOperationStatus.CLAIMED:
        updated = current.model_copy(update={"status": WarehouseOperationStatus.RUNNING})
        repository.save_operation(current, updated)
        current = updated
    phase_paths = {
        WarehouseOperationKind.PROVISION: (
            WarehouseOperationPhase.PROVIDER_CREATED,
            WarehouseOperationPhase.VALIDATING,
        ),
        WarehouseOperationKind.RESUME: (
            WarehouseOperationPhase.RESUMED,
            WarehouseOperationPhase.VALIDATING,
        ),
        WarehouseOperationKind.RETIRE: (),
        WarehouseOperationKind.SUSPEND: (WarehouseOperationPhase.SUSPENDED,),
    }
    remaining_phases = phase_paths[operation_kind]
    if current.phase in remaining_phases:
        remaining_phases = remaining_phases[remaining_phases.index(current.phase) + 1 :]
    for phase in remaining_phases:
        updated = current.model_copy(
            update={
                "phase": phase,
                "provider_resource_handle": (
                    "provider-resource"
                    if phase is WarehouseOperationPhase.PROVIDER_CREATED
                    else current.provider_resource_handle
                ),
            }
        )
        repository.save_operation(current, updated)
        current = updated
    return current


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
        "verified_at": LATER,
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
        "observed_at": LATER,
    }
    values.update(updates)
    return WarehouseValidationEvidence.model_validate(values)


def local_validation_evidence(
    binding: WarehouseBinding, restore: WarehouseRestoreVerification
) -> WarehouseValidationEvidence:
    return validation_evidence(
        binding,
        restore,
        validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        encryption_at_rest_disposition=(EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
    )


def resume_evidence(
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
        "observed_at": LATER,
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
        "observed_at": LATER,
    }
    values.update(updates)
    return WarehouseRetirementEvidence.model_validate(values)


def live_operation(binding: WarehouseBinding) -> PrivateWarehouseOperation:
    return PrivateWarehouseOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id="wop-test",
        operation_kind=WarehouseOperationKind.RETIRE,
        engine_kind=binding.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )


def record_terminal_operation(
    repository: SQLiteWarehouseRepository,
    binding: WarehouseBinding,
    status: WarehouseOperationStatus,
) -> PrivateWarehouseOperation:
    claimed = live_operation(binding)
    repository.claim_operation(claimed)
    running = claimed.model_copy(update={"status": WarehouseOperationStatus.RUNNING})
    repository.save_operation(claimed, running)
    terminal = running.model_copy(
        update={
            "status": status,
            "phase": (
                WarehouseOperationPhase.RETIRED
                if status is WarehouseOperationStatus.SUCCEEDED
                else WarehouseOperationPhase.CLAIMED
            ),
            "failure_classification": (
                WarehouseFailureClassification.PERMANENT_CONFIGURATION
                if status is WarehouseOperationStatus.FAILED
                else None
            ),
            "updated_at": LATER,
        }
    )
    repository.save_operation(running, terminal)
    return terminal


def terminal_resources(
    repository: SQLiteWarehouseRepository, binding: WarehouseBinding
) -> tuple[PrivateWarehouseResource, ...]:
    operation = live_operation(binding)
    repository.claim_operation(operation)
    resources = tuple(
        PrivateWarehouseResource(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation_id=operation.operation_id,
            resource_id=f"whr-{index}",
            resource_kind=WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
            provider_resource_handle=f"volume-{index}",
            creation_state=WarehouseResourceCreationState.CREATED,
            retention_deadline=NOW + timedelta(days=30),
            cleanup_status=status,
            cleanup_failure_classification=(
                WarehouseFailureClassification.INTEGRITY_FAILURE
                if status is WarehouseResourceCleanupStatus.FAILED
                else None
            ),
            created_at=NOW,
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


def test_production_policy_admits_only_production_with_proven_encryption() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    policy = warehouse_control.ProductionWarehouseReadinessPolicy()

    policy.admit(validation_evidence(binding, restore))

    with pytest.raises(WarehouseAdmissionError, match="production"):
        policy.admit(local_validation_evidence(binding, restore))


def test_local_policy_admits_only_local_with_deferred_encryption() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    policy = warehouse_control.LocalAcceptanceWarehouseReadinessPolicy()

    policy.admit(local_validation_evidence(binding, restore))

    with pytest.raises(WarehouseAdmissionError, match="local acceptance"):
        policy.admit(validation_evidence(binding, restore))


def test_default_policy_rejects_local_acceptance_evidence() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)

    with pytest.raises(WarehouseAdmissionError, match="production"):
        control.record_validation(
            binding.tenant_id,
            binding.binding_id,
            local_validation_evidence(binding, restore),
            restore,
            operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_explicit_local_policy_admits_local_acceptance_evidence() -> None:
    control, repository, clock = new_control(local_acceptance=True)
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    clock.value = LATER

    ready = control.record_validation(
        binding.tenant_id,
        binding.binding_id,
        local_validation_evidence(binding, restore),
        restore,
        operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert ready.provisioned_at == LATER


def test_plain_transition_cannot_report_suspension_without_a_provider_operation() -> None:
    control, repository, _ = new_control()
    ready = source_binding(
        control,
        repository,
        WarehouseBindingState.READY,
        provisioned_at=PROVISIONED_AT,
    )

    with pytest.raises(ValueError, match="not allowed"):
        control.transition(
            ready.tenant_id,
            ready.binding_id,
            WarehouseBindingState.SUSPENDED,
            expected_revision=ready.revision,
        )

    assert control.get(ready.tenant_id, ready.binding_id) == ready


def test_initial_validation_atomically_sets_provisioned_time_once() -> None:
    control, repository, clock = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    evidence = validation_evidence(binding, restore)
    clock.value = LATER

    ready = control.record_validation(
        binding.tenant_id,
        binding.binding_id,
        evidence,
        restore,
        operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert ready.revision == binding.revision + 1
    assert ready.provisioned_at == LATER
    assert control.get(binding.tenant_id, binding.binding_id) == ready


def test_evidence_observed_at_the_source_transition_time_is_fresh() -> None:
    control, repository, clock = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding, verified_at=binding.updated_at)
    evidence = validation_evidence(binding, restore, observed_at=binding.updated_at)
    clock.value = LATER

    ready = control.record_validation(
        binding.tenant_id,
        binding.binding_id,
        evidence,
        restore,
        operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY


@pytest.mark.parametrize(
    ("evidence_updates", "restore_updates", "message"),
    (
        ({"tenant_id": "tenant-b"}, {}, "tenant"),
        ({"binding_id": "whb-other"}, {}, "binding"),
        ({"binding_revision": 99}, {}, "revision"),
        ({"engine_kind": EngineKind.CLICKHOUSE}, {}, "engine"),
        ({}, {"tenant_id": "tenant-b"}, "tenant"),
        ({}, {"binding_id": "whb-other"}, "binding"),
        ({}, {"binding_revision": 99}, "revision"),
        ({}, {"engine_kind": EngineKind.CLICKHOUSE}, "engine"),
        ({"backup_artifact_digest": "f" * 64}, {}, "backup"),
        ({"restore_verification_digest": "f" * 64}, {}, "restore"),
        ({"principal_profile_digest": "f" * 64}, {}, "principal"),
    ),
)
def test_initial_validation_refuses_mismatched_ownership_engine_or_linked_digest(
    evidence_updates: dict[str, object],
    restore_updates: dict[str, object],
    message: str,
) -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding, **restore_updates)
    evidence = validation_evidence(binding, restore, **evidence_updates)

    with pytest.raises(WarehouseAdmissionError, match=message):
        control.record_validation(
            binding.tenant_id,
            binding.binding_id,
            evidence,
            restore,
            operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_initial_validation_refuses_the_wrong_state_and_stale_revision() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.PROVISIONING)
    restore = restore_verification(binding)
    evidence = validation_evidence(binding, restore)
    operation = admission_operation(repository, binding, WarehouseOperationKind.PROVISION)

    with pytest.raises(WarehouseAdmissionError, match="validating"):
        control.record_validation(
            binding.tenant_id,
            binding.binding_id,
            evidence,
            restore,
            operation=operation,
            expected_revision=binding.revision,
        )
    with pytest.raises(ValueError, match="revision is stale"):
        control.record_validation(
            binding.tenant_id,
            binding.binding_id,
            evidence,
            restore,
            operation=operation,
            expected_revision=binding.revision - 1,
        )


def test_initial_validation_refuses_reused_evidence_identity() -> None:
    control, repository, clock = new_control()
    first = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    first_restore = restore_verification(first)
    clock.value = LATER
    control.record_validation(
        first.tenant_id,
        first.binding_id,
        validation_evidence(first, first_restore),
        first_restore,
        operation=admission_operation(repository, first, WarehouseOperationKind.PROVISION),
        expected_revision=first.revision,
    )
    second = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    second_restore = restore_verification(second, verification_id="wrv-second")

    with pytest.raises(WarehouseValidationConflictError, match="already recorded"):
        control.record_validation(
            second.tenant_id,
            second.binding_id,
            validation_evidence(second, second_restore),
            second_restore,
            operation=admission_operation(repository, second, WarehouseOperationKind.PROVISION),
            expected_revision=second.revision,
        )

    assert control.get(second.tenant_id, second.binding_id) == second


def test_resume_validation_preserves_the_first_provisioned_time() -> None:
    control, repository, clock = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.SUSPENDED,
        provisioned_at=PROVISIONED_AT,
    )
    clock.value = LATER

    ready = control.record_resume_validation(
        binding.tenant_id,
        binding.binding_id,
        resume_evidence(binding),
        operation=admission_operation(repository, binding, WarehouseOperationKind.RESUME),
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert ready.provisioned_at == PROVISIONED_AT


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"tenant_id": "tenant-b"}, "tenant"),
        ({"binding_id": "whb-other"}, "binding"),
        ({"binding_revision": 99}, "revision"),
        ({"engine_kind": EngineKind.CLICKHOUSE}, "engine"),
        ({"observed_at": NOW - timedelta(seconds=1)}, "fresh"),
    ),
)
def test_resume_validation_refuses_mismatched_or_stale_evidence(
    updates: dict[str, object], message: str
) -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.SUSPENDED,
        provisioned_at=PROVISIONED_AT,
    )

    with pytest.raises(WarehouseAdmissionError, match=message):
        control.record_resume_validation(
            binding.tenant_id,
            binding.binding_id,
            resume_evidence(binding, **updates),
            operation=admission_operation(repository, binding, WarehouseOperationKind.RESUME),
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_resume_validation_requires_suspended_state_and_a_provisioned_binding() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)

    with pytest.raises(WarehouseAdmissionError, match="suspended"):
        control.record_resume_validation(
            binding.tenant_id,
            binding.binding_id,
            resume_evidence(binding),
            operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
            expected_revision=binding.revision,
        )


@pytest.mark.parametrize(
    "source_state",
    (WarehouseBindingState.RETIRING, WarehouseBindingState.FAILED),
)
def test_retirement_admits_only_evidence_for_retiring_or_failed_revision(
    source_state: WarehouseBindingState,
) -> None:
    control, repository, clock = new_control()
    binding = source_binding(
        control,
        repository,
        source_state,
        provisioned_at=PROVISIONED_AT,
    )
    clock.value = LATER

    retired = control.record_retirement(
        binding.tenant_id,
        binding.binding_id,
        retirement_evidence(binding),
        operation=admission_operation(repository, binding, WarehouseOperationKind.RETIRE),
        expected_revision=binding.revision,
    )

    assert retired.lifecycle_state is WarehouseBindingState.RETIRED
    assert retired.provisioned_at == PROVISIONED_AT


def test_retirement_requires_exact_terminal_resource_dispositions() -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.RETIRING,
        provisioned_at=PROVISIONED_AT,
    )
    resources = terminal_resources(repository, binding)

    retired = control.record_retirement(
        binding.tenant_id,
        binding.binding_id,
        retirement_evidence(binding, resources),
        operation=admission_operation(repository, binding, WarehouseOperationKind.RETIRE),
        expected_revision=binding.revision,
    )

    assert retired.lifecycle_state is WarehouseBindingState.RETIRED


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
def test_retirement_refuses_inventory_or_cleanup_count_mismatch(
    updates: dict[str, object], message: str
) -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.RETIRING,
        provisioned_at=PROVISIONED_AT,
    )
    resources = terminal_resources(repository, binding)

    with pytest.raises(WarehouseAdmissionError, match=message):
        control.record_retirement(
            binding.tenant_id,
            binding.binding_id,
            retirement_evidence(binding, resources, **updates),
            operation=admission_operation(repository, binding, WarehouseOperationKind.RETIRE),
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_retirement_refuses_a_resource_disposition_changed_after_the_service_read() -> None:
    repository = ResourceMutationBeforeRetirementRepository(":memory:")
    control = WarehouseControlService(repository, clock=Clock())
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.RETIRING,
        provisioned_at=PROVISIONED_AT,
    )
    resources = terminal_resources(repository, binding)
    repository.resource_update = PrivateWarehouseResource.model_validate(
        {
            **resources[1].model_dump(),
            "cleanup_status": WarehouseResourceCleanupStatus.FAILED,
            "cleanup_failure_classification": WarehouseFailureClassification.INTEGRITY_FAILURE,
            "updated_at": LATER + timedelta(seconds=1),
        }
    )

    with pytest.raises(WarehouseValidationConflictError, match="cleanup disposition"):
        control.record_retirement(
            binding.tenant_id,
            binding.binding_id,
            retirement_evidence(binding, resources),
            operation=admission_operation(repository, binding, WarehouseOperationKind.RETIRE),
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_retirement_refuses_a_nonterminal_resource_disposition() -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.RETIRING,
        provisioned_at=PROVISIONED_AT,
    )
    operation = live_operation(binding)
    repository.claim_operation(operation)
    pending = PrivateWarehouseResource(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=operation.operation_id,
        resource_id="whr-pending",
        resource_kind=WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
        provider_resource_handle="volume-pending",
        creation_state=WarehouseResourceCreationState.CREATED,
        retention_deadline=NOW + timedelta(days=30),
        cleanup_status=WarehouseResourceCleanupStatus.PENDING,
        created_at=NOW,
        updated_at=LATER,
    )
    repository.record_resources((pending,))

    with pytest.raises(WarehouseAdmissionError, match="terminal"):
        control.record_retirement(
            binding.tenant_id,
            binding.binding_id,
            retirement_evidence(binding, (pending,)),
            operation=operation.model_copy(update={"status": WarehouseOperationStatus.RUNNING}),
            expected_revision=binding.revision,
        )


def test_draft_is_abandoned_without_retirement_evidence() -> None:
    control, _, _ = new_control()
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )

    retired = control.abandon_draft(
        binding.tenant_id,
        binding.binding_id,
        expected_revision=binding.revision,
    )

    assert retired.lifecycle_state is WarehouseBindingState.RETIRED
    assert retired.provisioned_at is None


def test_abandon_draft_refuses_a_binding_that_left_draft() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.PROVISIONING)

    with pytest.raises(WarehouseAdmissionError, match="draft"):
        control.abandon_draft(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )


def test_abandon_draft_refuses_a_draft_with_a_live_operation() -> None:
    control, repository, _ = new_control()
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    repository.claim_operation(live_operation(binding))

    with pytest.raises(WarehouseAdmissionError, match="live operation"):
        control.abandon_draft(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )


def test_abandon_draft_refuses_a_draft_with_a_provisioned_time() -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.DRAFT,
        provisioned_at=PROVISIONED_AT,
    )

    with pytest.raises(WarehouseAdmissionError, match="provisioned"):
        control.abandon_draft(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


@pytest.mark.parametrize(
    "status",
    (WarehouseOperationStatus.SUCCEEDED, WarehouseOperationStatus.FAILED),
)
def test_abandon_draft_refuses_a_draft_with_a_terminal_operation(
    status: WarehouseOperationStatus,
) -> None:
    control, repository, _ = new_control()
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    record_terminal_operation(repository, binding, status)

    with pytest.raises(WarehouseAdmissionError, match="operation"):
        control.abandon_draft(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_abandon_draft_refuses_a_draft_with_any_resource() -> None:
    control, repository, _ = new_control()
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    warehouse_operation = record_terminal_operation(
        repository,
        binding,
        WarehouseOperationStatus.SUCCEEDED,
    )
    warehouse_resource = PrivateWarehouseResource(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=warehouse_operation.operation_id,
        resource_id="whr-abandonment-blocker",
        resource_kind=WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
        provider_resource_handle="volume-abandonment-blocker",
        creation_state=WarehouseResourceCreationState.CREATED,
        retention_deadline=NOW + timedelta(days=30),
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
        created_at=NOW,
        updated_at=LATER,
    )
    repository.record_resources((warehouse_resource,))

    with pytest.raises(WarehouseAdmissionError, match="resource"):
        control.abandon_draft(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )

    assert control.get(binding.tenant_id, binding.binding_id) == binding


@pytest.mark.parametrize(
    ("classification", "terminal"),
    (
        (WarehouseFailureClassification.TRANSIENT_TRANSPORT, False),
        (WarehouseFailureClassification.TRANSIENT_UNAVAILABLE, False),
        (WarehouseFailureClassification.THROTTLED, False),
        (WarehouseFailureClassification.AMBIGUOUS_OUTCOME, False),
        (WarehouseFailureClassification.AUTHORIZATION_DENIED, True),
        (WarehouseFailureClassification.STATEMENT_REJECTED, True),
        (WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE, True),
        (WarehouseFailureClassification.INTEGRITY_FAILURE, True),
        (WarehouseFailureClassification.PERMANENT_CONFIGURATION, True),
    ),
)
def test_only_five_failure_classifications_can_become_terminal_binding_failure(
    classification: WarehouseFailureClassification, terminal: bool
) -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.PROVISIONING)

    if terminal:
        failed = control.record_terminal_failure(
            binding.tenant_id,
            binding.binding_id,
            classification,
            expected_revision=binding.revision,
        )

        assert failed.lifecycle_state is WarehouseBindingState.FAILED
    else:
        with pytest.raises(WarehouseAdmissionError, match="terminal"):
            control.record_terminal_failure(
                binding.tenant_id,
                binding.binding_id,
                classification,
                expected_revision=binding.revision,
            )

        assert control.get(binding.tenant_id, binding.binding_id) == binding


def test_terminal_failure_preserves_the_first_provisioned_time() -> None:
    control, repository, _ = new_control()
    binding = source_binding(
        control,
        repository,
        WarehouseBindingState.VALIDATING,
        provisioned_at=PROVISIONED_AT,
    )

    failed = control.record_terminal_failure(
        binding.tenant_id,
        binding.binding_id,
        WarehouseFailureClassification.INTEGRITY_FAILURE,
        expected_revision=binding.revision,
    )

    assert failed.provisioned_at == PROVISIONED_AT


@pytest.mark.parametrize(
    ("kind", "fail_after_prefix"),
    (
        ("initial", "INSERT INTO warehouse_validation_evidence"),
        ("resume", "INSERT INTO warehouse_resume_validation_evidence"),
        ("retirement", "INSERT INTO warehouse_retirement_evidence"),
    ),
)
def test_service_admission_never_exposes_an_advanced_binding_after_repository_failure(
    kind: str, fail_after_prefix: str
) -> None:
    control, repository, _ = new_control()
    source_state = {
        "initial": WarehouseBindingState.VALIDATING,
        "resume": WarehouseBindingState.SUSPENDED,
        "retirement": WarehouseBindingState.RETIRING,
    }[kind]
    binding = source_binding(
        control,
        repository,
        source_state,
        provisioned_at=None if kind == "initial" else PROVISIONED_AT,
    )
    repository._connection = FailingConnection(
        repository._connection,
        fail_after_prefix=fail_after_prefix,
    )

    with pytest.raises(WarehousePersistenceError):
        if kind == "initial":
            restore = restore_verification(binding)
            control.record_validation(
                binding.tenant_id,
                binding.binding_id,
                validation_evidence(binding, restore),
                restore,
                operation=admission_operation(
                    repository, binding, WarehouseOperationKind.PROVISION
                ),
                expected_revision=binding.revision,
            )
        elif kind == "resume":
            control.record_resume_validation(
                binding.tenant_id,
                binding.binding_id,
                resume_evidence(binding),
                operation=admission_operation(repository, binding, WarehouseOperationKind.RESUME),
                expected_revision=binding.revision,
            )
        else:
            control.record_retirement(
                binding.tenant_id,
                binding.binding_id,
                retirement_evidence(binding),
                operation=admission_operation(repository, binding, WarehouseOperationKind.RETIRE),
                expected_revision=binding.revision,
            )

    assert repository.load(binding.tenant_id, binding.binding_id) == binding


def test_task_four_admission_preserves_nondisclosure_for_foreign_and_missing_bindings() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    evidence = validation_evidence(binding, restore)
    operation = admission_operation(repository, binding, WarehouseOperationKind.PROVISION)

    failures: list[tuple[object, ...]] = []
    for tenant_id, binding_id in (
        ("tenant-b", binding.binding_id),
        (binding.tenant_id, "whb-missing"),
    ):
        with pytest.raises(KeyError) as failure:
            control.record_validation(
                tenant_id,
                binding_id,
                evidence,
                restore,
                operation=operation,
                expected_revision=binding.revision,
            )
        failures.append(failure.value.args)

    assert failures == [
        ("warehouse binding was not found",),
        ("warehouse binding was not found",),
    ]


def test_recorded_initial_evidence_uses_canonical_repository_payload() -> None:
    control, repository, _ = new_control()
    binding = source_binding(control, repository, WarehouseBindingState.VALIDATING)
    restore = restore_verification(binding)
    evidence = validation_evidence(binding, restore)

    control.record_validation(
        binding.tenant_id,
        binding.binding_id,
        evidence,
        restore,
        operation=admission_operation(repository, binding, WarehouseOperationKind.PROVISION),
        expected_revision=binding.revision,
    )

    row = repository._connection.execute(
        "SELECT payload FROM warehouse_validation_evidence WHERE tenant_id = ? AND evidence_id = ?",
        (binding.tenant_id, evidence.evidence_id),
    ).fetchone()
    assert row == (canonical_bytes(evidence),)
