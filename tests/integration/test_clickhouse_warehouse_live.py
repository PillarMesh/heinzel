from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pillarmesh_contract_model import digest
from pillarmesh_provider_clickhouse import (
    ClickHouseBackupCommandBoundary,
    ClickHouseWarehouseProvider,
    ClickHouseWarehouseSettings,
)
from pillarmesh_provider_clickhouse.warehouse import (
    ClickHouseHTTPClient,
    _restore_identity,
    _warehouse_identity,
)
from pillarmesh_provider_sdk import ComposeResourceKind, DockerComposeProcess
from pillarmesh_warehouse_control import (
    EngineKind,
    InitialWarehouseValidationResult,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    ResumeWarehouseValidationResult,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseLifecycleCheckpoint,
    WarehouseLifecycleOrchestrator,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationSecrets,
    WarehouseOperationStatus,
    WarehouseProviderError,
    WarehouseProvisionResult,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseSecretRetiredError,
    WarehouseSecretStorageError,
    WarehouseValidationEvidence,
    WarehouseValidationResult,
)
from pillarmesh_warehouse_control import secrets as warehouse_secrets
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from pillarmesh_warehouse_control.service import WarehouseControlService
from pydantic import SecretStr

from tests.conformance.warehouse_lifecycle import (
    assert_warehouse_lifecycle_contract,
    require_warehouse_lifecycle_conformance,
)
from tests.emulators.warehouses.postgresql.init_tls import generate_tls_material

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "tests" / "emulators" / "warehouses" / "clickhouse" / "compose.yaml"


class _LiveCheckpointCrash(BaseException):
    pass


@dataclass(slots=True)
class _LiveFaultController:
    checkpoint: WarehouseLifecycleCheckpoint | None
    triggered: bool = False

    def __call__(self, checkpoint: WarehouseLifecycleCheckpoint) -> None:
        if self.checkpoint is checkpoint and not self.triggered:
            self.triggered = True
            raise _LiveCheckpointCrash


@dataclass(slots=True)
class _MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _RepositoryResourceRecorder:
    def __init__(
        self,
        repository: SQLiteWarehouseRepository,
        *,
        binding_id: str,
        clock: _MutableClock,
    ) -> None:
        self._repository = repository
        self._binding_id = binding_id
        self._clock = clock

    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]:
        return self._repository.load_resources(tenant_id, binding_id)

    def record_planned(self, resource: PrivateWarehouseResource) -> None:
        self._repository.record_resources((resource,))

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]:
        return self._repository.reopen_resources_for_recreation(
            expected_resources,
            reopened_at=reopened_at,
        )

    def mark_created(
        self,
        tenant_id: str,
        resource_id: str,
        provider_resource_handle: str,
    ) -> None:
        resource = self._resource(tenant_id, resource_id)
        if resource.provider_resource_handle != provider_resource_handle:
            raise RuntimeError("provider resource handle changed after planning")
        self._repository.save_resource(
            resource.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.CREATED,
                    "updated_at": self._clock(),
                }
            )
        )

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None:
        resource = self._resource(tenant_id, resource_id)
        self._repository.save_resource(
            resource.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.AMBIGUOUS,
                    "updated_at": self._clock(),
                }
            )
        )

    def record_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        resource = self._resource(tenant_id, resource_id)
        self._repository.save_resource(
            resource.model_copy(
                update={
                    "cleanup_status": status,
                    "cleanup_failure_classification": classification,
                    "updated_at": self._clock(),
                }
            )
        )

    def record_cleanup_batch(self, resources: tuple[PrivateWarehouseResource, ...]) -> None:
        self._repository.save_resources(resources)

    def _resource(self, tenant_id: str, resource_id: str) -> PrivateWarehouseResource:
        return next(
            resource
            for resource in self._repository.load_resources(tenant_id, self._binding_id)
            if resource.resource_id == resource_id
        )


class _CapturingProvider:
    engine_kind = EngineKind.CLICKHOUSE

    def __init__(self, provider: ClickHouseWarehouseProvider) -> None:
        self._provider = provider
        self.provision_results: list[WarehouseProvisionResult] = []
        self.validation_results: list[WarehouseValidationResult] = []
        self.retirement_bindings: list[WarehouseBinding] = []
        self.retirement_operations: list[PrivateWarehouseOperation] = []

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        result = self._provider.provision(binding, operation)
        self.provision_results.append(result)
        return result

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        result = self._provider.reconcile(binding, operation)
        self.provision_results.append(result)
        return result

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult:
        result = self._provider.validate(binding, operation, resume=resume)
        self.validation_results.append(result)
        return result

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._provider.suspend(binding, operation)

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._provider.resume(binding, operation)

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        result = self._provider.retire(binding, operation)
        self.retirement_bindings.append(binding)
        self.retirement_operations.append(operation)
        return result


