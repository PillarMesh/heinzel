from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pillarmesh_warehouse_control import (
    InitialWarehouseValidationResult,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    ResumeWarehouseValidationResult,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseRetirementEvidence,
    WarehouseValidationResult,
)

_PRIMARY_RESOURCE_KINDS = frozenset(
    {
        WarehouseResourceKind.COMPOSE_PROJECT,
        WarehouseResourceKind.WAREHOUSE_CONTAINER,
        WarehouseResourceKind.PRIVATE_NETWORK,
        WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
        WarehouseResourceKind.PRIVATE_DIRECTORY,
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.HOST_PORT_FILE,
        WarehouseResourceKind.TLS_PRIVATE_KEY,
        WarehouseResourceKind.TLS_CERTIFICATE,
        WarehouseResourceKind.BACKUP_ARTIFACT,
        WarehouseResourceKind.BACKUP_STAGING_FILE,
        WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
    }
)
_RESTORE_RESOURCE_KINDS = frozenset(
    {
        WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
        WarehouseResourceKind.RESTORE_CONTAINER,
        WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
        WarehouseResourceKind.RESTORE_DATA_VOLUME,
    }
)


class WarehouseLifecycleConformanceError(RuntimeError):
    def __init__(self, check_name: str) -> None:
        super().__init__(f"warehouse lifecycle conformance failed: {check_name}")
        self.check_name = check_name


def require_warehouse_lifecycle_conformance(condition: bool, check_name: str) -> None:
    if not condition:
        raise WarehouseLifecycleConformanceError(check_name)


def _invoke_warehouse_lifecycle_verifier(
    verifier: Callable[[], bool],
    check_name: str,
) -> None:
    callback_failed = False
    verified = False
    try:
        verified = verifier()
    except Exception:
        callback_failed = True
    if callback_failed:
        raise WarehouseLifecycleConformanceError(check_name)
    require_warehouse_lifecycle_conformance(verified, check_name)


def _single_resource(
    resources: tuple[PrivateWarehouseResource, ...],
    resource_kind: WarehouseResourceKind,
) -> PrivateWarehouseResource:
    matching = tuple(resource for resource in resources if resource.resource_kind is resource_kind)
    require_warehouse_lifecycle_conformance(
        len(matching) == 1,
        f"{resource_kind.value}_resource_count",
    )
    return matching[0]


