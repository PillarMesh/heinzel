from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Never, TypeVar

from heinzel_contract_model import digest

from .errors import (
    WarehouseAdmissionError,
    WarehouseOperationConflictError,
    WarehouseProviderError,
    WarehouseProviderOperation,
)
from .evidence import WarehouseRetirementEvidence
from .faults import (
    WarehouseFaultHook,
    WarehouseLifecycleCheckpoint,
    noop_warehouse_fault_hook,
)
from .models import (
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
)
from .private_state import (
    PrivateWarehouseOperation,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseResourceCleanupStatus,
)
from .protocols import (
    InitialWarehouseValidationResult,
    ResumeWarehouseValidationResult,
    WarehouseProvider,
    WarehouseProvisionResult,
)
from .repository import WarehouseRepository
from .retirement import canonical_retirement_resource_snapshot, retirement_evidence_mismatch
from .service import WarehouseControlService

_RESUMABLE_FAILURES = frozenset(
    {
        WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        WarehouseFailureClassification.THROTTLED,
        WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
    }
)
_TERMINAL_FAILURE_STATES = frozenset(
    {WarehouseBindingState.PROVISIONING, WarehouseBindingState.VALIDATING}
)
_TERMINAL_OPERATION_PHASES = {
    WarehouseOperationKind.PROVISION: WarehouseOperationPhase.VALIDATED,
    WarehouseOperationKind.SUSPEND: WarehouseOperationPhase.SUSPENDED,
    WarehouseOperationKind.RESUME: WarehouseOperationPhase.VALIDATED,
    WarehouseOperationKind.RETIRE: WarehouseOperationPhase.RETIRED,
}
_Result = TypeVar("_Result")