@dataclass(frozen=True, slots=True)
class _HarnessConfiguration:
    repository_path: Path
    private_directory: Path
    secret_directory: Path
    authority_key: SecretStr
    secret_reference: str
    provision_operation_id: str
    binding: WarehouseBinding
    clock: _MutableClock
    credential_canaries: tuple[str, ...]
    backup_key: str
    tls_private_marker: str
    tls_certificate_marker: str


def _load_durable_validation_results(
    configuration: _HarnessConfiguration,
) -> tuple[WarehouseValidationResult, ...]:
    connection = sqlite3.connect(
        configuration.repository_path.resolve().as_uri() + "?mode=ro",
        uri=True,
    )
    try:
        initial_rows = connection.execute(
            "SELECT payload FROM warehouse_validation_evidence "
            "WHERE tenant_id = ? AND binding_id = ? ORDER BY binding_revision",
            (configuration.binding.tenant_id, configuration.binding.binding_id),
        ).fetchall()
        restore_rows = connection.execute(
            "SELECT payload FROM warehouse_restore_verifications "
            "WHERE tenant_id = ? AND binding_id = ? ORDER BY binding_revision",
            (configuration.binding.tenant_id, configuration.binding.binding_id),
        ).fetchall()
        resume_rows = connection.execute(
            "SELECT payload FROM warehouse_resume_validation_evidence "
            "WHERE tenant_id = ? AND binding_id = ? ORDER BY binding_revision",
            (configuration.binding.tenant_id, configuration.binding.binding_id),
        ).fetchall()
    finally:
        connection.close()
    require_warehouse_lifecycle_conformance(
        len(initial_rows) == 1 and len(restore_rows) == 1,
        "durable_initial_validation_evidence_count",
    )
    initial = InitialWarehouseValidationResult(
        evidence=WarehouseValidationEvidence.model_validate_json(initial_rows[0][0]),
        restore_verification=WarehouseRestoreVerification.model_validate_json(restore_rows[0][0]),
    )
    resumed = tuple(
        ResumeWarehouseValidationResult(
            evidence=WarehouseResumeValidationEvidence.model_validate_json(row[0])
        )
        for row in resume_rows
    )
    return (initial, *resumed)


