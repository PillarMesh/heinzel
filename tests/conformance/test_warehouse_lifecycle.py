from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import digest
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
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
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
    WarehouseValidationResult,
)

from tests.conformance.warehouse_lifecycle import (
    WarehouseLifecycleConformanceError,
    assert_warehouse_lifecycle_contract,
)

_NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
_PRIVATE_MARKER = "private-tenant-optimized-canary"
_VERIFIER_CHECK_NAMES = (
    "provider_restart_replay",
    "restore_resource_absence",
    "initial_backup_artifact_presence",
    "retained_backup_artifact_presence",
    "retained_backup_key_presence",
    "deleted_backup_artifact_absence",
    "deleted_backup_key_absence",
    "final_resource_absence",
)


class FatalVerifierSignal(BaseException):
    pass


def _binding(state: WarehouseBindingState, revision: int) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="wb-optimized-contract",
        tenant_id=_PRIVATE_MARKER,
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capacity_profile="mvp-fixed",
        capability_profile_digest="1" * 64,
        lifecycle_state=state,
        revision=revision,
        created_at=_NOW,
        updated_at=_NOW + timedelta(seconds=revision),
        provisioned_at=_NOW if state is not WarehouseBindingState.DRAFT else None,
    )


def _initial_validation() -> InitialWarehouseValidationResult:
    restore = WarehouseRestoreVerification(
        verification_id="wrv-optimized-contract",
        tenant_id=_PRIVATE_MARKER,
        binding_id="wb-optimized-contract",
        binding_revision=3,
        engine_kind=EngineKind.POSTGRESQL,
        source_backup_artifact_digest="2" * 64,
        representative_data_digest="3" * 64,
        schema_metadata_digest="4" * 64,
        principal_profile_digest="5" * 64,
        integrity_marker_digest="6" * 64,
        query_behavior_digest="7" * 64,
        verified_at=_NOW + timedelta(minutes=1),
    )
    evidence = WarehouseValidationEvidence(
        evidence_id="wev-optimized-contract",
        tenant_id=_PRIVATE_MARKER,
        binding_id="wb-optimized-contract",
        binding_revision=3,
        validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        engine_kind=EngineKind.POSTGRESQL,
        engine_version="18.6",
        engine_build_digest="8" * 64,
        engine_image_digest="9" * 64,
        principal_profile_digest=restore.principal_profile_digest,
        namespace_grant_matrix_digest="a" * 64,
        tls_probe_digest="b" * 64,
        network_isolation_probe_digest="c" * 64,
        encryption_at_rest_evidence_digest="d" * 64,
        encryption_at_rest_disposition=(EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
        positive_probe_digest="e" * 64,
        denial_probe_digest="f" * 64,
        ledger_probe_digest="0" * 64,
        monitoring_probe_digest="1" * 64,
        capacity_alert_probe_digest="2" * 64,
        backup_artifact_digest=restore.source_backup_artifact_digest,
        restore_verification_digest=digest(restore),
        restore_cleanup_digest="3" * 64,
        observed_at=_NOW + timedelta(minutes=1),
    )
    return InitialWarehouseValidationResult(evidence=evidence, restore_verification=restore)


def _resume_validation() -> ResumeWarehouseValidationResult:
    return ResumeWarehouseValidationResult(
        evidence=WarehouseResumeValidationEvidence(
            evidence_id="wrev-optimized-contract",
            tenant_id=_PRIVATE_MARKER,
            binding_id="wb-optimized-contract",
            binding_revision=5,
            engine_kind=EngineKind.POSTGRESQL,
            engine_version="18.6",
            engine_build_digest="8" * 64,
            engine_image_digest="9" * 64,
            tls_probe_digest="b" * 64,
            network_isolation_probe_digest="c" * 64,
            monitoring_probe_digest="1" * 64,
            positive_probe_digest="e" * 64,
            denial_probe_digest="f" * 64,
            storage_integrity_probe_digest="4" * 64,
            observed_at=_NOW + timedelta(minutes=2),
        )
    )


def _retirement_evidence(*, retained: int) -> WarehouseRetirementEvidence:
    return WarehouseRetirementEvidence(
        evidence_id=f"wret-optimized-contract-{retained}",
        tenant_id=_PRIVATE_MARKER,
        binding_id="wb-optimized-contract",
        binding_revision=7,
        resource_inventory_digest="5" * 64,
        cleanup_disposition_digest="6" * 64,
        retention_policy_digest="7" * 64,
        completed_resource_count=0 if retained else 18,
        retained_resource_count=retained,
        cleanup_failed_resource_count=0,
        observed_at=_NOW + timedelta(minutes=3),
    )


def _resource(
    resource_kind: WarehouseResourceKind,
    *,
    cleanup_status: WarehouseResourceCleanupStatus,
    resource_id_suffix: str = "",
    retention_deadline: datetime | None = None,
) -> PrivateWarehouseResource:
    return PrivateWarehouseResource(
        tenant_id=_PRIVATE_MARKER,
        binding_id="wb-optimized-contract",
        binding_revision=2,
        operation_id="wop-optimized-contract",
        resource_id=f"resource-{resource_kind.value}{resource_id_suffix}",
        resource_kind=resource_kind,
        provider_resource_handle=f"handle-{resource_kind.value}",
        creation_state=WarehouseResourceCreationState.CREATED,
        retention_deadline=retention_deadline or _NOW + timedelta(hours=1),
        cleanup_status=cleanup_status,
        created_at=_NOW,
        updated_at=_NOW,
    )


_RESOURCE_KINDS = tuple(WarehouseResourceKind)


class OptimizedContractDriver:
    def __init__(
        self,
        *,
        invalid_ready: bool = False,
        failing_verifier: str | None = None,
        verifier_exception: BaseException | None = None,
        duplicate_backup_key: bool = False,
        retirement_operation_kind: WarehouseOperationKind = WarehouseOperationKind.RETIRE,
    ) -> None:
        self.binding = _binding(WarehouseBindingState.DRAFT, 1)
        self._ready = _binding(
            WarehouseBindingState.FAILED if invalid_ready else WarehouseBindingState.READY,
            4,
        )
        self._initial_validation = _initial_validation()
        self._resume_validation = _resume_validation()
        self._retained = _retirement_evidence(retained=2)
        self._deleted = _retirement_evidence(retained=0)
        self._resumed = False
        self._deleted_resources = False
        self.calls = {"provision": 0, "resources": 0, "validation_results": 0}
        self.verifier_calls: dict[str, int] = {}
        self._failing_verifier = failing_verifier
        self._verifier_exception = verifier_exception
        self._duplicate_backup_key = duplicate_backup_key
        self._retirement_operation_kind = retirement_operation_kind
        self._backup_artifact_checks = iter(
            (
                "initial_backup_artifact_presence",
                "retained_backup_artifact_presence",
                "deleted_backup_artifact_absence",
            )
        )
        self._backup_key_checks = iter(
            ("retained_backup_key_presence", "deleted_backup_key_absence")
        )
        self._resource_absence_checks = iter(("restore_resource_absence", "final_resource_absence"))

    def _record_verifier(self, check_name: str) -> bool:
        self.verifier_calls[check_name] = self.verifier_calls.get(check_name, 0) + 1
        if check_name != self._failing_verifier:
            return True
        if self._verifier_exception is not None:
            raise self._verifier_exception
        return False

    def provision(self, *, expected_revision: int) -> WarehouseBinding:
        if expected_revision != self.binding.revision:
            raise RuntimeError("fixture received an invalid provisioning revision")
        self.calls["provision"] += 1
        return self._ready

    def validation_results(self) -> tuple[WarehouseValidationResult, ...]:
        self.calls["validation_results"] += 1
        if self._resumed:
            return (self._initial_validation, self._resume_validation)
        return (self._initial_validation,)

    def resources(self) -> tuple[PrivateWarehouseResource, ...]:
        self.calls["resources"] += 1
        cleanup_status = (
            WarehouseResourceCleanupStatus.COMPLETE
            if self._deleted_resources
            else WarehouseResourceCleanupStatus.RETAINED
        )
        resources = tuple(
            _resource(
                resource_kind,
                cleanup_status=(
                    WarehouseResourceCleanupStatus.COMPLETE
                    if resource_kind.value.startswith("restore_")
                    else cleanup_status
                ),
            )
            for resource_kind in _RESOURCE_KINDS
        )
        if not self._duplicate_backup_key:
            return resources
        return (
            *resources,
            _resource(
                WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
                cleanup_status=cleanup_status,
                resource_id_suffix="-secondary",
                retention_deadline=_NOW + timedelta(hours=2),
            ),
        )

    def suspend(self, *, expected_revision: int) -> WarehouseBinding:
        if expected_revision != self._ready.revision:
            raise RuntimeError("fixture received an invalid suspension revision")
        return _binding(WarehouseBindingState.SUSPENDED, 5)

    def resume(self, *, expected_revision: int) -> WarehouseBinding:
        if expected_revision != 5:
            raise RuntimeError("fixture received an invalid resume revision")
        self._resumed = True
        return _binding(WarehouseBindingState.READY, 6)

    def begin_retirement(
        self, *, expected_revision: int
    ) -> tuple[WarehouseBinding, PrivateWarehouseOperation]:
        if expected_revision != 6:
            raise RuntimeError("fixture received an invalid retirement revision")
        binding = _binding(WarehouseBindingState.RETIRING, 7)
        return binding, PrivateWarehouseOperation(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation_id="wop-retire-optimized-contract",
            operation_kind=self._retirement_operation_kind,
            engine_kind=binding.engine_kind,
            status=WarehouseOperationStatus.RUNNING,
            phase=WarehouseOperationPhase.CLAIMED,
            started_at=_NOW,
            updated_at=_NOW,
        )

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        if binding.revision != operation.binding_revision:
            raise RuntimeError("fixture retirement operation is stale")
        return self._retained

    def record_retired(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseBinding:
        if (
            binding.revision != evidence.binding_revision
            or binding.revision != operation.binding_revision
        ):
            raise RuntimeError("fixture retirement evidence is stale")
        return _binding(WarehouseBindingState.RETIRED, 8)

    def delete_retained_resources(
        self,
        binding: WarehouseBinding,
    ) -> WarehouseRetirementEvidence:
        if binding.lifecycle_state is not WarehouseBindingState.RETIRED:
            raise RuntimeError("fixture cleanup requires a retired binding")
        self._deleted_resources = True
        return self._deleted

    def advance_for_resume(self) -> None:
        return None

    def advance_past_retention(self) -> None:
        return None

    def verify_backup_artifact(
        self,
        resource: PrivateWarehouseResource,
        *,
        should_exist: bool,
    ) -> bool:
        del resource, should_exist
        return self._record_verifier(next(self._backup_artifact_checks))

    def verify_backup_key(self, *, should_exist: bool) -> bool:
        del should_exist
        return self._record_verifier(next(self._backup_key_checks))

    def verify_absent_resources(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> bool:
        if not resources:
            return False
        return self._record_verifier(next(self._resource_absence_checks))

    def verify_provider_restart_replay(self) -> bool:
        return self._record_verifier("provider_restart_replay")


def _run_optimized_scenario(
    *,
    invalid_ready: bool = False,
    failing_verifier: str | None = None,
    callback_exception_kind: str | None = None,
) -> subprocess.CompletedProcess[str]:
    script = f"""
import json
import sys
from tests.conformance.test_warehouse_lifecycle import OptimizedContractDriver
from tests.conformance.warehouse_lifecycle import (
    WarehouseLifecycleConformanceError,
    assert_warehouse_lifecycle_contract,
)

driver = OptimizedContractDriver(
    invalid_ready={invalid_ready!r},
    failing_verifier={failing_verifier!r},
    verifier_exception=(
        PermissionError('/private/{_PRIVATE_MARKER}/warehouse-secret')
        if {callback_exception_kind!r} == 'permission'
        else WarehouseLifecycleConformanceError('{_PRIVATE_MARKER}')
        if {callback_exception_kind!r} == 'conformance'
        else None
    ),
)
try:
    assert_warehouse_lifecycle_contract(lambda: driver)
except Exception as error:
    if error.__cause__ is not None or error.__context__ is not None:
        print('unsafe exception chain', file=sys.stderr)
        raise SystemExit(8)
    print(f"{{type(error).__name__}}:{{error}}", file=sys.stderr)
    raise SystemExit(7)
print(json.dumps({{"calls": driver.calls, "verifiers": driver.verifier_calls}}, sort_keys=True))
"""
    return subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )


def test_optimized_python_executes_conformance_authority_and_fails_closed() -> None:
    passing = _run_optimized_scenario()
    failing = _run_optimized_scenario(invalid_ready=True)

    assert passing.returncode == 0, passing.stderr
    assert json.loads(passing.stdout) == {
        "calls": {
            "provision": 3,
            "resources": 5,
            "validation_results": 4,
        },
        "verifiers": {check_name: 1 for check_name in _VERIFIER_CHECK_NAMES},
    }
    assert failing.returncode == 7
    assert failing.stderr.startswith("WarehouseLifecycleConformanceError:")
    assert _PRIVATE_MARKER not in failing.stderr


@pytest.mark.parametrize("callback_exception_kind", ("permission", "conformance"))
@pytest.mark.parametrize("check_name", _VERIFIER_CHECK_NAMES)
def test_optimized_python_executes_and_sanitizes_every_verifier_callback(
    check_name: str,
    callback_exception_kind: str,
) -> None:
    failed_result = _run_optimized_scenario(
        failing_verifier=check_name,
        callback_exception_kind=callback_exception_kind,
    )

    assert failed_result.returncode == 7
    assert failed_result.stderr == (
        f"WarehouseLifecycleConformanceError:warehouse lifecycle conformance failed: {check_name}\n"
    )
    assert _PRIVATE_MARKER not in failed_result.stderr


@pytest.mark.parametrize(
    "fatal_signal",
    (KeyboardInterrupt(), SystemExit(19), GeneratorExit(), FatalVerifierSignal()),
)
def test_conformance_verifier_boundary_does_not_catch_fatal_signals(
    fatal_signal: BaseException,
) -> None:
    driver = OptimizedContractDriver(
        failing_verifier="provider_restart_replay",
        verifier_exception=fatal_signal,
    )

    with pytest.raises(type(fatal_signal)):
        assert_warehouse_lifecycle_contract(lambda: driver)


def test_conformance_verifier_boundary_normalizes_callback_conformance_errors() -> None:
    callback_error = WarehouseLifecycleConformanceError(_PRIVATE_MARKER)
    driver = OptimizedContractDriver(
        failing_verifier="provider_restart_replay",
        verifier_exception=callback_error,
    )

    with pytest.raises(WarehouseLifecycleConformanceError) as failure:
        assert_warehouse_lifecycle_contract(lambda: driver)

    assert failure.value is not callback_error
    assert str(failure.value) == ("warehouse lifecycle conformance failed: provider_restart_replay")
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert _PRIVATE_MARKER not in str(failure.value)


def test_conformance_verifier_boundary_suppresses_private_exception_context() -> None:
    private_path = f"/private/{_PRIVATE_MARKER}/warehouse-secret"
    driver = OptimizedContractDriver(
        failing_verifier="provider_restart_replay",
        verifier_exception=PermissionError(private_path),
    )

    with pytest.raises(WarehouseLifecycleConformanceError) as failure:
        assert_warehouse_lifecycle_contract(lambda: driver)

    assert str(failure.value) == ("warehouse lifecycle conformance failed: provider_restart_replay")
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert private_path not in str(failure.value)


def test_conformance_rejects_a_retirement_operation_with_the_wrong_kind() -> None:
    driver = OptimizedContractDriver(
        retirement_operation_kind=WarehouseOperationKind.PROVISION,
    )

    with pytest.raises(WarehouseLifecycleConformanceError, match="retirement_operation_binding"):
        assert_warehouse_lifecycle_contract(lambda: driver)


def test_conformance_accepts_multiple_backup_keys_and_selects_the_first() -> None:
    driver = OptimizedContractDriver(duplicate_backup_key=True)

    observation = assert_warehouse_lifecycle_contract(lambda: driver)

    backup_keys = tuple(
        resource
        for resource in observation.initial_resources
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY
    )
    assert len(backup_keys) == 2
    assert backup_keys[0].retention_deadline == observation.backup_resource.retention_deadline
    assert backup_keys[1].retention_deadline != observation.backup_resource.retention_deadline
