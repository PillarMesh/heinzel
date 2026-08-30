from __future__ import annotations

import io
import json
import os
import socket
import subprocess
from base64 import b64encode
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal

import httpx
import pillarmesh_provider_clickhouse.warehouse as warehouse_module
import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_clickhouse import (
    CLICKHOUSE_WAREHOUSE_IMAGE,
    ClickHouseWarehouseProvider,
    ClickHouseWarehouseSettings,
)
from pillarmesh_provider_clickhouse.warehouse import (
    ClickHouseBackupCommandBoundary,
    ClickHouseBackupLifecycleResult,
    ClickHouseConnectionTarget,
    ClickHouseGrantPlan,
    ClickHouseHTTPClient,
    _classification_for_error,
    _expected_grant_rows,
    _rewrite_private_binary_file,
    assert_supported_semantics,
    derive_clickhouse_grant_plan,
)
from pillarmesh_provider_sdk import (
    BackupStreamIntegrityError,
    ComposeCommandError,
    DockerComposeProcess,
    decrypt_backup_stream,
)
from pillarmesh_warehouse_control import (
    EngineKind,
    InitialWarehouseValidationResult,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    ResumeWarehouseValidationResult,
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
    WarehouseRestoreVerification,
)
from pydantic import SecretStr, ValidationError

_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_DATABASE_CLASSES_FOR_TEST = (
    "raw",
    "conformed",
    "product",
    "consumption",
    "quarantine",
    "control",
)
_ROLE_CLASSES_FOR_TEST = (
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "backup_restore",
    "customer_sql",
    "catalog",
    "bi",
)


@dataclass(frozen=True, slots=True)
class _SecretCapability:
    value: SecretStr

    def resolve(self) -> SecretStr:
        return self.value


@dataclass(slots=True)
class _MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class _RecordingResources:
    def __init__(self) -> None:
        self.resources: dict[str, PrivateWarehouseResource] = {}
        self.events: list[tuple[str, str]] = []
        self.mark_failures = 0
        self.cleanup_record_failures = 0
        self.reopen_calls: list[tuple[PrivateWarehouseResource, ...]] = []

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
                key=lambda resource: resource.resource_id,
            )
        )

    def record_planned(self, resource: PrivateWarehouseResource) -> None:
        existing = self.resources.get(resource.resource_id)
        if existing is not None and existing != resource:
            raise RuntimeError("resource identity changed")
        self.resources[resource.resource_id] = resource
        self.events.append(("planned", resource.resource_kind.value))

    def mark_created(
        self,
        tenant_id: str,
        resource_id: str,
        provider_resource_handle: str,
    ) -> None:
        if self.mark_failures:
            self.mark_failures -= 1
            raise RuntimeError("injected resource mark interruption")
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

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]:
        self.reopen_calls.append(expected_resources)
        reopened = tuple(
            resource.model_copy(
                update={
                    "creation_state": WarehouseResourceCreationState.PLANNED,
                    "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
                    "cleanup_failure_classification": None,
                    "updated_at": reopened_at,
                }
            )
            for resource in expected_resources
        )
        self.resources.update((resource.resource_id, resource) for resource in reopened)
        return reopened

    def record_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None:
        if self.cleanup_record_failures:
            self.cleanup_record_failures -= 1
            raise RuntimeError("injected cleanup recording failure")
        resource = self.resources[resource_id]
        assert resource.tenant_id == tenant_id
        self.resources[resource_id] = resource.model_copy(
            update={
                "cleanup_status": status,
                "cleanup_failure_classification": classification,
            }
        )

    def record_cleanup_batch(self, resources: tuple[PrivateWarehouseResource, ...]) -> None:
        self.resources.update((resource.resource_id, resource) for resource in resources)


class _RecordingCompose:
    def __init__(self, recorder: _RecordingResources) -> None:
        self._recorder = recorder
        self.events: list[str] = []
        self.absent = True
        self.removed: set[tuple[str, str]] = set()
        self.restore_up_failures = 0
        self.native_cleanup_failures = 0
        self.native_cleanup_attempts = 0
        # The provider clears the fixed native backup name before claiming it, so
        # tests that inject a post-backup cleanup failure must let that earlier
        # attempt through.
        self.native_cleanup_ignored_attempts = 0
        self.native_backup_present = False
        self.running = True

    def resource_is_absent(self, **arguments: Any) -> bool:
        key = (str(arguments["resource_kind"]), str(arguments["identifier"]))
        if key in self.removed:
            return True
        if arguments.get("resource_kind") == "volume":
            return False
        return self.absent

    def up(self, **_arguments: Any) -> None:
        assert self._recorder.resources
        assert all(
            resource.creation_state is WarehouseResourceCreationState.PLANNED
            for resource in self._recorder.resources.values()
        )
        self.events.append("up")
        self.absent = False

    def stop(self, **_arguments: Any) -> None:
        self.events.append("stop")
        self.running = False

    def start(self, **_arguments: Any) -> None:
        self.events.append("start")
        self.running = True

    def remove_resource(self, **arguments: Any) -> None:
        key = (str(arguments["resource_kind"]), str(arguments["identifier"]))
        self.removed.add(key)
        self.events.append(f"remove:{key[0]}")

    def exec(self, **arguments: Any) -> bytes:
        self.events.append("exec")
        command_arguments = tuple(arguments.get("arguments", ()))
        if any("restore_endpoint_reachable" in argument for argument in command_arguments):
            return b"restore_endpoint_denied_tcp\n"
        if command_arguments[:3] == ("--profile", "restore", "up") and self.restore_up_failures:
            self.restore_up_failures -= 1
            raise RuntimeError("injected restore start failure")
        if "/var/lib/clickhouse/backups/pillarmesh-native.zip" in command_arguments:
            self.native_cleanup_attempts += 1
            self.native_backup_present = False
            if self.native_cleanup_ignored_attempts:
                self.native_cleanup_ignored_attempts -= 1
            elif self.native_cleanup_failures:
                self.native_cleanup_failures -= 1
                raise RuntimeError("injected native cleanup failure")
        return b""

    @contextmanager
    def exec_stream(self, **_arguments: Any) -> Any:
        assert any(
            resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
            for resource in self._recorder.resources.values()
        )
        assert any(
            resource.resource_kind is WarehouseResourceKind.RESTORE_DATA_VOLUME
            for resource in self._recorder.resources.values()
        )
        self.events.append("exec_stream")
        yield io.BytesIO(b"clickhouse-native-backup" * 1024)

    def inspect_container_image(self, **_arguments: Any) -> str:
        return CLICKHOUSE_WAREHOUSE_IMAGE

    def inspect_container_image_id(self, **_arguments: Any) -> str:
        return "sha256:" + "b" * 64

    def inspect_image_id(self, **_arguments: Any) -> str:
        return "sha256:" + "b" * 64

    def inspect_container_running(self, **_arguments: Any) -> bool:
        return self.running

    def inspect_container_has_published_ports(self, **arguments: Any) -> bool:
        identifier = str(arguments["identifier"])
        return not any(
            resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
            and resource.provider_resource_handle == identifier
            for resource in self._recorder.resources.values()
        )

    def inspect_container_networks(self, **arguments: Any) -> tuple[str, ...]:
        identifier = str(arguments["identifier"])
        if identifier.endswith("-isolation-probe"):
            return tuple(
                resource.provider_resource_handle
                for resource in self._recorder.resources.values()
                if resource.resource_kind is WarehouseResourceKind.PRIVATE_NETWORK
                and resource.provider_resource_handle.endswith("-private")
            )
        restore = any(
            resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
            and resource.provider_resource_handle == identifier
            for resource in self._recorder.resources.values()
        )
        network_kind = (
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK
            if restore
            else WarehouseResourceKind.PRIVATE_NETWORK
        )
        return tuple(
            sorted(
                resource.provider_resource_handle
                for resource in self._recorder.resources.values()
                if resource.resource_kind is network_kind
            )
        )

    def inspect_container_network_ipv4_address(self, **arguments: Any) -> str:
        identifier = str(arguments["identifier"])
        address = (
            "172.31.0.9"
            if any(
                resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
                and resource.provider_resource_handle == identifier
                for resource in self._recorder.resources.values()
            )
            else "172.30.0.9"
        )
        validated = DockerComposeProcess._validated_network_ipv4_address({"IPAddress": address})
        assert validated is not None
        return validated

    def inspect_network_internal(self, **arguments: Any) -> bool:
        identifier = str(arguments["identifier"])
        if any(
            resource.resource_kind is WarehouseResourceKind.RESTORE_PRIVATE_NETWORK
            and resource.provider_resource_handle == identifier
            for resource in self._recorder.resources.values()
        ):
            return True
        return identifier.endswith("-private")


