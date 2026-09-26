from __future__ import annotations

import io
import json
import os
import socket
import struct
import threading
import tracemalloc
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import IO, Any, Literal, NoReturn

import heinzel_provider_postgresql.warehouse as warehouse_module
import heinzel_provider_postgresql.warehouse_database as warehouse_database
import heinzel_provider_postgresql.warehouse_protocol as warehouse_protocol
import psycopg
import pytest
from heinzel_contract_model import digest
from heinzel_provider_postgresql import (
    PostgreSQLBackupCommandBoundary,
    PostgreSQLWarehouseProvider,
)
from heinzel_provider_postgresql.warehouse import (
    PostgreSQLBackupIntegrityError,
    _assert_restore_matches_source,
    _assert_retirement_journal,
    _classification_for_error,
    _decrypt_backup_stream,
    _encrypt_backup_stream,
    _observe_restore_isolation,
    _operation_resource,
    _planned_primary_resources,
    _planned_restore_resources,
    _remove_exact_restore_resources,
    _restore_identity,
    _wait_for_primary_administration,
    _warehouse_identity,
    evaluate_mvp_fixed_capacity,
)
from heinzel_provider_postgresql.warehouse_database import (
    PostgreSQLConnectionTarget,
    PostgreSQLDatabaseObservation,
    PostgreSQLGrantPlan,
    PostgreSQLProbeCleanupError,
    _apply_grants,
    _catalog_metadata_summary,
    _grant_summary,
    _observe_engine,
    _principal_summary,
    _public_schema_summary,
    _remove_default_access,
    _require_database_denial,
    assert_password_connection_denied,
    assert_plaintext_connection_denied,
    derive_grant_plan,
    inspect_probe_logins,
    prepare_restored_database,
    probe_login_scope,
)
from heinzel_provider_postgresql.warehouse_settings import (
    POSTGRESQL_WAREHOUSE_IMAGE,
    PostgreSQLWarehouseSettings,
)
from heinzel_provider_sdk import (
    ComposeCommandError,
    ComposeErrorClassification,
    ComposeResource,
    ComposeResourceKind,
)
from heinzel_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseBackupCommandSecretCapability,
    WarehouseBackupRetirementCapability,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseLifecycleCheckpoint,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationSecretCapability,
    WarehouseOperationStatus,
    WarehouseProviderError,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository
from heinzel_warehouse_control.service import WarehouseControlService
from pydantic import SecretStr, ValidationError

_BACKUP_HEADER = struct.Struct(">8sI4s")
_BACKUP_RECORD = struct.Struct(">QBI")
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _SecretCapability:
    value: SecretStr

    def resolve(self) -> SecretStr:
        return self.value


@dataclass(frozen=True, slots=True)
class _BackupSecretCapability:
    password: SecretStr
    encryption_key: SecretStr

    def resolve_backup_restore_password(self) -> SecretStr:
        return self.password

    def resolve_backup_encryption_key(self) -> SecretStr:
        return self.encryption_key


@dataclass(slots=True)
class _BackupRetirement:
    resource_handle: str
    artifact: Path
    retired: bool = False

    def retire(self) -> None:
        if not self.artifact.is_file() or self.artifact.stat().st_size != 0:
            raise AssertionError("backup key retirement preceded artifact deletion")
        self.retired = True

    def is_retired(self) -> bool:
        return self.retired


class _SimulatedRetirementCrash(BaseException):
    pass


class _SimulatedRestoreCrash(BaseException):
    pass


class _BackupCommands:
    def __init__(self, secrets: WarehouseBackupCommandSecretCapability) -> None:
        self._secrets = secrets


class _RecordingResources:
    def __init__(self) -> None:
        self.resources: dict[str, PrivateWarehouseResource] = {}
        self.events: list[tuple[str, str]] = []
        self.cleanup_batches: list[tuple[str, ...]] = []

    def record_planned(self, resource: PrivateWarehouseResource) -> None:
        existing = self.resources.get(resource.resource_id)
        if existing is not None and existing != resource:
            raise RuntimeError("stable resource already belongs to another operation")
        self.resources[resource.resource_id] = resource
        self.events.append(("planned", resource.resource_kind.value))

    def mark_created(
        self,
        tenant_id: str,
        resource_id: str,
        provider_resource_handle: str,
    ) -> None:
        resource = self.resources[resource_id]
        assert resource.tenant_id == tenant_id
        assert resource.provider_resource_handle == provider_resource_handle
        self.resources[resource_id] = resource.model_copy(
            update={"creation_state": WarehouseResourceCreationState.CREATED}
        )
        self.events.append(("created", resource.resource_kind.value))

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None:
        resource = self.resources[resource_id]
        assert resource.tenant_id == tenant_id
        self.resources[resource_id] = resource.model_copy(
            update={"creation_state": WarehouseResourceCreationState.AMBIGUOUS}
        )
        self.events.append(("ambiguous", resource.resource_kind.value))

    def record_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        resource = self.resources[resource_id]
        assert resource.tenant_id == tenant_id
        self.resources[resource_id] = resource.model_copy(
            update={
                "cleanup_status": status,
                "cleanup_failure_classification": classification,
            }
        )
        self.events.append((status.value, resource.resource_kind.value))

    def record_cleanup_batch(self, resources: tuple[PrivateWarehouseResource, ...]) -> None:
        for resource in resources:
            assert resource.resource_id in self.resources
        self.resources.update((resource.resource_id, resource) for resource in resources)
        self.events.extend(
            (resource.cleanup_status.value, resource.resource_kind.value) for resource in resources
        )
        self.cleanup_batches.append(tuple(resource.resource_id for resource in resources))

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]:
        for expected in expected_resources:
            if self.resources.get(expected.resource_id) != expected:
                raise RuntimeError("warehouse resource durable state changed")
            if expected.cleanup_status is WarehouseResourceCleanupStatus.RETAINED:
                raise RuntimeError("retained warehouse resource cannot be recreated")
        reopened = tuple(
            expected.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.PLANNED,
                    "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
                    "cleanup_failure_classification": None,
                    "state_revision": expected.state_revision + 1,
                    "updated_at": reopened_at,
                }
            )
            for expected in expected_resources
        )
        self.resources.update((resource.resource_id, resource) for resource in reopened)
        self.events.extend(("reopened", resource.resource_kind.value) for resource in reopened)
        return reopened

    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]:
        return tuple(
            sorted(
                (
                    resource
                    for resource in self.resources.values()
                    if resource.tenant_id == tenant_id and resource.binding_id == binding_id
                ),
                key=lambda resource: (
                    resource.binding_revision,
                    resource.operation_id,
                    resource.resource_id,
                ),
            )
        )


