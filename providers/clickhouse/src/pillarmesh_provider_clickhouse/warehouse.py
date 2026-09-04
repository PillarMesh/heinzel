from __future__ import annotations

import hmac
import io
import os
import socket
import ssl
import stat
import sys
import threading
import time
from base64 import b64decode
from binascii import Error as Base64Error
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import IO, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    BackupStreamIntegrityError,
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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .settings import (
    CLICKHOUSE_SERVER_VERSION,
    CLICKHOUSE_WAREHOUSE_IMAGE,
    ClickHouseWarehouseSettings,
)

type _Entropy = Callable[[int], bytes]


class _Client(Protocol):
    """`operation` is the closed provider vocabulary, not `str`.

    Declaring it `str` made this protocol wider than every implementation, so no
    client actually satisfied it and a caller could name an operation that the
    resulting `WarehouseProviderError` cannot carry.
    """

    def execute(self, statement: str, *, operation: _ClickHouseOperation = "validate") -> bytes: ...

    def query_lines(
        self, statement: str, *, operation: _ClickHouseOperation = "validate"
    ) -> tuple[str, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ClickHouseWarehouseIdentity:
    private_resource_handle: str
    project_name: str
    container_name: str
    private_network_name: str
    loopback_network_name: str
    data_volume_name: str
    binding_directory: Path
    bootstrap_credential_file: Path
    bootstrap_users_file: Path
    host_port_file: Path
    server_private_key_file: Path
    server_certificate_file: Path
    client_private_key_file: Path
    client_certificate_file: Path
    root_certificate_file: Path


@dataclass(frozen=True, slots=True)
class _ClickHouseRestoreIdentity:
    project_name: str
    container_name: str
    isolation_probe_container_name: str
    private_network_name: str
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


class _ClickHouseRestoreReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    verification: WarehouseRestoreVerification
    network_isolation_probe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    integrity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _restore_receipt_integrity(
    verification: WarehouseRestoreVerification,
    network_isolation_probe_digest: str,
    *,
    key: bytes,
) -> str:
    payload = {
        "schema_version": "2",
        "verification": verification.model_dump(mode="json"),
        "network_isolation_probe_digest": network_isolation_probe_digest,
    }
    return hmac.new(key, canonical_bytes(payload), sha256).hexdigest()


class _RestoreLoopbackTunnel:
    def __init__(self, *, compose: DockerComposeProcess, project_name: str) -> None:
        self._compose = compose
        self._project_name = project_name
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if not isinstance(port, int) or not 1 <= port <= 65_535:
                raise OSError("loopback reservation returned an invalid port")
        except BaseException as error:
            if listener is not None:
                with suppress(BaseException):
                    listener.close()
            if isinstance(error, Exception):
                raise ComposeCommandError(
                    "ClickHouse restore tunnel was unavailable",
                    classification="unavailable",
                ) from None
            raise
        self._listener: socket.socket | None = listener
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
        self.port = port

    def start(self, *, environment: Mapping[str, str]) -> None:
        if self._server_thread is not None or self._stop.is_set():
            raise RuntimeError("ClickHouse restore loopback tunnel was already started")
        listener = self._listener
        if listener is None:
            raise ComposeCommandError(
                "ClickHouse restore tunnel was unavailable",
                classification="unavailable",
            )
        try:
            listener.listen()
            listener.settimeout(0.1)
        except BaseException as error:
            self._stop.set()
            with suppress(BaseException):
                listener.close()
            self._listener = None
            if isinstance(error, Exception):
                raise ComposeCommandError(
                    "ClickHouse restore tunnel was unavailable",
                    classification="unavailable",
                ) from None
            raise
        self._environment = dict(environment)
        with self._condition:
            self._server_stopped = False
        self._server_thread = threading.Thread(
            target=self._serve,
            name="pillarmesh-clickhouse-restore-tunnel",
            daemon=True,
        )
        try:
            self._server_thread.start()
        except BaseException as error:
            self._stop.set()
            with self._condition:
                self._server_stopped = True
                self._condition.notify_all()
            with suppress(BaseException):
                listener.close()
            self._listener = None
            self._server_thread = None
            if isinstance(error, Exception):
                raise ComposeCommandError(
                    "ClickHouse restore tunnel was unavailable",
                    classification="unavailable",
                ) from None
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
                self._record_failure(RuntimeError("ClickHouse restore tunnel had no listener"))
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
                    name="pillarmesh-clickhouse-restore-connection",
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
                    RuntimeError("ClickHouse restore tunnel started without scope")
                )
                return
            bridge_script = (
                "exec 3<>/dev/tcp/127.0.0.1/8443\n"
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
                        "restore",
                        "exec",
                        "-T",
                        "clickhouse_restore",
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


@dataclass(frozen=True, slots=True)
class ClickHouseBackupLifecycleResult:
    backup_artifact_digest: str
    restore_verification: WarehouseRestoreVerification
    restore_cleanup_digest: str
    positive_probe_digest: str
    denial_probe_digest: str
    network_isolation_probe_digest: str


@dataclass(frozen=True, slots=True)
class _ClickHouseDatabaseObservation:
    engine_version: str
    engine_build_digest: str
    principal_profile_digest: str
    namespace_grant_matrix_digest: str
    tls_probe_digest: str
    positive_probe_digest: str
    denial_probe_digest: str
    ledger_probe_digest: str
    monitoring_probe_digest: str
    storage_integrity_probe_digest: str


@dataclass(frozen=True, slots=True)
class _ClickHouseGrantProbeObservation:
    positive: tuple[tuple[str, str], ...]
    denied: tuple[tuple[str, str], ...]


class _BackupCommands(Protocol):
    def prepare_principal(self, client: _Client, plan: ClickHouseGrantPlan) -> None: ...

    def backup_and_restore(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
        plan: ClickHouseGrantPlan,
    ) -> ClickHouseBackupLifecycleResult: ...

    def resources(self) -> tuple[PrivateWarehouseResource, ...]: ...

    def retire_backup(self, *, binding: WarehouseBinding) -> None: ...


class ClickHouseBackupCommandBoundary:
    def __init__(
        self,
        *,
        settings: ClickHouseWarehouseSettings,
        compose: DockerComposeProcess,
        resource_recorder: WarehouseResourceRecorder,
        secret_capability: WarehouseBackupCommandSecretCapability,
        retirement_capability: WarehouseBackupRetirementCapability,
        client_factory: Callable[[ClickHouseConnectionTarget], _Client],
        clock: Callable[[], datetime],
        entropy: _Entropy = os.urandom,
        fault_hook: WarehouseFaultHook = noop_warehouse_fault_hook,
    ) -> None:
        self._settings = settings
        self._compose = compose
        self._resource_recorder = resource_recorder
        self._secret_capability = secret_capability
        self._retirement_capability = retirement_capability
        self._client_factory = client_factory
        self._clock = clock
        self._entropy = entropy
        self._fault_hook = fault_hook
        self._resource_ids: set[str] = set()
        self._binding: WarehouseBinding | None = None

    def prepare_principal(self, client: _Client, plan: ClickHouseGrantPlan) -> None:
        password = self._secret_capability.resolve_backup_restore_password().get_secret_value()
        for statement in (
            f"CREATE USER IF NOT EXISTS `{plan.users['backup_restore']}` "
            f"IDENTIFIED WITH sha256_password BY {_sql_string_literal(password)}",
            f"GRANT `{plan.roles['backup_restore']}` TO `{plan.users['backup_restore']}`",
        ):
            client.execute(statement, operation="provision")

    def backup_and_restore(
        self,
        *,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
        plan: ClickHouseGrantPlan,
    ) -> ClickHouseBackupLifecycleResult:
        resources = _planned_backup_resources(
            binding,
            operation,
            identity,
            self._retirement_capability.resource_handle,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        existing_resource_ids = {
            resource.resource_id
            for resource in self._resource_recorder.load_resources(
                binding.tenant_id, binding.binding_id
            )
        }
        current = _record_backup_plan(
            self._resource_recorder,
            binding,
            operation,
            resources,
        )
        self._resource_ids = {resource.resource_id for resource in current}
        self._binding = binding
        newly_recorded = tuple(
            resource for resource in current if resource.resource_id not in existing_resource_ids
        )
        if any(
            resource.resource_kind
            in {
                WarehouseResourceKind.BACKUP_ARTIFACT,
                WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            }
            for resource in newly_recorded
        ):
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED)
        artifact = next(
            resource
            for resource in current
            if resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
        )
        staging = next(
            resource
            for resource in current
            if Path(resource.provider_resource_handle).name == "clickhouse-restore.zip"
        )
        artifact_staging = next(
            resource
            for resource in current
            if Path(resource.provider_resource_handle).name == "clickhouse-backup.pmwhbk.incomplete"
        )
        artifact_path = Path(artifact.provider_resource_handle)
        staging_path = Path(staging.provider_resource_handle)
        artifact_staging_path = Path(artifact_staging.provider_resource_handle)
        backup_grants = self._probe_backup_principal(identity, plan)
        if artifact.creation_state is not WarehouseResourceCreationState.CREATED:
            if _private_file_exists(artifact_path):
                _assert_private_file(artifact_path)
                self._remove_native_backup(identity, environment)
            else:
                self._create_encrypted_backup(
                    identity,
                    environment,
                    plan,
                    artifact_path,
                    artifact_staging_path,
                )
            for resource in current:
                if resource.resource_kind in {
                    WarehouseResourceKind.BACKUP_ARTIFACT,
                    WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
                }:
                    self._resource_recorder.mark_created(
                        binding.tenant_id,
                        resource.resource_id,
                        resource.provider_resource_handle,
                    )
            _erase_private_file(artifact_staging_path, parent=identity.binding_directory)
            _record_cleanup(
                self._resource_recorder,
                artifact_staging,
                WarehouseResourceCleanupStatus.COMPLETE,
                None,
            )
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED)
        if any(
            resource.resource_kind
            in {
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                WarehouseResourceKind.RESTORE_CONTAINER,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_DATA_VOLUME,
            }
            for resource in newly_recorded
        ):
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_PLAN)
        restore_receipt = self._restore_and_verify(
            binding,
            operation,
            identity,
            environment,
            plan,
            artifact_path,
            staging_path,
            current,
        )
        refreshed = _load_resources_by_ids(
            self._resource_recorder,
            binding,
            self._resource_ids,
        )
        artifact_digest = _file_sha256(artifact_path)
        restore_resources = tuple(
            resource
            for resource in refreshed
            if resource.resource_kind
            in {
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                WarehouseResourceKind.RESTORE_CONTAINER,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_DATA_VOLUME,
            }
        )
        return ClickHouseBackupLifecycleResult(
            backup_artifact_digest=artifact_digest,
            restore_verification=restore_receipt.verification,
            restore_cleanup_digest=digest(
                tuple(
                    (resource.resource_id, resource.cleanup_status)
                    for resource in restore_resources
                )
            ),
            positive_probe_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-backup-positive-probe-v1",
                    "artifact_digest": artifact_digest,
                    "grants": backup_grants.positive,
                }
            ),
            denial_probe_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-backup-denial-probe-v1",
                    "denied_grants": backup_grants.denied,
                }
            ),
            network_isolation_probe_digest=restore_receipt.network_isolation_probe_digest,
        )

    def _probe_backup_principal(
        self,
        identity: _ClickHouseWarehouseIdentity,
        plan: ClickHouseGrantPlan,
    ) -> _ClickHouseGrantProbeObservation:
        password = self._secret_capability.resolve_backup_restore_password().get_secret_value()
        client = _open_ready_client(
            self._client_factory,
            _connection_target(
                identity,
                username=plan.users["backup_restore"],
                password=password,
            ),
            operation="validate",
            timeout_seconds=self._settings.readiness_timeout_seconds,
        )
        positive = tuple(
            ("backup_restore", f"BACKUP ON `{database}`.*") for database in plan.databases.values()
        )
        denied = (
            ("backup_restore", f"INSERT ON `{plan.databases['raw']}`.*"),
            ("backup_restore", "CREATE USER ON *.*"),
        )
        try:
            if any(not _check_grant(client, privilege) for _, privilege in positive) or any(
                _check_grant(client, privilege) for _, privilege in denied
            ):
                raise WarehouseProviderError(
                    operation="validate",
                    classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
                )
        finally:
            client.close()
        return _ClickHouseGrantProbeObservation(positive=positive, denied=denied)

    def resources(self) -> tuple[PrivateWarehouseResource, ...]:
        if self._binding is None:
            return ()
        return _load_resources_by_ids(
            self._resource_recorder,
            self._binding,
            self._resource_ids,
        )

    def retire_backup(self, *, binding: WarehouseBinding) -> None:
        resources = tuple(
            resource
            for resource in self._resource_recorder.load_resources(
                binding.tenant_id,
                binding.binding_id,
            )
            if resource.resource_kind
            in {
                WarehouseResourceKind.BACKUP_ARTIFACT,
                WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
                WarehouseResourceKind.BACKUP_STAGING_FILE,
                WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT,
            }
        )
        self._binding = binding
        self._resource_ids.update(resource.resource_id for resource in resources)
        ordered_resources = sorted(
            resources,
            key=lambda resource: (
                resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
                resource.resource_id,
            ),
        )
        for resource in ordered_resources:
            if resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE:
                continue
            if resource.resource_kind is WarehouseResourceKind.BACKUP_STAGING_FILE:
                try:
                    _erase_private_file(
                        Path(resource.provider_resource_handle),
                        parent=Path(resource.provider_resource_handle).parent,
                    )
                except Exception as error:
                    _record_cleanup(
                        self._resource_recorder,
                        resource,
                        WarehouseResourceCleanupStatus.FAILED,
                        _classification_for_error(error),
                    )
                else:
                    _record_cleanup(
                        self._resource_recorder,
                        resource,
                        WarehouseResourceCleanupStatus.COMPLETE,
                        None,
                    )
                continue
            if self._clock() < resource.retention_deadline:
                if resource.cleanup_status is not WarehouseResourceCleanupStatus.RETAINED:
                    _record_cleanup(
                        self._resource_recorder,
                        resource,
                        WarehouseResourceCleanupStatus.RETAINED,
                        None,
                    )
                continue
            try:
                if resource.resource_kind is WarehouseResourceKind.BACKUP_ENCRYPTION_KEY:
                    current_resources = _load_resources_by_ids(
                        self._resource_recorder,
                        binding,
                        self._resource_ids,
                    )
                    artifact = next(
                        candidate
                        for candidate in current_resources
                        if candidate.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
                    )
                    if artifact.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE:
                        raise RuntimeError(
                            "ClickHouse backup key retirement requires artifact erasure"
                        )
                    self._retirement_capability.retire()
                    if not self._retirement_capability.is_retired():
                        raise RuntimeError("ClickHouse backup key retirement was not verified")
                else:
                    _erase_private_file(
                        Path(resource.provider_resource_handle),
                        parent=Path(resource.provider_resource_handle).parent,
                    )
            except Exception as error:
                if binding.lifecycle_state is WarehouseBindingState.RETIRED:
                    raise
                _record_cleanup(
                    self._resource_recorder,
                    resource,
                    WarehouseResourceCleanupStatus.FAILED,
                    _classification_for_error(error),
                )
            else:
                _record_cleanup(
                    self._resource_recorder,
                    resource,
                    WarehouseResourceCleanupStatus.COMPLETE,
                    None,
                )

    def _create_encrypted_backup(
        self,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
        plan: ClickHouseGrantPlan,
        artifact_path: Path,
        temporary_path: Path,
    ) -> None:
        backup_name = "pillarmesh-native.zip"
        password = self._secret_capability.resolve_backup_restore_password().get_secret_value()
        key = _decode_backup_key(
            self._secret_capability.resolve_backup_encryption_key().get_secret_value()
        )
        backup_completed = False
        try:
            # A hard kill skips the finally below, leaving the fixed-name artifact
            # behind. ClickHouse then rejects the replayed BACKUP as already
            # existing, which classifies terminal and fails the binding. `rm -f`
            # is idempotent, so clear the name before claiming it.
            self._remove_native_backup(identity, environment)
            client = _open_ready_client(
                self._client_factory,
                _connection_target(
                    identity,
                    username=plan.users["backup_restore"],
                    password=password,
                ),
                operation="validate",
                timeout_seconds=self._settings.readiness_timeout_seconds,
            )
            try:
                databases = ", ".join(f"DATABASE `{name}`" for name in plan.databases.values())
                client.execute(
                    f"BACKUP {databases} TO Disk('backups', '{backup_name}')",
                    operation="validate",
                )
            finally:
                client.close()
            with (
                self._compose.exec_stream(
                    project_name=identity.project_name,
                    arguments=(
                        "exec",
                        "-T",
                        "clickhouse",
                        "cat",
                        f"/var/lib/clickhouse/backups/{backup_name}",
                    ),
                    environment=environment,
                ) as source,
                _rewrite_private_binary_file(temporary_path) as destination,
            ):
                encrypt_backup_stream(
                    source,
                    destination,
                    key=key,
                    chunk_size=self._settings.backup_chunk_bytes,
                    nonce_prefix=self._entropy(4),
                )
            os.replace(temporary_path, artifact_path)
            _create_private_tombstone(temporary_path)
            _fsync_directory(identity.binding_directory)
            _assert_private_file(artifact_path)
            backup_completed = True
        except BaseException:
            with suppress(Exception):
                if _private_file_exists(temporary_path):
                    _erase_private_file(temporary_path, parent=identity.binding_directory)
            raise
        finally:
            try:
                self._remove_native_backup(identity, environment)
            except Exception:
                if backup_completed:
                    raise

    def _remove_native_backup(
        self,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
    ) -> None:
        backup_path = "/var/lib/clickhouse/backups/pillarmesh-native.zip"
        self._compose.exec(
            project_name=identity.project_name,
            arguments=(
                "exec",
                "-T",
                "clickhouse",
                "sh",
                "-ceu",
                'rm -f -- "$1" && test ! -e "$1"',
                "--",
                backup_path,
            ),
            environment=environment,
        )

    def _restore_and_verify(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
        plan: ClickHouseGrantPlan,
        artifact_path: Path,
        staging_path: Path,
        resources: tuple[PrivateWarehouseResource, ...],
    ) -> _ClickHouseRestoreReceipt:
        restore_resource_kinds = {
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
        restore_resources = tuple(
            resource for resource in resources if resource.resource_kind in restore_resource_kinds
        )
        verification_resource = next(
            resource
            for resource in resources
            if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
        )
        verification_path = Path(verification_resource.provider_resource_handle)
        staging_resource = next(
            resource
            for resource in resources
            if resource.resource_kind is WarehouseResourceKind.BACKUP_STAGING_FILE
        )
        restore_password_resource = next(
            resource
            for resource in resources
            if Path(resource.provider_resource_handle).name == "clickhouse-restore-password"
        )
        restore_users_resource = next(
            resource
            for resource in resources
            if Path(resource.provider_resource_handle).name == "restore-users.xml"
        )
        artifact_digest = _file_sha256(artifact_path)
        key = _decode_backup_key(
            self._secret_capability.resolve_backup_encryption_key().get_secret_value()
        )
        if _private_file_exists(verification_path):
            receipt = _load_restore_receipt(
                verification_path,
                binding,
                operation,
                artifact_digest=artifact_digest,
                key=key,
            )
            if verification_resource.creation_state is not WarehouseResourceCreationState.CREATED:
                self._resource_recorder.mark_created(
                    binding.tenant_id,
                    verification_resource.resource_id,
                    verification_resource.provider_resource_handle,
                )
            self._reconcile_verified_restore_cleanup(
                identity=identity,
                environment=environment,
                restore_resources=restore_resources,
                staging_resource=staging_resource,
                staging_path=staging_path,
                credential_resources=(
                    (restore_users_resource, Path(restore_users_resource.provider_resource_handle)),
                    (
                        restore_password_resource,
                        Path(restore_password_resource.provider_resource_handle),
                    ),
                ),
            )
            return receipt
        cleanup_states = {resource.cleanup_status for resource in restore_resources}
        if len(cleanup_states) > 1:
            _remove_restore_resources(
                self._compose,
                self._resource_recorder,
                restore_resources,
                {},
            )
            restore_resources = _load_resources_by_ids(
                self._resource_recorder,
                binding,
                {resource.resource_id for resource in restore_resources},
            )
        if cleanup_states != {WarehouseResourceCleanupStatus.PENDING}:
            restore_resources = self._resource_recorder.reopen_resources_for_recreation(
                restore_resources,
                reopened_at=self._clock(),
            )
        cleanup_was_pending = any(
            resource.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE
            for resource in restore_resources
        )
        try:
            with (
                artifact_path.open("rb") as source,
                _rewrite_private_binary_file(staging_path) as destination,
            ):
                decrypt_backup_stream(
                    source,
                    destination,
                    key=key,
                    maximum_chunk_size=self._settings.backup_chunk_bytes,
                )
        except BaseException:
            _erase_private_file(staging_path, parent=identity.binding_directory)
            _record_cleanup(
                self._resource_recorder,
                staging_resource,
                WarehouseResourceCleanupStatus.COMPLETE,
                None,
            )
            raise
        restore_password_path = Path(restore_password_resource.provider_resource_handle)
        restore_users_path = Path(restore_users_resource.provider_resource_handle)
        tunnel: _RestoreLoopbackTunnel | None = None
        try:
            tunnel = _RestoreLoopbackTunnel(
                compose=self._compose,
                project_name=_restore_identity(binding, operation).project_name,
            )
            client, restore_environment = self._prepare_restore_client(
                binding,
                operation,
                identity,
                staging_path,
                staging_resource,
                restore_password_resource,
                restore_users_resource,
                restore_resources,
                tunnel,
            )
        except BaseException:
            _cleanup_restore_attempt(
                compose=self._compose,
                recorder=self._resource_recorder,
                client=None,
                tunnel=tunnel,
                restore_resources=restore_resources,
                environment={},
                staging_resource=staging_resource,
                staging_path=staging_path,
                credential_resources=(
                    (restore_users_resource, restore_users_path),
                    (restore_password_resource, restore_password_path),
                ),
                parent=identity.binding_directory,
            )
            raise
        try:
            databases = ", ".join(f"DATABASE `{database}`" for database in plan.databases.values())
            network_isolation_probe_digest = _observe_restore_isolation(
                self._compose,
                primary=identity,
                restore=_restore_identity(binding, operation),
                primary_environment=environment,
                restore_environment=restore_environment,
            )
            client.execute(
                f"RESTORE {databases} FROM Disk('backups', 'restore.zip')",
                operation="validate",
            )
            for statement in plan.statements:
                client.execute(statement, operation="validate")
            restore_principal_passwords = {
                principal_class: self._entropy(32).hex()
                for principal_class in _ROLE_CLASSES
                if principal_class != "backup_restore"
            }
            for statement in _principal_statements(plan, restore_principal_passwords):
                client.execute(statement, operation="validate")
            self.prepare_principal(client, plan)
            versions = client.query_lines("SELECT version()")
            restored_databases = client.query_lines(
                "SELECT name FROM system.databases ORDER BY name"
            )
            restored_roles = client.query_lines("SELECT name FROM system.roles ORDER BY name")
            restored_role_grants = client.query_lines(
                "SELECT granted_role_name, user_name FROM system.role_grants ORDER BY 1, 2"
            )
            restored_users = client.query_lines("SELECT name FROM system.users ORDER BY name")
            restored_grants = _query_grant_rows(client, plan)
            restored_markers = client.query_lines(
                f"SELECT marker FROM `{plan.databases['control']}`."
                "`pillarmesh_validation_ledger` "
                "WHERE marker = 'pillarmesh-storage-marker' LIMIT 2"
            )
            restored_schema = client.query_lines(
                "SELECT database, name, engine FROM system.tables "
                "WHERE database IN ("
                + ", ".join(_sql_string_literal(database) for database in plan.databases.values())
                + ") ORDER BY database, name"
            )
            restored_query_result = client.query_lines(
                f"SELECT count() FROM `{plan.databases['control']}`."
                "`pillarmesh_validation_ledger` "
                "WHERE marker = 'pillarmesh-storage-marker'"
            )
            if (
                len(versions) != 1
                or not set(plan.databases.values()).issubset(restored_databases)
                or not _principal_state_matches(
                    plan,
                    roles=restored_roles,
                    role_grants=restored_role_grants,
                    users=restored_users,
                    grants=restored_grants,
                )
                or restored_markers != ("pillarmesh-storage-marker",)
                or not restored_schema
                or restored_query_result != ("1",)
            ):
                raise WarehouseProviderError(
                    operation="validate",
                    classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
                )
            verification = _observed_restore_verification(
                binding,
                operation,
                artifact_digest=artifact_digest,
                databases=restored_databases,
                schema=restored_schema,
                roles=restored_roles,
                users=restored_users,
                markers=restored_markers,
                query_result=restored_query_result,
                verified_at=self._clock(),
            )
            receipt = _ClickHouseRestoreReceipt(
                verification=verification,
                network_isolation_probe_digest=network_isolation_probe_digest,
                integrity_digest=_restore_receipt_integrity(
                    verification,
                    network_isolation_probe_digest,
                    key=key,
                ),
            )
            _write_or_load_private_file(
                verification_path,
                receipt.model_dump_json(),
                preserve_existing=True,
            )
            if verification_resource.creation_state is not WarehouseResourceCreationState.CREATED:
                self._resource_recorder.mark_created(
                    binding.tenant_id,
                    verification_resource.resource_id,
                    verification_resource.provider_resource_handle,
                )
                self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_VERIFIED)
        finally:
            _cleanup_restore_attempt(
                compose=self._compose,
                recorder=self._resource_recorder,
                client=client,
                tunnel=tunnel,
                restore_resources=restore_resources,
                environment=restore_environment,
                staging_resource=staging_resource,
                staging_path=staging_path,
                credential_resources=(
                    (restore_users_resource, restore_users_path),
                    (restore_password_resource, restore_password_path),
                ),
                parent=identity.binding_directory,
            )
            if cleanup_was_pending:
                self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CLEANUP)
        return receipt

    def _reconcile_verified_restore_cleanup(
        self,
        *,
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
        restore_resources: tuple[PrivateWarehouseResource, ...],
        staging_resource: PrivateWarehouseResource,
        staging_path: Path,
        credential_resources: tuple[tuple[PrivateWarehouseResource, Path], ...],
    ) -> None:
        failures: list[Exception] = []
        try:
            self._remove_native_backup(identity, environment)
        except Exception as error:
            failures.append(error)
        try:
            _remove_restore_resources(
                self._compose,
                self._resource_recorder,
                restore_resources,
                {},
            )
        except Exception as error:
            failures.append(error)
        _erase_private_resource(
            self._resource_recorder,
            staging_resource,
            staging_path,
            parent=identity.binding_directory,
            failures=failures,
        )
        for resource, path in credential_resources:
            _erase_private_resource(
                self._resource_recorder,
                resource,
                path,
                parent=identity.binding_directory,
                failures=failures,
            )
        if failures:
            raise failures[0]

    def _prepare_restore_client(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        identity: _ClickHouseWarehouseIdentity,
        staging_path: Path,
        staging_resource: PrivateWarehouseResource,
        restore_password_resource: PrivateWarehouseResource,
        restore_users_resource: PrivateWarehouseResource,
        restore_resources: tuple[PrivateWarehouseResource, ...],
        tunnel: _RestoreLoopbackTunnel,
    ) -> tuple[_Client, Mapping[str, str]]:
        if staging_resource.creation_state is not WarehouseResourceCreationState.CREATED:
            self._resource_recorder.mark_created(
                binding.tenant_id,
                staging_resource.resource_id,
                staging_resource.provider_resource_handle,
            )
        restore = _restore_identity(binding, operation)
        restore_password_path = Path(restore_password_resource.provider_resource_handle)
        restore_users_path = Path(restore_users_resource.provider_resource_handle)
        bootstrap_password = _write_or_recreate_private_file(
            restore_password_path,
            self._entropy(32).hex(),
            preserve_existing=True,
        )
        _write_or_recreate_private_file(
            restore_users_path,
            _bootstrap_users_configuration(bootstrap_password, network_enabled=True),
            preserve_existing=False,
        )
        for resource in (restore_password_resource, restore_users_resource):
            if resource.creation_state is not WarehouseResourceCreationState.CREATED:
                self._resource_recorder.mark_created(
                    binding.tenant_id,
                    resource.resource_id,
                    resource.provider_resource_handle,
                )
        restore_environment = _restore_environment(
            identity,
            restore,
            staging_path=staging_path,
            bootstrap_password=bootstrap_password,
        )
        self._compose.exec(
            project_name=restore.project_name,
            arguments=(
                "--profile",
                "restore",
                "up",
                "--detach",
                "clickhouse_restore",
                "clickhouse_isolation_probe",
            ),
            environment=restore_environment,
        )
        for resource in restore_resources:
            if (
                resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
                and self._compose.inspect_container_running(
                    identifier=resource.provider_resource_handle,
                    environment=restore_environment,
                )
                is not True
            ):
                raise ComposeCommandError(
                    "ClickHouse restore container startup could not be verified",
                    classification="ambiguous",
                )
        created_any = any(
            resource.creation_state is not WarehouseResourceCreationState.CREATED
            for resource in restore_resources
        )
        for resource in restore_resources:
            if resource.creation_state is not WarehouseResourceCreationState.CREATED:
                self._resource_recorder.mark_created(
                    binding.tenant_id,
                    resource.resource_id,
                    resource.provider_resource_handle,
                )
        if created_any:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED)
        tunnel.start(environment=restore_environment)
        client = _open_ready_client(
            self._client_factory,
            _restore_connection_target(
                identity,
                port=tunnel.port,
                username="default",
                password=bootstrap_password,
            ),
            operation="validate",
            timeout_seconds=self._settings.readiness_timeout_seconds,
        )
        return client, restore_environment


