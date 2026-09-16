from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import pillarmesh_provider_postgresql.warehouse as postgresql_warehouse_module
import pillarmesh_provider_sdk.compose as provider_compose_module
import pytest
from cryptography.fernet import Fernet
from pillarmesh_contract_model import digest
from pillarmesh_provider_postgresql import (
    PostgreSQLBackupCommandBoundary,
    PostgreSQLWarehouseProvider,
    PostgreSQLWarehouseSettings,
)
from pillarmesh_provider_postgresql.warehouse import _restore_identity, _warehouse_identity
from pillarmesh_provider_sdk import (
    ComposeErrorClassification,
    ComposeResourceKind,
    DockerComposeProcess,
)
from pillarmesh_warehouse_control import (
    EngineKind,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
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
    WarehouseRetirementEvidence,
    WarehouseSecretRetiredError,
    WarehouseSecretStorageError,
    WarehouseValidationResult,
)
from pillarmesh_warehouse_control import secrets as warehouse_secrets
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from pillarmesh_warehouse_control.service import WarehouseControlService
from pydantic import SecretStr

import tests.conformance.warehouse_lifecycle as warehouse_lifecycle_module
from tests.conformance.warehouse_lifecycle import (
    WarehouseLifecycleContractDriver,
    WarehouseLifecycleProviderFactory,
    assert_warehouse_lifecycle_contract,
    require_warehouse_lifecycle_conformance,
)
from tests.emulators.warehouses.postgresql.init_tls import generate_tls_material

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "tests" / "emulators" / "warehouses" / "postgresql" / "compose.yaml"
_RESTORE_RESOURCE_KINDS = {
    WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
    WarehouseResourceKind.RESTORE_CONTAINER,
    WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
    WarehouseResourceKind.RESTORE_DATA_VOLUME,
}


def _lexical_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


class _LiveCheckpointCrash(BaseException):
    pass


@dataclass(slots=True)
class _LiveFaultController:
    phase: str | None
    triggered: bool = False

    def crash(self, phase: str) -> None:
        if self.phase == phase and not self.triggered:
            self.triggered = True
            raise _LiveCheckpointCrash


class _LiveFaultComposeProcess(DockerComposeProcess):
    def __init__(self, *, fault_controller: _LiveFaultController) -> None:
        super().__init__(
            compose_file=COMPOSE_FILE,
            timeout_seconds=300,
            termination_grace_seconds=5,
        )
        self._fault_controller = fault_controller
        self._restore_stream_completed = False

    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        super().up(project_name=project_name, environment=environment)
        self._fault_controller.crash("primary_compose_up_before_created_commit")

    def exec(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        nonzero_classification: ComposeErrorClassification = "rejected",
    ) -> bytes:
        # Forwarded rather than dropped: a caller that classifies its own nonzero
        # exit (the ClickHouse provider does) would otherwise raise TypeError here.
        result = super().exec(
            project_name=project_name,
            arguments=arguments,
            environment=environment,
            input_bytes=input_bytes,
            nonzero_classification=nonzero_classification,
        )
        if arguments == ("--profile", "restore", "up", "--detach", "postgresql_restore"):
            self._fault_controller.crash("restore_compose_up_before_created_commit")
        return result

    @contextmanager
    def exec_stream(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: IO[bytes] | None = None,
    ) -> Iterator[IO[bytes]]:
        with super().exec_stream(
            project_name=project_name,
            arguments=arguments,
            environment=environment,
            stdin=stdin,
        ) as output:
            yield output
        if "pg_restore" in arguments:
            self._restore_stream_completed = True

    def remove_resource(
        self,
        *,
        resource_kind: provider_compose_module.ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> None:
        if resource_kind == "volume" and self._restore_stream_completed:
            self._fault_controller.crash("restore_stream_completed_before_volume_cleanup")
        super().remove_resource(
            resource_kind=resource_kind,
            identifier=identifier,
            environment=environment,
        )


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
        fault_controller: _LiveFaultController,
    ) -> None:
        self._repository = repository
        self._binding_id = binding_id
        self._clock = clock
        self._fault_controller = fault_controller

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
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT:
            self._fault_controller.crash("backup_artifact_before_created_commit")
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
    engine_kind = EngineKind.POSTGRESQL

    def __init__(self, provider: PostgreSQLWarehouseProvider) -> None:
        self._provider = provider
        self.provision_results: list[WarehouseProvisionResult] = []
        self.validation_results: list[WarehouseValidationResult] = []
        self.retirement_results: list[WarehouseRetirementEvidence] = []

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        result = self._provider.provision(binding, operation)
        self.provision_results.append(result)
        return result

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._provider.reconcile(binding, operation)

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
        self.retirement_results.append(result)
        return result