@dataclass(slots=True)
class _ClickHouseLifecycleDriver:
    configuration: _HarnessConfiguration
    repository: SQLiteWarehouseRepository
    control: WarehouseControlService
    orchestrator: WarehouseLifecycleOrchestrator
    provider: _CapturingProvider
    compose: DockerComposeProcess
    authority: warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority
    binding: WarehouseBinding
    restart_provider: Callable[[], ClickHouseWarehouseProvider]
    private_directory: Path
    secret_directory: Path
    private_markers: tuple[str, ...]
    clock: _MutableClock

    @property
    def provisioning_binding(self) -> WarehouseBinding:
        return self.binding.model_copy(
            update={
                "lifecycle_state": WarehouseBindingState.PROVISIONING,
                "revision": self.binding.revision + 1,
                "updated_at": self.clock(),
            }
        )

    @property
    def provision_operation(self) -> PrivateWarehouseOperation:
        binding = self.provisioning_binding
        return PrivateWarehouseOperation(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            operation_id=self.configuration.provision_operation_id,
            operation_kind=WarehouseOperationKind.PROVISION,
            engine_kind=EngineKind.CLICKHOUSE,
            status=WarehouseOperationStatus.CLAIMED,
            phase=WarehouseOperationPhase.CLAIMED,
            started_at=self.clock(),
            updated_at=self.clock(),
        )

    def provision(self, *, expected_revision: int) -> WarehouseBinding:
        return self.orchestrator.provision(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=expected_revision,
        )

    def validation_results(self) -> tuple[WarehouseValidationResult, ...]:
        return _load_durable_validation_results(self.configuration)

    def resources(self) -> tuple[PrivateWarehouseResource, ...]:
        return self.repository.load_resources(self.binding.tenant_id, self.binding.binding_id)

    def suspend(self, *, expected_revision: int) -> WarehouseBinding:
        return self.orchestrator.suspend(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=expected_revision,
        )

    def resume(self, *, expected_revision: int) -> WarehouseBinding:
        return self.orchestrator.resume(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=expected_revision,
        )

    def begin_retirement(
        self,
        *,
        expected_revision: int,
    ) -> tuple[WarehouseBinding, PrivateWarehouseOperation]:
        self.orchestrator.retire(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=expected_revision,
        )
        return (
            self.provider.retirement_bindings[-1],
            self.provider.retirement_operations[-1],
        )

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        del operation
        return self.repository.load_retirement_evidence(
            binding.tenant_id,
            binding.binding_id,
            binding.revision,
        )

    def record_retired(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseBinding:
        durable_operation = self.repository.load_operation(
            operation.tenant_id,
            operation.binding_id,
            operation.operation_id,
        )
        require_warehouse_lifecycle_conformance(
            durable_operation.tenant_id == operation.tenant_id
            and durable_operation.binding_id == operation.binding_id
            and durable_operation.binding_revision == operation.binding_revision
            and durable_operation.operation_kind is operation.operation_kind
            and durable_operation.engine_kind is operation.engine_kind
            and durable_operation.status is WarehouseOperationStatus.SUCCEEDED
            and durable_operation.phase is WarehouseOperationPhase.RETIRED,
            "durable_retirement_operation",
        )
        durable_evidence = self.repository.load_retirement_evidence(
            binding.tenant_id,
            binding.binding_id,
            binding.revision,
        )
        require_warehouse_lifecycle_conformance(
            durable_evidence == evidence,
            "durable_retirement_evidence",
        )
        return self.control.get(binding.tenant_id, binding.binding_id)

    def delete_retained_resources(
        self,
        binding: WarehouseBinding,
    ) -> WarehouseRetirementEvidence:
        return self.orchestrator.delete_retained_resources(
            binding.tenant_id,
            binding.binding_id,
            expected_revision=binding.revision,
            authorized=True,
        )

    def advance_for_resume(self) -> None:
        self.clock.advance(timedelta(minutes=5))

    def advance_past_retention(self) -> None:
        self.clock.advance(timedelta(hours=2))

    def verify_backup_artifact(
        self,
        resource: PrivateWarehouseResource,
        *,
        should_exist: bool,
    ) -> bool:
        path = Path(resource.provider_resource_handle)
        return path.is_file() and (path.stat().st_size > 0) is should_exist

    def verify_backup_key(self, *, should_exist: bool) -> bool:
        try:
            self.authority.backup_command_capability(
                self.configuration.secret_reference,
                operation_id=self.configuration.provision_operation_id,
            )
        except WarehouseSecretRetiredError:
            return not should_exist
        return should_exist

    def verify_absent_resources(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> bool:
        compose_kinds: dict[WarehouseResourceKind, ComposeResourceKind] = {
            WarehouseResourceKind.WAREHOUSE_CONTAINER: "container",
            WarehouseResourceKind.PRIVATE_NETWORK: "network",
            WarehouseResourceKind.WAREHOUSE_DATA_VOLUME: "volume",
            WarehouseResourceKind.RESTORE_CONTAINER: "container",
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK: "network",
            WarehouseResourceKind.RESTORE_DATA_VOLUME: "volume",
        }
        file_kinds = {
            WarehouseResourceKind.CREDENTIAL_FILE,
            WarehouseResourceKind.HOST_PORT_FILE,
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            WarehouseResourceKind.TLS_CERTIFICATE,
            WarehouseResourceKind.BACKUP_ARTIFACT,
            WarehouseResourceKind.BACKUP_STAGING_FILE,
        }
        for resource in resources:
            compose_kind = compose_kinds.get(resource.resource_kind)
            if compose_kind is not None:
                absent = self.compose.resource_is_absent(
                    resource_kind=compose_kind,
                    identifier=resource.provider_resource_handle,
                    environment={},
                )
                if absent is not True:
                    return False
            elif resource.resource_kind in file_kinds:
                path = Path(resource.provider_resource_handle)
                if not path.is_file() or path.stat().st_size != 0:
                    return False
            elif resource.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY:
                path = Path(resource.provider_resource_handle)
                if not path.is_dir() or not all(
                    child.stat().st_size == 0 for child in path.iterdir()
                ):
                    return False
        return True

    def verify_provider_restart_replay(self) -> bool:
        operation = self.repository.load_operation(
            self.binding.tenant_id,
            self.binding.binding_id,
            self.configuration.provision_operation_id,
        )
        current = self.control.get(self.binding.tenant_id, self.binding.binding_id)
        expected = self.provider.provision_results[0]
        expected_validation = self.validation_results()[0]
        resources_before_replay = self.resources()
        restarted = self.restart_provider()
        validating = current.model_copy(
            update={
                "lifecycle_state": WarehouseBindingState.VALIDATING,
                "revision": operation.binding_revision + 1,
                "provisioned_at": None,
            }
        )

        if restarted.reconcile(current, operation) != expected:
            return False
        if restarted.reconcile(current, operation) != expected:
            return False
        if restarted.validate(validating, operation, resume=False) != expected_validation:
            return False
        if self.resources() != resources_before_replay:
            return False

        competing = operation.model_copy(update={"operation_id": "wop-" + "f" * 24})
        try:
            self.restart_provider().reconcile(current, competing)
        except WarehouseProviderError as error:
            return error.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
        return False

    def restart(self) -> _ClickHouseLifecycleDriver:
        self.repository.close()
        return _open_lifecycle_driver(self.configuration)

    def close(self) -> None:
        self.repository.close()


def _remove_exact_resources(
    compose: DockerComposeProcess,
    *,
    container_names: tuple[str, ...],
    network_names: tuple[str, ...],
    volume_name: str,
) -> None:
    first_failure: Exception | None = None
    specifications: tuple[tuple[ComposeResourceKind, str], ...] = (
        *(("container", name) for name in container_names),
        *(("network", name) for name in network_names),
        ("volume", volume_name),
    )
    for resource_kind, identifier in specifications:
        try:
            compose.remove_resource(
                resource_kind=resource_kind,
                identifier=identifier,
                environment={},
            )
        except Exception as error:
            if first_failure is None:
                first_failure = error
    for resource_kind, identifier in specifications:
        try:
            absent = compose.resource_is_absent(
                resource_kind=resource_kind,
                identifier=identifier,
                environment={},
            )
            if absent is not True:
                raise RuntimeError("ClickHouse exact fallback cleanup could not prove absence")
        except Exception as error:
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        raise first_failure


def _cleanup_exact_driver_resources(driver: _ClickHouseLifecycleDriver) -> None:
    identity = _warehouse_identity(driver.provisioning_binding, driver.private_directory)
    restore = _restore_identity(driver.provisioning_binding, driver.provision_operation)
    first_failure: Exception | None = None
    cleanups: tuple[Callable[[], None], ...] = (
        lambda: _remove_exact_resources(
            driver.compose,
            container_names=(restore.container_name, restore.isolation_probe_container_name),
            network_names=(restore.private_network_name, restore.loopback_network_name),
            volume_name=restore.data_volume_name,
        ),
        lambda: _remove_exact_resources(
            driver.compose,
            container_names=(identity.container_name,),
            network_names=(identity.private_network_name, identity.loopback_network_name),
            volume_name=identity.data_volume_name,
        ),
        driver.repository.close,
    )
    for cleanup in cleanups:
        try:
            cleanup()
        except Exception as error:
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        raise first_failure


def _open_lifecycle_driver(
    configuration: _HarnessConfiguration,
    *,
    fault_checkpoint: WarehouseLifecycleCheckpoint | None = None,
) -> _ClickHouseLifecycleDriver:
    repository = SQLiteWarehouseRepository(str(configuration.repository_path))
    control = WarehouseControlService(
        repository,
        clock=configuration.clock,
        readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
    )
    authority = warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority(
        directory=configuration.secret_directory,
        key=configuration.authority_key,
    )
    recorder = _RepositoryResourceRecorder(
        repository,
        binding_id=configuration.binding.binding_id,
        clock=configuration.clock,
    )
    compose = DockerComposeProcess(
        compose_file=COMPOSE_FILE,
        timeout_seconds=300,
        termination_grace_seconds=5,
    )
    settings = ClickHouseWarehouseSettings(
        private_operation_directory=configuration.private_directory,
        retention_period=timedelta(hours=1),
        readiness_timeout_seconds=120,
    )
    fault_controller = _LiveFaultController(fault_checkpoint)

    def provider_factory() -> ClickHouseWarehouseProvider:
        backup = ClickHouseBackupCommandBoundary(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            secret_capability=authority.backup_command_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
            ),
            retirement_capability=authority.backup_retirement_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
            ),
            client_factory=ClickHouseHTTPClient,
            clock=configuration.clock,
            fault_hook=fault_controller,
        )
        return ClickHouseWarehouseProvider(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            administration_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="administration",
            ),
            ingestion_runtime_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="ingestion_runtime",
            ),
            transformation_runtime_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="transformation_runtime",
            ),
            customer_sql_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="customer_sql",
            ),
            catalog_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="catalog",
            ),
            bi_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="bi",
            ),
            tls_private_key_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="tls_private_key",
            ),
            tls_certificate_secret=authority.operation_capability(
                configuration.secret_reference,
                operation_id=configuration.provision_operation_id,
                purpose="tls_certificate",
            ),
            backup_commands=backup,
            client_factory=ClickHouseHTTPClient,
            clock=configuration.clock,
            fault_hook=fault_controller,
        )

    capturing_provider = _CapturingProvider(provider_factory())
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=capturing_provider,
        clock=configuration.clock,
    )
    return _ClickHouseLifecycleDriver(
        configuration=configuration,
        repository=repository,
        control=control,
        orchestrator=orchestrator,
        provider=capturing_provider,
        compose=compose,
        authority=authority,
        binding=configuration.binding,
        restart_provider=provider_factory,
        private_directory=configuration.private_directory,
        secret_directory=configuration.secret_directory,
        private_markers=(
            *configuration.credential_canaries,
            configuration.backup_key,
            configuration.tls_private_marker,
            configuration.tls_certificate_marker,
        ),
        clock=configuration.clock,
    )


