from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel, canonical_bytes, digest
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
    WarehouseLifecycleCheckpoint,
    WarehouseLifecycleOrchestrator,
    WarehouseOperationKind,
    WarehouseOperationStatus,
    WarehouseProviderError,
    WarehouseProvisionResult,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository
from pydantic import ConfigDict, Field, model_validator

type EngineName = Literal["postgresql", "clickhouse"]
type ProviderOperation = Literal[
    "provision", "reconcile", "validate", "suspend", "resume", "retire"
]
type FaultState = Literal["process_stopped", "operation_live", "operation_non_live"]
type ReplayDisposition = Literal[
    "durable_restart_replayed",
    "remained_resumable_after_restart",
    "terminal_failure_remained_non_live",
]
type ScenarioDisposition = Literal["passed", "skipped", "failed"]

_NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
_OBSERVED_AT = _NOW + timedelta(minutes=1)
_PROVIDER_OPERATIONS: tuple[ProviderOperation, ...] = (
    "provision",
    "reconcile",
    "validate",
    "suspend",
    "resume",
    "retire",
)
_CLASSIFICATIONS = (
    WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
    WarehouseFailureClassification.PERMANENT_CONFIGURATION,
)
_CHECKPOINT_OCCURRENCES = {
    checkpoint: 4 if checkpoint is WarehouseLifecycleCheckpoint.AFTER_OPERATION_CLAIM else 1
    for checkpoint in WarehouseLifecycleCheckpoint
}


