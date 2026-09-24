from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal, cast

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    PrivateWarehouseOperation,
    ResumeWarehouseValidationResult,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseFailureClassification,
    WarehouseLifecycleOrchestrator,
    WarehouseOperationConflictError,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehousePersistenceError,
    WarehouseProvider,
    WarehouseProviderError,
    WarehouseProvisionResult,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from heinzel_warehouse_control.repository import (
    SQLiteWarehouseRepository,
    _Connection,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
OBSERVED_AT = NOW + timedelta(minutes=1)
type ProviderOperation = Literal[
    "provision", "reconcile", "validate", "suspend", "resume", "retire"
]
type StableReplayPath = Literal["provision", "suspend", "resume", "retire", "delete_retained"]


class SimulatedStop(BaseException):
    pass


class EventRecorder:
    def __init__(self, *, stop_after: str | None = None) -> None:
        self.events: list[str] = []
        self._stop_after = stop_after
        self._stopped = False

    def record(self, event: str) -> None:
        self.events.append(event)
        if event == self._stop_after and not self._stopped:
            self._stopped = True
            raise SimulatedStop

    def stop_after(self, event: str) -> None:
        self._stop_after = event
        self._stopped = False


class RecordingRepository(SQLiteWarehouseRepository):
    def __init__(self, recorder: EventRecorder) -> None:
        super().__init__(":memory:")
        self._recorder = recorder
        self.operation_sequence_allocations = 0

    def next_operation_sequence(self, tenant_id: str) -> int:
        self.operation_sequence_allocations += 1
        return super().next_operation_sequence(tenant_id)

    def claim_operation(self, operation: PrivateWarehouseOperation) -> bool:
        claimed = super().claim_operation(operation)
        self._recorder.record(f"claim:{operation.binding_revision}")
        return claimed


class TerminalFailureConnection:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self._terminal_operation_updated = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        normalized_sql = " ".join(sql.split())
        if (
            normalized_sql.startswith("UPDATE private_warehouse_operations")
            and parameters[0] == WarehouseOperationStatus.FAILED.value
        ):
            self._terminal_operation_updated = True
        if self._terminal_operation_updated and normalized_sql.startswith(
            "INSERT INTO warehouse_bindings"
        ):
            raise sqlite3.OperationalError("private terminal failure detail")
        return cursor

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class StableAdmissionConnection:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self._terminal_operation_updated = False

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        normalized_sql = " ".join(sql.split())
        if (
            normalized_sql.startswith("UPDATE private_warehouse_operations")
            and parameters[0] == WarehouseOperationStatus.SUCCEEDED.value
        ):
            self._terminal_operation_updated = True
        if self._terminal_operation_updated and normalized_sql.startswith(
            "INSERT INTO warehouse_bindings"
        ):
            raise sqlite3.OperationalError("private stable admission detail")
        return cursor

    def executescript(self, sql: str) -> sqlite3.Cursor:
        return self._connection.executescript(sql)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class RecordingControlService(WarehouseControlService):
    def __init__(self, repository: RecordingRepository, recorder: EventRecorder) -> None:
        super().__init__(repository, clock=lambda: NOW)
        self._recorder = recorder

    def transition(
        self,
        tenant_id: str,
        binding_id: str,
        lifecycle_state: WarehouseBindingState,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().transition(
            tenant_id,
            binding_id,
            lifecycle_state,
            expected_revision=expected_revision,
        )
        self._recorder.record(f"transition:{lifecycle_state.value}")
        return binding

    def record_validation(
        self,
        tenant_id: str,
        binding_id: str,
        evidence: WarehouseValidationEvidence,
        restore: WarehouseRestoreVerification,
        *,
        operation: PrivateWarehouseOperation,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().record_validation(
            tenant_id,
            binding_id,
            evidence,
            restore,
            expected_revision=expected_revision,
            operation=operation,
        )
        self._recorder.record("record_validation")
        return binding

    def record_suspension(
        self,
        operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().record_suspension(
            operation,
            expected_revision=expected_revision,
        )
        self._recorder.record("transition:suspended")
        return binding

    def record_resume_validation(
        self,
        tenant_id: str,
        binding_id: str,
        evidence: WarehouseResumeValidationEvidence,
        *,
        operation: PrivateWarehouseOperation,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().record_resume_validation(
            tenant_id,
            binding_id,
            evidence,
            expected_revision=expected_revision,
            operation=operation,
        )
        self._recorder.record("record_resume_validation")
        return binding

    def record_retirement(
        self,
        tenant_id: str,
        binding_id: str,
        evidence: WarehouseRetirementEvidence,
        *,
        operation: PrivateWarehouseOperation,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().record_retirement(
            tenant_id,
            binding_id,
            evidence,
            expected_revision=expected_revision,
            operation=operation,
        )
        self._recorder.record("record_retirement")
        return binding

    def record_terminal_operation_failure(
        self,
        operation: PrivateWarehouseOperation,
        classification: WarehouseFailureClassification,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = super().record_terminal_operation_failure(
            operation,
            classification,
            expected_revision=expected_revision,
        )
        self._recorder.record("record_terminal_operation_failure")
        return binding


class StrictWarehouseProvider:
    engine_kind = EngineKind.POSTGRESQL

    def __init__(self, recorder: EventRecorder) -> None:
        self._recorder = recorder
        self.failures: dict[str, list[BaseException]] = {}
        self.observed_engines: list[EngineKind] = []

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        self._validate_call(binding, operation, operation_kind=WarehouseOperationKind.PROVISION)
        self._raise_next("provider:provision")
        result = self._provision_result(operation)
        self._recorder.record("provider:provision")
        return result

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        self._validate_call(binding, operation, operation_kind=operation.operation_kind)
        self._raise_next("provider:reconcile")
        result = self._provision_result(operation)
        self._recorder.record("provider:reconcile")
        return result

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult | ResumeWarehouseValidationResult:
        expected_kind = (
            WarehouseOperationKind.RESUME if resume else WarehouseOperationKind.PROVISION
        )
        self._validate_call(binding, operation, operation_kind=expected_kind)
        event = "provider:validate:resume" if resume else "provider:validate:initial"
        self._raise_next(event)
        if resume:
            result: InitialWarehouseValidationResult | ResumeWarehouseValidationResult = (
                ResumeWarehouseValidationResult(evidence=_resume_evidence(binding))
            )
        else:
            restore = _restore_verification(binding)
            result = InitialWarehouseValidationResult(
                evidence=_validation_evidence(binding, restore),
                restore_verification=restore,
            )
        self._recorder.record(event)
        return result

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._validate_call(binding, operation, operation_kind=WarehouseOperationKind.SUSPEND)
        self._raise_next("provider:suspend")
        self._recorder.record("provider:suspend")

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._validate_call(binding, operation, operation_kind=WarehouseOperationKind.RESUME)
        self._raise_next("provider:resume")
        self._recorder.record("provider:resume")

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        self._validate_call(binding, operation, operation_kind=WarehouseOperationKind.RETIRE)
        self._raise_next("provider:retire")
        result = _retirement_evidence(binding)
        self._recorder.record("provider:retire")
        return result

    def _validate_call(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        operation_kind: WarehouseOperationKind,
    ) -> None:
        assert WarehouseBinding.model_validate(binding.model_dump()) == binding
        assert PrivateWarehouseOperation.model_validate(operation.model_dump()) == operation
        assert binding.tenant_id == operation.tenant_id
        assert binding.binding_id == operation.binding_id
        assert binding.engine_kind is operation.engine_kind is self.engine_kind
        assert operation.operation_kind is operation_kind
        self.observed_engines.append(binding.engine_kind)

    def _raise_next(self, event: str) -> None:
        queued = self.failures.get(event, [])
        if queued:
            raise queued.pop(0)

    @staticmethod
    def _provision_result(operation: PrivateWarehouseOperation) -> WarehouseProvisionResult:
        return WarehouseProvisionResult(
            tenant_id=operation.tenant_id,
            binding_id=operation.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=operation.engine_kind,
            private_resource_handle="private-resource-handle",
            provider_build_digest="a" * 64,
            resource_inventory_digest="b" * 64,
        )


class WrongValidationOwnershipProvider(StrictWarehouseProvider):
    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult | ResumeWarehouseValidationResult:
        result = super().validate(binding, operation, resume=resume)
        if isinstance(result, InitialWarehouseValidationResult):
            evidence = WarehouseValidationEvidence.model_validate(
                {**result.evidence.model_dump(), "tenant_id": "tenant-other"}
            )
            return InitialWarehouseValidationResult(
                evidence=evidence,
                restore_verification=result.restore_verification,
            )
        return result


class ReturningSuspendProvider(StrictWarehouseProvider):
    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        super().suspend(binding, operation)
        return cast(None, "private-result-canary")


class LostSuspendResponseProvider(StrictWarehouseProvider):
    def __init__(self, recorder: EventRecorder) -> None:
        super().__init__(recorder)
        self.suspend_effect_count = 0

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._validate_call(binding, operation, operation_kind=WarehouseOperationKind.SUSPEND)
        self.suspend_effect_count += 1
        self._recorder.record("provider:suspend")
        if self.suspend_effect_count == 1:
            raise _provider_error(
                "suspend",
                WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
            )


class MalformedRetirementProvider(StrictWarehouseProvider):
    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        super().retire(binding, operation)
        return cast(WarehouseRetirementEvidence, object())


class MalformedProvisionProvider(StrictWarehouseProvider):
    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        result = super().provision(binding, operation)
        return WarehouseProvisionResult.model_construct(
            **{
                **result.model_dump(),
                "private_resource_handle": None,
            }
        )


def _new_orchestrator(
    *,
    stop_after: str | None = None,
    provider_type: type[StrictWarehouseProvider] = StrictWarehouseProvider,
) -> tuple[
    WarehouseLifecycleOrchestrator,
    RecordingControlService,
    RecordingRepository,
    StrictWarehouseProvider,
    EventRecorder,
]:
    recorder = EventRecorder(stop_after=stop_after)
    repository = RecordingRepository(recorder)
    control = RecordingControlService(repository, recorder)
    provider = provider_type(recorder)
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=provider,
        clock=lambda: NOW,
    )
    return orchestrator, control, repository, provider, recorder


def _draft(control: WarehouseControlService) -> WarehouseBinding:
    return control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )


def _restore_verification(binding: WarehouseBinding) -> WarehouseRestoreVerification:
    return WarehouseRestoreVerification(
        verification_id="wrv-orchestration",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=binding.engine_kind,
        source_backup_artifact_digest="1" * 64,
        representative_data_digest="2" * 64,
        schema_metadata_digest="3" * 64,
        principal_profile_digest="4" * 64,
        integrity_marker_digest="5" * 64,
        query_behavior_digest="6" * 64,
        verified_at=OBSERVED_AT,
    )


def _validation_evidence(
    binding: WarehouseBinding, restore: WarehouseRestoreVerification
) -> WarehouseValidationEvidence:
    return WarehouseValidationEvidence(
        evidence_id="wev-orchestration",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        validation_profile=WarehouseValidationProfile.PRODUCTION,
        engine_kind=binding.engine_kind,
        engine_version="18.6",
        engine_build_digest="7" * 64,
        engine_image_digest="8" * 64,
        principal_profile_digest=restore.principal_profile_digest,
        namespace_grant_matrix_digest="9" * 64,
        tls_probe_digest="a" * 64,
        network_isolation_probe_digest="b" * 64,
        encryption_at_rest_evidence_digest="c" * 64,
        encryption_at_rest_disposition=EncryptionAtRestDisposition.PROVEN,
        positive_probe_digest="d" * 64,
        denial_probe_digest="e" * 64,
        ledger_probe_digest="f" * 64,
        monitoring_probe_digest="0" * 64,
        capacity_alert_probe_digest="1" * 64,
        backup_artifact_digest=restore.source_backup_artifact_digest,
        restore_verification_digest=digest(restore),
        restore_cleanup_digest="2" * 64,
        observed_at=OBSERVED_AT,
    )


def _resume_evidence(binding: WarehouseBinding) -> WarehouseResumeValidationEvidence:
    return WarehouseResumeValidationEvidence(
        evidence_id="wrev-orchestration",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=binding.engine_kind,
        engine_version="18.6",
        engine_build_digest="0" * 64,
        engine_image_digest="1" * 64,
        tls_probe_digest="2" * 64,
        network_isolation_probe_digest="3" * 64,
        monitoring_probe_digest="4" * 64,
        positive_probe_digest="5" * 64,
        denial_probe_digest="6" * 64,
        storage_integrity_probe_digest="7" * 64,
        observed_at=OBSERVED_AT,
    )


def _retirement_evidence(binding: WarehouseBinding) -> WarehouseRetirementEvidence:
    return WarehouseRetirementEvidence(
        evidence_id="wret-orchestration",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        resource_inventory_digest=digest(
            {"domain": "warehouse_retirement_resource_inventory_v1", "resources": []}
        ),
        cleanup_disposition_digest=digest(
            {"domain": "warehouse_retirement_cleanup_disposition_v1", "resources": []}
        ),
        retention_policy_digest=digest(
            {"domain": "warehouse_retirement_retention_policy_v1", "resources": []}
        ),
        completed_resource_count=0,
        retained_resource_count=0,
        cleanup_failed_resource_count=0,
        observed_at=OBSERVED_AT,
    )


def _provider_error(
    operation: ProviderOperation, classification: WarehouseFailureClassification
) -> WarehouseProviderError:
    return WarehouseProviderError(operation=operation, classification=classification)


def test_provider_protocol_exposes_exactly_six_operations() -> None:
    implemented = {
        name
        for name, value in WarehouseProvider.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert implemented == {"provision", "reconcile", "validate", "suspend", "resume", "retire"}


def test_provision_orders_transition_claim_provider_and_evidence_admission() -> None:
    orchestrator, control, repository, _, recorder = _new_orchestrator()
    binding = _draft(control)

    ready = orchestrator.provision(
        binding.tenant_id,
        binding.binding_id,
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert recorder.events == [
        "transition:provisioning",
        "claim:2",
        "provider:provision",
        "transition:validating",
        "provider:validate:initial",
        "record_validation",
    ]
    expected_operation_id = (
        "wop-"
        + digest(
            {
                "domain": "heinzel-warehouse-operation-v1",
                "tenant_id": binding.tenant_id,
                "sequence": 1,
            }
        )[:24]
    )
    operation = repository.load_operation(
        binding.tenant_id, binding.binding_id, expected_operation_id
    )
    assert operation.status is WarehouseOperationStatus.SUCCEEDED
    assert operation.phase is WarehouseOperationPhase.VALIDATED
    assert repository.operation_sequence_allocations == 1


@pytest.mark.parametrize(
    "stop_after",
    (
        "transition:provisioning",
        "claim:2",
        "provider:provision",
        "transition:validating",
        "provider:validate:initial",
        "record_validation",
    ),
)
def test_provision_replay_resumes_each_durable_edge_without_repeating_create(
    stop_after: str,
) -> None:
    orchestrator, control, repository, _, recorder = _new_orchestrator(stop_after=stop_after)
    binding = _draft(control)

    with pytest.raises(SimulatedStop):
        orchestrator.provision(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
        )

    ready = orchestrator.provision(
        binding.tenant_id,
        binding.binding_id,
        expected_revision=binding.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert recorder.events.count("provider:provision") <= 1
    if stop_after != "transition:provisioning":
        assert repository.operation_sequence_allocations == 1
    if stop_after not in {"transition:provisioning", "record_validation"}:
        assert "provider:reconcile" in recorder.events
    assert repository.load_live_operation(binding.tenant_id, binding.binding_id) is None


def test_next_lifecycle_action_closes_a_stale_validating_operation_at_terminal_phase() -> None:
    orchestrator, control, repository, _, _ = _new_orchestrator(stop_after="record_validation")
    draft = _draft(control)

    with pytest.raises(SimulatedStop):
        orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )
    ready = control.get(draft.tenant_id, draft.binding_id)

    suspended = orchestrator.suspend(
        ready.tenant_id,
        ready.binding_id,
        expected_revision=ready.revision,
    )

    assert suspended.lifecycle_state is WarehouseBindingState.SUSPENDED
    assert repository.load_live_operation(ready.tenant_id, ready.binding_id) is None


@pytest.mark.parametrize(
    ("lifecycle_action", "stop_event", "terminal_state"),
    (
        ("provision", "record_validation", WarehouseBindingState.READY),
        ("suspend", "transition:suspended", WarehouseBindingState.SUSPENDED),
        ("resume", "record_resume_validation", WarehouseBindingState.READY),
        ("retire", "record_retirement", WarehouseBindingState.RETIRED),
    ),
)
def test_stable_binding_admission_atomically_closes_its_operation(
    lifecycle_action: str,
    stop_event: str,
    terminal_state: WarehouseBindingState,
) -> None:
    orchestrator, control, repository, _, recorder = _new_orchestrator(
        stop_after="record_validation" if lifecycle_action == "provision" else None
    )
    draft = _draft(control)
    if lifecycle_action == "provision":
        source = draft
    else:
        source = orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )
        recorder.stop_after(stop_event)
        if lifecycle_action == "resume":
            source = orchestrator.suspend(
                source.tenant_id,
                source.binding_id,
                expected_revision=source.revision,
            )
            recorder.stop_after(stop_event)

    with pytest.raises(SimulatedStop):
        getattr(orchestrator, lifecycle_action)(
            source.tenant_id,
            source.binding_id,
            expected_revision=source.revision,
        )

    stable = control.get(source.tenant_id, source.binding_id)
    assert stable.lifecycle_state is terminal_state
    assert repository.load_live_operation(source.tenant_id, source.binding_id) is None


def test_stable_admission_rollback_keeps_the_binding_and_operation_replayable() -> None:
    orchestrator, control, repository, _, recorder = _new_orchestrator()
    draft = _draft(control)
    connection = repository._connection
    repository._connection = StableAdmissionConnection(connection)

    with pytest.raises(WarehousePersistenceError) as failure:
        orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )

    repository._connection = connection
    assert "private stable admission detail" not in str(failure.value)
    validating = control.get(draft.tenant_id, draft.binding_id)
    assert validating.lifecycle_state is WarehouseBindingState.VALIDATING
    live = repository.load_live_operation(draft.tenant_id, draft.binding_id)
    assert live is not None
    assert live.status is WarehouseOperationStatus.RUNNING
    assert live.phase is WarehouseOperationPhase.VALIDATING

    ready = orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert repository.load_live_operation(draft.tenant_id, draft.binding_id) is None
    assert recorder.events.count("provider:provision") == 1


def test_claim_replay_loads_before_allocating_and_preserves_the_frozen_engine() -> None:
    orchestrator, control, repository, provider, _ = _new_orchestrator(stop_after="claim:2")
    draft = _draft(control)

    with pytest.raises(SimulatedStop):
        orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )
    provisioning = control.get(draft.tenant_id, draft.binding_id)
    with pytest.raises(ValueError, match="immutable after draft"):
        control.revise_draft(
            draft.tenant_id,
            draft.binding_id,
            engine_kind=EngineKind.CLICKHOUSE,
            expected_revision=provisioning.revision,
        )

    ready = orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )

    assert ready.engine_kind is EngineKind.POSTGRESQL
    assert provider.observed_engines and set(provider.observed_engines) == {EngineKind.POSTGRESQL}
    assert repository.operation_sequence_allocations == 1


def test_full_lifecycle_uses_suspend_resume_validation_and_retirement_operations() -> None:
    orchestrator, control, _, _, recorder = _new_orchestrator()
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )
    recorder.events.clear()

    suspended = orchestrator.suspend(
        ready.tenant_id, ready.binding_id, expected_revision=ready.revision
    )
    resumed = orchestrator.resume(
        suspended.tenant_id,
        suspended.binding_id,
        expected_revision=suspended.revision,
    )
    retired = orchestrator.retire(
        resumed.tenant_id,
        resumed.binding_id,
        expected_revision=resumed.revision,
    )

    assert retired.lifecycle_state is WarehouseBindingState.RETIRED
    assert recorder.events == [
        f"claim:{ready.revision}",
        "provider:suspend",
        "transition:suspended",
        f"claim:{suspended.revision}",
        "provider:resume",
        "provider:validate:resume",
        "record_resume_validation",
        "transition:retiring",
        f"claim:{resumed.revision + 1}",
        "provider:retire",
        "record_retirement",
    ]


def test_retirement_atomically_closes_its_operation_before_stable_replay() -> None:
    orchestrator, control, repository, _, _ = _new_orchestrator()
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )
    retiring = control.transition(
        ready.tenant_id,
        ready.binding_id,
        WarehouseBindingState.RETIRING,
        expected_revision=ready.revision,
    )
    operation = PrivateWarehouseOperation(
        tenant_id=retiring.tenant_id,
        binding_id=retiring.binding_id,
        binding_revision=retiring.revision,
        operation_id="wop-completed-retirement-replay",
        operation_kind=WarehouseOperationKind.RETIRE,
        engine_kind=retiring.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )
    repository.claim_operation(operation)
    running = operation.model_copy(update={"status": WarehouseOperationStatus.RUNNING})
    repository.save_operation(operation, running)
    retired = control.record_retirement(
        retiring.tenant_id,
        retiring.binding_id,
        _retirement_evidence(retiring),
        operation=running,
        expected_revision=retiring.revision,
    )

    replayed = orchestrator.retire(
        retired.tenant_id,
        retired.binding_id,
        expected_revision=retiring.revision,
    )

    assert replayed == retired
    assert repository.load_live_operation(retired.tenant_id, retired.binding_id) is None
    completed = repository.load_operation(
        operation.tenant_id, operation.binding_id, operation.operation_id
    )
    assert completed.status is WarehouseOperationStatus.SUCCEEDED
    assert completed.phase is WarehouseOperationPhase.RETIRED


def _advance_replay_binding(
    repository: SQLiteWarehouseRepository,
    binding: WarehouseBinding,
    lifecycle_state: WarehouseBindingState,
) -> WarehouseBinding:
    advanced = WarehouseBinding.model_validate(
        {
            **binding.model_dump(),
            "lifecycle_state": lifecycle_state,
            "revision": binding.revision + 1,
            "updated_at": OBSERVED_AT,
            "provisioned_at": (
                NOW
                if lifecycle_state
                in {
                    WarehouseBindingState.READY,
                    WarehouseBindingState.SUSPENDED,
                    WarehouseBindingState.RETIRING,
                    WarehouseBindingState.RETIRED,
                }
                else binding.provisioned_at
            ),
        }
    )
    repository.save(advanced)
    return advanced


def _claim_replay_operation(
    repository: SQLiteWarehouseRepository,
    binding: WarehouseBinding,
    operation_kind: WarehouseOperationKind,
    replay_path: StableReplayPath,
) -> PrivateWarehouseOperation:
    operation = PrivateWarehouseOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=f"wop-{replay_path}-{binding.revision}",
        operation_kind=operation_kind,
        engine_kind=binding.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )
    assert repository.claim_operation(operation)
    return operation