class _RecordingClient:
    def __init__(
        self,
        *,
        restore_failures: int = 0,
        provision_failures: int = 0,
        compose: _RecordingCompose | None = None,
    ) -> None:
        self._compose = compose
        self.statements: list[str] = []
        self.username = "default"
        self.endpoint = "https://primary"
        self.disabled_bootstrap_endpoints: set[str] = set()
        self.grant_drift = False
        self.version = "25.8.32.4"
        self.restore_failures = restore_failures
        self.provision_failures = provision_failures

    def execute(self, statement: str, *, operation: str = "validate") -> bytes:
        self.statements.append(statement)
        if statement.startswith("BACKUP ") and self._compose is not None:
            # ClickHouse refuses to write a backup to a destination that already
            # exists rather than overwriting it.
            if self._compose.native_backup_present:
                raise RuntimeError("backup already exists")
            self._compose.native_backup_present = True
        if statement.startswith("RESTORE ") and self.restore_failures:
            self.restore_failures -= 1
            raise WarehouseProviderError(
                operation="validate",
                classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
            )
        if statement.startswith("CREATE DATABASE") and self.provision_failures:
            self.provision_failures -= 1
            raise RuntimeError("injected provision interruption")
        if statement == "SYSTEM RELOAD CONFIG":
            self.disabled_bootstrap_endpoints.add(self.endpoint)
        if statement.startswith("CHECK GRANT"):
            return self._grant_result(statement.removeprefix("CHECK GRANT ")).encode("ascii")
        return b""

    def query_lines(self, statement: str, *, operation: str = "validate") -> tuple[str, ...]:
        self.statements.append(statement)
        if statement == "SELECT 1":
            if self.username == "default" and self.endpoint in self.disabled_bootstrap_endpoints:
                raise WarehouseProviderError(
                    operation=operation,
                    classification=WarehouseFailureClassification.AUTHORIZATION_DENIED,
                )
            return ("1",)
        if statement == "SELECT version()":
            return (self.version,)
        if statement.startswith("SELECT name FROM system.roles"):
            return tuple(
                sorted(
                    recorded.split("`")[1]
                    for recorded in self.statements
                    if recorded.startswith("CREATE ROLE IF NOT EXISTS `")
                )
            )
        if statement.startswith("SELECT granted_role_name"):
            return tuple(
                sorted(
                    "\t".join((parts[1], parts[3]))
                    for recorded in self.statements
                    if recorded.startswith("GRANT `") and " TO `" in recorded
                    for parts in (recorded.split("`"),)
                )
            )
        if statement.startswith("SELECT name FROM system.users"):
            return (
                "default",
                *tuple(
                    sorted(
                        recorded.split("`")[1]
                        for recorded in self.statements
                        if recorded.startswith("CREATE USER IF NOT EXISTS `")
                    )
                ),
            )
        if statement.startswith("SELECT user_name, role_name, access_type"):
            administration_role = next(
                recorded.split("`")[1]
                for recorded in self.statements
                if recorded.startswith("CREATE ROLE IF NOT EXISTS `")
                and recorded.split("`")[1].endswith("_administration")
            )
            prefix = administration_role.removesuffix("_administration")
            plan = ClickHouseGrantPlan(
                databases={name: f"{prefix}_{name}" for name in _DATABASE_CLASSES_FOR_TEST},
                roles={name: f"{prefix}_{name}" for name in _ROLE_CLASSES_FOR_TEST},
                users={name: f"{prefix}_{name}_user" for name in _ROLE_CLASSES_FOR_TEST},
                statements=(),
            )
            rows = set(_expected_grant_rows(plan))
            if self.grant_drift:
                rows.add(
                    "\t".join(
                        (
                            "\\N",
                            plan.roles["customer_sql"],
                            "SELECT",
                            plan.databases["raw"],
                            "\\N",
                            "\\N",
                            "0",
                            "0",
                        )
                    )
                )
            return tuple(sorted(rows))
        if statement.startswith("SELECT metric"):
            return (
                "FilesystemMainPathTotalBytes\t100",
                "FilesystemMainPathUsedBytes\t20",
            )
        if statement.startswith("SELECT marker"):
            return ("pillarmesh-storage-marker",)
        if statement.startswith("SELECT name FROM system.databases"):
            return tuple(
                recorded.split("`")[1]
                for recorded in self.statements
                if recorded.startswith("CREATE DATABASE IF NOT EXISTS `")
            )
        if statement.startswith("SELECT database, name, engine FROM system.tables"):
            control_database = next(
                recorded.split("`")[1]
                for recorded in self.statements
                if recorded.startswith("CREATE TABLE IF NOT EXISTS `")
            )
            return (f"{control_database}\tpillarmesh_validation_ledger\tMergeTree",)
        if statement.startswith("SELECT count() FROM"):
            return ("1",)
        return ()

    def _grant_result(self, privilege: str) -> str:
        principal_class = next(
            principal
            for principal in (
                "administration",
                "ingestion_runtime",
                "transformation_runtime",
                "customer_sql",
                "catalog",
                "bi",
                "backup_restore",
            )
            if f"_{principal}_user" in self.username
        )
        granted = {
            "administration": lambda: privilege == "CREATE USER ON *.*",
            "ingestion_runtime": lambda: privilege.startswith("INSERT ON `"),
            "transformation_runtime": lambda: privilege.startswith(("SELECT ON `", "INSERT ON `")),
            "customer_sql": lambda: privilege.endswith(".`customer_probe`"),
            "catalog": lambda: privilege == "SELECT ON system.tables",
            "bi": lambda: privilege.endswith(".`certified_probe`"),
            "backup_restore": lambda: privilege.startswith("BACKUP ON `"),
        }[principal_class]()
        return "1" if granted else "0"

    def close(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _BackupCommands:
    result: ClickHouseBackupLifecycleResult

    def prepare_principal(self, client: Any, plan: Any) -> None:
        client.execute(
            f"CREATE USER IF NOT EXISTS `{plan.users['backup_restore']}` "
            "IDENTIFIED WITH sha256_password BY 'backup-secret'",
            operation="provision",
        )
        client.execute(
            f"GRANT `{plan.roles['backup_restore']}` TO `{plan.users['backup_restore']}`",
            operation="provision",
        )

    def backup_and_restore(self, **_arguments: Any) -> ClickHouseBackupLifecycleResult:
        return self.result

    def resources(self) -> tuple[PrivateWarehouseResource, ...]:
        return ()

    def retire_backup(self, **_arguments: Any) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _BackupSecretCapability:
    password: SecretStr
    key: SecretStr

    def resolve_backup_restore_password(self) -> SecretStr:
        return self.password

    def resolve_backup_encryption_key(self) -> SecretStr:
        return self.key


@dataclass(slots=True)
class _BackupRetirementCapability:
    resource_handle: str = "secret://clickhouse-backup-key"
    retired: bool = False

    def retire(self) -> None:
        self.retired = True

    def is_retired(self) -> bool:
        return self.retired


def _binding(
    *,
    engine_kind: EngineKind = EngineKind.CLICKHOUSE,
    lifecycle_state: WarehouseBindingState = WarehouseBindingState.PROVISIONING,
    revision: int = 2,
) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="whb-" + "1" * 24,
        tenant_id="tenant-a",
        engine_kind=engine_kind,
        region="local",
        capacity_profile="mvp-fixed",
        capability_profile_digest="a" * 64,
        lifecycle_state=lifecycle_state,
        revision=revision,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _operation(
    *,
    engine_kind: EngineKind = EngineKind.CLICKHOUSE,
    operation_kind: WarehouseOperationKind = WarehouseOperationKind.PROVISION,
    binding_revision: int = 2,
) -> PrivateWarehouseOperation:
    return PrivateWarehouseOperation(
        tenant_id="tenant-a",
        binding_id="whb-" + "1" * 24,
        binding_revision=binding_revision,
        operation_id="wop-" + "2" * 24,
        operation_kind=operation_kind,
        engine_kind=engine_kind,
        status=WarehouseOperationStatus.RUNNING,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=_NOW,
        updated_at=_NOW,
    )


def _provider(
    private_directory: Path,
    *,
    recorder: _RecordingResources,
    compose: _RecordingCompose,
    client: _RecordingClient,
    backup_commands: Any = None,
    clock: Any = lambda: _NOW,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None] = lambda _checkpoint: None,
) -> ClickHouseWarehouseProvider:
    private_directory.mkdir(mode=0o700, exist_ok=True)
    ordinary: WarehouseOperationSecretCapability = _SecretCapability(SecretStr("secret"))
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

    def client_factory(target: ClickHouseConnectionTarget) -> _RecordingClient:
        client.username = target.username
        client.endpoint = target.endpoint
        return client

    return ClickHouseWarehouseProvider(
        settings=ClickHouseWarehouseSettings(
            private_operation_directory=private_directory,
            retention_period=timedelta(hours=1),
        ),
        compose=compose,
        resource_recorder=recorder,
        administration_secret=ordinary,
        ingestion_runtime_secret=ordinary,
        transformation_runtime_secret=ordinary,
        customer_sql_secret=ordinary,
        catalog_secret=ordinary,
        bi_secret=ordinary,
        tls_private_key_secret=tls_private_key,
        tls_certificate_secret=tls_certificate,
        backup_commands=backup_commands or _BackupCommands(_backup_result()),
        client_factory=client_factory,
        clock=clock,
        entropy=lambda size: b"x" * size,
        fault_hook=fault_hook,
    )


def _provider_with_native_backup(
    private_directory: Path,
    *,
    recorder: _RecordingResources,
    compose: _RecordingCompose,
    client: _RecordingClient,
    fault_hook: Callable[[WarehouseLifecycleCheckpoint], None] = lambda _checkpoint: None,
) -> tuple[ClickHouseWarehouseProvider, ClickHouseBackupCommandBoundary]:
    private_directory.mkdir(mode=0o700)
    settings = ClickHouseWarehouseSettings(
        private_operation_directory=private_directory,
        retention_period=timedelta(hours=1),
        backup_chunk_bytes=4096,
    )
    boundary = ClickHouseBackupCommandBoundary(
        settings=settings,
        compose=compose,
        resource_recorder=recorder,
        secret_capability=_BackupSecretCapability(
            password=SecretStr("backup-secret"),
            key=SecretStr(b64encode(b"k" * 32).decode("ascii")),
        ),
        retirement_capability=_BackupRetirementCapability(),
        client_factory=lambda target: _recording_client_for_target(client, target),
        clock=lambda: _NOW,
        entropy=lambda size: b"n" * size,
        fault_hook=fault_hook,
    )
    provider = _provider(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=boundary,
        fault_hook=fault_hook,
    )
    return provider, boundary


def _backup_result() -> ClickHouseBackupLifecycleResult:
    restore = WarehouseRestoreVerification(
        verification_id="wrv-" + "3" * 24,
        tenant_id="tenant-a",
        binding_id="whb-" + "1" * 24,
        binding_revision=2,
        engine_kind=EngineKind.CLICKHOUSE,
        source_backup_artifact_digest="1" * 64,
        representative_data_digest="2" * 64,
        schema_metadata_digest="3" * 64,
        principal_profile_digest="4" * 64,
        integrity_marker_digest="5" * 64,
        query_behavior_digest="6" * 64,
        verified_at=_NOW,
    )
    return ClickHouseBackupLifecycleResult(
        backup_artifact_digest="1" * 64,
        restore_verification=restore,
        restore_cleanup_digest="7" * 64,
        positive_probe_digest="8" * 64,
        denial_probe_digest="9" * 64,
        network_isolation_probe_digest="a" * 64,
    )


def test_clickhouse_warehouse_provider_exposes_the_six_operation_shape() -> None:
    assert ClickHouseWarehouseProvider.engine_kind is EngineKind.CLICKHOUSE
    for operation_name in (
        "provision",
        "reconcile",
        "validate",
        "suspend",
        "resume",
        "retire",
    ):
        assert callable(getattr(ClickHouseWarehouseProvider, operation_name))


def test_clickhouse_settings_pin_the_approved_lts_image_and_require_a_private_directory() -> None:
    assert CLICKHOUSE_WAREHOUSE_IMAGE == (
        "clickhouse/clickhouse-server:25.8.32.4@"
        "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
    )
    with pytest.raises(ValidationError):
        ClickHouseWarehouseSettings(private_operation_directory=Path("relative/private"))


