from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    ResumeWarehouseValidationResult,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseLifecycleOrchestrator,
    WarehouseProviderError,
    WarehouseProvisionResult,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from pillarmesh_warehouse_control.retirement import canonical_retirement_resource_snapshot


@dataclass(slots=True)
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class RetainedResourceProvider:
    def __init__(
        self,
        *,
        engine_kind: EngineKind,
        repository: SQLiteWarehouseRepository,
        clock: MutableClock,
        production_evidence: bool = True,
    ) -> None:
        self.engine_kind = engine_kind
        self._repository = repository
        self._clock = clock
        self._production_evidence = production_evidence
        self.private_canary = f"private://{engine_kind.value}/credential-canary"

    def provision(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        resource = PrivateWarehouseResource(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            resource_id="wrs-"
            + digest(
                {
                    "tenant_id": binding.tenant_id,
                    "binding_id": binding.binding_id,
                    "engine_kind": binding.engine_kind,
                }
            )[:24],
            resource_kind=WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
            provider_resource_handle=self.private_canary,
            creation_state=WarehouseResourceCreationState.CREATED,
            retention_deadline=operation.started_at + timedelta(hours=1),
            cleanup_status=WarehouseResourceCleanupStatus.PENDING,
            created_at=operation.started_at,
            updated_at=operation.started_at,
        )
        self._repository.record_resources((resource,))
        return self._provision_result(binding, operation)

    def reconcile(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        return self._provision_result(binding, operation)

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult | ResumeWarehouseValidationResult:
        if resume:
            return ResumeWarehouseValidationResult(
                evidence=_resume_evidence(binding, self._clock())
            )
        restore = _restore_verification(binding, self._clock())
        return InitialWarehouseValidationResult(
            evidence=_validation_evidence(
                binding,
                restore,
                self._clock(),
                production=self._production_evidence,
            ),
            restore_verification=restore,
        )

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        del binding, operation

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        del binding, operation

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        resources = self._repository.load_resources(binding.tenant_id, binding.binding_id)
        updated = tuple(
            resource.model_copy(
                update={
                    "cleanup_status": (
                        WarehouseResourceCleanupStatus.COMPLETE
                        if self._clock() >= resource.retention_deadline
                        else WarehouseResourceCleanupStatus.RETAINED
                    ),
                    "updated_at": self._clock(),
                }
            )
            for resource in resources
        )
        self._repository.save_resources(updated)
        snapshot = canonical_retirement_resource_snapshot(updated)
        return WarehouseRetirementEvidence(
            evidence_id="wret-" + digest({"operation_id": operation.operation_id})[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            resource_inventory_digest=snapshot.resource_inventory_digest,
            cleanup_disposition_digest=snapshot.cleanup_disposition_digest,
            retention_policy_digest=snapshot.retention_policy_digest,
            completed_resource_count=snapshot.completed_resource_count,
            retained_resource_count=snapshot.retained_resource_count,
            cleanup_failed_resource_count=snapshot.cleanup_failed_resource_count,
            observed_at=self._clock(),
        )

    def _provision_result(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        return WarehouseProvisionResult(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=binding.engine_kind,
            private_resource_handle=self.private_canary,
            provider_build_digest="1" * 64,
            resource_inventory_digest="2" * 64,
        )


@pytest.mark.parametrize("engine_kind", tuple(EngineKind))
def test_retired_binding_remains_immutable_while_authorized_cleanup_deletes_retained_data(
    engine_kind: EngineKind,
) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 12, tzinfo=UTC))
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=clock)
    provider = RetainedResourceProvider(
        engine_kind=engine_kind,
        repository=repository,
        clock=clock,
    )
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=provider,
        clock=clock,
    )
    draft = control.create_draft(
        tenant_id=f"tenant-{engine_kind.value}",
        engine_kind=engine_kind,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    ready = orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )
    with pytest.raises(KeyError, match="not found"):
        control.get("wrong-tenant", ready.binding_id)
    with pytest.raises(ValueError, match="stale"):
        orchestrator.suspend(
            ready.tenant_id,
            ready.binding_id,
            expected_revision=ready.revision - 1,
        )
    suspended = orchestrator.suspend(
        ready.tenant_id,
        ready.binding_id,
        expected_revision=ready.revision,
    )
    clock.advance(timedelta(minutes=1))
    resumed = orchestrator.resume(
        suspended.tenant_id,
        suspended.binding_id,
        expected_revision=suspended.revision,
    )
    assert resumed.lifecycle_state is WarehouseBindingState.READY
    assert resumed.provisioned_at == ready.provisioned_at

    unrelated_draft = control.create_draft(
        tenant_id=f"unrelated-{engine_kind.value}",
        engine_kind=engine_kind,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    unrelated_ready = orchestrator.provision(
        unrelated_draft.tenant_id,
        unrelated_draft.binding_id,
        expected_revision=unrelated_draft.revision,
    )
    unrelated_before = repository.load_resources(
        unrelated_ready.tenant_id,
        unrelated_ready.binding_id,
    )

    retired = orchestrator.retire(
        resumed.tenant_id,
        resumed.binding_id,
        expected_revision=resumed.revision,
    )

    retained = repository.load_resources(retired.tenant_id, retired.binding_id)
    assert retired.lifecycle_state is WarehouseBindingState.RETIRED
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED for resource in retained
    )
    with pytest.raises(ValueError, match="retention deadline"):
        orchestrator.delete_retained_resources(
            retired.tenant_id,
            retired.binding_id,
            expected_revision=retired.revision,
            authorized=True,
        )
    assert repository.load_resources(retired.tenant_id, retired.binding_id) == retained
    clock.advance(timedelta(hours=2))
    with pytest.raises(PermissionError, match="explicit authorization"):
        orchestrator.delete_retained_resources(
            retired.tenant_id,
            retired.binding_id,
            expected_revision=retired.revision,
            authorized=False,
        )

    deletion = orchestrator.delete_retained_resources(
        retired.tenant_id,
        retired.binding_id,
        expected_revision=retired.revision,
        authorized=True,
    )

    assert control.get(retired.tenant_id, retired.binding_id) == retired
    assert (
        repository.load_retirement_evidence(
            retired.tenant_id,
            retired.binding_id,
            retired.revision,
        )
        == deletion
    )
    assert (
        orchestrator.delete_retained_resources(
            retired.tenant_id,
            retired.binding_id,
            expected_revision=retired.revision,
            authorized=True,
        )
        == deletion
    )
    assert deletion.completed_resource_count == len(retained)
    assert deletion.retained_resource_count == 0
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in repository.load_resources(retired.tenant_id, retired.binding_id)
    )
    assert (
        repository.load_resources(
            unrelated_ready.tenant_id,
            unrelated_ready.binding_id,
        )
        == unrelated_before
    )
    public_artifacts = (draft, ready, suspended, resumed, retired, deletion)
    assert all(
        provider.private_canary not in artifact.model_dump_json() for artifact in public_artifacts
    )