def _stable_replay_scenario(
    replay_path: StableReplayPath,
    *,
    matching_revision: bool,
) -> tuple[
    RecordingRepository,
    EventRecorder,
    WarehouseBinding,
    PrivateWarehouseOperation,
    Callable[[], object],
]:
    orchestrator, control, repository, _, recorder = _new_orchestrator()
    binding = _draft(control)
    operation_kind = {
        "provision": WarehouseOperationKind.PROVISION,
        "suspend": WarehouseOperationKind.SUSPEND,
        "resume": WarehouseOperationKind.RESUME,
        "retire": WarehouseOperationKind.RETIRE,
        "delete_retained": WarehouseOperationKind.RETIRE,
    }[replay_path]

    if replay_path == "provision":
        initial_revision = binding.revision
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.PROVISIONING)
        if matching_revision:
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.VALIDATING)
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.READY)
        # The four replay paths return different artifacts; the binding covers both.
        invoke: partial[WarehouseBinding] | partial[WarehouseRetirementEvidence] = partial(
            orchestrator.provision,
            binding.tenant_id,
            binding.binding_id,
            expected_revision=initial_revision,
        )
    elif replay_path == "suspend":
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.READY)
        operation_revision = binding.revision
        if matching_revision:
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.SUSPENDED)
        invoke = partial(
            orchestrator.suspend,
            binding.tenant_id,
            binding.binding_id,
            expected_revision=operation_revision,
        )
    elif replay_path == "resume":
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.SUSPENDED)
        operation_revision = binding.revision
        if matching_revision:
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.READY)
        invoke = partial(
            orchestrator.resume,
            binding.tenant_id,
            binding.binding_id,
            expected_revision=operation_revision,
        )
    elif replay_path == "retire":
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.RETIRING)
        operation_revision = binding.revision
        if matching_revision:
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
        binding = _advance_replay_binding(repository, binding, WarehouseBindingState.RETIRED)
        invoke = partial(
            orchestrator.retire,
            binding.tenant_id,
            binding.binding_id,
            expected_revision=operation_revision,
        )
    else:
        if matching_revision:
            binding = _advance_replay_binding(repository, binding, WarehouseBindingState.RETIRED)
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
        else:
            binding = _advance_replay_binding(repository, binding, WarehouseBindingState.RETIRING)
            operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
            binding = _advance_replay_binding(repository, binding, WarehouseBindingState.RETIRED)
        repository.record_retained_resource_deletion(
            _retirement_evidence(binding),
            expected_revision=binding.revision,
        )
        invoke = partial(
            orchestrator.delete_retained_resources,
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
            authorized=True,
        )

    if not matching_revision and replay_path != "delete_retained":
        operation = _claim_replay_operation(repository, binding, operation_kind, replay_path)
    return repository, recorder, binding, operation, invoke