class _RepositoryResources:
    def __init__(self, repository: SQLiteWarehouseRepository, *, binding_id: str) -> None:
        self._repository = repository
        self._binding_id = binding_id

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
        current = self._resource(tenant_id, resource_id)
        assert current.provider_resource_handle == provider_resource_handle
        self._repository.save_resource(
            current.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.CREATED,
                    "updated_at": _NOW,
                }
            )
        )

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None:
        current = self._resource(tenant_id, resource_id)
        self._repository.save_resource(
            current.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.AMBIGUOUS,
                    "updated_at": _NOW,
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
        current = self._resource(tenant_id, resource_id)
        self._repository.save_resource(
            current.model_copy(
                update={
                    "cleanup_status": status,
                    "cleanup_failure_classification": classification,
                    "updated_at": _NOW,
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


class _ComposeDouble:
    """The whole ComposeBoundary surface, raising on every call.

    Each compose stand-in below inherits this and overrides only the calls its own
    test drives. That keeps two things honest at once: a stand-in really is a
    ComposeBoundary, so the provider accepts it without a cast; and the boundary is
    pinned in exactly one place, so widening or renaming anything on it fails here
    rather than passing and reaching Docker at run time.
    """

    def _unexpected(self, call: str) -> NoReturn:
        raise AssertionError(f"{type(self).__name__} does not expect compose.{call}")

    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._unexpected("up")

    def stop(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._unexpected("stop")

    def start(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._unexpected("start")

    def down(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._unexpected("down")

    def exec(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        nonzero_classification: ComposeErrorClassification = "rejected",
    ) -> bytes:
        self._unexpected("exec")

    def exec_stream(
        self,
        *,
        project_name: str,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
        stdin: IO[bytes] | None = None,
    ) -> AbstractContextManager[IO[bytes]]:
        self._unexpected("exec_stream")

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]:
        self._unexpected("discover_resources")

    def remove_resource(
        self, *, resource_kind: ComposeResourceKind, identifier: str, environment: Mapping[str, str]
    ) -> None:
        self._unexpected("remove_resource")

    def resource_is_absent(
        self, *, resource_kind: ComposeResourceKind, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        self._unexpected("resource_is_absent")

    def inspect_container_image(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> str | None:
        self._unexpected("inspect_container_image")

    def inspect_container_running(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        self._unexpected("inspect_container_running")

    def inspect_container_has_published_ports(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        self._unexpected("inspect_container_has_published_ports")

    def inspect_container_networks(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> tuple[str, ...] | None:
        self._unexpected("inspect_container_networks")

    def inspect_network_internal(
        self, *, identifier: str, environment: Mapping[str, str]
    ) -> bool | None:
        self._unexpected("inspect_network_internal")


class _ComposeStopsAfterPlanning(_ComposeDouble):
    def __init__(
        self,
        recorder: _RecordingResources,
        *,
        failure: Exception,
    ) -> None:
        self._recorder = recorder
        self._failure = failure
        self.up_calls = 0
        self.planned_kinds_at_up: frozenset[WarehouseResourceKind] = frozenset()

    def resource_is_absent(self, **_kwargs: object) -> bool:
        return True

    def up(self, **_kwargs: object) -> None:
        self.up_calls += 1
        self.planned_kinds_at_up = frozenset(
            resource.resource_kind for resource in self._recorder.resources.values()
        )
        raise self._failure


class _BackupStreamCompose(_ComposeDouble):
    def __init__(self, recorder: _RecordingResources, payload: bytes) -> None:
        self._recorder = recorder
        self._payload = payload
        self.planned_handles_at_stream: frozenset[str] = frozenset()
        self.existing_handles_at_stream: frozenset[str] = frozenset()
        self.cleanup_statuses_at_stream: dict[str, WarehouseResourceCleanupStatus] = {}

    @contextmanager
    def exec_stream(self, **_kwargs: object) -> Any:
        self.planned_handles_at_stream = frozenset(
            resource.provider_resource_handle for resource in self._recorder.resources.values()
        )
        self.existing_handles_at_stream = frozenset(
            handle for handle in self.planned_handles_at_stream if Path(handle).exists()
        )
        self.cleanup_statuses_at_stream = {
            resource.provider_resource_handle: resource.cleanup_status
            for resource in self._recorder.resources.values()
        }
        yield io.BytesIO(self._payload)


class _RestoreStreamCompose(_ComposeDouble):
    def __init__(self, recorder: _RecordingResources) -> None:
        self._recorder = recorder
        self.planned_handles_at_stream: frozenset[str] = frozenset()
        self.existing_handles_at_stream: frozenset[str] = frozenset()
        self.arguments_at_stream: tuple[str, ...] = ()

    @contextmanager
    def exec_stream(
        self,
        *,
        arguments: tuple[str, ...],
        stdin: IO[bytes] | None = None,
        **_kwargs: object,
    ) -> Any:
        assert stdin is not None
        self.arguments_at_stream = arguments
        self.planned_handles_at_stream = frozenset(
            resource.provider_resource_handle for resource in self._recorder.resources.values()
        )
        self.existing_handles_at_stream = frozenset(
            handle for handle in self.planned_handles_at_stream if Path(handle).exists()
        )
        while stdin.read(4096):
            pass
        yield io.BytesIO()


class _DirtyRestoreReplayCompose(_ComposeDouble):
    def __init__(self, identity: Any) -> None:
        self._identity = identity
        self._present = {
            identity.container_name,
            identity.network_name,
            identity.loopback_network_name,
            identity.data_volume_name,
        }
        self.dirty_volume = True
        self.events: list[tuple[str, str]] = []

    def remove_resource(
        self,
        *,
        resource_kind: str,
        identifier: str,
        **_kwargs: object,
    ) -> None:
        self.events.append(("remove", resource_kind))
        self._present.discard(identifier)
        if resource_kind == "volume":
            self.dirty_volume = False

    def resource_is_absent(self, *, identifier: str, **_kwargs: object) -> bool:
        return identifier not in self._present

    def discover_resources(self, **_kwargs: object) -> tuple[ComposeResource, ...]:
        return ()

    def exec(self, *, arguments: tuple[str, ...], **_kwargs: object) -> bytes:
        assert arguments == ("--profile", "restore", "up", "--detach", "postgresql_restore")
        self.events.append(("up", "restore"))
        if self._present or self.dirty_volume:
            raise RuntimeError("dirty deterministic restore state was reused")
        self._present.update(
            {
                self._identity.container_name,
                self._identity.network_name,
                self._identity.loopback_network_name,
                self._identity.data_volume_name,
            }
        )
        return b""

    def inspect_container_running(self, **_kwargs: object) -> bool:
        return True

    def inspect_container_image(self, **_kwargs: object) -> str:
        return POSTGRESQL_WAREHOUSE_IMAGE

    def inspect_container_has_published_ports(self, **_kwargs: object) -> bool:
        return False


class _TwoCrashRestoreCompose(_ComposeDouble):
    def __init__(self, identity: Any, *, binding_id: str) -> None:
        self._identity = identity
        self._binding_id = binding_id
        self._present: set[str] = set()
        self.recorder: _RepositoryResources | None = None
        self.crash_after_up = False
        self.crashed = False
        self.states_at_up: list[
            tuple[
                tuple[
                    WarehouseResourceKind,
                    WarehouseResourceCreationState,
                    WarehouseResourceCleanupStatus,
                ],
                ...,
            ]
        ] = []

    @property
    def present(self) -> frozenset[str]:
        return frozenset(self._present)

    def remove_resource(
        self,
        *,
        identifier: str,
        **_kwargs: object,
    ) -> None:
        if self.crashed:
            raise _SimulatedRestoreCrash("process exited before restore cleanup")
        self._present.discard(identifier)

    def resource_is_absent(self, *, identifier: str, **_kwargs: object) -> bool:
        return identifier not in self._present

    def discover_resources(self, **_kwargs: object) -> tuple[ComposeResource, ...]:
        return ()

    def exec(self, *, arguments: tuple[str, ...], **_kwargs: object) -> bytes:
        assert arguments == ("--profile", "restore", "up", "--detach", "postgresql_restore")
        assert self.recorder is not None
        states = tuple(
            (
                resource.resource_kind,
                resource.creation_state,
                resource.cleanup_status,
            )
            for resource in self.recorder.load_resources("tenant-a", self._binding_id)
            if resource.resource_kind
            in {
                WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                WarehouseResourceKind.RESTORE_CONTAINER,
                WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                WarehouseResourceKind.RESTORE_DATA_VOLUME,
            }
        )
        self.states_at_up.append(states)
        assert len(states) == 5
        assert all(
            creation_state is WarehouseResourceCreationState.PLANNED
            and cleanup_status is WarehouseResourceCleanupStatus.PENDING
            for _kind, creation_state, cleanup_status in states
        )
        self._present.update(
            {
                self._identity.container_name,
                self._identity.network_name,
                self._identity.loopback_network_name,
                self._identity.data_volume_name,
            }
        )
        if self.crash_after_up:
            self.crashed = True
            raise _SimulatedRestoreCrash("process exited after Compose up")
        return b""

    def inspect_container_running(self, **_kwargs: object) -> bool:
        return True

    def inspect_container_image(self, **_kwargs: object) -> str:
        return POSTGRESQL_WAREHOUSE_IMAGE

    def inspect_container_has_published_ports(self, **_kwargs: object) -> bool:
        return False


class _RestoreIsolationCompose(_ComposeDouble):
    def __init__(
        self,
        *,
        primary_container: str,
        restore_container: str,
        primary_network: str,
        restore_network: str,
        primary_loopback_network: str,
        restore_loopback_network: str,
        route_failure: BaseException | None = None,
        reconnect_failure: Exception | None = None,
        restore_loopback_internal: bool = False,
        primary_running: bool = True,
    ) -> None:
        self._primary_container = primary_container
        self._restore_container = restore_container
        self._primary_network = primary_network
        self._restore_network = restore_network
        self._primary_loopback_network = primary_loopback_network
        self._restore_loopback_network = restore_loopback_network
        self._route_failure = route_failure
        self._reconnect_failure = reconnect_failure
        self._restore_loopback_internal = restore_loopback_internal
        self._primary_running = primary_running
        self.primary_loopback_attached = True
        self.network_changes: list[tuple[str, str]] = []
        self.exec_arguments: tuple[str, ...] | None = None
        self.primary_inspections = 0
        self.route_probes = 0

    def resource_is_absent(self, *, identifier: str, **_kwargs: object) -> bool:
        assert identifier == self._primary_container
        self.primary_inspections += 1
        return False

    def inspect_container_networks(self, *, identifier: str, **_kwargs: object) -> tuple[str, ...]:
        if identifier == self._primary_container:
            networks: tuple[str, ...] = (self._primary_network,)
            if self.primary_loopback_attached:
                networks = (*networks, self._primary_loopback_network)
            return tuple(sorted(networks))
        assert identifier == self._restore_container
        return tuple(sorted((self._restore_network, self._restore_loopback_network)))

    def inspect_container_running(self, *, identifier: str, **_kwargs: object) -> bool:
        assert identifier in {self._primary_container, self._restore_container}
        return self._primary_running if identifier == self._primary_container else True

    def inspect_network_internal(self, *, identifier: str, **_kwargs: object) -> bool:
        assert identifier in {
            self._primary_network,
            self._primary_loopback_network,
            self._restore_network,
            self._restore_loopback_network,
        }
        return identifier in {
            self._primary_network,
            self._restore_network,
            *((self._restore_loopback_network,) if self._restore_loopback_internal else ()),
        }

    def exec(self, *, arguments: tuple[str, ...], **_kwargs: object) -> bytes:
        assert self.primary_loopback_attached is True
        self.route_probes += 1
        self.exec_arguments = arguments
        if self._route_failure is not None:
            raise self._route_failure
        return (
            b"primary_name_route_denied\n"
            b"primary_host_route_denied\n"
            b"primary_gateway_name_route_denied\n"
            b"primary_gateway_ip_route_denied\n"
        )

    def disconnect_container_network(
        self,
        *,
        identifier: str,
        network: str,
        **_kwargs: object,
    ) -> None:
        assert identifier == self._primary_container
        assert network == self._primary_loopback_network
        self.network_changes.append(("disconnect", network))
        self.primary_loopback_attached = False

    def connect_container_network(
        self,
        *,
        identifier: str,
        network: str,
        **_kwargs: object,
    ) -> None:
        assert identifier == self._primary_container
        assert network == self._primary_loopback_network
        self.network_changes.append(("connect", network))
        if self._reconnect_failure is not None:
            raise self._reconnect_failure
        self.primary_loopback_attached = True


class _ExistingCompose(_ComposeDouble):
    def __init__(self) -> None:
        self.up_calls = 0

    def resource_is_absent(self, **_kwargs: object) -> bool:
        return False

    def inspect_container_image(self, **_kwargs: object) -> str:
        return POSTGRESQL_WAREHOUSE_IMAGE

    def inspect_container_running(self, **_kwargs: object) -> bool:
        return True

    def exec(self, **kwargs: object) -> bytes:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        return f"127.0.0.1:{environment['HEINZEL_POSTGRES_HOST_PORT']}\n".encode()

    def up(self, **_kwargs: object) -> None:
        self.up_calls += 1


class _SuspendedCompose(_ExistingCompose):
    def inspect_container_running(self, **_kwargs: object) -> bool:
        return False


class _RunningSuspendCompose(_ExistingCompose):
    def __init__(self) -> None:
        super().__init__()
        self.running = True
        self.stop_calls = 0

    def inspect_container_running(self, **_kwargs: object) -> bool:
        return self.running

    def stop(self, **_kwargs: object) -> None:
        self.stop_calls += 1
        self.running = False


class _RetirementCompose(_ComposeDouble):
    def resource_is_absent(self, **_kwargs: object) -> bool:
        return True

    def discover_resources(self, **_kwargs: object) -> tuple[ComposeResource, ...]:
        return ()

    def stop(self, **_kwargs: object) -> None:
        raise AssertionError("an absent container must not be stopped")

    def remove_resource(self, **_kwargs: object) -> None:
        raise AssertionError("a retained or absent resource must not be removed")


class _ExactRestoreCleanupCompose(_ComposeDouble):
    def __init__(self) -> None:
        self.removals: list[tuple[str, str]] = []

    def resource_is_absent(self, **_kwargs: object) -> bool:
        return False

    def remove_resource(self, *, resource_kind: str, identifier: str, **_kwargs: object) -> None:
        self.removals.append((resource_kind, identifier))


class _RecordingGrantCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any, _parameters: object = None) -> None:
        if isinstance(statement, str):
            self.statements.append(statement)
        else:
            self.statements.append(statement.as_string(None))


class _UnexpectedGrantCursor:
    def __init__(self) -> None:
        self._row: tuple[bool] | None = None

    def __enter__(self) -> _UnexpectedGrantCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[str, ...]) -> None:
        if "has_schema_privilege" in statement:
            role, target = parameters[:2]
            privilege = parameters[2] if len(parameters) == 3 else "USAGE"
            self._row = (
                role.endswith("_customer_sql") and target.endswith("_raw") and privilege == "USAGE",
            )
            return
        role, target, privilege = parameters
        allowed = (
            (
                role.endswith("_ingestion_runtime")
                and target.endswith(".ingestion_probe")
                and privilege == "INSERT"
            )
            or (
                role.endswith("_transformation_runtime")
                and target.endswith(".ingestion_probe")
                and privilege == "SELECT"
            )
            or (
                role.endswith("_backup_restore")
                and target.endswith(".ingestion_probe")
                and privilege == "SELECT"
            )
            or (
                role.endswith("_customer_sql")
                and target.endswith(".customer_probe")
                and privilege == "SELECT"
            )
            or (
                role.endswith("_bi")
                and target.endswith(".certified_probe")
                and privilege == "SELECT"
            )
        )
        self._row = (allowed,)

    def fetchone(self) -> tuple[bool]:
        assert self._row is not None
        return self._row


class _UnexpectedGrantConnection:
    def cursor(self) -> _UnexpectedGrantCursor:
        return _UnexpectedGrantCursor()


class _UnexpectedPrincipalCursor:
    def __init__(self, plan: Any) -> None:
        self._plan = plan
        self._rows: tuple[tuple[object, ...], ...] = ()

    def __enter__(self) -> _UnexpectedPrincipalCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, _parameters: object) -> None:
        if "pg_auth_members" in statement:
            self._rows = ((self._plan.role("backup_restore"), "pg_read_all_data"),)
            return
        principals = (
            "administration",
            "ingestion_runtime",
            "transformation_runtime",
            "answer_runtime",
            "backup_restore",
            "customer_sql",
            "catalog",
            "bi",
        )
        self._rows = tuple(
            (
                self._plan.role(principal),
                principal == "customer_sql",
                principal == "administration",
                principal == "administration",
                False,
                True,
                False,
                False,
            )
            for principal in principals
        )

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self._rows


class _UnexpectedPrincipalConnection:
    def __init__(self, plan: Any) -> None:
        self._plan = plan

    def cursor(self) -> _UnexpectedPrincipalCursor:
        return _UnexpectedPrincipalCursor(self._plan)


class _EngineObservationCursor:
    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self._rows = iter(rows)

    def __enter__(self) -> _EngineObservationCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _statement: str) -> None:
        return None

    def fetchone(self) -> tuple[object, ...]:
        return next(self._rows)


class _EngineObservationConnection:
    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self._rows = rows

    def cursor(self) -> _EngineObservationCursor:
        return _EngineObservationCursor(self._rows)


class _RestoredPreparationCursor:
    def __init__(self, plan: Any) -> None:
        self._plan = plan
        self.statements: list[str] = []

    def __enter__(self) -> _RestoredPreparationCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: Any, _parameters: object = None) -> None:
        if isinstance(statement, str):
            self.statements.append(statement)
        else:
            self.statements.append(statement.as_string(None))

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (
                    (self._plan.namespace("raw"), "ingestion_probe"),
                    (self._plan.namespace("conformed"), "transformation_probe"),
                    (self._plan.namespace("product"), "product_probe"),
                    (self._plan.namespace("consumption"), "customer_probe"),
                    (self._plan.namespace("consumption"), "certified_probe"),
                    (self._plan.namespace("quarantine"), "quarantine_probe"),
                    (self._plan.namespace("control"), "lifecycle_ledger"),
                    (self._plan.namespace("control"), "ingestion_append_ledger"),
                    (self._plan.unrelated_namespace, "private_probe"),
                )
            )
        )


class _RestoredPreparationConnection:
    def __init__(self, plan: Any) -> None:
        self.cursor_value = _RestoredPreparationCursor(plan)

    def cursor(self) -> _RestoredPreparationCursor:
        return self.cursor_value


class _ProbeScopeCursor:
    def __init__(self, connection: _ProbeScopeConnection) -> None:
        self._connection = connection
        self._row: tuple[object, ...] | None = None
        self._rows: tuple[tuple[object, ...], ...] = ()

    def __enter__(self) -> _ProbeScopeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: Any, _parameters: object = None) -> None:
        rendered = statement if isinstance(statement, str) else statement.as_string(None)
        if rendered.startswith("SELECT 1 FROM pg_catalog.pg_roles"):
            self._row = (1,)
            return
        if rendered.startswith("SELECT rolname FROM pg_catalog.pg_roles"):
            self._rows = tuple((role,) for role in sorted(self._connection.surviving_roles))
            return
        if not rendered.startswith("DROP ROLE IF EXISTS"):
            return
        matched_role = next(
            role for role in self._connection.expected_cleanup_roles if f'"{role}"' in rendered
        )
        self._connection.drop_attempts.append(matched_role)
        if matched_role in self._connection.surviving_roles:
            failures_remaining = self._connection.cleanup_failures_remaining
            if failures_remaining is None or failures_remaining > 0:
                if failures_remaining is not None:
                    self._connection.cleanup_failures_remaining = failures_remaining - 1
                raise RuntimeError("fault-injected probe cleanup failure")
            self._connection.surviving_roles.remove(matched_role)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self._rows


class _ProbeScopeConnection:
    def __init__(
        self,
        plan: PostgreSQLGrantPlan,
        surviving_principal: str,
        *,
        cleanup_failures_remaining: int | None = None,
    ) -> None:
        principals = (
            "administration",
            "ingestion_runtime",
            "transformation_runtime",
            "answer_runtime",
            "backup_restore",
            "customer_sql",
            "catalog",
            "bi",
        )
        self.expected_probe_roles = tuple(plan.probe_role(principal) for principal in principals)
        self.expected_cleanup_roles = (*self.expected_probe_roles, plan.administration_test_role)
        surviving_role = (
            plan.administration_test_role
            if surviving_principal == "administration_test"
            else plan.probe_role(surviving_principal)
        )
        self.surviving_roles = {surviving_role}
        self.drop_attempts: list[str] = []
        self.cleanup_failures_remaining = cleanup_failures_remaining

    def cursor(self) -> _ProbeScopeCursor:
        return _ProbeScopeCursor(self)


class _RowsCursor:
    def __init__(
        self,
        *,
        row: tuple[object, ...] | None = None,
        rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self._row = row
        self._rows = rows
        self.statements: list[str] = []

    def __enter__(self) -> _RowsCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: Any, _parameters: object = None) -> None:
        self.statements.append(
            statement if isinstance(statement, str) else statement.as_string(None)
        )

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        return self._rows


class _RowsConnection:
    def __init__(
        self,
        *,
        row: tuple[object, ...] | None = None,
        rows: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self.cursor_value = _RowsCursor(row=row, rows=rows)

    def cursor(self) -> _RowsCursor:
        return self.cursor_value


def _binding(*, tenant_id: str = "tenant-a") -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="whb-" + "1" * 24,
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capacity_profile="mvp-fixed",
        capability_profile_digest="a" * 64,
        lifecycle_state=WarehouseBindingState.PROVISIONING,
        revision=2,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _operation(
    *,
    operation_id: str = "wop-" + "2" * 24,
    tenant_id: str = "tenant-a",
) -> PrivateWarehouseOperation:
    return PrivateWarehouseOperation(
        tenant_id=tenant_id,
        binding_id="whb-" + "1" * 24,
        binding_revision=2,
        operation_id=operation_id,
        operation_kind=WarehouseOperationKind.PROVISION,
        engine_kind=EngineKind.POSTGRESQL,
        status=WarehouseOperationStatus.RUNNING,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    private_directory: Path,
    *,
    recorder: _RecordingResources,
    compose: Any,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None] = lambda _checkpoint: None,
    clock: Callable[[], datetime] = lambda: _NOW,
) -> PostgreSQLWarehouseProvider:
    private_directory.mkdir(mode=0o700, exist_ok=True)
    ordinary: WarehouseOperationSecretCapability = _SecretCapability(SecretStr("ordinary-secret"))
    tls_private_key: WarehouseOperationSecretCapability = _SecretCapability(
        SecretStr(
            json.dumps(
                {
                    "schema_version": "1",
                    "server_private_key_pem": "server-private-key",
                    "client_private_key_pem": "client-private-key",
                }
            )
        )
    )
    tls_certificate: WarehouseOperationSecretCapability = _SecretCapability(
        SecretStr(
            json.dumps(
                {
                    "schema_version": "1",
                    "ca_certificate_pem": "ca-certificate",
                    "server_certificate_pem": "server-certificate",
                    "client_certificate_pem": "client-certificate",
                }
            )
        )
    )
    backup: WarehouseBackupCommandSecretCapability = _BackupSecretCapability(
        SecretStr("backup-secret"),
        SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
    )
    return PostgreSQLWarehouseProvider(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            retention_period=timedelta(hours=1),
        ),
        compose=compose,
        resource_recorder=recorder,
        administration_secret=ordinary,
        ingestion_runtime_secret=ordinary,
        transformation_runtime_secret=ordinary,
        answer_runtime_secret=ordinary,
        customer_sql_secret=ordinary,
        catalog_secret=ordinary,
        bi_secret=ordinary,
        tls_private_key_secret=tls_private_key,
        tls_certificate_secret=tls_certificate,
        backup_commands=_BackupCommands(backup),
        connect=lambda *_args, **_kwargs: None,
        clock=clock,
        entropy=lambda size: b"e" * size,
        fault_hook=fault_hook,
    )


def _seed_created_primary_resources(
    *,
    private_directory: Path,
    recorder: _RecordingResources,
    binding: WarehouseBinding,
    operation: PrivateWarehouseOperation,
) -> tuple[PrivateWarehouseResource, ...]:
    private_directory.mkdir(mode=0o700)
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    identity.credential_file.write_text("durable-bootstrap-password", encoding="ascii")
    identity.credential_file.chmod(0o600)
    identity.host_port_file.write_text("15432\n", encoding="ascii")
    identity.host_port_file.chmod(0o600)
    planned = _planned_primary_resources(
        binding,
        operation,
        identity,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )
    created = tuple(
        resource.model_copy(
            update={
                "creation_state": WarehouseResourceCreationState.CREATED,
                "updated_at": _NOW + timedelta(seconds=1),
            }
        )
        for resource in planned
    )
    recorder.resources.update((resource.resource_id, resource) for resource in created)
    return created


def test_postgresql_warehouse_provider_exposes_the_six_operation_shape() -> None:
    assert PostgreSQLWarehouseProvider.engine_kind is EngineKind.POSTGRESQL
    for operation_name in (
        "provision",
        "reconcile",
        "validate",
        "suspend",
        "resume",
        "retire",
    ):
        assert callable(getattr(PostgreSQLWarehouseProvider, operation_name))


def test_warehouse_settings_require_an_absolute_private_directory() -> None:
    with pytest.raises(ValidationError):
        PostgreSQLWarehouseSettings(private_operation_directory=Path("relative/private"))


def test_streaming_backup_round_trip_never_allocates_the_whole_payload(tmp_path: Path) -> None:
    chunk_size = 64 * 1024
    source_path = tmp_path / "source.dump"
    encrypted_path = tmp_path / "encrypted.backup"
    restored_path = tmp_path / "restored.dump"
    source_chunk = b"deterministic-customer-fixture\x00" * 2_048
    with source_path.open("wb") as source_file:
        for _ in range(128):
            source_file.write(source_chunk)

    tracemalloc.start()
    try:
        with source_path.open("rb") as source_file, encrypted_path.open("wb") as encrypted_file:
            encryption = _encrypt_backup_stream(
                source_file,
                encrypted_file,
                key=b"k" * 32,
                chunk_size=chunk_size,
                nonce_prefix=b"abcd",
            )
        with encrypted_path.open("rb") as encrypted_file, restored_path.open("wb") as restored_file:
            decryption = _decrypt_backup_stream(
                encrypted_file,
                restored_file,
                key=b"k" * 32,
                maximum_chunk_size=chunk_size,
            )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert source_path.stat().st_size > chunk_size * 100
    assert encryption.chunk_count > 100
    assert decryption.chunk_count == encryption.chunk_count
    assert encryption.maximum_buffer_bytes <= (chunk_size * 2) + 32
    assert decryption.maximum_buffer_bytes <= (chunk_size * 2) + 32
    assert peak_bytes < chunk_size * 16
    assert restored_path.read_bytes() == source_path.read_bytes()


def test_streaming_backup_rejects_truncation_before_writing_a_complete_restore() -> None:
    encrypted = io.BytesIO()
    _encrypt_backup_stream(
        io.BytesIO(b"first chunk" * 1_024),
        encrypted,
        key=b"t" * 32,
        chunk_size=4_096,
        nonce_prefix=b"trnc",
    )
    truncated = io.BytesIO(encrypted.getvalue()[:-1])
    restored = io.BytesIO()

    with pytest.raises(PostgreSQLBackupIntegrityError):
        _decrypt_backup_stream(
            truncated,
            restored,
            key=b"t" * 32,
            maximum_chunk_size=4_096,
        )


def test_streaming_backup_rejects_reordered_authenticated_chunks() -> None:
    encrypted = io.BytesIO()
    _encrypt_backup_stream(
        io.BytesIO(b"a" * 4_096 + b"b" * 4_096 + b"c"),
        encrypted,
        key=b"r" * 32,
        chunk_size=4_096,
        nonce_prefix=b"rord",
    )
    payload = encrypted.getvalue()
    header = payload[: _BACKUP_HEADER.size]
    offset = _BACKUP_HEADER.size
    frames: list[bytes] = []
    while offset < len(payload):
        record = payload[offset : offset + _BACKUP_RECORD.size]
        _index, _final, ciphertext_size = _BACKUP_RECORD.unpack(record)
        frame_end = offset + _BACKUP_RECORD.size + ciphertext_size
        frames.append(payload[offset:frame_end])
        offset = frame_end
    reordered = io.BytesIO(header + frames[1] + frames[0] + b"".join(frames[2:]))

    with pytest.raises(PostgreSQLBackupIntegrityError):
        _decrypt_backup_stream(
            reordered,
            io.BytesIO(),
            key=b"r" * 32,
            maximum_chunk_size=4_096,
        )


def test_mvp_fixed_capacity_alert_opens_and_clears_at_fixed_thresholds() -> None:
    opened = evaluate_mvp_fixed_capacity(
        observed_bytes=85,
        capacity_bytes=100,
        alert_was_open=False,
    )
    cleared = evaluate_mvp_fixed_capacity(
        observed_bytes=74,
        capacity_bytes=100,
        alert_was_open=True,
    )

    assert (opened.alert_is_open, opened.transition) == (True, "opened")
    assert (cleared.alert_is_open, cleared.transition) == (False, "cleared")


def test_ingestion_create_is_confined_to_raw_and_control_is_append_only() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    cursor = _RecordingGrantCursor()

    _apply_grants(cursor, plan)

    raw_grant = (
        f'GRANT USAGE, CREATE ON SCHEMA "{plan.namespace("raw")}" '
        f'TO "{plan.role("ingestion_runtime")}"'
    )
    control_grant = (
        f'GRANT USAGE ON SCHEMA "{plan.namespace("control")}" TO "{plan.role("ingestion_runtime")}"'
    )
    assert raw_grant in cursor.statements
    assert control_grant in cursor.statements
    assert not any(
        "USAGE, CREATE" in statement and plan.namespace("control") in statement
        for statement in cursor.statements
    )


def test_answer_runtime_can_only_read_approved_consumption_objects() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    cursor = _RecordingGrantCursor()

    _apply_grants(cursor, plan)

    role = plan.role("answer_runtime")
    consumption = plan.namespace("consumption")
    assert f'GRANT USAGE ON SCHEMA "{consumption}" TO "{role}"' in cursor.statements
    assert f'GRANT SELECT ON "{consumption}".customer_probe TO "{role}"' in cursor.statements
    assert not any(
        statement.endswith(f'TO "{role}"')
        and any(privilege in statement for privilege in ("INSERT", "UPDATE", "DELETE", "CREATE"))
        for statement in cursor.statements
    )


def test_grant_observation_rejects_unexpected_governed_schema_access() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")

    with pytest.raises(RuntimeError, match="grant matrix"):
        _grant_summary(_UnexpectedGrantConnection(), plan)


def test_principal_observation_rejects_an_unexpected_login_role() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")

    with pytest.raises(RuntimeError, match="principal profile"):
        _principal_summary(_UnexpectedPrincipalConnection(plan), plan)


@pytest.mark.parametrize(
    ("tls_row", "database_bytes"),
    [
        ((True, "TLSv1.3", None, True), 1),
        ((True, "TLSv1.3", "TLS_AES_256_GCM_SHA384", True), 0),
    ],
)
def test_engine_observation_rejects_missing_tls_or_monitoring_values(
    tls_row: tuple[object, ...],
    database_bytes: int,
) -> None:
    connection = _EngineObservationConnection(
        (
            ("180006", "18.6"),
            tls_row,
            (0, 0, 0),
            (database_bytes,),
        )
    )

    with pytest.raises(RuntimeError, match="observation"):
        _observe_engine(connection)


def test_restore_preparation_never_recreates_or_reseeds_governed_objects() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    connection = _RestoredPreparationConnection(plan)

    prepare_restored_database(connection, plan)

    governed_mutations = tuple(
        statement
        for statement in connection.cursor_value.statements
        if statement.startswith(("CREATE SCHEMA", "CREATE TABLE", "INSERT INTO"))
    )
    assert governed_mutations == ()


def test_restore_verification_rejects_a_source_digest_mismatch() -> None:
    source = PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest="engine",
        principal_profile_digest="principal",
        namespace_grant_matrix_digest="grants",
        tls_probe_digest="tls",
        positive_probe_digest="positive",
        denial_probe_digest="denial",
        ledger_probe_digest="ledger",
        monitoring_probe_digest="monitoring",
        storage_integrity_probe_digest="storage",
        representative_data_digest="representative",
        schema_metadata_digest="schema",
        integrity_marker_digest="integrity",
        query_behavior_digest="query",
    )
    restored = replace(source, representative_data_digest="different")

    with pytest.raises(PostgreSQLBackupIntegrityError):
        _assert_restore_matches_source(source, restored)


def test_provision_records_every_stable_resource_before_compose_up(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _ComposeStopsAfterPlanning(
        recorder,
        failure=RuntimeError("stop after observing the record-before-create boundary"),
    )
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        fault_hook=checkpoints.append,
    )

    with pytest.raises(WarehouseProviderError):
        provider.provision(_binding(), _operation())

    assert compose.planned_kinds_at_up == {
        WarehouseResourceKind.COMPOSE_PROJECT,
        WarehouseResourceKind.WAREHOUSE_CONTAINER,
        WarehouseResourceKind.PRIVATE_NETWORK,
        WarehouseResourceKind.WAREHOUSE_DATA_VOLUME,
        WarehouseResourceKind.PRIVATE_DIRECTORY,
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.HOST_PORT_FILE,
        WarehouseResourceKind.TLS_PRIVATE_KEY,
        WarehouseResourceKind.TLS_CERTIFICATE,
    }
    assert (
        sum(
            resource.resource_kind is WarehouseResourceKind.PRIVATE_NETWORK
            for resource in recorder.resources.values()
        )
        == 2
    )
    private_file_kinds = {
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.HOST_PORT_FILE,
        WarehouseResourceKind.TLS_PRIVATE_KEY,
        WarehouseResourceKind.TLS_CERTIFICATE,
    }
    private_files = tuple(
        Path(resource.provider_resource_handle)
        for resource in recorder.resources.values()
        if resource.resource_kind in private_file_kinds
    )
    assert set(private_files) == {
        _warehouse_identity(_binding(), tmp_path / "private").credential_file,
        _warehouse_identity(_binding(), tmp_path / "private").host_port_file,
        _warehouse_identity(_binding(), tmp_path / "private").hba_configuration,
        _warehouse_identity(_binding(), tmp_path / "private").tls_private_key,
        _warehouse_identity(_binding(), tmp_path / "private").client_private_key,
        _warehouse_identity(_binding(), tmp_path / "private").tls_certificate,
        _warehouse_identity(_binding(), tmp_path / "private").client_certificate,
        _warehouse_identity(_binding(), tmp_path / "private").root_certificate,
    }
    assert all(path.is_file() for path in private_files)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in private_files)
    assert checkpoints == [WarehouseLifecycleCheckpoint.AFTER_RESOURCE_PLAN]