def test_clickhouse_provision_records_every_resource_before_create_and_replays_verbatim(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
        fault_hook=checkpoints.append,
    )

    first = provider.provision(_binding(), _operation())
    resources_after_first = recorder.load_resources("tenant-a", "whb-" + "1" * 24)
    second = provider.provision(_binding(), _operation())

    assert first == second
    assert first.engine_kind is EngineKind.CLICKHOUSE
    assert compose.events == ["up", "exec"]
    assert resources_after_first == recorder.load_resources("tenant-a", "whb-" + "1" * 24)
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        for resource in resources_after_first
    )
    assert {resource.resource_kind for resource in resources_after_first} >= {
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
    plan = derive_clickhouse_grant_plan(first.private_resource_handle)
    assert tuple(client.statements[1 : 1 + len(plan.statements)]) == plan.statements
    assert "SYSTEM RELOAD CONFIG" in client.statements
    bootstrap_credential = next(
        resource
        for resource in resources_after_first
        if Path(resource.provider_resource_handle).name == "bootstrap.secret"
    )
    assert Path(bootstrap_credential.provider_resource_handle).stat().st_size == 0
    bootstrap_users = next(
        resource
        for resource in resources_after_first
        if Path(resource.provider_resource_handle).name == "bootstrap-users.xml"
    )
    disabled_configuration = Path(bootstrap_users.provider_resource_handle).read_text(
        encoding="utf-8"
    )
    assert "<ip>0.0.0.0</ip>" in disabled_configuration
    assert "<access_management>0</access_management>" in disabled_configuration
    assert all(user in "\n".join(client.statements) for user in plan.users.values())
    assert checkpoints == [
        WarehouseLifecycleCheckpoint.AFTER_RESOURCE_PLAN,
        WarehouseLifecycleCheckpoint.AFTER_PROVIDER_CREATE,
    ]


def test_clickhouse_provision_rejects_cross_engine_ownership_before_creating_resources(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )

    with pytest.raises(WarehouseProviderError) as captured:
        provider.provision(_binding(engine_kind=EngineKind.POSTGRESQL), _operation())

    assert captured.value.classification is (WarehouseFailureClassification.PERMANENT_CONFIGURATION)
    assert not recorder.resources
    assert compose.events == []


def test_clickhouse_reconcile_adopts_a_completed_suspend_without_repeating_stop(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provisioning = _binding()
    provider.provision(provisioning, _operation())
    ready = provisioning.model_copy(
        update={"lifecycle_state": WarehouseBindingState.READY, "revision": 4}
    )
    suspend_operation = _operation(
        operation_kind=WarehouseOperationKind.SUSPEND,
        binding_revision=ready.revision,
    ).model_copy(update={"operation_id": "wop-" + "4" * 24})
    compose.running = False
    events_before_reconcile = tuple(compose.events)

    result = provider.reconcile(ready, suspend_operation)

    assert result.operation_id == suspend_operation.operation_id
    assert tuple(compose.events) == events_before_reconcile


def test_clickhouse_reconcile_completes_suspend_when_claim_preceded_the_effect(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provisioning = _binding()
    provider.provision(provisioning, _operation())
    ready = provisioning.model_copy(
        update={"lifecycle_state": WarehouseBindingState.READY, "revision": 4}
    )
    suspend_operation = _operation(
        operation_kind=WarehouseOperationKind.SUSPEND,
        binding_revision=ready.revision,
    ).model_copy(update={"operation_id": "wop-" + "4" * 24})
    compose.running = True
    compose.events.clear()

    provider.reconcile(ready, suspend_operation)

    assert compose.running is False
    assert compose.events == ["stop"]


def test_clickhouse_reconcile_completes_an_interrupted_recorded_provision(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient(provision_failures=1)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )

    with pytest.raises(WarehouseProviderError):
        provider.provision(_binding(), _operation())

    interrupted = recorder.load_resources("tenant-a", "whb-" + "1" * 24)
    assert interrupted
    assert all(
        resource.creation_state is WarehouseResourceCreationState.PLANNED
        for resource in interrupted
    )
    assert compose.absent is False

    recovered = provider.reconcile(_binding(), _operation())

    assert recovered.engine_kind is EngineKind.CLICKHOUSE
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        for resource in recorder.load_resources("tenant-a", "whb-" + "1" * 24)
    )
    assert "SYSTEM RELOAD CONFIG" in client.statements


def test_clickhouse_reconcile_recovers_after_bootstrap_tombstone_before_resource_marks(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    recorder.mark_failures = 1
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )

    with pytest.raises(WarehouseProviderError):
        provider.provision(_binding(), _operation())

    credential = next(
        resource
        for resource in recorder.resources.values()
        if Path(resource.provider_resource_handle).name == "bootstrap.secret"
    )
    assert Path(credential.provider_resource_handle).stat().st_size == 0

    recovered = provider.reconcile(_binding(), _operation())

    assert recovered.engine_kind is EngineKind.CLICKHOUSE
    assert Path(credential.provider_resource_handle).stat().st_size == 0
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        for resource in recorder.resources.values()
    )


def test_clickhouse_provision_rejects_an_unexpected_container_network(tmp_path: Path) -> None:
    class _UnexpectedNetworkCompose(_RecordingCompose):
        def inspect_container_networks(self, **arguments: Any) -> tuple[str, ...]:
            return (*super().inspect_container_networks(**arguments), "unrelated-network")

    recorder = _RecordingResources()
    compose = _UnexpectedNetworkCompose(recorder)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )

    with pytest.raises(WarehouseProviderError) as captured:
        provider.provision(_binding(), _operation())

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


def test_clickhouse_provision_rejects_container_image_content_drift(tmp_path: Path) -> None:
    class _ImageDriftCompose(_RecordingCompose):
        def inspect_container_image_id(self, **_arguments: Any) -> str:
            return "sha256:" + "c" * 64

    recorder = _RecordingResources()
    compose = _ImageDriftCompose(recorder)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )

    with pytest.raises(WarehouseProviderError) as captured:
        provider.provision(_binding(), _operation())

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


def test_clickhouse_validation_emits_initial_and_fresh_resume_evidence(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    clock = _MutableClock(_NOW)
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=_BackupCommands(_backup_result()),
        clock=clock,
    )
    provider.provision(_binding(), _operation())

    initial = provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )
    clock.value += timedelta(minutes=5)
    resumed = provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.READY, revision=5),
        _operation(
            operation_kind=WarehouseOperationKind.RESUME,
            binding_revision=5,
        ),
        resume=True,
    )

    assert isinstance(initial, InitialWarehouseValidationResult)
    assert initial.evidence.engine_kind is EngineKind.CLICKHOUSE
    assert initial.evidence.engine_version == "25.8.32.4"
    assert initial.restore_verification == _backup_result().restore_verification
    assert initial.evidence.positive_probe_digest != initial.evidence.denial_probe_digest
    assert isinstance(resumed, ResumeWarehouseValidationResult)
    assert resumed.evidence.observed_at > initial.evidence.observed_at
    assert resumed.evidence.engine_image_digest == initial.evidence.engine_image_digest


def test_clickhouse_restore_verification_uses_current_validating_binding_revision(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, _boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )

    result = provider.validate(
        validating_binding,
        _operation(binding_revision=2),
        resume=False,
    )

    assert isinstance(result, InitialWarehouseValidationResult)
    assert result.evidence.binding_revision == 3
    assert result.restore_verification.binding_revision == 3
    assert (
        result.restore_verification.principal_profile_digest
        == result.evidence.principal_profile_digest
    )

    replayed = provider.validate(
        validating_binding,
        _operation(binding_revision=2),
        resume=False,
    )

    assert isinstance(replayed, InitialWarehouseValidationResult)
    assert replayed.restore_verification == result.restore_verification


def test_clickhouse_validation_ledger_insert_is_replay_safe(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=_BackupCommands(_backup_result()),
    )
    provider.provision(_binding(), _operation())

    provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    ledger_insert = next(
        statement for statement in client.statements if statement.startswith("INSERT INTO")
    )
    assert "WHERE NOT EXISTS" in ledger_insert


def test_clickhouse_validation_checks_positive_and_denied_grants_for_each_principal(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=_BackupCommands(_backup_result()),
    )
    provider.provision(_binding(), _operation())

    provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    grant_checks = tuple(
        statement for statement in client.statements if statement.startswith("CHECK GRANT")
    )
    assert len(grant_checks) >= 15