@pytest.mark.parametrize(
    "replay_path",
    ("provision", "suspend", "resume", "retire", "delete_retained"),
)
def test_stable_replay_closes_only_the_exact_transition_revision(
    replay_path: StableReplayPath,
) -> None:
    repository, recorder, binding, operation, invoke = _stable_replay_scenario(
        replay_path,
        matching_revision=True,
    )
    provider_events = tuple(event for event in recorder.events if event.startswith("provider:"))

    assert invoke() is not None

    assert repository.load_live_operation(binding.tenant_id, binding.binding_id) is None
    completed = repository.load_operation(
        operation.tenant_id, operation.binding_id, operation.operation_id
    )
    assert completed.status is WarehouseOperationStatus.SUCCEEDED
    assert (
        tuple(event for event in recorder.events if event.startswith("provider:"))
        == provider_events
    )


@pytest.mark.parametrize(
    "replay_path",
    ("provision", "suspend", "resume", "retire", "delete_retained"),
)
def test_stable_replay_refuses_to_certify_an_operation_for_the_wrong_revision(
    replay_path: StableReplayPath,
) -> None:
    repository, recorder, binding, operation, invoke = _stable_replay_scenario(
        replay_path,
        matching_revision=False,
    )
    provider_events = tuple(event for event in recorder.events if event.startswith("provider:"))

    with pytest.raises(WarehouseOperationConflictError, match="stable binding transition"):
        invoke()

    assert repository.load_live_operation(binding.tenant_id, binding.binding_id) == operation
    assert (
        tuple(event for event in recorder.events if event.startswith("provider:"))
        == provider_events
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
def test_completed_replay_advances_a_claimed_operation_through_legal_phases(
    operation_kind: WarehouseOperationKind,
    terminal_phase: WarehouseOperationPhase,
) -> None:
    orchestrator, control, repository, _, _ = _new_orchestrator()
    binding = _draft(control)
    operation = PrivateWarehouseOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=f"wop-completed-{operation_kind.value}-replay",
        operation_kind=operation_kind,
        engine_kind=binding.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )
    repository.claim_operation(operation)

    orchestrator._finish_operation(operation, phase=terminal_phase)

    completed = repository.load_operation(
        operation.tenant_id, operation.binding_id, operation.operation_id
    )
    assert completed.status is WarehouseOperationStatus.SUCCEEDED
    assert completed.phase is terminal_phase