@dataclass(frozen=True, slots=True)
class _LiveHarness:
    repository: SQLiteWarehouseRepository
    control: WarehouseControlService
    orchestrator: WarehouseLifecycleOrchestrator
    provider: _CapturingProvider
    compose: DockerComposeProcess
    authority: warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority
    secret_reference: str
    provision_operation_id: str
    private_directory: Path
    secret_directory: Path
    binding: WarehouseBinding
    clock: _MutableClock
    credential_canaries: tuple[str, ...]
    backup_chunk_bytes: int
    restart_provider_factory: Callable[[], PostgreSQLWarehouseProvider]

    def provision(self, *, expected_revision: int) -> WarehouseBinding:
        return self.orchestrator.provision(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=expected_revision,
        )

    def validation_results(self) -> tuple[WarehouseValidationResult, ...]:
        return tuple(self.provider.validation_results)

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
        self, *, expected_revision: int
    ) -> tuple[WarehouseBinding, PrivateWarehouseOperation]:
        retiring = self.control.transition(
            self.binding.tenant_id,
            self.binding.binding_id,
            WarehouseBindingState.RETIRING,
            expected_revision=expected_revision,
        )
        return retiring, _claim_retirement_operation(self, retiring)

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        return self.provider.retire(binding, operation)

    def record_retired(
        self,
        binding: WarehouseBinding,
        evidence: WarehouseRetirementEvidence,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseBinding:
        self.control.record_retirement(
            self.binding.tenant_id,
            self.binding.binding_id,
            evidence,
            operation=operation,
            expected_revision=binding.revision,
        )
        return self.orchestrator.retire(
            self.binding.tenant_id,
            self.binding.binding_id,
            expected_revision=binding.revision,
        )

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
        self.clock.advance(timedelta(seconds=1))

    def advance_past_retention(self) -> None:
        self.clock.advance(timedelta(hours=2))

    def verify_backup_artifact(
        self,
        resource: PrivateWarehouseResource,
        *,
        should_exist: bool,
    ) -> bool:
        backup_path = Path(resource.provider_resource_handle)
        if should_exist:
            return (
                _lexical_entry_exists(backup_path)
                and backup_path.is_file()
                and backup_path.stat().st_size > self.backup_chunk_bytes
                and backup_path.stat().st_mode & 0o777 == 0o600
            )
        return (
            _lexical_entry_exists(backup_path)
            and backup_path.is_file()
            and backup_path.stat().st_size == 0
            and backup_path.stat().st_mode & 0o777 == 0o600
        )

    def verify_backup_key(self, *, should_exist: bool) -> bool:
        try:
            self.authority.backup_command_capability(
                self.secret_reference,
                operation_id=self.provision_operation_id,
            )
        except WarehouseSecretRetiredError:
            return not should_exist
        return should_exist

    def verify_absent_resources(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> bool:
        docker_kinds: dict[WarehouseResourceKind, ComposeResourceKind] = {
            WarehouseResourceKind.WAREHOUSE_CONTAINER: "container",
            WarehouseResourceKind.PRIVATE_NETWORK: "network",
            WarehouseResourceKind.WAREHOUSE_DATA_VOLUME: "volume",
            WarehouseResourceKind.RESTORE_CONTAINER: "container",
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK: "network",
            WarehouseResourceKind.RESTORE_DATA_VOLUME: "volume",
        }
        for resource in resources:
            docker_kind = docker_kinds.get(resource.resource_kind)
            if docker_kind is not None:
                absent = self.compose.resource_is_absent(
                    resource_kind=docker_kind,
                    identifier=resource.provider_resource_handle,
                    environment={},
                )
                if docker_kind == "network" and absent is None:
                    if resource.parent_resource_handle is None:
                        return False
                    discovered = self.compose.discover_resources(
                        project_name=resource.parent_resource_handle,
                        environment={},
                    )
                    if any(item.resource_kind == "network" for item in discovered):
                        return False
                elif absent is not True:
                    return False
                continue
            if resource.resource_kind in {
                WarehouseResourceKind.CREDENTIAL_FILE,
                WarehouseResourceKind.HOST_PORT_FILE,
                WarehouseResourceKind.TLS_PRIVATE_KEY,
                WarehouseResourceKind.TLS_CERTIFICATE,
                WarehouseResourceKind.BACKUP_ARTIFACT,
                WarehouseResourceKind.BACKUP_STAGING_FILE,
                WarehouseResourceKind.BACKUP_RETIREMENT_JOURNAL,
            }:
                path = Path(resource.provider_resource_handle)
                if not (
                    _lexical_entry_exists(path)
                    and path.is_file()
                    and path.stat().st_size == 0
                    and path.stat().st_mode & 0o777 == 0o600
                ):
                    return False
                continue
            if resource.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY:
                path = Path(resource.provider_resource_handle)
                if not (
                    _lexical_entry_exists(path)
                    and path.is_dir()
                    and path.stat().st_mode & 0o777 == 0o700
                ):
                    return False
        return True

    def verify_provider_restart_replay(self) -> bool:
        operation = self.repository.load_operation(
            self.binding.tenant_id,
            self.binding.binding_id,
            self.provision_operation_id,
        )
        current = self.control.get(self.binding.tenant_id, self.binding.binding_id)
        expected = self.provider.provision_results[0]
        expected_validation = self.provider.validation_results[0]
        resources_before_replay = self.resources()
        restarted = self.restart_provider_factory()
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
            self.restart_provider_factory().reconcile(current, competing)
        except WarehouseProviderError as error:
            return error.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
        return False


def _provider_factory_for(
    driver: WarehouseLifecycleContractDriver,
) -> WarehouseLifecycleProviderFactory:
    """Bind one already-built driver as the factory the contract suite calls.

    Closing over the loop variable directly is what a default-argument lambda was
    working around; binding a parameter instead satisfies the checker and the
    loop-capture lint without either workaround.
    """
    return lambda: driver


def _new_harness(
    tmp_path: Path,
    *,
    fault_phase: str | None = None,
    credential_canaries: tuple[str, ...] | None = None,
) -> _LiveHarness:
    clock = _MutableClock(datetime.now(UTC))
    fault_controller = _LiveFaultController(fault_phase)
    tenant_id = "tenant-postgresql-live-" + digest(str(tmp_path.resolve()))[:12]
    repository = SQLiteWarehouseRepository(str(tmp_path / "warehouse.sqlite"))
    control = WarehouseControlService(
        repository,
        clock=clock,
        readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
    )
    binding = control.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
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
    private_directory = tmp_path / "private-operation"
    private_directory.mkdir(mode=0o700)
    secret_directory = tmp_path / "operation-secrets"
    secret_directory.mkdir(mode=0o700)
    material = generate_tls_material(now=clock())
    credential_canaries = credential_canaries or tuple(secrets.token_urlsafe(24) for _ in range(8))
    if len(credential_canaries) != 8:
        raise ValueError("PostgreSQL live witness requires eight credential canaries")
    backup_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    operation_secrets = WarehouseOperationSecrets(
        administration_password=SecretStr(credential_canaries[0]),
        ingestion_runtime_password=SecretStr(credential_canaries[1]),
        transformation_runtime_password=SecretStr(credential_canaries[2]),
        answer_runtime_password=SecretStr(credential_canaries[3]),
        backup_restore_password=SecretStr(credential_canaries[4]),
        customer_sql_probe_password=SecretStr(credential_canaries[5]),
        catalog_password=SecretStr(credential_canaries[6]),
        bi_password=SecretStr(credential_canaries[7]),
        tls_private_key_pem=SecretStr(material.private_key_bundle),
        tls_certificate_pem=SecretStr(material.certificate_bundle),
        backup_encryption_key_b64=SecretStr(backup_key),
    )
    authority = warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority(
        directory=secret_directory,
        key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    secret_reference = authority.store(provision_operation_id, operation_secrets)
    recorder = _RepositoryResourceRecorder(
        repository,
        binding_id=binding.binding_id,
        clock=clock,
        fault_controller=fault_controller,
    )
    compose = _LiveFaultComposeProcess(fault_controller=fault_controller)
    settings = PostgreSQLWarehouseSettings(
        private_operation_directory=private_directory,
        retention_period=timedelta(hours=1),
        backup_chunk_bytes=32 * 1024,
        capacity_bytes=64 * 1024 * 1024,
    )

    def provider_factory() -> PostgreSQLWarehouseProvider:
        backup_commands = PostgreSQLBackupCommandBoundary(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            secrets=authority.backup_command_capability(
                secret_reference,
                operation_id=provision_operation_id,
            ),
            retirement=authority.backup_retirement_capability(
                secret_reference,
                operation_id=provision_operation_id,
            ),
            connect=__import__("psycopg").connect,
            clock=clock,
        )
        return PostgreSQLWarehouseProvider(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            administration_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="administration",
            ),
            ingestion_runtime_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="ingestion_runtime",
            ),
            transformation_runtime_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="transformation_runtime",
            ),
            answer_runtime_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="answer_runtime",
            ),
            customer_sql_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="customer_sql",
            ),
            catalog_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="catalog",
            ),
            bi_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="bi",
            ),
            tls_private_key_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="tls_private_key",
            ),
            tls_certificate_secret=authority.operation_capability(
                secret_reference,
                operation_id=provision_operation_id,
                purpose="tls_certificate",
            ),
            backup_commands=backup_commands,
            connect=__import__("psycopg").connect,
            clock=clock,
        )

    provider = provider_factory()
    capturing_provider = _CapturingProvider(provider)
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=capturing_provider,
        clock=clock,
    )
    return _LiveHarness(
        repository=repository,
        control=control,
        orchestrator=orchestrator,
        provider=capturing_provider,
        compose=compose,
        authority=authority,
        secret_reference=secret_reference,
        provision_operation_id=provision_operation_id,
        private_directory=private_directory,
        secret_directory=secret_directory,
        binding=binding,
        clock=clock,
        credential_canaries=(*credential_canaries, backup_key),
        backup_chunk_bytes=settings.backup_chunk_bytes,
        restart_provider_factory=provider_factory,
    )