def test_provision_replay_completes_a_partial_durable_plan_before_create(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    expected = _planned_primary_resources(
        binding,
        operation,
        identity,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )
    recorder = _RecordingResources()
    for resource in expected[:3]:
        recorder.record_planned(resource)
    compose = _ComposeStopsAfterPlanning(
        recorder,
        failure=RuntimeError("stop after the recovered plan is complete"),
    )

    with pytest.raises(WarehouseProviderError):
        _provider(private_directory, recorder=recorder, compose=compose).provision(
            binding,
            operation,
        )

    assert compose.up_calls == 1
    assert tuple(sorted(recorder.resources)) == tuple(
        sorted(resource.resource_id for resource in expected)
    )


def test_stable_resource_handles_never_embed_tenant_text(tmp_path: Path) -> None:
    tenant_canary = 'tenant"; DROP ROLE administration; --'
    recorder = _RecordingResources()
    compose = _ComposeStopsAfterPlanning(recorder, failure=RuntimeError("planned"))
    provider = _provider(tmp_path / "private", recorder=recorder, compose=compose)

    with pytest.raises(WarehouseProviderError):
        provider.provision(
            _binding(tenant_id=tenant_canary),
            _operation(tenant_id=tenant_canary),
        )

    private_identifiers = tuple(
        value
        for resource in recorder.resources.values()
        for value in (resource.resource_id, resource.provider_resource_handle)
    )
    assert private_identifiers
    assert all(tenant_canary not in value for value in private_identifiers)


def test_competing_operation_cannot_adopt_the_recorded_project(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _ComposeStopsAfterPlanning(recorder, failure=RuntimeError("stop after planning"))
    provider = _provider(tmp_path / "private", recorder=recorder, compose=compose)
    binding = _binding()

    with pytest.raises(WarehouseProviderError):
        provider.provision(binding, _operation())
    with pytest.raises(WarehouseProviderError) as captured:
        provider.provision(binding, _operation(operation_id="wop-" + "3" * 24))

    assert compose.up_calls == 1
    assert captured.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE


def test_provider_reconcile_reconstructs_the_same_inventory_after_process_restart(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _ExistingCompose()
    binding = _binding()
    operation = _operation()
    created = _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=binding,
        operation=operation,
    )
    first_provider = _provider(private_directory, recorder=recorder, compose=compose)
    first_provider._primary_resources.update(
        (resource.resource_id, resource) for resource in created
    )
    expected = first_provider.reconcile(binding, operation)
    restarted_provider = _provider(private_directory, recorder=recorder, compose=compose)

    replayed = restarted_provider.reconcile(binding, operation)

    assert replayed == expected
    assert compose.up_calls == 0


def test_provider_reconcile_admits_the_original_operation_after_ready_revision(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _ExistingCompose()
    binding = _binding()
    operation = _operation()
    _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=binding,
        operation=operation,
    )
    ready = binding.model_copy(
        update={
            "lifecycle_state": WarehouseBindingState.READY,
            "revision": operation.binding_revision + 2,
        }
    )

    result = _provider(private_directory, recorder=recorder, compose=compose).reconcile(
        ready,
        operation,
    )

    assert result.operation_id == operation.operation_id
    assert result.resource_inventory_digest
    assert compose.up_calls == 0


def test_reconcile_adopts_a_completed_suspend_without_repeating_stop(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _SuspendedCompose()
    provisioning = _binding()
    _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=provisioning,
        operation=_operation(),
    )
    ready = provisioning.model_copy(
        update={"lifecycle_state": WarehouseBindingState.READY, "revision": 4}
    )
    suspend_operation = _operation(operation_id="wop-" + "4" * 24).model_copy(
        update={
            "binding_revision": ready.revision,
            "operation_kind": WarehouseOperationKind.SUSPEND,
        }
    )

    result = _provider(
        private_directory,
        recorder=recorder,
        compose=compose,
    ).reconcile(ready, suspend_operation)

    assert result.operation_id == suspend_operation.operation_id
    assert (
        result.private_resource_handle
        == _warehouse_identity(ready, private_directory).private_resource_handle
    )


def test_reconcile_completes_suspend_when_claim_preceded_the_effect(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _RunningSuspendCompose()
    provisioning = _binding()
    _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=provisioning,
        operation=_operation(),
    )
    ready = provisioning.model_copy(
        update={"lifecycle_state": WarehouseBindingState.READY, "revision": 4}
    )
    suspend_operation = _operation(operation_id="wop-" + "4" * 24).model_copy(
        update={
            "binding_revision": ready.revision,
            "operation_kind": WarehouseOperationKind.SUSPEND,
        }
    )

    _provider(
        private_directory,
        recorder=recorder,
        compose=compose,
    ).reconcile(ready, suspend_operation)

    assert compose.running is False
    assert compose.stop_calls == 1


def test_provider_provision_replay_uses_durable_resources_after_restart(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _ExistingCompose()
    binding = _binding()
    operation = _operation()
    created = _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=binding,
        operation=operation,
    )
    first_provider = _provider(private_directory, recorder=recorder, compose=compose)
    first_provider._primary_resources.update(
        (resource.resource_id, resource) for resource in created
    )
    expected = first_provider.reconcile(binding, operation)
    restarted_provider = _provider(private_directory, recorder=recorder, compose=compose)

    replayed = restarted_provider.provision(binding, operation)

    assert replayed == expected
    assert compose.up_calls == 0


def test_provider_reconcile_rejects_a_competing_operation_before_compose_inspection(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _ExistingCompose()
    binding = _binding()
    original_operation = _operation()
    _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=binding,
        operation=original_operation,
    )
    restarted_provider = _provider(private_directory, recorder=recorder, compose=compose)
    competing_operation = _operation(operation_id="wop-" + "3" * 24)

    with pytest.raises(WarehouseProviderError) as captured:
        restarted_provider.reconcile(binding, competing_operation)

    assert captured.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert compose.up_calls == 0


def test_provider_retirement_reloads_the_original_primary_inventory_after_restart(
    tmp_path: Path,
) -> None:
    class _StrictRetirementResources(_RecordingResources):
        def record_cleanup(
            self,
            tenant_id: str,
            resource_id: str,
            status: WarehouseResourceCleanupStatus,
            classification: WarehouseFailureClassification | None,
        ) -> None:
            if self.resources[resource_id].cleanup_status is (
                WarehouseResourceCleanupStatus.COMPLETE
            ):
                raise AssertionError("terminal cleanup cannot be rewritten")
            super().record_cleanup(tenant_id, resource_id, status, classification)

    class _DeletionCompose(_RetirementCompose):
        def remove_resource(self, **kwargs: object) -> None:
            assert kwargs["resource_kind"] == "volume"

    private_directory = tmp_path / "private"
    recorder = _StrictRetirementResources()
    clock_value = [_NOW]
    provision_binding = _binding()
    provision_operation = _operation()
    original_resources = _seed_created_primary_resources(
        private_directory=private_directory,
        recorder=recorder,
        binding=provision_binding,
        operation=provision_operation,
    )
    private_file_kinds = {
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.HOST_PORT_FILE,
        WarehouseResourceKind.TLS_PRIVATE_KEY,
        WarehouseResourceKind.TLS_CERTIFICATE,
    }
    for resource in original_resources:
        path = Path(resource.provider_resource_handle)
        if resource.resource_kind in private_file_kinds and not path.exists():
            path.write_bytes(b"private-tombstone-source")
            path.chmod(0o600)
    retiring = provision_binding.model_copy(
        update={
            "lifecycle_state": WarehouseBindingState.RETIRING,
            "revision": provision_operation.binding_revision + 3,
        }
    )
    retirement_operation = provision_operation.model_copy(
        update={
            "binding_revision": retiring.revision,
            "operation_id": "wop-" + "4" * 24,
            "operation_kind": WarehouseOperationKind.RETIRE,
        }
    )
    checkpoints: list[WarehouseLifecycleCheckpoint] = []

    provider = _provider(
        private_directory,
        recorder=recorder,
        compose=_DeletionCompose(),
        fault_hook=checkpoints.append,
        clock=lambda: clock_value[0],
    )
    evidence = provider.retire(retiring, retirement_operation)

    active_kinds = {
        WarehouseResourceKind.COMPOSE_PROJECT,
        WarehouseResourceKind.WAREHOUSE_CONTAINER,
        WarehouseResourceKind.PRIVATE_NETWORK,
    }
    expected_completed = sum(
        resource.resource_kind in active_kinds for resource in original_resources
    )
    assert evidence.completed_resource_count == expected_completed
    assert evidence.retained_resource_count == len(original_resources) - expected_completed
    assert evidence.cleanup_failed_resource_count == 0
    assert checkpoints == [WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION]

    clock_value[0] += timedelta(hours=2)
    retired = retiring.model_copy(
        update={"lifecycle_state": WarehouseBindingState.RETIRED, "revision": 7}
    )
    deletion_operation = retirement_operation.model_copy(
        update={"binding_revision": retired.revision, "operation_id": "wop-" + "7" * 24}
    )

    deletion = provider.retire(retired, deletion_operation)

    assert deletion.retained_resource_count == 0
    failed_resources = tuple(
        resource
        for resource in recorder.resources.values()
        if resource.cleanup_status is WarehouseResourceCleanupStatus.FAILED
    )
    assert (
        tuple(
            (resource.resource_kind, resource.provider_resource_handle)
            for resource in failed_resources
        )
        == ()
    )
    assert deletion.cleanup_failed_resource_count == len(failed_resources)


def test_provider_sanitizes_compose_diagnostics_and_classifies_unavailability(
    tmp_path: Path,
) -> None:
    diagnostic_canary = "docker-daemon-private-diagnostic-canary"
    recorder = _RecordingResources()
    compose = _ComposeStopsAfterPlanning(
        recorder,
        failure=ComposeCommandError(diagnostic_canary, classification="unavailable"),
    )
    provider = _provider(tmp_path / "private", recorder=recorder, compose=compose)

    with pytest.raises(WarehouseProviderError) as captured:
        provider.provision(_binding(), _operation())

    error = captured.value
    assert error.operation == "provision"
    assert error.classification is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    assert str(error) == "warehouse provider provision failed: transient_unavailable"
    assert diagnostic_canary not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_backup_capability_is_confined_to_the_backup_command_boundary(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    recorder = _RecordingResources()
    compose = _ComposeStopsAfterPlanning(recorder, failure=RuntimeError("unused"))
    backup_secrets: WarehouseBackupCommandSecretCapability = _BackupSecretCapability(
        SecretStr("backup-secret"),
        SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
    )
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=compose,
        resource_recorder=recorder,
        secrets=backup_secrets,
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=private_directory / "unused-backup",
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
        entropy=lambda size: b"b" * size,
    )
    ordinary: WarehouseOperationSecretCapability = _SecretCapability(SecretStr("ordinary-secret"))
    provider = PostgreSQLWarehouseProvider(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=compose,
        resource_recorder=recorder,
        administration_secret=ordinary,
        ingestion_runtime_secret=ordinary,
        transformation_runtime_secret=ordinary,
        answer_runtime_secret=ordinary,
        customer_sql_secret=ordinary,
        catalog_secret=ordinary,
        bi_secret=ordinary,
        tls_private_key_secret=ordinary,
        tls_certificate_secret=ordinary,
        backup_commands=boundary,
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )

    assert not hasattr(boundary, "resolve_backup_restore_password")
    assert not hasattr(boundary, "resolve_backup_encryption_key")
    assert all(value is not backup_secrets for value in vars(provider).values())


def test_backup_records_passfile_and_staging_file_before_creation(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    recorder = _RecordingResources()
    compose = _BackupStreamCompose(recorder, b"backup-row\n" * 1024)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            backup_chunk_bytes=4 * 1024,
        ),
        compose=compose,
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
        entropy=lambda size: b"b" * size,
        fault_hook=checkpoints.append,
    )

    created_artifact, _ = boundary._create_backup(
        binding=binding,
        operation=operation,
        identity=identity,
        environment={},
        plan=derive_grant_plan(identity.private_resource_handle),
        password="backup-secret",
        encryption_key=b"k" * 32,
    )

    passfile = identity.binding_directory / "backup.pgpass"
    staging = identity.binding_directory / "warehouse.pgdump.aead.incomplete"
    assert compose.planned_handles_at_stream.issuperset(
        {str(artifact), str(passfile), str(staging)}
    )
    assert compose.existing_handles_at_stream.issuperset({str(passfile), str(staging)})
    assert compose.cleanup_statuses_at_stream[str(passfile)] is (
        WarehouseResourceCleanupStatus.PENDING
    )
    assert compose.cleanup_statuses_at_stream[str(staging)] is (
        WarehouseResourceCleanupStatus.PENDING
    )
    assert created_artifact == artifact
    assert artifact.is_file()
    assert passfile.is_file()
    assert passfile.stat().st_size == 0
    assert staging.is_file()
    assert staging.stat().st_size == 0
    temporary_resources = tuple(
        resource
        for resource in recorder.resources.values()
        if resource.provider_resource_handle in {str(passfile), str(staging)}
    )
    assert {resource.resource_kind for resource in temporary_resources} == {
        WarehouseResourceKind.CREDENTIAL_FILE,
        WarehouseResourceKind.BACKUP_STAGING_FILE,
    }
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in temporary_resources
    )
    assert checkpoints == [
        WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED,
        WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED,
    ]