def test_clickhouse_validation_rejects_unplanned_direct_grant_drift(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    client.grant_drift = True

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is WarehouseFailureClassification.INTEGRITY_FAILURE


def test_clickhouse_validation_rejects_server_version_drift(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    client.version = "25.8.32.5"

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is (
        WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    )


def test_clickhouse_suspend_and_resume_preserve_the_data_volume(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())

    provider.suspend(
        _binding(lifecycle_state=WarehouseBindingState.READY, revision=4),
        _operation(
            operation_kind=WarehouseOperationKind.SUSPEND,
            binding_revision=4,
        ),
    )
    provider.resume(
        _binding(lifecycle_state=WarehouseBindingState.SUSPENDED, revision=5),
        _operation(
            operation_kind=WarehouseOperationKind.RESUME,
            binding_revision=5,
        ),
    )

    assert compose.events == ["up", "exec", "stop", "start"]


def test_clickhouse_retirement_retains_then_erases_customer_resources(
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

    recorder = _StrictRetirementResources()
    compose = _RecordingCompose(recorder)
    clock = _MutableClock(_NOW)
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    provider = _provider(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
        backup_commands=_BackupCommands(_backup_result()),
        clock=clock,
        fault_hook=checkpoints.append,
    )
    binding = _binding()
    provider.provision(binding, _operation())
    retiring = _binding(lifecycle_state=WarehouseBindingState.RETIRING, revision=6)
    retirement = _operation(
        operation_kind=WarehouseOperationKind.RETIRE,
        binding_revision=6,
    ).model_copy(update={"operation_id": "wop-" + "6" * 24})

    retained = provider.retire(retiring, retirement)
    clock.value += timedelta(hours=2)
    retired = retiring.model_copy(
        update={"lifecycle_state": WarehouseBindingState.RETIRED, "revision": 7}
    )
    deletion = retirement.model_copy(
        update={"binding_revision": retired.revision, "operation_id": "wop-" + "7" * 24}
    )
    deleted = provider.retire(retired, deletion)

    assert retained.retained_resource_count > 0
    assert retained.cleanup_failed_resource_count == 0
    assert deleted.retained_resource_count == 0
    assert deleted.cleanup_failed_resource_count == 0
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in recorder.resources.values()
    )
    assert checkpoints[-2:] == [
        WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION,
        WarehouseLifecycleCheckpoint.AFTER_RETIREMENT_DISPOSITION,
    ]
    file_resources = (
        resource
        for resource in recorder.resources.values()
        if resource.resource_kind
        in {
            WarehouseResourceKind.CREDENTIAL_FILE,
            WarehouseResourceKind.HOST_PORT_FILE,
            WarehouseResourceKind.TLS_PRIVATE_KEY,
            WarehouseResourceKind.TLS_CERTIFICATE,
        }
    )
    assert all(
        Path(resource.provider_resource_handle).stat().st_size == 0 for resource in file_resources
    )


def test_clickhouse_native_backup_records_resources_and_encrypts_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TerminalLedger(_RecordingResources):
        terminal = False

        def record_cleanup(
            self,
            tenant_id: str,
            resource_id: str,
            status: WarehouseResourceCleanupStatus,
            classification: WarehouseFailureClassification | None,
        ) -> None:
            if (
                self.terminal
                and self.resources[resource_id].cleanup_status
                is WarehouseResourceCleanupStatus.COMPLETE
            ):
                raise AssertionError("terminal cleanup cannot be rewritten")
            super().record_cleanup(tenant_id, resource_id, status, classification)

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    recorder = _TerminalLedger()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    opened_usernames: list[str] = []
    key = b"k" * 32
    settings = ClickHouseWarehouseSettings(
        private_operation_directory=private_directory,
        retention_period=timedelta(hours=1),
        backup_chunk_bytes=4096,
    )
    checkpoints: list[WarehouseLifecycleCheckpoint] = []
    clock = _MutableClock(_NOW)

    def backup_client_factory(target: ClickHouseConnectionTarget) -> _RecordingClient:
        opened_usernames.append(target.username)
        client.username = target.username
        client.endpoint = target.endpoint
        return client

    boundary = ClickHouseBackupCommandBoundary(
        settings=settings,
        compose=compose,
        resource_recorder=recorder,
        secret_capability=_BackupSecretCapability(
            password=SecretStr("backup-secret"),
            key=SecretStr(b64encode(key).decode("ascii")),
        ),
        retirement_capability=_BackupRetirementCapability(),
        client_factory=backup_client_factory,
        clock=clock,
        entropy=lambda size: b"n" * size,
        fault_hook=checkpoints.append,
    )
    provider = _provider(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=boundary,
    )
    provisioned = provider.provision(_binding(), _operation())

    result = provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    assert isinstance(result, InitialWarehouseValidationResult)
    assert (
        result.evidence.backup_artifact_digest
        == result.restore_verification.source_backup_artifact_digest
    )
    resources = boundary.resources()
    restore_resources = tuple(
        resource
        for resource in resources
        if resource.resource_kind
        in {
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
    )
    assert restore_resources
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in restore_resources
    )
    assert checkpoints == [
        WarehouseLifecycleCheckpoint.AFTER_BACKUP_RECORDED,
        WarehouseLifecycleCheckpoint.AFTER_BACKUP_CREATED,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_PLAN,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_VERIFIED,
        WarehouseLifecycleCheckpoint.AFTER_RESTORE_CLEANUP,
    ]
    restore_credentials = tuple(
        resource
        for resource in resources
        if resource.resource_kind is WarehouseResourceKind.CREDENTIAL_FILE
        and "restore" in Path(resource.provider_resource_handle).name
    )
    assert len(restore_credentials) == 2
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        and Path(resource.provider_resource_handle).stat().st_size == 0
        for resource in restore_credentials
    )
    assert [event for event in compose.events if event.startswith("remove:")] == [
        "remove:container",
        "remove:container",
        "remove:network",
        "remove:network",
        "remove:volume",
    ]
    artifact = next(
        resource
        for resource in resources
        if resource.resource_kind is WarehouseResourceKind.BACKUP_ARTIFACT
    )
    encrypted = Path(artifact.provider_resource_handle).read_bytes()
    assert b"clickhouse-native-backup" not in encrypted
    restored = io.BytesIO()
    decrypt_backup_stream(
        io.BytesIO(encrypted),
        restored,
        key=key,
        maximum_chunk_size=settings.backup_chunk_bytes,
    )
    assert restored.getvalue() == b"clickhouse-native-backup" * 1024
    assert provisioned.private_resource_handle not in encrypted.decode("latin-1")
    backup_checks = tuple(
        statement for statement in client.statements if statement.startswith("CHECK GRANT")
    )
    assert any("BACKUP ON" in statement for statement in backup_checks)
    assert any("INSERT ON" in statement for statement in backup_checks)
    assert any("CREATE USER ON *.*" in statement for statement in backup_checks)
    backup_username = derive_clickhouse_grant_plan(provisioned.private_resource_handle).users[
        "backup_restore"
    ]
    assert opened_usernames.count(backup_username) >= 2
    assert sum(statement.startswith("SELECT marker FROM") for statement in client.statements) == 2
    assert (
        sum(
            statement.startswith("SELECT name FROM system.roles") for statement in client.statements
        )
        == 2
    )
    verification_receipt = next(
        resource
        for resource in resources
        if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
    )
    assert verification_receipt.creation_state is WarehouseResourceCreationState.CREATED
    assert Path(verification_receipt.provider_resource_handle).stat().st_size > 0

    replay = provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    assert isinstance(replay, InitialWarehouseValidationResult)
    assert replay.restore_verification == result.restore_verification
    assert sum(statement.startswith("RESTORE ") for statement in client.statements) == 1

    boundary.retire_backup(
        binding=_binding(lifecycle_state=WarehouseBindingState.READY, revision=4)
    )

    resources_after_retirement = boundary.resources()
    staging_resource = next(
        resource
        for resource in resources_after_retirement
        if resource.resource_kind is WarehouseResourceKind.BACKUP_STAGING_FILE
    )
    retained_resources = tuple(
        resource
        for resource in resources_after_retirement
        if resource.resource_kind
        in {
            WarehouseResourceKind.BACKUP_ARTIFACT,
            WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT,
        }
    )
    assert staging_resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
        for resource in retained_resources
    )

    recorder.terminal = True
    clock.value += timedelta(hours=2)
    retired = _binding(lifecycle_state=WarehouseBindingState.RETIRED, revision=7)
    real_erase = warehouse_module._erase_private_file
    failures = 1

    def fail_once(path: Path, *, parent: Path) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("injected retained deletion failure")
        real_erase(path, parent=parent)

    monkeypatch.setattr(warehouse_module, "_erase_private_file", fail_once)

    with pytest.raises(RuntimeError, match="injected retained deletion failure"):
        boundary.retire_backup(binding=retired)

    assert all(
        resource.cleanup_status
        in {WarehouseResourceCleanupStatus.RETAINED, WarehouseResourceCleanupStatus.COMPLETE}
        for resource in boundary.resources()
    )
    boundary.retire_backup(binding=retired)
    boundary.retire_backup(binding=retired)

    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in boundary.resources()
        if resource.resource_kind
        in {
            WarehouseResourceKind.BACKUP_ARTIFACT,
            WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            WarehouseResourceKind.BACKUP_STAGING_FILE,
            WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT,
        }
    )


def test_clickhouse_receipt_replay_erases_operation_scoped_plaintext(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    initial = provider.validate(validating_binding, _operation(), resume=False)
    leftover_names = {
        "clickhouse-restore.zip",
        "clickhouse-restore-password",
        "restore-users.xml",
    }
    leftovers = tuple(
        resource
        for resource in boundary.resources()
        if Path(resource.provider_resource_handle).name in leftover_names
    )
    for resource in leftovers:
        path = Path(resource.provider_resource_handle)
        path.write_text("leftover-sensitive-material", encoding="utf-8")
        recorder.resources[resource.resource_id] = resource.model_copy(
            update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
        )

    replayed = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(initial, InitialWarehouseValidationResult)
    assert isinstance(replayed, InitialWarehouseValidationResult)
    assert replayed.restore_verification == initial.restore_verification
    refreshed = {resource.resource_id: resource for resource in boundary.resources()}
    assert all(
        refreshed[resource.resource_id].cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        and Path(resource.provider_resource_handle).stat().st_size == 0
        for resource in leftovers
    )


def test_clickhouse_receipt_replay_rejects_forged_verification_fields(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    provider.validate(validating_binding, _operation(), resume=False)
    receipt_resource = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
    )
    receipt_path = Path(receipt_resource.provider_resource_handle)
    forged = json.loads(receipt_path.read_text(encoding="utf-8"))
    forged["verification"]["verification_id"] = "wrv-forged-receipt"
    receipt_path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert captured.value.classification is WarehouseFailureClassification.INTEGRITY_FAILURE


def test_clickhouse_receipt_replay_rejects_another_operation_identity(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    operation = _operation()
    provider.provision(_binding(), operation)
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    result = provider.validate(validating_binding, operation, resume=False)
    receipt_resource = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
    )
    another_operation = operation.model_copy(update={"operation_id": "wop-" + "9" * 24})

    with pytest.raises(WarehouseProviderError) as captured:
        warehouse_module._load_restore_receipt(
            Path(receipt_resource.provider_resource_handle),
            validating_binding,
            another_operation,
            artifact_digest=result.restore_verification.source_backup_artifact_digest,
            key=b"k" * 32,
        )

    assert captured.value.classification is WarehouseFailureClassification.INTEGRITY_FAILURE


def test_clickhouse_receipt_replay_does_not_recover_tombstoned_credentials(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    initial = provider.validate(validating_binding, _operation(), resume=False)
    restore_container = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
    )
    recorder.resources[restore_container.resource_id] = restore_container.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )

    replayed = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(initial, InitialWarehouseValidationResult)
    assert isinstance(replayed, InitialWarehouseValidationResult)
    assert replayed.restore_verification == initial.restore_verification
    refreshed = {resource.resource_id: resource for resource in boundary.resources()}
    assert (
        refreshed[restore_container.resource_id].cleanup_status
        is WarehouseResourceCleanupStatus.COMPLETE
    )