class WarehouseLifecycleContractDriver(Protocol):
    @property
    def binding(self) -> WarehouseBinding: ...

    def provision(self, *, expected_revision: int) -> WarehouseBinding: ...

    def validation_results(self) -> tuple[WarehouseValidationResult, ...]: ...

    def resources(self) -> tuple[PrivateWarehouseResource, ...]: ...

    def suspend(self, *, expected_revision: int) -> WarehouseBinding: ...

    def resume(self, *, expected_revision: int) -> WarehouseBinding: ...

    def begin_retirement(
        self, *, expected_revision: int
    ) -> tuple[WarehouseBinding, PrivateWarehouseOperation]: ...

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence: ...

    def record_retired(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseBinding: ...

    def delete_retained_resources(
        self,
        binding: WarehouseBinding,
    ) -> WarehouseRetirementEvidence: ...

    def advance_for_resume(self) -> None: ...

    def advance_past_retention(self) -> None: ...

    def verify_backup_artifact(
        self,
        resource: PrivateWarehouseResource,
        *,
        should_exist: bool,
    ) -> bool: ...

    def verify_backup_key(self, *, should_exist: bool) -> bool: ...

    def verify_absent_resources(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> bool: ...

    def verify_provider_restart_replay(self) -> bool: ...


type WarehouseLifecycleProviderFactory = Callable[[], WarehouseLifecycleContractDriver]


@dataclass(frozen=True, slots=True)
class WarehouseLifecycleContractObservation:
    ready: WarehouseBinding
    suspended: WarehouseBinding
    resumed: WarehouseBinding
    retired: WarehouseBinding
    initial_validation: InitialWarehouseValidationResult
    resume_validation: ResumeWarehouseValidationResult
    retained_retirement: WarehouseRetirementEvidence
    deletion_retirement: WarehouseRetirementEvidence
    initial_resources: tuple[PrivateWarehouseResource, ...]
    final_resources: tuple[PrivateWarehouseResource, ...]
    backup_resource: PrivateWarehouseResource


def assert_warehouse_lifecycle_contract(
    provider_factory: WarehouseLifecycleProviderFactory,
) -> WarehouseLifecycleContractObservation:
    driver = provider_factory()
    initial_binding = driver.binding

    ready = driver.provision(expected_revision=initial_binding.revision)
    require_warehouse_lifecycle_conformance(
        ready.lifecycle_state is WarehouseBindingState.READY,
        "initial_ready_state",
    )
    validation_results = driver.validation_results()
    require_warehouse_lifecycle_conformance(
        len(validation_results) == 1,
        "initial_validation_result_count",
    )
    initial_validation = validation_results[0]
    require_warehouse_lifecycle_conformance(
        isinstance(initial_validation, InitialWarehouseValidationResult),
        "initial_validation_result_type",
    )
    require_warehouse_lifecycle_conformance(
        initial_validation.evidence.positive_probe_digest
        != initial_validation.evidence.denial_probe_digest,
        "initial_positive_denial_separation",
    )
    _invoke_warehouse_lifecycle_verifier(
        driver.verify_provider_restart_replay,
        "provider_restart_replay",
    )

    initial_resources = driver.resources()
    resource_counts = Counter(resource.resource_kind for resource in initial_resources)
    require_warehouse_lifecycle_conformance(
        all(resource_counts[kind] >= 1 for kind in _PRIMARY_RESOURCE_KINDS),
        "primary_resource_inventory",
    )
    require_warehouse_lifecycle_conformance(
        resource_counts[WarehouseResourceKind.BACKUP_ARTIFACT] == 1,
        "backup_artifact_resource_count",
    )
    require_warehouse_lifecycle_conformance(
        all(resource_counts[kind] >= 1 for kind in _RESTORE_RESOURCE_KINDS),
        "restore_resource_inventory",
    )
    restore_resources = tuple(
        resource
        for resource in initial_resources
        if resource.resource_kind in _RESTORE_RESOURCE_KINDS
    )
    require_warehouse_lifecycle_conformance(
        all(
            resource.creation_state is WarehouseResourceCreationState.CREATED
            and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in restore_resources
        ),
        "restore_resource_terminal_cleanup",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_absent_resources(restore_resources),
        "restore_resource_absence",
    )
    backup_resource = _single_resource(
        initial_resources,
        WarehouseResourceKind.BACKUP_ARTIFACT,
    )
    backup_key_resources = tuple(
        resource
        for resource in initial_resources
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY
    )
    backup_key_resource = backup_key_resources[0]
    require_warehouse_lifecycle_conformance(
        backup_key_resource.retention_deadline == backup_resource.retention_deadline,
        "backup_key_retention_deadline",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_backup_artifact(backup_resource, should_exist=True),
        "initial_backup_artifact_presence",
    )

    for _ in range(2):
        replayed = driver.provision(expected_revision=initial_binding.revision)
        require_warehouse_lifecycle_conformance(
            replayed == ready,
            "provision_replay_binding",
        )
        require_warehouse_lifecycle_conformance(
            driver.resources() == initial_resources,
            "provision_replay_resources",
        )
        require_warehouse_lifecycle_conformance(
            len(driver.validation_results()) == 1,
            "provision_replay_validation_result_count",
        )

    suspended = driver.suspend(expected_revision=ready.revision)
    require_warehouse_lifecycle_conformance(
        suspended.lifecycle_state is WarehouseBindingState.SUSPENDED,
        "suspended_state",
    )
    driver.advance_for_resume()
    resumed = driver.resume(expected_revision=suspended.revision)
    require_warehouse_lifecycle_conformance(
        resumed.lifecycle_state is WarehouseBindingState.READY,
        "resumed_ready_state",
    )
    validation_results = driver.validation_results()
    require_warehouse_lifecycle_conformance(
        len(validation_results) == 2,
        "resume_validation_result_count",
    )
    resume_validation = validation_results[-1]
    require_warehouse_lifecycle_conformance(
        isinstance(resume_validation, ResumeWarehouseValidationResult),
        "resume_validation_result_type",
    )
    require_warehouse_lifecycle_conformance(
        resume_validation.evidence.observed_at > initial_validation.evidence.observed_at,
        "resume_validation_freshness",
    )
    require_warehouse_lifecycle_conformance(
        resume_validation.evidence.engine_image_digest
        == initial_validation.evidence.engine_image_digest,
        "resume_engine_image_identity",
    )

    retiring, retirement_operation = driver.begin_retirement(expected_revision=resumed.revision)
    require_warehouse_lifecycle_conformance(
        retiring.lifecycle_state is WarehouseBindingState.RETIRING,
        "retirement_binding_state",
    )
    require_warehouse_lifecycle_conformance(
        retirement_operation.tenant_id == retiring.tenant_id
        and retirement_operation.binding_id == retiring.binding_id
        and retirement_operation.binding_revision == retiring.revision
        and retirement_operation.engine_kind is retiring.engine_kind
        and retirement_operation.operation_kind is WarehouseOperationKind.RETIRE
        and retirement_operation.status is WarehouseOperationStatus.RUNNING
        and retirement_operation.phase is WarehouseOperationPhase.CLAIMED,
        "retirement_operation_binding",
    )
    retained_retirement = driver.retire(retiring, retirement_operation)
    require_warehouse_lifecycle_conformance(
        retained_retirement.retained_resource_count > 0,
        "retirement_retained_resource_count",
    )
    require_warehouse_lifecycle_conformance(
        retained_retirement.cleanup_failed_resource_count == 0,
        "retirement_cleanup_failure_count",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_backup_artifact(backup_resource, should_exist=True),
        "retained_backup_artifact_presence",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_backup_key(should_exist=True),
        "retained_backup_key_presence",
    )
    retained_resources = driver.resources()
    retained_pair = tuple(
        resource
        for resource in retained_resources
        if resource.resource_id in {backup_resource.resource_id, backup_key_resource.resource_id}
    )
    require_warehouse_lifecycle_conformance(
        len(retained_pair) == 2,
        "retained_backup_pair_count",
    )
    require_warehouse_lifecycle_conformance(
        all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
            for resource in retained_pair
        ),
        "retained_backup_pair_status",
    )

    retired = driver.record_retired(retiring, retained_retirement, retirement_operation)
    require_warehouse_lifecycle_conformance(
        retired.lifecycle_state is WarehouseBindingState.RETIRED,
        "retired_state",
    )
    driver.advance_past_retention()
    deletion_retirement = driver.delete_retained_resources(retired)
    require_warehouse_lifecycle_conformance(
        deletion_retirement.retained_resource_count == 0,
        "post_retention_retained_resource_count",
    )
    require_warehouse_lifecycle_conformance(
        deletion_retirement.cleanup_failed_resource_count == 0,
        "post_retention_cleanup_failure_count",
    )
    final_resources = driver.resources()
    require_warehouse_lifecycle_conformance(
        all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in final_resources
        ),
        "final_resource_cleanup_status",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_backup_artifact(backup_resource, should_exist=False),
        "deleted_backup_artifact_absence",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_backup_key(should_exist=False),
        "deleted_backup_key_absence",
    )
    final_pair = tuple(
        resource
        for resource in final_resources
        if resource.resource_id in {backup_resource.resource_id, backup_key_resource.resource_id}
    )
    require_warehouse_lifecycle_conformance(
        len(final_pair) == 2,
        "final_backup_pair_count",
    )
    require_warehouse_lifecycle_conformance(
        all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in final_pair
        ),
        "final_backup_pair_cleanup_status",
    )
    _invoke_warehouse_lifecycle_verifier(
        lambda: driver.verify_absent_resources(final_resources),
        "final_resource_absence",
    )

    return WarehouseLifecycleContractObservation(
        ready=ready,
        suspended=suspended,
        resumed=resumed,
        retired=retired,
        initial_validation=initial_validation,
        resume_validation=resume_validation,
        retained_retirement=retained_retirement,
        deletion_retirement=deletion_retirement,
        initial_resources=initial_resources,
        final_resources=final_resources,
        backup_resource=backup_resource,
    )


__all__ = [
    "WarehouseLifecycleConformanceError",
    "WarehouseLifecycleContractDriver",
    "WarehouseLifecycleContractObservation",
    "WarehouseLifecycleProviderFactory",
    "assert_warehouse_lifecycle_contract",
    "require_warehouse_lifecycle_conformance",
]