@pytest.mark.parametrize(
    ("operation_kind", "intermediate_phases", "terminal_phase"),
    (
        (
            WarehouseOperationKind.PROVISION,
            (
                WarehouseOperationPhase.PROVIDER_CREATED,
                WarehouseOperationPhase.VALIDATING,
            ),
            WarehouseOperationPhase.VALIDATED,
        ),
        (
            WarehouseOperationKind.RESUME,
            (
                WarehouseOperationPhase.RESUMED,
                WarehouseOperationPhase.VALIDATING,
            ),
            WarehouseOperationPhase.VALIDATED,
        ),
        (
            WarehouseOperationKind.RETIRE,
            (),
            WarehouseOperationPhase.RETIRED,
        ),
    ),
)
def test_completed_replay_normalizes_reconciliation_before_advancing_phase(
    operation_kind: WarehouseOperationKind,
    intermediate_phases: tuple[WarehouseOperationPhase, ...],
    terminal_phase: WarehouseOperationPhase,
) -> None:
    orchestrator, control, repository, _, _ = _new_orchestrator()
    binding = _draft(control)
    operation = PrivateWarehouseOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=f"wop-reconciled-{operation_kind.value}-replay",
        operation_kind=operation_kind,
        engine_kind=binding.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )
    repository.claim_operation(operation)
    for intermediate_phase in intermediate_phases:
        operation = orchestrator._save_operation_update(
            operation,
            status=WarehouseOperationStatus.RUNNING,
            phase=intermediate_phase,
        )
    operation = orchestrator._save_operation_update(
        operation,
        status=WarehouseOperationStatus.RECONCILING,
    )

    orchestrator._finish_operation(operation, phase=terminal_phase)

    completed = repository.load_operation(
        operation.tenant_id, operation.binding_id, operation.operation_id
    )
    assert completed.status is WarehouseOperationStatus.SUCCEEDED
    assert completed.phase is terminal_phase