def test_clickhouse_receipt_replay_rejects_a_symlinked_tombstone_and_continues_cleanup(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    provider.validate(validating_binding, _operation(), resume=False)
    resources_by_name = {
        Path(resource.provider_resource_handle).name: resource for resource in boundary.resources()
    }
    symlink_resource = resources_by_name["restore-users.xml"]
    later_resource = resources_by_name["clickhouse-restore-password"]
    symlink_path = Path(symlink_resource.provider_resource_handle)
    later_path = Path(later_resource.provider_resource_handle)
    outside_tombstone = tmp_path / "outside-tombstone"
    outside_tombstone.touch(mode=0o600)
    symlink_path.unlink()
    symlink_path.symlink_to(outside_tombstone)
    later_path.write_text("leftover-sensitive-material", encoding="utf-8")
    recorder.resources[later_resource.resource_id] = later_resource.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert captured.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    assert symlink_path.is_symlink()
    assert outside_tombstone.stat().st_size == 0
    refreshed = {resource.resource_id: resource for resource in boundary.resources()}
    assert later_path.lstat().st_size == 0
    assert (
        refreshed[later_resource.resource_id].cleanup_status
        is WarehouseResourceCleanupStatus.COMPLETE
    )


def test_clickhouse_receipt_replay_accumulates_disappearance_after_lstat_and_continues_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    provider.validate(validating_binding, _operation(), resume=False)
    resources_by_name = {
        Path(resource.provider_resource_handle).name: resource for resource in boundary.resources()
    }
    raced_resource = resources_by_name["restore-users.xml"]
    later_resource = resources_by_name["clickhouse-restore-password"]
    raced_path = Path(raced_resource.provider_resource_handle)
    later_path = Path(later_resource.provider_resource_handle)
    raced_path.write_text("raced-sensitive-material", encoding="utf-8")
    later_path.write_text("later-sensitive-material", encoding="utf-8")
    for resource in (raced_resource, later_resource):
        recorder.resources[resource.resource_id] = resource.model_copy(
            update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
        )
    original_lstat = Path.lstat
    raced = False

    def lstat_then_disappear(path: Path) -> Any:
        nonlocal raced
        status_result = original_lstat(path)
        if path == raced_path and not raced:
            raced = True
            path.unlink()
        return status_result

    monkeypatch.setattr(Path, "lstat", lstat_then_disappear)

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert raced
    assert captured.value.classification is WarehouseFailureClassification.INVALID_PROVIDER_RESPONSE
    refreshed = {resource.resource_id: resource for resource in boundary.resources()}
    assert later_path.lstat().st_size == 0
    assert (
        refreshed[later_resource.resource_id].cleanup_status
        is WarehouseResourceCleanupStatus.COMPLETE
    )


def test_clickhouse_receipt_replay_removes_the_native_backup_name(tmp_path: Path) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient(compose=compose)
    provider, _boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    provider.validate(validating_binding, _operation(), resume=False)
    attempts_before_replay = compose.native_cleanup_attempts
    compose.native_backup_present = True

    provider.validate(validating_binding, _operation(), resume=False)

    assert compose.native_cleanup_attempts == attempts_before_replay + 1
    assert not compose.native_backup_present


def test_clickhouse_receipt_replay_fails_closed_when_cleanup_absence_is_ambiguous(
    tmp_path: Path,
) -> None:
    class _AmbiguousCleanupCompose(_RecordingCompose):
        ambiguous = False

        def resource_is_absent(self, **arguments: Any) -> bool | None:
            if self.ambiguous and arguments["resource_kind"] == "container":
                return None
            return super().resource_is_absent(**arguments)

    recorder = _RecordingResources()
    compose = _AmbiguousCleanupCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    provider.validate(validating_binding, _operation(), resume=False)
    restore_container = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
    )
    recorder.resources[restore_container.resource_id] = restore_container.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )
    compose.ambiguous = True

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


def test_clickhouse_receipt_replay_never_classifies_uncertain_docker_cleanup_as_sql_rejection(
    tmp_path: Path,
) -> None:
    class _RejectedCleanupCompose(_RecordingCompose):
        reject_cleanup = False

        def remove_resource(self, **arguments: Any) -> None:
            if self.reject_cleanup and arguments["resource_kind"] == "container":
                raise ComposeCommandError(
                    "sanitized Docker cleanup failure",
                    classification="rejected",
                )
            super().remove_resource(**arguments)

    recorder = _RecordingResources()
    compose = _RejectedCleanupCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3)
    provider.validate(validating_binding, _operation(), resume=False)
    restore_container = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
    )
    recorder.resources[restore_container.resource_id] = restore_container.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )
    compose.removed.discard(("container", restore_container.provider_resource_handle))
    compose.reject_cleanup = True

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


@pytest.mark.parametrize(
    ("compose_classification", "provider_classification"),
    (
        ("unavailable", WarehouseFailureClassification.TRANSIENT_UNAVAILABLE),
        ("timeout", WarehouseFailureClassification.TRANSIENT_TRANSPORT),
    ),
)
def test_clickhouse_receipt_cleanup_transport_failures_remain_retryable_at_provider_boundary(
    tmp_path: Path,
    compose_classification: Literal["unavailable", "timeout"],
    provider_classification: WarehouseFailureClassification,
) -> None:
    class _CleanupTransportCompose(_RecordingCompose):
        fail_cleanup = False

        def resource_is_absent(self, **arguments: Any) -> bool:
            if self.fail_cleanup and arguments["resource_kind"] == "container":
                raise ComposeCommandError(
                    "sanitized Docker cleanup transport failure",
                    classification=compose_classification,
                )
            return super().resource_is_absent(**arguments)

    recorder = _RecordingResources()
    compose = _CleanupTransportCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3)
    provider.validate(validating_binding, _operation(), resume=False)
    restore_container = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
    )
    recorder.resources[restore_container.resource_id] = restore_container.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )
    compose.fail_cleanup = True

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(validating_binding, _operation(), resume=False)

    assert captured.value.classification is provider_classification


class _RestoreIsolationCompose(_RecordingCompose):
    def __init__(self, recorder: _RecordingResources) -> None:
        super().__init__(recorder)
        self.container_networks: dict[str, tuple[str, ...] | None] = {}
        self.network_ipv4_addresses: dict[tuple[str, str], str | None] = {}
        self.network_internal: dict[str, bool | None] = {}
        self.published_ports: dict[str, bool | None] = {}
        self.probe_output = b"restore_endpoint_denied_tcp\n"
        self.probe_calls: list[tuple[str, ...]] = []
        self.probe_project_names: list[str] = []
        self.network_inspection_calls: list[str] = []

    def inspect_container_networks(self, **arguments: Any) -> tuple[str, ...] | None:
        identifier = str(arguments["identifier"])
        self.network_inspection_calls.append(identifier)
        if identifier in self.container_networks:
            return self.container_networks[identifier]
        return super().inspect_container_networks(**arguments)

    def inspect_network_internal(self, **arguments: Any) -> bool | None:
        identifier = str(arguments["identifier"])
        if identifier in self.network_internal:
            return self.network_internal[identifier]
        return super().inspect_network_internal(**arguments)

    def inspect_container_network_ipv4_address(self, **arguments: Any) -> str | None:
        key = (str(arguments["identifier"]), str(arguments["network_name"]))
        if key in self.network_ipv4_addresses:
            address = self.network_ipv4_addresses[key]
            if not isinstance(address, str):
                return None
            return DockerComposeProcess._validated_network_ipv4_address({"IPAddress": address})
        return super().inspect_container_network_ipv4_address(**arguments)

    def inspect_container_has_published_ports(self, **arguments: Any) -> bool | None:
        identifier = str(arguments["identifier"])
        if identifier in self.published_ports:
            return self.published_ports[identifier]
        return super().inspect_container_has_published_ports(**arguments)

    def exec(self, **arguments: Any) -> bytes:
        command_arguments = tuple(arguments.get("arguments", ()))
        if any("restore_endpoint_reachable" in argument for argument in command_arguments):
            self.probe_calls.append(command_arguments)
            self.probe_project_names.append(str(arguments["project_name"]))
            return self.probe_output
        return super().exec(**arguments)


def _restore_isolation_fixture(
    tmp_path: Path,
) -> tuple[
    _RestoreIsolationCompose,
    warehouse_module._ClickHouseWarehouseIdentity,
    warehouse_module._ClickHouseRestoreIdentity,
]:
    recorder = _RecordingResources()
    compose = _RestoreIsolationCompose(recorder)
    primary = warehouse_module._warehouse_identity(_binding(), tmp_path)
    restore = warehouse_module._restore_identity(_binding(), _operation())
    probe_container_name = f"{restore.container_name}-isolation-probe"
    primary_networks = tuple(sorted((primary.private_network_name, primary.loopback_network_name)))
    restore_networks = tuple(sorted((restore.private_network_name, restore.loopback_network_name)))
    compose.container_networks = {
        primary.container_name: primary_networks,
        restore.container_name: restore_networks,
        probe_container_name: (primary.private_network_name,),
    }
    compose.network_internal = {
        restore.private_network_name: True,
        restore.loopback_network_name: True,
        primary.private_network_name: True,
    }
    compose.published_ports = {
        restore.container_name: False,
        probe_container_name: False,
    }
    compose.network_ipv4_addresses = {
        (restore.container_name, restore.private_network_name): "172.31.0.9"
    }
    return compose, primary, restore


def test_clickhouse_restore_isolation_digest_binds_observed_topology_and_denial(
    tmp_path: Path,
) -> None:
    compose, primary, restore = _restore_isolation_fixture(tmp_path)
    primary_networks = tuple(sorted((primary.private_network_name, primary.loopback_network_name)))
    restore_networks = tuple(sorted((restore.private_network_name, restore.loopback_network_name)))

    observed = warehouse_module._observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={},
        restore_environment={},
    )

    assert observed == digest(
        {
            "domain": "pillarmesh-clickhouse-restore-network-isolation-v4",
            "primary_networks": primary_networks,
            "restore_networks": restore_networks,
            "restore_network_internal": (
                (restore.private_network_name, True),
                (restore.loopback_network_name, True),
            ),
            "restore_published_ports": False,
            "probe_networks": (primary.private_network_name,),
            "probe_published_ports": False,
            "restore_endpoint": (restore.private_network_name, "172.31.0.9", 8443),
            "outside_probe": "restore_endpoint_denied_tcp",
        }
    )
    probe_arguments = compose.probe_calls[-1]
    assert probe_arguments[-2:] == ("172.31.0.9", "8443")
    assert restore.container_name not in probe_arguments
    assert "clickhouse_isolation_probe" in probe_arguments
    assert compose.probe_project_names == [restore.project_name]
    assert f"{restore.container_name}-isolation-probe" in compose.network_inspection_calls


def test_clickhouse_restore_isolation_digest_changes_with_the_inspected_endpoint(
    tmp_path: Path,
) -> None:
    compose, primary, restore = _restore_isolation_fixture(tmp_path)
    first = warehouse_module._observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={},
        restore_environment={},
    )
    compose.network_ipv4_addresses[(restore.container_name, restore.private_network_name)] = (
        "172.31.0.10"
    )

    second = warehouse_module._observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={},
        restore_environment={},
    )

    assert second != first
    assert compose.probe_calls[-1][-2:] == ("172.31.0.10", "8443")