def test_backup_cleanup_attempts_every_file_without_replacing_the_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    recorder = _RecordingResources()
    primary_failure = RuntimeError("primary backup stream failed")
    stream_failed = Event()

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            stream_failed.set()
            raise primary_failure
            yield io.BytesIO()  # pragma: no cover

    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            backup_chunk_bytes=4 * 1024,
        ),
        compose=Compose(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
        entropy=lambda size: b"b" * size,
    )
    real_delete = PostgreSQLBackupCommandBoundary._delete_ephemeral_file
    cleanup_attempts: list[str] = []

    def fail_cleanup(
        self: PostgreSQLBackupCommandBoundary,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        if not stream_failed.is_set():
            real_delete(self, resource, binding_directory=binding_directory)
            return
        cleanup_attempts.append(Path(resource.provider_resource_handle).name)
        raise OSError(f"cleanup failed for {resource.resource_kind.value}")

    monkeypatch.setattr(PostgreSQLBackupCommandBoundary, "_delete_ephemeral_file", fail_cleanup)

    with pytest.raises(RuntimeError) as captured:
        boundary._create_backup(
            binding=binding,
            operation=operation,
            identity=identity,
            environment={},
            plan=derive_grant_plan(identity.private_resource_handle),
            password="backup-secret",
            encryption_key=b"k" * 32,
        )

    assert captured.value is primary_failure
    assert cleanup_attempts == ["backup.pgpass", "warehouse.pgdump.aead.incomplete"]


def test_existing_backup_preparation_attempts_every_exact_temporary_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    with artifact.open("wb") as destination:
        _encrypt_backup_stream(
            io.BytesIO(b"existing-backup\n" * 1024),
            destination,
            key=b"k" * 32,
            chunk_size=4 * 1024,
            nonce_prefix=b"n" * 4,
        )
    artifact.chmod(0o600)
    recorder = _RecordingResources()
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            backup_chunk_bytes=4 * 1024,
        ),
        compose=_ComposeDouble(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )
    real_delete = PostgreSQLBackupCommandBoundary._delete_ephemeral_file
    first_failure = OSError("first existing-artifact cleanup failed")
    attempts: list[str] = []

    def fail_first_cleanup(
        self: PostgreSQLBackupCommandBoundary,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        name = Path(resource.provider_resource_handle).name
        attempts.append(name)
        if name == "backup.pgpass":
            raise first_failure
        real_delete(self, resource, binding_directory=binding_directory)

    monkeypatch.setattr(
        PostgreSQLBackupCommandBoundary,
        "_delete_ephemeral_file",
        fail_first_cleanup,
    )

    with pytest.raises(OSError) as captured:
        boundary._create_backup(
            binding=binding,
            operation=operation,
            identity=identity,
            environment={},
            plan=derive_grant_plan(identity.private_resource_handle),
            password="backup-secret",
            encryption_key=b"k" * 32,
        )

    assert captured.value is first_failure
    assert attempts == ["backup.pgpass", "warehouse.pgdump.aead.incomplete"]


def test_new_backup_preparation_attempts_every_exact_temporary_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    recorder = _RecordingResources()
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=_ComposeDouble(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )
    real_prepare = PostgreSQLBackupCommandBoundary._prepare_ephemeral_file
    first_failure = OSError("first pre-create cleanup failed")
    attempts: list[str] = []

    def fail_first_preparation(
        self: PostgreSQLBackupCommandBoundary,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        name = Path(resource.provider_resource_handle).name
        attempts.append(name)
        if name == "backup.pgpass":
            raise first_failure
        real_prepare(self, resource, binding_directory=binding_directory)

    monkeypatch.setattr(
        PostgreSQLBackupCommandBoundary,
        "_prepare_ephemeral_file",
        fail_first_preparation,
    )

    with pytest.raises(OSError) as captured:
        boundary._create_backup(
            binding=binding,
            operation=operation,
            identity=identity,
            environment={},
            plan=derive_grant_plan(identity.private_resource_handle),
            password="backup-secret",
            encryption_key=b"k" * 32,
        )

    assert captured.value is first_failure
    assert attempts == ["backup.pgpass", "warehouse.pgdump.aead.incomplete"]


def test_private_file_cleanup_never_deletes_an_unrecorded_sibling(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    identity.credential_file.write_text("recorded", encoding="ascii")
    identity.credential_file.chmod(0o600)
    identity.client_private_key.write_text("unrecorded", encoding="ascii")
    identity.client_private_key.chmod(0o600)
    resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle=str(identity.credential_file),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=_NOW,
    ).model_copy(update={"creation_state": WarehouseResourceCreationState.CREATED})
    recorder = _RecordingResources()
    recorder.resources[resource.resource_id] = resource
    provider = _provider(private_directory, recorder=recorder, compose=_ComposeDouble())
    provider._primary_resources[resource.resource_id] = resource

    provider._delete_retained_resource(resource, identity, {})

    assert identity.credential_file.is_file()
    assert identity.credential_file.stat().st_size == 0
    assert identity.client_private_key.read_text(encoding="ascii") == "unrecorded"
    assert identity.binding_directory.is_dir()
    assert recorder.resources[resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.COMPLETE
    )


@pytest.mark.parametrize("replacement_kind", ("symlink", "regular"))
def test_private_file_cleanup_fails_closed_before_erasure_when_recorded_name_swaps_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: Literal["symlink", "regular"],
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    identity.credential_file.write_text("recorded", encoding="ascii")
    identity.credential_file.chmod(0o600)
    unrelated = identity.binding_directory / "unrelated"
    unrelated.write_text("unrelated", encoding="ascii")
    unrelated.chmod(0o600)
    preserved = identity.binding_directory / "recorded-preserved"
    resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle=str(identity.credential_file),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=_NOW,
    ).model_copy(update={"creation_state": WarehouseResourceCreationState.CREATED})
    recorder = _RecordingResources()
    recorder.resources[resource.resource_id] = resource
    provider = _provider(private_directory, recorder=recorder, compose=_ComposeDouble())
    provider._primary_resources[resource.resource_id] = resource
    real_lstat = os.lstat
    recorded_lstat_calls = 0

    def swap_before_verification(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        # The os.lstat overload the code under test uses: it calls lstat both bare and
        # with dir_fd, never on a file descriptor, and only the dir_fd call is the one
        # this fault targets.
        nonlocal recorded_lstat_calls
        if Path(os.fsdecode(path)).name == identity.credential_file.name and dir_fd is not None:
            recorded_lstat_calls += 1
            if recorded_lstat_calls == 2:
                identity.credential_file.rename(preserved)
                if replacement_kind == "symlink":
                    identity.credential_file.symlink_to(unrelated)
                else:
                    unrelated.rename(identity.credential_file)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", swap_before_verification)

    provider._delete_retained_resource(resource, identity, {})

    assert recorded_lstat_calls >= 2
    assert preserved.read_text(encoding="ascii") == "recorded"
    if replacement_kind == "symlink":
        assert identity.credential_file.is_symlink()
        assert unrelated.read_text(encoding="ascii") == "unrelated"
    else:
        assert identity.credential_file.read_text(encoding="ascii") == "unrelated"
        assert unrelated.exists() is False
    assert recorder.resources[resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.FAILED
    )


def test_private_file_cleanup_fails_closed_on_a_final_regular_file_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    identity.credential_file.write_text("recorded", encoding="ascii")
    identity.credential_file.chmod(0o600)
    replacement = identity.binding_directory / "replacement"
    replacement.write_text("replacement", encoding="ascii")
    replacement.chmod(0o600)
    erased_recorded_file = identity.binding_directory / "recorded-erased"
    resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle=str(identity.credential_file),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=_NOW,
    ).model_copy(update={"creation_state": WarehouseResourceCreationState.CREATED})
    recorder = _RecordingResources()
    recorder.resources[resource.resource_id] = resource
    provider = _provider(private_directory, recorder=recorder, compose=_ComposeDouble())
    provider._primary_resources[resource.resource_id] = resource
    real_same_inode = warehouse_module._same_inode
    comparisons = 0

    def swap_after_the_last_pre_unlink_check(
        first: Any,
        second: Any,
    ) -> bool:
        nonlocal comparisons
        comparisons += 1
        matches = real_same_inode(first, second)
        if comparisons == 4:
            identity.credential_file.rename(erased_recorded_file)
            replacement.rename(identity.credential_file)
        return matches

    monkeypatch.setattr(warehouse_module, "_same_inode", swap_after_the_last_pre_unlink_check)

    provider._delete_retained_resource(resource, identity, {})

    assert comparisons >= 4
    assert identity.credential_file.read_text(encoding="ascii") == "replacement"
    assert erased_recorded_file.is_file()
    assert erased_recorded_file.stat().st_size == 0
    assert recorder.resources[resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.FAILED
    )


def test_restore_records_passfile_before_creation_and_deletes_its_exact_resource(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    with artifact.open("wb") as destination:
        _encrypt_backup_stream(
            io.BytesIO(b"restore-row\n" * 1024),
            destination,
            key=b"k" * 32,
            chunk_size=4 * 1024,
            nonce_prefix=b"n" * 4,
        )
    artifact.chmod(0o600)
    recorder = _RecordingResources()
    compose = _RestoreStreamCompose(recorder)
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            backup_chunk_bytes=4 * 1024,
        ),
        compose=compose,
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )

    boundary._stream_restore(
        binding=binding,
        operation=operation,
        project_name="pm-restore",
        environment={},
        identity=identity,
        artifact=artifact,
        encryption_key=b"k" * 32,
        restore_password="restore-password",
    )

    passfile = identity.binding_directory / "restore.pgpass"
    assert str(passfile) in compose.planned_handles_at_stream
    assert str(passfile) in compose.existing_handles_at_stream
    assert passfile.is_file()
    assert passfile.stat().st_size == 0
    assert compose.arguments_at_stream[:3] == ("--profile", "restore", "exec")
    assert "postgresql_restore" in compose.arguments_at_stream
    resource = next(
        resource
        for resource in recorder.resources.values()
        if resource.provider_resource_handle == str(passfile)
    )
    assert resource.resource_kind is WarehouseResourceKind.CREDENTIAL_FILE
    assert resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE


@pytest.mark.parametrize("failure_kind", ("process", "integrity"))
def test_restore_cleanup_never_replaces_process_or_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: Literal["process", "integrity"],
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    if failure_kind == "integrity":
        artifact.write_bytes(b"truncated-encrypted-backup")
    else:
        with artifact.open("wb") as destination:
            _encrypt_backup_stream(
                io.BytesIO(b"restore-row\n" * 1024),
                destination,
                key=b"k" * 32,
                chunk_size=4 * 1024,
                nonce_prefix=b"n" * 4,
            )
    artifact.chmod(0o600)
    recorder = _RecordingResources()
    primary_failure = RuntimeError("primary restore process failed")
    restore_finished = Event()

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, *, stdin: IO[bytes] | None = None, **_kwargs: object) -> Any:
            # The provider always streams into compose; the boundary allows omitting
            # stdin, so a stand-in that reads it says so instead of assuming it.
            assert stdin is not None
            if failure_kind == "process":
                restore_finished.set()
                raise primary_failure
            while stdin.read(4096):
                pass
            try:
                yield io.BytesIO()
            finally:
                restore_finished.set()

    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            backup_chunk_bytes=4 * 1024,
        ),
        compose=Compose(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )
    real_delete = PostgreSQLBackupCommandBoundary._delete_ephemeral_file
    cleanup_attempts: list[str] = []

    def fail_cleanup(
        self: PostgreSQLBackupCommandBoundary,
        resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        if not restore_finished.is_set():
            real_delete(self, resource, binding_directory=binding_directory)
            return
        cleanup_attempts.append(Path(resource.provider_resource_handle).name)
        raise OSError("restore passfile cleanup failed")

    monkeypatch.setattr(PostgreSQLBackupCommandBoundary, "_delete_ephemeral_file", fail_cleanup)

    expected_error = RuntimeError if failure_kind == "process" else PostgreSQLBackupIntegrityError
    with pytest.raises(expected_error) as captured:
        boundary._stream_restore(
            binding=binding,
            operation=operation,
            project_name="pm-restore",
            environment={},
            identity=identity,
            artifact=artifact,
            encryption_key=b"k" * 32,
            restore_password="restore-password",
        )

    if failure_kind == "process":
        assert captured.value is primary_failure
    assert cleanup_attempts == ["restore.pgpass"]


def test_restore_integrity_failure_precedes_the_causal_process_failure_and_is_sanitized(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"truncated-encrypted-backup")
    artifact.chmod(0o600)
    recorder = _RecordingResources()
    process_canary = "private-pg-restore-statement-detail"

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, *, stdin: IO[bytes] | None = None, **_kwargs: object) -> Any:
            # The provider always streams into compose; the boundary allows omitting
            # stdin, so a stand-in that reads it says so instead of assuming it.
            assert stdin is not None
            while stdin.read(4096):
                pass
            raise RuntimeError(process_canary)
            yield io.BytesIO()  # pragma: no cover

    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=Compose(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )

    with pytest.raises(PostgreSQLBackupIntegrityError) as captured:
        boundary._stream_restore(
            binding=binding,
            operation=operation,
            project_name="pm-restore",
            environment={},
            identity=identity,
            artifact=artifact,
            encryption_key=b"k" * 32,
            restore_password="restore-password",
        )

    assert _classification_for_error(captured.value) is (
        WarehouseFailureClassification.INTEGRITY_FAILURE
    )
    public_error = warehouse_module._translate_provider_error("validate", captured.value)
    assert public_error.classification is WarehouseFailureClassification.INTEGRITY_FAILURE
    assert str(public_error) == "warehouse provider validate failed: integrity_failure"
    assert process_canary not in repr(public_error)
    assert public_error.__cause__ is None
    assert public_error.__context__ is None


def test_restore_preserves_an_unrelated_process_failure_recorded_before_integrity() -> None:
    process_failure = RuntimeError("unrelated process failure")
    integrity_failure = PostgreSQLBackupIntegrityError()

    selected = warehouse_module._select_restore_stream_failure(
        warehouse_module._OrderedRestoreFailure(order=1, error=process_failure),
        warehouse_module._OrderedRestoreFailure(order=2, error=integrity_failure),
        decrypt_worker_alive=False,
    )

    assert selected is process_failure


def test_restore_preserves_a_prior_nonprocess_primary_while_integrity_and_cleanup_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"truncated-encrypted-backup")
    artifact.chmod(0o600)
    recorder = _RecordingResources()
    prior_failure = _SimulatedRestoreCrash("prior controller interruption")

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            raise prior_failure
            yield io.BytesIO()  # pragma: no cover

    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=Compose(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
    )
    cleanup_attempted = Event()

    def fail_cleanup(
        _self: PostgreSQLBackupCommandBoundary,
        _resource: PrivateWarehouseResource,
        *,
        binding_directory: Path,
    ) -> None:
        assert binding_directory == identity.binding_directory
        cleanup_attempted.set()
        raise OSError("private cleanup detail")

    monkeypatch.setattr(
        PostgreSQLBackupCommandBoundary,
        "_delete_ephemeral_file",
        fail_cleanup,
    )

    with pytest.raises(_SimulatedRestoreCrash) as captured:
        boundary._stream_restore(
            binding=binding,
            operation=operation,
            project_name="pm-restore",
            environment={},
            identity=identity,
            artifact=artifact,
            encryption_key=b"k" * 32,
            restore_password="restore-password",
        )

    assert captured.value is prior_failure
    assert cleanup_attempted.is_set()


def test_incomplete_restore_replay_removes_exact_dirty_state_before_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    primary = _warehouse_identity(binding, private_directory)
    primary.binding_directory.mkdir(mode=0o700)
    restore = _restore_identity(binding, operation)
    recorder = _RecordingResources()
    restore_resources = _planned_restore_resources(
        binding,
        operation,
        restore,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )
    for resource in restore_resources:
        recorder.resources[resource.resource_id] = resource.model_copy(
            update={"creation_state": WarehouseResourceCreationState.CREATED}
        )
    compose = _DirtyRestoreReplayCompose(restore)
    artifact = primary.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"encrypted-backup-placeholder")
    artifact.chmod(0o600)
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(private_operation_directory=private_directory),
        compose=compose,
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=_BackupRetirement(
            resource_handle="whs-test#backup-encryption-key",
            artifact=artifact,
        ),
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW,
        entropy=lambda size: b"r" * size,
        fault_hook=checkpoints.append,
    )
    boundary._load_durable_resources(binding)

    class Tunnel:
        port = 55432

        def __init__(self, **_kwargs: object) -> None:
            return None

        def start(self, **_kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

        def assert_healthy(self) -> None:
            return None

    class Connection:
        autocommit = False

        def close(self) -> None:
            return None

    observation = PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest="engine",
        principal_profile_digest="principal",
        namespace_grant_matrix_digest="grants",
        tls_probe_digest="tls",
        positive_probe_digest="positive",
        denial_probe_digest="denial",
        ledger_probe_digest="ledger",
        monitoring_probe_digest="monitoring",
        storage_integrity_probe_digest="storage",
        representative_data_digest="representative",
        schema_metadata_digest="schema",
        integrity_marker_digest="integrity",
        query_behavior_digest="query",
    )

    def stream_restore(*_args: object, **_kwargs: object) -> None:
        assert compose.dirty_volume is False
        compose.events.append(("stream", "restore"))

    monkeypatch.setattr(warehouse_module, "_RestoreLoopbackTunnel", Tunnel)
    monkeypatch.setattr(
        warehouse_module,
        "_observe_restore_isolation",
        lambda *_args, **_kwargs: "isolated",
    )
    monkeypatch.setattr(
        warehouse_module,
        "wait_for_tls_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(
        warehouse_module,
        "create_canonical_roles",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        warehouse_module,
        "prepare_restored_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        warehouse_module,
        "observe_restored_database",
        lambda *_args, **_kwargs: observation,
    )
    monkeypatch.setattr(PostgreSQLBackupCommandBoundary, "_stream_restore", stream_restore)

    restored, _cleanup_digest, _isolation_digest = boundary._restore_backup(
        binding=binding,
        operation=operation,
        identity=primary,
        primary_environment={"scope": "primary"},
        artifact=artifact,
        artifact_digest="a" * 64,
        encryption_key=b"k" * 32,
        plan=derive_grant_plan(primary.private_resource_handle),
    )

    assert restored == observation
    assert compose.events[:5] == [
        ("remove", "container"),
        ("remove", "network"),
        ("remove", "network"),
        ("remove", "volume"),
        ("up", "restore"),
    ]
    assert checkpoints == [
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_VERIFIED,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_CLEANUP,
    ]