def _claim_retirement_operation(
    harness: _LiveHarness, binding: WarehouseBinding
) -> PrivateWarehouseOperation:
    sequence = harness.repository.next_operation_sequence(binding.tenant_id)
    operation_id = (
        "wop-"
        + digest(
            {
                "domain": "pillarmesh-warehouse-operation-v1",
                "tenant_id": binding.tenant_id,
                "sequence": sequence,
            }
        )[:24]
    )
    operation = PrivateWarehouseOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id=operation_id,
        operation_kind=WarehouseOperationKind.RETIRE,
        engine_kind=EngineKind.POSTGRESQL,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=harness.clock(),
        updated_at=harness.clock(),
    )
    harness.repository.claim_operation(operation)
    running = operation.model_copy(update={"status": WarehouseOperationStatus.RUNNING})
    harness.repository.save_operation(operation, running)
    return running


def _cleanup_exact_resources(harness: _LiveHarness) -> None:
    with suppress(Exception):
        operation = harness.repository.load_operation(
            harness.binding.tenant_id,
            harness.binding.binding_id,
            harness.provision_operation_id,
        )
        restore_identity = _restore_identity(harness.binding, operation)
        _remove_exact_test_docker_resources(
            harness.compose,
            container_name=restore_identity.container_name,
            volume_name=restore_identity.data_volume_name,
            network_names=(
                restore_identity.network_name,
                restore_identity.loopback_network_name,
            ),
        )
    with suppress(Exception):
        identity = _warehouse_identity(harness.binding, harness.private_directory)
        _remove_exact_test_docker_resources(
            harness.compose,
            container_name=identity.container_name,
            volume_name=identity.data_volume_name,
            network_names=(identity.network_name, identity.loopback_network_name),
        )
    with suppress(Exception):
        harness.authority.delete(
            harness.secret_reference,
            operation_id=harness.provision_operation_id,
        )
    with suppress(Exception):
        harness.repository.close()