def _run_captured_restore_probe_script(
    tmp_path: Path,
    *,
    status: int,
    diagnostic: str,
) -> subprocess.CompletedProcess[bytes]:
    compose, primary, restore = _restore_isolation_fixture(tmp_path)
    warehouse_module._observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={},
        restore_environment={},
    )
    probe_script = next(
        argument for argument in compose.probe_calls[-1] if "restore_endpoint_reachable" in argument
    )
    command_directory = tmp_path / "probe-bin"
    command_directory.mkdir()
    timeout_command = command_directory / "timeout"
    timeout_command.write_text(
        '#!/bin/sh\nprintf \'%s\' "$PROBE_DIAGNOSTIC" >&2\nexit "$PROBE_STATUS"\n',
        encoding="utf-8",
    )
    timeout_command.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{command_directory}:{os.environ['PATH']}",
        "PROBE_STATUS": str(status),
        "PROBE_DIAGNOSTIC": diagnostic,
    }
    return subprocess.run(
        ("bash", "-ceu", probe_script, "--", "172.31.0.9", "8443"),
        env=environment,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("status", (124, 125, 126, 127))
def test_clickhouse_restore_probe_timeout_and_invocation_statuses_never_prove_isolation(
    tmp_path: Path,
    status: int,
) -> None:
    result = _run_captured_restore_probe_script(
        tmp_path,
        status=status,
        diagnostic="sanitized probe invocation failure",
    )

    assert result.returncode == status
    assert b"restore_endpoint_denied" not in result.stdout


def test_clickhouse_restore_probe_dns_failure_never_proves_isolation(tmp_path: Path) -> None:
    result = _run_captured_restore_probe_script(
        tmp_path,
        status=1,
        diagnostic="bash: host: Name or service not known",
    )

    assert result.returncode == 1
    assert b"restore_endpoint_denied" not in result.stdout


@pytest.mark.parametrize(
    "diagnostic",
    (
        "bash: connect: Connection refused",
        "bash: connect: Network is unreachable",
        "bash: connect: No route to host",
    ),
)
def test_clickhouse_restore_probe_accepts_only_recognized_tcp_denials(
    tmp_path: Path,
    diagnostic: str,
) -> None:
    result = _run_captured_restore_probe_script(
        tmp_path,
        status=1,
        diagnostic=diagnostic,
    )

    assert result.returncode == 0
    assert result.stdout == b"restore_endpoint_denied_tcp\n"


@pytest.mark.parametrize(
    "defect",
    [
        "published_port",
        "external_network",
        "non_internal_network",
        "reachable_probe",
        "resolution_failure",
        "malformed_endpoint",
        "non_routable_endpoint",
        "malformed_inspect",
        "malformed_probe",
        "probe_on_restore_network",
        "probe_published_port",
    ],
)
def test_clickhouse_restore_isolation_refuses_unproven_topology(
    tmp_path: Path,
    defect: str,
) -> None:
    compose, primary, restore = _restore_isolation_fixture(tmp_path)
    if defect == "published_port":
        compose.published_ports[restore.container_name] = True
    elif defect == "external_network":
        compose.container_networks[restore.container_name] = (
            restore.private_network_name,
            "unexpected-external-network",
        )
    elif defect == "non_internal_network":
        compose.network_internal[restore.loopback_network_name] = False
    elif defect == "reachable_probe":
        compose.probe_output = b"restore_endpoint_reachable\n"
    elif defect == "resolution_failure":
        compose.probe_output = b"restore_endpoint_resolution_failed\n"
    elif defect == "malformed_endpoint":
        compose.network_ipv4_addresses[(restore.container_name, restore.private_network_name)] = (
            None
        )
    elif defect == "non_routable_endpoint":
        compose.network_ipv4_addresses[(restore.container_name, restore.private_network_name)] = (
            "192.0.2.3"
        )
    elif defect == "malformed_inspect":
        compose.container_networks[restore.container_name] = None
    elif defect == "probe_on_restore_network":
        compose.container_networks[f"{restore.container_name}-isolation-probe"] = (
            restore.private_network_name,
        )
    elif defect == "probe_published_port":
        compose.published_ports[f"{restore.container_name}-isolation-probe"] = True
    else:
        compose.probe_output = b"untrusted-output\n"

    with pytest.raises(ComposeCommandError) as captured:
        warehouse_module._observe_restore_isolation(
            compose,
            primary=primary,
            restore=restore,
            primary_environment={},
            restore_environment={},
        )

    assert captured.value.classification == "ambiguous"


@pytest.mark.parametrize(
    ("compose_classification", "provider_classification"),
    (
        (
            "unavailable",
            WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        ),
        (
            "timeout",
            WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        ),
    ),
)
def test_clickhouse_restore_probe_transport_failures_remain_retryable_at_provider_boundary(
    tmp_path: Path,
    compose_classification: Literal["unavailable", "timeout"],
    provider_classification: WarehouseFailureClassification,
) -> None:
    class _ProbeFailureCompose(_RestoreIsolationCompose):
        def exec(self, **arguments: Any) -> bytes:
            command_arguments = tuple(arguments.get("arguments", ()))
            if any("restore_endpoint_reachable" in argument for argument in command_arguments):
                raise ComposeCommandError(
                    "sanitized restore probe failure",
                    classification=compose_classification,
                )
            return super().exec(**arguments)

    private_directory = tmp_path / "private"
    recorder = _RecordingResources()
    compose = _ProbeFailureCompose(recorder)
    primary = warehouse_module._warehouse_identity(_binding(), private_directory)
    restore = warehouse_module._restore_identity(_binding(), _operation())
    compose.container_networks = {
        primary.container_name: tuple(
            sorted((primary.private_network_name, primary.loopback_network_name))
        ),
        restore.container_name: tuple(
            sorted((restore.private_network_name, restore.loopback_network_name))
        ),
    }
    compose.network_internal = {
        restore.private_network_name: True,
        restore.loopback_network_name: True,
    }
    compose.published_ports = {restore.container_name: False}
    compose.network_ipv4_addresses = {
        (restore.container_name, restore.private_network_name): "172.31.0.9"
    }
    client = _RecordingClient()
    provider, _boundary = _provider_with_native_backup(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is provider_classification


def test_clickhouse_restore_tunnel_reserves_its_port_until_start() -> None:
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=object(),
        project_name="restore-project",
    )
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            competitor.bind(("127.0.0.1", tunnel.port))
    finally:
        competitor.close()
        tunnel.close()


def test_clickhouse_restore_tunnel_forwards_directly_and_releases_its_port() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class _ForwardingCompose:
        @contextmanager
        def exec_stream(
            self,
            *,
            project_name: str,
            arguments: tuple[str, ...],
            stdin: Any,
            **_kwargs: object,
        ) -> Any:
            calls.append((project_name, arguments))
            assert stdin.read(4) == b"ping"
            yield io.BytesIO(b"pong")

    compose: Any = _ForwardingCompose()
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=compose,
        project_name="restore-project",
    )
    port = tunnel.port
    try:
        tunnel.start(environment={"scope": "restore"})
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"ping")
            client.shutdown(socket.SHUT_WR)

            assert client.recv(4) == b"pong"
        tunnel.assert_healthy()
    finally:
        tunnel.close()

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)
    assert calls[0][0] == "restore-project"
    assert calls[0][1][:7] == (
        "--profile",
        "restore",
        "exec",
        "-T",
        "clickhouse_restore",
        "bash",
        "-ceu",
    )


def test_clickhouse_restore_tunnel_close_cancels_and_joins_active_handlers() -> None:
    handler_started = Event()

    class _BlockingCompose:
        @contextmanager
        def exec_stream(self, *, stdin: Any, **_kwargs: object) -> Any:
            handler_started.set()
            while stdin.read(4096):
                pass
            yield io.BytesIO()

    compose: Any = _BlockingCompose()
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=compose,
        project_name="restore-project",
    )
    tunnel.start(environment={"scope": "restore"})
    client = socket.create_connection(("127.0.0.1", tunnel.port), timeout=2)
    assert handler_started.wait(timeout=2)
    close_failures: list[BaseException] = []

    def close_tunnel() -> None:
        try:
            tunnel.close()
        except BaseException as error:
            close_failures.append(error)

    close_thread = Thread(target=close_tunnel, daemon=True)
    close_thread.start()
    try:
        close_thread.join(timeout=2)
        assert not close_thread.is_alive()
        assert close_failures == []
        assert tunnel._handler_threads
        assert all(not handler.is_alive() for handler in tunnel._handler_threads)
    finally:
        client.close()
        close_thread.join(timeout=2)


def test_clickhouse_restore_tunnel_start_failure_is_retryable_and_cleanup_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailedThread:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def join(self, timeout: float) -> None:
            assert timeout > 0

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(warehouse_module.threading, "Thread", _FailedThread)
    tunnel = warehouse_module._RestoreLoopbackTunnel(
        compose=object(),
        project_name="restore-project",
    )

    with pytest.raises(ComposeCommandError) as captured:
        tunnel.start(environment={"scope": "restore"})

    assert captured.value.classification == "unavailable"
    tunnel.close()