class WarehouseFaultCleanupProof(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    repository_reopen_count: int = Field(ge=0)
    repository_session_count: int = Field(ge=1)
    provider_session_count: int = Field(ge=1)
    distinct_session_generation_count: int = Field(ge=1)
    session_generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provision_effect_count: int = Field(ge=0)
    unique_resource_count: int = Field(ge=0)
    residual_resource_count: int = Field(ge=0)
    suspend_effect_count: int = Field(ge=0)
    resume_effect_count: int = Field(ge=0)
    retire_effect_count: int = Field(ge=0)
    terminal_operation_count: int = Field(ge=0)
    terminal_resource_count: int = Field(ge=0)


def _cleanup_proof_digest(
    engine_kind: EngineName,
    scenario_id: str,
    proof: WarehouseFaultCleanupProof,
) -> str:
    return digest(
        {
            "domain": "heinzel-warehouse-lifecycle-offline-control-plane-cleanup-proof-v2",
            "engine_kind": engine_kind,
            "scenario_id": scenario_id,
            "proof": proof.model_dump(mode="json"),
        }
    )


def _session_generation_digest(generations: tuple[int, ...]) -> str:
    return digest(
        {
            "domain": "heinzel-warehouse-lifecycle-session-generation-journal-v1",
            "generations": generations,
        }
    )


class WarehouseFaultScenarioOutcome(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    engine_kind: EngineName
    scenario_id: str = Field(pattern=r"^(checkpoint|classification):[a-z_]+(:[a-z_]+|:[1-9])$")
    checkpoint: WarehouseLifecycleCheckpoint | None
    checkpoint_occurrence: int | None = Field(default=None, ge=1)
    provider_operation: ProviderOperation | None
    failure_classification: WarehouseFailureClassification | None
    expected_fault_state: FaultState
    observed_fault_state: FaultState
    expected_terminal_state: Literal["retired"] = "retired"
    observed_terminal_state: Literal["retired"]
    replay_disposition: ReplayDisposition
    cleanup_proof: WarehouseFaultCleanupProof
    cleanup_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: ScenarioDisposition

    @model_validator(mode="after")
    def requires_one_well_formed_scenario_shape(self) -> Self:
        if self.scenario_id.startswith("checkpoint:"):
            expected_id = (
                None
                if self.checkpoint is None or self.checkpoint_occurrence is None
                else f"checkpoint:{self.checkpoint.value}:{self.checkpoint_occurrence}"
            )
            valid = (
                self.scenario_id == expected_id
                and self.provider_operation is None
                and self.failure_classification is None
                and self.expected_fault_state == "process_stopped"
                and self.replay_disposition == "durable_restart_replayed"
            )
            expected_cleanup = {
                "repository_reopen_count": 2,
                "repository_session_count": 3,
                "provider_session_count": 3,
                "distinct_session_generation_count": 3,
                "session_generation_digest": _session_generation_digest((1, 2, 3)),
                "provision_effect_count": 1,
                "unique_resource_count": 1,
                "residual_resource_count": 0,
                "suspend_effect_count": 1,
                "resume_effect_count": 1,
                "retire_effect_count": 1,
                "terminal_operation_count": 4,
                "terminal_resource_count": 1,
            }
        else:
            expected_id = (
                None
                if self.provider_operation is None or self.failure_classification is None
                else (
                    f"classification:{self.provider_operation}:{self.failure_classification.value}"
                )
            )
            expected_fault_state = (
                "operation_live"
                if self.failure_classification
                is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
                else "operation_non_live"
            )
            valid = (
                self.scenario_id == expected_id
                and self.checkpoint is None
                and self.checkpoint_occurrence is None
                and self.expected_fault_state == expected_fault_state
                and self.replay_disposition
                == (
                    "remained_resumable_after_restart"
                    if expected_fault_state == "operation_live"
                    else "terminal_failure_remained_non_live"
                )
            )
            created_resource_count = (
                0
                if self.failure_classification
                is WarehouseFailureClassification.PERMANENT_CONFIGURATION
                and self.provider_operation in {"provision", "reconcile"}
                else 1
            )
            terminal_operation_count = (
                0
                if self.provider_operation is None
                else {
                    "provision": 2,
                    "reconcile": 2,
                    "validate": 2,
                    "suspend": 3,
                    "resume": 4,
                    "retire": (
                        3
                        if self.failure_classification
                        is WarehouseFailureClassification.PERMANENT_CONFIGURATION
                        else 2
                    ),
                }.get(self.provider_operation, 0)
            )
            expected_cleanup = {
                "repository_reopen_count": 3 if self.provider_operation == "reconcile" else 2,
                "repository_session_count": 4 if self.provider_operation == "reconcile" else 3,
                "provider_session_count": 4 if self.provider_operation == "reconcile" else 3,
                "distinct_session_generation_count": (
                    4 if self.provider_operation == "reconcile" else 3
                ),
                "session_generation_digest": _session_generation_digest(
                    (1, 2, 3, 4) if self.provider_operation == "reconcile" else (1, 2, 3)
                ),
                "provision_effect_count": created_resource_count,
                "unique_resource_count": created_resource_count,
                "residual_resource_count": 0,
                "suspend_effect_count": int(
                    self.provider_operation == "resume"
                    or (
                        self.provider_operation == "suspend"
                        and self.failure_classification
                        is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
                    )
                ),
                "resume_effect_count": int(
                    self.provider_operation == "resume"
                    and self.failure_classification
                    is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
                ),
                "retire_effect_count": created_resource_count,
                "terminal_operation_count": terminal_operation_count,
                "terminal_resource_count": created_resource_count,
            }
        observed_cleanup = self.cleanup_proof.model_dump(exclude={"schema_version"})
        valid = (
            valid
            and observed_cleanup == expected_cleanup
            and self.cleanup_proof_digest
            == _cleanup_proof_digest(self.engine_kind, self.scenario_id, self.cleanup_proof)
        )
        if not valid:
            raise ValueError("fault matrix outcome has an invalid scenario shape")
        return self


class SimulatedProcessStop(BaseException):
    pass


class StopOnce:
    def __init__(
        self,
        checkpoint: WarehouseLifecycleCheckpoint,
        *,
        occurrence: int = 1,
    ) -> None:
        if occurrence < 1:
            raise ValueError("occurrence is 1-based")
        self._checkpoint = checkpoint
        self._occurrence = occurrence
        self._seen = 0
        self.stopped = False
        self.observed: list[WarehouseLifecycleCheckpoint] = []

    def __call__(self, checkpoint: WarehouseLifecycleCheckpoint) -> None:
        self.observed.append(checkpoint)
        if checkpoint is not self._checkpoint or self.stopped:
            return
        self._seen += 1
        if self._seen == self._occurrence:
            self.stopped = True
            raise SimulatedProcessStop


class RecordCheckpoints:
    def __init__(self) -> None:
        self.observed: list[WarehouseLifecycleCheckpoint] = []

    def __call__(self, checkpoint: WarehouseLifecycleCheckpoint) -> None:
        self.observed.append(checkpoint)


class _SessionOpenJournal:
    def __init__(self, scenario_directory: Path) -> None:
        self._directory = scenario_directory / "session-open-journal"
        if self._directory.is_symlink():
            raise RuntimeError("fault matrix session journal cannot be a symlink")
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def append(self) -> int:
        while True:
            generations = self.generations()
            generation = generations[-1] + 1 if generations else 1
            path = self._directory / f"{generation:08d}.json"
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(canonical_bytes({"schema_version": "1", "generation": generation}))
                    stream.flush()
                    os.fsync(stream.fileno())
                directory_descriptor = os.open(
                    self._directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return generation

    def generations(self) -> tuple[int, ...]:
        generations: list[int] = []
        for path in sorted(self._directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise RuntimeError("fault matrix session journal contains an invalid entry")
            try:
                generation = int(path.stem)
                payload = json.loads(path.read_bytes())
            except (OSError, ValueError, json.JSONDecodeError):
                raise RuntimeError(
                    "fault matrix session journal contains an invalid entry"
                ) from None
            expected = {"schema_version": "1", "generation": generation}
            if payload != expected or path.read_bytes() != canonical_bytes(expected):
                raise RuntimeError("fault matrix session journal contains an invalid entry")
            generations.append(generation)
        observed = tuple(generations)
        if observed != tuple(range(1, len(observed) + 1)):
            raise RuntimeError("fault matrix session journal generations are not contiguous")
        return observed


class _DurableProviderState:
    def __init__(self, path: Path) -> None:
        self._path = path
        if not path.exists():
            self._write(
                {
                    "completed_provider_checkpoints": [],
                    "effect_counts": {
                        "provision": 0,
                        "resume": 0,
                        "retire": 0,
                        "suspend": 0,
                    },
                    "primary_resources": {},
                    "provision_operation_ids": [],
                    "running": True,
                }
            )

    def load(self) -> dict[str, object]:
        payload = json.loads(self._path.read_text())
        if not isinstance(payload, dict):
            raise RuntimeError("durable provider state is not an object")
        return payload

    def update(self, **changes: object) -> None:
        payload = self.load()
        payload.update(changes)
        self._write(payload)

    def _write(self, payload: dict[str, object]) -> None:
        temporary = self._path.with_suffix(".temporary")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class LifecycleProvider:
    def __init__(
        self,
        engine_kind: EngineKind,
        fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
        state_path: Path,
    ) -> None:
        self.engine_kind = engine_kind
        self._fault_hook = fault_hook
        self._state = _DurableProviderState(state_path)
        self.failures: dict[ProviderOperation, WarehouseFailureClassification] = {}

    @property
    def provision_operation_ids(self) -> tuple[str, ...]:
        operation_ids = self._state.load()["provision_operation_ids"]
        if not isinstance(operation_ids, list):
            raise RuntimeError("durable provision operation ids are invalid")
        return tuple(str(value) for value in operation_ids)

    @property
    def effect_counts(self) -> dict[str, int]:
        counts = self._state.load()["effect_counts"]
        if not isinstance(counts, dict):
            raise RuntimeError("durable provider effect counts are invalid")
        return {str(name): int(value) for name, value in counts.items()}

    @property
    def primary_resources(self) -> dict[str, str]:
        resources = self._state.load()["primary_resources"]
        if not isinstance(resources, dict):
            raise RuntimeError("durable primary resources are invalid")
        return {str(operation_id): str(state) for operation_id, state in resources.items()}

    @property
    def running(self) -> bool:
        return bool(self._state.load()["running"])

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        del binding
        self._raise_next("provision")
        self._provision_or_adopt(operation)
        return _provision_result(operation)

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        self._raise_next("reconcile")
        if operation.operation_kind is WarehouseOperationKind.PROVISION:
            self._provision_or_adopt(operation)
        elif operation.operation_kind is WarehouseOperationKind.SUSPEND and self.running:
            self.suspend(binding, operation)
        elif operation.operation_kind is WarehouseOperationKind.RESUME and not self.running:
            self.resume(binding, operation)
        return _provision_result(operation)

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult | ResumeWarehouseValidationResult:
        del operation
        self._raise_next("validate")
        if resume:
            return ResumeWarehouseValidationResult(evidence=_resume_evidence(binding))
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED)
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED)
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RESTORE_PLAN)
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED)
        restore = _restore_verification(binding)
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RESTORE_VERIFIED)
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CLEANUP)
        return InitialWarehouseValidationResult(
            evidence=_validation_evidence(binding, restore),
            restore_verification=restore,
        )

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        del binding, operation
        self._raise_next("suspend")
        self._record_effect("suspend", running=False)

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        del binding, operation
        self._raise_next("resume")
        self._record_effect("resume", running=True)

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        del operation
        self._raise_next("retire")
        completed = self._completed_provider_checkpoints()
        if WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION not in completed:
            resources = self.primary_resources
            active = tuple(
                operation_id for operation_id, state in resources.items() if state == "active"
            )
            if len(active) > 1:
                raise RuntimeError("fault matrix provider has duplicate active primary resources")
            if active:
                resources[active[0]] = "retired"
                counts = self.effect_counts
                counts["retire"] += 1
                self._state.update(
                    effect_counts=counts,
                    primary_resources=resources,
                    running=False,
                )
            self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION)
        return _retirement_evidence(binding)

    def _provision_or_adopt(self, operation: PrivateWarehouseOperation) -> None:
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_RESOURCE_PLAN)
        resources = self.primary_resources
        resource_state = resources.get(operation.operation_id)
        if resource_state is None:
            resources[operation.operation_id] = "active"
            counts = self.effect_counts
            counts["provision"] += 1
            self._state.update(
                effect_counts=counts,
                primary_resources=resources,
                provision_operation_ids=(
                    *self.provision_operation_ids,
                    operation.operation_id,
                ),
                running=True,
            )
        elif resource_state != "active":
            raise RuntimeError("fault matrix provider cannot adopt a retired primary resource")
        self._complete_provider_phase(WarehouseLifecycleCheckpoint.AFTER_PROVIDER_CREATE)

    def _completed_provider_checkpoints(self) -> set[WarehouseLifecycleCheckpoint]:
        checkpoints = self._state.load()["completed_provider_checkpoints"]
        if not isinstance(checkpoints, list):
            raise RuntimeError("durable provider checkpoints are invalid")
        return {WarehouseLifecycleCheckpoint(value) for value in checkpoints}

    def _complete_provider_phase(self, checkpoint: WarehouseLifecycleCheckpoint) -> None:
        completed = self._completed_provider_checkpoints()
        if checkpoint in completed:
            return
        completed.add(checkpoint)
        self._state.update(completed_provider_checkpoints=sorted(item.value for item in completed))
        self._fault_hook(checkpoint)

    def _record_effect(self, name: str, *, running: bool) -> None:
        counts = self.effect_counts
        counts[name] += 1
        self._state.update(effect_counts=counts, running=running)

    def _raise_next(self, operation: ProviderOperation) -> None:
        classification = self.failures.pop(operation, None)
        if classification is not None:
            raise WarehouseProviderError(operation=operation, classification=classification)