def _new_lifecycle_driver(
    tmp_path: Path,
    *,
    fault_checkpoint: WarehouseLifecycleCheckpoint | None = None,
    credential_canaries: tuple[str, ...] | None = None,
    backup_key_bytes: bytes | None = None,
) -> _ClickHouseLifecycleDriver:
    clock = _MutableClock(datetime.now(UTC))
    private_directory = tmp_path / "private-operation"
    private_directory.mkdir(mode=0o700)
    secret_directory = tmp_path / "operation-secrets"
    secret_directory.mkdir(mode=0o700)
    repository_path = tmp_path / "warehouse.sqlite"
    repository = SQLiteWarehouseRepository(str(repository_path))
    control = WarehouseControlService(
        repository,
        clock=clock,
        readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
    )
    tenant_id = "tenant-clickhouse-live-" + digest(str(tmp_path.resolve()))[:12]
    binding = control.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.CLICKHOUSE,
        region="local",
        capacity_profile="mvp-fixed",
    )
    provision_operation_id = (
        "wop-"
        + digest(
            {
                "domain": "pillarmesh-warehouse-operation-v1",
                "tenant_id": tenant_id,
                "sequence": 1,
            }
        )[:24]
    )
    material = generate_tls_material(now=clock())
    credential_canaries = credential_canaries or tuple(secrets.token_urlsafe(24) for _ in range(7))
    if len(credential_canaries) != 7:
        raise ValueError("ClickHouse live witness requires seven credential canaries")
    backup_key = base64.b64encode(backup_key_bytes or os.urandom(32)).decode("ascii")
    operation_secrets = WarehouseOperationSecrets(
        administration_password=SecretStr(credential_canaries[0]),
        ingestion_runtime_password=SecretStr(credential_canaries[1]),
        transformation_runtime_password=SecretStr(credential_canaries[2]),
        backup_restore_password=SecretStr(credential_canaries[3]),
        customer_sql_probe_password=SecretStr(credential_canaries[4]),
        catalog_password=SecretStr(credential_canaries[5]),
        bi_password=SecretStr(credential_canaries[6]),
        tls_private_key_pem=SecretStr(material.private_key_bundle),
        tls_certificate_pem=SecretStr(material.certificate_bundle),
        backup_encryption_key_b64=SecretStr(backup_key),
    )
    authority_key = SecretStr(Fernet.generate_key().decode("ascii"))
    authority = warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority(
        directory=secret_directory,
        key=authority_key,
    )
    secret_reference = authority.store(provision_operation_id, operation_secrets)
    repository.close()
    configuration = _HarnessConfiguration(
        repository_path=repository_path,
        private_directory=private_directory,
        secret_directory=secret_directory,
        authority_key=authority_key,
        secret_reference=secret_reference,
        provision_operation_id=provision_operation_id,
        binding=binding,
        clock=clock,
        credential_canaries=credential_canaries,
        backup_key=backup_key,
        tls_private_marker=material.private_key_bundle,
        tls_certificate_marker=material.certificate_bundle,
    )
    return _open_lifecycle_driver(configuration, fault_checkpoint=fault_checkpoint)


