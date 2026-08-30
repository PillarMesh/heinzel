from __future__ import annotations

import base64
import hashlib
import io
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Literal

import psycopg
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    BackupStreamIntegrityError,
    BackupStreamObservation,
    ComposeCommandError,
    DockerComposeProcess,
    decrypt_backup_stream,
    encrypt_backup_stream,
)
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    ResumeWarehouseValidationResult,
    WarehouseBackupCommandSecretCapability,
    WarehouseBackupRetirementCapability,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseFaultHook,
    WarehouseLifecycleCheckpoint,
    WarehouseOperationKind,
    WarehouseOperationSecretCapability,
    WarehouseProviderError,
    WarehouseProvisionResult,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
    WarehouseResourceRecorder,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
    WarehouseValidationResult,
    noop_warehouse_fault_hook,
)
from pillarmesh_warehouse_control.retirement import canonical_retirement_resource_snapshot
from pydantic import BaseModel, ConfigDict

from .warehouse_database import (
    PostgreSQLConnectionTarget,
    PostgreSQLDatabaseObservation,
    PostgreSQLGrantPlan,
    assert_password_connection_denied,
    assert_plaintext_connection_denied,
    connect_tls,
    create_canonical_roles,
    derive_grant_plan,
    observe_database,
    observe_restored_database,
    prepare_database,
    prepare_restored_database,
    probe_login_scope,
    rotate_bootstrap_password,
    wait_for_tls_connection,
    with_probe_login_cleanup_evidence,
)
from .warehouse_protocol import connect_denial_probe
from .warehouse_settings import (
    POSTGRESQL_WAREHOUSE_IMAGE,
    PostgreSQLWarehouseSettings,
)

_PRIMARY_COMPOSE_SERVICE = "postgresql"
_RESTORE_COMPOSE_PROFILE = "restore"
_RESTORE_COMPOSE_SERVICE = "postgresql_restore"


class PostgreSQLBackupIntegrityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("postgresql backup artifact failed integrity validation")


@dataclass(frozen=True, slots=True)
class _OrderedRestoreFailure:
    order: int
    error: BaseException


class _NullWriter:
    def write(self, value: bytes) -> int:
        return len(value)


@dataclass(frozen=True, slots=True)
class MVPFixedCapacityObservation:
    alert_is_open: bool
    transition: Literal["opened", "cleared", "unchanged"]


@dataclass(frozen=True, slots=True)
class _PostgreSQLWarehouseIdentity:
    private_resource_handle: str
    project_name: str
    container_name: str
    network_name: str
    loopback_network_name: str
    data_volume_name: str
    binding_directory: Path
    credential_file: Path
    tls_private_key: Path
    tls_certificate: Path
    client_private_key: Path
    client_certificate: Path
    root_certificate: Path
    hba_configuration: Path
    host_port_file: Path


@dataclass(frozen=True, slots=True)
class _PostgreSQLRestoreIdentity:
    project_name: str
    container_name: str
    network_name: str
    loopback_network_name: str
    data_volume_name: str


class _TLSPrivateKeyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    server_private_key_pem: str
    client_private_key_pem: str


class _TLSCertificateBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    ca_certificate_pem: str
    server_certificate_pem: str
    client_certificate_pem: str


@dataclass(frozen=True, slots=True)
class _BackupLifecycleResult:
    backup_artifact_digest: str
    restore_verification: WarehouseRestoreVerification
    restore_cleanup_digest: str
    positive_probe_digest: str
    denial_probe_digest: str
    network_isolation_probe_digest: str


type _Connect = Callable[..., object]
type _Entropy = Callable[[int], bytes]
type _ComposeResourceKind = Literal["container", "volume", "network"]
type _RetirementCheckpoint = Callable[[str], None]


def _raise_primary_or_cleanup_failures(
    primary_failure: BaseException | None,
    cleanup_failures: list[BaseException],
) -> None:
    if primary_failure is not None:
        for _cleanup_failure in cleanup_failures:
            primary_failure.add_note("PostgreSQL cleanup also failed")
        raise primary_failure
    if cleanup_failures:
        raise cleanup_failures[0]


def _attempt_all_resource_actions(
    resources: tuple[PrivateWarehouseResource, ...],
    action: Callable[[PrivateWarehouseResource], None],
    *,
    primary_failure: BaseException | None = None,
) -> None:
    cleanup_failures: list[BaseException] = []
    for resource in resources:
        try:
            action(resource)
        except BaseException as error:
            cleanup_failures.append(error)
    _raise_primary_or_cleanup_failures(primary_failure, cleanup_failures)


def _select_restore_stream_failure(
    process_failure: _OrderedRestoreFailure | None,
    decrypt_failure: _OrderedRestoreFailure | None,
    *,
    decrypt_worker_alive: bool,
) -> BaseException | None:
    if process_failure is not None and not isinstance(process_failure.error, Exception):
        return process_failure.error
    if (
        decrypt_failure is not None
        and isinstance(decrypt_failure.error, PostgreSQLBackupIntegrityError)
        and (process_failure is None or decrypt_failure.order < process_failure.order)
    ):
        return decrypt_failure.error
    if process_failure is not None:
        return process_failure.error
    if decrypt_worker_alive:
        return RuntimeError("PostgreSQL restore decrypt stream did not terminate")
    if decrypt_failure is not None:
        return decrypt_failure.error
    return None