def _remove_exact_test_docker_resources(
    compose: DockerComposeProcess,
    *,
    container_name: str,
    volume_name: str,
    network_names: tuple[str, ...],
) -> None:
    first_failure: Exception | None = None
    removals: tuple[tuple[ComposeResourceKind, str], ...] = (
        ("container", container_name),
        *(("network", network_name) for network_name in network_names),
        ("volume", volume_name),
    )
    for resource_kind, identifier in removals:
        try:
            compose.remove_resource(
                resource_kind=resource_kind,
                identifier=identifier,
                environment={},
            )
        except Exception as error:
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        raise first_failure


def test_postgresql_backup_key_absence_requires_a_durable_retirement_tombstone(
    tmp_path: Path,
) -> None:
    harness = _new_harness(tmp_path)
    retirement = harness.authority.backup_retirement_capability(
        harness.secret_reference,
        operation_id=harness.provision_operation_id,
    )
    try:
        retirement.retire()

        backup_key_absent = harness.verify_backup_key(should_exist=False)

        assert backup_key_absent is True
    finally:
        harness.repository.close()


def test_postgresql_optimized_backup_key_absence_rejects_active_ciphertext() -> None:
    script = """
import os
import tempfile
from pathlib import Path

from pillarmesh_warehouse_control import WarehouseSecretRetiredError, WarehouseSecretStorageError
from tests.integration.test_postgresql_warehouse_live import _new_harness

with tempfile.TemporaryDirectory() as directory:
    harness = _new_harness(Path(directory).resolve())
    try:
        capability = harness.authority.backup_command_capability(
            harness.secret_reference,
            operation_id=harness.provision_operation_id,
        )
        active_canary = capability.resolve_backup_encryption_key().get_secret_value()
        if active_canary != harness.credential_canaries[-1]:
            raise SystemExit(2)
        ciphertext = next(harness.secret_directory.glob('*.fernet'))
        ciphertext_size = ciphertext.stat().st_size
        tombstone = harness.secret_directory / f'.{ciphertext.name}.delete'
        descriptor = os.open(tombstone, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            harness.verify_backup_key(should_exist=False)
        except WarehouseSecretRetiredError:
            raise SystemExit(3)
        except WarehouseSecretStorageError:
            pass
        else:
            raise SystemExit(4)
        if ciphertext.stat().st_size != ciphertext_size or ciphertext_size == 0:
            raise SystemExit(5)
    finally:
        harness.repository.close()
print('postgresql active ciphertext rejected')
"""

    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "postgresql active ciphertext rejected\n"