def test_stale_live_operation_is_reconciled_and_closed_before_a_new_claim() -> None:
    orchestrator, control, repository, _, recorder = _new_orchestrator()
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )
    completed = repository.load_operation(
        ready.tenant_id,
        ready.binding_id,
        "wop-"
        + digest(
            {
                "domain": "heinzel-warehouse-operation-v1",
                "tenant_id": ready.tenant_id,
                "sequence": 1,
            }
        )[:24],
    )
    legacy_live = completed.model_copy(
        update={
            "status": WarehouseOperationStatus.RUNNING,
            "phase": WarehouseOperationPhase.VALIDATED,
            "updated_at": NOW,
        }
    )
    injected = repository._connection.execute(
        "UPDATE private_warehouse_operations SET status = ?, payload = ? "
        "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ? "
        "AND operation_id = ?",
        (
            legacy_live.status.value,
            canonical_bytes(legacy_live),
            legacy_live.tenant_id,
            legacy_live.binding_id,
            legacy_live.binding_revision,
            legacy_live.operation_id,
        ),
    )
    assert injected.rowcount == 1
    repository._connection.commit()
    recorder.events.clear()

    suspended = orchestrator.suspend(
        ready.tenant_id, ready.binding_id, expected_revision=ready.revision
    )

    assert suspended.lifecycle_state is WarehouseBindingState.SUSPENDED
    assert recorder.events[:3] == [
        "provider:reconcile",
        f"claim:{ready.revision}",
        "provider:suspend",
    ]
    assert (
        repository.load_operation(ready.tenant_id, ready.binding_id, completed.operation_id).status
        is WarehouseOperationStatus.SUCCEEDED
    )