def test_restore_two_crash_replay_durably_reopens_every_exact_resource_before_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "warehouse.sqlite"
    repository = SQLiteWarehouseRepository(str(database_path))
    control = WarehouseControlService(repository, clock=lambda: _NOW)
    draft_binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capacity_profile="mvp-fixed",
    )
    binding = draft_binding.model_copy(
        update={
            "lifecycle_state": WarehouseBindingState.PROVISIONING,
            "revision": 2,
            "updated_at": _NOW,
        }
    )
    operation = _operation().model_copy(update={"binding_id": binding.binding_id})
    repository.save(binding)
    claimed_operation = operation.model_copy(update={"status": WarehouseOperationStatus.CLAIMED})
    assert repository.claim_operation(claimed_operation)
    repository.save_operation(claimed_operation, operation)
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    primary = _warehouse_identity(binding, private_directory)
    primary.binding_directory.mkdir(mode=0o700)
    artifact = primary.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"encrypted-backup-placeholder")
    artifact.chmod(0o600)
    restore = _restore_identity(binding, operation)
    compose = _TwoCrashRestoreCompose(restore, binding_id=binding.binding_id)
    observation = PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest="engine",
        principal_profile_digest="principal",
        namespace_grant_matrix_digest="grants",
        tls_probe_digest="tls",
        positive_probe_digest="positive",
        denial_probe_digest="denial",
        ledger_probe_digest="ledger",
        monitoring_probe_digest="monitoring",
        storage_integrity_probe_digest="storage",
        representative_data_digest="representative",
        schema_metadata_digest="schema",
        integrity_marker_digest="integrity",
        query_behavior_digest="query",
    )

    class Tunnel:
        port = 55432

        def __init__(self, **_kwargs: object) -> None:
            return None

        def start(self, **_kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

        def assert_healthy(self) -> None:
            return None

    class Connection:
        autocommit = False

        def close(self) -> None:
            return None

    monkeypatch.setattr(warehouse_module, "_RestoreLoopbackTunnel", Tunnel)
    monkeypatch.setattr(
        warehouse_module,
        "_observe_restore_isolation",
        lambda *_args, **_kwargs: "isolated",
    )
    monkeypatch.setattr(
        warehouse_module,
        "wait_for_tls_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    monkeypatch.setattr(warehouse_module, "create_canonical_roles", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        warehouse_module,
        "prepare_restored_database",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        warehouse_module,
        "observe_restored_database",
        lambda *_args, **_kwargs: observation,
    )
    monkeypatch.setattr(
        PostgreSQLBackupCommandBoundary,
        "_stream_restore",
        lambda *_args, **_kwargs: None,
    )

    def restore_with(current_repository: SQLiteWarehouseRepository) -> None:
        recorder = _RepositoryResources(current_repository, binding_id=binding.binding_id)
        compose.recorder = recorder
        boundary = PostgreSQLBackupCommandBoundary(
            settings=PostgreSQLWarehouseSettings(
                private_operation_directory=private_directory,
            ),
            compose=compose,
            resource_recorder=recorder,
            secrets=_BackupSecretCapability(
                SecretStr("backup-secret"),
                SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
            ),
            retirement=_BackupRetirement(
                resource_handle="whs-test#backup-encryption-key",
                artifact=artifact,
            ),
            connect=lambda *_args, **_kwargs: None,
            clock=lambda: _NOW,
            entropy=lambda size: b"r" * size,
        )
        boundary._load_durable_resources(binding)
        boundary._restore_backup(
            binding=binding,
            operation=operation,
            identity=primary,
            primary_environment={"scope": "primary"},
            artifact=artifact,
            artifact_digest="a" * 64,
            encryption_key=b"k" * 32,
            plan=derive_grant_plan(primary.private_resource_handle),
        )

    restore_with(repository)
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in repository.load_resources(binding.tenant_id, binding.binding_id)
    )
    repository.close()

    replay_repository = SQLiteWarehouseRepository(str(database_path))
    compose.crash_after_up = True
    with pytest.raises(_SimulatedRestoreCrash):
        restore_with(replay_repository)
    restore_resources = tuple(
        resource
        for resource in replay_repository.load_resources(binding.tenant_id, binding.binding_id)
        if resource.resource_kind
        in {
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
    )
    assert len(restore_resources) == 5
    assert all(
        resource.creation_state is WarehouseResourceCreationState.PLANNED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.PENDING
        and resource.cleanup_failure_classification is None
        for resource in restore_resources
    )
    assert compose.present == frozenset(
        {
            restore.container_name,
            restore.network_name,
            restore.loopback_network_name,
            restore.data_volume_name,
        }
    )
    replay_repository.close()

    final_repository = SQLiteWarehouseRepository(str(database_path))
    compose.crash_after_up = False
    compose.crashed = False
    restore_with(final_repository)
    final_resources = tuple(
        resource
        for resource in final_repository.load_resources(binding.tenant_id, binding.binding_id)
        if resource.resource_kind
        in {
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
    )
    assert len(final_resources) == 5
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in final_resources
    )
    assert compose.present == frozenset()


def test_restore_isolation_is_observed_while_the_primary_remains_running(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    primary = _warehouse_identity(binding, private_directory)
    restore = _restore_identity(binding, operation)
    primary.binding_directory.mkdir(mode=0o700)
    primary.host_port_file.write_text("15432\n", encoding="ascii")
    primary.host_port_file.chmod(0o600)
    compose = _RestoreIsolationCompose(
        primary_container=primary.container_name,
        restore_container=restore.container_name,
        primary_network=primary.network_name,
        restore_network=restore.network_name,
        primary_loopback_network=primary.loopback_network_name,
        restore_loopback_network=restore.loopback_network_name,
        restore_loopback_internal=True,
    )

    observed = _observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={"scope": "primary"},
        restore_environment={"scope": "restore"},
    )

    assert observed == digest(
        {
            "domain": "heinzel-postgresql-running-primary-restore-isolation-v4",
            "primary_running": True,
            "primary_networks": tuple(
                sorted((primary.network_name, primary.loopback_network_name))
            ),
            "restore_networks": tuple(
                sorted((restore.network_name, restore.loopback_network_name))
            ),
            "internal_restore_networks": (
                restore.network_name,
                restore.loopback_network_name,
            ),
            "route_observations": (
                "primary_name_route_denied",
                "primary_host_route_denied",
                "primary_gateway_name_route_denied",
                "primary_gateway_ip_route_denied",
            ),
        }
    )
    assert compose.primary_inspections == 1
    assert compose.network_changes == []
    assert compose.exec_arguments is not None
    assert primary.container_name in compose.exec_arguments
    assert "15432" in compose.exec_arguments


def test_restore_isolation_route_failure_never_mutates_the_primary(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    primary = _warehouse_identity(binding, private_directory)
    restore = _restore_identity(binding, operation)
    primary.binding_directory.mkdir(mode=0o700)
    primary.host_port_file.write_text("15432\n", encoding="ascii")
    primary.host_port_file.chmod(0o600)
    route_failure = RuntimeError("route observation failed")
    compose = _RestoreIsolationCompose(
        primary_container=primary.container_name,
        restore_container=restore.container_name,
        primary_network=primary.network_name,
        restore_network=restore.network_name,
        primary_loopback_network=primary.loopback_network_name,
        restore_loopback_network=restore.loopback_network_name,
        route_failure=route_failure,
        restore_loopback_internal=True,
    )

    with pytest.raises(RuntimeError) as captured:
        _observe_restore_isolation(
            compose,
            primary=primary,
            restore=restore,
            primary_environment={"scope": "primary"},
            restore_environment={"scope": "restore"},
        )

    assert captured.value is route_failure
    assert compose.primary_loopback_attached is True
    assert compose.network_changes == []


def test_restore_isolation_uses_repeatable_restore_topology_without_mutating_primary(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    primary = _warehouse_identity(binding, private_directory)
    restore = _restore_identity(binding, operation)
    primary.binding_directory.mkdir(mode=0o700)
    primary.host_port_file.write_text("15432\n", encoding="ascii")
    primary.host_port_file.chmod(0o600)
    compose = _RestoreIsolationCompose(
        primary_container=primary.container_name,
        restore_container=restore.container_name,
        primary_network=primary.network_name,
        restore_network=restore.network_name,
        primary_loopback_network=primary.loopback_network_name,
        restore_loopback_network=restore.loopback_network_name,
        restore_loopback_internal=True,
    )

    before_stream = _observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={"scope": "primary"},
        restore_environment={"scope": "restore"},
    )
    after_validation = _observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={"scope": "primary"},
        restore_environment={"scope": "restore"},
    )

    assert before_stream == after_validation
    assert compose.primary_loopback_attached is True
    assert compose.network_changes == []
    assert compose.route_probes == 2


def test_restore_loopback_tunnel_forwards_without_adding_a_container_route() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(
            self,
            *,
            project_name: str,
            arguments: tuple[str, ...],
            stdin: IO[bytes] | None = None,
            **_kwargs: object,
        ) -> Any:
            assert stdin is not None
            calls.append((project_name, arguments))
            assert stdin.read(4) == b"ping"
            yield io.BytesIO(b"pong")

    tunnel_type = warehouse_module._RestoreLoopbackTunnel
    tunnel = tunnel_type(compose=Compose(), project_name="restore-project")
    try:
        tunnel.start(environment={"scope": "restore"})
        with socket.create_connection(("127.0.0.1", tunnel.port), timeout=2) as client:
            client.sendall(b"ping")
            client.shutdown(socket.SHUT_WR)

            assert client.recv(4) == b"pong"
        tunnel.assert_healthy()
    finally:
        tunnel.close()

    assert len(calls) == 1
    assert calls[0][0] == "restore-project"
    assert calls[0][1][:7] == (
        "--profile",
        "restore",
        "exec",
        "-T",
        "postgresql_restore",
        "bash",
        "-ceu",
    )


def test_restore_tunnel_start_failure_never_leaves_cleanup_waiting_for_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedThread:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def join(self, timeout: float) -> None:
            assert timeout > 0

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(threading, "Thread", FailedThread)
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=_ComposeDouble(),
        project_name="restore-project",
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        tunnel.start(environment={"scope": "restore"})

    close_failures: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as error:
            close_failures.append(error)

    close_thread = Thread(target=close_tunnel, daemon=True)
    close_thread.start()
    # Generous: the assertion below is that the thread ended without the test
    # releasing it, not that it ended quickly. A tight join turns a loaded host
    # into a failure, and the release path below still covers a genuine hang.
    close_thread.join(timeout=10)
    terminated_without_test_release = not close_thread.is_alive()
    if close_thread.is_alive():
        with tunnel._condition:
            tunnel._server_stopped = True
            tunnel._condition.notify_all()
        close_thread.join(timeout=2)

    assert terminated_without_test_release
    assert close_failures == []


def test_restore_tunnel_close_joins_a_handler_accepted_during_server_shutdown() -> None:
    accepted = Event()
    release_accept = Event()
    handler_started = Event()
    server_socket, client_socket = socket.socketpair()

    class Listener:
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            accepted.set()
            assert release_accept.wait(timeout=2)
            return server_socket, ("127.0.0.1", 0)

        def close(self) -> None:
            return None

        def shutdown(self, _how: int = socket.SHUT_RDWR) -> None:
            release_accept.set()

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, *, stdin: IO[bytes] | None = None, **_kwargs: object) -> Any:
            # The provider always streams into compose; the boundary allows omitting
            # stdin, so a stand-in that reads it says so instead of assuming it.
            assert stdin is not None
            handler_started.set()
            while stdin.read(4096):
                pass
            yield io.BytesIO()

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    tunnel._listener = Listener()  # type: ignore[assignment]
    tunnel._environment = {"scope": "restore"}
    real_server_thread = Thread(target=tunnel._serve, daemon=True)

    class ServerThreadJoinGate:
        def join(self, timeout: float) -> None:
            release_accept.set()
            real_server_thread.join(timeout=timeout)

        def is_alive(self) -> bool:
            return real_server_thread.is_alive()

    tunnel._server_thread = ServerThreadJoinGate()  # type: ignore[assignment]
    real_server_thread.start()
    assert accepted.wait(timeout=2)
    close_failure: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as error:
            close_failure.append(error)

    close_thread = Thread(target=close_tunnel, daemon=True)
    close_thread.start()
    try:
        close_thread.join(timeout=2)
        assert not close_thread.is_alive()
        assert close_failure == []
        assert handler_started.wait(timeout=2)
        assert tunnel._handler_threads
        assert all(not handler.is_alive() for handler in tunnel._handler_threads)
        handlers_at_return = tuple(tunnel._handler_threads)
        assert not real_server_thread.is_alive()
        assert tuple(tunnel._handler_threads) == handlers_at_return
    finally:
        with suppress(OSError):
            client_socket.shutdown(socket.SHUT_RDWR)
        client_socket.close()
        release_accept.set()
        real_server_thread.join(timeout=2)
        for handler in tunnel._handler_threads:
            handler.join(timeout=2)
        close_thread.join(timeout=2)


def test_restore_tunnel_close_cancels_a_server_ignoring_initial_socket_close() -> None:
    accept_started = Event()
    cancel_accept = Event()

    class Listener:
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            accept_started.set()
            assert cancel_accept.wait(timeout=10)
            raise OSError("listener cancelled")

        def shutdown(self, _how: int = socket.SHUT_RDWR) -> None:
            cancel_accept.set()

        def close(self) -> None:
            return None

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=_ComposeDouble(),
        project_name="restore-project",
    )
    tunnel._listener = Listener()  # type: ignore[assignment]
    tunnel._environment = {"scope": "restore"}
    server_thread = Thread(target=tunnel._serve, daemon=True)
    tunnel._server_thread = server_thread
    server_thread.start()
    assert accept_started.wait(timeout=2)
    close_failures: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as error:
            close_failures.append(error)

    close_thread = Thread(target=close_tunnel, daemon=True)
    close_thread.start()

    try:
        close_thread.join(timeout=10)
        terminated_without_test_release = not close_thread.is_alive()
    finally:
        cancel_accept.set()
        server_thread.join(timeout=2)
        close_thread.join(timeout=2)

    assert terminated_without_test_release
    assert close_failures == []
    assert not server_thread.is_alive()


def test_restore_tunnel_close_cancels_a_handler_ignoring_initial_socket_shutdown() -> None:
    read_started = Event()
    cancel_input = Event()
    shutdown_attempted = Event()

    class BlockingInput:
        def __enter__(self) -> BlockingInput:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, _size: int = -1) -> bytes:
            read_started.set()
            assert cancel_input.wait(timeout=10)
            return b""

        def close(self) -> None:
            cancel_input.set()

    blocking_input = BlockingInput()

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def makefile(self, _mode: str) -> BlockingInput:
            return blocking_input

        def shutdown(self, _how: int) -> None:
            shutdown_attempted.set()

        def close(self) -> None:
            return None

        def sendall(self, _value: bytes) -> None:
            return None

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, *, stdin: IO[bytes] | None = None, **_kwargs: object) -> Any:
            # The provider always streams into compose; the boundary allows omitting
            # stdin, so a stand-in that reads it says so instead of assuming it.
            assert stdin is not None
            while stdin.read(4096):
                pass
            yield io.BytesIO()

    client = Client()
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    tunnel._environment = {"scope": "restore"}
    handler = Thread(target=tunnel._forward, args=(client,), daemon=True)
    # _clients is a set[socket.socket], a concrete class, so this partial stand-in
    # cannot be typed as one however faithfully it behaves.
    tunnel._clients.add(client)  # type: ignore[arg-type]
    tunnel._handler_threads.append(handler)
    handler.start()
    assert read_started.wait(timeout=2)
    close_failures: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as error:
            close_failures.append(error)

    close_thread = Thread(target=close_tunnel, daemon=True)
    close_thread.start()

    try:
        close_thread.join(timeout=10)
        terminated_without_test_release = not close_thread.is_alive()
    finally:
        cancel_input.set()
        handler.join(timeout=2)
        close_thread.join(timeout=2)

    assert terminated_without_test_release
    assert close_failures == []
    assert shutdown_attempted.is_set()
    assert not handler.is_alive()


def test_restore_tunnel_keeps_a_validated_response_after_rejected_bridge_teardown() -> None:
    allow_bridge_exit = Event()
    bridge_exited = Event()
    private_canary = "private-provider-rejection-detail"
    payload = b"SFATAL\0VFATAL\0C28000\0M" + private_canary.encode("ascii") + b"\0\0"
    response = b"E" + struct.pack("!I", len(payload) + 4) + payload

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, *, stdin: IO[bytes] | None = None, **_kwargs: object) -> Any:
            # The provider always streams into compose; the boundary allows omitting
            # stdin, so a stand-in that reads it says so instead of assuming it.
            assert stdin is not None
            request_size = struct.unpack("!I", stdin.read(4))[0]
            assert len(stdin.read(request_size - 4)) == request_size - 4
            yield io.BytesIO(response)
            assert allow_bridge_exit.wait(timeout=2)
            bridge_exited.set()
            raise ComposeCommandError(
                "Docker Compose command failed",
                classification="rejected",
            )

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    try:
        tunnel.start(environment={"scope": "restore"})
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification) as captured:
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )
        assert private_canary not in str(captured.value)
        allow_bridge_exit.set()
        assert bridge_exited.wait(timeout=2)
        for handler in tuple(tunnel._handler_threads):
            handler.join(timeout=2)
            assert not handler.is_alive()

        tunnel.assert_healthy()
    finally:
        allow_bridge_exit.set()
        with suppress(ComposeCommandError):
            tunnel.close()


@pytest.mark.parametrize("classification", ("rejected", "unavailable", "timeout"))
def test_restore_tunnel_pre_response_failures_remain_fatal_and_sanitized(
    classification: Literal["rejected", "unavailable", "timeout"],
) -> None:
    private_canary = "private-probe-credential"

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            raise ComposeCommandError(
                "Docker Compose command failed",
                classification=classification,
            )
            yield io.BytesIO()  # pragma: no cover

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    try:
        tunnel.start(environment={"scope": "restore"})
        with pytest.raises(psycopg.OperationalError) as captured:
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password=private_canary,
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )

        assert type(captured.value) is psycopg.OperationalError
        assert str(captured.value) == "PostgreSQL denial probe transport failed"
        assert private_canary not in str(captured.value)
    finally:
        with suppress(ComposeCommandError):
            tunnel.close()


def test_restore_tunnel_pre_response_rejection_is_fatal_without_poisoning_retry() -> None:
    attempts = 0
    payload = b"SFATAL\0VFATAL\0C28000\0Mexplicit authorization denial\0\0"
    response = b"E" + struct.pack("!I", len(payload) + 4) + payload

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ComposeCommandError(
                    "Docker Compose command failed",
                    classification="rejected",
                )
            yield io.BytesIO(response)

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    try:
        tunnel.start(environment={"scope": "restore"})
        with pytest.raises(psycopg.OperationalError):
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )
        for handler in tuple(tunnel._handler_threads):
            handler.join(timeout=2)
            assert not handler.is_alive()

        tunnel.assert_healthy()
    finally:
        with suppress(ComposeCommandError):
            tunnel.close()