class _LifecycleSession:
    def __init__(
        self,
        *,
        scenario_directory: Path,
        engine_kind: EngineKind,
        fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
    ) -> None:
        database_path = scenario_directory / "warehouse.sqlite"
        self._database_path = database_path
        self._session_journal = _SessionOpenJournal(scenario_directory)
        self.repository = SQLiteWarehouseRepository(str(database_path))
        try:
            self.control = WarehouseControlService(self.repository, clock=lambda: _NOW)
            self.provider = LifecycleProvider(
                engine_kind,
                fault_hook,
                scenario_directory / "provider-state.json",
            )
            self.orchestrator = WarehouseLifecycleOrchestrator(
                control=self.control,
                repository=self.repository,
                provider=self.provider,
                clock=lambda: _NOW,
                fault_hook=fault_hook,
            )
            self.generation = self._session_journal.append()
        except BaseException:
            with suppress(BaseException):
                self.repository.close()
            raise

    def close(self) -> None:
        self.repository.close()

    def session_generations(self) -> tuple[int, ...]:
        return self._session_journal.generations()

    def terminal_operation_count(self, tenant_id: str, binding_id: str) -> int:
        with sqlite3.connect(f"file:{self._database_path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM private_warehouse_operations "
                "WHERE tenant_id = ? AND binding_id = ? AND status IN (?, ?)",
                (
                    tenant_id,
                    binding_id,
                    WarehouseOperationStatus.SUCCEEDED.value,
                    WarehouseOperationStatus.FAILED.value,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("fault matrix operation count was unavailable")
        return int(row[0])


def _open_session(
    scenario_directory: Path,
    engine_kind: EngineKind,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
) -> _LifecycleSession:
    return _LifecycleSession(
        scenario_directory=scenario_directory,
        engine_kind=engine_kind,
        fault_hook=fault_hook,
    )


def _provision_result(operation: PrivateWarehouseOperation) -> WarehouseProvisionResult:
    return WarehouseProvisionResult(
        tenant_id=operation.tenant_id,
        binding_id=operation.binding_id,
        binding_revision=operation.binding_revision,
        operation_id=operation.operation_id,
        engine_kind=operation.engine_kind,
        private_resource_handle=f"resource:{operation.operation_id}",
        provider_build_digest="1" * 64,
        resource_inventory_digest="2" * 64,
    )


def _restore_verification(binding: WarehouseBinding) -> WarehouseRestoreVerification:
    return WarehouseRestoreVerification(
        verification_id="wrv-fault-matrix",
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
        verified_at=_OBSERVED_AT,
    )


def _validation_evidence(
    binding: WarehouseBinding,
    restore: WarehouseRestoreVerification,
) -> WarehouseValidationEvidence:
    return WarehouseValidationEvidence(
        evidence_id="wev-fault-matrix",
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        validation_profile=WarehouseValidationProfile.PRODUCTION,
        engine_kind=binding.engine_kind,
        engine_version="1.0",
        engine_build_digest="9" * 64,
        engine_image_digest="a" * 64,
        principal_profile_digest=restore.principal_profile_digest,
        namespace_grant_matrix_digest="b" * 64,
        tls_probe_digest="c" * 64,
        network_isolation_probe_digest="d" * 64,
        encryption_at_rest_evidence_digest="e" * 64,
        encryption_at_rest_disposition=EncryptionAtRestDisposition.PROVEN,
        positive_probe_digest="f" * 64,
        denial_probe_digest="0" * 64,
        ledger_probe_digest="1" * 64,
        monitoring_probe_digest="2" * 64,
        capacity_alert_probe_digest="3" * 64,
        backup_artifact_digest=restore.source_backup_artifact_digest,
        restore_verification_digest=digest(restore),
        restore_cleanup_digest="4" * 64,
        observed_at=_OBSERVED_AT,
    )


def _resume_evidence(binding: WarehouseBinding) -> WarehouseResumeValidationEvidence:
    return WarehouseResumeValidationEvidence(
        evidence_id="wrev-fault-matrix",
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
        observed_at=_OBSERVED_AT + timedelta(minutes=1),
    )


def _retirement_evidence(binding: WarehouseBinding) -> WarehouseRetirementEvidence:
    return WarehouseRetirementEvidence(
        evidence_id="wret-fault-matrix",
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
        observed_at=_OBSERVED_AT,
    )


def _scenario_directory(root: Path, engine_kind: EngineKind, scenario_id: str) -> Path:
    path = root / engine_kind.value / digest(scenario_id)[:16]
    path.mkdir(mode=0o700, parents=True)
    return path


def _create_draft(
    session: _LifecycleSession, engine_kind: EngineKind, scenario_id: str
) -> WarehouseBinding:
    return session.control.create_draft(
        tenant_id=f"fault-{engine_kind.value}-{digest(scenario_id)[:12]}",
        engine_kind=engine_kind,
        region="local",
        capacity_profile="mvp-fixed",
    )


def _reopen(
    session: _LifecycleSession,
    scenario_directory: Path,
    engine_kind: EngineKind,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
) -> _LifecycleSession:
    session.close()
    return _open_session(scenario_directory, engine_kind, fault_hook)


def _invoke_with_process_restart(
    session: _LifecycleSession,
    scenario_directory: Path,
    engine_kind: EngineKind,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
    invoke: Callable[[_LifecycleSession], WarehouseBinding],
) -> tuple[_LifecycleSession, WarehouseBinding]:
    try:
        return session, invoke(session)
    except SimulatedProcessStop:
        restarted = _reopen(session, scenario_directory, engine_kind, fault_hook)
        return restarted, invoke(restarted)


def _drive_complete_lifecycle(
    session: _LifecycleSession,
    scenario_directory: Path,
    engine_kind: EngineKind,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
    draft: WarehouseBinding,
) -> tuple[_LifecycleSession, WarehouseBinding]:
    session, ready = _invoke_with_process_restart(
        session,
        scenario_directory,
        engine_kind,
        fault_hook,
        lambda active: active.orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        ),
    )
    session, suspended = _invoke_with_process_restart(
        session,
        scenario_directory,
        engine_kind,
        fault_hook,
        lambda active: active.orchestrator.suspend(
            ready.tenant_id,
            ready.binding_id,
            expected_revision=ready.revision,
        ),
    )
    session, resumed = _invoke_with_process_restart(
        session,
        scenario_directory,
        engine_kind,
        fault_hook,
        lambda active: active.orchestrator.resume(
            suspended.tenant_id,
            suspended.binding_id,
            expected_revision=suspended.revision,
        ),
    )
    session, retired = _invoke_with_process_restart(
        session,
        scenario_directory,
        engine_kind,
        fault_hook,
        lambda active: active.orchestrator.retire(
            resumed.tenant_id,
            resumed.binding_id,
            expected_revision=resumed.revision,
        ),
    )
    return session, retired


def _retired_terminal_state(state: WarehouseBindingState) -> Literal["retired"]:
    """The outcome field is `Literal["retired"]`; `state.value` is only `str`.

    `_verify_durable_terminal_cleanup` already refuses to return a binding in any
    other state, so this re-states that guarantee where the checker can see it.
    """
    if state is not WarehouseBindingState.RETIRED:
        raise RuntimeError("fault matrix outcome observed a non-retired terminal state")
    return "retired"


def _verify_durable_terminal_cleanup(
    session: _LifecycleSession,
    scenario_directory: Path,
    engine_kind: EngineKind,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None],
    binding: WarehouseBinding,
    *,
    scenario_id: str,
) -> tuple[WarehouseFaultCleanupProof, str, WarehouseBinding]:
    reopened = _reopen(session, scenario_directory, engine_kind, fault_hook)
    try:
        durable = reopened.control.get(binding.tenant_id, binding.binding_id)
        resources = reopened.provider.primary_resources
        if any(state not in {"active", "retired"} for state in resources.values()):
            raise RuntimeError("fault matrix provider resource state is invalid")
        residual_resource_count = sum(state == "active" for state in resources.values())
        terminal_resource_count = sum(state == "retired" for state in resources.values())
        if (
            durable.lifecycle_state is not WarehouseBindingState.RETIRED
            or residual_resource_count
            or len(resources) != terminal_resource_count
        ):
            raise RuntimeError("fault matrix scenario did not reach durable exact cleanup")
        effects = reopened.provider.effect_counts
        generations = reopened.session_generations()
        session_count = len(generations)
        proof = WarehouseFaultCleanupProof(
            repository_reopen_count=session_count - 1,
            repository_session_count=session_count,
            provider_session_count=session_count,
            distinct_session_generation_count=len(set(generations)),
            session_generation_digest=_session_generation_digest(generations),
            provision_effect_count=effects["provision"],
            unique_resource_count=len(resources),
            residual_resource_count=residual_resource_count,
            suspend_effect_count=effects["suspend"],
            resume_effect_count=effects["resume"],
            retire_effect_count=effects["retire"],
            terminal_operation_count=reopened.terminal_operation_count(
                binding.tenant_id, binding.binding_id
            ),
            terminal_resource_count=terminal_resource_count,
        )
        return proof, _cleanup_proof_digest(engine_kind.value, scenario_id, proof), durable
    finally:
        reopened.close()