class ClickHouseWarehouseProvider:
    engine_kind = EngineKind.CLICKHOUSE

    def __init__(
        self,
        *,
        settings: ClickHouseWarehouseSettings,
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
        backup_commands: _BackupCommands | None,
        client_factory: Callable[[ClickHouseConnectionTarget], _Client],
        clock: Callable[[], datetime],
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
        self._client_factory = client_factory
        self._clock = clock
        self._entropy = entropy
        self._fault_hook = fault_hook

    def provision(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        try:
            return self._ensure_primary_resources(binding, operation, reconcile=False)
        except Exception as error:
            raise _translate_provider_error("provision", error) from None

    def reconcile(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseProvisionResult:
        try:
            if operation.operation_kind is WarehouseOperationKind.PROVISION:
                return self._ensure_primary_resources(binding, operation, reconcile=True)
            _assert_operation_ownership(binding, operation, operation.operation_kind)
            identity = _warehouse_identity(binding, self._settings.private_operation_directory)
            resources = _load_primary_resources_for_retirement(
                self._resource_recorder,
                binding,
                operation,
                identity,
            )
            environment = _compose_environment(identity)
            if operation.operation_kind is WarehouseOperationKind.SUSPEND:
                running = self._compose.inspect_container_running(
                    identifier=identity.container_name,
                    environment=environment,
                )
                if running is None:
                    raise ComposeCommandError(
                        "ClickHouse suspended state could not be reconciled",
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
                        "ClickHouse resumed state could not be reconciled",
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
                        "domain": "pillarmesh-clickhouse-warehouse-provider-v1",
                        "engine_kind": self.engine_kind.value,
                    }
                ),
                resource_inventory_digest=_stable_resource_inventory_digest(resources),
            )
        except Exception as error:
            raise _translate_provider_error("reconcile", error) from None

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult:
        try:
            return self._validate(binding, operation, resume=resume)
        except Exception as error:
            raise _translate_provider_error("validate", error) from None

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        try:
            self._suspend(binding, operation)
        except Exception as error:
            raise _translate_provider_error("suspend", error) from None

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        try:
            self._resume(binding, operation)
        except Exception as error:
            raise _translate_provider_error("resume", error) from None

    def retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        try:
            return self._retire(binding, operation)
        except Exception as error:
            raise _translate_provider_error("retire", error) from None

    def _ensure_primary_resources(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        reconcile: bool,
    ) -> WarehouseProvisionResult:
        if reconcile:
            _assert_reconciliation_ownership(binding, operation)
        else:
            _assert_operation_ownership(binding, operation, WarehouseOperationKind.PROVISION)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        expected = _planned_primary_resources(
            binding,
            operation,
            identity,
            retention_deadline=operation.started_at + self._settings.retention_period,
        )
        recorded = _load_owned_resources(
            self._resource_recorder,
            binding,
            operation,
            expected,
        )
        container_absent = self._compose.resource_is_absent(
            resource_kind="container",
            identifier=identity.container_name,
            environment={},
        )
        if container_absent is None:
            raise ComposeCommandError(
                "ClickHouse warehouse inspection was ambiguous",
                classification="ambiguous",
            )
        if not recorded and not container_absent:
            raise RuntimeError("ClickHouse warehouse refused to adopt an unrecorded project")
        if len(recorded) != len(expected) and not container_absent:
            raise RuntimeError(
                "ClickHouse warehouse resources were created before the durable plan completed"
            )
        by_id = {resource.resource_id: resource for resource in recorded}
        planned_any = False
        for resource in expected:
            if resource.resource_id not in by_id:
                self._resource_recorder.record_planned(resource)
                by_id[resource.resource_id] = resource
                planned_any = True
        if planned_any:
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RESOURCE_PLAN)
        resources = tuple(by_id[resource.resource_id] for resource in expected)
        requires_database_prepare = container_absent or any(
            resource.creation_state is not WarehouseResourceCreationState.CREATED
            for resource in resources
        )
        bootstrap_password = ""
        if requires_database_prepare:
            bootstrap_password = _prepare_private_files(
                identity,
                root=self._settings.private_operation_directory,
                bootstrap_password=self._entropy(32).hex(),
                tls_private_key=self._tls_private_key_secret.resolve().get_secret_value(),
                tls_certificate=self._tls_certificate_secret.resolve().get_secret_value(),
            )
        environment = _compose_environment(identity)
        if container_absent:
            self._compose.up(project_name=identity.project_name, environment=environment)
            self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_PROVIDER_CREATE)
        if requires_database_prepare:
            administration_password = self._administration_secret.resolve().get_secret_value()
            try:
                client = _open_ready_client(
                    self._client_factory,
                    _connection_target(
                        identity,
                        username=derive_clickhouse_grant_plan(
                            identity.private_resource_handle
                        ).users["administration"],
                        password=administration_password,
                    ),
                    operation="provision",
                    timeout_seconds=self._settings.readiness_timeout_seconds,
                )
            except WarehouseProviderError as error:
                if error.classification is not WarehouseFailureClassification.AUTHORIZATION_DENIED:
                    raise
                client = _open_ready_client(
                    self._client_factory,
                    _connection_target(
                        identity,
                        username="default",
                        password=bootstrap_password,
                    ),
                    operation="provision",
                    timeout_seconds=self._settings.readiness_timeout_seconds,
                )
            try:
                plan = derive_clickhouse_grant_plan(identity.private_resource_handle)
                for statement in plan.statements:
                    client.execute(statement, operation="provision")
                principal_passwords = {
                    "administration": administration_password,
                    "ingestion_runtime": (
                        self._ingestion_runtime_secret.resolve().get_secret_value()
                    ),
                    "transformation_runtime": (
                        self._transformation_runtime_secret.resolve().get_secret_value()
                    ),
                    "customer_sql": self._customer_sql_secret.resolve().get_secret_value(),
                    "catalog": self._catalog_secret.resolve().get_secret_value(),
                    "bi": self._bi_secret.resolve().get_secret_value(),
                }
                for statement in _principal_statements(plan, principal_passwords):
                    client.execute(statement, operation="provision")
                if self._backup_commands is None:
                    raise RuntimeError("ClickHouse backup command boundary is unavailable")
                self._backup_commands.prepare_principal(client, plan)
                _disable_bootstrap_user(identity.bootstrap_users_file, bootstrap_password)
                self._compose.exec(
                    project_name=identity.project_name,
                    arguments=(
                        "exec",
                        "-T",
                        "--user",
                        "root",
                        "clickhouse",
                        "install",
                        "-m",
                        "0644",
                        "-o",
                        "clickhouse",
                        "-g",
                        "clickhouse",
                        "/pillarmesh-private/bootstrap-users.xml",
                        "/etc/clickhouse-server/users.d/pillarmesh-bootstrap.xml",
                    ),
                    environment=environment,
                )
                client.execute("SYSTEM RELOAD CONFIG", operation="provision")
            finally:
                client.close()
            _assert_bootstrap_access_denied(
                self._client_factory,
                _connection_target(
                    identity,
                    username="default",
                    password=bootstrap_password,
                ),
            )
            _erase_private_file(
                identity.bootstrap_credential_file,
                parent=identity.binding_directory,
            )
        _verify_primary_container(self._compose, identity, environment)
        for resource in resources:
            if resource.creation_state is WarehouseResourceCreationState.CREATED:
                continue
            self._resource_recorder.mark_created(
                binding.tenant_id,
                resource.resource_id,
                resource.provider_resource_handle,
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
                    "domain": "pillarmesh-clickhouse-warehouse-provider-v1",
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
        _assert_operation_ownership(
            binding,
            operation,
            WarehouseOperationKind.RESUME if resume else WarehouseOperationKind.PROVISION,
            binding_revision_offset=0 if resume else 1,
        )
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        environment = _compose_environment(identity)
        _verify_primary_container(self._compose, identity, environment)
        plan = derive_clickhouse_grant_plan(identity.private_resource_handle)
        client = _open_ready_client(
            self._client_factory,
            _connection_target(
                identity,
                username=plan.users["administration"],
                password=self._administration_secret.resolve().get_secret_value(),
            ),
            operation="validate",
            timeout_seconds=self._settings.readiness_timeout_seconds,
        )
        try:
            grant_probes = self._probe_ordinary_principal_grants(identity, plan)
            observation = _observe_database(client, plan, grant_probes)
        finally:
            client.close()
        if resume:
            return ResumeWarehouseValidationResult(
                evidence=self._resume_evidence(binding, operation, observation)
            )
        if self._backup_commands is None:
            raise RuntimeError("ClickHouse backup command boundary is unavailable")
        backup = self._backup_commands.backup_and_restore(
            binding=binding,
            operation=operation,
            identity=identity,
            environment=environment,
            plan=plan,
        )
        return InitialWarehouseValidationResult(
            evidence=self._initial_evidence(binding, operation, observation, backup),
            restore_verification=backup.restore_verification,
        )

    def _probe_ordinary_principal_grants(
        self,
        identity: _ClickHouseWarehouseIdentity,
        plan: ClickHouseGrantPlan,
    ) -> _ClickHouseGrantProbeObservation:
        secrets = {
            "administration": self._administration_secret,
            "ingestion_runtime": self._ingestion_runtime_secret,
            "transformation_runtime": self._transformation_runtime_secret,
            "customer_sql": self._customer_sql_secret,
            "catalog": self._catalog_secret,
            "bi": self._bi_secret,
        }
        positive_observations: list[tuple[str, str]] = []
        denial_observations: list[tuple[str, str]] = []
        for (
            principal_class,
            positive_privileges,
            denied_privileges,
        ) in _ordinary_principal_probe_specifications(plan):
            client = _open_ready_client(
                self._client_factory,
                _connection_target(
                    identity,
                    username=plan.users[principal_class],
                    password=secrets[principal_class].resolve().get_secret_value(),
                ),
                operation="validate",
                timeout_seconds=self._settings.readiness_timeout_seconds,
            )
            try:
                for privilege in positive_privileges:
                    granted = _check_grant(client, privilege)
                    if not granted:
                        raise WarehouseProviderError(
                            operation="validate",
                            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
                        )
                    positive_observations.append((principal_class, privilege))
                for privilege in denied_privileges:
                    granted = _check_grant(client, privilege)
                    if granted:
                        raise WarehouseProviderError(
                            operation="validate",
                            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
                        )
                    denial_observations.append((principal_class, privilege))
            finally:
                client.close()
        return _ClickHouseGrantProbeObservation(
            positive=tuple(positive_observations),
            denied=tuple(denial_observations),
        )

    def _initial_evidence(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        observation: _ClickHouseDatabaseObservation,
        backup: ClickHouseBackupLifecycleResult,
    ) -> WarehouseValidationEvidence:
        capacity = _capacity_probe_digest(self._settings.capacity_bytes)
        return WarehouseValidationEvidence(
            evidence_id="wev-"
            + digest(
                {
                    "domain": "clickhouse-initial-evidence-v1",
                    "operation_id": operation.operation_id,
                }
            )[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
            engine_kind=EngineKind.CLICKHOUSE,
            engine_version=observation.engine_version,
            engine_build_digest=observation.engine_build_digest,
            engine_image_digest=_pinned_image_digest(),
            principal_profile_digest=observation.principal_profile_digest,
            namespace_grant_matrix_digest=observation.namespace_grant_matrix_digest,
            tls_probe_digest=observation.tls_probe_digest,
            network_isolation_probe_digest=backup.network_isolation_probe_digest,
            encryption_at_rest_evidence_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-local-encryption-disposition-v1",
                    "disposition": "deferred_local_acceptance",
                }
            ),
            encryption_at_rest_disposition=(EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
            positive_probe_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-all-positive-probes-v1",
                    "ordinary": observation.positive_probe_digest,
                    "backup": backup.positive_probe_digest,
                }
            ),
            denial_probe_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-all-denial-probes-v1",
                    "ordinary": observation.denial_probe_digest,
                    "backup": backup.denial_probe_digest,
                }
            ),
            ledger_probe_digest=observation.ledger_probe_digest,
            monitoring_probe_digest=observation.monitoring_probe_digest,
            capacity_alert_probe_digest=capacity,
            backup_artifact_digest=backup.backup_artifact_digest,
            restore_verification_digest=digest(backup.restore_verification),
            restore_cleanup_digest=backup.restore_cleanup_digest,
            observed_at=self._clock(),
        )

    def _resume_evidence(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        observation: _ClickHouseDatabaseObservation,
    ) -> WarehouseResumeValidationEvidence:
        return WarehouseResumeValidationEvidence(
            evidence_id="wrev-"
            + digest(
                {
                    "domain": "clickhouse-resume-evidence-v1",
                    "operation_id": operation.operation_id,
                }
            )[:24],
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            engine_kind=EngineKind.CLICKHOUSE,
            engine_version=observation.engine_version,
            engine_build_digest=observation.engine_build_digest,
            engine_image_digest=_pinned_image_digest(),
            tls_probe_digest=observation.tls_probe_digest,
            network_isolation_probe_digest=digest(
                {
                    "domain": "pillarmesh-clickhouse-resume-network-isolation-v1",
                    "loopback_https": True,
                    "compose_network_internal": True,
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
        _assert_operation_ownership(binding, operation, WarehouseOperationKind.SUSPEND)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        environment = _compose_environment(identity)
        self._compose.stop(project_name=identity.project_name, environment=environment)
        volume_absent = self._compose.resource_is_absent(
            resource_kind="volume",
            identifier=identity.data_volume_name,
            environment=environment,
        )
        if volume_absent is not False:
            raise ComposeCommandError(
                "ClickHouse data volume could not be verified after suspend",
                classification="ambiguous",
            )

    def _resume(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> None:
        _assert_operation_ownership(binding, operation, WarehouseOperationKind.RESUME)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        environment = _compose_environment(identity)
        self._compose.start(project_name=identity.project_name, environment=environment)
        _verify_primary_container(self._compose, identity, environment)
        client = _open_ready_client(
            self._client_factory,
            _connection_target(
                identity,
                username=derive_clickhouse_grant_plan(identity.private_resource_handle).users[
                    "administration"
                ],
                password=self._administration_secret.resolve().get_secret_value(),
            ),
            operation="resume",
            timeout_seconds=self._settings.readiness_timeout_seconds,
        )
        try:
            versions = client.query_lines("SELECT version()", operation="resume")
        finally:
            client.close()
        if versions != (CLICKHOUSE_SERVER_VERSION,):
            raise WarehouseProviderError(
                operation="resume",
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )

    def _retire(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
    ) -> WarehouseRetirementEvidence:
        _assert_operation_ownership(binding, operation, WarehouseOperationKind.RETIRE)
        identity = _warehouse_identity(binding, self._settings.private_operation_directory)
        resources = _load_primary_resources_for_retirement(
            self._resource_recorder,
            binding,
            operation,
            identity,
        )
        if not resources:
            raise RuntimeError("ClickHouse durable resource inventory is absent")
        environment = _compose_environment(identity)
        self._retire_active_resources(resources, identity, environment)
        deadline_reached = all(
            self._clock() >= resource.retention_deadline for resource in resources
        )
        if self._backup_commands is not None:
            self._backup_commands.retire_backup(binding=binding)
        for resource in sorted(
            resources,
            key=lambda candidate: (
                candidate.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY,
                candidate.resource_id,
            ),
        ):
            if resource.resource_kind in {
                WarehouseResourceKind.COMPOSE_PROJECT,
                WarehouseResourceKind.WAREHOUSE_CONTAINER,
                WarehouseResourceKind.PRIVATE_NETWORK,
            }:
                continue
            if not deadline_reached:
                _record_cleanup(
                    self._resource_recorder,
                    resource,
                    WarehouseResourceCleanupStatus.RETAINED,
                    None,
                )
                continue
            self._delete_retained_resource(
                resource,
                identity,
                environment,
                terminal_binding=binding.lifecycle_state is WarehouseBindingState.RETIRED,
            )
        current_primary = _load_resources_by_ids(
            self._resource_recorder,
            binding,
            {resource.resource_id for resource in resources},
        )
        backup_resources = (
            self._backup_commands.resources() if self._backup_commands is not None else ()
        )
        snapshot = canonical_retirement_resource_snapshot(current_primary + backup_resources)
        if snapshot.pending_resource_count:
            raise RuntimeError("ClickHouse retirement left a resource disposition pending")
        observed_at = self._clock()
        evidence = WarehouseRetirementEvidence(
            evidence_id="wret-"
            + digest(
                {
                    "domain": "clickhouse-retirement-evidence-v1",
                    "operation_id": operation.operation_id,
                    "observed_at": observed_at,
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
            observed_at=observed_at,
        )
        self._fault_hook(WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION)
        return evidence

    def _retire_active_resources(
        self,
        resources: tuple[PrivateWarehouseResource, ...],
        identity: _ClickHouseWarehouseIdentity,
        environment: Mapping[str, str],
    ) -> None:
        active_specs: tuple[
            tuple[WarehouseResourceKind, Literal["container", "network"], str], ...
        ] = (
            (WarehouseResourceKind.WAREHOUSE_CONTAINER, "container", identity.container_name),
            (WarehouseResourceKind.PRIVATE_NETWORK, "network", identity.private_network_name),
            (WarehouseResourceKind.PRIVATE_NETWORK, "network", identity.loopback_network_name),
        )
        failures: list[Exception] = []
        for resource_kind, compose_kind, identifier in active_specs:
            matching = next(
                resource
                for resource in resources
                if resource.resource_kind is resource_kind
                and resource.provider_resource_handle == identifier
            )
            if matching.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE:
                continue
            try:
                absent = self._compose.resource_is_absent(
                    resource_kind=compose_kind,
                    identifier=identifier,
                    environment=environment,
                )
                if absent is None:
                    raise ComposeCommandError(
                        "ClickHouse active resource inspection was ambiguous",
                        classification="ambiguous",
                    )
                if not absent:
                    if compose_kind == "container":
                        with suppress(ComposeCommandError):
                            self._compose.stop(
                                project_name=identity.project_name,
                                environment=environment,
                            )
                    self._compose.remove_resource(
                        resource_kind=compose_kind,
                        identifier=identifier,
                        environment=environment,
                    )
            except Exception as error:
                failures.append(error)
                _record_cleanup(
                    self._resource_recorder,
                    matching,
                    WarehouseResourceCleanupStatus.FAILED,
                    _classification_for_error(error),
                )
            else:
                _record_cleanup(
                    self._resource_recorder,
                    matching,
                    WarehouseResourceCleanupStatus.COMPLETE,
                    None,
                )
        project = next(
            resource
            for resource in resources
            if resource.resource_kind is WarehouseResourceKind.COMPOSE_PROJECT
        )
        if project.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE:
            return
        _record_cleanup(
            self._resource_recorder,
            project,
            (
                WarehouseResourceCleanupStatus.FAILED
                if failures
                else WarehouseResourceCleanupStatus.COMPLETE
            ),
            (_classification_for_error(failures[0]) if failures else None),
        )

    def _delete_retained_resource(
        self,
        resource: PrivateWarehouseResource,
        identity: _ClickHouseWarehouseIdentity,
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
                if (
                    self._compose.resource_is_absent(
                        resource_kind="volume",
                        identifier=resource.provider_resource_handle,
                        environment=environment,
                    )
                    is not True
                ):
                    raise RuntimeError("ClickHouse data volume deletion was not verified")
            elif resource.resource_kind in {
                WarehouseResourceKind.CREDENTIAL_FILE,
                WarehouseResourceKind.HOST_PORT_FILE,
                WarehouseResourceKind.TLS_PRIVATE_KEY,
                WarehouseResourceKind.TLS_CERTIFICATE,
            }:
                _erase_private_file(
                    Path(resource.provider_resource_handle),
                    parent=identity.binding_directory,
                )
            elif resource.resource_kind is WarehouseResourceKind.PRIVATE_DIRECTORY:
                _assert_private_directory(identity.binding_directory)
                if any(path.stat().st_size for path in identity.binding_directory.iterdir()):
                    raise RuntimeError("ClickHouse private directory still contains active data")
        except Exception as error:
            if terminal_binding:
                raise
            _record_cleanup(
                self._resource_recorder,
                resource,
                WarehouseResourceCleanupStatus.FAILED,
                _classification_for_error(error),
            )
            return
        _record_cleanup(
            self._resource_recorder,
            resource,
            WarehouseResourceCleanupStatus.COMPLETE,
            None,
        )


type _ClickHouseOperation = Literal[
    "provision", "reconcile", "validate", "suspend", "resume", "retire"
]
type _ClickHouseSemantic = Literal[
    "append_only_insert",
    "set_based_transform",
    "read_only_query",
    "metadata_inspection",
    "native_backup_restore",
]

_DATABASE_CLASSES = (
    "raw",
    "conformed",
    "product",
    "consumption",
    "quarantine",
    "control",
)
_ROLE_CLASSES = (
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "backup_restore",
    "customer_sql",
    "catalog",
    "bi",
)
_SUPPORTED_SEMANTICS: frozenset[str] = frozenset(
    {
        "append_only_insert",
        "set_based_transform",
        "read_only_query",
        "metadata_inspection",
        "native_backup_restore",
    }
)


class ClickHouseConnectionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    endpoint: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr
    root_certificate: Path
    client_certificate: Path
    client_private_key: Path

    @field_validator("endpoint")
    @classmethod
    def requires_loopback_https(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ClickHouse endpoint must be loopback HTTPS")
        return value.rstrip("/")


class ClickHouseHTTPClient:
    def __init__(
        self,
        target: ClickHouseConnectionTarget,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        verify: ssl.SSLContext | bool
        if transport is None:
            context = ssl.create_default_context(cafile=str(target.root_certificate))
            context.load_cert_chain(
                certfile=str(target.client_certificate),
                keyfile=str(target.client_private_key),
            )
            verify = context
        else:
            verify = False
        self._client = httpx.Client(
            base_url=target.endpoint,
            auth=(target.username, target.password.get_secret_value()),
            headers={"content-type": "text/plain; charset=utf-8"},
            timeout=10.0,
            transport=transport,
            trust_env=False,
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    def execute(
        self,
        statement: str,
        *,
        operation: _ClickHouseOperation = "validate",
    ) -> bytes:
        try:
            response = self._client.post("/", content=statement.encode("utf-8"))
        except httpx.TransportError:
            raise WarehouseProviderError(
                operation=operation,
                classification=WarehouseFailureClassification.TRANSIENT_TRANSPORT,
            ) from None
        if response.status_code != 200:
            raise WarehouseProviderError(
                operation=operation,
                classification=_classification_for_status(response.status_code),
            ) from None
        return response.content

    def query_lines(
        self,
        statement: str,
        *,
        operation: _ClickHouseOperation = "validate",
    ) -> tuple[str, ...]:
        payload = self.execute(
            statement.rstrip().rstrip(";") + " FORMAT TabSeparatedRaw",
            operation=operation,
        )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise WarehouseProviderError(
                operation=operation,
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            ) from None
        return tuple(text.splitlines())


def _open_ready_client(
    factory: Callable[[ClickHouseConnectionTarget], _Client],
    target: ClickHouseConnectionTarget,
    *,
    operation: _ClickHouseOperation,
    timeout_seconds: float,
) -> _Client:
    deadline = time.monotonic() + timeout_seconds
    retryable = {
        WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        WarehouseFailureClassification.THROTTLED,
    }
    while True:
        client = factory(target)
        try:
            if client.query_lines("SELECT 1", operation=operation) == ("1",):
                return client
        except WarehouseProviderError as error:
            if error.classification not in retryable:
                client.close()
                raise
            last_error: WarehouseProviderError | None = error
        else:
            last_error = None
        client.close()
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            raise WarehouseProviderError(
                operation=operation,
                classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
            )
        time.sleep(0.25)


def _assert_bootstrap_access_denied(
    factory: Callable[[ClickHouseConnectionTarget], _Client],
    target: ClickHouseConnectionTarget,
) -> None:
    client = factory(target)
    try:
        try:
            client.query_lines("SELECT 1", operation="provision")
        except WarehouseProviderError as error:
            if error.classification is WarehouseFailureClassification.AUTHORIZATION_DENIED:
                return
            raise
        raise WarehouseProviderError(
            operation="provision",
            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        )
    finally:
        client.close()


def _classification_for_status(status_code: int) -> WarehouseFailureClassification:
    if status_code in {401, 403}:
        return WarehouseFailureClassification.AUTHORIZATION_DENIED
    if status_code == 429:
        return WarehouseFailureClassification.THROTTLED
    if status_code in {408, 502, 503, 504}:
        return WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    if status_code == 500:
        return WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    if status_code in {400, 404, 409, 422}:
        return WarehouseFailureClassification.STATEMENT_REJECTED
    return WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE


@dataclass(frozen=True, slots=True)
class ClickHouseGrantPlan:
    databases: Mapping[str, str]
    roles: Mapping[str, str]
    users: Mapping[str, str]
    statements: tuple[str, ...]


def derive_clickhouse_grant_plan(private_resource_handle: str) -> ClickHouseGrantPlan:
    prefix = (
        "pm_"
        + digest(
            {
                "domain": "pillarmesh-clickhouse-grant-plan-v1",
                "private_resource_handle": private_resource_handle,
            }
        )[:12]
    )
    databases = MappingProxyType(
        {database_class: f"{prefix}_{database_class}" for database_class in _DATABASE_CLASSES}
    )
    roles = MappingProxyType({role_class: f"{prefix}_{role_class}" for role_class in _ROLE_CLASSES})
    users = MappingProxyType(
        {role_class: f"{prefix}_{role_class}_user" for role_class in _ROLE_CLASSES}
    )
    statements = _grant_statements(databases=databases, roles=roles)
    return ClickHouseGrantPlan(
        databases=databases,
        roles=roles,
        users=users,
        statements=statements,
    )


def assert_supported_semantics(requested_semantics: frozenset[str]) -> None:
    if not requested_semantics.issubset(_SUPPORTED_SEMANTICS):
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        )


def _grant_statements(
    *,
    databases: Mapping[str, str],
    roles: Mapping[str, str],
) -> tuple[str, ...]:
    statements = [
        *(f"CREATE DATABASE IF NOT EXISTS `{database}`" for database in databases.values()),
        (
            f"CREATE TABLE IF NOT EXISTS `{databases['consumption']}`.`customer_probe` "
            "(id UInt64, payload String) ENGINE = MergeTree ORDER BY id"
        ),
        (
            f"CREATE TABLE IF NOT EXISTS `{databases['consumption']}`.`certified_probe` "
            "(id UInt64, payload String) ENGINE = MergeTree ORDER BY id"
        ),
        *(f"CREATE ROLE IF NOT EXISTS `{role}`" for role in roles.values()),
        (
            "GRANT CREATE DATABASE, DROP DATABASE, CREATE USER, ALTER USER, DROP USER, "
            "CREATE ROLE, ALTER ROLE, DROP ROLE, "
            f"ROLE ADMIN ON *.* TO `{roles['administration']}`"
        ),
    ]
    statements.extend(
        f"GRANT SELECT, INSERT, CREATE TABLE, ALTER, DROP TABLE ON `{database}`.* "
        f"TO `{roles['administration']}`"
        for database in databases.values()
    )
    statements.extend(
        f"GRANT SELECT ON system.{table} TO `{roles['administration']}`"
        for table in ("roles", "role_grants", "users", "grants", "asynchronous_metrics")
    )
    statements.extend(
        (
            f"GRANT SELECT, INSERT, CREATE TABLE ON `{databases['raw']}`.* "
            f"TO `{roles['ingestion_runtime']}`",
            f"GRANT SELECT, INSERT ON `{databases['control']}`.* TO `{roles['ingestion_runtime']}`",
            f"GRANT SELECT ON `{databases['raw']}`.* TO `{roles['transformation_runtime']}`",
        )
    )
    statements.extend(
        (
            f"GRANT SELECT, INSERT, CREATE TABLE, ALTER, DROP TABLE ON `{databases[name]}`.* "
            f"TO `{roles['transformation_runtime']}`"
        )
        for name in ("conformed", "product", "quarantine", "consumption")
    )
    statements.extend(
        f"GRANT BACKUP ON `{database}`.* TO `{roles['backup_restore']}`"
        for database in databases.values()
    )
    statements.extend(
        (
            f"GRANT SELECT ON `{databases['consumption']}`.`customer_probe` "
            f"TO `{roles['customer_sql']}`",
            f"GRANT SELECT ON `{databases['consumption']}`.`certified_probe` TO `{roles['bi']}`",
            f"GRANT SELECT ON system.databases TO `{roles['catalog']}`",
            f"GRANT SELECT ON system.tables TO `{roles['catalog']}`",
            f"GRANT SELECT ON system.columns TO `{roles['catalog']}`",
        )
    )
    return tuple(statements)


def _principal_statements(
    plan: ClickHouseGrantPlan,
    passwords: Mapping[str, str],
) -> tuple[str, ...]:
    expected = set(_ROLE_CLASSES) - {"backup_restore"}
    if set(passwords) != expected:
        raise ValueError("ClickHouse principal password set is incomplete")
    statements: list[str] = []
    for principal_class in _ROLE_CLASSES:
        if principal_class == "backup_restore":
            continue
        password_literal = _sql_string_literal(passwords[principal_class])
        statements.extend(
            (
                f"CREATE USER IF NOT EXISTS `{plan.users[principal_class]}` "
                f"IDENTIFIED WITH sha256_password BY {password_literal}",
                f"GRANT `{plan.roles[principal_class]}` TO `{plan.users[principal_class]}`",
            )
        )
    return tuple(statements)


def _sql_string_literal(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("ClickHouse secret cannot be represented as a SQL string")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _principal_profile_digest(
    *,
    roles: tuple[str, ...],
    users: tuple[str, ...],
) -> str:
    return digest(
        {
            "domain": "pillarmesh-clickhouse-principal-profile-v1",
            "roles": tuple(sorted(set(roles))),
            "users": tuple(sorted(set(users))),
        }
    )


def _observe_database(
    client: _Client,
    plan: ClickHouseGrantPlan,
    grant_probes: _ClickHouseGrantProbeObservation,
) -> _ClickHouseDatabaseObservation:
    versions = client.query_lines("SELECT version()")
    if versions != (CLICKHOUSE_SERVER_VERSION,):
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        )
    roles = client.query_lines("SELECT name FROM system.roles ORDER BY name")
    role_grants = client.query_lines(
        "SELECT granted_role_name, user_name FROM system.role_grants ORDER BY 1, 2"
    )
    users = client.query_lines("SELECT name FROM system.users ORDER BY name")
    grants = _query_grant_rows(client, plan)
    metrics = client.query_lines(
        "SELECT metric, value FROM system.asynchronous_metrics "
        "WHERE metric IN ('FilesystemMainPathUsedBytes', 'FilesystemMainPathTotalBytes') "
        "ORDER BY metric"
    )
    metric_names = tuple(line.partition("\t")[0] for line in metrics)
    if set(metric_names) != {
        "FilesystemMainPathTotalBytes",
        "FilesystemMainPathUsedBytes",
    }:
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        )
    control_database = plan.databases["control"]
    client.execute(
        f"CREATE TABLE IF NOT EXISTS `{control_database}`.`pillarmesh_validation_ledger` "
        "(marker String) ENGINE = MergeTree ORDER BY marker"
    )
    client.execute(
        f"INSERT INTO `{control_database}`.`pillarmesh_validation_ledger` "
        "SELECT 'pillarmesh-storage-marker' WHERE NOT EXISTS ("
        f"SELECT 1 FROM `{control_database}`.`pillarmesh_validation_ledger` "
        "WHERE marker = 'pillarmesh-storage-marker' LIMIT 1)"
    )
    markers = client.query_lines(
        f"SELECT marker FROM `{control_database}`.`pillarmesh_validation_ledger` "
        "WHERE marker = 'pillarmesh-storage-marker' LIMIT 2"
    )
    if not _principal_state_matches(
        plan,
        roles=roles,
        role_grants=role_grants,
        users=users,
        grants=grants,
    ) or markers != ("pillarmesh-storage-marker",):
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        )
    engine_version = versions[0]
    return _ClickHouseDatabaseObservation(
        engine_version=engine_version,
        engine_build_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-engine-build-v1",
                "version": engine_version,
            }
        ),
        principal_profile_digest=_principal_profile_digest(roles=roles, users=users),
        namespace_grant_matrix_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-grant-matrix-v1",
                "role_grants": role_grants,
                "grants": grants,
            }
        ),
        tls_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-tls-probe-v1",
                "https": True,
                "mutual_tls": True,
            }
        ),
        positive_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-positive-probes-v1",
                "roles": len(roles),
                "metrics": metric_names,
                "grants": grant_probes.positive,
            }
        ),
        denial_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-denial-probes-v1",
                "denied_grants": grant_probes.denied,
            }
        ),
        ledger_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-ledger-probe-v1",
                "markers": markers,
            }
        ),
        monitoring_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-monitoring-probe-v1",
                "metrics": metric_names,
            }
        ),
        storage_integrity_probe_digest=digest(
            {
                "domain": "pillarmesh-clickhouse-storage-integrity-v1",
                "markers": markers,
            }
        ),
    )