class _RestoreLoopbackTunnel:
    def __init__(self, *, compose: DockerComposeProcess, project_name: str) -> None:
        self._compose = compose
        self._project_name = project_name
        self._listener: socket.socket | None = None
        self._environment: Mapping[str, str] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._server_thread: threading.Thread | None = None
        self._server_stopped = True
        self._handler_threads: list[threading.Thread] = []
        self._active_handlers: set[threading.Thread] = set()
        self._clients: set[socket.socket] = set()
        self._client_inputs: dict[socket.socket, IO[bytes]] = {}
        self._failure: Exception | None = None
        self.port = _allocate_loopback_port()

    def start(self, *, environment: Mapping[str, str]) -> None:
        if self._server_thread is not None or self._stop.is_set():
            raise RuntimeError("PostgreSQL restore loopback tunnel was already started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            listener.settimeout(0.1)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._environment = dict(environment)
        with self._condition:
            self._server_stopped = False
        self._server_thread = threading.Thread(
            target=self._serve,
            name="pillarmesh-postgresql-restore-tunnel",
            daemon=True,
        )
        try:
            self._server_thread.start()
        except BaseException:
            self._stop.set()
            with self._condition:
                self._server_stopped = True
                self._condition.notify_all()
            with suppress(BaseException):
                listener.close()
            self._listener = None
            self._server_thread = None
            raise

    def assert_healthy(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def close(self) -> None:
        self._stop.set()
        cleanup_failures: list[BaseException] = []
        listener = self._listener
        if listener is not None:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            except BaseException as error:
                cleanup_failures.append(error)
            try:
                listener.close()
            except BaseException as error:
                cleanup_failures.append(error)
        server_thread = self._server_thread
        if server_thread is not None:
            with self._condition:
                self._condition.wait_for(lambda: self._server_stopped)
            while server_thread.is_alive():
                server_thread.join(timeout=0.1)

        cancelled_clients: set[int] = set()
        cancelled_inputs: set[int] = set()
        while True:
            with self._condition:
                active_handlers = tuple(self._active_handlers)
                clients = tuple(
                    client for client in self._clients if id(client) not in cancelled_clients
                )
                client_inputs = tuple(
                    client_input
                    for client_input in self._client_inputs.values()
                    if id(client_input) not in cancelled_inputs
                )
                if not active_handlers:
                    handlers = tuple(self._handler_threads)
                    break
            for client in clients:
                cancelled_clients.add(id(client))
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                except BaseException as error:
                    cleanup_failures.append(error)
                try:
                    client.close()
                except BaseException as error:
                    cleanup_failures.append(error)
            for client_input in client_inputs:
                cancelled_inputs.add(id(client_input))
                try:
                    client_input.close()
                except BaseException as error:
                    cleanup_failures.append(error)
            with self._condition:
                has_uncancelled_client = any(
                    id(client) not in cancelled_clients for client in self._clients
                )
                has_uncancelled_input = any(
                    id(client_input) not in cancelled_inputs
                    for client_input in self._client_inputs.values()
                )
                if self._active_handlers and not (has_uncancelled_client or has_uncancelled_input):
                    self._condition.wait()
        for handler in handlers:
            while handler.is_alive():
                handler.join(timeout=0.1)
        try:
            self.assert_healthy()
        except BaseException as error:
            cleanup_failures.append(error)
        if cleanup_failures:
            raise cleanup_failures[0]

    def _serve(self) -> None:
        with self._condition:
            self._server_stopped = False
            self._condition.notify_all()
        try:
            listener = self._listener
            if listener is None:
                self._record_failure(RuntimeError("PostgreSQL restore tunnel had no listener"))
                return
            while not self._stop.is_set():
                try:
                    client, _address = listener.accept()
                except TimeoutError:
                    continue
                except OSError as error:
                    if not self._stop.is_set():
                        self._record_failure(error)
                    return
                handler = threading.Thread(
                    target=self._forward,
                    args=(client,),
                    name="pillarmesh-postgresql-restore-connection",
                    daemon=True,
                )
                with self._condition:
                    self._clients.add(client)
                    self._handler_threads.append(handler)
                    self._active_handlers.add(handler)
                    self._condition.notify_all()
                try:
                    handler.start()
                except BaseException as error:
                    with self._condition:
                        self._clients.discard(client)
                        self._active_handlers.discard(handler)
                        self._condition.notify_all()
                    with suppress(OSError):
                        client.close()
                    if isinstance(error, Exception):
                        self._record_failure(error)
                        return
                    raise
        finally:
            with self._condition:
                self._server_stopped = True
                self._condition.notify_all()

    def _forward(self, client: socket.socket) -> None:
        current_thread = threading.current_thread()
        client_input: IO[bytes] | None = None
        with self._condition:
            self._clients.add(client)
            self._active_handlers.add(current_thread)
            self._condition.notify_all()
        try:
            environment = self._environment
            if environment is None:
                self._record_failure(
                    RuntimeError("PostgreSQL restore tunnel started without scope")
                )
                return
            bridge_script = (
                "exec 3<>/dev/tcp/127.0.0.1/5432\n"
                "cat <&3 &\n"
                "reader_pid=$!\n"
                "cat >&3 || true\n"
                'wait "$reader_pid" || true\n'
            )
            client_input = client.makefile("rb")
            with self._condition:
                self._client_inputs[client] = client_input
                self._condition.notify_all()
            with (
                client,
                client_input,
                self._compose.exec_stream(
                    project_name=self._project_name,
                    arguments=(
                        "--profile",
                        _RESTORE_COMPOSE_PROFILE,
                        "exec",
                        "-T",
                        _RESTORE_COMPOSE_SERVICE,
                        "bash",
                        "-ceu",
                        bridge_script,
                    ),
                    environment=environment,
                    stdin=client_input,
                ) as container_output,
            ):
                read_output = (
                    container_output.read1
                    if isinstance(container_output, io.BufferedReader)
                    else container_output.read
                )
                while chunk := read_output(64 * 1024):
                    client.sendall(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ComposeCommandError as error:
            if error.classification != "rejected" and not self._stop.is_set():
                self._record_failure(error)
        except Exception as error:
            if not self._stop.is_set():
                self._record_failure(error)
        finally:
            with self._condition:
                self._client_inputs.pop(client, None)
                self._clients.discard(client)
                self._active_handlers.discard(current_thread)
                self._condition.notify_all()

    def _record_failure(self, error: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error


class PostgreSQLBackupCommandBoundary:
    __slots__ = (
        "_clock",
        "_compose",
        "_connect",
        "_denial_connect",
        "_entropy",
        "_fault_hook",
        "_resource_recorder",
        "_resources",
        "_retirement",
        "_retirement_checkpoint",
        "_secrets",
        "_settings",
    )

    def __init__(
        self,
        *,
        settings: PostgreSQLWarehouseSettings,
        compose: DockerComposeProcess,
        resource_recorder: WarehouseResourceRecorder,
        secrets: WarehouseBackupCommandSecretCapability,
        retirement: WarehouseBackupRetirementCapability,
        connect: _Connect,
        clock: Callable[[], datetime],
        denial_connect: _Connect = connect_denial_probe,
        entropy: _Entropy = os.urandom,
        retirement_checkpoint: _RetirementCheckpoint | None = None,
        fault_hook: WarehouseFaultHook = noop_warehouse_fault_hook,
    ) -> None:
        self._settings = settings
        self._compose = compose
        self._resource_recorder = resource_recorder
        self._resources: dict[str, PrivateWarehouseResource] = {}
        self._secrets = secrets
        self._retirement = retirement
        self._retirement_checkpoint = retirement_checkpoint or (lambda _phase: None)
        self._connect = connect
        self._denial_connect = denial_connect
        self._clock = clock
        self._entropy = entropy
        self._fault_hook = fault_hook

    def backup_and_restore(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _PostgreSQLWarehouseIdentity,
        environment: Mapping[str, str],
        target: PostgreSQLConnectionTarget,
        plan: PostgreSQLGrantPlan,
        administration_connection: Any,
    ) -> _BackupLifecycleResult:
        self._load_durable_resources(binding)
        key_resource = _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            provider_resource_handle=self._retirement.resource_handle,
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        self._record_planned(key_resource)
        if self._retirement.is_retired():
            raise RuntimeError("PostgreSQL backup encryption key was already retired")
        self._mark_created(key_resource)
        backup_password = self._secrets.resolve_backup_restore_password().get_secret_value()
        encryption_key = _decode_backup_key(
            self._secrets.resolve_backup_encryption_key().get_secret_value()
        )
        backup_observation: PostgreSQLDatabaseObservation
        with probe_login_scope(
            administration_connection,
            plan,
            {"backup_restore": backup_password},
        ):
            backup_observation = observe_database(
                administration_connection,
                plan,
                connect=self._connect,
                denial_connect=self._denial_connect,
                target=target,
                probe_passwords={"backup_restore": backup_password},
            )
            artifact, artifact_digest = self._create_backup(
                binding=binding,
                operation=operation,
                identity=identity,
                environment=environment,
                plan=plan,
                password=backup_password,
                encryption_key=encryption_key,
            )
        backup_observation = with_probe_login_cleanup_evidence(
            administration_connection,
            plan,
            backup_observation,
        )
        (
            restore_observation,
            restore_cleanup_digest,
            network_isolation_probe_digest,
        ) = self._restore_backup(
            binding=binding,
            operation=operation,
            identity=identity,
            primary_environment=environment,
            artifact=artifact,
            artifact_digest=artifact_digest,
            encryption_key=encryption_key,
            plan=plan,
        )
        _assert_restore_matches_source(backup_observation, restore_observation)
        restore_verification = WarehouseRestoreVerification(
            verification_id="wrv-"
            + digest(
                {
                    "domain": "pillarmesh-postgresql-restore-verification-v1",
                    "operation_id": operation.operation_id,
                }
            )[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            engine_kind=EngineKind.POSTGRESQL,
            source_backup_artifact_digest=artifact_digest,
            representative_data_digest=restore_observation.representative_data_digest,
            schema_metadata_digest=restore_observation.schema_metadata_digest,
            principal_profile_digest=restore_observation.principal_profile_digest,
            integrity_marker_digest=restore_observation.integrity_marker_digest,
            query_behavior_digest=restore_observation.query_behavior_digest,
            verified_at=self._clock(),
        )
        return _BackupLifecycleResult(
            backup_artifact_digest=artifact_digest,
            restore_verification=restore_verification,
            restore_cleanup_digest=restore_cleanup_digest,
            positive_probe_digest=backup_observation.positive_probe_digest,
            denial_probe_digest=backup_observation.denial_probe_digest,
            network_isolation_probe_digest=network_isolation_probe_digest,
        )

    def resources(self) -> tuple[PrivateWarehouseResource, ...]:
        return tuple(self._resources.values())

    def retire_backup(self, *, binding: WarehouseBinding) -> None:
        self._load_durable_resources(binding)
        key = next(
            (
                resource
                for resource in self._resources.values()
                if resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY
            ),
            None,
        )
        backup = next(
            (
                resource
                for resource in self._resources.values()
                if resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
            ),
            None,
        )
        if backup is None and key is None:
            return
        if key is None:
            raise RuntimeError("PostgreSQL backup encryption key resource was not recorded")
        if backup is None:
            identity = _warehouse_identity(binding, self._settings.private_operation_directory)
            artifact_path = identity.binding_directory / "warehouse.pgdump.aead"
            if _lstat_optional_path(artifact_path) is not None:
                raise RuntimeError("PostgreSQL refused an unrecorded backup artifact")
            backup = _related_resource(
                key,
                resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
                provider_resource_handle=str(artifact_path),
            )
            self._record_planned(backup)
        if backup.retention_deadline != key.retention_deadline:
            raise RuntimeError("PostgreSQL backup retention resources disagree")
        journal_path = Path(backup.provider_resource_handle).with_name("backup-retirement.json")
        journal = _related_resource(
            backup,
            resource_kind=WarehouseResourceKind.BACKUP_RETIREMENT_JOURNAL,
            provider_resource_handle=str(journal_path),
        )
        self._record_planned(journal)
        try:
            if _lstat_optional_path(journal_path) is not None:
                if _private_file_is_tombstone(journal_path):
                    _assert_private_file_tombstone(Path(backup.provider_resource_handle))
                    if not self._retirement.is_retired():
                        raise RuntimeError(
                            "PostgreSQL backup retirement tombstone preceded key retirement"
                        )
                    self._mark_created(journal)
                    self._record_cleanup_batch(
                        (backup, key, journal),
                        WarehouseResourceCleanupStatus.COMPLETE,
                        None,
                    )
                    self._retirement_checkpoint("cleanup_batch_durable")
                    return
                phases = _assert_retirement_journal(journal_path)
            else:
                _write_private_file(journal_path, b"intent\n")
                self._retirement_checkpoint("intent_file_durable")
                _fsync_directory(journal_path.parent)
                self._retirement_checkpoint("intent_directory_durable")
                phases = ("intent",)
            self._mark_created(journal)
            self._retirement_checkpoint("intent_resource_durable")

            if self._clock() < backup.retention_deadline:
                self._record_cleanup_batch(
                    (backup, key, journal),
                    WarehouseResourceCleanupStatus.RETAINED,
                    None,
                )
                return

            artifact_path = Path(backup.provider_resource_handle)
            if "artifact_absent" not in phases:
                _delete_recorded_private_file(
                    artifact_path,
                    binding_directory=journal_path.parent,
                )
                self._retirement_checkpoint("artifact_directory_durable")
                _assert_private_file_tombstone(artifact_path)
                _append_retirement_journal(journal_path, "artifact_absent")
                self._retirement_checkpoint("artifact_absence_file_durable")
                _fsync_directory(journal_path.parent)
                self._retirement_checkpoint("artifact_absence_directory_durable")
                phases = ("intent", "artifact_absent")
            _assert_private_file_tombstone(artifact_path)

            if "key_retired" not in phases:
                if not self._retirement.is_retired():
                    self._retirement.retire()
                if not self._retirement.is_retired():
                    raise RuntimeError(
                        "PostgreSQL backup encryption key retirement was not verified"
                    )
                self._retirement_checkpoint("key_retirement_durable")
                _append_retirement_journal(journal_path, "key_retired")
                self._retirement_checkpoint("key_retirement_file_durable")
                _fsync_directory(journal_path.parent)
                self._retirement_checkpoint("key_retirement_directory_durable")
            if not self._retirement.is_retired():
                raise RuntimeError("PostgreSQL backup encryption key retirement was not verified")

            _delete_recorded_private_file(
                journal_path,
                binding_directory=journal_path.parent,
            )
            _assert_private_file_tombstone(journal_path)
            self._retirement_checkpoint("journal_directory_durable")
        except Exception as error:
            if binding.lifecycle_state is WarehouseBindingState.RETIRED:
                raise
            classification = _classification_for_error(error)
            self._record_cleanup_batch(
                (backup, key, journal),
                WarehouseResourceCleanupStatus.FAILED,
                classification,
            )
            return
        self._record_cleanup_batch(
            (backup, key, journal),
            WarehouseResourceCleanupStatus.COMPLETE,
            None,
        )
        self._retirement_checkpoint("cleanup_batch_durable")

    def _create_backup(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _PostgreSQLWarehouseIdentity,
        environment: Mapping[str, str],
        plan: PostgreSQLGrantPlan,
        password: str,
        encryption_key: bytes,
    ) -> tuple[Path, str]:
        artifact = identity.binding_directory / "warehouse.pgdump.aead"
        passfile = identity.binding_directory / "backup.pgpass"
        temporary = identity.binding_directory / "warehouse.pgdump.aead.incomplete"
        resource = _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
            provider_resource_handle=str(artifact),
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        passfile_resource = _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
            provider_resource_handle=str(passfile),
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        staging_resource = _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.BACKUP_STAGING_FILE,
            provider_resource_handle=str(temporary),
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        newly_recorded = tuple(
            planned
            for planned in (resource, passfile_resource, staging_resource)
            if planned.resource_id not in self._resources
        )
        for planned in (resource, passfile_resource, staging_resource):
            self._record_planned(planned)
        if newly_recorded:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED)
        if _lstat_optional_path(artifact) is not None:
            _attempt_all_resource_actions(
                (passfile_resource, staging_resource),
                lambda temporary_resource: self._delete_ephemeral_file(
                    temporary_resource,
                    binding_directory=identity.binding_directory,
                ),
            )
            _assert_private_file(artifact)
            with artifact.open("rb") as source:
                _decrypt_backup_stream(
                    source,
                    _NullWriter(),
                    key=encryption_key,
                    maximum_chunk_size=self._settings.backup_chunk_bytes,
                )
            was_created = (
                self._resources[resource.resource_id].creation_state
                is WarehouseResourceCreationState.CREATED
            )
            self._mark_created(resource)
            if not was_created:
                self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED)
            return artifact, _sha256_file(artifact)
        _attempt_all_resource_actions(
            (passfile_resource, staging_resource),
            lambda temporary_resource: self._prepare_ephemeral_file(
                temporary_resource,
                binding_directory=identity.binding_directory,
            ),
        )
        _write_private_file(
            passfile,
            _pgpass_line(plan.probe_role("backup_restore"), password).encode("utf-8"),
        )
        self._mark_created(passfile_resource)
        primary_failure: BaseException | None = None
        try:
            descriptor = _open_private_file_tombstone_for_rewrite(temporary)
            self._mark_created(staging_resource)
            try:
                with (
                    os.fdopen(descriptor, "wb", closefd=True) as destination,
                    self._compose.exec_stream(
                        project_name=identity.project_name,
                        arguments=_dump_arguments(plan),
                        environment=environment,
                    ) as source,
                ):
                    observation = _encrypt_backup_stream(
                        source,
                        destination,
                        key=encryption_key,
                        chunk_size=self._settings.backup_chunk_bytes,
                        nonce_prefix=self._entropy(4),
                    )
                    destination.flush()
                    os.fsync(destination.fileno())
                if observation.chunk_count < 2:
                    raise RuntimeError("PostgreSQL backup did not cross a streaming chunk boundary")
                os.replace(temporary, artifact)
                _fsync_directory(identity.binding_directory)
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                raise
        except BaseException as error:
            primary_failure = error
        _attempt_all_resource_actions(
            (passfile_resource, staging_resource),
            lambda temporary_resource: self._delete_ephemeral_file(
                temporary_resource,
                binding_directory=identity.binding_directory,
            ),
            primary_failure=primary_failure,
        )
        _assert_private_file(artifact)
        self._mark_created(resource)
        self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED)
        return artifact, _sha256_file(artifact)

    def _delete_ephemeral_file(
        self,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        path = Path(resource.provider_resource_handle)
        _delete_recorded_private_file(path, binding_directory=binding_directory)
        _assert_private_file_tombstone(path)
        current = self._resources[resource.resource_id]
        if current.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE:
            return
        self._record_cleanup(resource, WarehouseResourceCleanupStatus.COMPLETE, None)

    def _prepare_ephemeral_file(
        self,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        path = Path(resource.provider_resource_handle)
        _delete_recorded_private_file(path, binding_directory=binding_directory)
        _assert_private_file_tombstone(path)
        current = self._resources[resource.resource_id]
        if (
            current.cleanup_status is WarehouseResourceCleanupStatus.PENDING
            and current.cleanup_failure_classification is None
        ):
            return
        self._reopen_resources_for_recreation((current,))

    def _restore_backup(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _PostgreSQLWarehouseIdentity,
        primary_environment: Mapping[str, str],
        artifact: Path,
        artifact_digest: str,
        encryption_key: bytes,
        plan: PostgreSQLGrantPlan,
    ) -> tuple[PostgreSQLDatabaseObservation, str, str]:
        restore_identity = _restore_identity(binding, operation)
        restore_password = self._entropy(32).hex()
        tunnel = _RestoreLoopbackTunnel(
            compose=self._compose,
            project_name=restore_identity.project_name,
        )
        environment = _restore_compose_environment(
            identity,
            restore_identity,
            restore_password,
            tunnel.port,
        )
        resources = _planned_restore_resources(
            binding,
            operation,
            restore_identity,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        newly_recorded = tuple(
            resource for resource in resources if resource.resource_id not in self._resources
        )
        for resource in resources:
            self._record_planned(resource)
        if newly_recorded:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_PLAN)
        recorded_resources = tuple(self._resources[resource.resource_id] for resource in resources)
        observation: PostgreSQLDatabaseObservation | None = None
        isolation_before_stream: str | None = None
        isolation_after_validation: str | None = None
        primary_failure: BaseException | None = None
        try:
            _remove_exact_restore_resources(
                self._compose,
                resources=recorded_resources,
                environment=environment,
            )
            _verify_compose_resources_absent(
                self._compose,
                project_name=restore_identity.project_name,
                container_name=restore_identity.container_name,
                network_names=(
                    restore_identity.network_name,
                    restore_identity.loopback_network_name,
                ),
                volume_name=restore_identity.data_volume_name,
                environment=environment,
            )
            recorded_resources = self._reopen_resources_for_recreation(recorded_resources)
            self._compose.exec(
                project_name=restore_identity.project_name,
                arguments=(
                    "--profile",
                    _RESTORE_COMPOSE_PROFILE,
                    "up",
                    "--detach",
                    _RESTORE_COMPOSE_SERVICE,
                ),
                environment=environment,
            )
            _verify_compose_resources(
                self._compose,
                project_name=restore_identity.project_name,
                container_name=restore_identity.container_name,
                network_names=(
                    restore_identity.network_name,
                    restore_identity.loopback_network_name,
                ),
                volume_name=restore_identity.data_volume_name,
                environment=environment,
                published_port_required=False,
            )
            for resource in resources:
                self._mark_created(resource)
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED)
            tunnel.start(environment=environment)
            isolation_before_stream = _observe_restore_isolation(
                self._compose,
                primary=identity,
                restore=restore_identity,
                primary_environment=primary_environment,
                restore_environment=environment,
            )
            target = _connection_target(environment=environment, identity=identity)
            with closing(
                wait_for_tls_connection(
                    self._connect,
                    target,
                    user="postgres",
                    password=restore_password,
                )
            ) as connection:
                connection.autocommit = True
                create_canonical_roles(connection, plan)
            self._stream_restore(
                binding=binding,
                operation=operation,
                project_name=restore_identity.project_name,
                environment=environment,
                identity=identity,
                artifact=artifact,
                encryption_key=encryption_key,
                restore_password=restore_password,
            )
            with closing(
                wait_for_tls_connection(
                    self._connect,
                    target,
                    user="postgres",
                    password=restore_password,
                )
            ) as connection:
                connection.autocommit = True
                prepare_restored_database(connection, plan)
                observation = observe_restored_database(connection, plan)
            tunnel.assert_healthy()
            isolation_after_validation = _observe_restore_isolation(
                self._compose,
                primary=identity,
                restore=restore_identity,
                primary_environment=primary_environment,
                restore_environment=environment,
            )
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_VERIFIED)
        except BaseException as error:
            primary_failure = error
        cleanup_failures: list[BaseException] = []
        try:
            tunnel.close()
        except BaseException as error:
            cleanup_failures.append(error)
        try:
            _remove_exact_restore_resources(
                self._compose,
                resources=recorded_resources,
                environment=environment,
            )
            _verify_compose_resources_absent(
                self._compose,
                project_name=restore_identity.project_name,
                container_name=restore_identity.container_name,
                network_names=(
                    restore_identity.network_name,
                    restore_identity.loopback_network_name,
                ),
                volume_name=restore_identity.data_volume_name,
                environment=environment,
            )
        except BaseException as error:
            cleanup_failures.append(error)
        if not any(not isinstance(error, Exception) for error in cleanup_failures):
            cleanup_error = next(
                (error for error in cleanup_failures if isinstance(error, Exception)),
                None,
            )
            for resource in resources:
                try:
                    if cleanup_error is None:
                        self._record_cleanup(
                            resource,
                            WarehouseResourceCleanupStatus.COMPLETE,
                            None,
                        )
                    else:
                        self._record_cleanup(
                            resource,
                            WarehouseResourceCleanupStatus.FAILED,
                            _classification_for_error(cleanup_error),
                        )
                except BaseException as error:
                    cleanup_failures.append(error)
        if not cleanup_failures:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CLEANUP)
        _raise_primary_or_cleanup_failures(primary_failure, cleanup_failures)
        if observation is None:
            raise RuntimeError("PostgreSQL isolated restore did not produce an observation")
        if isolation_before_stream is None or isolation_after_validation is None:
            raise RuntimeError("PostgreSQL isolated restore did not produce network evidence")
        network_isolation_probe_digest = digest(
            {
                "domain": "pillarmesh-postgresql-restore-window-isolation-v1",
                "before_stream": isolation_before_stream,
                "after_validation": isolation_after_validation,
            }
        )
        restore_resources = tuple(self._resources[resource.resource_id] for resource in resources)
        return (
            observation,
            digest(
                {
                    "domain": "pillarmesh-postgresql-restore-cleanup-v1",
                    "source_backup_artifact_digest": artifact_digest,
                    "resources": tuple(
                        (
                            resource.resource_id,
                            resource.resource_kind,
                            resource.creation_state,
                            resource.cleanup_status,
                        )
                        for resource in restore_resources
                    ),
                }
            ),
            network_isolation_probe_digest,
        )

    def _stream_restore(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        project_name: str,
        environment: Mapping[str, str],
        identity: _PostgreSQLWarehouseIdentity,
        artifact: Path,
        encryption_key: bytes,
        restore_password: str,
    ) -> None:
        passfile = identity.binding_directory / "restore.pgpass"
        passfile_resource = _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
            provider_resource_handle=str(passfile),
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        self._record_planned(passfile_resource)
        self._prepare_ephemeral_file(
            passfile_resource,
            binding_directory=identity.binding_directory,
        )
        _write_private_file(
            passfile,
            _pgpass_line("postgres", restore_password).encode("utf-8"),
        )
        self._mark_created(passfile_resource)
        read_descriptor, write_descriptor = os.pipe()
        failure_lock = threading.Lock()
        failure_order = 0
        decrypt_failure: list[_OrderedRestoreFailure] = []

        def ordered_failure(error: BaseException) -> _OrderedRestoreFailure:
            nonlocal failure_order
            with failure_lock:
                failure_order += 1
                return _OrderedRestoreFailure(order=failure_order, error=error)

        def decrypt() -> None:
            try:
                with (
                    artifact.open("rb") as source,
                    os.fdopen(write_descriptor, "wb", closefd=True) as destination,
                ):
                    try:
                        _decrypt_backup_stream(
                            source,
                            destination,
                            key=encryption_key,
                            maximum_chunk_size=self._settings.backup_chunk_bytes,
                        )
                    except BaseException as error:
                        failure = ordered_failure(error)
                        with failure_lock:
                            decrypt_failure.append(failure)
                        return
            except BaseException as error:
                failure = ordered_failure(error)
                with failure_lock:
                    decrypt_failure.append(failure)
                with suppress(OSError):
                    os.close(write_descriptor)

        worker = threading.Thread(target=decrypt, name="postgresql-restore-decrypt", daemon=True)
        worker.start()
        process_failure: _OrderedRestoreFailure | None = None
        try:
            with (
                os.fdopen(read_descriptor, "rb", closefd=True) as restored_stream,
                self._compose.exec_stream(
                    project_name=project_name,
                    arguments=_restore_arguments(),
                    environment=environment,
                    stdin=restored_stream,
                ) as output,
            ):
                while output.read(self._settings.backup_chunk_bytes):
                    pass
        except BaseException as error:
            process_failure = ordered_failure(error)
        worker.join(timeout=300)
        with failure_lock:
            recorded_decrypt_failure = decrypt_failure[0] if decrypt_failure else None
        primary_failure = _select_restore_stream_failure(
            process_failure,
            recorded_decrypt_failure,
            decrypt_worker_alive=worker.is_alive(),
        )
        cleanup_failures = []
        try:
            self._delete_ephemeral_file(
                passfile_resource,
                binding_directory=identity.binding_directory,
            )
        except BaseException as error:
            cleanup_failures.append(error)
        _raise_primary_or_cleanup_failures(primary_failure, cleanup_failures)

    def _record_planned(self, resource: PrivateWarehouseResource) -> None:
        existing = self._resources.get(resource.resource_id)
        if existing is not None:
            if _planned_resource_identity(existing) != _planned_resource_identity(resource):
                raise RuntimeError("PostgreSQL backup resource identity changed")
            return
        self._resource_recorder.record_planned(resource)
        self._resources[resource.resource_id] = resource

    def _reopen_resources_for_recreation(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> tuple[PrivateWarehouseResource, ...]:
        reopened_at = self._clock()
        expected = tuple(
            resource.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.PLANNED,
                    "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
                    "cleanup_failure_classification": None,
                    "updated_at": reopened_at,
                }
            )
            for resource in resources
        )
        reopened = self._resource_recorder.reopen_resources_for_recreation(
            resources,
            reopened_at=reopened_at,
        )
        if reopened != expected:
            raise RuntimeError("PostgreSQL restore resource reopening was not durable")
        self._resources.update((resource.resource_id, resource) for resource in reopened)
        return reopened

    def _mark_created(self, resource: PrivateWarehouseResource) -> None:
        current = self._resources[resource.resource_id]
        if current.creation_state is WarehouseResourceCreationState.CREATED:
            return
        self._resource_recorder.mark_created(
            resource.tenant_id,
            resource.resource_id,
            resource.provider_resource_handle,
        )
        created = _refresh_recorded_resources(
            self._resource_recorder,
            (current,),
        )
        if created[0].creation_state is not WarehouseResourceCreationState.CREATED:
            raise RuntimeError("PostgreSQL backup resource creation was not durable")
        self._resources[resource.resource_id] = created[0]

    def _record_cleanup(
        self,
        resource: PrivateWarehouseResource,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        self._resource_recorder.record_cleanup(
            resource.tenant_id,
            resource.resource_id,
            status,
            classification,
        )
        current = self._resources[resource.resource_id]
        recorded = _refresh_recorded_resources(
            self._resource_recorder,
            (current,),
        )
        if (
            recorded[0].cleanup_status is not status
            or recorded[0].cleanup_failure_classification is not classification
        ):
            raise RuntimeError("PostgreSQL backup resource cleanup was not durable")
        self._resources[resource.resource_id] = recorded[0]

    def _record_cleanup_batch(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        updated = tuple(
            self._resources[resource.resource_id].model_copy(
                update={
                    "cleanup_status": status,
                    "cleanup_failure_classification": classification,
                    "updated_at": self._clock(),
                }
            )
            for resource in resources
            if (
                self._resources[resource.resource_id].cleanup_status is not status
                or self._resources[resource.resource_id].cleanup_failure_classification
                is not classification
            )
        )
        if not updated:
            return
        self._resource_recorder.record_cleanup_batch(updated)
        recorded = _refresh_recorded_resources(self._resource_recorder, updated)
        if any(
            resource.cleanup_status is not status
            or resource.cleanup_failure_classification is not classification
            for resource in recorded
        ):
            raise RuntimeError("PostgreSQL backup resource cleanup batch was not durable")
        self._resources.update((resource.resource_id, resource) for resource in recorded)

    def _load_durable_resources(self, binding: WarehouseBinding) -> None:
        resources = self._resource_recorder.load_resources(binding.tenant_id, binding.binding_id)
        for resource in resources:
            if resource.resource_kind not in {
                WarehouseResourceKind.BACKUP_ARTIFACT,
                WarehouseResourceKind.BACKUP_STAGING_FILE,
                WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
                WarehouseResourceKind.BACKUP_RETIREMENT_JOURNAL,
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                WarehouseResourceKind.RESTORE_CONTAINER,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_DATA_VOLUME,
            } and not (
                resource.resource_kind is WarehouseResourceKind.CREDENTIAL_FILE
                and Path(resource.provider_resource_handle).name
                in {"backup.pgpass", "restore.pgpass"}
            ):
                continue
            existing = self._resources.get(resource.resource_id)
            if existing is not None and _planned_resource_identity(existing) != (
                _planned_resource_identity(resource)
            ):
                raise RuntimeError("PostgreSQL backup durable resource identity changed")
            self._resources[resource.resource_id] = resource


def evaluate_mvp_fixed_capacity(
    *,
    observed_bytes: int,
    capacity_bytes: int,
    alert_was_open: bool,
) -> MVPFixedCapacityObservation:
    if observed_bytes < 0 or capacity_bytes <= 0:
        raise ValueError("capacity observations must be nonnegative with positive capacity")
    if not alert_was_open and observed_bytes * 100 >= capacity_bytes * 85:
        return MVPFixedCapacityObservation(alert_is_open=True, transition="opened")
    if alert_was_open and observed_bytes * 100 <= capacity_bytes * 75:
        return MVPFixedCapacityObservation(alert_is_open=False, transition="cleared")
    return MVPFixedCapacityObservation(
        alert_is_open=alert_was_open,
        transition="unchanged",
    )


def _assert_restore_matches_source(
    source: PostgreSQLDatabaseObservation,
    restored: PostgreSQLDatabaseObservation,
) -> None:
    comparable_fields = (
        "engine_version",
        "engine_build_digest",
        "principal_profile_digest",
        "namespace_grant_matrix_digest",
        "storage_integrity_probe_digest",
        "representative_data_digest",
        "schema_metadata_digest",
        "integrity_marker_digest",
    )
    if any(getattr(source, field) != getattr(restored, field) for field in comparable_fields):
        raise PostgreSQLBackupIntegrityError


def _encrypt_backup_stream(
    source: IO[bytes],
    destination: IO[bytes],
    *,
    key: bytes,
    chunk_size: int,
    nonce_prefix: bytes,
) -> BackupStreamObservation:
    return encrypt_backup_stream(
        source,
        destination,
        key=key,
        chunk_size=chunk_size,
        nonce_prefix=nonce_prefix,
    )


def _decrypt_backup_stream(
    source: IO[bytes],
    destination: IO[bytes] | _NullWriter,
    *,
    key: bytes,
    maximum_chunk_size: int,
) -> BackupStreamObservation:
    try:
        return decrypt_backup_stream(
            source,
            destination,
            key=key,
            maximum_chunk_size=maximum_chunk_size,
        )
    except BackupStreamIntegrityError:
        raise PostgreSQLBackupIntegrityError from None


class PostgreSQLWarehouseProvider:
    engine_kind = EngineKind.POSTGRESQL

    def __init__(
        self,
        *,
        settings: PostgreSQLWarehouseSettings,
        compose: DockerComposeProcess,
        resource_recorder: WarehouseResourceRecorder,
        administration_secret: WarehouseOperationSecretCapability,
        ingestion_runtime_secret: WarehouseOperationSecretCapability,
        transformation_runtime_secret: WarehouseOperationSecretCapability,
        customer_sql_secret: WarehouseOperationSecretCapability,
        catalog_secret: WarehouseOperationSecretCapability,
        bi_secret: WarehouseOperationSecretCapability,
        tls_private_key_secret: WarehouseOperationSecretCapability,
        tls_certificate_secret: WarehouseOperationSecretCapability,
        backup_commands: object,
        connect: _Connect,
        clock: Callable[[], datetime],
        denial_connect: _Connect = connect_denial_probe,
        entropy: _Entropy = os.urandom,
        fault_hook: WarehouseFaultHook = noop_warehouse_fault_hook,
    ) -> None:
        self._settings = settings
        self._compose = compose
        self._resource_recorder = resource_recorder
        self._administration_secret = administration_secret
        self._ingestion_runtime_secret = ingestion_runtime_secret
        self._transformation_runtime_secret = transformation_runtime_secret
        self._customer_sql_secret = customer_sql_secret
        self._catalog_secret = catalog_secret
        self._bi_secret = bi_secret
        self._tls_private_key_secret = tls_private_key_secret
        self._tls_certificate_secret = tls_certificate_secret
        self._backup_commands = backup_commands
        self._connect = connect
        self._denial_connect = denial_connect
        self._clock = clock
        self._entropy = entropy
        self._fault_hook = fault_hook
        self._primary_resources: dict[str, PrivateWarehouseResource] = {}

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        failure: WarehouseProviderError | None = None
        result: WarehouseProvisionResult | None = None
        try:
            result = self._provision(binding, operation)
        except Exception as error:
            failure = _translate_provider_error("provision", error)
        if failure is not None:
            raise failure from None
        if result is None:
            raise WarehouseProviderError(
                operation="provision",
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        return result

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        failure: WarehouseProviderError | None = None
        result: WarehouseProvisionResult | None = None
        try:
            result = self._reconcile(binding, operation)
        except Exception as error:
            failure = _translate_provider_error("reconcile", error)
        if failure is not None:
            raise failure from None
        if result is None:
            raise WarehouseProviderError(
                operation="reconcile",
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        return result

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult:
        failure: WarehouseProviderError | None = None
        result: WarehouseValidationResult | None = None
        try:
            result = self._validate(binding, operation, resume=resume)
        except Exception as error:
            failure = _translate_provider_error("validate", error)
        if failure is not None:
            raise failure from None
        if result is None:
            raise WarehouseProviderError(
                operation="validate",
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        return result

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        failure: WarehouseProviderError | None = None
        try:
            self._suspend(binding, operation)
        except Exception as error:
            failure = _translate_provider_error("suspend", error)
        if failure is not None:
            raise failure from None

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        failure: WarehouseProviderError | None = None
        try:
            self._resume(binding, operation)
        except Exception as error:
            failure = _translate_provider_error("resume", error)
        if failure is not None:
            raise failure from None

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        failure: WarehouseProviderError | None = None
        result: WarehouseRetirementEvidence | None = None
        try:
            result = self._retire(binding, operation)
        except Exception as error:
            failure = _translate_provider_error("retire", error)
        if failure is not None:
            raise failure from None
        if result is None:
            raise WarehouseProviderError(
                operation="retire",
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        return result

    def _provision(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        _assert_provider_ownership(
            binding, operation, expected_kind=WarehouseOperationKind.PROVISION
        )
        return self._ensure_primary_resources(binding, operation)

    def _ensure_primary_resources(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        expected_resources = _planned_primary_resources(
            binding,
            operation,
            identity,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        resources = _load_owned_resources(
            self._resource_recorder,
            binding,
            operation,
            expected_resources,
        )
        container_absent = self._compose.resource_is_absent(
            resource_kind="container",
            identifier=identity.container_name,
            environment={},
        )
        if container_absent is None:
            raise ComposeCommandError(
                "Docker Compose warehouse inspection was ambiguous",
                classification="ambiguous",
            )
        if not resources and not container_absent:
            raise RuntimeError("PostgreSQL warehouse refused to adopt an unrecorded project")
        if len(resources) != len(expected_resources) and not container_absent:
            raise RuntimeError(
                "PostgreSQL warehouse resources were created before the durable plan completed"
            )
        resources_by_id = {resource.resource_id: resource for resource in resources}
        planned_any = False
        for resource in expected_resources:
            if resource.resource_id in resources_by_id:
                continue
            self._resource_recorder.record_planned(resource)
            resources_by_id[resource.resource_id] = resource
            planned_any = True
        if planned_any:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESOURCE_PLAN)
        resources = tuple(resources_by_id[resource.resource_id] for resource in expected_resources)
        self._primary_resources = {resource.resource_id: resource for resource in resources}
        if container_absent:
            bootstrap_password = self._prepare_private_files(identity)
            environment = _compose_environment(identity, bootstrap_password)
            self._compose.up(project_name=identity.project_name, environment=environment)
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_PROVIDER_CREATE)
        else:
            bootstrap_password = _load_or_create_bootstrap_password(
                identity.credential_file,
                entropy=self._entropy,
            )
        environment = _compose_environment(identity, bootstrap_password)
        target = _connection_target(environment=environment, identity=identity)
        _verify_compose_resources(
            self._compose,
            project_name=identity.project_name,
            container_name=identity.container_name,
            network_names=(identity.network_name, identity.loopback_network_name),
            volume_name=identity.data_volume_name,
            environment=environment,
        )
        requires_database_prepare = container_absent or any(
            resource.creation_state is not WarehouseResourceCreationState.CREATED
            for resource in resources
        )
        if requires_database_prepare:
            administration_password = self._administration_secret.resolve().get_secret_value()
        if container_absent:
            with closing(
                wait_for_tls_connection(
                    self._connect,
                    target,
                    user="postgres",
                    password=bootstrap_password,
                )
            ) as bootstrap_connection:
                bootstrap_connection.autocommit = True
                assert_plaintext_connection_denied(
                    self._connect,
                    target,
                    user="postgres",
                    password=bootstrap_password,
                    denial_connect=self._denial_connect,
                )
                rotate_bootstrap_password(bootstrap_connection, administration_password)
            assert_password_connection_denied(
                self._connect,
                target,
                user="postgres",
                password=bootstrap_password,
                active_password=administration_password,
                denial_connect=self._denial_connect,
            )
        elif requires_database_prepare:
            recovered_connection, bootstrap_is_active = _wait_for_primary_administration(
                self._connect,
                target,
                administration_password=administration_password,
                bootstrap_password=bootstrap_password,
            )
            with closing(recovered_connection):
                assert_plaintext_connection_denied(
                    self._connect,
                    target,
                    user="postgres",
                    password=(
                        bootstrap_password if bootstrap_is_active else administration_password
                    ),
                    denial_connect=self._denial_connect,
                )
                if bootstrap_is_active:
                    rotate_bootstrap_password(
                        recovered_connection,
                        administration_password,
                    )
            assert_password_connection_denied(
                self._connect,
                target,
                user="postgres",
                password=bootstrap_password,
                active_password=administration_password,
                denial_connect=self._denial_connect,
            )
        if requires_database_prepare:
            with closing(
                wait_for_tls_connection(
                    self._connect,
                    target,
                    user="postgres",
                    password=administration_password,
                )
            ) as administration_connection:
                administration_connection.autocommit = True
                prepare_database(
                    administration_connection,
                    derive_grant_plan(identity.private_resource_handle),
                )
            for resource in resources:
                if resource.creation_state is WarehouseResourceCreationState.CREATED:
                    continue
                self._resource_recorder.mark_created(
                    binding.tenant_id,
                    resource.resource_id,
                    resource.provider_resource_handle,
                )
            recorded = _refresh_recorded_resources(self._resource_recorder, resources)
            if any(
                resource.creation_state is not WarehouseResourceCreationState.CREATED
                for resource in recorded
            ):
                raise RuntimeError("PostgreSQL warehouse resource creation was not durable")
            self._primary_resources.update(
                (resource.resource_id, resource) for resource in recorded
            )
        return WarehouseProvisionResult(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=self.engine_kind,
            private_resource_handle=identity.private_resource_handle,
            provider_build_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-warehouse-provider-v1",
                    "engine_kind": self.engine_kind.value,
                }
            ),
            resource_inventory_digest=_stable_resource_inventory_digest(resources),
        )

    def _reconcile(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        if operation.operation_kind is WarehouseOperationKind.PROVISION:
            if not _is_owned_provision_reconciliation(binding, operation):
                raise WarehouseProviderError(
                    operation="reconcile",
                    classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
                )
            return self._ensure_primary_resources(binding, operation)
        _assert_provider_ownership(
            binding,
            operation,
            expected_kind=operation.operation_kind,
        )
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        resources = _load_primary_resources_for_retirement(
            self._resource_recorder,
            binding,
            operation,
            identity,
        )
        environment = _retirement_compose_environment(identity)
        if operation.operation_kind is WarehouseOperationKind.SUSPEND:
            running = self._compose.inspect_container_running(
                identifier=identity.container_name,
                environment=environment,
            )
            if running is None:
                raise ComposeCommandError(
                    "PostgreSQL suspended state could not be reconciled",
                    classification="ambiguous",
                )
            if running:
                self._suspend(binding, operation)
        elif operation.operation_kind is WarehouseOperationKind.RESUME:
            running = self._compose.inspect_container_running(
                identifier=identity.container_name,
                environment=environment,
            )
            if running is None:
                raise ComposeCommandError(
                    "PostgreSQL resumed state could not be reconciled",
                    classification="ambiguous",
                )
            if not running:
                self._resume(binding, operation)
        elif operation.operation_kind is WarehouseOperationKind.RETIRE:
            self._retire(binding, operation)
        return WarehouseProvisionResult(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=self.engine_kind,
            private_resource_handle=identity.private_resource_handle,
            provider_build_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-warehouse-provider-v1",
                    "engine_kind": self.engine_kind.value,
                }
            ),
            resource_inventory_digest=_stable_resource_inventory_digest(resources),
        )

    def _validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult:
        _assert_provider_ownership(
            binding,
            operation,
            expected_kind=(
                WarehouseOperationKind.RESUME if resume else WarehouseOperationKind.PROVISION
            ),
            binding_revision_offset=0 if resume else 1,
        )
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        bootstrap_password = _load_or_create_bootstrap_password(
            identity.credential_file,
            entropy=self._entropy,
        )
        environment = _compose_environment(identity, bootstrap_password)
        target = _connection_target(environment=environment, identity=identity)
        _verify_compose_resources(
            self._compose,
            project_name=identity.project_name,
            container_name=identity.container_name,
            network_names=(identity.network_name, identity.loopback_network_name),
            volume_name=identity.data_volume_name,
            environment=environment,
        )
        plan = derive_grant_plan(identity.private_resource_handle)
        probe_passwords = {
            "administration": self._administration_secret.resolve().get_secret_value(),
            "ingestion_runtime": self._ingestion_runtime_secret.resolve().get_secret_value(),
            "transformation_runtime": (
                self._transformation_runtime_secret.resolve().get_secret_value()
            ),
            "customer_sql": self._customer_sql_secret.resolve().get_secret_value(),
            "catalog": self._catalog_secret.resolve().get_secret_value(),
            "bi": self._bi_secret.resolve().get_secret_value(),
        }
        assert_plaintext_connection_denied(
            self._connect,
            target,
            user="postgres",
            password=probe_passwords["administration"],
            denial_connect=self._denial_connect,
        )
        with closing(
            wait_for_tls_connection(
                self._connect,
                target,
                user="postgres",
                password=probe_passwords["administration"],
            )
        ) as administration_connection:
            administration_connection.autocommit = True
            with probe_login_scope(administration_connection, plan, probe_passwords):
                observation = observe_database(
                    administration_connection,
                    plan,
                    connect=self._connect,
                    denial_connect=self._denial_connect,
                    target=target,
                    probe_passwords=probe_passwords,
                )
            observation = with_probe_login_cleanup_evidence(
                administration_connection,
                plan,
                observation,
            )
            if resume:
                return ResumeWarehouseValidationResult(
                    evidence=self._resume_evidence(binding, operation, observation)
                )
            if not isinstance(self._backup_commands, PostgreSQLBackupCommandBoundary):
                raise RuntimeError("PostgreSQL backup command boundary is unavailable")
            backup = self._backup_commands.backup_and_restore(
                binding=binding,
                operation=operation,
                identity=identity,
                environment=environment,
                target=target,
                plan=plan,
                administration_connection=administration_connection,
            )
        return InitialWarehouseValidationResult(
            evidence=self._initial_evidence(binding, operation, observation, backup),
            restore_verification=backup.restore_verification,
        )

    def _initial_evidence(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        observation: PostgreSQLDatabaseObservation,
        backup: _BackupLifecycleResult,
    ) -> WarehouseValidationEvidence:
        opened = evaluate_mvp_fixed_capacity(
            observed_bytes=85,
            capacity_bytes=100,
            alert_was_open=False,
        )
        cleared = evaluate_mvp_fixed_capacity(
            observed_bytes=74,
            capacity_bytes=100,
            alert_was_open=opened.alert_is_open,
        )
        return WarehouseValidationEvidence(
            evidence_id="wev-"
            + digest(
                {"domain": "postgresql-initial-evidence-v1", "operation_id": operation.operation_id}
            )[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
            engine_kind=EngineKind.POSTGRESQL,
            engine_version=observation.engine_version,
            engine_build_digest=observation.engine_build_digest,
            engine_image_digest=_pinned_image_digest(),
            principal_profile_digest=observation.principal_profile_digest,
            namespace_grant_matrix_digest=observation.namespace_grant_matrix_digest,
            tls_probe_digest=observation.tls_probe_digest,
            network_isolation_probe_digest=backup.network_isolation_probe_digest,
            encryption_at_rest_evidence_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-local-encryption-disposition-v1",
                    "disposition": "deferred_local_acceptance",
                }
            ),
            encryption_at_rest_disposition=(EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
            positive_probe_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-all-positive-probes-v1",
                    "ordinary": observation.positive_probe_digest,
                    "backup": backup.positive_probe_digest,
                }
            ),
            denial_probe_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-all-denial-probes-v1",
                    "ordinary": observation.denial_probe_digest,
                    "backup": backup.denial_probe_digest,
                }
            ),
            ledger_probe_digest=observation.ledger_probe_digest,
            monitoring_probe_digest=observation.monitoring_probe_digest,
            capacity_alert_probe_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-capacity-alert-v1",
                    "opened": {
                        "alert_is_open": opened.alert_is_open,
                        "transition": opened.transition,
                    },
                    "cleared": {
                        "alert_is_open": cleared.alert_is_open,
                        "transition": cleared.transition,
                    },
                }
            ),
            backup_artifact_digest=backup.backup_artifact_digest,
            restore_verification_digest=digest(backup.restore_verification),
            restore_cleanup_digest=backup.restore_cleanup_digest,
            observed_at=self._clock(),
        )

    def _resume_evidence(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        observation: PostgreSQLDatabaseObservation,
    ) -> WarehouseResumeValidationEvidence:
        return WarehouseResumeValidationEvidence(
            evidence_id="wrev-"
            + digest(
                {"domain": "postgresql-resume-evidence-v1", "operation_id": operation.operation_id}
            )[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            engine_kind=EngineKind.POSTGRESQL,
            engine_version=observation.engine_version,
            engine_build_digest=observation.engine_build_digest,
            engine_image_digest=_pinned_image_digest(),
            tls_probe_digest=observation.tls_probe_digest,
            network_isolation_probe_digest=digest(
                {
                    "domain": "pillarmesh-postgresql-resume-network-isolation-v1",
                    "loopback_only": True,
                    "compose_network_internal": True,
                    "plaintext_denied": True,
                }
            ),
            monitoring_probe_digest=observation.monitoring_probe_digest,
            positive_probe_digest=observation.positive_probe_digest,
            denial_probe_digest=observation.denial_probe_digest,
            storage_integrity_probe_digest=observation.storage_integrity_probe_digest,
            observed_at=self._clock(),
        )

    def _suspend(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> None:
        _assert_provider_ownership(binding, operation, expected_kind=WarehouseOperationKind.SUSPEND)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        environment = _compose_environment(
            identity,
            _load_or_create_bootstrap_password(identity.credential_file, entropy=self._entropy),
        )
        self._compose.stop(project_name=identity.project_name, environment=environment)
        absent = self._compose.resource_is_absent(
            resource_kind="volume",
            identifier=identity.data_volume_name,
            environment=environment,
        )
        if absent is not False:
            raise ComposeCommandError(
                "PostgreSQL warehouse data volume could not be verified after suspend",
                classification="ambiguous",
            )

    def _resume(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> None:
        _assert_provider_ownership(binding, operation, expected_kind=WarehouseOperationKind.RESUME)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        environment = _compose_environment(
            identity,
            _load_or_create_bootstrap_password(identity.credential_file, entropy=self._entropy),
        )
        self._compose.start(project_name=identity.project_name, environment=environment)
        target = _connection_target(environment=environment, identity=identity)
        with closing(
            wait_for_tls_connection(
                self._connect,
                target,
                user="postgres",
                password=self._administration_secret.resolve().get_secret_value(),
            )
        ):
            pass

    def _retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        _assert_provider_ownership(binding, operation, expected_kind=WarehouseOperationKind.RETIRE)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        resources = _load_primary_resources_for_retirement(
            self._resource_recorder,
            binding,
            operation,
            identity,
        )
        if not resources:
            raise RuntimeError("PostgreSQL warehouse durable resource inventory is absent")
        self._primary_resources = {resource.resource_id: resource for resource in resources}
        environment = _retirement_compose_environment(identity)
        self._retire_active_resources(binding, identity, environment)
        if isinstance(self._backup_commands, PostgreSQLBackupCommandBoundary):
            self._backup_commands.retire_backup(binding=binding)
        deadline_reached = all(
            self._clock() >= resource.retention_deadline
            for resource in self._primary_resources.values()
        )
        retained_resources = tuple(
            sorted(
                self._primary_resources.values(),
                key=lambda resource: (
                    resource.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY,
                    resource.resource_id,
                ),
            )
        )
        for resource in retained_resources:
            if resource.resource_kind in {
                WarehouseResourceKind.COMPOSE_PROJECT,
                WarehouseResourceKind.WAREHOUSE_CONTAINER,
                WarehouseResourceKind.PRIVATE_NETWORK,
            }:
                continue
            if deadline_reached:
                self._delete_retained_resource(
                    resource,
                    identity,
                    environment,
                    terminal_binding=binding.lifecycle_state is WarehouseBindingState.RETIRED,
                )
            else:
                self._record_primary_cleanup(
                    resource,
                    WarehouseResourceCleanupStatus.RETAINED,
                    None,
                )
        resources = tuple(self._primary_resources.values()) + (
            self._backup_commands.resources()
            if isinstance(self._backup_commands, PostgreSQLBackupCommandBoundary)
            else ()
        )
        snapshot = canonical_retirement_resource_snapshot(resources)
        if snapshot.pending_resource_count:
            raise RuntimeError("PostgreSQL retirement left a resource disposition pending")
        evidence = WarehouseRetirementEvidence(
            evidence_id="wret-"
            + digest(
                {
                    "domain": "postgresql-retirement-evidence-v1",
                    "operation_id": operation.operation_id,
                    "observed_at": self._clock(),
                }
            )[:24],
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
        self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION)
        return evidence

    def _retire_active_resources(
        self,
        binding: WarehouseBinding,
        identity: _PostgreSQLWarehouseIdentity,
        environment: Mapping[str, str],
    ) -> None:
        container_absent = self._compose.resource_is_absent(
            resource_kind="container",
            identifier=identity.container_name,
            environment=environment,
        )
        if container_absent is None:
            raise ComposeCommandError(
                "PostgreSQL warehouse container inspection was ambiguous",
                classification="ambiguous",
            )
        if not container_absent:
            with suppress(ComposeCommandError):
                self._compose.stop(project_name=identity.project_name, environment=environment)
            self._compose.remove_resource(
                resource_kind="container",
                identifier=identity.container_name,
                environment=environment,
            )
        for network_name in (identity.network_name, identity.loopback_network_name):
            network_absent = self._compose.resource_is_absent(
                resource_kind="network",
                identifier=network_name,
                environment=environment,
            )
            if network_absent is None:
                discovered = self._compose.discover_resources(
                    project_name=identity.project_name,
                    environment=environment,
                )
                if not any(resource.resource_kind == "network" for resource in discovered):
                    network_absent = True
            if network_absent is None:
                raise ComposeCommandError(
                    "PostgreSQL warehouse network inspection was ambiguous",
                    classification="ambiguous",
                )
            if not network_absent:
                self._compose.remove_resource(
                    resource_kind="network",
                    identifier=network_name,
                    environment=environment,
                )
        for resource in tuple(self._primary_resources.values()):
            if (
                resource.resource_kind
                in {
                    WarehouseResourceKind.COMPOSE_PROJECT,
                    WarehouseResourceKind.WAREHOUSE_CONTAINER,
                    WarehouseResourceKind.PRIVATE_NETWORK,
                }
                and resource.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE
            ):
                self._record_primary_cleanup(
                    resource,
                    WarehouseResourceCleanupStatus.COMPLETE,
                    None,
                )

    def _delete_retained_resource(
        self,
        resource: PrivateWarehouseResource,
        identity: _PostgreSQLWarehouseIdentity,
        environment: Mapping[str, str],
        *,
        terminal_binding: bool = False,
    ) -> None:
        try:
            if resource.resource_kind is WarehouseResourceKind.WAREHOUSE_DATA_VOLUME:
                self._compose.remove_resource(
                    resource_kind="volume",
                    identifier=resource.provider_resource_handle,
                    environment=environment,
                )
                absent = self._compose.resource_is_absent(
                    resource_kind="volume",
                    identifier=resource.provider_resource_handle,
                    environment=environment,
                )
                if absent is not True:
                    raise RuntimeError("PostgreSQL data volume deletion was not verified")
            elif resource.resource_kind in {
                WarehouseResourceKind.CREDENTIAL_FILE,
                WarehouseResourceKind.HOST_PORT_FILE,
                WarehouseResourceKind.TLS_PRIVATE_KEY,
                WarehouseResourceKind.TLS_CERTIFICATE,
            }:
                _delete_recorded_private_file(
                    Path(resource.provider_resource_handle),
                    binding_directory=identity.binding_directory,
                )
            elif resource.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY:
                directory = Path(resource.provider_resource_handle)
                if directory != identity.binding_directory:
                    raise ValueError("private warehouse directory resource identity changed")
                self._assert_private_directory_tombstone(resource, identity)
        except Exception as error:
            if terminal_binding:
                raise
            self._record_primary_cleanup(
                resource,
                WarehouseResourceCleanupStatus.FAILED,
                _classification_for_error(error),
            )
            return
        self._record_primary_cleanup(resource, WarehouseResourceCleanupStatus.COMPLETE, None)

    def _assert_private_directory_tombstone(
        self,
        directory_resource: PrivateWarehouseResource,
        identity: _PostgreSQLWarehouseIdentity,
    ) -> None:
        directory = Path(directory_resource.provider_resource_handle)
        status_result = _lstat_optional_path(directory)
        if status_result is None:
            return
        if (
            not stat.S_ISDIR(status_result.st_mode)
            or stat.S_ISLNK(status_result.st_mode)
            or stat.S_IMODE(status_result.st_mode) != 0o700
        ):
            raise ValueError("private warehouse directory tombstone is unavailable")
        recorded = self._resource_recorder.load_resources(
            directory_resource.tenant_id,
            directory_resource.binding_id,
        )
        file_kinds = {
            WarehouseResourceKind.CREDENTIAL_FILE,
            WarehouseResourceKind.HOST_PORT_FILE,
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            WarehouseResourceKind.TLS_CERTIFICATE,
            WarehouseResourceKind.BACKUP_ARTIFACT,
            WarehouseResourceKind.BACKUP_STAGING_FILE,
            WarehouseResourceKind.BACKUP_RETIREMENT_JOURNAL,
        }
        expected_paths = {
            Path(resource.provider_resource_handle)
            for resource in recorded
            if resource.parent_resource_handle == identity.private_resource_handle
            and resource.resource_kind in file_kinds
            and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        }
        actual_paths = set(directory.iterdir())
        if actual_paths != expected_paths:
            raise ValueError("private warehouse directory tombstone inventory changed")
        for path in actual_paths:
            _assert_private_file_tombstone(path)

    def _record_primary_cleanup(
        self,
        resource: PrivateWarehouseResource,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        self._resource_recorder.record_cleanup(
            resource.tenant_id,
            resource.resource_id,
            status,
            classification,
        )
        recorded = _refresh_recorded_resources(
            self._resource_recorder,
            (resource,),
        )
        if (
            recorded[0].cleanup_status is not status
            or recorded[0].cleanup_failure_classification is not classification
        ):
            raise RuntimeError("PostgreSQL warehouse resource cleanup was not durable")
        self._primary_resources[resource.resource_id] = recorded[0]

    def _prepare_private_files(self, identity: _PostgreSQLWarehouseIdentity) -> str:
        _assert_private_directory(self._settings.private_operation_directory)
        _ensure_private_directory(identity.binding_directory)
        bootstrap_password = _load_or_create_bootstrap_password(
            identity.credential_file,
            entropy=self._entropy,
        )
        _load_or_create_host_port(identity.host_port_file)
        private_keys = _TLSPrivateKeyBundle.model_validate_json(
            self._tls_private_key_secret.resolve().get_secret_value()
        )
        certificates = _TLSCertificateBundle.model_validate_json(
            self._tls_certificate_secret.resolve().get_secret_value()
        )
        private_files = {
            identity.tls_private_key: private_keys.server_private_key_pem,
            identity.client_private_key: private_keys.client_private_key_pem,
            identity.tls_certificate: certificates.server_certificate_pem,
            identity.client_certificate: certificates.client_certificate_pem,
            identity.root_certificate: certificates.ca_certificate_pem,
            identity.hba_configuration: (
                "local all all trust\n"
                "hostnossl all all 0.0.0.0/0 reject\n"
                "hostnossl all all ::/0 reject\n"
                "hostssl all all 0.0.0.0/0 scram-sha-256 clientcert=verify-ca\n"
                "hostssl all all ::/0 scram-sha-256 clientcert=verify-ca\n"
            ),
        }
        for path, value in private_files.items():
            _write_private_file(path, value.encode("utf-8"))
        return bootstrap_password


def _warehouse_identity(
    binding: WarehouseBinding,
    private_operation_directory: Path,
) -> _PostgreSQLWarehouseIdentity:
    identity_digest = digest(
        {
            "domain": "pillarmesh-postgresql-warehouse-binding-v1",
            "tenant_id": binding.tenant_id,
            "binding_id": binding.binding_id,
        }
    )[:20]
    private_resource_handle = f"pgw-{identity_digest}"
    project_name = f"pm-pg-{identity_digest}"
    binding_directory = private_operation_directory / private_resource_handle
    return _PostgreSQLWarehouseIdentity(
        private_resource_handle=private_resource_handle,
        project_name=project_name,
        container_name=f"{project_name}-database",
        network_name=f"{project_name}-private",
        loopback_network_name=f"{project_name}-loopback",
        data_volume_name=f"{project_name}-data",
        binding_directory=binding_directory,
        credential_file=binding_directory / "postgresql.pgpass",
        tls_private_key=binding_directory / "server.key",
        tls_certificate=binding_directory / "server.crt",
        client_private_key=binding_directory / "client.key",
        client_certificate=binding_directory / "client.crt",
        root_certificate=binding_directory / "ca.crt",
        hba_configuration=binding_directory / "pg_hba.conf",
        host_port_file=binding_directory / "host.port",
    )


def _restore_identity(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
) -> _PostgreSQLRestoreIdentity:
    restore_digest = digest(
        {
            "domain": "pillarmesh-postgresql-restore-project-v1",
            "tenant_id": binding.tenant_id,
            "binding_id": binding.binding_id,
            "operation_id": operation.operation_id,
        }
    )[:20]
    project_name = f"pm-pgr-{restore_digest}"
    return _PostgreSQLRestoreIdentity(
        project_name=project_name,
        container_name=f"{project_name}-database",
        network_name=f"{project_name}-private",
        loopback_network_name=f"{project_name}-loopback",
        data_volume_name=f"{project_name}-data",
    )


def _planned_primary_resources(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _PostgreSQLWarehouseIdentity,
    *,
    retention_deadline: datetime,
) -> tuple[PrivateWarehouseResource, ...]:
    specs = _primary_resource_specs(identity)
    return tuple(
        PrivateWarehouseResource(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            resource_id="wrs-"
            + digest(
                {
                    "domain": "pillarmesh-postgresql-warehouse-resource-v1",
                    "tenant_id": binding.tenant_id,
                    "binding_id": binding.binding_id,
                    "resource_kind": _primary_resource_identity_kind(
                        identity,
                        resource_kind,
                        provider_resource_handle,
                    ),
                    "provider_resource_handle": provider_resource_handle,
                }
            )[:24],
            resource_kind=resource_kind,
            provider_resource_handle=provider_resource_handle,
            parent_resource_handle=parent_resource_handle,
            creation_state=WarehouseResourceCreationState.PLANNED,
            retention_deadline=retention_deadline,
            cleanup_status=WarehouseResourceCleanupStatus.PENDING,
            created_at=operation.started_at,
            updated_at=operation.started_at,
        )
        for resource_kind, provider_resource_handle, parent_resource_handle in specs
    )


def _primary_resource_specs(
    identity: _PostgreSQLWarehouseIdentity,
) -> tuple[tuple[WarehouseResourceKind, str, str | None], ...]:
    return (
        (WarehouseResourceKind.COMPOSE_PROJECT, identity.project_name, None),
        (
            WarehouseResourceKind.WAREHOUSE_CONTAINER,
            identity.container_name,
            identity.project_name,
        ),
        (WarehouseResourceKind.PRIVATE_NETWORK, identity.network_name, identity.project_name),
        (
            WarehouseResourceKind.PRIVATE_NETWORK,
            identity.loopback_network_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
            identity.data_volume_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.PRIVATE_DIRECTORY,
            str(identity.binding_directory),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.CREDENTIAL_FILE,
            str(identity.credential_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.HOST_PORT_FILE,
            str(identity.host_port_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.CREDENTIAL_FILE,
            str(identity.hba_configuration),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            str(identity.tls_private_key),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            str(identity.client_private_key),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.tls_certificate),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.client_certificate),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.root_certificate),
            identity.private_resource_handle,
        ),
    )


def _primary_resource_identity_kind(
    identity: _PostgreSQLWarehouseIdentity,
    resource_kind: WarehouseResourceKind,
    provider_resource_handle: str,
) -> str:
    # Resource IDs predate the removal of the provider-specific shared enum value. Preserve that
    # one identity input across migration while exposing only the approved provider-neutral kind.
    if resource_kind is WarehouseResourceKind.CREDENTIAL_FILE and provider_resource_handle == str(
        identity.hba_configuration
    ):
        return "hba_configuration"
    return resource_kind.value


def _load_owned_resources(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    expected_resources: tuple[PrivateWarehouseResource, ...],
) -> tuple[PrivateWarehouseResource, ...]:
    recorded = recorder.load_resources(binding.tenant_id, binding.binding_id)
    expected_by_id = {resource.resource_id: resource for resource in expected_resources}
    expected_handles = {resource.provider_resource_handle for resource in expected_resources}
    candidates = tuple(
        resource
        for resource in recorded
        if resource.resource_id in expected_by_id
        or resource.provider_resource_handle in expected_handles
    )
    if not candidates:
        return ()
    candidate_by_id = {resource.resource_id: resource for resource in candidates}
    if len(candidate_by_id) != len(candidates):
        raise RuntimeError("PostgreSQL warehouse durable resource identity is ambiguous")
    for current in candidates:
        expected = expected_by_id.get(current.resource_id)
        if expected is None or (
            current.operation_id != operation.operation_id
            or _planned_resource_identity(current) != _planned_resource_identity(expected)
        ):
            raise RuntimeError("PostgreSQL warehouse durable resource ownership changed")
    return tuple(
        candidate_by_id[expected.resource_id]
        for expected in expected_resources
        if expected.resource_id in candidate_by_id
    )


def _load_primary_resources_for_retirement(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _PostgreSQLWarehouseIdentity,
) -> tuple[PrivateWarehouseResource, ...]:
    specs = _primary_resource_specs(identity)
    expected_by_handle = {
        provider_resource_handle: (resource_kind, parent_resource_handle)
        for resource_kind, provider_resource_handle, parent_resource_handle in specs
    }
    candidates = tuple(
        resource
        for resource in recorder.load_resources(binding.tenant_id, binding.binding_id)
        if resource.provider_resource_handle in expected_by_handle
    )
    if not candidates:
        return ()
    candidate_by_handle = {resource.provider_resource_handle: resource for resource in candidates}
    if len(candidates) != len(specs) or len(candidate_by_handle) != len(candidates):
        raise RuntimeError("PostgreSQL warehouse durable resource inventory is incomplete")
    ownership = {
        (
            resource.operation_id,
            resource.binding_revision,
            resource.created_at,
            resource.retention_deadline,
        )
        for resource in candidates
    }
    if len(ownership) != 1:
        raise RuntimeError("PostgreSQL warehouse durable resource ownership changed")
    original_operation_id, binding_revision, _created_at, _retention_deadline = next(
        iter(ownership)
    )
    if (
        original_operation_id == operation.operation_id
        or binding_revision >= operation.binding_revision
    ):
        raise RuntimeError("PostgreSQL warehouse durable resource ownership changed")
    ordered: list[PrivateWarehouseResource] = []
    for resource_kind, provider_resource_handle, parent_resource_handle in specs:
        current = candidate_by_handle[provider_resource_handle]
        expected_resource_id = (
            "wrs-"
            + digest(
                {
                    "domain": "pillarmesh-postgresql-warehouse-resource-v1",
                    "tenant_id": binding.tenant_id,
                    "binding_id": binding.binding_id,
                    "resource_kind": _primary_resource_identity_kind(
                        identity,
                        resource_kind,
                        provider_resource_handle,
                    ),
                    "provider_resource_handle": provider_resource_handle,
                }
            )[:24]
        )
        if (
            current.resource_id != expected_resource_id
            or current.resource_kind is not resource_kind
            or current.parent_resource_handle != parent_resource_handle
            or current.tenant_id != binding.tenant_id
            or current.binding_id != binding.binding_id
        ):
            raise RuntimeError("PostgreSQL warehouse durable resource ownership changed")
        ordered.append(current)
    return tuple(ordered)


def _planned_resource_identity(resource: PrivateWarehouseResource) -> tuple[object, ...]:
    return (
        resource.tenant_id,
        resource.binding_id,
        resource.binding_revision,
        resource.operation_id,
        resource.resource_id,
        resource.resource_kind,
        resource.provider_resource_handle,
        resource.parent_resource_handle,
        resource.retention_deadline,
        resource.created_at,
    )


def _refresh_recorded_resources(
    recorder: WarehouseResourceRecorder,
    expected_resources: tuple[PrivateWarehouseResource, ...],
) -> tuple[PrivateWarehouseResource, ...]:
    if not expected_resources:
        return ()
    tenant_id = expected_resources[0].tenant_id
    binding_id = expected_resources[0].binding_id
    if any(
        resource.tenant_id != tenant_id or resource.binding_id != binding_id
        for resource in expected_resources
    ):
        raise RuntimeError("PostgreSQL warehouse resource refresh crossed an ownership boundary")
    recorded_by_id = {
        resource.resource_id: resource
        for resource in recorder.load_resources(tenant_id, binding_id)
    }
    recorded = []
    for expected in expected_resources:
        current = recorded_by_id.get(expected.resource_id)
        if current is None or _planned_resource_identity(current) != _planned_resource_identity(
            expected
        ):
            raise RuntimeError("PostgreSQL warehouse durable resource identity changed")
        recorded.append(current)
    return tuple(recorded)


def _stable_resource_inventory_digest(
    resources: tuple[PrivateWarehouseResource, ...],
) -> str:
    return digest(
        tuple(
            _planned_resource_identity(resource)
            for resource in sorted(resources, key=lambda item: item.resource_id)
        )
    )


def _compose_environment(
    identity: _PostgreSQLWarehouseIdentity,
    bootstrap_password: str,
) -> Mapping[str, str]:
    return {
        "PILLARMESH_POSTGRES_BOOTSTRAP_PASSWORD": bootstrap_password,
        "PILLARMESH_POSTGRES_CONTAINER_NAME": identity.container_name,
        "PILLARMESH_POSTGRES_NETWORK_NAME": identity.network_name,
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_NAME": identity.loopback_network_name,
        "PILLARMESH_POSTGRES_DATA_VOLUME_NAME": identity.data_volume_name,
        "PILLARMESH_POSTGRES_PRIVATE_DIRECTORY": str(identity.binding_directory),
        "PILLARMESH_POSTGRES_HOST_PORT": str(_load_host_port(identity.host_port_file)),
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_INTERNAL": "false",
        "PILLARMESH_POSTGRES_RESTORE_CONTAINER_NAME": f"{identity.container_name}-inactive",
    }


def _restore_compose_environment(
    primary: _PostgreSQLWarehouseIdentity,
    restore: _PostgreSQLRestoreIdentity,
    bootstrap_password: str,
    host_port: int,
) -> Mapping[str, str]:
    return {
        "PILLARMESH_POSTGRES_BOOTSTRAP_PASSWORD": bootstrap_password,
        "PILLARMESH_POSTGRES_CONTAINER_NAME": f"{restore.container_name}-inactive",
        "PILLARMESH_POSTGRES_NETWORK_NAME": restore.network_name,
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_NAME": restore.loopback_network_name,
        "PILLARMESH_POSTGRES_DATA_VOLUME_NAME": restore.data_volume_name,
        "PILLARMESH_POSTGRES_PRIVATE_DIRECTORY": str(primary.binding_directory),
        "PILLARMESH_POSTGRES_HOST_PORT": str(host_port),
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_INTERNAL": "true",
        "PILLARMESH_POSTGRES_RESTORE_CONTAINER_NAME": restore.container_name,
    }


def _retirement_compose_environment(
    identity: _PostgreSQLWarehouseIdentity,
) -> Mapping[str, str]:
    return {
        "PILLARMESH_POSTGRES_BOOTSTRAP_PASSWORD": "retired",
        "PILLARMESH_POSTGRES_CONTAINER_NAME": identity.container_name,
        "PILLARMESH_POSTGRES_NETWORK_NAME": identity.network_name,
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_NAME": identity.loopback_network_name,
        "PILLARMESH_POSTGRES_DATA_VOLUME_NAME": identity.data_volume_name,
        "PILLARMESH_POSTGRES_PRIVATE_DIRECTORY": str(identity.binding_directory),
        "PILLARMESH_POSTGRES_HOST_PORT": "1",
        "PILLARMESH_POSTGRES_LOOPBACK_NETWORK_INTERNAL": "false",
        "PILLARMESH_POSTGRES_RESTORE_CONTAINER_NAME": f"{identity.container_name}-inactive",
    }


def _planned_restore_resources(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _PostgreSQLRestoreIdentity,
    *,
    retention_deadline: datetime,
) -> tuple[PrivateWarehouseResource, ...]:
    specs = (
        (WarehouseResourceKind.RESTORE_COMPOSE_PROJECT, identity.project_name, None),
        (
            WarehouseResourceKind.RESTORE_CONTAINER,
            identity.container_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            identity.network_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            identity.loopback_network_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
            identity.data_volume_name,
            identity.project_name,
        ),
    )
    return tuple(
        _operation_resource(
            binding,
            operation,
            resource_kind=kind,
            provider_resource_handle=handle,
            parent_resource_handle=parent,
            retention_deadline=retention_deadline,
        )
        for kind, handle, parent in specs
    )


def _operation_resource(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    *,
    resource_kind: WarehouseResourceKind,
    provider_resource_handle: str,
    parent_resource_handle: str | None,
    retention_deadline: datetime,
) -> PrivateWarehouseResource:
    return PrivateWarehouseResource(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=operation.binding_revision,
        operation_id=operation.operation_id,
        resource_id="wrs-"
        + digest(
            {
                "domain": "pillarmesh-postgresql-warehouse-operation-resource-v1",
                "tenant_id": binding.tenant_id,
                "binding_id": binding.binding_id,
                "operation_id": operation.operation_id,
                "resource_kind": resource_kind.value,
                "provider_resource_handle": provider_resource_handle,
            }
        )[:24],
        resource_kind=resource_kind,
        provider_resource_handle=provider_resource_handle,
        parent_resource_handle=parent_resource_handle,
        creation_state=WarehouseResourceCreationState.PLANNED,
        retention_deadline=retention_deadline,
        cleanup_status=WarehouseResourceCleanupStatus.PENDING,
        created_at=operation.started_at,
        updated_at=operation.started_at,
    )


def _related_resource(
    source: PrivateWarehouseResource,
    *,
    resource_kind: WarehouseResourceKind,
    provider_resource_handle: str,
) -> PrivateWarehouseResource:
    return PrivateWarehouseResource(
        tenant_id=source.tenant_id,
        binding_id=source.binding_id,
        binding_revision=source.binding_revision,
        operation_id=source.operation_id,
        resource_id="wrs-"
        + digest(
            {
                "domain": "pillarmesh-postgresql-warehouse-operation-resource-v1",
                "tenant_id": source.tenant_id,
                "binding_id": source.binding_id,
                "operation_id": source.operation_id,
                "resource_kind": resource_kind.value,
                "provider_resource_handle": provider_resource_handle,
            }
        )[:24],
        resource_kind=resource_kind,
        provider_resource_handle=provider_resource_handle,
        parent_resource_handle=source.parent_resource_handle,
        creation_state=WarehouseResourceCreationState.PLANNED,
        retention_deadline=source.retention_deadline,
        cleanup_status=WarehouseResourceCleanupStatus.PENDING,
        created_at=source.created_at,
        updated_at=source.created_at,
    )


def _connection_target(
    *,
    environment: Mapping[str, str],
    identity: _PostgreSQLWarehouseIdentity,
) -> PostgreSQLConnectionTarget:
    try:
        port = int(environment["PILLARMESH_POSTGRES_HOST_PORT"])
    except (KeyError, ValueError):
        raise ComposeCommandError(
            "PostgreSQL warehouse host binding was invalid",
            classification="ambiguous",
        ) from None
    if not 1 <= port <= 65_535:
        raise ComposeCommandError(
            "PostgreSQL warehouse host binding was not loopback-only",
            classification="ambiguous",
        )
    return PostgreSQLConnectionTarget(
        host="127.0.0.1",
        port=port,
        root_certificate=identity.root_certificate,
        client_certificate=identity.client_certificate,
        client_private_key=identity.client_private_key,
    )


def _observe_restore_isolation(
    compose: DockerComposeProcess,
    *,
    primary: _PostgreSQLWarehouseIdentity,
    restore: _PostgreSQLRestoreIdentity,
    primary_environment: Mapping[str, str],
    restore_environment: Mapping[str, str],
) -> str:
    primary_absent = compose.resource_is_absent(
        resource_kind="container",
        identifier=primary.container_name,
        environment=primary_environment,
    )
    if primary_absent is not False:
        raise RuntimeError("PostgreSQL primary was not running during restore isolation proof")
    primary_running = compose.inspect_container_running(
        identifier=primary.container_name,
        environment=primary_environment,
    )
    if primary_running is not True:
        raise RuntimeError("PostgreSQL primary was not running during restore isolation proof")
    primary_networks = compose.inspect_container_networks(
        identifier=primary.container_name,
        environment=primary_environment,
    )
    restore_networks = compose.inspect_container_networks(
        identifier=restore.container_name,
        environment=restore_environment,
    )
    expected_primary_networks = tuple(sorted((primary.network_name, primary.loopback_network_name)))
    expected_restore_networks = tuple(sorted((restore.network_name, restore.loopback_network_name)))
    if primary_networks != expected_primary_networks or restore_networks != (
        expected_restore_networks
    ):
        raise RuntimeError("PostgreSQL restore container network attachment was not isolated")
    if set(primary_networks) & set(restore_networks):
        raise RuntimeError("PostgreSQL restore shared a network with the running primary")
    network_observations = tuple(
        (
            network,
            compose.inspect_network_internal(identifier=network, environment=environment),
        )
        for network, environment in (
            (primary.network_name, primary_environment),
            (primary.loopback_network_name, primary_environment),
            (restore.network_name, restore_environment),
            (restore.loopback_network_name, restore_environment),
        )
    )
    expected_network_observations = (
        (primary.network_name, True),
        (primary.loopback_network_name, False),
        (restore.network_name, True),
        (restore.loopback_network_name, True),
    )
    if network_observations != expected_network_observations:
        raise RuntimeError("PostgreSQL restore network boundary was not controlled")
    route_observations = _observe_restore_route_denial(
        compose,
        primary=primary,
        restore=restore,
        restore_environment=restore_environment,
    )
    return digest(
        {
            "domain": "pillarmesh-postgresql-running-primary-restore-isolation-v4",
            "primary_running": primary_running,
            "primary_networks": primary_networks,
            "restore_networks": restore_networks,
            "internal_restore_networks": (
                restore.network_name,
                restore.loopback_network_name,
            ),
            "route_observations": route_observations,
        }
    )


def _observe_restore_route_denial(
    compose: DockerComposeProcess,
    *,
    primary: _PostgreSQLWarehouseIdentity,
    restore: _PostgreSQLRestoreIdentity,
    restore_environment: Mapping[str, str],
) -> tuple[str, ...]:
    route_script = (
        "host_alias=$(getent ahostsv4 host.docker.internal 2>/dev/null "
        "| awk 'NR == 1 {print $1}' || true)\n"
        'if [ -n "$host_alias" ]; then\n'
        '  case "$host_alias" in 127.*|0.0.0.0) exit 46;; esac\n'
        '  if pg_isready --host "$host_alias" --port "$2" --timeout 2 '
        ">/dev/null 2>&1; then exit 42; fi\n"
        "fi\n"
        'if pg_isready --host "$1" --port 5432 --timeout 2 >/dev/null 2>&1; '
        "then exit 41; fi\n"
        'if pg_isready --host gateway.docker.internal --port "$2" --timeout 2 '
        ">/dev/null 2>&1; then exit 43; fi\n"
        "gateway_hex=$(awk '$2 == \"00000000\" {print $3; exit}' /proc/net/route)\n"
        'if [ -n "$gateway_hex" ]; then\n'
        '  gateway_ip=$(printf "%d.%d.%d.%d" "0x${gateway_hex:6:2}" '
        '"0x${gateway_hex:4:2}" "0x${gateway_hex:2:2}" "0x${gateway_hex:0:2}")\n'
        '  if pg_isready --host "$gateway_ip" --port "$2" --timeout 2 '
        ">/dev/null 2>&1; then exit 44; fi\n"
        "fi\n"
        "printf 'primary_name_route_denied\\nprimary_host_route_denied\\n"
        "primary_gateway_name_route_denied\\nprimary_gateway_ip_route_denied\\n'"
    )
    route_output = compose.exec(
        project_name=restore.project_name,
        arguments=(
            "--profile",
            _RESTORE_COMPOSE_PROFILE,
            "exec",
            "-T",
            _RESTORE_COMPOSE_SERVICE,
            "bash",
            "-ceu",
            route_script,
            "--",
            primary.container_name,
            str(_load_host_port(primary.host_port_file)),
        ),
        environment=restore_environment,
    )
    try:
        route_observations = tuple(route_output.decode("ascii").splitlines())
    except UnicodeError:
        raise RuntimeError("PostgreSQL restore route observation was invalid") from None
    if route_observations != (
        "primary_name_route_denied",
        "primary_host_route_denied",
        "primary_gateway_name_route_denied",
        "primary_gateway_ip_route_denied",
    ):
        raise RuntimeError("PostgreSQL restore route denial was not observed")
    return route_observations


def _verify_compose_resources(
    compose: DockerComposeProcess,
    *,
    project_name: str,
    container_name: str,
    network_names: tuple[str, ...],
    volume_name: str,
    environment: Mapping[str, str],
    published_port_required: bool = True,
) -> None:
    resource_specs: tuple[tuple[_ComposeResourceKind, str], ...] = (
        ("container", container_name),
        *(("network", network_name) for network_name in network_names),
        ("volume", volume_name),
    )
    for resource_kind, identifier in resource_specs:
        absent = compose.resource_is_absent(
            resource_kind=resource_kind,
            identifier=identifier,
            environment=environment,
        )
        if absent is not False:
            raise ComposeCommandError(
                "PostgreSQL warehouse resource inspection was ambiguous",
                classification="ambiguous",
            )
    running = compose.inspect_container_running(
        identifier=container_name,
        environment=environment,
    )
    if running is not True:
        raise ComposeCommandError(
            "PostgreSQL warehouse container was not running",
            classification="ambiguous",
        )
    image = compose.inspect_container_image(identifier=container_name, environment=environment)
    if image != POSTGRESQL_WAREHOUSE_IMAGE:
        raise ComposeCommandError(
            "PostgreSQL warehouse image did not match the pinned digest",
            classification="ambiguous",
        )
    if not published_port_required:
        published_ports = compose.inspect_container_has_published_ports(
            identifier=container_name,
            environment=environment,
        )
        if published_ports is not False:
            raise ComposeCommandError(
                "PostgreSQL isolated restore unexpectedly published a host port",
                classification="ambiguous",
            )
        return
    published_port = compose.exec(
        project_name=project_name,
        arguments=("port", _PRIMARY_COMPOSE_SERVICE, "5432"),
        environment=environment,
    )
    expected_binding = f"127.0.0.1:{environment.get('PILLARMESH_POSTGRES_HOST_PORT', '')}"
    if published_port.decode("utf-8", errors="replace").strip() != expected_binding:
        raise ComposeCommandError(
            "PostgreSQL warehouse host binding was not loopback-only",
            classification="ambiguous",
        )


def _verify_compose_resources_absent(
    compose: DockerComposeProcess,
    *,
    project_name: str,
    container_name: str,
    network_names: tuple[str, ...],
    volume_name: str,
    environment: Mapping[str, str],
) -> None:
    resource_specs: tuple[tuple[_ComposeResourceKind, str], ...] = (
        ("container", container_name),
        *(("network", network_name) for network_name in network_names),
        ("volume", volume_name),
    )
    for resource_kind, identifier in resource_specs:
        absent = compose.resource_is_absent(
            resource_kind=resource_kind,
            identifier=identifier,
            environment=environment,
        )
        if absent is not True:
            raise ComposeCommandError(
                "PostgreSQL restore resource deletion was not verified",
                classification="ambiguous",
            )


def _remove_exact_restore_resources(
    compose: DockerComposeProcess,
    *,
    resources: tuple[PrivateWarehouseResource, ...],
    environment: Mapping[str, str],
) -> None:
    first_failure: Exception | None = None
    resource_specs = _recorded_restore_resource_specs(resources)
    for resource_kind, identifier in resource_specs:
        try:
            absent = compose.resource_is_absent(
                resource_kind=resource_kind,
                identifier=identifier,
                environment=environment,
            )
            if absent is True:
                continue
            if absent is None:
                raise ComposeCommandError(
                    "PostgreSQL restore cleanup inspection was ambiguous",
                    classification="ambiguous",
                )
            compose.remove_resource(
                resource_kind=resource_kind,
                identifier=identifier,
                environment=environment,
            )
        except Exception as error:
            if first_failure is None:
                first_failure = error
    if first_failure is not None:
        raise first_failure


def _recorded_restore_resource_specs(
    resources: tuple[PrivateWarehouseResource, ...],
) -> tuple[tuple[_ComposeResourceKind, str], ...]:
    project_resources = tuple(
        resource
        for resource in resources
        if resource.resource_kind is WarehouseResourceKind.RESTORE_COMPOSE_PROJECT
    )
    if len(resources) != 5 or len(project_resources) != 1:
        raise RuntimeError("PostgreSQL restore cleanup authority was incomplete")
    project_handle = project_resources[0].provider_resource_handle
    kind_mapping: dict[WarehouseResourceKind, _ComposeResourceKind] = {
        WarehouseResourceKind.RESTORE_CONTAINER: "container",
        WarehouseResourceKind.RESTORE_PRIVATE_NETWORK: "network",
        WarehouseResourceKind.RESTORE_DATA_VOLUME: "volume",
    }
    resource_specs: list[tuple[_ComposeResourceKind, str]] = []
    for resource in resources:
        compose_kind = kind_mapping.get(resource.resource_kind)
        if compose_kind is None:
            if resource.resource_kind is WarehouseResourceKind.RESTORE_COMPOSE_PROJECT:
                continue
            raise RuntimeError("PostgreSQL restore cleanup authority was invalid")
        if resource.parent_resource_handle != project_handle:
            raise RuntimeError("PostgreSQL restore cleanup authority changed")
        resource_specs.append((compose_kind, resource.provider_resource_handle))
    if (
        tuple(resource_kind for resource_kind, _identifier in resource_specs).count("container")
        != 1
    ):
        raise RuntimeError("PostgreSQL restore container cleanup authority was incomplete")
    if tuple(resource_kind for resource_kind, _identifier in resource_specs).count("network") != 2:
        raise RuntimeError("PostgreSQL restore network cleanup authority was incomplete")
    if tuple(resource_kind for resource_kind, _identifier in resource_specs).count("volume") != 1:
        raise RuntimeError("PostgreSQL restore volume cleanup authority was incomplete")
    if len({identifier for _resource_kind, identifier in resource_specs}) != 4:
        raise RuntimeError("PostgreSQL restore cleanup authority was ambiguous")
    return tuple(resource_specs)


def _dump_arguments(plan: PostgreSQLGrantPlan) -> tuple[str, ...]:
    return (
        "exec",
        "--no-TTY",
        "--env",
        "PGPASSFILE=/pillarmesh-private/backup.pgpass",
        "--env",
        "PGSSLMODE=verify-full",
        "--env",
        "PGSSLROOTCERT=/pillarmesh-private/ca.crt",
        "--env",
        "PGSSLCERT=/pillarmesh-private/client.crt",
        "--env",
        "PGSSLKEY=/pillarmesh-private/client.key",
        _PRIMARY_COMPOSE_SERVICE,
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--host=127.0.0.1",
        "--port=5432",
        "--dbname=pillarmesh_warehouse",
        f"--username={plan.probe_role('backup_restore')}",
    )


def _restore_arguments() -> tuple[str, ...]:
    return (
        "--profile",
        _RESTORE_COMPOSE_PROFILE,
        "exec",
        "--no-TTY",
        "--env",
        "PGPASSFILE=/pillarmesh-private/restore.pgpass",
        "--env",
        "PGSSLMODE=verify-full",
        "--env",
        "PGSSLROOTCERT=/pillarmesh-private/ca.crt",
        "--env",
        "PGSSLCERT=/pillarmesh-private/client.crt",
        "--env",
        "PGSSLKEY=/pillarmesh-private/client.key",
        _RESTORE_COMPOSE_SERVICE,
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "--no-privileges",
        "--host=127.0.0.1",
        "--port=5432",
        "--dbname=pillarmesh_warehouse",
        "--username=postgres",
    )


def _pgpass_line(user: str, password: str) -> str:
    escaped_password = password.replace("\\", "\\\\").replace(":", "\\:")
    return f"127.0.0.1:5432:pillarmesh_warehouse:{user}:{escaped_password}\n"


def _decode_backup_key(encoded: str) -> bytes:
    try:
        value = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeError, ValueError):
        raise ValueError("PostgreSQL backup encryption key is invalid") from None
    if len(value) != 32:
        raise ValueError("PostgreSQL backup encryption key is invalid")
    return value


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pinned_image_digest() -> str:
    prefix = "@sha256:"
    if prefix not in POSTGRESQL_WAREHOUSE_IMAGE:
        raise ValueError("PostgreSQL warehouse image is not digest-pinned")
    value = POSTGRESQL_WAREHOUSE_IMAGE.rsplit(prefix, 1)[1]
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("PostgreSQL warehouse image digest is invalid")
    return value


def _assert_provider_ownership(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    *,
    expected_kind: WarehouseOperationKind,
    binding_revision_offset: int = 0,
) -> None:
    if (
        binding.tenant_id != operation.tenant_id
        or binding.binding_id != operation.binding_id
        or binding.revision != operation.binding_revision + binding_revision_offset
        or binding.engine_kind is not EngineKind.POSTGRESQL
        or operation.engine_kind is not EngineKind.POSTGRESQL
        or operation.operation_kind is not expected_kind
    ):
        raise WarehouseProviderError(
            operation=expected_kind.value,
            classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        )


def _is_owned_provision_reconciliation(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
) -> bool:
    expected_revision = {
        WarehouseBindingState.PROVISIONING: operation.binding_revision,
        WarehouseBindingState.VALIDATING: operation.binding_revision + 1,
        WarehouseBindingState.READY: operation.binding_revision + 2,
    }.get(binding.lifecycle_state)
    return (
        binding.tenant_id == operation.tenant_id
        and binding.binding_id == operation.binding_id
        and binding.revision == expected_revision
        and binding.engine_kind is EngineKind.POSTGRESQL
        and operation.engine_kind is EngineKind.POSTGRESQL
        and operation.operation_kind is WarehouseOperationKind.PROVISION
    )


def _assert_private_directory(directory: Path) -> None:
    status_result = directory.lstat()
    if (
        not stat.S_ISDIR(status_result.st_mode)
        or stat.S_ISLNK(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o700
    ):
        raise ValueError("private operation directory is unavailable")


def _ensure_private_directory(directory: Path) -> None:
    with suppress(FileExistsError):
        directory.mkdir(mode=0o700)
    _assert_private_directory(directory)


def _load_or_create_bootstrap_password(path: Path, *, entropy: _Entropy) -> str:
    try:
        value = path.read_text(encoding="ascii")
    except FileNotFoundError:
        value = entropy(32).hex()
        _write_private_file(path, value.encode("ascii"))
    if not value or any(character.isspace() for character in value):
        raise ValueError("private credential file is invalid")
    _assert_private_file(path)
    return value


def _load_or_create_host_port(path: Path) -> int:
    try:
        return _load_host_port(path)
    except FileNotFoundError:
        port = _allocate_loopback_port()
        _write_private_file(path, f"{port}\n".encode("ascii"))
        return _load_host_port(path)


def _load_host_port(path: Path) -> int:
    _assert_private_file(path)
    try:
        port = int(path.read_text(encoding="ascii").strip())
    except (UnicodeError, ValueError):
        raise ValueError("private PostgreSQL host port is invalid") from None
    if not 1 <= port <= 65_535:
        raise ValueError("private PostgreSQL host port is invalid")
    return port


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if not isinstance(port, int) or not 1 <= port <= 65_535:
        raise RuntimeError("loopback host port allocation failed")
    return port


def _wait_for_primary_administration(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    administration_password: str,
    bootstrap_password: str,
    timeout_seconds: float = 120,
) -> tuple[Any, bool]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        transport_failure: psycopg.OperationalError | None = None
        authorization_failures: list[psycopg.OperationalError] = []
        for password, bootstrap_is_active in (
            (administration_password, False),
            (bootstrap_password, True),
        ):
            try:
                connection = connect_tls(
                    connect,
                    target,
                    user="postgres",
                    password=password,
                )
            except (
                psycopg.errors.InvalidPassword,
                psycopg.errors.InvalidAuthorizationSpecification,
            ) as error:
                authorization_failures.append(error)
            except psycopg.OperationalError as error:
                transport_failure = error
            else:
                connection.autocommit = True
                return connection, bootstrap_is_active
        if transport_failure is None and authorization_failures:
            raise authorization_failures[0]
        if time.monotonic() >= deadline:
            if transport_failure is not None:
                raise transport_failure
            if authorization_failures:
                raise authorization_failures[0]
            raise RuntimeError("PostgreSQL administration recovery produced no outcome")
        time.sleep(0.25)


def _write_private_file(path: Path, value: bytes) -> None:
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        initial_status = os.lstat(path)
        _validate_private_file_status(initial_status)
        descriptor = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    else:
        initial_status = os.fstat(descriptor)
        created = True
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = os.lstat(path)
        _validate_private_file_status(descriptor_status)
        _validate_private_file_status(name_status)
        if not (
            _same_inode(initial_status, descriptor_status)
            and _same_inode(descriptor_status, name_status)
        ):
            raise ValueError("private warehouse file identity changed across replay")
        if not created:
            existing = bytearray()
            os.lseek(descriptor, 0, os.SEEK_SET)
            while chunk := os.read(descriptor, 64 * 1024):
                existing.extend(chunk)
            if existing:
                if bytes(existing) != value:
                    raise ValueError("private warehouse file content changed across replay")
                return
            os.lseek(descriptor, 0, os.SEEK_SET)
        payload = memoryview(value)
        while payload:
            written = os.write(descriptor, payload)
            if written <= 0:
                raise OSError("private warehouse file write failed")
            payload = payload[written:]
        os.fsync(descriptor)
        final_status = os.lstat(path)
        _validate_private_file_status(final_status)
        if not _same_inode(descriptor_status, final_status):
            raise ValueError("private warehouse file identity changed across replay")
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    _assert_private_file(path)


def _open_private_file_tombstone_for_rewrite(path: Path) -> int:
    initial_status = os.lstat(path)
    _validate_private_file_status(initial_status)
    if initial_status.st_size != 0:
        raise ValueError("private warehouse file tombstone is unavailable")
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = os.lstat(path)
        _validate_private_file_status(descriptor_status)
        _validate_private_file_status(name_status)
        if not (
            _same_inode(initial_status, descriptor_status)
            and _same_inode(descriptor_status, name_status)
            and descriptor_status.st_size == 0
        ):
            raise ValueError("private warehouse file tombstone identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _assert_private_file(path: Path) -> None:
    status_result = os.lstat(path)
    _validate_private_file_status(status_result)


def _assert_private_file_tombstone(path: Path) -> None:
    status_result = os.lstat(path)
    _validate_private_file_status(status_result)
    if status_result.st_size != 0:
        raise RuntimeError("private warehouse file erasure was not verified")


def _private_file_is_tombstone(path: Path) -> bool:
    status_result = _lstat_optional_path(path)
    if status_result is None:
        return False
    _validate_private_file_status(status_result)
    return status_result.st_size == 0


def _validate_private_file_status(status_result: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status_result.st_mode)
        or stat.S_ISLNK(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o600
        or status_result.st_nlink != 1
    ):
        raise ValueError("private warehouse file is unavailable")


def _lstat_optional_path(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _lstat_optional_at(directory_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _assert_retirement_journal(path: Path) -> tuple[str, ...]:
    _assert_private_file(path)
    try:
        phases = tuple(path.read_text(encoding="ascii").splitlines())
    except UnicodeError:
        raise ValueError("private warehouse retirement journal is invalid") from None
    valid_prefixes = (
        ("intent",),
        ("intent", "artifact_absent"),
        ("intent", "artifact_absent", "key_retired"),
    )
    if phases not in valid_prefixes:
        raise ValueError("private warehouse retirement journal is invalid")
    return phases


def _append_retirement_journal(
    path: Path,
    phase: Literal["artifact_absent", "key_retired"],
) -> None:
    phases = _assert_retirement_journal(path)
    expected = ("intent",) if phase == "artifact_absent" else ("intent", "artifact_absent")
    if phases != expected:
        raise ValueError("private warehouse retirement journal is invalid")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = path.lstat()
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or stat.S_IMODE(descriptor_status.st_mode) != 0o600
            or descriptor_status.st_nlink != 1
            or (descriptor_status.st_dev, descriptor_status.st_ino)
            != (name_status.st_dev, name_status.st_ino)
        ):
            raise ValueError("private warehouse retirement journal is unavailable")
        payload = memoryview(f"{phase}\n".encode("ascii"))
        while payload:
            written = os.write(descriptor, payload)
            if written <= 0:
                raise OSError("private warehouse retirement journal write failed")
            payload = payload[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    _assert_retirement_journal(path)


def _delete_recorded_private_file(
    path: Path,
    *,
    binding_directory: Path,
) -> None:
    if path.parent != binding_directory or path.name in {"", ".", ".."}:
        raise ValueError("private warehouse file resource identity changed")
    directory_descriptor = os.open(
        binding_directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        directory_status = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_IMODE(directory_status.st_mode) != 0o700
            or directory_status.st_uid != os.geteuid()
        ):
            raise ValueError("private warehouse directory is unavailable")
        initial_status = _lstat_optional_at(directory_descriptor, path.name)
        if initial_status is None:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                raise ValueError("private warehouse file resource identity changed") from None
            initial_status = os.fstat(descriptor)
        else:
            _validate_private_file_status(initial_status)
            descriptor = os.open(
                path.name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_descriptor,
            )
        try:
            descriptor_status = os.fstat(descriptor)
            _validate_private_file_status(descriptor_status)
            current_status = _lstat_optional_at(directory_descriptor, path.name)
            if current_status is None:
                raise ValueError("private warehouse file resource identity changed")
            _validate_private_file_status(current_status)
            if not (
                _same_inode(initial_status, descriptor_status)
                and _same_inode(descriptor_status, current_status)
            ):
                raise ValueError("private warehouse file resource identity changed")
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            erased_status = os.fstat(descriptor)
            _validate_private_file_status(erased_status)
            named_erased_status = _lstat_optional_at(directory_descriptor, path.name)
            if named_erased_status is None:
                raise ValueError("private warehouse file resource identity changed")
            _validate_private_file_status(named_erased_status)
            if (
                not _same_inode(descriptor_status, erased_status)
                or not _same_inode(erased_status, named_erased_status)
                or erased_status.st_size != 0
            ):
                raise ValueError("private warehouse file resource identity changed")
            final_named_status = _lstat_optional_at(directory_descriptor, path.name)
            if final_named_status is None:
                raise ValueError("private warehouse file resource identity changed")
            _validate_private_file_status(final_named_status)
            if not _same_inode(erased_status, final_named_status):
                raise ValueError("private warehouse file resource identity changed")
            os.fsync(directory_descriptor)
        finally:
            os.close(descriptor)
        final_status = _lstat_optional_at(directory_descriptor, path.name)
        if final_status is None:
            raise RuntimeError("private warehouse file tombstone was not durable")
        _validate_private_file_status(final_status)
        if final_status.st_size != 0:
            raise RuntimeError("private warehouse file erasure was not verified")
    finally:
        os.close(directory_descriptor)


def _classification_for_error(error: Exception) -> WarehouseFailureClassification:
    if isinstance(error, WarehouseProviderError):
        return error.classification
    if isinstance(error, ComposeCommandError):
        return {
            "unavailable": WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
            "timeout": WarehouseFailureClassification.TRANSIENT_TRANSPORT,
            "rejected": WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
            "ambiguous": WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
            "output_limit": WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        }[error.classification]
    if isinstance(
        error,
        (
            psycopg.errors.InvalidPassword,
            psycopg.errors.InvalidAuthorizationSpecification,
        ),
    ):
        return WarehouseFailureClassification.AUTHORIZATION_DENIED
    if isinstance(error, psycopg.errors.InsufficientPrivilege):
        return WarehouseFailureClassification.AUTHORIZATION_DENIED
    if isinstance(error, (psycopg.IntegrityError, PostgreSQLBackupIntegrityError)):
        return WarehouseFailureClassification.INTEGRITY_FAILURE
    if isinstance(error, psycopg.OperationalError):
        return WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    if isinstance(error, psycopg.DatabaseError):
        return WarehouseFailureClassification.STATEMENT_REJECTED
    return WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE


def _translate_provider_error(
    operation: Literal["provision", "reconcile", "validate", "suspend", "resume", "retire"],
    error: Exception,
) -> WarehouseProviderError:
    classification = _classification_for_error(error)
    return WarehouseProviderError(operation=operation, classification=classification)


__all__ = [
    "MVPFixedCapacityObservation",
    "PostgreSQLBackupCommandBoundary",
    "PostgreSQLBackupIntegrityError",
    "PostgreSQLWarehouseProvider",
    "evaluate_mvp_fixed_capacity",
]