def test_clickhouse_live_harness_reopens_the_binding_from_sqlite(tmp_path: Path) -> None:
    driver = _new_lifecycle_driver(tmp_path)
    original_binding = driver.binding

    restarted = driver.restart()
    try:
        durable_binding = restarted.control.get(
            original_binding.tenant_id,
            original_binding.binding_id,
        )

        assert restarted is not driver
        assert restarted.repository is not driver.repository
        assert restarted.binding == original_binding
        assert durable_binding == original_binding
    finally:
        restarted.close()


def test_clickhouse_live_harness_uses_the_provider_backup_key_encoding(tmp_path: Path) -> None:
    key_bytes = bytes.fromhex("fb" * 32)
    driver = _new_lifecycle_driver(tmp_path, backup_key_bytes=key_bytes)
    try:
        decoded_backup_key = base64.b64decode(driver.configuration.backup_key, validate=True)

        assert decoded_backup_key == key_bytes
    finally:
        driver.close()


def test_clickhouse_backup_key_absence_requires_a_durable_retirement_tombstone(
    tmp_path: Path,
) -> None:
    driver = _new_lifecycle_driver(tmp_path)
    retirement = driver.authority.backup_retirement_capability(
        driver.configuration.secret_reference,
        operation_id=driver.configuration.provision_operation_id,
    )
    try:
        retirement.retire()

        backup_key_absent = driver.verify_backup_key(should_exist=False)

        assert backup_key_absent is True
    finally:
        driver.close()