def test_clickhouse_initial_validation_uses_observed_restore_isolation(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    compose, primary, restore = _restore_isolation_fixture(private_directory)
    recorder = compose._recorder
    client = _RecordingClient()
    provider, _boundary = _provider_with_native_backup(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    expected_digest = warehouse_module._observe_restore_isolation(
        compose,
        primary=primary,
        restore=restore,
        primary_environment={},
        restore_environment={},
    )

    validation = provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    assert isinstance(validation, InitialWarehouseValidationResult)
    assert validation.evidence.network_isolation_probe_digest == expected_digest


def test_clickhouse_initial_validation_refuses_a_published_restore_port(
    tmp_path: Path,
) -> None:
    private_directory = tmp_path / "private"
    compose, _primary, restore = _restore_isolation_fixture(private_directory)
    compose.published_ports[restore.container_name] = True
    recorder = compose._recorder
    client = _RecordingClient()
    provider, _boundary = _provider_with_native_backup(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME


def test_clickhouse_restore_probe_must_be_running_before_its_creation_is_recorded(
    tmp_path: Path,
) -> None:
    class _StoppedProbeCompose(_RecordingCompose):
        def inspect_container_running(self, **arguments: Any) -> bool:
            return not str(arguments["identifier"]).endswith("-isolation-probe")

    recorder = _RecordingResources()
    compose = _StoppedProbeCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME
    probe_resource = next(
        resource
        for resource in boundary.resources()
        if resource.provider_resource_handle.endswith("-isolation-probe")
    )
    assert probe_resource.creation_state is WarehouseResourceCreationState.PLANNED
    assert probe_resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
    assert (
        "container",
        probe_resource.provider_resource_handle,
    ) in compose.removed


def test_clickhouse_restore_probe_crash_replay_reopens_and_cleans_the_exact_resource(
    tmp_path: Path,
) -> None:
    class _CheckpointCrash(BaseException):
        pass

    crash_pending = True

    def crash_after_restore_created(checkpoint: WarehouseLifecycleCheckpoint) -> None:
        nonlocal crash_pending
        if checkpoint is WarehouseLifecycleCheckpoint.AFTER_RESTORE_CREATED and crash_pending:
            crash_pending = False
            raise _CheckpointCrash

    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
        fault_hook=crash_after_restore_created,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3)

    with pytest.raises(_CheckpointCrash):
        provider.validate(validating_binding, _operation(), resume=False)

    probe_after_crash = next(
        resource
        for resource in boundary.resources()
        if resource.provider_resource_handle.endswith("-isolation-probe")
    )
    assert probe_after_crash.creation_state is WarehouseResourceCreationState.CREATED
    assert probe_after_crash.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
    assert (
        "container",
        probe_after_crash.provider_resource_handle,
    ) in compose.removed
    recorder.reopen_calls.clear()

    recovered = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)
    assert len(recorder.reopen_calls) == 1
    reopened_probe = next(
        resource
        for resource in recorder.reopen_calls[0]
        if resource.provider_resource_handle == probe_after_crash.provider_resource_handle
    )
    assert reopened_probe.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
    final_probe = next(
        resource
        for resource in boundary.resources()
        if resource.resource_id == probe_after_crash.resource_id
    )
    assert final_probe.creation_state is WarehouseResourceCreationState.CREATED
    assert final_probe.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE


def test_clickhouse_failed_restore_never_replays_as_verified(tmp_path: Path) -> None:
    class _OneCleanupFailureCompose(_RecordingCompose):
        def __init__(self, recorder: _RecordingResources) -> None:
            super().__init__(recorder)
            self.cleanup_failures = 1

        def remove_resource(self, **arguments: Any) -> None:
            if self.cleanup_failures:
                self.cleanup_failures -= 1
                self._recorder.cleanup_record_failures = 1
                self.events.append(f"remove_failed:{arguments['resource_kind']}")
                raise RuntimeError("injected cleanup failure")
            super().remove_resource(**arguments)

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    recorder = _RecordingResources()
    compose = _OneCleanupFailureCompose(recorder)
    client = _RecordingClient(restore_failures=1)
    settings = ClickHouseWarehouseSettings(
        private_operation_directory=private_directory,
        retention_period=timedelta(hours=1),
        backup_chunk_bytes=4096,
    )

    boundary = ClickHouseBackupCommandBoundary(
        settings=settings,
        compose=compose,
        resource_recorder=recorder,
        secret_capability=_BackupSecretCapability(
            password=SecretStr("backup-secret"),
            key=SecretStr(b64encode(b"k" * 32).decode("ascii")),
        ),
        retirement_capability=_BackupRetirementCapability(),
        client_factory=lambda target: _recording_client_for_target(client, target),
        clock=lambda: _NOW,
        entropy=lambda size: b"n" * size,
    )
    provider = _provider(
        private_directory,
        recorder=recorder,
        compose=compose,
        client=client,
        backup_commands=boundary,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )

    with pytest.raises(WarehouseProviderError) as first_failure:
        provider.validate(validating_binding, _operation(), resume=False)

    assert first_failure.value.classification is WarehouseFailureClassification.INTEGRITY_FAILURE
    receipt_after_failure = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
    )
    assert receipt_after_failure.creation_state is WarehouseResourceCreationState.PLANNED
    assert not Path(receipt_after_failure.provider_resource_handle).exists()
    assert "remove_failed:container" in compose.events
    assert "remove:volume" in compose.events
    restore_after_failure = tuple(
        resource
        for resource in boundary.resources()
        if resource.resource_kind
        in {
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
    )
    assert any(
        resource.cleanup_status
        in {WarehouseResourceCleanupStatus.FAILED, WarehouseResourceCleanupStatus.PENDING}
        for resource in restore_after_failure
    )
    assert any(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in restore_after_failure
    )

    recovered = provider.validate(validating_binding, _operation(), resume=False)
    replayed = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)
    assert isinstance(replayed, InitialWarehouseValidationResult)
    assert replayed.restore_verification == recovered.restore_verification
    assert sum(statement.startswith("RESTORE ") for statement in client.statements) == 2


def test_clickhouse_tunnel_reservation_failure_tombstones_decrypted_staging_and_cleanup_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())

    def unavailable_socket(*_arguments: object, **_keywords: object) -> socket.socket:
        raise OSError("sanitized tunnel reservation failure")

    monkeypatch.setattr(warehouse_module.socket, "socket", unavailable_socket)

    with pytest.raises(WarehouseProviderError) as captured:
        provider.validate(
            _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
            _operation(),
            resume=False,
        )

    assert captured.value.classification is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    ephemeral_resources = tuple(
        resource
        for resource in boundary.resources()
        if Path(resource.provider_resource_handle).name
        in {"clickhouse-restore.zip", "clickhouse-restore-password", "restore-users.xml"}
    )
    assert len(ephemeral_resources) == 3
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in ephemeral_resources
    )
    staging = next(
        resource
        for resource in ephemeral_resources
        if Path(resource.provider_resource_handle).name == "clickhouse-restore.zip"
    )
    staging_status = Path(staging.provider_resource_handle).lstat()
    assert staging_status.st_size == 0
    assert staging_status.st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "cleanup_states",
    (
        (
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.FAILED,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
        ),
        (
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.PENDING,
        ),
        (
            WarehouseResourceCleanupStatus.FAILED,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.FAILED,
            WarehouseResourceCleanupStatus.PENDING,
            WarehouseResourceCleanupStatus.COMPLETE,
            WarehouseResourceCleanupStatus.FAILED,
        ),
    ),
)
def test_clickhouse_mixed_restore_cleanup_reconciles_and_reopens_every_resource_before_up(
    tmp_path: Path,
    cleanup_states: tuple[
        WarehouseResourceCleanupStatus,
        WarehouseResourceCleanupStatus,
        WarehouseResourceCleanupStatus,
        WarehouseResourceCleanupStatus,
        WarehouseResourceCleanupStatus,
        WarehouseResourceCleanupStatus,
    ],
) -> None:
    class _RecreationStateCompose(_RecordingCompose):
        def __init__(self, recorder: _RecordingResources) -> None:
            super().__init__(recorder)
            self.restore_states_before_up: tuple[
                tuple[
                    WarehouseResourceCreationState,
                    WarehouseResourceCleanupStatus,
                    WarehouseFailureClassification | None,
                ],
                ...,
            ] = ()

        def exec(self, **arguments: Any) -> bytes:
            command_arguments = tuple(arguments.get("arguments", ()))
            if command_arguments[:3] == ("--profile", "restore", "up"):
                self.restore_states_before_up = tuple(
                    (
                        resource.creation_state,
                        resource.cleanup_status,
                        resource.cleanup_failure_classification,
                    )
                    for resource in self._recorder.resources.values()
                    if resource.resource_kind
                    in {
                        WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
                        WarehouseResourceKind.RESTORE_CONTAINER,
                        WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
                        WarehouseResourceKind.RESTORE_DATA_VOLUME,
                    }
                )
            return super().exec(**arguments)

    recorder = _RecordingResources()
    compose = _RecreationStateCompose(recorder)
    client = _RecordingClient(restore_failures=1)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3)

    with pytest.raises(WarehouseProviderError):
        provider.validate(validating_binding, _operation(), resume=False)

    restore_resources = tuple(
        resource
        for resource in boundary.resources()
        if resource.resource_kind
        in {
            WarehouseResourceKind.RESTORE_COMPOSE_PROJECT,
            WarehouseResourceKind.RESTORE_CONTAINER,
            WarehouseResourceKind.RESTORE_PRIVATE_NETWORK,
            WarehouseResourceKind.RESTORE_DATA_VOLUME,
        }
    )
    assert len(restore_resources) == len(cleanup_states)
    for resource, cleanup_status in zip(restore_resources, cleanup_states, strict=True):
        recorder.resources[resource.resource_id] = resource.model_copy(
            update={
                "cleanup_status": cleanup_status,
                "cleanup_failure_classification": (
                    WarehouseFailureClassification.AMBIGUOUS_OUTCOME
                    if cleanup_status is WarehouseResourceCleanupStatus.FAILED
                    else None
                ),
            }
        )
    recorder.reopen_calls.clear()

    recovered = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)
    assert len(recorder.reopen_calls) == 1
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        and resource.cleanup_failure_classification is None
        for resource in recorder.reopen_calls[0]
    )
    assert compose.restore_states_before_up
    assert all(
        state
        == (
            WarehouseResourceCreationState.PLANNED,
            WarehouseResourceCleanupStatus.PENDING,
            None,
        )
        for state in compose.restore_states_before_up
    )


def test_clickhouse_restore_start_failure_erases_plaintext_and_credentials(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    compose.restore_up_failures = 1
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )

    with pytest.raises(WarehouseProviderError):
        provider.validate(validating_binding, _operation(), resume=False)

    ephemeral_files = tuple(
        resource
        for resource in boundary.resources()
        if Path(resource.provider_resource_handle).name
        in {"clickhouse-restore.zip", "clickhouse-restore-password", "restore-users.xml"}
    )
    assert len(ephemeral_files) == 3
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        and Path(resource.provider_resource_handle).stat().st_size == 0
        for resource in ephemeral_files
    )
    receipt = next(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT
    )
    assert not Path(receipt.provider_resource_handle).exists()

    recovered = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)


def test_clickhouse_restore_isolation_probe_container_is_durable_and_terminally_cleaned(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=_RecordingClient(),
    )
    provider.provision(_binding(), _operation())

    provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )

    restore = warehouse_module._restore_identity(_binding(), _operation())
    restore_containers = tuple(
        resource
        for resource in boundary.resources()
        if resource.resource_kind is WarehouseResourceKind.RESTORE_CONTAINER
    )
    assert {resource.provider_resource_handle for resource in restore_containers} == {
        restore.container_name,
        f"{restore.container_name}-isolation-probe",
    }
    assert all(
        resource.creation_state is WarehouseResourceCreationState.CREATED
        and resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
        for resource in restore_containers
    )