def _principal_state_matches(
    plan: ClickHouseGrantPlan,
    *,
    roles: tuple[str, ...],
    role_grants: tuple[str, ...],
    users: tuple[str, ...],
    grants: tuple[str, ...],
) -> bool:
    observed_role_grants = {
        tuple(line.split("\t", maxsplit=1)) for line in role_grants if line.count("\t") == 1
    }
    expected_role_grants = {
        (plan.roles[principal_class], plan.users[principal_class])
        for principal_class in _ROLE_CLASSES
    }
    return (
        set(roles) == set(plan.roles.values())
        and set(users) == {*plan.users.values(), "default"}
        and expected_role_grants == observed_role_grants
        and set(grants) == _expected_grant_rows(plan)
    )


def _query_grant_rows(client: _Client, plan: ClickHouseGrantPlan) -> tuple[str, ...]:
    role_literals = ", ".join(_sql_string_literal(role) for role in plan.roles.values())
    return client.query_lines(
        "SELECT user_name, role_name, access_type, database, table, column, "
        "is_partial_revoke, grant_option FROM system.grants "
        f"WHERE role_name IN ({role_literals}) "
        "ORDER BY role_name, access_type, database, table, column"
    )


def _expected_grant_rows(plan: ClickHouseGrantPlan) -> set[str]:
    rows: set[str] = set()

    def add(
        role_class: str,
        access_type: str,
        database: str = "\\N",
        table: str = "\\N",
    ) -> None:
        rows.add(
            "\t".join(
                ("\\N", plan.roles[role_class], access_type, database, table, "\\N", "0", "0")
            )
        )

    for access_type in (
        "CREATE DATABASE",
        "DROP DATABASE",
        "CREATE USER",
        "ALTER USER",
        "DROP USER",
        "CREATE ROLE",
        "ALTER ROLE",
        "DROP ROLE",
        "ROLE ADMIN",
    ):
        add("administration", access_type)
    for database in plan.databases.values():
        for access_type in ("SELECT", "INSERT", "ALTER", "CREATE TABLE", "DROP TABLE"):
            add("administration", access_type, database)
    for table in ("roles", "role_grants", "users", "grants", "asynchronous_metrics"):
        add("administration", "SELECT", "system", table)
    for access_type in ("SELECT", "INSERT", "CREATE TABLE"):
        add("ingestion_runtime", access_type, plan.databases["raw"])
    for access_type in ("SELECT", "INSERT"):
        add("ingestion_runtime", access_type, plan.databases["control"])
    add("transformation_runtime", "SELECT", plan.databases["raw"])
    for database_class in ("conformed", "product", "quarantine", "consumption"):
        for access_type in ("SELECT", "INSERT", "ALTER", "CREATE TABLE", "DROP TABLE"):
            add("transformation_runtime", access_type, plan.databases[database_class])
    for database in plan.databases.values():
        add("backup_restore", "BACKUP", database)
    add("customer_sql", "SELECT", plan.databases["consumption"], "customer_probe")
    add("bi", "SELECT", plan.databases["consumption"], "certified_probe")
    for table in ("databases", "tables", "columns"):
        add("catalog", "SELECT", "system", table)
    return rows


