from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pillarmesh_contract_model import digest

from .errors import WarehouseAdmissionError, WarehouseValidationConflictError
from .evidence import (
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
)
from .models import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
)
from .private_state import (
    PrivateWarehouseOperation,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
)
from .readiness import ProductionWarehouseReadinessPolicy, WarehouseReadinessPolicy
from .repository import StaleRevisionError, WarehouseRepository
from .retirement import canonical_retirement_resource_snapshot, retirement_evidence_mismatch

_TRANSITIONS: dict[WarehouseBindingState, frozenset[WarehouseBindingState]] = {
    WarehouseBindingState.DRAFT: frozenset(
        {WarehouseBindingState.PROVISIONING, WarehouseBindingState.RETIRED}
    ),
    WarehouseBindingState.PROVISIONING: frozenset(
        {WarehouseBindingState.VALIDATING, WarehouseBindingState.FAILED}
    ),
    WarehouseBindingState.VALIDATING: frozenset(
        {WarehouseBindingState.READY, WarehouseBindingState.FAILED}
    ),
    WarehouseBindingState.READY: frozenset(
        {WarehouseBindingState.SUSPENDED, WarehouseBindingState.RETIRING}
    ),
    WarehouseBindingState.FAILED: frozenset({WarehouseBindingState.RETIRED}),
    WarehouseBindingState.SUSPENDED: frozenset(
        {WarehouseBindingState.READY, WarehouseBindingState.RETIRING}
    ),
    WarehouseBindingState.RETIRING: frozenset({WarehouseBindingState.RETIRED}),
    WarehouseBindingState.RETIRED: frozenset(),
}
_PLAIN_TRANSITIONS: dict[WarehouseBindingState, frozenset[WarehouseBindingState]] = {
    WarehouseBindingState.DRAFT: frozenset({WarehouseBindingState.PROVISIONING}),
    WarehouseBindingState.PROVISIONING: frozenset({WarehouseBindingState.VALIDATING}),
    WarehouseBindingState.VALIDATING: frozenset(),
    WarehouseBindingState.READY: frozenset({WarehouseBindingState.RETIRING}),
    WarehouseBindingState.FAILED: frozenset(),
    WarehouseBindingState.SUSPENDED: frozenset({WarehouseBindingState.RETIRING}),
    WarehouseBindingState.RETIRING: frozenset(),
    WarehouseBindingState.RETIRED: frozenset(),
}
_TERMINAL_FAILURE_SOURCE_STATES = frozenset(
    {
        WarehouseBindingState.PROVISIONING,
        WarehouseBindingState.VALIDATING,
    }
)
_TERMINAL_FAILURE_CLASSIFICATIONS = frozenset(
    {
        WarehouseFailureClassification.AUTHORIZATION_DENIED,
        WarehouseFailureClassification.STATEMENT_REJECTED,
        WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        WarehouseFailureClassification.INTEGRITY_FAILURE,
        WarehouseFailureClassification.PERMANENT_CONFIGURATION,
    }
)