@pytest.mark.parametrize(
    "classification",
    (
        WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        WarehouseFailureClassification.THROTTLED,
    ),
)
def test_resumable_provider_failures_leave_binding_and_operation_resumable(
    classification: WarehouseFailureClassification,
) -> None:
    orchestrator, control, repository, provider, _ = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [_provider_error("provision", classification)]

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value.classification is classification
    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.PROVISIONING
    )
    live = repository.load_live_operation(draft.tenant_id, draft.binding_id)
    assert live is not None
    assert live.status is WarehouseOperationStatus.RUNNING
    assert live.failure_classification is None


def test_ambiguous_provider_effect_reconciles_before_progressing() -> None:
    orchestrator, control, _, provider, recorder = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [
        _provider_error("provision", WarehouseFailureClassification.AMBIGUOUS_OUTCOME)
    ]

    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert recorder.events.index("provider:reconcile") < recorder.events.index(
        "transition:validating"
    )


def test_lost_suspend_response_reconciles_without_repeating_the_external_effect() -> None:
    orchestrator, control, _, provider, recorder = _new_orchestrator(
        provider_type=LostSuspendResponseProvider
    )
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )
    recorder.events.clear()

    suspended = orchestrator.suspend(
        ready.tenant_id,
        ready.binding_id,
        expected_revision=ready.revision,
    )

    assert suspended.lifecycle_state is WarehouseBindingState.SUSPENDED
    assert isinstance(provider, LostSuspendResponseProvider)
    assert provider.suspend_effect_count == 1
    assert recorder.events == [
        f"claim:{ready.revision}",
        "provider:suspend",
        "provider:reconcile",
        "transition:suspended",
    ]