def _ordinary_principal_probe_specifications(
    plan: ClickHouseGrantPlan,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    raw = plan.databases["raw"]
    conformed = plan.databases["conformed"]
    consumption = plan.databases["consumption"]
    customer_probe = f"`{consumption}`.`customer_probe`"
    certified_probe = f"`{consumption}`.`certified_probe`"
    return (
        (
            "administration",
            ("CREATE USER ON *.*",),
            (f"BACKUP ON `{raw}`.*",),
        ),
        (
            "ingestion_runtime",
            (f"INSERT ON `{raw}`.*", f"INSERT ON `{plan.databases['control']}`.*"),
            (f"SELECT ON `{consumption}`.*", "CREATE USER ON *.*"),
        ),
        (
            "transformation_runtime",
            (f"SELECT ON `{raw}`.*", f"INSERT ON `{conformed}`.*"),
            (f"BACKUP ON `{raw}`.*", "CREATE USER ON *.*"),
        ),
        (
            "customer_sql",
            (f"SELECT ON {customer_probe}",),
            (f"SELECT ON {certified_probe}", f"SELECT ON `{raw}`.*", "CREATE USER ON *.*"),
        ),
        (
            "catalog",
            ("SELECT ON system.tables",),
            (f"SELECT ON `{consumption}`.*", "CREATE USER ON *.*"),
        ),
        (
            "bi",
            (f"SELECT ON {certified_probe}",),
            (f"SELECT ON {customer_probe}", f"SELECT ON `{raw}`.*", "CREATE USER ON *.*"),
        ),
    )


def _check_grant(client: _Client, privilege: str) -> bool:
    payload = client.execute(f"CHECK GRANT {privilege}")
    try:
        result = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        ) from None
    if result == "1":
        return True
    if result == "0":
        return False
    raise WarehouseProviderError(
        operation="validate",
        classification=WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
    )