class WarehouseControlService:
    def __init__(
        self,
        repository: WarehouseRepository,
        *,
        clock: Callable[[], datetime],
        readiness_policy: WarehouseReadinessPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._readiness_policy = (
            ProductionWarehouseReadinessPolicy() if readiness_policy is None else readiness_policy
        )

    def create_draft(
        self,
        *,
        tenant_id: str,
        engine_kind: EngineKind,
        region: str,
        capacity_profile: Literal["mvp-fixed"],
    ) -> WarehouseBinding:
        sequence = self._repository.next_sequence(tenant_id)
        binding_id = (
            "whb-"
            + digest(
                {
                    "domain": "pillarmesh-warehouse-binding-v1",
                    "tenant_id": tenant_id,
                    "sequence": sequence,
                }
            )[:24]
        )
        now = self._clock()
        binding = WarehouseBinding(
            binding_id=binding_id,
            tenant_id=tenant_id,
            engine_kind=engine_kind,
            region=region,
            capacity_profile=capacity_profile,
            capability_profile_digest=self._capability_profile_digest(
                capacity_profile, engine_kind, "pillarmesh_cloud"
            ),
            lifecycle_state=WarehouseBindingState.DRAFT,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self._repository.save(binding)
        return binding

    def revise_draft(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        engine_kind: EngineKind | None = None,
        region: str | None = None,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state is not WarehouseBindingState.DRAFT:
            raise ValueError("binding is immutable after draft")
        revised_engine_kind = engine_kind if engine_kind is not None else binding.engine_kind
        revised = self._rebuild_binding(
            binding,
            engine_kind=revised_engine_kind,
            region=region if region is not None else binding.region,
            lifecycle_state=binding.lifecycle_state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        self._save_advanced_binding(revised)
        return revised

    def transition(
        self,
        tenant_id: str,
        binding_id: str,
        lifecycle_state: WarehouseBindingState,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if lifecycle_state not in _PLAIN_TRANSITIONS[binding.lifecycle_state]:
            raise ValueError(
                "transition "
                f"{binding.lifecycle_state.value} -> {lifecycle_state.value} is not allowed"
            )
        transitioned = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=lifecycle_state,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        self._save_advanced_binding(transitioned)
        return transitioned

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
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        self._assert_state(binding, WarehouseBindingState.VALIDATING)
        if binding.provisioned_at is not None:
            raise WarehouseAdmissionError(
                "warehouse initial validation cannot replace provisioned_at"
            )
        self._assert_evidence_ownership(
            binding,
            tenant_id=evidence.tenant_id,
            binding_id=evidence.binding_id,
            binding_revision=evidence.binding_revision,
        )
        self._assert_evidence_ownership(
            binding,
            tenant_id=restore.tenant_id,
            binding_id=restore.binding_id,
            binding_revision=restore.binding_revision,
        )
        if evidence.engine_kind is not binding.engine_kind:
            raise WarehouseAdmissionError(
                "warehouse validation evidence engine does not match binding"
            )
        if restore.engine_kind is not binding.engine_kind:
            raise WarehouseAdmissionError(
                "warehouse restore verification engine does not match binding"
            )
        if evidence.backup_artifact_digest != restore.source_backup_artifact_digest:
            raise WarehouseAdmissionError(
                "warehouse validation backup digest does not match restore"
            )
        if evidence.restore_verification_digest != digest(restore):
            raise WarehouseAdmissionError(
                "warehouse validation restore digest does not match verification"
            )
        if evidence.principal_profile_digest != restore.principal_profile_digest:
            raise WarehouseAdmissionError(
                "warehouse validation principal profile digest does not match restore"
            )
        self._assert_fresh(binding, evidence.observed_at)
        self._assert_fresh(binding, restore.verified_at)
        self._readiness_policy.admit(evidence)
        now = self._clock()
        ready = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.READY,
            revision=binding.revision + 1,
            updated_at=now,
            provisioned_at=now,
        )
        succeeded = self._succeeded_operation(
            binding,
            operation,
            operation_kind=WarehouseOperationKind.PROVISION,
            terminal_phase=WarehouseOperationPhase.VALIDATED,
        )
        self._repository.record_initial_validation_operation(
            ready,
            evidence,
            restore,
            operation,
            succeeded,
            expected_revision=expected_revision,
        )
        return ready

    def record_suspension(
        self,
        operation: PrivateWarehouseOperation,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(operation.tenant_id, operation.binding_id)
        self._assert_current_revision(binding, expected_revision)
        self._assert_state(binding, WarehouseBindingState.READY)
        if binding.provisioned_at is None:
            raise WarehouseAdmissionError(
                "warehouse suspension requires an initially provisioned binding"
            )
        suspended = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.SUSPENDED,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        succeeded = self._succeeded_operation(
            binding,
            operation,
            operation_kind=WarehouseOperationKind.SUSPEND,
            terminal_phase=WarehouseOperationPhase.SUSPENDED,
        )
        self._repository.record_suspension(
            suspended,
            operation,
            succeeded,
            expected_revision=expected_revision,
        )
        return suspended

    def record_resume_validation(
        self,
        tenant_id: str,
        binding_id: str,
        evidence: WarehouseResumeValidationEvidence,
        *,
        operation: PrivateWarehouseOperation,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        self._assert_state(binding, WarehouseBindingState.SUSPENDED)
        if binding.provisioned_at is None:
            raise WarehouseAdmissionError(
                "warehouse suspended binding has no initial provisioned_at"
            )
        self._assert_evidence_ownership(
            binding,
            tenant_id=evidence.tenant_id,
            binding_id=evidence.binding_id,
            binding_revision=evidence.binding_revision,
        )
        if evidence.engine_kind is not binding.engine_kind:
            raise WarehouseAdmissionError("warehouse resume evidence engine does not match binding")
        self._assert_fresh(binding, evidence.observed_at)
        ready = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.READY,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        succeeded = self._succeeded_operation(
            binding,
            operation,
            operation_kind=WarehouseOperationKind.RESUME,
            terminal_phase=WarehouseOperationPhase.VALIDATED,
        )
        self._repository.record_resume_validation_operation(
            ready,
            evidence,
            operation,
            succeeded,
            expected_revision=expected_revision,
        )
        return ready

    def record_retirement(
        self,
        tenant_id: str,
        binding_id: str,
        evidence: WarehouseRetirementEvidence,
        *,
        operation: PrivateWarehouseOperation,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state not in {
            WarehouseBindingState.RETIRING,
            WarehouseBindingState.FAILED,
        }:
            raise WarehouseAdmissionError(
                "warehouse retirement requires a retiring or failed binding"
            )
        self._assert_evidence_ownership(
            binding,
            tenant_id=evidence.tenant_id,
            binding_id=evidence.binding_id,
            binding_revision=evidence.binding_revision,
        )
        self._assert_fresh(binding, evidence.observed_at)
        resources = self._repository.load_resources(tenant_id, binding_id)
        snapshot = canonical_retirement_resource_snapshot(resources)
        if snapshot.pending_resource_count:
            raise WarehouseAdmissionError(
                "warehouse retirement requires terminal resource cleanup dispositions"
            )
        mismatch = retirement_evidence_mismatch(evidence, snapshot)
        if mismatch is not None:
            raise WarehouseAdmissionError(
                f"warehouse retirement {mismatch} does not match resource ledger"
            )
        retired = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.RETIRED,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        succeeded = self._succeeded_operation(
            binding,
            operation,
            operation_kind=WarehouseOperationKind.RETIRE,
            terminal_phase=WarehouseOperationPhase.RETIRED,
        )
        self._repository.record_retirement_operation(
            retired,
            evidence,
            operation,
            succeeded,
            expected_revision=expected_revision,
        )
        return retired

    def abandon_draft(
        self,
        tenant_id: str,
        binding_id: str,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        self._assert_state(binding, WarehouseBindingState.DRAFT)
        if binding.provisioned_at is not None:
            raise WarehouseAdmissionError(
                "warehouse provisioned binding cannot be abandoned as a draft"
            )
        retired = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.RETIRED,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=None,
        )
        try:
            self._repository.abandon_draft(
                retired,
                expected_revision=expected_revision,
            )
        except WarehouseValidationConflictError as error:
            raise WarehouseAdmissionError(str(error)) from error
        return retired

    def record_terminal_failure(
        self,
        tenant_id: str,
        binding_id: str,
        classification: WarehouseFailureClassification,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        if classification not in _TERMINAL_FAILURE_CLASSIFICATIONS:
            raise WarehouseAdmissionError(
                "warehouse failure classification cannot support a terminal binding failure"
            )
        binding = self.get(tenant_id, binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state not in _TERMINAL_FAILURE_SOURCE_STATES:
            raise WarehouseAdmissionError("warehouse binding state cannot enter terminal failure")
        failed = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.FAILED,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        self._save_advanced_binding(failed)
        return failed

    def record_terminal_operation_failure(
        self,
        operation: PrivateWarehouseOperation,
        classification: WarehouseFailureClassification,
        *,
        expected_revision: int,
    ) -> WarehouseBinding:
        if classification not in _TERMINAL_FAILURE_CLASSIFICATIONS:
            raise WarehouseAdmissionError(
                "warehouse failure classification cannot support a terminal binding failure"
            )
        binding = self.get(operation.tenant_id, operation.binding_id)
        self._assert_current_revision(binding, expected_revision)
        if binding.lifecycle_state not in _TERMINAL_FAILURE_SOURCE_STATES:
            raise WarehouseAdmissionError("warehouse binding state cannot enter terminal failure")
        failed_binding = self._rebuild_binding(
            binding,
            engine_kind=binding.engine_kind,
            region=binding.region,
            lifecycle_state=WarehouseBindingState.FAILED,
            revision=binding.revision + 1,
            updated_at=self._clock(),
            provisioned_at=binding.provisioned_at,
        )
        failed_operation = PrivateWarehouseOperation.model_validate(
            {
                **operation.model_dump(),
                "status": WarehouseOperationStatus.FAILED,
                "failure_classification": classification,
                "updated_at": self._clock(),
            }
        )
        self._repository.record_terminal_operation_failure(
            operation,
            failed_operation,
            failed_binding,
            expected_revision=expected_revision,
        )
        return failed_binding

    def get(self, tenant_id: str, binding_id: str) -> WarehouseBinding:
        binding = self._repository.load(tenant_id, binding_id)
        if binding is None or binding.tenant_id != tenant_id:
            # A missing binding and another tenant's binding report identically on
            # purpose: distinguishing them would answer "does this identifier exist"
            # for any caller holding a leaked identifier.
            raise KeyError("warehouse binding was not found")
        return binding

    @staticmethod
    def _capability_profile_digest(
        capacity_profile: Literal["mvp-fixed"],
        engine_kind: EngineKind,
        deployment_mode: Literal["pillarmesh_cloud"],
    ) -> str:
        return digest(
            {
                "capacity_profile": capacity_profile,
                "engine_kind": engine_kind,
                "deployment_mode": deployment_mode,
            }
        )

    @staticmethod
    def _assert_current_revision(binding: WarehouseBinding, expected_revision: int) -> None:
        if binding.revision != expected_revision:
            raise ValueError("binding revision is stale")

    @staticmethod
    def _assert_state(binding: WarehouseBinding, expected_state: WarehouseBindingState) -> None:
        if binding.lifecycle_state is not expected_state:
            raise WarehouseAdmissionError(
                f"warehouse admission requires {expected_state.value} binding state"
            )

    @staticmethod
    def _assert_evidence_ownership(
        binding: WarehouseBinding,
        *,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> None:
        if tenant_id != binding.tenant_id:
            raise WarehouseAdmissionError("warehouse evidence tenant does not match binding")
        if binding_id != binding.binding_id:
            raise WarehouseAdmissionError("warehouse evidence binding does not match binding")
        if binding_revision != binding.revision:
            raise WarehouseAdmissionError("warehouse evidence revision does not match binding")

    @staticmethod
    def _assert_fresh(binding: WarehouseBinding, observed_at: datetime) -> None:
        if observed_at < binding.updated_at:
            raise WarehouseAdmissionError(
                "warehouse admission requires fresh evidence for the current binding revision"
            )

    def _save_advanced_binding(self, binding: WarehouseBinding) -> None:
        try:
            self._repository.save(binding)
        except StaleRevisionError as error:
            raise ValueError("binding revision is stale") from error

    def _succeeded_operation(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        operation_kind: WarehouseOperationKind,
        terminal_phase: WarehouseOperationPhase,
    ) -> PrivateWarehouseOperation:
        if (
            operation.tenant_id != binding.tenant_id
            or operation.binding_id != binding.binding_id
            or operation.engine_kind is not binding.engine_kind
            or operation.operation_kind is not operation_kind
        ):
            raise WarehouseAdmissionError(
                "warehouse stable admission operation does not match binding"
            )
        return PrivateWarehouseOperation.model_validate(
            {
                **operation.model_dump(),
                "status": WarehouseOperationStatus.SUCCEEDED,
                "phase": terminal_phase,
                "failure_classification": None,
                "updated_at": self._clock(),
            }
        )

    def _rebuild_binding(
        self,
        binding: WarehouseBinding,
        *,
        engine_kind: EngineKind,
        region: str,
        lifecycle_state: WarehouseBindingState,
        revision: int,
        updated_at: datetime,
        provisioned_at: datetime | None,
    ) -> WarehouseBinding:
        return WarehouseBinding(
            schema_version=binding.schema_version,
            binding_id=binding.binding_id,
            tenant_id=binding.tenant_id,
            engine_kind=engine_kind,
            deployment_mode=binding.deployment_mode,
            region=region,
            capacity_profile=binding.capacity_profile,
            capability_profile_digest=self._capability_profile_digest(
                binding.capacity_profile, engine_kind, binding.deployment_mode
            ),
            lifecycle_state=lifecycle_state,
            revision=revision,
            created_at=binding.created_at,
            updated_at=updated_at,
            provisioned_at=provisioned_at,
        )