def _checkpoint_occurrence_counts(
    root: Path, engine_kind: EngineKind
) -> dict[WarehouseLifecycleCheckpoint, int]:
    scenario_id = "checkpoint-baseline"
    directory = _scenario_directory(root, engine_kind, scenario_id)
    recorder = RecordCheckpoints()
    session = _open_session(directory, engine_kind, recorder)
    draft = _create_draft(session, engine_kind, scenario_id)
    try:
        session, _ = _drive_complete_lifecycle(
            session,
            directory,
            engine_kind,
            recorder,
            draft,
        )
        return {
            checkpoint: sum(observed is checkpoint for observed in recorder.observed)
            for checkpoint in WarehouseLifecycleCheckpoint
        }
    finally:
        session.close()


def _run_checkpoint_scenario(
    root: Path,
    engine_kind: EngineKind,
    checkpoint: WarehouseLifecycleCheckpoint,
    occurrence: int,
) -> WarehouseFaultScenarioOutcome:
    scenario_id = f"checkpoint:{checkpoint.value}:{occurrence}"
    directory = _scenario_directory(root, engine_kind, scenario_id)
    stop = StopOnce(checkpoint, occurrence=occurrence)
    session = _open_session(directory, engine_kind, stop)
    draft = _create_draft(session, engine_kind, scenario_id)
    session, retired = _drive_complete_lifecycle(
        session,
        directory,
        engine_kind,
        stop,
        draft,
    )
    if not stop.stopped:
        session.close()
        raise RuntimeError("fault matrix checkpoint occurrence was not reached")
    if session.provider.effect_counts != {
        "provision": 1,
        "resume": 1,
        "retire": 1,
        "suspend": 1,
    }:
        session.close()
        raise RuntimeError("fault matrix replay duplicated a provider effect")
    if (
        len(session.provider.provision_operation_ids) != 1
        or len(set(session.provider.provision_operation_ids)) != 1
    ):
        session.close()
        raise RuntimeError("fault matrix replay did not create exactly one primary resource")
    if session.provider.primary_resources != {
        session.provider.provision_operation_ids[0]: "retired"
    }:
        session.close()
        raise RuntimeError("fault matrix replay did not retire its exact primary resource")
    proof, proof_digest, durable = _verify_durable_terminal_cleanup(
        session,
        directory,
        engine_kind,
        stop,
        retired,
        scenario_id=scenario_id,
    )
    return WarehouseFaultScenarioOutcome(
        engine_kind=engine_kind.value,
        scenario_id=scenario_id,
        checkpoint=checkpoint,
        checkpoint_occurrence=occurrence,
        provider_operation=None,
        failure_classification=None,
        expected_fault_state="process_stopped",
        observed_fault_state="process_stopped",
        observed_terminal_state=_retired_terminal_state(durable.lifecycle_state),
        replay_disposition="durable_restart_replayed",
        cleanup_proof=proof,
        cleanup_proof_digest=proof_digest,
        disposition="passed",
    )