def test_clickhouse_native_archive_cleanup_is_retried_after_artifact_commit(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    compose.native_cleanup_failures = 1
    compose.native_cleanup_ignored_attempts = 1
    client = _RecordingClient()
    provider, _boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )

    with pytest.raises(WarehouseProviderError):
        provider.validate(validating_binding, _operation(), resume=False)

    recovered = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)
    # Pre-clean, the failed post-backup cleanup, then the recovery cleanup.
    assert compose.native_cleanup_attempts == 3
    assert sum(statement.startswith("BACKUP ") for statement in client.statements) == 1


def test_clickhouse_replays_backup_after_a_kill_leaves_the_native_archive(
    tmp_path: Path,
) -> None:
    """A hard kill skips the finally that removes the fixed-name native backup.

    The encrypted artifact was never promoted, so the replay must create it
    again, which means it must first clear the name the killed attempt left
    behind. Without that, ClickHouse rejects the replayed BACKUP as already
    existing and the binding fails instead of converging.
    """
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient(compose=compose)
    provider, _boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    validating_binding = _binding(
        lifecycle_state=WarehouseBindingState.VALIDATING,
        revision=3,
    )
    # The state a killed attempt leaves behind: the native archive is present and
    # no encrypted artifact was committed.
    compose.native_backup_present = True

    recovered = provider.validate(validating_binding, _operation(), resume=False)

    assert isinstance(recovered, InitialWarehouseValidationResult)
    assert sum(statement.startswith("BACKUP ") for statement in client.statements) == 1


def test_clickhouse_staging_retirement_failure_does_not_stop_other_dispositions(
    tmp_path: Path,
) -> None:
    recorder = _RecordingResources()
    compose = _RecordingCompose(recorder)
    client = _RecordingClient()
    provider, boundary = _provider_with_native_backup(
        tmp_path / "private",
        recorder=recorder,
        compose=compose,
        client=client,
    )
    provider.provision(_binding(), _operation())
    provider.validate(
        _binding(lifecycle_state=WarehouseBindingState.VALIDATING, revision=3),
        _operation(),
        resume=False,
    )
    staging = next(
        resource
        for resource in boundary.resources()
        if Path(resource.provider_resource_handle).name == "clickhouse-restore.zip"
    )
    staging_path = Path(staging.provider_resource_handle)
    staging_path.chmod(0o644)
    recorder.resources[staging.resource_id] = staging.model_copy(
        update={"cleanup_status": WarehouseResourceCleanupStatus.PENDING}
    )

    boundary.retire_backup(
        binding=_binding(lifecycle_state=WarehouseBindingState.READY, revision=4)
    )

    refreshed = boundary.resources()
    failed_staging = next(
        resource for resource in refreshed if resource.resource_id == staging.resource_id
    )
    assert failed_staging.cleanup_status is WarehouseResourceCleanupStatus.FAILED
    assert all(
        resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
        for resource in refreshed
        if resource.resource_kind
        in {
            WarehouseResourceKind.BACKUP_ARTIFACT,
            WarehouseResourceKind.BACKUP_ENCRYPTION_KEY,
            WarehouseResourceKind.RESTORE_VERIFICATION_RECEIPT,
        }
    )


def _recording_client_for_target(
    client: _RecordingClient,
    target: ClickHouseConnectionTarget,
) -> _RecordingClient:
    client.username = target.username
    client.endpoint = target.endpoint
    return client


def test_clickhouse_private_file_rewrite_does_not_swallow_a_body_file_exists_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private.bin"

    with (
        pytest.raises(FileExistsError, match="body failed"),
        _rewrite_private_binary_file(path),
    ):
        raise FileExistsError("body failed")


def test_clickhouse_classifies_backup_authentication_failure_as_integrity_failure() -> None:
    assert _classification_for_error(BackupStreamIntegrityError()) is (
        WarehouseFailureClassification.INTEGRITY_FAILURE
    )


def test_clickhouse_grant_plan_uses_seven_separate_roles_and_six_scoped_databases() -> None:
    private_handle = "customer-private-handle-never-in-sql"

    plan = derive_clickhouse_grant_plan(private_handle)

    assert tuple(plan.databases) == (
        "raw",
        "conformed",
        "product",
        "consumption",
        "quarantine",
        "control",
    )
    assert tuple(plan.roles) == (
        "administration",
        "ingestion_runtime",
        "transformation_runtime",
        "backup_restore",
        "customer_sql",
        "catalog",
        "bi",
    )
    assert tuple(plan.users) == tuple(plan.roles)
    assert len(set(plan.users.values())) == 7
    assert len(set(plan.databases.values())) == 6
    assert len(set(plan.roles.values())) == 7
    assert private_handle not in "\n".join(plan.statements)


def test_clickhouse_nonadministration_grants_never_receive_role_or_global_authority() -> None:
    plan = derive_clickhouse_grant_plan("private-handle")
    administration_role = plan.roles["administration"]

    for statement in plan.statements:
        if statement.startswith("GRANT ") and (
            "CREATE USER" in statement or "CREATE ROLE" in statement or "ROLE ADMIN" in statement
        ):
            assert statement.endswith(f"TO `{administration_role}`")
        if not statement.endswith(f"TO `{administration_role}`"):
            assert " ON *.* " not in statement
            assert " WITH GRANT OPTION" not in statement


def test_clickhouse_administration_role_can_observe_validation_system_tables() -> None:
    plan = derive_clickhouse_grant_plan("private-handle")
    administration_role = plan.roles["administration"]

    assert {
        f"GRANT SELECT ON system.roles TO `{administration_role}`",
        f"GRANT SELECT ON system.role_grants TO `{administration_role}`",
        f"GRANT SELECT ON system.users TO `{administration_role}`",
        f"GRANT SELECT ON system.grants TO `{administration_role}`",
        f"GRANT SELECT ON system.asynchronous_metrics TO `{administration_role}`",
    }.issubset(plan.statements)


def test_clickhouse_administration_role_does_not_inherit_backup_through_all_privileges() -> None:
    plan = derive_clickhouse_grant_plan("private-handle")
    administration_role = plan.roles["administration"]

    administration_grants = tuple(
        statement
        for statement in plan.statements
        if statement.startswith("GRANT ") and statement.endswith(f"TO `{administration_role}`")
    )

    assert all("GRANT ALL" not in statement for statement in administration_grants)
    assert all("BACKUP" not in statement for statement in administration_grants)


def test_clickhouse_customer_and_bi_grants_are_explicit_certified_objects() -> None:
    plan = derive_clickhouse_grant_plan("private-handle")
    consumption = plan.databases["consumption"]

    assert (
        f"GRANT SELECT ON `{consumption}`.`customer_probe` TO `{plan.roles['customer_sql']}`"
        in plan.statements
    )
    assert (
        f"GRANT SELECT ON `{consumption}`.`certified_probe` TO `{plan.roles['bi']}`"
        in plan.statements
    )
    assert all(
        not statement.startswith(f"GRANT SELECT ON `{consumption}`.*")
        for statement in plan.statements
    )


@pytest.mark.parametrize(
    "unsupported_semantic",
    (
        "multi_statement_transaction",
        "transactional_ddl_rollback",
        "deferred_foreign_key",
        "row_by_row_update",
        "row_by_row_delete",
    ),
)
def test_clickhouse_unsupported_semantics_fail_as_permanent_configuration(
    unsupported_semantic: str,
) -> None:
    with pytest.raises(WarehouseProviderError) as captured:
        assert_supported_semantics(frozenset({unsupported_semantic}))

    assert captured.value.operation == "validate"
    assert captured.value.classification is (WarehouseFailureClassification.PERMANENT_CONFIGURATION)


def test_clickhouse_http_client_executes_against_the_scoped_https_endpoint(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"25.8.32.4\n")

    client = ClickHouseHTTPClient(
        ClickHouseConnectionTarget(
            endpoint="https://localhost:18443",
            username="pm_administration",
            password="private-password",
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        transport=httpx.MockTransport(respond),
    )
    try:
        lines = client.query_lines("SELECT version()")
    finally:
        client.close()

    assert lines == ("25.8.32.4",)
    assert len(requests) == 1
    assert requests[0].url == "https://localhost:18443/"
    assert requests[0].content == b"SELECT version() FORMAT TabSeparatedRaw"
    assert requests[0].headers["authorization"].startswith("Basic ")
    assert b"private-password" not in requests[0].content


@pytest.mark.parametrize(
    ("status_code", "classification"),
    (
        (401, WarehouseFailureClassification.AUTHORIZATION_DENIED),
        (403, WarehouseFailureClassification.AUTHORIZATION_DENIED),
        (429, WarehouseFailureClassification.THROTTLED),
        (503, WarehouseFailureClassification.TRANSIENT_UNAVAILABLE),
        (400, WarehouseFailureClassification.STATEMENT_REJECTED),
        (500, WarehouseFailureClassification.TRANSIENT_UNAVAILABLE),
    ),
)
def test_clickhouse_http_client_sanitizes_server_failures(
    tmp_path: Path,
    status_code: int,
    classification: WarehouseFailureClassification,
) -> None:
    canary = "private-clickhouse-diagnostic"
    client = ClickHouseHTTPClient(
        ClickHouseConnectionTarget(
            endpoint="https://localhost:18443",
            username="pm_runtime",
            password="private-password",
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, text=canary)),
    )
    try:
        with pytest.raises(WarehouseProviderError) as captured:
            client.execute("SELECT 1")
    finally:
        client.close()

    assert captured.value.operation == "validate"
    assert captured.value.classification is classification
    assert canary not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_clickhouse_http_client_classifies_transport_without_driver_leakage(
    tmp_path: Path,
) -> None:
    canary = "private-transport-endpoint"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(canary, request=request)

    client = ClickHouseHTTPClient(
        ClickHouseConnectionTarget(
            endpoint="https://localhost:18443",
            username="pm_runtime",
            password="private-password",
            root_certificate=tmp_path / "ca.crt",
            client_certificate=tmp_path / "client.crt",
            client_private_key=tmp_path / "client.key",
        ),
        transport=httpx.MockTransport(fail),
    )
    try:
        with pytest.raises(WarehouseProviderError) as captured:
            client.execute("SELECT 1")
    finally:
        client.close()

    assert captured.value.classification is WarehouseFailureClassification.TRANSIENT_TRANSPORT
    assert canary not in repr(captured.value)