def _capacity_probe_digest(capacity_bytes: int) -> str:
    warning_open = (capacity_bytes * 85) // 100
    warning_cleared = (capacity_bytes * 74) // 100
    return digest(
        {
            "domain": "pillarmesh-clickhouse-capacity-alert-v1",
            "capacity_bytes": capacity_bytes,
            "opened_at_bytes": warning_open,
            "cleared_at_bytes": warning_cleared,
            "open_transition": True,
            "clear_transition": True,
        }
    )


def _pinned_image_digest() -> str:
    prefix = "@sha256:"
    if prefix not in CLICKHOUSE_WAREHOUSE_IMAGE:
        raise ValueError("ClickHouse warehouse image is not digest-pinned")
    image_digest = CLICKHOUSE_WAREHOUSE_IMAGE.rsplit(prefix, 1)[1]
    if len(image_digest) != 64 or any(
        character not in "0123456789abcdef" for character in image_digest
    ):
        raise ValueError("ClickHouse warehouse image digest is invalid")
    return image_digest


def _warehouse_identity(
    binding: WarehouseBinding,
    private_operation_directory: Path,
) -> _ClickHouseWarehouseIdentity:
    identity_digest = digest(
        {
            "domain": "pillarmesh-clickhouse-warehouse-binding-v1",
            "tenant_id": binding.tenant_id,
            "binding_id": binding.binding_id,
        }
    )[:20]
    private_resource_handle = f"chw-{identity_digest}"
    project_name = f"pm-ch-{identity_digest}"
    binding_directory = private_operation_directory / private_resource_handle
    return _ClickHouseWarehouseIdentity(
        private_resource_handle=private_resource_handle,
        project_name=project_name,
        container_name=f"{project_name}-database",
        private_network_name=f"{project_name}-private",
        loopback_network_name=f"{project_name}-loopback",
        data_volume_name=f"{project_name}-data",
        binding_directory=binding_directory,
        bootstrap_credential_file=binding_directory / "bootstrap.secret",
        bootstrap_users_file=binding_directory / "bootstrap-users.xml",
        host_port_file=binding_directory / "host.port",
        server_private_key_file=binding_directory / "server.key",
        server_certificate_file=binding_directory / "server.crt",
        client_private_key_file=binding_directory / "client.key",
        client_certificate_file=binding_directory / "client.crt",
        root_certificate_file=binding_directory / "ca.crt",
    )