def test_clickhouse_optimized_backup_key_absence_rejects_active_ciphertext() -> None:
    script = """
import os
import tempfile
from pathlib import Path

from pillarmesh_warehouse_control import WarehouseSecretRetiredError, WarehouseSecretStorageError
from tests.integration.test_clickhouse_warehouse_live import _new_lifecycle_driver

with tempfile.TemporaryDirectory() as directory:
    driver = _new_lifecycle_driver(Path(directory).resolve())
    try:
        capability = driver.authority.backup_command_capability(
            driver.configuration.secret_reference,
            operation_id=driver.configuration.provision_operation_id,
        )
        active_canary = capability.resolve_backup_encryption_key().get_secret_value()
        if active_canary != driver.configuration.backup_key:
            raise SystemExit(2)
        ciphertext = next(driver.secret_directory.glob('*.fernet'))
        ciphertext_size = ciphertext.stat().st_size
        tombstone = driver.secret_directory / f'.{ciphertext.name}.delete'
        descriptor = os.open(tombstone, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            driver.verify_backup_key(should_exist=False)
        except WarehouseSecretRetiredError:
            raise SystemExit(3)
        except WarehouseSecretStorageError:
            pass
        else:
            raise SystemExit(4)
        if ciphertext.stat().st_size != ciphertext_size or ciphertext_size == 0:
            raise SystemExit(5)
    finally:
        driver.close()
print('clickhouse active ciphertext rejected')
"""

    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "clickhouse active ciphertext rejected\n"


@pytest.mark.parametrize("storage_failure", ("corrupt", "unavailable"))
def test_clickhouse_backup_key_absence_fails_closed_for_storage_failure(
    tmp_path: Path,
    storage_failure: str,
) -> None:
    driver = _new_lifecycle_driver(tmp_path)
    if storage_failure == "corrupt":
        secret_file = next(driver.secret_directory.glob("*.fernet"))
        secret_file.write_bytes(b"corrupt-secret-storage")
        os.chmod(secret_file, 0o600)
    else:
        os.close(driver.authority._directory_descriptor)
        driver.authority._directory_descriptor = -1
    try:
        with pytest.raises(WarehouseSecretStorageError) as failure:
            driver.verify_backup_key(should_exist=False)

        assert not isinstance(failure.value, WarehouseSecretRetiredError)
    finally:
        driver.close()