@pytest.mark.parametrize("engine_kind", tuple(EngineKind))
def test_production_policy_denial_never_admits_a_ready_binding(engine_kind: EngineKind) -> None:
    clock = MutableClock(datetime(2026, 8, 28, 12, tzinfo=UTC))
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=clock)
    provider = RetainedResourceProvider(
        engine_kind=engine_kind,
        repository=repository,
        clock=clock,
        production_evidence=False,
    )
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=provider,
        clock=clock,
    )
    draft = control.create_draft(
        tenant_id=f"tenant-denied-{engine_kind.value}",
        engine_kind=engine_kind,
        region="us-west",
        capacity_profile="mvp-fixed",
    )

    with pytest.raises(WarehouseProviderError):
        orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )

    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.FAILED
    )


def _restore_verification(
    binding: WarehouseBinding,
    observed_at: datetime,
) -> WarehouseRestoreVerification:
    return WarehouseRestoreVerification(
        verification_id="wrv-e2e",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=binding.engine_kind,
        source_backup_artifact_digest="3" * 64,
        representative_data_digest="4" * 64,
        schema_metadata_digest="5" * 64,
        principal_profile_digest="6" * 64,
        integrity_marker_digest="7" * 64,
        query_behavior_digest="8" * 64,
        verified_at=observed_at,
    )


def _validation_evidence(
    binding: WarehouseBinding,
    restore: WarehouseRestoreVerification,
    observed_at: datetime,
    *,
    production: bool,
) -> WarehouseValidationEvidence:
    return WarehouseValidationEvidence(
        evidence_id="wev-e2e",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        validation_profile=(
            WarehouseValidationProfile.PRODUCTION
            if production
            else WarehouseValidationProfile.LOCAL_ACCEPTANCE
        ),
        engine_kind=binding.engine_kind,
        engine_version="1.0",
        engine_build_digest="9" * 64,
        engine_image_digest="a" * 64,
        principal_profile_digest=restore.principal_profile_digest,
        namespace_grant_matrix_digest="b" * 64,
        tls_probe_digest="c" * 64,
        network_isolation_probe_digest="d" * 64,
        encryption_at_rest_evidence_digest="e" * 64,
        encryption_at_rest_disposition=(
            EncryptionAtRestDisposition.PROVEN
            if production
            else EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE
        ),
        positive_probe_digest="f" * 64,
        denial_probe_digest="0" * 64,
        ledger_probe_digest="1" * 64,
        monitoring_probe_digest="2" * 64,
        capacity_alert_probe_digest="3" * 64,
        backup_artifact_digest=restore.source_backup_artifact_digest,
        restore_verification_digest=digest(restore),
        restore_cleanup_digest="4" * 64,
        observed_at=observed_at,
    )


def _resume_evidence(
    binding: WarehouseBinding,
    observed_at: datetime,
) -> WarehouseResumeValidationEvidence:
    return WarehouseResumeValidationEvidence(
        evidence_id="wrev-e2e",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=binding.engine_kind,
        engine_version="1.0",
        engine_build_digest="5" * 64,
        engine_image_digest="a" * 64,
        tls_probe_digest="6" * 64,
        network_isolation_probe_digest="7" * 64,
        monitoring_probe_digest="8" * 64,
        positive_probe_digest="9" * 64,
        denial_probe_digest="b" * 64,
        storage_integrity_probe_digest="c" * 64,
        observed_at=observed_at,
    )