class WarehouseLifecycleOrchestrator:
    def __init__(
        self,
        *,
        control: WarehouseControlService,
        repository: WarehouseRepository,
        provider: WarehouseProvider,
        clock: Callable[[], datetime],
        fault_hook: WarehouseFaultHook = noop_warehouse_fault_hook,
    ) -> None:
        self._control = control
        self._repository = repository
        self._provider = provider
        self._clock = clock
        self._fault_hook = fault_hook

    def provision(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self._control.get(tenant_id, binding_id)
        if binding.lifecycle_state is WarehouseBindingState.DRAFT:
            self._assert_revision(binding, expected_revision)
            binding = self._control.transition(
                tenant_id,
                binding_id,
                WarehouseBindingState.PROVISIONING,
                expected_revision=binding.revision,
            )
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_PROVISIONING_TRANSITION)
        else:
            self._assert_provision_replay(binding, expected_revision)

        self._assert_provider_engine(binding, operation="provision")
        if binding.lifecycle_state is WarehouseBindingState.READY:
            self._close_completed_replay(
                binding,
                WarehouseOperationKind.PROVISION,
                operation_revision=binding.revision - 2,
            )
            return binding

        operation_revision = (
            binding.revision
            if binding.lifecycle_state is WarehouseBindingState.PROVISIONING
            else binding.revision - 1
        )
        operation, adopted = self._claim_operation(
            binding,
            operation_kind=WarehouseOperationKind.PROVISION,
            operation_revision=operation_revision,
        )
        if adopted:
            provision_result, operation = self._reconcile(binding, operation)
        else:
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
            )
            provision_result, operation = self._provision(binding, operation)
        operation = self._record_provision_result(operation, provision_result)

        if binding.lifecycle_state is WarehouseBindingState.PROVISIONING:
            binding = self._control.transition(
                tenant_id,
                binding_id,
                WarehouseBindingState.VALIDATING,
                expected_revision=binding.revision,
            )
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_VALIDATING_TRANSITION)
        operation = self._save_operation_update(
            operation,
            status=WarehouseOperationStatus.RUNNING,
            phase=WarehouseOperationPhase.VALIDATING,
        )
        validation, operation = self._validate(binding, operation, resume=False)
        if not isinstance(validation, InitialWarehouseValidationResult):
            self._fail_invalid_result(binding, operation, operation_name="validate")
        self._fault_hook(WarehouseLifecycleCheckpoint.BEFORE_VALIDATION_ADMISSION)
        try:
            ready = self._control.record_validation(
                tenant_id,
                binding_id,
                validation.evidence,
                validation.restore_verification,
                expected_revision=binding.revision,
                operation=operation,
            )
        except WarehouseAdmissionError:
            self._fail_invalid_result(binding, operation, operation_name="validate")
        return ready

    def suspend(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self._control.get(tenant_id, binding_id)
        if binding.lifecycle_state is WarehouseBindingState.SUSPENDED:
            if binding.revision != expected_revision + 1:
                raise ValueError("binding revision is stale")
            self._close_completed_replay(
                binding,
                WarehouseOperationKind.SUSPEND,
                operation_revision=binding.revision - 1,
            )
            return binding
        self._assert_revision(binding, expected_revision)
        if binding.lifecycle_state is not WarehouseBindingState.READY:
            raise ValueError("warehouse suspension requires ready binding state")
        self._assert_provider_engine(binding, operation="suspend")
        operation, adopted = self._claim_operation(
            binding,
            operation_kind=WarehouseOperationKind.SUSPEND,
            operation_revision=binding.revision,
        )
        if adopted:
            _, operation = self._reconcile(binding, operation)
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
                phase=WarehouseOperationPhase.SUSPENDED,
            )
        if operation.phase is not WarehouseOperationPhase.SUSPENDED:
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
            )
            operation = self._void_effect("suspend", binding, operation, self._provider.suspend)
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_SUSPEND_EFFECT)
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
                phase=WarehouseOperationPhase.SUSPENDED,
            )
        suspended = self._control.record_suspension(
            operation,
            expected_revision=binding.revision,
        )
        return suspended

    def resume(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self._control.get(tenant_id, binding_id)
        if binding.lifecycle_state is WarehouseBindingState.READY:
            if binding.revision != expected_revision + 1:
                raise ValueError("binding revision is stale")
            self._close_completed_replay(
                binding,
                WarehouseOperationKind.RESUME,
                operation_revision=binding.revision - 1,
            )
            return binding
        self._assert_revision(binding, expected_revision)
        if binding.lifecycle_state is not WarehouseBindingState.SUSPENDED:
            raise ValueError("warehouse resume requires suspended binding state")
        self._assert_provider_engine(binding, operation="resume")
        operation, adopted = self._claim_operation(
            binding,
            operation_kind=WarehouseOperationKind.RESUME,
            operation_revision=binding.revision,
        )
        if adopted:
            _, operation = self._reconcile(binding, operation)
            resumed_phase = (
                WarehouseOperationPhase.RESUMED
                if operation.phase is WarehouseOperationPhase.CLAIMED
                else operation.phase
            )
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
                phase=resumed_phase,
            )
        if operation.phase not in {
            WarehouseOperationPhase.RESUMED,
            WarehouseOperationPhase.VALIDATING,
            WarehouseOperationPhase.VALIDATED,
        }:
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
            )
            operation = self._void_effect("resume", binding, operation, self._provider.resume)
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESUME_EFFECT)
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
                phase=WarehouseOperationPhase.RESUMED,
            )
        if operation.phase is not WarehouseOperationPhase.VALIDATED:
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
                phase=WarehouseOperationPhase.VALIDATING,
            )
        validation, operation = self._validate(binding, operation, resume=True)
        if not isinstance(validation, ResumeWarehouseValidationResult):
            self._fail_invalid_result(binding, operation, operation_name="validate")
        self._fault_hook(WarehouseLifecycleCheckpoint.BEFORE_RESUME_ADMISSION)
        try:
            ready = self._control.record_resume_validation(
                tenant_id,
                binding_id,
                validation.evidence,
                expected_revision=binding.revision,
                operation=operation,
            )
        except WarehouseAdmissionError:
            self._fail_invalid_result(binding, operation, operation_name="validate")
        return ready

    def retire(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self._control.get(tenant_id, binding_id)
        if binding.lifecycle_state is WarehouseBindingState.RETIRED:
            if binding.revision not in {expected_revision + 1, expected_revision + 2}:
                raise ValueError("binding revision is stale")
            self._close_completed_replay(
                binding,
                WarehouseOperationKind.RETIRE,
                operation_revision=binding.revision - 1,
            )
            return binding
        if binding.lifecycle_state in {
            WarehouseBindingState.READY,
            WarehouseBindingState.SUSPENDED,
        }:
            self._assert_revision(binding, expected_revision)
            binding = self._control.transition(
                tenant_id,
                binding_id,
                WarehouseBindingState.RETIRING,
                expected_revision=binding.revision,
            )
        elif binding.lifecycle_state is WarehouseBindingState.RETIRING:
            if binding.revision != expected_revision + 1:
                raise ValueError("binding revision is stale")
        elif binding.lifecycle_state is WarehouseBindingState.FAILED:
            self._assert_revision(binding, expected_revision)
        else:
            raise ValueError("warehouse retirement requires ready, suspended, or failed binding")
        self._assert_provider_engine(binding, operation="retire")
        operation, adopted = self._claim_operation(
            binding,
            operation_kind=WarehouseOperationKind.RETIRE,
            operation_revision=binding.revision,
        )
        if adopted:
            _, operation = self._reconcile(binding, operation)
        operation = self._save_operation_update(operation, status=WarehouseOperationStatus.RUNNING)
        evidence, operation = self._retire(binding, operation)
        self._fault_hook(WarehouseLifecycleCheckpoint.BEFORE_RETIREMENT_ADMISSION)
        try:
            retired = self._control.record_retirement(
                tenant_id,
                binding_id,
                evidence,
                expected_revision=binding.revision,
                operation=operation,
            )
        except WarehouseAdmissionError:
            self._fail_invalid_result(binding, operation, operation_name="retire")
        return retired

    def delete_retained_resources(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
        authorized: bool,
    ) -> WarehouseRetirementEvidence:
        if not authorized:
            raise PermissionError("retained warehouse deletion requires explicit authorization")
        binding = self._control.get(tenant_id, binding_id)
        self._assert_revision(binding, expected_revision)
        if binding.lifecycle_state is not WarehouseBindingState.RETIRED:
            raise ValueError("retained warehouse deletion requires retired binding state")
        self._assert_provider_engine(binding, operation="retire")
        resources = self._repository.load_resources(tenant_id, binding_id)
        retained_resources = tuple(
            resource
            for resource in resources
            if resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
        )
        if any(self._clock() < resource.retention_deadline for resource in retained_resources):
            raise ValueError("retained warehouse deletion requires elapsed retention deadline")
        if not retained_resources:
            try:
                recorded = self._repository.load_retirement_evidence(
                    tenant_id,
                    binding_id,
                    binding.revision,
                )
            except KeyError:
                pass
            else:
                self._close_completed_replay(
                    binding,
                    WarehouseOperationKind.RETIRE,
                    operation_revision=binding.revision,
                )
                return recorded
        operation, adopted = self._claim_operation(
            binding,
            operation_kind=WarehouseOperationKind.RETIRE,
            operation_revision=binding.revision,
        )
        if adopted:
            _, operation = self._reconcile(binding, operation)
        operation = self._save_operation_update(operation, status=WarehouseOperationStatus.RUNNING)
        evidence, operation = self._retire(binding, operation)
        snapshot = canonical_retirement_resource_snapshot(
            self._repository.load_resources(tenant_id, binding_id)
        )
        if (
            evidence.tenant_id != binding.tenant_id
            or evidence.binding_id != binding.binding_id
            or evidence.binding_revision != operation.binding_revision
            or evidence.observed_at < binding.updated_at
            or snapshot.pending_resource_count
            or snapshot.retained_resource_count
            or snapshot.cleanup_failed_resource_count
            or retirement_evidence_mismatch(evidence, snapshot) is not None
        ):
            self._fail_invalid_result(binding, operation, operation_name="retire")
        self._repository.record_retained_resource_deletion(
            evidence,
            expected_revision=binding.revision,
        )
        self._finish_operation(operation, phase=WarehouseOperationPhase.RETIRED)
        return evidence

    def _claim_operation(
        self,
        binding: WarehouseBinding,
        *,
        operation_kind: WarehouseOperationKind,
        operation_revision: int,
    ) -> tuple[PrivateWarehouseOperation, bool]:
        existing = self._repository.load_live_operation(binding.tenant_id, binding.binding_id)
        if existing is not None:
            if self._matches_operation(
                existing,
                binding,
                operation_kind=operation_kind,
                operation_revision=operation_revision,
            ):
                return existing, True
            self._reconcile_stale(binding, existing)
        if binding.revision != operation_revision:
            raise WarehouseOperationConflictError(
                "warehouse operation required for the current lifecycle phase was not recorded"
            )
        operation_id = (
            "wop-"
            + digest(
                {
                    "domain": "heinzel-warehouse-operation-v1",
                    "tenant_id": binding.tenant_id,
                    "sequence": self._repository.next_operation_sequence(binding.tenant_id),
                }
            )[:24]
        )
        now = self._clock()
        operation = PrivateWarehouseOperation(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation_revision,
            operation_id=operation_id,
            operation_kind=operation_kind,
            engine_kind=binding.engine_kind,
            status=WarehouseOperationStatus.CLAIMED,
            phase=WarehouseOperationPhase.CLAIMED,
            started_at=now,
            updated_at=now,
        )
        self._repository.claim_operation(operation)
        self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_OPERATION_CLAIM)
        return operation, False

    def _reconcile_stale(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> None:
        _, reconciled = self._reconcile(binding, operation)
        self._finish_operation(
            reconciled,
            phase=_TERMINAL_OPERATION_PHASES[reconciled.operation_kind],
        )

    def _reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> tuple[WarehouseProvisionResult, PrivateWarehouseOperation]:
        reconciling = self._save_operation_update(
            operation,
            status=WarehouseOperationStatus.RECONCILING,
        )
        try:
            result = self._invoke_provider(
                "reconcile",
                lambda: self._provider.reconcile(binding, reconciling),
            )
            self._assert_provision_result(result, reconciling, operation_name="reconcile")
            return result, reconciling
        except WarehouseProviderError as error:
            self._raise_operation_failure(binding, reconciling, error)

    def _provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> tuple[WarehouseProvisionResult, PrivateWarehouseOperation]:
        try:
            result = self._invoke_provider(
                "provision",
                lambda: self._provider.provision(binding, operation),
            )
        except WarehouseProviderError as error:
            if error.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME:
                return self._reconcile(binding, operation)
            self._raise_operation_failure(binding, operation, error)
        try:
            self._assert_provision_result(result, operation, operation_name="provision")
        except WarehouseProviderError as error:
            self._raise_operation_failure(binding, operation, error)
        return result, operation

    def _validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> tuple[
        InitialWarehouseValidationResult | ResumeWarehouseValidationResult,
        PrivateWarehouseOperation,
    ]:
        try:
            return (
                self._invoke_provider(
                    "validate",
                    lambda: self._provider.validate(binding, operation, resume=resume),
                ),
                operation,
            )
        except WarehouseProviderError as error:
            if error.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME:
                _, reconciled = self._reconcile(binding, operation)
                try:
                    return (
                        self._invoke_provider(
                            "validate",
                            lambda: self._provider.validate(binding, reconciled, resume=resume),
                        ),
                        reconciled,
                    )
                except WarehouseProviderError as retry_error:
                    self._raise_operation_failure(binding, reconciled, retry_error)
            self._raise_operation_failure(binding, operation, error)

    def _retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> tuple[WarehouseRetirementEvidence, PrivateWarehouseOperation]:
        try:
            result = self._invoke_provider(
                "retire",
                lambda: self._provider.retire(binding, operation),
            )
        except WarehouseProviderError as error:
            if error.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME:
                _, operation = self._reconcile(binding, operation)
                try:
                    result = self._invoke_provider(
                        "retire",
                        lambda: self._provider.retire(binding, operation),
                    )
                except WarehouseProviderError as retry_error:
                    self._raise_operation_failure(binding, operation, retry_error)
            else:
                self._raise_operation_failure(binding, operation, error)
        if not isinstance(result, WarehouseRetirementEvidence):
            self._fail_invalid_result(binding, operation, operation_name="retire")
        return result, operation

    def _void_effect(
        self,
        operation_name: WarehouseProviderOperation,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        effect: Callable[[WarehouseBinding, PrivateWarehouseOperation], None],
    ) -> PrivateWarehouseOperation:
        try:
            result = self._invoke_provider(operation_name, lambda: effect(binding, operation))
        except WarehouseProviderError as error:
            if error.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME:
                _, reconciled = self._reconcile(binding, operation)
                return reconciled
            else:
                self._raise_operation_failure(binding, operation, error)
        if result is not None:
            self._fail_invalid_result(binding, operation, operation_name=operation_name)
        return operation

    def _invoke_provider(
        self,
        operation_name: WarehouseProviderOperation,
        call: Callable[[], _Result],
    ) -> _Result:
        boundary_error: WarehouseProviderError
        try:
            return call()
        except WarehouseProviderError as error:
            classification = (
                error.classification
                if isinstance(error.operation, str)
                and error.operation == operation_name
                and isinstance(error.classification, WarehouseFailureClassification)
                else WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
            )
            boundary_error = WarehouseProviderError(
                operation=operation_name,
                classification=classification,
            )
        except Exception:
            boundary_error = WarehouseProviderError(
                operation=operation_name,
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        raise boundary_error from None

    def _raise_operation_failure(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        error: WarehouseProviderError,
    ) -> Never:
        if error.classification in _RESUMABLE_FAILURES:
            status = (
                WarehouseOperationStatus.RECONCILING
                if error.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME
                else WarehouseOperationStatus.RUNNING
            )
            self._save_operation_update(
                operation,
                status=status,
                failure_classification=None,
            )
            raise error
        if binding.lifecycle_state in _TERMINAL_FAILURE_STATES:
            self._control.record_terminal_operation_failure(
                operation,
                error.classification,
                expected_revision=binding.revision,
            )
        else:
            self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.FAILED,
                failure_classification=error.classification,
            )
        raise error

    def _record_provision_result(
        self,
        operation: PrivateWarehouseOperation,
        result: WarehouseProvisionResult,
    ) -> PrivateWarehouseOperation:
        phase = operation.phase
        if phase in {
            WarehouseOperationPhase.CLAIMED,
            WarehouseOperationPhase.RESOURCES_PLANNED,
        }:
            phase = WarehouseOperationPhase.PROVIDER_CREATED
        return self._save_operation_update(
            operation,
            status=WarehouseOperationStatus.RUNNING,
            phase=phase,
            provider_resource_handle=result.private_resource_handle,
        )

    def _finish_operation(
        self,
        operation: PrivateWarehouseOperation,
        *,
        phase: WarehouseOperationPhase,
    ) -> None:
        if operation.status in {
            WarehouseOperationStatus.CLAIMED,
            WarehouseOperationStatus.RECONCILING,
        }:
            operation = self._save_operation_update(
                operation,
                status=WarehouseOperationStatus.RUNNING,
            )
        phase_routes = {
            WarehouseOperationKind.PROVISION: {
                WarehouseOperationPhase.CLAIMED: (
                    WarehouseOperationPhase.PROVIDER_CREATED,
                    WarehouseOperationPhase.VALIDATING,
                    WarehouseOperationPhase.VALIDATED,
                ),
                WarehouseOperationPhase.RESOURCES_PLANNED: (
                    WarehouseOperationPhase.PROVIDER_CREATED,
                    WarehouseOperationPhase.VALIDATING,
                    WarehouseOperationPhase.VALIDATED,
                ),
                WarehouseOperationPhase.PROVIDER_CREATED: (
                    WarehouseOperationPhase.VALIDATING,
                    WarehouseOperationPhase.VALIDATED,
                ),
                WarehouseOperationPhase.VALIDATING: (WarehouseOperationPhase.VALIDATED,),
                WarehouseOperationPhase.VALIDATED: (),
            },
            WarehouseOperationKind.SUSPEND: {
                WarehouseOperationPhase.CLAIMED: (WarehouseOperationPhase.SUSPENDED,),
                WarehouseOperationPhase.SUSPENDED: (),
            },
            WarehouseOperationKind.RESUME: {
                WarehouseOperationPhase.CLAIMED: (
                    WarehouseOperationPhase.RESUMED,
                    WarehouseOperationPhase.VALIDATING,
                    WarehouseOperationPhase.VALIDATED,
                ),
                WarehouseOperationPhase.RESUMED: (
                    WarehouseOperationPhase.VALIDATING,
                    WarehouseOperationPhase.VALIDATED,
                ),
                WarehouseOperationPhase.VALIDATING: (WarehouseOperationPhase.VALIDATED,),
                WarehouseOperationPhase.VALIDATED: (),
            },
            WarehouseOperationKind.RETIRE: {
                WarehouseOperationPhase.CLAIMED: (WarehouseOperationPhase.RETIRED,),
                WarehouseOperationPhase.RETIREMENT_DISPOSITION_RECORDED: (
                    WarehouseOperationPhase.RETIRED,
                ),
                WarehouseOperationPhase.RETIRED: (),
            },
        }
        if operation.phase is not phase:
            remaining_phases = phase_routes[operation.operation_kind][operation.phase]
            if phase not in remaining_phases:
                raise WarehouseOperationConflictError(
                    "warehouse operation cannot complete through the requested phase"
                )
            for next_phase in remaining_phases[: remaining_phases.index(phase) + 1]:
                operation = self._save_operation_update(
                    operation,
                    status=operation.status,
                    phase=next_phase,
                )
        self._save_operation_update(
            operation,
            status=WarehouseOperationStatus.SUCCEEDED,
            phase=phase,
            failure_classification=None,
        )

    def _close_completed_replay(
        self,
        binding: WarehouseBinding,
        operation_kind: WarehouseOperationKind,
        *,
        operation_revision: int,
    ) -> None:
        live = self._repository.load_live_operation(binding.tenant_id, binding.binding_id)
        if live is None:
            return
        if (
            live.operation_kind is operation_kind
            and live.engine_kind is binding.engine_kind
            and live.binding_revision == operation_revision
        ):
            self._finish_operation(live, phase=_TERMINAL_OPERATION_PHASES[operation_kind])
            return
        raise WarehouseOperationConflictError(
            "warehouse live operation does not match the stable binding transition"
        )

    def _assert_provider_engine(
        self,
        binding: WarehouseBinding,
        *,
        operation: WarehouseProviderOperation,
    ) -> None:
        if self._provider.engine_kind is binding.engine_kind:
            return
        error = WarehouseProviderError(
            operation=operation,
            classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        )
        if binding.lifecycle_state in _TERMINAL_FAILURE_STATES:
            self._control.record_terminal_failure(
                binding.tenant_id,
                binding.binding_id,
                error.classification,
                expected_revision=binding.revision,
            )
        raise error

    def _fail_invalid_result(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        operation_name: WarehouseProviderOperation,
    ) -> Never:
        self._raise_operation_failure(
            binding,
            operation,
            WarehouseProviderError(
                operation=operation_name,
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            ),
        )

    @staticmethod
    def _assert_provision_result(
        result: WarehouseProvisionResult,
        operation: PrivateWarehouseOperation,
        *,
        operation_name: WarehouseProviderOperation,
    ) -> None:
        validated: WarehouseProvisionResult | None = None
        if isinstance(result, WarehouseProvisionResult):
            with suppress(Exception):
                validated = WarehouseProvisionResult.model_validate(result)
        if (
            validated is None
            or result.tenant_id != operation.tenant_id
            or result.binding_id != operation.binding_id
            or result.binding_revision != operation.binding_revision
            or result.operation_id != operation.operation_id
            or result.engine_kind is not operation.engine_kind
        ):
            raise WarehouseProviderError(
                operation=operation_name,
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )

    @staticmethod
    def _matches_operation(
        operation: PrivateWarehouseOperation,
        binding: WarehouseBinding,
        *,
        operation_kind: WarehouseOperationKind,
        operation_revision: int,
    ) -> bool:
        return (
            operation.tenant_id == binding.tenant_id
            and operation.binding_id == binding.binding_id
            and operation.binding_revision == operation_revision
            and operation.operation_kind is operation_kind
            and operation.engine_kind is binding.engine_kind
        )

    def _update_operation(
        self,
        operation: PrivateWarehouseOperation,
        *,
        status: WarehouseOperationStatus,
        phase: WarehouseOperationPhase | None = None,
        provider_resource_handle: str | None = None,
        failure_classification: WarehouseFailureClassification | None = None,
    ) -> PrivateWarehouseOperation:
        return PrivateWarehouseOperation.model_validate(
            {
                **operation.model_dump(),
                "status": status,
                "phase": operation.phase if phase is None else phase,
                "provider_resource_handle": (
                    operation.provider_resource_handle
                    if provider_resource_handle is None
                    else provider_resource_handle
                ),
                "failure_classification": failure_classification,
                "updated_at": self._clock(),
            }
        )

    def _save_operation_update(
        self,
        operation: PrivateWarehouseOperation,
        *,
        status: WarehouseOperationStatus,
        phase: WarehouseOperationPhase | None = None,
        provider_resource_handle: str | None = None,
        failure_classification: WarehouseFailureClassification | None = None,
    ) -> PrivateWarehouseOperation:
        updated = self._update_operation(
            operation,
            status=status,
            phase=phase,
            provider_resource_handle=provider_resource_handle,
            failure_classification=failure_classification,
        )
        self._repository.save_operation(operation, updated)
        return updated

    @staticmethod
    def _assert_revision(binding: WarehouseBinding, expected_revision: int) -> None:
        if binding.revision != expected_revision:
            raise ValueError("binding revision is stale")

    @staticmethod
    def _assert_provision_replay(binding: WarehouseBinding, expected_revision: int) -> None:
        expected_offset = {
            WarehouseBindingState.PROVISIONING: 1,
            WarehouseBindingState.VALIDATING: 2,
            WarehouseBindingState.READY: 3,
        }.get(binding.lifecycle_state)
        if expected_offset is None:
            raise ValueError("warehouse provisioning requires draft or recoverable binding state")
        if binding.revision != expected_revision + expected_offset:
            raise ValueError("binding revision is stale")


__all__ = ["WarehouseLifecycleOrchestrator"]