def _classification_target(
    session: _LifecycleSession,
    draft: WarehouseBinding,
    provider_operation: ProviderOperation,
) -> Callable[[_LifecycleSession], WarehouseBinding]:
    if provider_operation in {"provision", "reconcile", "validate"}:
        return lambda active: active.orchestrator.provision(
            draft.tenant_id,
            draft.binding_id,
            expected_revision=draft.revision,
        )
    ready = session.orchestrator.provision(
        draft.tenant_id,
        draft.binding_id,
        expected_revision=draft.revision,
    )
    if provider_operation == "suspend":
        return lambda active: active.orchestrator.suspend(
            ready.tenant_id,
            ready.binding_id,
            expected_revision=ready.revision,
        )
    if provider_operation == "retire":
        return lambda active: active.orchestrator.retire(
            ready.tenant_id,
            ready.binding_id,
            expected_revision=ready.revision,
        )
    suspended = session.orchestrator.suspend(
        ready.tenant_id,
        ready.binding_id,
        expected_revision=ready.revision,
    )
    if provider_operation == "resume":
        return lambda active: active.orchestrator.resume(
            suspended.tenant_id,
            suspended.binding_id,
            expected_revision=suspended.revision,
        )
    raise ValueError("unsupported provider operation")


def _retire_current(
    session: _LifecycleSession, tenant_id: str, binding_id: str
) -> WarehouseBinding:
    current = session.control.get(tenant_id, binding_id)
    if current.lifecycle_state is WarehouseBindingState.RETIRED:
        return current
    expected_revision = (
        current.revision - 1
        if current.lifecycle_state is WarehouseBindingState.RETIRING
        else current.revision
    )
    return session.orchestrator.retire(
        tenant_id,
        binding_id,
        expected_revision=expected_revision,
    )