@pytest.mark.live
@pytest.mark.emulator
def test_real_clickhouse_satisfies_provider_neutral_lifecycle_contract(tmp_path: Path) -> None:
    driver = _new_lifecycle_driver(tmp_path)
    try:
        observation = assert_warehouse_lifecycle_contract(lambda: driver)
        restore = _restore_identity(driver.provisioning_binding, driver.provision_operation)
        restore_containers = {
            resource.provider_resource_handle
            for resource in observation.initial_resources
            if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
        }
        assert restore_containers == {
            restore.container_name,
            restore.isolation_probe_container_name,
        }

        reopened = SQLiteWarehouseRepository(str(driver.configuration.repository_path))
        try:
            durable_binding = reopened.load(driver.binding.tenant_id, driver.binding.binding_id)
            durable_operation = reopened.load_operation(
                driver.binding.tenant_id,
                driver.binding.binding_id,
                driver.configuration.provision_operation_id,
            )
            durable_resources = reopened.load_resources(
                driver.binding.tenant_id,
                driver.binding.binding_id,
            )
        finally:
            reopened.close()
        durable_validations = _load_durable_validation_results(driver.configuration)
        assert durable_binding == observation.retired
        assert durable_operation.status is WarehouseOperationStatus.SUCCEEDED
        assert durable_resources == observation.final_resources
        assert durable_validations == (
            observation.initial_validation,
            observation.resume_validation,
        )
        assert all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in durable_resources
        )
    finally:
        _cleanup_exact_driver_resources(driver)


@pytest.mark.live
@pytest.mark.emulator
def test_real_clickhouse_hard_crash_reopens_sqlite_and_replays_to_exact_deprovision(
    tmp_path: Path,
) -> None:
    crashed = _new_lifecycle_driver(
        tmp_path,
        fault_checkpoint=WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED,
    )
    configuration = crashed.configuration
    try:
        with pytest.raises(_LiveCheckpointCrash):
            crashed.provision(expected_revision=crashed.binding.revision)

        durable_operation = crashed.repository.load_live_operation(
            crashed.binding.tenant_id,
            crashed.binding.binding_id,
        )
        durable_resources = crashed.resources()
        restore = _restore_identity(crashed.provisioning_binding, crashed.provision_operation)
        assert durable_operation is not None
        assert durable_operation.status is WarehouseOperationStatus.RUNNING
        assert any(
            resource.provider_resource_handle == restore.isolation_probe_container_name
            and resource.creation_state is WarehouseResourceCreationState.PLANNED
            and resource.cleanup_status is WarehouseResourceCleanupStatus.PENDING
            for resource in durable_resources
        )

        crashed.close()
        recovered = _open_lifecycle_driver(configuration)
        try:
            ready = recovered.provision(expected_revision=recovered.binding.revision)
            assert ready.lifecycle_state is WarehouseBindingState.READY
            recovered_validations = recovered.validation_results()
            assert len(recovered_validations) == 1
            recovered_initial = recovered_validations[0]
            assert isinstance(recovered_initial, InitialWarehouseValidationResult)
            assert recovered_initial.evidence.binding_revision == ready.revision - 1
            assert recovered_initial.restore_verification.binding_revision == ready.revision - 1
            assert recovered_initial.evidence.restore_verification_digest == digest(
                recovered_initial.restore_verification
            )
            recovered.provider.validation_results.clear()

            observation = assert_warehouse_lifecycle_contract(lambda: recovered)
            reopened = SQLiteWarehouseRepository(str(configuration.repository_path))
            try:
                final_binding = reopened.load(
                    recovered.binding.tenant_id,
                    recovered.binding.binding_id,
                )
                final_operation = reopened.load_operation(
                    recovered.binding.tenant_id,
                    recovered.binding.binding_id,
                    configuration.provision_operation_id,
                )
                final_resources = reopened.load_resources(
                    recovered.binding.tenant_id,
                    recovered.binding.binding_id,
                )
            finally:
                reopened.close()
            final_validations = _load_durable_validation_results(configuration)
            assert final_binding == observation.retired
            assert final_operation.status is WarehouseOperationStatus.SUCCEEDED
            assert final_resources == observation.final_resources
            assert final_validations == (
                recovered_initial,
                observation.resume_validation,
            )
            final_resources_absent = recovered.verify_absent_resources(final_resources)
            require_warehouse_lifecycle_conformance(
                final_resources_absent,
                "recovered_final_resource_absence",
            )
        finally:
            _cleanup_exact_driver_resources(recovered)
    except BaseException:
        _cleanup_exact_driver_resources(crashed)
        raise