@pytest.mark.parametrize(
    "classification",
    (
        WarehouseFailureClassification.AUTHORIZATION_DENIED,
        WarehouseFailureClassification.STATEMENT_REJECTED,
        WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        WarehouseFailureClassification.INTEGRITY_FAILURE,
        WarehouseFailureClassification.PERMANENT_CONFIGURATION,
    ),
)
def test_terminal_provider_failure_records_terminal_operation_and_binding(
    classification: WarehouseFailureClassification,
) -> None:
    orchestrator, control, repository, provider, _ = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [_provider_error("provision", classification)]

    with pytest.raises(WarehouseProviderError):
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.FAILED
    )
    assert repository.load_live_operation(draft.tenant_id, draft.binding_id) is None


def test_terminal_failure_transaction_rollback_replays_by_reconciling_the_same_operation() -> None:
    orchestrator, control, repository, provider, recorder = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [
        _provider_error("provision", WarehouseFailureClassification.INTEGRITY_FAILURE)
    ]
    connection = repository._connection
    repository._connection = TerminalFailureConnection(connection)

    with pytest.raises(WarehousePersistenceError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    repository._connection = connection
    assert "private terminal failure detail" not in str(failure.value)
    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.PROVISIONING
    )
    live = repository.load_live_operation(draft.tenant_id, draft.binding_id)
    assert live is not None
    assert live.status is WarehouseOperationStatus.RUNNING
    sequence_allocations = repository.operation_sequence_allocations

    ready = orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )

    assert ready.lifecycle_state is WarehouseBindingState.READY
    assert repository.operation_sequence_allocations == sequence_allocations
    assert "provider:reconcile" in recorder.events
    assert recorder.events.count("provider:provision") == 0