def _run_classification_scenario(
    root: Path,
    engine_kind: EngineKind,
    provider_operation: ProviderOperation,
    classification: WarehouseFailureClassification,
) -> WarehouseFaultScenarioOutcome:
    scenario_id = f"classification:{provider_operation}:{classification.value}"
    directory = _scenario_directory(root, engine_kind, scenario_id)
    stop = StopOnce(WarehouseLifecycleCheckpoint.AFTER_OPERATION_CLAIM)
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None] = (
        stop if provider_operation == "reconcile" else lambda checkpoint: None
    )
    session = _open_session(directory, engine_kind, fault_hook)
    draft = _create_draft(session, engine_kind, scenario_id)
    if provider_operation == "reconcile":
        try:
            session.orchestrator.provision(
                draft.tenant_id,
                draft.binding_id,
                expected_revision=draft.revision,
            )
        except SimulatedProcessStop:
            session = _reopen(session, directory, engine_kind, fault_hook)
        else:
            session.close()
            raise RuntimeError("reconcile classification scenario did not stop after claim")
    target = _classification_target(session, draft, provider_operation)
    session.provider.failures[provider_operation] = classification
    try:
        target(session)
    except WarehouseProviderError as error:
        if error.classification is not classification:
            session.close()
            raise RuntimeError("fault matrix classification changed at the boundary") from None
    else:
        session.close()
        raise RuntimeError("fault matrix classification scenario did not fail")

    session = _reopen(session, directory, engine_kind, fault_hook)
    live = session.repository.load_live_operation(draft.tenant_id, draft.binding_id)
    observed_fault_state: FaultState = (
        "operation_live" if live is not None else "operation_non_live"
    )
    expected_fault_state: FaultState = (
        "operation_live"
        if classification is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
        else "operation_non_live"
    )
    if observed_fault_state != expected_fault_state:
        session.close()
        raise RuntimeError("fault matrix operation liveness did not match classification")
    if classification is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE:
        target(session)
        replay_disposition: ReplayDisposition = "remained_resumable_after_restart"
    else:
        replay_disposition = "terminal_failure_remained_non_live"
    retired = _retire_current(session, draft.tenant_id, draft.binding_id)
    proof, proof_digest, durable = _verify_durable_terminal_cleanup(
        session,
        directory,
        engine_kind,
        fault_hook,
        retired,
        scenario_id=scenario_id,
    )
    return WarehouseFaultScenarioOutcome(
        engine_kind=engine_kind.value,
        scenario_id=scenario_id,
        checkpoint=None,
        checkpoint_occurrence=None,
        provider_operation=provider_operation,
        failure_classification=classification,
        expected_fault_state=expected_fault_state,
        observed_fault_state=observed_fault_state,
        observed_terminal_state=_retired_terminal_state(durable.lifecycle_state),
        replay_disposition=replay_disposition,
        cleanup_proof=proof,
        cleanup_proof_digest=proof_digest,
        disposition="passed",
    )