@pytest.mark.parametrize("classification", ("unavailable", "timeout"))
def test_restore_tunnel_control_failure_remains_sticky_after_a_later_response(
    classification: Literal["unavailable", "timeout"],
) -> None:
    attempts = 0
    payload = b"SFATAL\0VFATAL\0C28000\0Mexplicit authorization denial\0\0"
    response = b"E" + struct.pack("!I", len(payload) + 4) + payload

    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ComposeCommandError(
                    "Docker Compose command failed",
                    classification=classification,
                )
            yield io.BytesIO(response)

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    try:
        tunnel.start(environment={"scope": "restore"})
        with pytest.raises(psycopg.OperationalError):
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )
        for handler in tuple(tunnel._handler_threads):
            handler.join(timeout=2)
            assert not handler.is_alive()

        with pytest.raises(ComposeCommandError) as captured:
            tunnel.assert_healthy()
        assert captured.value.classification == classification
    finally:
        with suppress(ComposeCommandError):
            tunnel.close()


@pytest.mark.parametrize(
    "response",
    (
        b"E" + struct.pack("!I", 3),
        b"E" + struct.pack("!I", 32) + b"truncated-private-response",
    ),
)
def test_restore_tunnel_malformed_or_incomplete_response_remains_fatal_and_sanitized(
    response: bytes,
) -> None:
    class Compose(_ComposeDouble):
        @contextmanager
        def exec_stream(self, **_kwargs: object) -> Any:
            yield io.BytesIO(response)

    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=Compose(),
        project_name="restore-project",
    )
    try:
        tunnel.start(environment={"scope": "restore"})
        with pytest.raises(psycopg.OperationalError) as captured:
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="private-probe-credential",
                host="127.0.0.1",
                port=tunnel.port,
                sslmode="disable",
                connect_timeout=2,
            )

        assert type(captured.value) is psycopg.OperationalError
        assert str(captured.value) == "PostgreSQL denial probe transport failed"
        assert "private" not in str(captured.value)
    finally:
        tunnel.close()


def test_restore_isolation_rejects_a_stopped_primary_without_running_evidence(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    primary = _warehouse_identity(binding, private_directory)
    restore = _restore_identity(binding, operation)
    primary.binding_directory.mkdir(mode=0o700)
    primary.host_port_file.write_text("15432\n", encoding="ascii")
    primary.host_port_file.chmod(0o600)
    compose = _RestoreIsolationCompose(
        primary_container=primary.container_name,
        restore_container=restore.container_name,
        primary_network=primary.network_name,
        restore_network=restore.network_name,
        primary_loopback_network=primary.loopback_network_name,
        restore_loopback_network=restore.loopback_network_name,
        primary_running=False,
        restore_loopback_internal=True,
    )

    with pytest.raises(RuntimeError, match="running"):
        _observe_restore_isolation(
            compose,
            primary=primary,
            restore=restore,
            primary_environment={"scope": "primary"},
            restore_environment={"scope": "restore"},
        )

    assert compose.exec_arguments is None
    assert compose.network_changes == []


def test_restore_cleanup_removes_only_the_exact_recorded_resource_identities() -> None:
    binding = _binding()
    operation = _operation()
    restore = _restore_identity(binding, operation)
    resources = _planned_restore_resources(
        binding,
        operation,
        restore,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )
    compose = _ExactRestoreCleanupCompose()

    _remove_exact_restore_resources(compose, resources=resources, environment={})

    assert compose.removals == [
        ("container", restore.container_name),
        ("network", restore.network_name),
        ("network", restore.loopback_network_name),
        ("volume", restore.data_volume_name),
    ]


def _retirement_boundary(
    *,
    private_directory: Path,
    recorder: _RecordingResources,
    retirement: WarehouseBackupRetirementCapability,
    checkpoint: Any | None = None,
    clock: Callable[[], datetime] = lambda: _NOW + timedelta(hours=2),
) -> PostgreSQLBackupCommandBoundary:
    return PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            retention_period=timedelta(hours=1),
        ),
        compose=_ComposeDouble(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=retirement,
        retirement_checkpoint=checkpoint,
        connect=lambda *_args, **_kwargs: None,
        clock=clock,
        entropy=lambda size: b"b" * size,
    )


def test_backup_retirement_plans_its_journal_before_terminal_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TerminalLedger(_RecordingResources):
        terminal = False

        def record_planned(self, resource: PrivateWarehouseResource) -> None:
            if self.terminal:
                raise AssertionError("terminal binding cannot add a resource")
            super().record_planned(resource)

        def record_cleanup_batch(
            self,
            resources: tuple[PrivateWarehouseResource, ...],
        ) -> None:
            if self.terminal and any(
                self.resources[resource.resource_id].cleanup_status
                is WarehouseResourceCleanupStatus.COMPLETE
                for resource in resources
            ):
                raise AssertionError("terminal cleanup cannot be rewritten")
            super().record_cleanup_batch(resources)

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"encrypted-backup")
    artifact.chmod(0o600)
    retirement = _BackupRetirement(
        resource_handle="whs-stable#backup-encryption-key",
        artifact=artifact,
    )
    recorder = _TerminalLedger()
    deadline = operation.started_at + timedelta(hours=1)
    artifact_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
        provider_resource_handle=str(artifact),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    key_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
        provider_resource_handle=retirement.resource_handle,
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    for resource in (artifact_resource, key_resource):
        recorder.record_planned(resource)
        recorder.mark_created(
            resource.tenant_id,
            resource.resource_id,
            resource.provider_resource_handle,
        )
    clock = [_NOW]
    boundary = _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=retirement,
        clock=lambda: clock[0],
    )

    boundary.retire_backup(binding=binding)

    journal = next(
        resource
        for resource in recorder.resources.values()
        if resource.resource_kind is WarehouseResourceKind.BACKUP_RETIREMENT_JOURNAL
    )
    assert journal.creation_state is WarehouseResourceCreationState.CREATED
    assert journal.cleanup_status is WarehouseResourceCleanupStatus.RETAINED

    recorder.terminal = True
    clock[0] += timedelta(hours=2)
    real_delete = warehouse_module._delete_recorded_private_file
    failures = 1

    def fail_once(path: Path, *, binding_directory: Path) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("injected retained deletion failure")
        real_delete(path, binding_directory=binding_directory)

    monkeypatch.setattr(warehouse_module, "_delete_recorded_private_file", fail_once)
    retired = binding.model_copy(
        update={"lifecycle_state": WarehouseBindingState.RETIRED, "revision": 7}
    )

    with pytest.raises(RuntimeError, match="injected retained deletion failure"):
        boundary.retire_backup(binding=retired)

    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
        for resource in recorder.resources.values()
    )
    boundary.retire_backup(binding=retired)
    boundary.retire_backup(binding=retired)

    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in recorder.resources.values()
    )


def test_backup_retirement_refreshes_repository_owned_resource_revisions(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    repository = SQLiteWarehouseRepository(str(tmp_path / "warehouse-state.sqlite3"))
    control = WarehouseControlService(repository, clock=lambda: _NOW)
    draft = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capacity_profile="mvp-fixed",
    )
    binding = draft.model_copy(
        update={
            "lifecycle_state": WarehouseBindingState.PROVISIONING,
            "revision": 2,
            "updated_at": _NOW,
        }
    )
    repository.save(binding)
    operation = _operation().model_copy(update={"binding_id": binding.binding_id})
    claimed_operation = operation.model_copy(update={"status": WarehouseOperationStatus.CLAIMED})
    assert repository.claim_operation(claimed_operation)
    repository.save_operation(claimed_operation, operation)
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"encrypted-backup")
    artifact.chmod(0o600)
    retirement = _BackupRetirement(
        resource_handle="whs-stable#backup-encryption-key",
        artifact=artifact,
    )
    recorder = _RepositoryResources(repository, binding_id=binding.binding_id)
    deadline = operation.started_at + timedelta(hours=1)
    resources = (
        _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
            provider_resource_handle=str(artifact),
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=deadline,
        ),
        _operation_resource(
            binding,
            operation,
            resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            provider_resource_handle=retirement.resource_handle,
            parent_resource_handle=identity.private_resource_handle,
            retention_deadline=deadline,
        ),
    )
    for resource in resources:
        recorder.record_planned(resource)
        recorder.mark_created(
            resource.tenant_id,
            resource.resource_id,
            resource.provider_resource_handle,
        )
    boundary = PostgreSQLBackupCommandBoundary(
        settings=PostgreSQLWarehouseSettings(
            private_operation_directory=private_directory,
            retention_period=timedelta(hours=1),
        ),
        compose=_ComposeDouble(),
        resource_recorder=recorder,
        secrets=_BackupSecretCapability(
            SecretStr("backup-secret"),
            SecretStr("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="),
        ),
        retirement=retirement,
        connect=lambda *_args, **_kwargs: None,
        clock=lambda: _NOW + timedelta(hours=2),
        entropy=lambda size: b"b" * size,
    )

    boundary.retire_backup(binding=binding)

    durable = repository.load_resources(binding.tenant_id, binding.binding_id)
    assert durable
    assert all(resource.state_revision > 0 for resource in durable)
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE for resource in durable
    )


def test_ephemeral_file_replay_reopens_completed_cleanup_before_recreation(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    path = private_directory / "restore.pgpass"
    path.touch(mode=0o600)
    resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.CREDENTIAL_FILE,
        provider_resource_handle=str(path),
        parent_resource_handle=str(private_directory),
        retention_deadline=operation.started_at + timedelta(hours=1),
    ).model_copy(
        update={
            "creation_state": WarehouseResourceCreationState.CREATED,
            "cleanup_status": WarehouseResourceCleanupStatus.COMPLETE,
            "state_revision": 2,
        }
    )
    recorder = _RecordingResources()
    recorder.resources[resource.resource_id] = resource
    boundary = _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=_BackupRetirement("unused", path),
    )
    boundary._resources[resource.resource_id] = resource

    boundary._prepare_ephemeral_file(resource, binding_directory=private_directory)

    reopened = recorder.resources[resource.resource_id]
    assert reopened.creation_state is WarehouseResourceCreationState.PLANNED
    assert reopened.cleanup_status is WarehouseResourceCleanupStatus.PENDING
    assert reopened.cleanup_failure_classification is None


@pytest.mark.parametrize(
    "crash_phase",
    (
        "intent_file_durable",
        "intent_directory_durable",
        "intent_resource_durable",
        "artifact_directory_durable",
        "artifact_absence_file_durable",
        "artifact_absence_directory_durable",
        "key_retirement_durable",
        "key_retirement_file_durable",
        "key_retirement_directory_durable",
        "journal_directory_durable",
        "cleanup_batch_durable",
    ),
)
def test_backup_artifact_and_key_retirement_replays_without_an_unusable_pair(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.write_bytes(b"encrypted-backup")
    artifact.chmod(0o600)
    retirement = _BackupRetirement(
        resource_handle="whs-stable#backup-encryption-key",
        artifact=artifact,
    )
    recorder = _RecordingResources()
    deadline = operation.started_at + timedelta(hours=1)
    artifact_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
        provider_resource_handle=str(artifact),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    key_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
        provider_resource_handle=retirement.resource_handle,
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    for resource in (artifact_resource, key_resource):
        recorder.record_planned(resource)
        recorder.mark_created(
            resource.tenant_id,
            resource.resource_id,
            resource.provider_resource_handle,
        )

    def crash_at(phase: str) -> None:
        if phase == crash_phase:
            raise _SimulatedRetirementCrash

    boundary = _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=retirement,
        checkpoint=crash_at,
    )

    with pytest.raises(_SimulatedRetirementCrash):
        boundary.retire_backup(binding=binding)

    if retirement.is_retired():
        assert artifact.is_file()
        assert artifact.stat().st_size == 0

    restarted = _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=retirement,
    )
    restarted.retire_backup(binding=binding)

    assert artifact.is_file()
    assert artifact.stat().st_size == 0
    assert retirement.is_retired() is True
    assert recorder.resources[artifact_resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.COMPLETE
    )
    assert recorder.resources[key_resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.COMPLETE
    )
    assert any(
        artifact_resource.resource_id in batch and key_resource.resource_id in batch
        for batch in recorder.cleanup_batches
    )
    journal = identity.binding_directory / "backup-retirement.json"
    assert journal.is_file()
    assert journal.stat().st_size == 0


def test_backup_retirement_recovers_the_key_checkpoint_before_artifact_planning(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    retirement = _BackupRetirement(
        resource_handle="whs-stable#backup-encryption-key",
        artifact=artifact,
    )
    recorder = _RecordingResources()
    key_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
        provider_resource_handle=retirement.resource_handle,
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )
    recorder.record_planned(key_resource)
    recorder.mark_created(
        key_resource.tenant_id,
        key_resource.resource_id,
        key_resource.provider_resource_handle,
    )

    _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=retirement,
    ).retire_backup(binding=binding)

    artifact_resource = next(
        resource
        for resource in recorder.resources.values()
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
    )
    assert artifact.is_file()
    assert artifact.stat().st_size == 0
    assert retirement.is_retired() is True
    assert artifact_resource.creation_state is WarehouseResourceCreationState.PLANNED
    assert artifact_resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
    assert recorder.resources[key_resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.COMPLETE
    )


def test_backup_retirement_never_completes_a_dangling_artifact_entry(tmp_path: Path) -> None:
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    binding = _binding()
    operation = _operation()
    identity = _warehouse_identity(binding, private_directory)
    identity.binding_directory.mkdir(mode=0o700)
    artifact = identity.binding_directory / "warehouse.pgdump.aead"
    artifact.symlink_to(identity.binding_directory / "missing-ciphertext")
    retirement = _BackupRetirement(
        resource_handle="whs-stable#backup-encryption-key",
        artifact=artifact,
    )
    recorder = _RecordingResources()
    deadline = operation.started_at + timedelta(hours=1)
    artifact_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ARTIFACT,
        provider_resource_handle=str(artifact),
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    key_resource = _operation_resource(
        binding,
        operation,
        resource_kind=WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
        provider_resource_handle=retirement.resource_handle,
        parent_resource_handle=identity.private_resource_handle,
        retention_deadline=deadline,
    )
    for resource in (artifact_resource, key_resource):
        recorder.record_planned(resource)
        recorder.mark_created(
            resource.tenant_id,
            resource.resource_id,
            resource.provider_resource_handle,
        )

    _retirement_boundary(
        private_directory=private_directory,
        recorder=recorder,
        retirement=retirement,
    ).retire_backup(binding=binding)

    assert artifact.is_symlink()
    assert retirement.is_retired() is False
    assert recorder.resources[artifact_resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.FAILED
    )
    assert recorder.resources[key_resource.resource_id].cleanup_status is (
        WarehouseResourceCleanupStatus.FAILED
    )


@pytest.mark.parametrize(
    "payload",
    (
        b"intent\nkey_retired\n",
        b"intent\nartifact_absent\nartifact_absent\n",
        b"artifact_absent\n",
    ),
)
def test_backup_retirement_journal_accepts_only_a_durable_phase_prefix(
    tmp_path: Path,
    payload: bytes,
) -> None:
    journal = tmp_path / "backup-retirement.json"
    journal.write_bytes(payload)
    journal.chmod(0o600)

    with pytest.raises(ValueError, match="retirement journal"):
        _assert_retirement_journal(journal)