def test_interruption_after_atomic_terminal_failure_never_reallocates_or_repeats_effect() -> None:
    orchestrator, control, repository, provider, recorder = _new_orchestrator(
        stop_after="record_terminal_operation_failure"
    )
    draft = _draft(control)
    provider.failures["provider:provision"] = [
        _provider_error("provision", WarehouseFailureClassification.INTEGRITY_FAILURE)
    ]

    with pytest.raises(SimulatedStop):
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.FAILED
    )
    assert repository.load_live_operation(draft.tenant_id, draft.binding_id) is None
    sequence_allocations = repository.operation_sequence_allocations
    provider_events = tuple(event for event in recorder.events if event.startswith("provider:"))

    with pytest.raises(ValueError, match="recoverable binding state"):
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert repository.operation_sequence_allocations == sequence_allocations
    assert (
        tuple(event for event in recorder.events if event.startswith("provider:"))
        == provider_events
    )


def test_provision_result_is_frozen_strict_and_rejects_unknown_or_wrong_fields() -> None:
    result = WarehouseProvisionResult(
        tenant_id="tenant-a",
        binding_id="whb-test",
        binding_revision=2,
        operation_id="wop-111111111111111111111111",
        engine_kind=EngineKind.POSTGRESQL,
        private_resource_handle="private-resource-handle",
        provider_build_digest="a" * 64,
        resource_inventory_digest="b" * 64,
    )

    with pytest.raises(ValidationError, match="frozen"):
        result.private_resource_handle = "replacement"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WarehouseProvisionResult.model_validate(
            {**result.model_dump(), "raw_diagnostic": "private"}
        )
    with pytest.raises(ValidationError):
        WarehouseProvisionResult.model_validate(
            {**result.model_dump(), "private_resource_handle": None}
        )


def test_malformed_provision_model_is_normalized_before_string_operations() -> None:
    orchestrator, control, _, _, _ = _new_orchestrator(provider_type=MalformedProvisionProvider)
    draft = _draft(control)

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_raw_provider_exception_is_sanitized_and_never_crosses_the_boundary() -> None:
    raw_diagnostic = "driver endpoint=/private/socket credential=secret-canary"
    orchestrator, control, _, provider, _ = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [RuntimeError(raw_diagnostic)]

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert raw_diagnostic not in str(failure.value)
    assert failure.value.__cause__ is None


def test_typed_provider_error_is_revalidated_and_replaced_without_driver_state() -> None:
    raw_diagnostic = "driver endpoint=/private/socket credential=typed-secret-canary"
    driver_error = _provider_error(
        "provision", WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    )
    driver_error.args = (raw_diagnostic,)
    driver_error.__cause__ = RuntimeError(raw_diagnostic)
    orchestrator, control, _, provider, _ = _new_orchestrator()
    draft = _draft(control)
    provider.failures["provider:provision"] = [driver_error]

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value is not driver_error
    assert failure.value.args == ("warehouse provider provision failed: transient_unavailable",)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert raw_diagnostic not in str(failure.value)
    assert "_raise_next" not in {
        frame.name for frame in traceback.extract_tb(failure.value.__traceback__)
    }


def test_provider_engine_mismatch_fails_before_any_provider_effect() -> None:
    orchestrator, control, _, provider, recorder = _new_orchestrator()
    draft = _draft(control)
    provider.engine_kind = EngineKind.CLICKHOUSE

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value.classification is WarehouseFailureClassification.PERMANENT_CONFIGURATION
    assert not any(event.startswith("provider:") for event in recorder.events)


def test_misowned_validation_result_is_a_terminal_sanitized_provider_failure() -> None:
    orchestrator, control, _, _, _ = _new_orchestrator(
        provider_type=WrongValidationOwnershipProvider
    )
    draft = _draft(control)

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.provision(draft.tenant_id, draft.binding_id, expected_revision=draft.revision)

    assert failure.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert control.get(draft.tenant_id, draft.binding_id).lifecycle_state is (
        WarehouseBindingState.FAILED
    )


def test_void_provider_operation_rejects_a_private_result_without_exposing_it() -> None:
    orchestrator, control, _, _, _ = _new_orchestrator(provider_type=ReturningSuspendProvider)
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.suspend(ready.tenant_id, ready.binding_id, expected_revision=ready.revision)

    assert failure.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert "private-result-canary" not in str(failure.value)


def test_malformed_retirement_result_is_a_sanitized_provider_failure() -> None:
    orchestrator, control, _, _, _ = _new_orchestrator(provider_type=MalformedRetirementProvider)
    draft = _draft(control)
    ready = orchestrator.provision(
        draft.tenant_id, draft.binding_id, expected_revision=draft.revision
    )

    with pytest.raises(WarehouseProviderError) as failure:
        orchestrator.retire(ready.tenant_id, ready.binding_id, expected_revision=ready.revision)

    assert failure.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