def required_fault_scenario_ids() -> frozenset[tuple[EngineName, str]]:
    checkpoint_ids = tuple(
        f"checkpoint:{checkpoint.value}:{occurrence}"
        for checkpoint, count in _CHECKPOINT_OCCURRENCES.items()
        for occurrence in range(1, count + 1)
    )
    classification_ids = tuple(
        f"classification:{operation}:{classification.value}"
        for operation in _PROVIDER_OPERATIONS
        for classification in _CLASSIFICATIONS
    )
    return frozenset(
        (engine_kind.value, scenario_id)
        for engine_kind in EngineKind
        for scenario_id in (*checkpoint_ids, *classification_ids)
    )


def canonical_fault_matrix_outcomes(
    outcomes: tuple[WarehouseFaultScenarioOutcome, ...],
) -> tuple[WarehouseFaultScenarioOutcome, ...]:
    try:
        validated = tuple(
            WarehouseFaultScenarioOutcome.model_validate_json(canonical_bytes(outcome))
            for outcome in outcomes
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Warehouse lifecycle offline control-plane fault matrix is incomplete or unsuccessful"
        ) from None
    expected = required_fault_scenario_ids()
    observed = tuple((outcome.engine_kind, outcome.scenario_id) for outcome in validated)
    if (
        len(observed) != len(expected)
        or len(set(observed)) != len(observed)
        or set(observed) != expected
        or any(outcome.disposition != "passed" for outcome in validated)
        or any(
            outcome.expected_fault_state != outcome.observed_fault_state
            or outcome.expected_terminal_state != outcome.observed_terminal_state
            for outcome in validated
        )
    ):
        raise ValueError(
            "Warehouse lifecycle offline control-plane fault matrix is incomplete or unsuccessful"
        )
    return tuple(sorted(validated, key=lambda outcome: (outcome.engine_kind, outcome.scenario_id)))