@pytest.mark.parametrize(
    "surviving_principal",
    (
        "administration",
        "ingestion_runtime",
        "transformation_runtime",
        "answer_runtime",
        "backup_restore",
        "customer_sql",
        "catalog",
        "bi",
        "administration_test",
    ),
)
def test_probe_login_cleanup_attempts_every_role_and_fails_closed_with_visible_survivors(
    surviving_principal: str,
) -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    connection = _ProbeScopeConnection(plan, surviving_principal)

    passwords = (
        {}
        if surviving_principal == "administration_test"
        else {surviving_principal: "probe-password"}
    )
    surviving_role = (
        plan.administration_test_role
        if surviving_principal == "administration_test"
        else plan.probe_role(surviving_principal)
    )

    with (
        pytest.raises(PostgreSQLProbeCleanupError) as captured,
        probe_login_scope(
            connection,
            plan,
            passwords,
        ),
    ):
        pass

    assert tuple(connection.drop_attempts) == connection.expected_cleanup_roles
    assert captured.value.surviving_roles == (surviving_role,)
    assert inspect_probe_logins(connection, plan) == (surviving_role,)


def test_probe_login_cleanup_never_replaces_the_primary_probe_failure() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    connection = _ProbeScopeConnection(plan, "catalog")
    primary_failure = RuntimeError("primary validation probe failed")

    with (
        pytest.raises(RuntimeError) as captured,
        probe_login_scope(
            connection,
            plan,
            {"catalog": "probe-password"},
        ),
    ):
        raise primary_failure

    assert captured.value is primary_failure
    assert inspect_probe_logins(connection, plan) == (plan.probe_role("catalog"),)
    assert "probe cleanup also failed" in " ".join(primary_failure.__notes__)


def test_probe_login_cleanup_replay_recovers_and_contributes_absence_evidence() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    connection = _ProbeScopeConnection(
        plan,
        "catalog",
        cleanup_failures_remaining=1,
    )

    with (
        pytest.raises(PostgreSQLProbeCleanupError),
        probe_login_scope(
            connection,
            plan,
            {"catalog": "probe-password"},
        ),
    ):
        pass

    with probe_login_scope(connection, plan, {"catalog": "probe-password"}):
        pass
    observation = PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest="engine",
        principal_profile_digest="principal",
        namespace_grant_matrix_digest="grants",
        tls_probe_digest="tls",
        positive_probe_digest="positive",
        denial_probe_digest="denial-before-cleanup",
        ledger_probe_digest="ledger",
        monitoring_probe_digest="monitoring",
        storage_integrity_probe_digest="storage",
        representative_data_digest="representative",
        schema_metadata_digest="schema",
        integrity_marker_digest="integrity",
        query_behavior_digest="query",
    )

    evidenced = warehouse_database.with_probe_login_cleanup_evidence(
        connection,
        plan,
        observation,
    )

    assert inspect_probe_logins(connection, plan) == ()
    assert evidenced.denial_probe_digest != observation.denial_probe_digest
    assert replace(evidenced, denial_probe_digest=observation.denial_probe_digest) == observation


def test_default_access_revokes_public_usage_and_create() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    cursor = _RecordingGrantCursor()

    _remove_default_access(cursor, plan)

    assert "REVOKE USAGE, CREATE ON SCHEMA public FROM PUBLIC" in cursor.statements


def test_public_schema_observation_rejects_any_inherited_public_access() -> None:
    plan = derive_grant_plan("pgw-stable-private-handle")
    connection = _RowsConnection(row=(True, False))

    with pytest.raises(RuntimeError, match="public namespace"):
        _public_schema_summary(connection, plan)


def test_catalog_metadata_probe_fetches_and_asserts_governed_objects() -> None:
    plan = PostgreSQLGrantPlan(prefix="pm_test")
    expected_objects = (
        ("pm_test_conformed", "transformation_probe"),
        ("pm_test_consumption", "certified_probe"),
        ("pm_test_consumption", "customer_probe"),
        ("pm_test_control", "ingestion_append_ledger"),
        ("pm_test_control", "lifecycle_ledger"),
        ("pm_test_product", "product_probe"),
        ("pm_test_quarantine", "quarantine_probe"),
        ("pm_test_raw", "ingestion_probe"),
    )
    connection = _RowsConnection(rows=expected_objects)

    summary = _catalog_metadata_summary(connection, plan)

    assert summary == {
        "domain": "heinzel-postgresql-catalog-metadata-v1",
        "objects": expected_objects,
    }
    assert connection.cursor_value.statements


def test_catalog_metadata_probe_rejects_an_incomplete_visible_inventory() -> None:
    plan = PostgreSQLGrantPlan(prefix="pm_test")
    connection = _RowsConnection(rows=(("pm_test_raw", "ingestion_probe"),))

    with pytest.raises(RuntimeError, match="catalog metadata"):
        _catalog_metadata_summary(connection, plan)


@pytest.mark.parametrize(
    "error",
    (
        psycopg.errors.InvalidPassword(),
        psycopg.errors.InvalidAuthorizationSpecification(),
    ),
)
def test_authentication_failures_are_terminal_authorization_denials(error: Exception) -> None:
    assert _classification_for_error(error) is WarehouseFailureClassification.AUTHORIZATION_DENIED


def test_transport_operational_failure_remains_transient() -> None:
    assert (
        _classification_for_error(psycopg.OperationalError())
        is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    )


def test_rejected_compose_effect_remains_ambiguous_for_reconciliation() -> None:
    error = ComposeCommandError("private Docker diagnostic", classification="rejected")

    assert _classification_for_error(error) is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


def test_ambiguous_exact_network_inspection_cannot_prove_restore_absence() -> None:
    class _AmbiguousNetworkCompose(_ComposeDouble):
        def resource_is_absent(self, *, resource_kind: str, **_kwargs: object) -> bool | None:
            return None if resource_kind == "network" else True

        def discover_resources(self, **_kwargs: object) -> tuple[ComposeResource, ...]:
            return ()

        def remove_resource(self, **_kwargs: object) -> None:
            raise AssertionError("ambiguous resources must not be mutated")

    compose = _AmbiguousNetworkCompose()

    with pytest.raises(ComposeCommandError) as verification_failure:
        warehouse_module._verify_compose_resources_absent(
            compose,
            project_name="pm-pgr-test",
            container_name="pm-pgr-test-database",
            network_names=("pm-pgr-test-private", "pm-pgr-test-loopback"),
            volume_name="pm-pgr-test-data",
            environment={},
        )

    assert verification_failure.value.classification == "ambiguous"


def test_ambiguous_exact_network_inspection_cannot_complete_restore_cleanup() -> None:
    binding = _binding()
    operation = _operation()
    restore = _restore_identity(binding, operation)
    resources = _planned_restore_resources(
        binding,
        operation,
        restore,
        retention_deadline=operation.started_at + timedelta(hours=1),
    )

    class _AmbiguousNetworkCompose(_ComposeDouble):
        def resource_is_absent(self, *, resource_kind: str, **_kwargs: object) -> bool | None:
            return None if resource_kind == "network" else True

        def discover_resources(self, **_kwargs: object) -> tuple[ComposeResource, ...]:
            return ()

        def remove_resource(self, **_kwargs: object) -> None:
            raise AssertionError("ambiguous resources must not be mutated")

    with pytest.raises(ComposeCommandError) as cleanup_failure:
        _remove_exact_restore_resources(
            _AmbiguousNetworkCompose(),
            resources=resources,
            environment={},
        )

    assert cleanup_failure.value.classification == "ambiguous"


def test_startup_denial_probe_uses_structured_authorization_sqlstate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from heinzel_provider_postgresql import warehouse_protocol

    client, server = socket.socketpair()
    payload = b"SFATAL\0VFATAL\0C28000\0Mexplicit authorization denial\0\0"
    server.sendall(b"E" + struct.pack("!I", len(payload) + 4) + payload)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: client,
    )

    try:
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
            warehouse_protocol.connect_denial_probe(
                dbname="heinzel_warehouse",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=5432,
                sslmode="disable",
                sslrootcert=str(tmp_path / "ca.crt"),
                sslcert=str(tmp_path / "client.crt"),
                sslkey=str(tmp_path / "client.key"),
                connect_timeout=5,
            )
    finally:
        server.close()


def test_startup_denial_probe_keeps_transport_failure_generic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from heinzel_provider_postgresql import warehouse_protocol

    transport_failure = OSError("route unavailable")

    def unavailable(*_args: object, **_kwargs: object) -> socket.socket:
        raise transport_failure

    monkeypatch.setattr(socket, "create_connection", unavailable)

    with pytest.raises(psycopg.OperationalError) as captured:
        warehouse_protocol.connect_denial_probe(
            dbname="heinzel_warehouse",
            user="probe",
            password="credential",
            host="127.0.0.1",
            port=5432,
            sslmode="disable",
            sslrootcert=str(tmp_path / "ca.crt"),
            sslcert=str(tmp_path / "client.crt"),
            sslkey=str(tmp_path / "client.key"),
            connect_timeout=5,
        )

    assert type(captured.value) is psycopg.OperationalError


@pytest.mark.parametrize(
    "assert_denied",
    (assert_password_connection_denied, assert_plaintext_connection_denied),
)
def test_credential_denial_probes_never_accept_a_transport_failure_as_denial(
    tmp_path: Path,
    assert_denied: Any,
) -> None:
    def unavailable(**_kwargs: object) -> object:
        raise psycopg.OperationalError

    with pytest.raises(psycopg.OperationalError):
        arguments = {
            "user": "postgres",
            "password": "credential",
        }
        if assert_denied is assert_password_connection_denied:
            arguments["active_password"] = "active-credential"
        assert_denied(
            unavailable,
            PostgreSQLConnectionTarget(
                host="localhost",
                port=5432,
                root_certificate=tmp_path / "ca.crt",
                client_certificate=tmp_path / "client.crt",
                client_private_key=tmp_path / "client.key",
            ),
            **arguments,
        )


@pytest.mark.parametrize(
    "assert_denied",
    (assert_password_connection_denied, assert_plaintext_connection_denied),
)
def test_credential_denial_probes_sandwich_denial_around_a_live_tls_control(
    tmp_path: Path,
    assert_denied: Any,
) -> None:
    attempts: list[tuple[object, object]] = []

    class Connection:
        def close(self) -> None:
            return None

    def connect(**kwargs: object) -> object:
        attempt = (kwargs["sslmode"], kwargs["password"])
        attempts.append(attempt)
        denied = (
            kwargs["sslmode"] == "disable"
            if assert_denied is assert_plaintext_connection_denied
            else kwargs["password"] == "retired-credential"
        )
        if denied:
            if assert_denied is assert_plaintext_connection_denied:
                raise psycopg.errors.InvalidAuthorizationSpecification
            raise psycopg.errors.InvalidPassword
        return Connection()

    arguments = {
        "user": "postgres",
        "password": "retired-credential",
    }
    if assert_denied is assert_password_connection_denied:
        arguments["active_password"] = "active-credential"
    assert_denied(
        connect,
        PostgreSQLConnectionTarget(
            host="localhost",
            port=5432,
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        **arguments,
    )

    assert len(attempts) == 3
    assert attempts[0] == attempts[2]
    assert attempts[1] != attempts[0]


@pytest.mark.parametrize(
    ("assert_denied", "terminal_denial"),
    (
        (assert_password_connection_denied, psycopg.errors.InvalidPassword()),
        (
            assert_plaintext_connection_denied,
            psycopg.errors.InvalidAuthorizationSpecification(),
        ),
    ),
)
def test_credential_denial_probes_propagate_transport_after_a_live_control(
    tmp_path: Path,
    assert_denied: Any,
    terminal_denial: psycopg.OperationalError,
) -> None:
    denied_attempts = 0
    transport_failure = psycopg.OperationalError("transport unavailable")

    class Connection:
        def close(self) -> None:
            return None

    def connect(**kwargs: object) -> object:
        nonlocal denied_attempts
        denied = (
            kwargs["sslmode"] == "disable"
            if assert_denied is assert_plaintext_connection_denied
            else kwargs["password"] == "retired-credential"
        )
        if not denied:
            return Connection()
        denied_attempts += 1
        if denied_attempts == 1:
            raise terminal_denial
        raise transport_failure

    arguments = {"user": "postgres", "password": "retired-credential"}
    if assert_denied is assert_password_connection_denied:
        arguments["active_password"] = "active-credential"

    with pytest.raises(psycopg.OperationalError) as captured:
        assert_denied(
            connect,
            PostgreSQLConnectionTarget(
                host="localhost",
                port=5432,
                root_certificate=tmp_path / "ca.crt",
                client_certificate=tmp_path / "client.crt",
                client_private_key=tmp_path / "client.key",
            ),
            **arguments,
        )

    assert captured.value is transport_failure


def test_unrelated_database_denial_probe_propagates_transport(tmp_path: Path) -> None:
    transport_failure = psycopg.OperationalError("transport unavailable")

    def unavailable(**_kwargs: object) -> object:
        raise transport_failure

    with pytest.raises(psycopg.OperationalError) as captured:
        _require_database_denial(
            unavailable,
            PostgreSQLConnectionTarget(
                host="localhost",
                port=5432,
                root_certificate=tmp_path / "ca.crt",
                client_certificate=tmp_path / "client.crt",
                client_private_key=tmp_path / "client.key",
            ),
            user="catalog",
            password="credential",
            database_name="unrelated",
        )

    assert captured.value is transport_failure


@pytest.mark.parametrize(
    "terminal_denial",
    (
        psycopg.errors.InvalidAuthorizationSpecification(),
        psycopg.errors.InsufficientPrivilege(),
    ),
)
def test_unrelated_database_probe_accepts_only_explicit_terminal_denials(
    tmp_path: Path,
    terminal_denial: psycopg.DatabaseError,
) -> None:
    def denied(**_kwargs: object) -> object:
        raise terminal_denial

    _require_database_denial(
        denied,
        PostgreSQLConnectionTarget(
            host="localhost",
            port=5432,
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        user="catalog",
        password="credential",
        database_name="unrelated",
    )


@pytest.mark.parametrize(
    ("active_password", "bootstrap_is_active", "attempts"),
    (
        ("administration-password", False, ("administration-password",)),
        (
            "bootstrap-password",
            True,
            ("administration-password", "bootstrap-password"),
        ),
    ),
)
def test_primary_reconciliation_recovers_either_side_of_bootstrap_rotation(
    tmp_path: Path,
    active_password: str,
    bootstrap_is_active: bool,
    attempts: tuple[str, ...],
) -> None:
    observed_attempts: list[str] = []
    connection = type("Connection", (), {"autocommit": False})()

    def connect(**kwargs: object) -> object:
        password = kwargs["password"]
        assert isinstance(password, str)
        observed_attempts.append(password)
        if password != active_password:
            raise psycopg.errors.InvalidPassword
        return connection

    recovered, observed_bootstrap_state = _wait_for_primary_administration(
        connect,
        PostgreSQLConnectionTarget(
            host="localhost",
            port=5432,
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        administration_password="administration-password",
        bootstrap_password="bootstrap-password",
        timeout_seconds=0.1,
    )

    assert recovered is connection
    assert observed_bootstrap_state is bootstrap_is_active
    assert tuple(observed_attempts) == attempts
    assert connection.autocommit is True