@pytest.mark.parametrize("storage_failure", ("corrupt", "unavailable"))
def test_postgresql_backup_key_absence_fails_closed_for_storage_failure(
    tmp_path: Path,
    storage_failure: str,
) -> None:
    harness = _new_harness(tmp_path)
    if storage_failure == "corrupt":
        secret_file = next(harness.secret_directory.glob("*.fernet"))
        secret_file.write_bytes(b"corrupt-secret-storage")
        os.chmod(secret_file, 0o600)
    else:
        os.close(harness.authority._directory_descriptor)
        harness.authority._directory_descriptor = -1
    try:
        with pytest.raises(WarehouseSecretStorageError) as failure:
            harness.verify_backup_key(should_exist=False)

        assert not isinstance(failure.value, WarehouseSecretRetiredError)
    finally:
        harness.repository.close()


@pytest.mark.live
@pytest.mark.emulator
def test_rendered_postgresql_compose_separates_primary_and_restore_routes(
    tmp_path: Path,
) -> None:
    expected_paths = {
        Path(postgresql_warehouse_module.__file__).resolve(): (
            ROOT
            / "providers"
            / "postgresql"
            / "src"
            / "pillarmesh_provider_postgresql"
            / "warehouse.py"
        ).resolve(),
        Path(provider_compose_module.__file__).resolve(): (
            ROOT / "packages" / "provider-sdk" / "src" / "pillarmesh_provider_sdk" / "compose.py"
        ).resolve(),
        Path(warehouse_lifecycle_module.__file__).resolve(): (
            ROOT / "tests" / "conformance" / "warehouse_lifecycle.py"
        ).resolve(),
        Path(__file__).resolve(): (
            ROOT / "tests" / "integration" / "test_postgresql_warehouse_live.py"
        ).resolve(),
    }
    assert all(loaded == expected for loaded, expected in expected_paths.items())
    assert (
        COMPOSE_FILE.resolve()
        == (ROOT / "tests" / "emulators" / "warehouses" / "postgresql" / "compose.yaml").resolve()
    )

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    environment = {
        "PILLARMESH_POSTGRES_BOOTSTRAP_PASSWORD": "render-only-secret",
        "PILLARMESH_POSTGRES_CONTAINER_NAME": "pm-render-primary",
        "PILLARMESH_POSTGRES_DATA_VOLUME_NAME": "pm-render-primary-data",
        "PILLARMESH_POSTGRES_HOST_PORT": "55432",
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_INTERNAL": "false",
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_NAME": "pm-render-primary-loopback",
        "PILLARMESH_POSTGRES_NETWORK_NAME": "pm-render-primary-internal",
        "PILLARMESH_POSTGRES_PRIVATE_DIRECTORY": str(private_directory),
        "PILLARMESH_POSTGRES_RESTORE_CONTAINER_NAME": "pm-render-inactive-restore",
    }
    compose = DockerComposeProcess(compose_file=COMPOSE_FILE)

    primary = json.loads(
        compose.exec(
            project_name="pm-render-primary",
            arguments=("config", "--format", "json"),
            environment=environment,
        )
    )["services"]["postgresql"]
    restore = json.loads(
        compose.exec(
            project_name="pm-render-restore",
            arguments=("--profile", "restore", "config", "--format", "json"),
            environment={
                **environment,
                "PILLARMESH_POSTGRES_CONTAINER_NAME": "pm-render-inactive-primary",
                "PILLARMESH_POSTGRES_DATA_VOLUME_NAME": "pm-render-restore-data",
                "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_INTERNAL": "true",
                "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_NAME": "pm-render-restore-loopback",
                "PILLARMESH_POSTGRES_NETWORK_NAME": "pm-render-restore-internal",
                "PILLARMESH_POSTGRES_RESTORE_CONTAINER_NAME": "pm-render-restore",
            },
        )
    )["services"]["postgresql_restore"]

    assert primary["ports"] == [
        {
            "mode": "ingress",
            "target": 5432,
            "published": "55432",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert primary["extra_hosts"] == ["host.docker.internal=host-gateway"]
    assert restore.get("ports", []) == []
    assert restore.get("extra_hosts", []) == []
    assert restore["profiles"] == ["restore"]


def test_live_cleanup_detects_a_dangling_private_file_symlink(tmp_path: Path) -> None:
    target = tmp_path / "missing-target"
    dangling = tmp_path / "recorded-private-file"
    dangling.symlink_to(target)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    resource = PrivateWarehouseResource(
        tenant_id="tenant-live-cleanup",
        binding_id="whb-" + "1" * 24,
        binding_revision=1,
        operation_id="wop-" + "2" * 24,
        resource_id="wrs-" + "3" * 24,
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle=str(dangling),
        parent_resource_handle=str(tmp_path),
        creation_state=WarehouseResourceCreationState.CREATED,
        retention_deadline=now,
        cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(
        warehouse_lifecycle_module.WarehouseLifecycleConformanceError,
        match="private_file_absence",
    ):
        warehouse_lifecycle_module.require_warehouse_lifecycle_conformance(
            _LiveHarness.verify_absent_resources(object.__new__(_LiveHarness), (resource,)),
            "private_file_absence",
        )


@pytest.mark.live
@pytest.mark.emulator
def test_real_postgresql_warehouse_lifecycle_is_replay_safe_and_retention_aware(
    tmp_path: Path,
) -> None:
    completed_evidence: list[tuple[str, str]] = []
    for run_index in range(2):
        run_directory = tmp_path / f"lifecycle-{run_index}"
        run_directory.mkdir(mode=0o700)
        harness = _new_harness(run_directory)
        try:
            observation = assert_warehouse_lifecycle_contract(_provider_factory_for(harness))
            initial = observation.initial_validation
            assert isinstance(initial.restore_verification, WarehouseRestoreVerification)
            resource_counts = Counter(
                resource.resource_kind for resource in observation.initial_resources
            )
            assert sum(resource_counts[kind] for kind in _RESTORE_RESOURCE_KINDS) == 5
            assert resource_counts[WarehouseResourceKind.RESTORE_PRIVATE_NETWORK] == 2
            backup_path = Path(observation.backup_resource.provider_resource_handle)
            tombstones = tuple(harness.secret_directory.glob("*.delete"))
            tombstone_size = tombstones[0].stat().st_size if len(tombstones) == 1 else None

            assert len(tombstones) == 1
            assert tombstone_size == 0

            public_payloads = (
                initial.evidence.model_dump_json(),
                initial.restore_verification.model_dump_json(),
                observation.deletion_retirement.model_dump_json(),
            )
            privacy_canaries = (
                *harness.credential_canaries,
                str(harness.private_directory),
                str(backup_path),
                "127.0.0.1",
            )
            assert all(
                canary not in payload for canary in privacy_canaries for payload in public_payloads
            )
            completed_evidence.append(
                (
                    initial.evidence.network_isolation_probe_digest,
                    observation.deletion_retirement.cleanup_disposition_digest,
                )
            )
        finally:
            _cleanup_exact_resources(harness)

    assert len(completed_evidence) == 2


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.parametrize(
    "fault_phase",
    (
        "primary_compose_up_before_created_commit",
        "backup_artifact_before_created_commit",
        "restore_compose_up_before_created_commit",
        "restore_stream_completed_before_volume_cleanup",
    ),
)
def test_real_postgresql_checkpoint_fault_replays_to_verified_terminal_cleanup(
    tmp_path: Path,
    fault_phase: str,
) -> None:
    harness = _new_harness(tmp_path, fault_phase=fault_phase)
    try:
        with pytest.raises(_LiveCheckpointCrash):
            harness.provision(expected_revision=harness.binding.revision)

        if fault_phase == "restore_stream_completed_before_volume_cleanup":
            assert isinstance(harness.compose, _LiveFaultComposeProcess)
            assert harness.compose._restore_stream_completed is True
            restore_volume = next(
                resource
                for resource in harness.resources()
                if resource.resource_kind is WarehouseResourceKind.RESTORE_DATA_VOLUME
            )
            restore_volume_absent = harness.compose.resource_is_absent(
                resource_kind="volume",
                identifier=restore_volume.provider_resource_handle,
                environment={},
            )
            require_warehouse_lifecycle_conformance(
                restore_volume_absent is False,
                "interrupted_restore_volume_presence",
            )

        restarted_provider = _CapturingProvider(harness.restart_provider_factory())
        restarted_orchestrator = WarehouseLifecycleOrchestrator(
            control=harness.control,
            repository=harness.repository,
            provider=restarted_provider,
            clock=harness.clock,
        )
        recovered = replace(
            harness,
            provider=restarted_provider,
            orchestrator=restarted_orchestrator,
        )

        ready = recovered.provision(expected_revision=harness.binding.revision)

        assert ready.lifecycle_state is WarehouseBindingState.READY
        retiring, retirement_operation = recovered.begin_retirement(
            expected_revision=ready.revision
        )
        retained = recovered.retire(retiring, retirement_operation)
        assert retained.retained_resource_count > 0
        assert retained.cleanup_failed_resource_count == 0
        retired = recovered.record_retired(retiring, retained, retirement_operation)
        assert retired.lifecycle_state is WarehouseBindingState.RETIRED
        recovered.advance_past_retention()
        deleted = recovered.delete_retained_resources(retired)
        assert deleted.retained_resource_count == 0
        assert deleted.cleanup_failed_resource_count == 0
        final_resources = recovered.resources()
        assert all(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in final_resources
        )
        final_resources_absent = recovered.verify_absent_resources(final_resources)
        require_warehouse_lifecycle_conformance(
            final_resources_absent,
            "recovered_final_resource_absence",
        )
        backup_key_absent = recovered.verify_backup_key(should_exist=False)
        require_warehouse_lifecycle_conformance(
            backup_key_absent,
            "recovered_backup_key_absence",
        )
    finally:
        _cleanup_exact_resources(harness)