def _restore_identity(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
) -> _ClickHouseRestoreIdentity:
    restore_digest = digest(
        {
            "domain": "pillarmesh-clickhouse-restore-project-v1",
            "tenant_id": binding.tenant_id,
            "binding_id": binding.binding_id,
            "operation_id": operation.operation_id,
        }
    )[:20]
    project_name = f"pm-chr-{restore_digest}"
    return _ClickHouseRestoreIdentity(
        project_name=project_name,
        container_name=f"{project_name}-database",
        isolation_probe_container_name=f"{project_name}-database-isolation-probe",
        private_network_name=f"{project_name}-private",
        loopback_network_name=f"{project_name}-loopback",
        data_volume_name=f"{project_name}-data",
    )


def _planned_primary_resources(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _ClickHouseWarehouseIdentity,
    *,
    retention_deadline: datetime,
) -> tuple[PrivateWarehouseResource, ...]:
    specifications = (
        (WarehouseResourceKind.COMPOSE_PROJECT, identity.project_name, None),
        (
            WarehouseResourceKind.WAREHOUSE_CONTAINER,
            identity.container_name,
            identity.project_name,
        ),
        (
            WarehouseResourceKind.PRIVATE_NETWORK,
            identity.private_network_name,
            identity.project_name,
        ),
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
            str(identity.bootstrap_credential_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.CREDENTIAL_FILE,
            str(identity.bootstrap_users_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.HOST_PORT_FILE,
            str(identity.host_port_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            str(identity.server_private_key_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.server_certificate_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            str(identity.client_private_key_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.client_certificate_file),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.TLS_CERTIFICATE,
            str(identity.root_certificate_file),
            identity.private_resource_handle,
        ),
    )
    resources: list[PrivateWarehouseResource] = []
    for resource_kind, provider_handle, parent_handle in specifications:
        resource_id = (
            "wrs-"
            + digest(
                {
                    "domain": "pillarmesh-clickhouse-warehouse-resource-v1",
                    "tenant_id": binding.tenant_id,
                    "binding_id": binding.binding_id,
                    "resource_kind": resource_kind.value,
                    "provider_resource_handle": provider_handle,
                }
            )[:24]
        )
        resources.append(
            PrivateWarehouseResource(
                tenant_id=binding.tenant_id,
                binding_id=binding.binding_id,
                binding_revision=operation.binding_revision,
                operation_id=operation.operation_id,
                resource_id=resource_id,
                resource_kind=resource_kind,
                provider_resource_handle=provider_handle,
                parent_resource_handle=parent_handle,
                creation_state=WarehouseResourceCreationState.PLANNED,
                retention_deadline=retention_deadline,
                cleanup_status=WarehouseResourceCleanupStatus.PENDING,
                created_at=operation.started_at,
                updated_at=operation.started_at,
            )
        )
    return tuple(resources)


def _planned_backup_resources(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _ClickHouseWarehouseIdentity,
    key_resource_handle: str,
    *,
    retention_deadline: datetime,
) -> tuple[PrivateWarehouseResource, ...]:
    restore = _restore_identity(binding, operation)
    specifications = (
        (
            WarehouseResourceKind.BACKUP_ARTIFACT,
            str(identity.binding_directory / "clickhouse-backup.pmwhbk"),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.BACKUP_STAGING_FILE,
            str(identity.binding_directory / "clickhouse-restore.zip"),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.BACKUP_STAGING_FILE,
            str(identity.binding_directory / "clickhouse-backup.pmwhbk.incomplete"),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.CREDENTIAL_FILE,
            str(identity.binding_directory / "clickhouse-restore-password"),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.CREDENTIAL_FILE,
            str(identity.binding_directory / "restore-users.xml"),
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            key_resource_handle,
            identity.private_resource_handle,
        ),
        (
            WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT,
            str(identity.binding_directory / "restore-verification.json"),
            identity.private_resource_handle,
        ),
        (WarehouseResourceKind.RESTORE_COMPOSE_PROJECT, restore.project_name, None),
        (
            WarehouseResourceKind.RESTORE_CONTAINER,
            restore.container_name,
            restore.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_CONTAINER,
            restore.isolation_probe_container_name,
            restore.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            restore.private_network_name,
            restore.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            restore.loopback_network_name,
            restore.project_name,
        ),
        (
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
            restore.data_volume_name,
            restore.project_name,
        ),
    )
    return tuple(
        _operation_resource(
            binding,
            operation,
            resource_kind=resource_kind,
            provider_resource_handle=provider_handle,
            parent_resource_handle=parent_handle,
            retention_deadline=retention_deadline,
        )
        for resource_kind, provider_handle, parent_handle in specifications
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
    resource_id = (
        "wrs-"
        + digest(
            {
                "domain": "pillarmesh-clickhouse-operation-resource-v1",
                "tenant_id": binding.tenant_id,
                "binding_id": binding.binding_id,
                "operation_id": operation.operation_id,
                "resource_kind": resource_kind.value,
                "provider_resource_handle": provider_resource_handle,
            }
        )[:24]
    )
    return PrivateWarehouseResource(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=operation.binding_revision,
        operation_id=operation.operation_id,
        resource_id=resource_id,
        resource_kind=resource_kind,
        provider_resource_handle=provider_resource_handle,
        parent_resource_handle=parent_resource_handle,
        creation_state=WarehouseResourceCreationState.PLANNED,
        retention_deadline=retention_deadline,
        cleanup_status=WarehouseResourceCleanupStatus.PENDING,
        created_at=operation.started_at,
        updated_at=operation.started_at,
    )


def _record_backup_plan(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    expected: tuple[PrivateWarehouseResource, ...],
) -> tuple[PrivateWarehouseResource, ...]:
    recorded = recorder.load_resources(binding.tenant_id, binding.binding_id)
    expected_by_id = {resource.resource_id: resource for resource in expected}
    current_by_id = {
        resource.resource_id: resource
        for resource in recorded
        if resource.resource_id in expected_by_id
    }
    for current in current_by_id.values():
        if _resource_identity(current) != _resource_identity(expected_by_id[current.resource_id]):
            raise RuntimeError("ClickHouse backup durable resource ownership changed")
    for resource in expected:
        if resource.resource_id not in current_by_id:
            recorder.record_planned(resource)
            current_by_id[resource.resource_id] = resource
    return tuple(current_by_id[resource.resource_id] for resource in expected)


def _load_owned_resources(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    expected_resources: tuple[PrivateWarehouseResource, ...],
) -> tuple[PrivateWarehouseResource, ...]:
    expected_by_id = {resource.resource_id: resource for resource in expected_resources}
    expected_handles = {resource.provider_resource_handle for resource in expected_resources}
    candidates = tuple(
        resource
        for resource in recorder.load_resources(binding.tenant_id, binding.binding_id)
        if resource.resource_id in expected_by_id
        or resource.provider_resource_handle in expected_handles
    )
    if not candidates:
        return ()
    if len({resource.resource_id for resource in candidates}) != len(candidates):
        raise RuntimeError("ClickHouse warehouse durable resource identity is ambiguous")
    current_by_id = {resource.resource_id: resource for resource in candidates}
    for current in candidates:
        expected = expected_by_id.get(current.resource_id)
        if (
            expected is None
            or current.operation_id != operation.operation_id
            or _resource_identity(current) != _resource_identity(expected)
        ):
            raise RuntimeError("ClickHouse warehouse durable resource ownership changed")
    return tuple(
        current_by_id[resource.resource_id]
        for resource in expected_resources
        if resource.resource_id in current_by_id
    )


def _load_primary_resources_for_retirement(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    identity: _ClickHouseWarehouseIdentity,
) -> tuple[PrivateWarehouseResource, ...]:
    expected_handles = {
        identity.project_name,
        identity.container_name,
        identity.private_network_name,
        identity.loopback_network_name,
        identity.data_volume_name,
        str(identity.binding_directory),
        str(identity.bootstrap_credential_file),
        str(identity.bootstrap_users_file),
        str(identity.host_port_file),
        str(identity.server_private_key_file),
        str(identity.server_certificate_file),
        str(identity.client_private_key_file),
        str(identity.client_certificate_file),
        str(identity.root_certificate_file),
    }
    resources = tuple(
        resource
        for resource in recorder.load_resources(binding.tenant_id, binding.binding_id)
        if resource.provider_resource_handle in expected_handles
    )
    if len(resources) != len(expected_handles):
        raise RuntimeError("ClickHouse durable resource inventory is incomplete")
    ownership = {(resource.operation_id, resource.binding_revision) for resource in resources}
    if len(ownership) != 1:
        raise RuntimeError("ClickHouse durable resource ownership changed")
    original_operation_id, original_revision = next(iter(ownership))
    if (
        original_operation_id == operation.operation_id
        or original_revision >= operation.binding_revision
    ):
        raise RuntimeError("ClickHouse durable resource ownership changed")
    return resources


def _load_resources_by_ids(
    recorder: WarehouseResourceRecorder,
    binding: WarehouseBinding,
    resource_ids: set[str],
) -> tuple[PrivateWarehouseResource, ...]:
    resources = tuple(
        resource
        for resource in recorder.load_resources(binding.tenant_id, binding.binding_id)
        if resource.resource_id in resource_ids
    )
    if len(resources) != len(resource_ids):
        raise RuntimeError("ClickHouse durable resource inventory changed")
    return resources


def _record_cleanup(
    recorder: WarehouseResourceRecorder,
    resource: PrivateWarehouseResource,
    status: WarehouseResourceCleanupStatus,
    classification: WarehouseFailureClassification | None,
) -> None:
    recorder.record_cleanup(
        resource.tenant_id,
        resource.resource_id,
        status,
        classification,
    )


def _resource_identity(resource: PrivateWarehouseResource) -> tuple[object, ...]:
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


def _stable_resource_inventory_digest(
    resources: tuple[PrivateWarehouseResource, ...],
) -> str:
    return digest(
        tuple(
            _resource_identity(resource)
            for resource in sorted(resources, key=lambda candidate: candidate.resource_id)
        )
    )


def _prepare_private_files(
    identity: _ClickHouseWarehouseIdentity,
    *,
    root: Path,
    bootstrap_password: str,
    tls_private_key: str,
    tls_certificate: str,
) -> str:
    _assert_private_directory(root)
    _ensure_private_directory(identity.binding_directory)
    private_keys = _TLSPrivateKeyBundle.model_validate_json(tls_private_key)
    certificates = _TLSCertificateBundle.model_validate_json(tls_certificate)
    credential = _write_or_recreate_private_file(
        identity.bootstrap_credential_file,
        bootstrap_password,
        preserve_existing=True,
    )
    _ensure_bootstrap_users_file(identity.bootstrap_users_file, credential)
    _write_or_load_private_file(
        identity.host_port_file,
        str(_allocate_primary_loopback_port()),
        preserve_existing=True,
    )
    _write_or_load_private_file(
        identity.server_private_key_file,
        private_keys.server_private_key_pem,
        preserve_existing=False,
    )
    _write_or_load_private_file(
        identity.server_certificate_file,
        certificates.server_certificate_pem,
        preserve_existing=False,
    )
    _write_or_load_private_file(
        identity.client_private_key_file,
        private_keys.client_private_key_pem,
        preserve_existing=False,
    )
    _write_or_load_private_file(
        identity.client_certificate_file,
        certificates.client_certificate_pem,
        preserve_existing=False,
    )
    _write_or_load_private_file(
        identity.root_certificate_file,
        certificates.ca_certificate_pem,
        preserve_existing=False,
    )
    return credential


def _bootstrap_users_configuration(password: str, *, network_enabled: bool) -> str:
    network = "::/0" if network_enabled else "0.0.0.0"
    password_digest = sha256(password.encode("utf-8")).hexdigest() if network_enabled else "0" * 64
    return (
        '<clickhouse><users><default replace="replace">'
        f"<password_sha256_hex>{password_digest}</password_sha256_hex>"
        f"<networks><ip>{network}</ip></networks>"
        "<profile>default</profile>"
        "<quota>default</quota>"
        f"<access_management>{1 if network_enabled else 0}</access_management>"
        f"<named_collection_control>{1 if network_enabled else 0}</named_collection_control>"
        f"<show_named_collections>{1 if network_enabled else 0}</show_named_collections>"
        "<show_named_collections_secrets>0</show_named_collections_secrets>"
        "</default></users></clickhouse>\n"
    )


def _ensure_bootstrap_users_file(path: Path, password: str) -> None:
    enabled = _bootstrap_users_configuration(password, network_enabled=True)
    disabled = _bootstrap_users_configuration("", network_enabled=False)
    try:
        existing = _write_or_load_private_file(
            path,
            enabled,
            preserve_existing=True,
        )
    except UnicodeError:
        raise ValueError("ClickHouse bootstrap user configuration is invalid") from None
    if existing not in {enabled, disabled}:
        raise ValueError("ClickHouse bootstrap user configuration changed across replay")


def _disable_bootstrap_user(path: Path, password: str) -> None:
    enabled = _bootstrap_users_configuration(password, network_enabled=True)
    disabled = _bootstrap_users_configuration("", network_enabled=False)
    _replace_private_text(path, expected=enabled, replacement=disabled)


def _write_or_load_private_file(
    path: Path,
    value: str,
    *,
    preserve_existing: bool,
) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    payload = value.encode("utf-8")
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _assert_private_file(path)
        existing = path.read_text(encoding="utf-8")
        if not preserve_existing and existing != value:
            raise ValueError("private ClickHouse file content changed across replay") from None
        return existing
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("private ClickHouse file write failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _assert_private_file(path)
    return value


def _write_or_recreate_private_file(
    path: Path,
    value: str,
    *,
    preserve_existing: bool,
) -> str:
    if _private_file_exists(path):
        _assert_private_file(path)
        if path.stat().st_size == 0:
            _replace_private_text(path, expected="", replacement=value)
            return value
    return _write_or_load_private_file(
        path,
        value,
        preserve_existing=preserve_existing,
    )


def _replace_private_text(path: Path, *, expected: str, replacement: str) -> None:
    _assert_private_file(path)
    descriptor = os.open(
        path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = path.lstat()
        if (
            (descriptor_status.st_dev, descriptor_status.st_ino)
            != (name_status.st_dev, name_status.st_ino)
            or stat.S_IMODE(descriptor_status.st_mode) != 0o600
            or descriptor_status.st_nlink != 1
        ):
            raise ValueError("private ClickHouse file identity changed")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as source:
            current = source.read()
        if current == replacement:
            return
        if current != expected:
            raise ValueError("private ClickHouse file content changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        payload = memoryview(replacement.encode("utf-8"))
        while payload:
            written = os.write(descriptor, payload)
            if written <= 0:
                raise OSError("private ClickHouse file replacement failed")
            payload = payload[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _assert_private_file(path)


def _load_private_text(path: Path) -> str:
    _assert_private_file(path)
    value = path.read_text(encoding="utf-8")
    if not value or any(character.isspace() for character in value):
        raise ValueError("private ClickHouse credential is invalid")
    return value


def _erase_private_file(path: Path, *, parent: Path) -> None:
    if path.parent != parent:
        raise ValueError("private ClickHouse file resource identity changed")
    _assert_private_file(path)
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        descriptor_status = os.fstat(descriptor)
        name_status = path.lstat()
        if (
            (descriptor_status.st_dev, descriptor_status.st_ino)
            != (name_status.st_dev, name_status.st_ino)
            or stat.S_IMODE(descriptor_status.st_mode) != 0o600
            or descriptor_status.st_nlink != 1
        ):
            raise ValueError("private ClickHouse file identity changed")
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _assert_private_file(path)
    if path.stat().st_size != 0:
        raise RuntimeError("private ClickHouse file erasure was not verified")


def _private_file_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _create_private_binary_file(path: Path) -> Iterator[IO[bytes]]:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    destination = os.fdopen(descriptor, "wb", closefd=True)
    try:
        yield destination
        destination.flush()
        os.fsync(destination.fileno())
    finally:
        destination.close()
    _assert_private_file(path)


@contextmanager
def _rewrite_private_binary_file(path: Path) -> Iterator[IO[bytes]]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _assert_private_file(path)
        descriptor = os.open(
            path,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        os.ftruncate(descriptor, 0)
    destination = os.fdopen(descriptor, "wb", closefd=True)
    try:
        yield destination
        destination.flush()
        os.fsync(destination.fileno())
    finally:
        destination.close()
    _assert_private_file(path)


def _create_private_tombstone(path: Path) -> None:
    try:
        with _create_private_binary_file(path):
            pass
    except FileExistsError:
        _erase_private_file(path, parent=path.parent)


def _decode_backup_key(encoded_key: str) -> bytes:
    try:
        key = b64decode(encoded_key, validate=True)
    except (Base64Error, ValueError):
        raise ValueError("ClickHouse backup encryption key is invalid") from None
    if len(key) != 32:
        raise ValueError("ClickHouse backup encryption key is invalid")
    return key


def _file_sha256(path: Path) -> str:
    _assert_private_file(path)
    hasher = sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _observed_restore_verification(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    *,
    artifact_digest: str,
    databases: tuple[str, ...],
    schema: tuple[str, ...],
    roles: tuple[str, ...],
    users: tuple[str, ...],
    markers: tuple[str, ...],
    query_result: tuple[str, ...],
    verified_at: datetime,
) -> WarehouseRestoreVerification:
    return WarehouseRestoreVerification(
        verification_id=_restore_verification_id(operation, artifact_digest),
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=EngineKind.CLICKHOUSE,
        source_backup_artifact_digest=artifact_digest,
        representative_data_digest=digest(
            {
                "domain": "clickhouse-restore-representative-data-v1",
                "markers": markers,
            }
        ),
        schema_metadata_digest=digest(
            {
                "domain": "clickhouse-restore-schema-metadata-v1",
                "databases": databases,
                "schema": schema,
            }
        ),
        principal_profile_digest=_principal_profile_digest(roles=roles, users=users),
        integrity_marker_digest=digest(
            {
                "domain": "clickhouse-restore-integrity-marker-v1",
                "markers": markers,
            }
        ),
        query_behavior_digest=digest(
            {
                "domain": "clickhouse-restore-query-behavior-v1",
                "result": query_result,
            }
        ),
        verified_at=verified_at,
    )


def _restore_verification_id(
    operation: PrivateWarehouseOperation,
    artifact_digest: str,
) -> str:
    return (
        "wrv-"
        + digest(
            {
                "domain": "clickhouse-restore-verification-v1",
                "operation_id": operation.operation_id,
                "artifact_digest": artifact_digest,
            }
        )[:24]
    )


def _load_restore_receipt(
    path: Path,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    *,
    artifact_digest: str,
    key: bytes,
) -> _ClickHouseRestoreReceipt:
    _assert_private_file(path)
    try:
        receipt = _ClickHouseRestoreReceipt.model_validate_json(
            path.read_text(encoding="utf-8"),
            strict=True,
        )
    except (OSError, UnicodeError, ValueError):
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        ) from None
    verification = receipt.verification
    if (
        verification.tenant_id != binding.tenant_id
        or verification.binding_id != binding.binding_id
        or verification.binding_revision != binding.revision
        or verification.engine_kind is not EngineKind.CLICKHOUSE
        or verification.source_backup_artifact_digest != artifact_digest
        or verification.verification_id != _restore_verification_id(operation, artifact_digest)
        or not hmac.compare_digest(
            receipt.integrity_digest,
            _restore_receipt_integrity(
                verification,
                receipt.network_isolation_probe_digest,
                key=key,
            ),
        )
    ):
        raise WarehouseProviderError(
            operation="validate",
            classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
        )
    return receipt


def _assert_private_directory(path: Path) -> None:
    status_result = path.lstat()
    if (
        not stat.S_ISDIR(status_result.st_mode)
        or stat.S_ISLNK(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o700
    ):
        raise ValueError("private ClickHouse directory is unavailable")


def _ensure_private_directory(path: Path) -> None:
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    _assert_private_directory(path)


def _assert_private_file(path: Path) -> None:
    status_result = path.lstat()
    _validate_private_file_status(status_result)


def _validate_private_file_status(status_result: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status_result.st_mode)
        or stat.S_ISLNK(status_result.st_mode)
        or stat.S_IMODE(status_result.st_mode) != 0o600
        or status_result.st_nlink != 1
    ):
        raise ValueError("private ClickHouse file is unavailable")


def _private_file_status_optional(path: Path) -> os.stat_result | None:
    try:
        status_result = path.lstat()
    except FileNotFoundError:
        return None
    _validate_private_file_status(status_result)
    return status_result


def _allocate_primary_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    if not isinstance(port, int) or not 1 <= port <= 65_535:
        raise RuntimeError("ClickHouse primary loopback port allocation failed")
    return port


def _compose_environment(
    identity: _ClickHouseWarehouseIdentity,
) -> Mapping[str, str]:
    return {
        "PILLARMESH_CLICKHOUSE_IMAGE": CLICKHOUSE_WAREHOUSE_IMAGE,
        "PILLARMESH_CLICKHOUSE_CONTAINER_NAME": identity.container_name,
        "PILLARMESH_CLICKHOUSE_PRIVATE_NETWORK_NAME": identity.private_network_name,
        "PILLARMESH_CLICKHOUSE_LOOPBACK_NETWORK_NAME": identity.loopback_network_name,
        "PILLARMESH_CLICKHOUSE_DATA_VOLUME_NAME": identity.data_volume_name,
        "PILLARMESH_CLICKHOUSE_PRIVATE_DIRECTORY": str(identity.binding_directory),
        "PILLARMESH_CLICKHOUSE_USERS_FILE": "bootstrap-users.xml",
        "PILLARMESH_CLICKHOUSE_HOST_PORT": identity.host_port_file.read_text(
            encoding="utf-8"
        ).strip(),
    }


def _restore_environment(
    primary: _ClickHouseWarehouseIdentity,
    restore: _ClickHouseRestoreIdentity,
    *,
    staging_path: Path,
    bootstrap_password: str,
) -> Mapping[str, str]:
    return {
        "PILLARMESH_CLICKHOUSE_IMAGE": CLICKHOUSE_WAREHOUSE_IMAGE,
        "PILLARMESH_CLICKHOUSE_BOOTSTRAP_PASSWORD": bootstrap_password,
        "PILLARMESH_CLICKHOUSE_CONTAINER_NAME": f"{restore.container_name}-inactive",
        "PILLARMESH_CLICKHOUSE_DATA_VOLUME_NAME": f"{restore.data_volume_name}-inactive",
        "PILLARMESH_CLICKHOUSE_HOST_PORT": "1",
        "PILLARMESH_CLICKHOUSE_PRIVATE_NETWORK_NAME": (f"{restore.private_network_name}-inactive"),
        "PILLARMESH_CLICKHOUSE_LOOPBACK_NETWORK_NAME": (
            f"{restore.loopback_network_name}-inactive"
        ),
        "PILLARMESH_CLICKHOUSE_PRIVATE_DIRECTORY": str(primary.binding_directory),
        "PILLARMESH_CLICKHOUSE_USERS_FILE": "restore-users.xml",
        "PILLARMESH_CLICKHOUSE_RESTORE_CONTAINER_NAME": restore.container_name,
        "PILLARMESH_CLICKHOUSE_RESTORE_ISOLATION_PROBE_CONTAINER_NAME": (
            restore.isolation_probe_container_name
        ),
        "PILLARMESH_CLICKHOUSE_ISOLATION_PROBE_NETWORK_NAME": primary.private_network_name,
        "PILLARMESH_CLICKHOUSE_RESTORE_PRIVATE_NETWORK_NAME": (restore.private_network_name),
        "PILLARMESH_CLICKHOUSE_RESTORE_LOOPBACK_NETWORK_NAME": (restore.loopback_network_name),
        "PILLARMESH_CLICKHOUSE_RESTORE_DATA_VOLUME_NAME": restore.data_volume_name,
    }


def _connection_target(
    identity: _ClickHouseWarehouseIdentity,
    *,
    username: str,
    password: str,
) -> ClickHouseConnectionTarget:
    port = int(identity.host_port_file.read_text(encoding="utf-8").strip())
    return ClickHouseConnectionTarget(
        endpoint=f"https://127.0.0.1:{port}",
        username=username,
        password=SecretStr(password),
        root_certificate=identity.root_certificate_file,
        client_certificate=identity.client_certificate_file,
        client_private_key=identity.client_private_key_file,
    )


def _restore_connection_target(
    primary: _ClickHouseWarehouseIdentity,
    *,
    port: int,
    username: str,
    password: str,
) -> ClickHouseConnectionTarget:
    return ClickHouseConnectionTarget(
        endpoint=f"https://127.0.0.1:{port}",
        username=username,
        password=SecretStr(password),
        root_certificate=primary.root_certificate_file,
        client_certificate=primary.client_certificate_file,
        client_private_key=primary.client_private_key_file,
    )


def _remove_restore_resources(
    compose: DockerComposeProcess,
    recorder: WarehouseResourceRecorder,
    resources: tuple[PrivateWarehouseResource, ...],
    environment: Mapping[str, str],
) -> None:
    removal_kinds: Mapping[
        WarehouseResourceKind,
        Literal["container", "network", "volume"],
    ] = {
        WarehouseResourceKind.RESTORE_CONTAINER: "container",
        WarehouseResourceKind.RESTORE_PRIVATE_NETWORK: "network",
        WarehouseResourceKind.RESTORE_DATA_VOLUME: "volume",
    }
    ordered = sorted(
        resources,
        key=lambda resource: (
            {
                WarehouseResourceKind.RESTORE_CONTAINER: 0,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK: 1,
                WarehouseResourceKind.RESTORE_DATA_VOLUME: 2,
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT: 3,
            }[resource.resource_kind],
            resource.resource_id,
        ),
    )
    failures: list[Exception] = []
    for resource in ordered:
        try:
            compose_kind = removal_kinds.get(resource.resource_kind)
            if compose_kind is not None:
                absent = compose.resource_is_absent(
                    resource_kind=compose_kind,
                    identifier=resource.provider_resource_handle,
                    environment=environment,
                )
                if absent is not True:
                    compose.remove_resource(
                        resource_kind=compose_kind,
                        identifier=resource.provider_resource_handle,
                        environment=environment,
                    )
                    absent = compose.resource_is_absent(
                        resource_kind=compose_kind,
                        identifier=resource.provider_resource_handle,
                        environment=environment,
                    )
                if absent is not True:
                    raise ComposeCommandError(
                        "ClickHouse restore resource cleanup was not verified",
                        classification="ambiguous",
                    )
        except Exception as error:
            failures.append(error)
            _attempt_record_cleanup(
                recorder,
                resource,
                WarehouseResourceCleanupStatus.FAILED,
                _classification_for_error(error),
                failures=failures,
            )
        else:
            if resource.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE:
                _attempt_record_cleanup(
                    recorder,
                    resource,
                    WarehouseResourceCleanupStatus.COMPLETE,
                    None,
                    failures=failures,
                )
    if failures:
        raise failures[0]


def _cleanup_restore_attempt(
    *,
    compose: DockerComposeProcess,
    recorder: WarehouseResourceRecorder,
    client: _Client | None,
    tunnel: _RestoreLoopbackTunnel | None,
    restore_resources: tuple[PrivateWarehouseResource, ...],
    environment: Mapping[str, str],
    staging_resource: PrivateWarehouseResource,
    staging_path: Path,
    credential_resources: tuple[tuple[PrivateWarehouseResource, Path], ...],
    parent: Path,
) -> None:
    primary_failure = sys.exception()
    cleanup_failures: list[Exception] = []
    if client is not None:
        try:
            client.close()
        except Exception as error:
            cleanup_failures.append(error)
    if tunnel is not None:
        try:
            tunnel.close()
        except Exception as error:
            cleanup_failures.append(error)
    try:
        _remove_restore_resources(compose, recorder, restore_resources, environment)
    except Exception as error:
        cleanup_failures.append(error)
    _erase_private_resource(
        recorder,
        staging_resource,
        staging_path,
        parent=parent,
        failures=cleanup_failures,
    )
    for resource, path in credential_resources:
        _erase_private_resource(
            recorder,
            resource,
            path,
            parent=parent,
            failures=cleanup_failures,
        )
    if primary_failure is None and cleanup_failures:
        raise cleanup_failures[0]


def _erase_private_resource(
    recorder: WarehouseResourceRecorder,
    resource: PrivateWarehouseResource,
    path: Path,
    *,
    parent: Path,
    failures: list[Exception],
) -> None:
    try:
        status_result = _private_file_status_optional(path)
        if status_result is None:
            if resource.cleanup_status is not WarehouseResourceCleanupStatus.COMPLETE:
                _attempt_record_cleanup(
                    recorder,
                    resource,
                    WarehouseResourceCleanupStatus.COMPLETE,
                    None,
                    failures=failures,
                )
            return
        if (
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            and status_result.st_size == 0
        ):
            return
        _erase_private_file(path, parent=parent)
    except Exception as error:
        failures.append(error)
        _attempt_record_cleanup(
            recorder,
            resource,
            WarehouseResourceCleanupStatus.FAILED,
            _classification_for_error(error),
            failures=failures,
        )
    else:
        _attempt_record_cleanup(
            recorder,
            resource,
            WarehouseResourceCleanupStatus.COMPLETE,
            None,
            failures=failures,
        )


def _attempt_record_cleanup(
    recorder: WarehouseResourceRecorder,
    resource: PrivateWarehouseResource,
    status: WarehouseResourceCleanupStatus,
    classification: WarehouseFailureClassification | None,
    *,
    failures: list[Exception],
) -> None:
    try:
        _record_cleanup(recorder, resource, status, classification)
    except Exception as error:
        failures.append(error)


def _verify_primary_container(
    compose: DockerComposeProcess,
    identity: _ClickHouseWarehouseIdentity,
    environment: Mapping[str, str],
) -> None:
    if (
        compose.inspect_container_image(
            identifier=identity.container_name,
            environment=environment,
        )
        != CLICKHOUSE_WAREHOUSE_IMAGE
    ):
        raise ComposeCommandError(
            "ClickHouse warehouse image could not be verified",
            classification="ambiguous",
        )
    container_image_id = compose.inspect_container_image_id(
        identifier=identity.container_name,
        environment=environment,
    )
    pinned_image_id = compose.inspect_image_id(
        identifier=CLICKHOUSE_WAREHOUSE_IMAGE,
        environment=environment,
    )
    if container_image_id is None or container_image_id != pinned_image_id:
        raise ComposeCommandError(
            "ClickHouse warehouse image content could not be verified",
            classification="ambiguous",
        )
    if (
        compose.inspect_container_running(
            identifier=identity.container_name,
            environment=environment,
        )
        is not True
    ):
        raise ComposeCommandError(
            "ClickHouse warehouse running state could not be verified",
            classification="ambiguous",
        )
    if (
        compose.inspect_container_has_published_ports(
            identifier=identity.container_name,
            environment=environment,
        )
        is not True
    ):
        raise ComposeCommandError(
            "ClickHouse loopback HTTPS binding could not be verified",
            classification="ambiguous",
        )
    expected_networks = tuple(
        sorted((identity.private_network_name, identity.loopback_network_name))
    )
    if (
        compose.inspect_container_networks(
            identifier=identity.container_name,
            environment=environment,
        )
        != expected_networks
    ):
        raise ComposeCommandError(
            "ClickHouse warehouse network membership could not be verified",
            classification="ambiguous",
        )
    if (
        compose.inspect_network_internal(
            identifier=identity.private_network_name,
            environment=environment,
        )
        is not True
        or compose.inspect_network_internal(
            identifier=identity.loopback_network_name,
            environment=environment,
        )
        is not False
    ):
        raise ComposeCommandError(
            "ClickHouse warehouse network isolation could not be verified",
            classification="ambiguous",
        )


def _observe_restore_isolation(
    compose: DockerComposeProcess,
    *,
    primary: _ClickHouseWarehouseIdentity,
    restore: _ClickHouseRestoreIdentity,
    primary_environment: Mapping[str, str],
    restore_environment: Mapping[str, str],
) -> str:
    primary_networks = compose.inspect_container_networks(
        identifier=primary.container_name,
        environment=primary_environment,
    )
    restore_networks = compose.inspect_container_networks(
        identifier=restore.container_name,
        environment=restore_environment,
    )
    probe_networks = compose.inspect_container_networks(
        identifier=restore.isolation_probe_container_name,
        environment=restore_environment,
    )
    expected_primary_networks = tuple(
        sorted((primary.private_network_name, primary.loopback_network_name))
    )
    expected_restore_networks = tuple(
        sorted((restore.private_network_name, restore.loopback_network_name))
    )
    restore_network_internal = (
        (
            restore.private_network_name,
            compose.inspect_network_internal(
                identifier=restore.private_network_name,
                environment=restore_environment,
            ),
        ),
        (
            restore.loopback_network_name,
            compose.inspect_network_internal(
                identifier=restore.loopback_network_name,
                environment=restore_environment,
            ),
        ),
    )
    published_ports = compose.inspect_container_has_published_ports(
        identifier=restore.container_name,
        environment=restore_environment,
    )
    probe_published_ports = compose.inspect_container_has_published_ports(
        identifier=restore.isolation_probe_container_name,
        environment=restore_environment,
    )
    restore_endpoint_ipv4_address = compose.inspect_container_network_ipv4_address(
        identifier=restore.container_name,
        network_name=restore.private_network_name,
        environment=restore_environment,
    )
    if (
        primary_networks != expected_primary_networks
        or restore_networks != expected_restore_networks
        or set(primary_networks) & set(restore_networks)
        or probe_networks != (primary.private_network_name,)
        or restore_network_internal
        != (
            (restore.private_network_name, True),
            (restore.loopback_network_name, True),
        )
        or published_ports is not False
        or probe_published_ports is not False
        or restore_endpoint_ipv4_address is None
    ):
        raise ComposeCommandError(
            "ClickHouse restore network isolation could not be verified",
            classification="ambiguous",
        )
    probe_script = (
        "if ! [[ $1 =~ ^([0-9]{1,3}\\.){3}[0-9]{1,3}$ ]]; then\n"
        "  exit 2\n"
        "elif ! command -v timeout >/dev/null 2>&1; then\n"
        "  exit 127\n"
        "elif probe_diagnostic=$("
        'timeout 3 bash -ceu \'exec 3<>/dev/tcp/$1/$2\' -- "$1" "$2" 2>&1); then\n'
        "  printf 'restore_endpoint_reachable\\n'\n"
        "else\n"
        "  probe_status=$?\n"
        '  if [ "$probe_status" -eq 1 ]; then\n'
        '    case "$probe_diagnostic" in\n'
        "      *'Connection refused'*|*'Network is unreachable'*|*'No route to host'*)\n"
        "        printf 'restore_endpoint_denied_tcp\\n'\n"
        "        ;;\n"
        '      *) exit "$probe_status" ;;\n'
        "    esac\n"
        "  else\n"
        '    exit "$probe_status"\n'
        "  fi\n"
        "fi\n"
    )
    probe_output = compose.exec(
        project_name=restore.project_name,
        arguments=(
            "exec",
            "-T",
            "clickhouse_isolation_probe",
            "bash",
            "-ceu",
            probe_script,
            "--",
            restore_endpoint_ipv4_address,
            "8443",
        ),
        environment=restore_environment,
        nonzero_classification="unavailable",
    )
    try:
        probe_observation = probe_output.decode("ascii").strip()
    except UnicodeError:
        raise ComposeCommandError(
            "ClickHouse restore denial probe was invalid",
            classification="ambiguous",
        ) from None
    if probe_observation != "restore_endpoint_denied_tcp":
        raise ComposeCommandError(
            "ClickHouse restore denial probe did not prove isolation",
            classification="ambiguous",
        )
    return digest(
        {
            "domain": "pillarmesh-clickhouse-restore-network-isolation-v4",
            "primary_networks": primary_networks,
            "restore_networks": restore_networks,
            "restore_network_internal": restore_network_internal,
            "restore_published_ports": published_ports,
            "probe_networks": probe_networks,
            "probe_published_ports": probe_published_ports,
            "restore_endpoint": (
                restore.private_network_name,
                restore_endpoint_ipv4_address,
                8443,
            ),
            "outside_probe": probe_observation,
        }
    )


def _assert_operation_ownership(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
    expected_kind: WarehouseOperationKind,
    *,
    binding_revision_offset: int = 0,
) -> None:
    if (
        binding.tenant_id != operation.tenant_id
        or binding.binding_id != operation.binding_id
        or binding.revision != operation.binding_revision + binding_revision_offset
        or binding.engine_kind is not EngineKind.CLICKHOUSE
        or operation.engine_kind is not EngineKind.CLICKHOUSE
        or operation.operation_kind is not expected_kind
    ):
        raise WarehouseProviderError(
            operation=expected_kind.value,
            classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        )


def _assert_reconciliation_ownership(
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
) -> None:
    expected_revision = {
        WarehouseBindingState.PROVISIONING: operation.binding_revision,
        WarehouseBindingState.VALIDATING: operation.binding_revision + 1,
        WarehouseBindingState.READY: operation.binding_revision + 2,
    }.get(binding.lifecycle_state)
    if (
        binding.tenant_id != operation.tenant_id
        or binding.binding_id != operation.binding_id
        or binding.revision != expected_revision
        or binding.engine_kind is not EngineKind.CLICKHOUSE
        or operation.engine_kind is not EngineKind.CLICKHOUSE
        or operation.operation_kind is not WarehouseOperationKind.PROVISION
    ):
        raise WarehouseProviderError(
            operation="reconcile",
            classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
        )


def _classification_for_error(error: Exception) -> WarehouseFailureClassification:
    if isinstance(error, WarehouseProviderError):
        return error.classification
    if isinstance(error, BackupStreamIntegrityError):
        return WarehouseFailureClassification.INTEGRITY_FAILURE
    if isinstance(error, ComposeCommandError):
        return {
            "unavailable": WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
            "timeout": WarehouseFailureClassification.TRANSIENT_TRANSPORT,
            "rejected": WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
            "ambiguous": WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
            "output_limit": WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE,
        }[error.classification]
    return WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE


def _translate_provider_error(
    operation: _ClickHouseOperation,
    error: Exception,
) -> WarehouseProviderError:
    return WarehouseProviderError(
        operation=operation,
        classification=_classification_for_error(error),
    )


def _not_implemented(operation: _ClickHouseOperation) -> WarehouseProviderError:
    return WarehouseProviderError(
        operation=operation,
        classification=WarehouseFailureClassification.PERMANENT_CONFIGURATION,
    )


__all__ = [
    "ClickHouseConnectionTarget",
    "ClickHouseGrantPlan",
    "ClickHouseHTTPClient",
    "ClickHouseWarehouseProvider",
    "assert_supported_semantics",
    "derive_clickhouse_grant_plan",
]