def run_warehouse_lifecycle_fault_matrix(root: Path) -> tuple[WarehouseFaultScenarioOutcome, ...]:
    if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
        raise ValueError("fault matrix root must be an empty private directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    outcomes: list[WarehouseFaultScenarioOutcome] = []
    for engine_kind in EngineKind:
        observed_occurrences = _checkpoint_occurrence_counts(root, engine_kind)
        if observed_occurrences != _CHECKPOINT_OCCURRENCES:
            raise RuntimeError("fault matrix checkpoint occurrence vocabulary changed")
        outcomes.extend(
            _run_checkpoint_scenario(root, engine_kind, checkpoint, occurrence)
            for checkpoint, count in _CHECKPOINT_OCCURRENCES.items()
            for occurrence in range(1, count + 1)
        )
        outcomes.extend(
            _run_classification_scenario(root, engine_kind, operation, classification)
            for operation in _PROVIDER_OPERATIONS
            for classification in _CLASSIFICATIONS
        )
    return canonical_fault_matrix_outcomes(tuple(outcomes))


__all__ = [
    "RecordCheckpoints",
    "SimulatedProcessStop",
    "StopOnce",
    "WarehouseFaultCleanupProof",
    "WarehouseFaultScenarioOutcome",
    "canonical_fault_matrix_outcomes",
    "required_fault_scenario_ids",
    "run_warehouse_lifecycle_fault_matrix",
]
