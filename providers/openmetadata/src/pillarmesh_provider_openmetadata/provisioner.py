from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import IO, Literal, Protocol

from cryptography.fernet import Fernet
from pillarmesh_catalog_control import CatalogBindingState, CatalogValidationEvidence
from pillarmesh_catalog_control.repository import (
    CatalogOperationConflictError,
    CatalogPersistenceError,
    CatalogRepository,
    CatalogResourceKind,
    PrivateCatalogOperation,
    PrivateCatalogResource,
)
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    ComposeCommandError,
    ComposeResource,
    ComposeResourceKind,
    DockerComposeProcess,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .client import (
    CORE_UPSTREAM_IMAGES,
    OpenMetadataClient,
    OpenMetadataSettings,
    _OpenMetadataCredentials,
    _service_identity_username,
)
from .models import (
    CatalogFailureClassification,
    CatalogObjectRef,
    CatalogProviderError,
    ClassificationPayload,
    GlossaryTermPayload,
    LineagePayload,
    ProviderBuildIdentity,
    ProviderHealth,
)

_MYSQL_DUMP_DIRECTORY = "/tmp/pillarmesh-openmetadata-dump"
_MYSQL_SOCKET = "/var/lib/mysql/mysql.sock"
_OPENMETADATA_COMPOSE_TIMEOUT_SECONDS = 600.0
_OPENMETADATA_TERMINATION_GRACE_SECONDS = 5.0
_LOCAL_PROVISIONING_LOCKS_GUARD = Lock()
_LOCAL_PROVISIONING_LOCKS: dict[tuple[str, str], Lock] = {}
_DOCKER_CALLER_ENVIRONMENT_KEYS = (
    "PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
_COMPOSE_CREDENTIAL_ENVIRONMENT_KEYS = (
    "PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD",
    "PILLARMESH_OPENMETADATA_DATABASE_PASSWORD",
    "PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD",
)
_OPENMETADATA_ACCOUNT_PASSWORD_PREFIX = "Aa0!"
_OPENMETADATA_ACCOUNT_PASSWORD_MAXIMUM_LENGTH = 56
_RESTORED_SEARCH_ENTITIES = "glossary,glossaryTerm,classification,tag,user"
_SEARCH_REBUILD_ATTEMPTS = 2

type RestoreStep = Literal[
    "target reset",
    "database startup",
    "database readiness",
    "database import",
    "stack startup",
    "terminal readiness",
]


def _run_restore_step(label: RestoreStep, operation: Callable[[], None]) -> None:
    failed = False
    try:
        operation()
    except Exception:
        failed = True
    if failed:
        raise CatalogProviderError(
            f"OpenMetadata isolated restore {label} failed",
            classification="transient",
        )


class ComposeController(Protocol):
    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def stop(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def start(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def down(self, *, project_name: str, environment: Mapping[str, str]) -> None: ...

    def backup(
        self, *, project_name: str, backup_path: Path, environment: Mapping[str, str]
    ) -> None: ...

    def restore(
        self, *, project_name: str, backup_path: Path, environment: Mapping[str, str]
    ) -> None: ...

    def rebuild_search_index(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> None: ...

    def verify_pinned_images(self, *, project_name: str, environment: Mapping[str, str]) -> str: ...

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]: ...

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]: ...

    def remove_resource(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> None: ...

    def resource_is_absent(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class OpenMetadataOperationSecrets:
    admin_password: SecretStr
    runtime_password: SecretStr
    administrator_password: SecretStr
    mysql_root_password: SecretStr
    database_password: SecretStr
    airflow_database_password: SecretStr

    def compose_environment(self) -> dict[str, str]:
        return {
            "PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD": (
                self.mysql_root_password.get_secret_value()
            ),
            "PILLARMESH_OPENMETADATA_DATABASE_PASSWORD": (
                self.database_password.get_secret_value()
            ),
            "PILLARMESH_OPENMETADATA_AIRFLOW_DATABASE_PASSWORD": (
                self.airflow_database_password.get_secret_value()
            ),
        }


class OpenMetadataSecretStore(Protocol):
    def create(self) -> tuple[str, OpenMetadataOperationSecrets]: ...

    def ensure(self, secret_reference: str) -> OpenMetadataOperationSecrets: ...

    def resolve(self, secret_reference: str) -> OpenMetadataOperationSecrets: ...

    def bootstrap_admin_password(self) -> SecretStr: ...

    def delete(self, secret_reference: str) -> None: ...


class InMemoryOpenMetadataSecretStore:
    def __init__(self, *, bootstrap_admin_password: SecretStr) -> None:
        self._bootstrap_admin_password = bootstrap_admin_password
        self._secrets_by_reference: dict[str, OpenMetadataOperationSecrets] = {}

    def create(self) -> tuple[str, OpenMetadataOperationSecrets]:
        secret_reference = "om-secret-reference-" + secrets.token_urlsafe(32)
        return secret_reference, self.ensure(secret_reference)

    def ensure(self, secret_reference: str) -> OpenMetadataOperationSecrets:
        existing = self._secrets_by_reference.get(secret_reference)
        if existing is not None:
            return existing
        secrets_bundle = _new_operation_secrets()
        return self._secrets_by_reference.setdefault(secret_reference, secrets_bundle)

    def resolve(self, secret_reference: str) -> OpenMetadataOperationSecrets:
        try:
            return self._secrets_by_reference[secret_reference]
        except KeyError:
            raise RuntimeError("OpenMetadata operation secret reference is unavailable") from None

    def bootstrap_admin_password(self) -> SecretStr:
        return self._bootstrap_admin_password

    def delete(self, secret_reference: str) -> None:
        self._secrets_by_reference.pop(secret_reference, None)


class _StoredOpenMetadataOperationSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    admin_password: str = Field(min_length=1)
    runtime_password: str = Field(min_length=1)
    administrator_password: str = Field(min_length=1)
    mysql_root_password: str = Field(min_length=1)
    database_password: str = Field(min_length=1)
    airflow_database_password: str = Field(min_length=1)

    @classmethod
    def from_operation_secrets(
        cls, operation_secrets: OpenMetadataOperationSecrets
    ) -> _StoredOpenMetadataOperationSecrets:
        return cls(
            admin_password=operation_secrets.admin_password.get_secret_value(),
            runtime_password=operation_secrets.runtime_password.get_secret_value(),
            administrator_password=(operation_secrets.administrator_password.get_secret_value()),
            mysql_root_password=operation_secrets.mysql_root_password.get_secret_value(),
            database_password=operation_secrets.database_password.get_secret_value(),
            airflow_database_password=(
                operation_secrets.airflow_database_password.get_secret_value()
            ),
        )

    def to_operation_secrets(self) -> OpenMetadataOperationSecrets:
        return OpenMetadataOperationSecrets(
            admin_password=SecretStr(self.admin_password),
            runtime_password=SecretStr(self.runtime_password),
            administrator_password=SecretStr(self.administrator_password),
            mysql_root_password=SecretStr(self.mysql_root_password),
            database_password=SecretStr(self.database_password),
            airflow_database_password=SecretStr(self.airflow_database_password),
        )


class EncryptedDirectoryOpenMetadataSecretStore:
    def __init__(
        self,
        *,
        directory: Path,
        key: SecretStr,
        bootstrap_admin_password: SecretStr,
    ) -> None:
        self._directory = directory.absolute()
        self._bootstrap_admin_password = bootstrap_admin_password
        try:
            self._fernet = Fernet(key.get_secret_value().encode("ascii"))
            _prepare_private_directory(self._directory)
        except Exception:
            raise _secret_storage_error() from None

    def create(self) -> tuple[str, OpenMetadataOperationSecrets]:
        try:
            for _ in range(16):
                secret_reference = "om-secret-" + secrets.token_urlsafe(32)
                destination = self._path_for(secret_reference)
                if destination.exists() or destination.is_symlink():
                    continue
                return secret_reference, self.ensure(secret_reference)
        except Exception:
            raise _secret_storage_error() from None
        raise _secret_storage_error()

    def ensure(self, secret_reference: str) -> OpenMetadataOperationSecrets:
        destination = self._path_for(secret_reference)
        try:
            return self.resolve(secret_reference)
        except RuntimeError:
            if destination.exists() or destination.is_symlink():
                raise _secret_storage_error() from None
        secrets_bundle = _new_operation_secrets()
        stored_secrets = _StoredOpenMetadataOperationSecrets.from_operation_secrets(secrets_bundle)
        encrypted_payload = self._fernet.encrypt(stored_secrets.model_dump_json().encode("utf-8"))
        try:
            _atomic_private_write(destination, encrypted_payload)
        except FileExistsError:
            return self.resolve(secret_reference)
        except Exception:
            raise _secret_storage_error() from None
        return secrets_bundle

    def resolve(self, secret_reference: str) -> OpenMetadataOperationSecrets:
        try:
            encrypted_payload = _read_private_file(self._path_for(secret_reference))
            decrypted_payload = self._fernet.decrypt(encrypted_payload)
            return _StoredOpenMetadataOperationSecrets.model_validate_json(
                decrypted_payload
            ).to_operation_secrets()
        except Exception:
            raise _secret_storage_error() from None

    def bootstrap_admin_password(self) -> SecretStr:
        return self._bootstrap_admin_password

    def delete(self, secret_reference: str) -> None:
        try:
            path = self._path_for(secret_reference)
            try:
                _read_private_file(path)
            except FileNotFoundError:
                return
            path.unlink()
            _sync_directory(self._directory)
        except Exception:
            raise _secret_storage_error() from None

    def _path_for(self, secret_reference: str) -> Path:
        _reject_symlink_components(self._directory)
        directory_status = self._directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_IMODE(directory_status.st_mode) != 0o700
        ):
            raise _secret_storage_error()
        if (
            not secret_reference.startswith("om-secret-")
            or not secret_reference.removeprefix("om-secret-")
            or not all(
                character.isalnum() or character in {"-", "_"} for character in secret_reference
            )
        ):
            raise _secret_storage_error()
        return self._directory / f"{secret_reference}.fernet"


class DockerComposeController:
    def __init__(self, *, compose_file: Path, readiness_script: Path, base_url: str) -> None:
        self._compose_file = compose_file
        self._process = DockerComposeProcess(
            compose_file=compose_file,
            run=_start_docker_process,
            timeout_seconds=_OPENMETADATA_COMPOSE_TIMEOUT_SECONDS,
            termination_grace_seconds=_OPENMETADATA_TERMINATION_GRACE_SECONDS,
        )
        self._readiness_script = readiness_script
        self._base_url = base_url

    def up(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._compose(project_name, "up", "--detach", environment=environment)
        self._run_readiness()

    def stop(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._compose(project_name, "stop", environment=environment)

    def start(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._compose(project_name, "start", environment=environment)
        self._run_readiness()

    def down(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        self._compose(
            project_name,
            "down",
            "--volumes",
            "--remove-orphans",
            environment=environment,
        )

    def backup(
        self, *, project_name: str, backup_path: Path, environment: Mapping[str, str]
    ) -> None:
        with (
            backup_path.open("wb") as destination,
            self._process.exec_stream(
                project_name=project_name,
                arguments=(
                    "exec",
                    "-T",
                    "mysql",
                    "sh",
                    "-c",
                    _mysql_shell_dump_command(),
                ),
                environment=self._database_environment(environment),
            ) as source,
        ):
            shutil.copyfileobj(source, destination, length=64 * 1024)
        if backup_path.stat().st_size == 0:
            raise RuntimeError("OpenMetadata backup failed")

    def restore(
        self, *, project_name: str, backup_path: Path, environment: Mapping[str, str]
    ) -> None:
        _run_restore_step(
            "target reset",
            lambda: self.down(project_name=project_name, environment=environment),
        )
        _run_restore_step(
            "database startup",
            lambda: self._compose(
                project_name,
                "up",
                "--detach",
                "mysql",
                "elasticsearch",
                environment=environment,
            ),
        )
        _run_restore_step(
            "database readiness",
            lambda: self._wait_for_mysql(project_name, environment=environment),
        )

        def import_backup() -> None:
            with (
                backup_path.open("rb") as source,
                self._process.exec_stream(
                    project_name=project_name,
                    arguments=(
                        "exec",
                        "-T",
                        "mysql",
                        "sh",
                        "-c",
                        _mysql_shell_restore_command(),
                    ),
                    environment=self._database_environment(environment),
                    stdin=source,
                ) as output,
            ):
                while output.read(64 * 1024):
                    pass

        _run_restore_step("database import", import_backup)
        _run_restore_step(
            "stack startup",
            lambda: self._compose(project_name, "up", "--detach", environment=environment),
        )
        _run_restore_step("terminal readiness", self._run_readiness)

    def rebuild_search_index(self, *, project_name: str, environment: Mapping[str, str]) -> None:
        # Ingestion is opt-in and independent of glossary indexing. Keeping the
        # reindex operation within the core services avoids starting an unconfigured,
        # memory-heavy Airflow process during restore.
        for _ in range(_SEARCH_REBUILD_ATTEMPTS):
            try:
                self._process.exec(
                    project_name=project_name,
                    arguments=(
                        "exec",
                        "-T",
                        "openmetadata-server",
                        "./bootstrap/openmetadata-ops.sh",
                        "reindex",
                        "--force",
                        "--entities=" + _RESTORED_SEARCH_ENTITIES,
                    ),
                    environment=self._compose_environment(environment),
                )
            except ComposeCommandError:
                continue
            else:
                return
        raise RuntimeError("OpenMetadata search index rebuild failed")

    def verify_pinned_images(self, *, project_name: str, environment: Mapping[str, str]) -> str:
        containers = tuple(
            resource
            for resource in self.discover_resources(
                project_name=project_name, environment=environment
            )
            if resource.resource_kind == "container"
        )
        observed_images: set[str] = set()
        for container in containers:
            image = self._process.inspect_container_image(
                identifier=container.identifier,
                environment=self._compose_environment(environment),
            )
            if image is None:
                raise RuntimeError("OpenMetadata container image observation failed")
            observed_images.add(image)
        if observed_images != set(CORE_UPSTREAM_IMAGES):
            raise RuntimeError("OpenMetadata container image set differs from the approved set")
        return digest(tuple(sorted(observed_images)))

    def planned_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]:
        try:
            return self._process.planned_resources(
                project_name=project_name,
                environment=self._compose_environment(environment),
            )
        except ComposeCommandError:
            raise RuntimeError("OpenMetadata Compose resource planning failed") from None

    def discover_resources(
        self, *, project_name: str, environment: Mapping[str, str]
    ) -> tuple[ComposeResource, ...]:
        try:
            return self._process.discover_resources(
                project_name=project_name,
                environment=self._compose_environment(environment),
            )
        except ComposeCommandError:
            raise RuntimeError("OpenMetadata Compose resource discovery failed") from None

    def remove_resource(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> None:
        try:
            self._process.remove_resource(
                resource_kind=resource_kind,
                identifier=identifier,
                environment=self._compose_environment(environment),
            )
        except ComposeCommandError:
            raise RuntimeError("OpenMetadata exact Compose resource cleanup failed") from None

    def resource_is_absent(
        self,
        *,
        resource_kind: ComposeResourceKind,
        identifier: str,
        environment: Mapping[str, str],
    ) -> bool | None:
        try:
            return self._process.resource_is_absent(
                resource_kind=resource_kind,
                identifier=identifier,
                environment=self._compose_environment(environment),
            )
        except ComposeCommandError:
            return None

    def _compose(self, project_name: str, *arguments: str, environment: Mapping[str, str]) -> None:
        try:
            self._process.exec(
                project_name=project_name,
                arguments=arguments,
                environment=self._compose_environment(environment),
            )
        except ComposeCommandError:
            raise RuntimeError("OpenMetadata Compose operation failed") from None

    @staticmethod
    def _compose_environment(environment: Mapping[str, str]) -> dict[str, str]:
        return {
            key: environment[key]
            for key in _COMPOSE_CREDENTIAL_ENVIRONMENT_KEYS
            if key in environment
        }

    @classmethod
    def _database_environment(cls, environment: Mapping[str, str]) -> dict[str, str]:
        password = environment.get("PILLARMESH_OPENMETADATA_MYSQL_ROOT_PASSWORD")
        if password is None:
            raise RuntimeError("OpenMetadata database password is unavailable")
        return cls._compose_environment(environment) | {"MYSQL_PWD": password}

    def _wait_for_mysql(self, project_name: str, *, environment: Mapping[str, str]) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                # The image's temporary initializer accepts its Unix socket but has TCP disabled.
                # mysqladmin ping therefore distinguishes the durable server before the import.
                self._process.exec(
                    project_name=project_name,
                    arguments=(
                        "exec",
                        "-T",
                        "mysql",
                        "sh",
                        "-c",
                        "exec mysqladmin --protocol=TCP --host=127.0.0.1 ping >/dev/null",
                    ),
                    environment=self._compose_environment(environment),
                )
            except ComposeCommandError:
                time.sleep(1)
            else:
                return
        raise RuntimeError("OpenMetadata database did not become ready for restore")

    def _run_readiness(self) -> None:
        result = subprocess.run(
            [sys.executable, str(self._readiness_script), "--url", self._base_url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("OpenMetadata did not reach terminal readiness")


class _Client(Protocol):
    def health(self) -> ProviderHealth: ...

    def build_identity(self) -> ProviderBuildIdentity: ...

    def assert_object_searchable(self, *, tenant_key: str, identity: str) -> None: ...

    def rotate_admin_password(self, new_password: SecretStr) -> None: ...

    def ensure_tenant_namespace(
        self, *, tenant_key: str, idempotency_key: str
    ) -> CatalogObjectRef: ...

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef: ...

    def ensure_glossary_term(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: GlossaryTermPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def ensure_classification(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: ClassificationPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def ensure_lineage(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: LineagePayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def get_object(self, *, tenant_key: str, identity: str) -> object: ...

    def delete_object(self, reference: CatalogObjectRef) -> None: ...

    def assign_owner(self, reference: CatalogObjectRef, owner: CatalogObjectRef) -> None: ...

    def discovered_resources(self) -> tuple[_DiscoveredProviderResource, ...]: ...

    def delete_recorded_resource(self, *, collection: str, identifier: str) -> None: ...

    def recorded_resource_is_absent(self, *, collection: str, identifier: str) -> bool | None: ...


class _DiscoveredProviderResource(Protocol):
    @property
    def resource_kind(self) -> str: ...

    @property
    def identifier(self) -> str: ...

    @property
    def collection(self) -> str: ...


class _RuntimeClient(Protocol):
    def assert_administration_denied(self) -> None: ...

    def assert_other_tenant_namespace_denied(self, *, tenant_key: str) -> None: ...


class _CatalogAvailabilityDecision(Protocol):
    def __call__(self, *, tenant_id: str, binding_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _PrivateBinding:
    tenant_id: str
    binding_id: str
    operation_id: str
    project_name: str
    resource_handle: str
    secret_reference: str


@dataclass(frozen=True, slots=True)
class _PlannedOpenMetadataResource:
    resource_kind: CatalogResourceKind
    planned_ref: str
    collection: str


class OpenMetadataProvisioner:
    def __init__(
        self,
        *,
        repository: CatalogRepository,
        compose: ComposeController,
        availability_decision: _CatalogAvailabilityDecision,
        client_factory: Callable[[_PrivateBinding], _Client] | None = None,
        runtime_client_factory: Callable[[_PrivateBinding], _RuntimeClient] | None = None,
        secret_store: OpenMetadataSecretStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._compose = compose
        self._availability_decision = availability_decision
        self._client_factory = client_factory
        self._runtime_client_factory = runtime_client_factory or self._runtime_client
        self._secret_store = secret_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._bindings_by_operation: dict[tuple[str, str, str], _PrivateBinding] = {}
        self._bindings_by_handle: dict[str, _PrivateBinding] = {}
        self._backup_paths: dict[str, Path] = {}

    def provision(self, *, tenant_id: str, binding_id: str, operation_id: str) -> str:
        try:
            with _exclusive_provisioning_lock(tenant_id=tenant_id, binding_id=binding_id):
                return self._provision(
                    tenant_id=tenant_id,
                    binding_id=binding_id,
                    operation_id=operation_id,
                )
        except CatalogProviderError:
            raise
        except Exception:
            raise CatalogProviderError(
                "OpenMetadata provisioning coordination failed",
                classification="transient",
            ) from None

    def _provision(self, *, tenant_id: str, binding_id: str, operation_id: str) -> str:
        loading_failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            catalog_binding = self._repository.load(tenant_id, binding_id)
        except KeyError:
            catalog_binding = None
            loading_failure = ("OpenMetadata provisioning request is invalid", "invalid_request")
        except Exception:
            catalog_binding = None
            loading_failure = ("OpenMetadata provisioning failed", "transient")
        if loading_failure is not None:
            raise CatalogProviderError(loading_failure[0], classification=loading_failure[1])
        if catalog_binding is None:
            raise RuntimeError("catalog binding load did not produce a result")
        if catalog_binding.lifecycle_state is not CatalogBindingState.PROVISIONING:
            raise CatalogProviderError(
                "OpenMetadata provisioning request is invalid", classification="invalid_request"
            )
        availability_failed = False
        try:
            catalog_selection = self._availability_decision(
                tenant_id=tenant_id,
                binding_id=binding_id,
            )
        except Exception:
            catalog_selection = None
            availability_failed = True
        if availability_failed:
            raise CatalogProviderError(
                "OpenMetadata catalog selection does not permit provisioning",
                classification="permanent",
            )
        if catalog_selection != "no_supported_catalog":
            raise CatalogProviderError(
                "OpenMetadata catalog selection does not permit provisioning",
                classification="permanent",
            )
        operation_recovery_failed = False
        try:
            operation = self._repository.load_operation(tenant_id, binding_id, operation_id)
        except KeyError:
            operation = None
        except Exception:
            operation = None
            operation_recovery_failed = True
        else:
            operation_recovery_failed = False
        if operation_recovery_failed:
            raise CatalogProviderError(
                "OpenMetadata private resource recovery failed", classification="transient"
            )
        if operation is not None:
            binding = _binding_from_operation(operation)
            self._remember_binding(binding)
            self._secrets_for(binding)
            if self._initial_provisioning_is_complete(binding):
                self._reconcile_initial_provisioning(binding)
                return binding.resource_handle
        else:
            claim_failure: tuple[str, CatalogFailureClassification] | None = None
            try:
                self._repository.claim_operation(
                    tenant_id,
                    binding_id,
                    operation_id,
                )
            except CatalogOperationConflictError:
                claim_failure = ("OpenMetadata binding already has another operation", "conflict")
            except KeyError:
                claim_failure = (
                    "OpenMetadata provisioning request is invalid",
                    "invalid_request",
                )
            except Exception:
                claim_failure = ("OpenMetadata private resource recovery failed", "transient")
            if claim_failure is not None:
                raise CatalogProviderError(claim_failure[0], classification=claim_failure[1])
            binding = _PrivateBinding(
                tenant_id=tenant_id,
                binding_id=binding_id,
                operation_id=operation_id,
                project_name=_project_name(operation_id),
                resource_handle=_resource_handle(operation_id),
                secret_reference=_operation_secret_reference(tenant_id, binding_id, operation_id),
            )
        operation_recorded = operation is not None
        provisioning_failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            self._ensure_secret_reference(binding.secret_reference)
            compose_environment = self._compose_environment(binding)
            planning_failed = False
            try:
                planned_compose_resources = self._compose.planned_resources(
                    project_name=binding.project_name,
                    environment=compose_environment,
                )
            except Exception:
                planned_compose_resources = ()
                planning_failed = True
            if planning_failed:
                raise CatalogProviderError(
                    "OpenMetadata resource discovery failed",
                    classification="transient",
                )
            self._record_operation_and_planned_resources(binding, planned_compose_resources)
            operation_recorded = True
            self._remember_binding(binding)
            self._compose.up(
                project_name=binding.project_name,
                environment=compose_environment,
            )
            self._bind_discovered_compose_resources(binding, planned_compose_resources)
            client, password_needs_rotation = self._provisioning_client(
                binding,
                prefer_rotated_password=operation is not None,
            )
            client.health()
            if password_needs_rotation:
                client.rotate_admin_password(self._secrets_for(binding).admin_password)
            namespace = self._provisioning_step(
                failure_message="OpenMetadata namespace provisioning failed",
                operation=lambda: self._ensure_openmetadata_resource(
                    binding,
                    client,
                    resource_kind=CatalogResourceKind.TENANT_NAMESPACE,
                    planned_ref=f"namespace:{tenant_id}",
                    collection="glossaries",
                    operation=lambda: client.ensure_tenant_namespace(
                        tenant_key=tenant_id,
                        idempotency_key=operation_id,
                    ),
                ),
            )
            runtime = self._provisioning_step(
                failure_message="OpenMetadata runtime identity provisioning failed",
                operation=lambda: self._ensure_openmetadata_resources(
                    binding,
                    client,
                    resources=(
                        _PlannedOpenMetadataResource(
                            resource_kind=CatalogResourceKind.CATALOG_POLICY,
                            planned_ref=f"policy:{tenant_id}:runtime-deny",
                            collection="policies",
                        ),
                        _PlannedOpenMetadataResource(
                            resource_kind=CatalogResourceKind.CATALOG_ROLE,
                            planned_ref=f"role:{tenant_id}:runtime",
                            collection="roles",
                        ),
                        _PlannedOpenMetadataResource(
                            resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
                            planned_ref=f"user:{tenant_id}:runtime",
                            collection="users",
                        ),
                    ),
                    operation=lambda: client.ensure_service_identity(
                        tenant_key=tenant_id,
                        identity="runtime",
                        idempotency_key=operation_id,
                    ),
                ),
            )
            self._provisioning_step(
                failure_message="OpenMetadata administrator identity provisioning failed",
                operation=lambda: self._ensure_openmetadata_resource(
                    binding,
                    client,
                    resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
                    planned_ref=f"user:{tenant_id}:administrator",
                    collection="users",
                    operation=lambda: client.ensure_service_identity(
                        tenant_key=tenant_id,
                        identity="administrator",
                        idempotency_key=operation_id,
                    ),
                ),
            )
            self._provisioning_step(
                failure_message="OpenMetadata owner assignment failed",
                operation=lambda: client.assign_owner(namespace, runtime),
            )
        except CatalogProviderError as error:
            if operation_recorded:
                with suppress(Exception):
                    self._retire_binding(binding)
            else:
                with suppress(Exception):
                    self._delete_secret_reference(binding.secret_reference)
            provisioning_failure = (str(error), error.classification)
        except CatalogOperationConflictError:
            provisioning_failure = (
                "OpenMetadata binding already has another operation",
                "conflict",
            )
        except Exception:
            if operation_recorded:
                with suppress(Exception):
                    self._retire_binding(binding)
            else:
                with suppress(Exception):
                    self._delete_secret_reference(binding.secret_reference)
            provisioning_failure = ("OpenMetadata provisioning failed", "transient")
        if provisioning_failure is not None:
            raise CatalogProviderError(
                provisioning_failure[0], classification=provisioning_failure[1]
            )
        return binding.resource_handle

    def validate(
        self, *, tenant_id: str, binding_id: str, private_resource_handle: str
    ) -> CatalogValidationEvidence:
        validation_failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            evidence = self._validate(
                tenant_id=tenant_id,
                binding_id=binding_id,
                private_resource_handle=private_resource_handle,
            )
        except CatalogProviderError as error:
            evidence = None
            validation_failure = (str(error), error.classification)
        except KeyError:
            evidence = None
            validation_failure = (
                "OpenMetadata validation request is invalid",
                "invalid_request",
            )
        except Exception:
            evidence = None
            validation_failure = ("OpenMetadata validation failed", "transient")
        if validation_failure is not None:
            raise CatalogProviderError(validation_failure[0], classification=validation_failure[1])
        if evidence is None:
            raise RuntimeError("catalog validation did not produce evidence")
        return evidence

    def retire_unrecorded_operation_claim(
        self, *, tenant_id: str, binding_id: str, operation_id: str
    ) -> None:
        """Retire the deterministic credential effect left by a pre-recording interruption."""
        try:
            self._repository.load_operation(tenant_id, binding_id, operation_id)
        except KeyError:
            pass
        except Exception:
            raise CatalogProviderError(
                "OpenMetadata private resource recovery failed", classification="transient"
            ) from None
        else:
            raise CatalogProviderError(
                "OpenMetadata operation is already recorded", classification="invalid_request"
            )
        try:
            claim_was_created = self._repository.claim_operation(
                tenant_id, binding_id, operation_id
            )
        except CatalogOperationConflictError:
            raise CatalogProviderError(
                "OpenMetadata binding already has another operation", classification="conflict"
            ) from None
        except Exception:
            raise CatalogProviderError(
                "OpenMetadata private resource recovery failed", classification="transient"
            ) from None
        if claim_was_created:
            raise CatalogProviderError(
                "OpenMetadata operation claim was not recorded", classification="invalid_request"
            )
        self._delete_secret_reference(
            _operation_secret_reference(tenant_id, binding_id, operation_id)
        )

    def _validate(
        self, *, tenant_id: str, binding_id: str, private_resource_handle: str
    ) -> CatalogValidationEvidence:
        binding = self._binding_for(private_resource_handle, None)
        if binding.tenant_id != tenant_id or binding.binding_id != binding_id:
            raise CatalogProviderError(
                "OpenMetadata private resource is unavailable", classification="invalid_request"
            )
        catalog_binding = self._repository.load(tenant_id, binding_id)
        if catalog_binding.lifecycle_state is not CatalogBindingState.VALIDATING:
            raise CatalogProviderError(
                "OpenMetadata validation request is invalid",
                classification="invalid_request",
            )
        client = self._client_for(binding)
        client.health()
        provider_build = client.build_identity()
        provider_image_set_digest = self._compose.verify_pinned_images(
            project_name=binding.project_name,
            environment=self._compose_environment(binding),
        )
        namespace = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.TENANT_NAMESPACE,
            planned_ref=f"namespace:{tenant_id}",
            collection="glossaries",
            operation=lambda: client.ensure_tenant_namespace(
                tenant_key=tenant_id,
                idempotency_key=binding.operation_id,
            ),
        )
        runtime = self._ensure_openmetadata_resources(
            binding,
            client,
            resources=(
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_POLICY,
                    planned_ref=f"policy:{tenant_id}:runtime-deny",
                    collection="policies",
                ),
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_ROLE,
                    planned_ref=f"role:{tenant_id}:runtime",
                    collection="roles",
                ),
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
                    planned_ref=f"user:{tenant_id}:runtime",
                    collection="users",
                ),
            ),
            operation=lambda: client.ensure_service_identity(
                tenant_key=tenant_id,
                identity="runtime",
                idempotency_key=binding.operation_id,
            ),
        )
        self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
            planned_ref=f"user:{tenant_id}:administrator",
            collection="users",
            operation=lambda: client.ensure_service_identity(
                tenant_key=tenant_id,
                identity="administrator",
                idempotency_key=binding.operation_id,
            ),
        )
        client.assign_owner(namespace, runtime)
        first_term = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.CATALOG_GLOSSARY_TERM,
            planned_ref=f"glossary-term:{tenant_id}:validation-term-from",
            collection="glossaryTerms",
            operation=lambda: client.ensure_glossary_term(
                tenant_key=tenant_id,
                identity="validation-term-from",
                payload=GlossaryTermPayload(
                    name="Validation source",
                    definition="Temporary tenant-scoped validation term.",
                    owner_ref="runtime",
                    provenance_ref="validation",
                ),
                idempotency_key=binding.operation_id,
            ),
        )
        second_term = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.CATALOG_GLOSSARY_TERM,
            planned_ref=f"glossary-term:{tenant_id}:validation-term-to",
            collection="glossaryTerms",
            operation=lambda: client.ensure_glossary_term(
                tenant_key=tenant_id,
                identity="validation-term-to",
                payload=GlossaryTermPayload(
                    name="Validation destination",
                    definition="Temporary tenant-scoped validation term.",
                    owner_ref="runtime",
                    provenance_ref="validation",
                ),
                idempotency_key=binding.operation_id,
            ),
        )
        classification = self._ensure_openmetadata_resources(
            binding,
            client,
            resources=(
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_CLASSIFICATION,
                    planned_ref=f"classification:{tenant_id}:validation-classification",
                    collection="classifications",
                ),
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_TAG,
                    planned_ref=f"tag:{tenant_id}:validation-classification",
                    collection="tags",
                ),
            ),
            operation=lambda: client.ensure_classification(
                tenant_key=tenant_id,
                identity="validation-classification",
                payload=ClassificationPayload(
                    subject_ref=first_term.stable_identity,
                    classification_ref="validation",
                    provenance_ref="validation",
                ),
                idempotency_key=binding.operation_id,
            ),
        )
        lineage = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.CATALOG_LINEAGE,
            planned_ref=f"lineage:{tenant_id}:validation-lineage",
            collection="lineage",
            operation=lambda: client.ensure_lineage(
                tenant_key=tenant_id,
                identity="validation-lineage",
                payload=LineagePayload(
                    from_ref=first_term.stable_identity,
                    to_ref=second_term.stable_identity,
                    producer_ref="validation",
                    evidence_ref="validation",
                ),
                idempotency_key=binding.operation_id,
            ),
        )
        snapshot = client.get_object(tenant_key=tenant_id, identity="validation-term-from")
        other_administrator = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
            planned_ref="user:other-tenant:administrator",
            collection="users",
            operation=lambda: client.ensure_service_identity(
                tenant_key="other-tenant",
                identity="administrator",
                idempotency_key=binding.operation_id,
            ),
        )
        other_namespace = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.TENANT_NAMESPACE,
            planned_ref="namespace:other-tenant",
            collection="glossaries",
            operation=lambda: client.ensure_tenant_namespace(
                tenant_key="other-tenant",
                idempotency_key=binding.operation_id,
            ),
        )
        client.assign_owner(other_namespace, other_administrator)
        runtime_client = self._runtime_client_factory(binding)
        runtime_client.assert_administration_denied()
        runtime_client.assert_other_tenant_namespace_denied(tenant_key="other-tenant")
        backup = self.backup_restore_and_probe(
            private_resource_handle=private_resource_handle,
            operation_id=binding.operation_id,
        )
        return CatalogValidationEvidence(
            evidence_id="catalog-validation-" + digest({"handle": private_resource_handle})[:24],
            tenant_id=tenant_id,
            binding_id=binding_id,
            binding_revision=catalog_binding.revision,
            provider_version=provider_build.provider_version,
            provider_build_digest=digest(provider_build),
            provider_image_set_digest=provider_image_set_digest,
            positive_probe_digest=digest(
                {
                    "namespace": namespace,
                    "first_term": first_term,
                    "second_term": second_term,
                    "classification": classification,
                    "lineage": lineage,
                    "snapshot": snapshot,
                }
            ),
            denial_probe_digest=digest(
                {
                    "runtime_identity": "denied",
                    "other_namespace": other_namespace.normalized_digest,
                }
            ),
            stable_identity_probe_digest=digest({"namespace": namespace.stable_identity}),
            backup_probe_digest=digest({"representative_objects_verified": backup}),
            observed_at=self._clock(),
        )

    def backup_restore_and_probe(self, *, private_resource_handle: str, operation_id: str) -> bool:
        binding = self._binding_for(private_resource_handle, operation_id)
        backup_path = self._backup_paths.get(private_resource_handle)
        if backup_path is None:
            existing_backup = next(
                (
                    Path(resource.provider_ref)
                    for resource in self._repository.load_resources(
                        binding.tenant_id, binding.binding_id
                    )
                    if resource.resource_kind is CatalogResourceKind.BACKUP_ARTIFACT
                    and resource.creation_state in {"created", "validated"}
                    and resource.cleanup_status == "not_started"
                ),
                None,
            )
            backup_path = existing_backup or (
                Path(tempfile.gettempdir())
                / f"pillarmesh-openmetadata-{secrets.token_urlsafe(32)}.tar"
            )
            self._backup_paths[private_resource_handle] = backup_path
        planned_ref = f"backup:{binding.operation_id}"
        backup_resource = self._plan_resource(
            binding,
            resource_kind=CatalogResourceKind.BACKUP_ARTIFACT,
            planned_ref=planned_ref,
        )
        environment = self._compose_environment(binding)
        failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            expected_snapshot = self._client_for(binding).get_object(
                tenant_key=binding.tenant_id,
                identity="validation-term-from",
            )
            self._compose.backup(
                project_name=binding.project_name,
                backup_path=backup_path,
                environment=environment,
            )
            if backup_resource.creation_state == "planned":
                if not backup_path.is_file():
                    raise CatalogProviderError(
                        "OpenMetadata backup and restore failed",
                        classification="transient",
                    )
                self._repository.mark_resource_created(
                    binding.tenant_id,
                    backup_resource.resource_id,
                    provider_ref=str(backup_path),
                )
            self._compose.restore(
                project_name=binding.project_name,
                backup_path=backup_path,
                environment=environment,
            )
            self._compose.rebuild_search_index(
                project_name=binding.project_name,
                environment=environment,
            )
            restored_client = self._client_for(binding)
            restored_client.health()
            restored_client.assert_object_searchable(
                tenant_key=binding.tenant_id,
                identity="validation-term-from",
            )
            restored_snapshot = restored_client.get_object(
                tenant_key=binding.tenant_id,
                identity="validation-term-from",
            )
            if restored_snapshot != expected_snapshot:
                raise CatalogProviderError(
                    "OpenMetadata restored metadata failed exact verification",
                    classification="permanent",
                )
        except CatalogProviderError as error:
            failure = (str(error), error.classification)
        except Exception:
            failure = ("OpenMetadata backup and restore failed", "transient")
        if failure is not None:
            raise CatalogProviderError(failure[0], classification=failure[1])
        return True

    def new_administrator_client(
        self,
        *,
        private_resource_handle: str,
        operation_id: str,
        password: SecretStr | None = None,
    ) -> OpenMetadataClient:
        """Create a fresh client without exposing the operation credentials to callers."""
        if self._client_factory is not None:
            raise CatalogProviderError(
                "OpenMetadata administrator client is unavailable",
                classification="permanent",
            )
        binding = self._binding_for(private_resource_handle, operation_id)
        secrets_bundle = self._secrets_for(binding)
        return OpenMetadataClient(
            settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
            credentials=_OpenMetadataCredentials(
                username="admin@open-metadata.org",
                password=password or secrets_bundle.admin_password,
                runtime_password=secrets_bundle.runtime_password,
                administrator_password=secrets_bundle.administrator_password,
            ),
        )

    def new_service_identity_client(
        self,
        *,
        private_resource_handle: str,
        operation_id: str,
        tenant_key: str,
        identity: Literal["runtime", "administrator"],
        password: SecretStr,
    ) -> OpenMetadataClient:
        if self._client_factory is not None:
            raise CatalogProviderError(
                "OpenMetadata service identity client is unavailable",
                classification="permanent",
            )
        binding = self._binding_for(private_resource_handle, operation_id)
        secrets_bundle = self._secrets_for(binding)
        return OpenMetadataClient(
            settings=OpenMetadataSettings(base_url="http://127.0.0.1:8585"),
            credentials=_OpenMetadataCredentials(
                username=_service_identity_username(tenant_key, identity),
                password=password,
                runtime_password=secrets_bundle.runtime_password,
                administrator_password=secrets_bundle.administrator_password,
            ),
        )

    def suspend(self, *, private_resource_handle: str, operation_id: str) -> None:
        binding = self._binding_for(private_resource_handle, operation_id)
        self._run_compose(
            lambda: self._compose.stop(
                project_name=binding.project_name,
                environment=self._compose_environment(binding),
            )
        )

    def resume(self, *, private_resource_handle: str, operation_id: str) -> None:
        binding = self._binding_for(private_resource_handle, operation_id)
        self._run_compose(
            lambda: self._compose.start(
                project_name=binding.project_name,
                environment=self._compose_environment(binding),
            )
        )
        self._client_for(binding).health()

    def retire(self, *, private_resource_handle: str, operation_id: str) -> None:
        binding = self._binding_for(private_resource_handle, operation_id)
        failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            self._retire_binding(binding)
        except CatalogProviderError as error:
            failure = (str(error), error.classification)
        except CatalogPersistenceError:
            failure = (
                "OpenMetadata cleanup state could not be recovered",
                "transient",
            )
        except Exception:
            failure = ("OpenMetadata exact resource cleanup failed", "transient")
        if failure is not None:
            raise CatalogProviderError(failure[0], classification=failure[1])

    def _binding_for(
        self, private_resource_handle: str, operation_id: str | None
    ) -> _PrivateBinding:
        binding = self._bindings_by_handle.get(private_resource_handle)
        if binding is None:
            recovery_failure: tuple[str, CatalogFailureClassification] | None = None
            try:
                binding = _binding_from_operation(
                    self._repository.load_operation_by_handle(private_resource_handle)
                )
            except KeyError:
                recovery_failure = (
                    "OpenMetadata private resource is unavailable",
                    "invalid_request",
                )
            except Exception:
                recovery_failure = (
                    "OpenMetadata private resource recovery failed",
                    "transient",
                )
            if recovery_failure is not None:
                raise CatalogProviderError(recovery_failure[0], classification=recovery_failure[1])
            if binding is None:
                raise CatalogProviderError(
                    "OpenMetadata private resource recovery failed",
                    classification="transient",
                )
            self._remember_binding(binding)
        if operation_id is not None and binding.operation_id != operation_id:
            raise CatalogProviderError(
                "OpenMetadata private resource is unavailable", classification="invalid_request"
            )
        return binding

    def _retire_binding(self, binding: _PrivateBinding) -> None:
        resources = self._repository.load_resources(binding.tenant_id, binding.binding_id)
        failures: list[CatalogProviderError] = []
        compose_resources = tuple(
            resource
            for resource in resources
            if _compose_kind_for_catalog(resource.resource_kind) is not None
        )
        for resource in sorted(
            (
                resource
                for resource in resources
                if _compose_kind_for_catalog(resource.resource_kind) is None
            ),
            key=_cleanup_order,
        ):
            failure = self._cleanup_resource(binding, resource)
            if failure is not None:
                failures.append(failure)
        if not failures:
            compose_failure = self._cleanup_compose_project(binding, compose_resources)
            if compose_failure is not None:
                failures.append(compose_failure)
        if failures:
            raise failures[0]
        completed_resources = self._repository.load_resources(binding.tenant_id, binding.binding_id)
        if any(resource.cleanup_status != "complete" for resource in completed_resources):
            raise CatalogProviderError(
                "OpenMetadata resource cleanup could not be verified",
                classification="transient",
            )
        self._delete_secret_reference(binding.secret_reference)

    def _cleanup_compose_project(
        self,
        binding: _PrivateBinding,
        resources: tuple[PrivateCatalogResource, ...],
    ) -> CatalogProviderError | None:
        pending_resources = tuple(
            resource for resource in resources if resource.cleanup_status != "complete"
        )
        if not pending_resources:
            return None
        if any(
            resource.cleanup_status not in {"not_started", "in_progress"}
            for resource in pending_resources
        ):
            return CatalogProviderError(
                "OpenMetadata resource cleanup is already terminal",
                classification="permanent",
            )
        try:
            self._compose.down(
                project_name=binding.project_name,
                environment=self._compose_environment(binding),
            )
        except Exception:
            return CatalogProviderError(
                "OpenMetadata exact resource cleanup failed", classification="transient"
            )
        try:
            remaining_resources = self._compose.discover_resources(
                project_name=binding.project_name,
                environment=self._compose_environment(binding),
            )
        except Exception:
            return CatalogProviderError(
                "OpenMetadata resource cleanup could not be verified", classification="transient"
            )
        if remaining_resources:
            return CatalogProviderError(
                "OpenMetadata exact resource cleanup failed", classification="transient"
            )
        for resource in pending_resources:
            try:
                if resource.cleanup_status == "not_started":
                    self._repository.begin_cleanup(binding.tenant_id, resource.resource_id)
                self._repository.complete_cleanup(
                    binding.tenant_id,
                    resource.resource_id,
                    cleaned_at=self._clock(),
                )
            except Exception:
                return CatalogProviderError(
                    "OpenMetadata cleanup state could not be recorded", classification="transient"
                )
        return None

    def _record_operation_and_planned_resources(
        self,
        binding: _PrivateBinding,
        planned_compose_resources: tuple[ComposeResource, ...],
    ) -> None:
        now = self._clock()
        operation = PrivateCatalogOperation(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            operation_id=binding.operation_id,
            resource_handle=binding.resource_handle,
            project_name=binding.project_name,
            secret_reference=binding.secret_reference,
            created_at=now,
        )
        resources = tuple(
            PrivateCatalogResource(
                tenant_id=binding.tenant_id,
                binding_id=binding.binding_id,
                resource_id=_resource_id(
                    binding,
                    _catalog_kind_for_compose(resource.resource_kind),
                    resource.identifier,
                ),
                resource_kind=_catalog_kind_for_compose(resource.resource_kind),
                provider_ref=resource.identifier,
                creation_state="planned",
                retention_deadline=now + timedelta(hours=1),
                cleanup_status="not_started",
                created_at=now,
                cleaned_at=None,
            )
            for resource in planned_compose_resources
        )
        try:
            existing_operation = self._repository.load_operation(
                binding.tenant_id,
                binding.binding_id,
                binding.operation_id,
            )
        except KeyError:
            self._repository.record_operation(operation, resources=resources)
            return
        if (
            existing_operation.tenant_id != operation.tenant_id
            or existing_operation.binding_id != operation.binding_id
            or existing_operation.operation_id != operation.operation_id
            or existing_operation.resource_handle != operation.resource_handle
            or existing_operation.project_name != operation.project_name
            or existing_operation.secret_reference != operation.secret_reference
        ):
            raise CatalogProviderError(
                "OpenMetadata private resource recovery failed",
                classification="transient",
            )
        for resource in resources:
            resource_missing = False
            try:
                existing_resource = self._repository.load_resource(
                    binding.tenant_id,
                    resource.resource_id,
                )
            except KeyError:
                existing_resource = None
                resource_missing = True
            if resource_missing or existing_resource is None:
                raise CatalogProviderError(
                    "OpenMetadata private resource recovery failed",
                    classification="transient",
                )
            if (
                existing_resource.binding_id != resource.binding_id
                or existing_resource.resource_kind is not resource.resource_kind
            ):
                raise CatalogProviderError(
                    "OpenMetadata private resource recovery failed",
                    classification="transient",
                )

    def _initial_provisioning_is_complete(self, binding: _PrivateBinding) -> bool:
        resources = self._repository.load_resources(binding.tenant_id, binding.binding_id)
        resources_by_id = {resource.resource_id: resource for resource in resources}
        required_provider_resources = (
            (CatalogResourceKind.TENANT_NAMESPACE, f"namespace:{binding.tenant_id}"),
            (CatalogResourceKind.CATALOG_POLICY, f"policy:{binding.tenant_id}:runtime-deny"),
            (CatalogResourceKind.CATALOG_ROLE, f"role:{binding.tenant_id}:runtime"),
            (CatalogResourceKind.SERVICE_ACCOUNT, f"user:{binding.tenant_id}:runtime"),
            (
                CatalogResourceKind.SERVICE_ACCOUNT,
                f"user:{binding.tenant_id}:administrator",
            ),
        )
        if any(
            (resource := resources_by_id.get(_resource_id(binding, kind, planned_ref))) is None
            or resource.creation_state not in {"created", "validated"}
            or resource.provider_ref.startswith("planned:")
            for kind, planned_ref in required_provider_resources
        ):
            return False
        compose_resources = tuple(
            resource
            for resource in resources
            if _compose_kind_for_catalog(resource.resource_kind) is not None
        )
        return bool(compose_resources) and all(
            resource.creation_state in {"created", "validated"}
            and not resource.provider_ref.startswith("planned:")
            for resource in compose_resources
        )

    def _reconcile_initial_provisioning(self, binding: _PrivateBinding) -> None:
        client = self._client_for(binding)
        client.health()
        namespace = self._ensure_openmetadata_resource(
            binding,
            client,
            resource_kind=CatalogResourceKind.TENANT_NAMESPACE,
            planned_ref=f"namespace:{binding.tenant_id}",
            collection="glossaries",
            operation=lambda: client.ensure_tenant_namespace(
                tenant_key=binding.tenant_id,
                idempotency_key=binding.operation_id,
            ),
        )
        runtime = self._ensure_openmetadata_resources(
            binding,
            client,
            resources=(
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_POLICY,
                    planned_ref=f"policy:{binding.tenant_id}:runtime-deny",
                    collection="policies",
                ),
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.CATALOG_ROLE,
                    planned_ref=f"role:{binding.tenant_id}:runtime",
                    collection="roles",
                ),
                _PlannedOpenMetadataResource(
                    resource_kind=CatalogResourceKind.SERVICE_ACCOUNT,
                    planned_ref=f"user:{binding.tenant_id}:runtime",
                    collection="users",
                ),
            ),
            operation=lambda: client.ensure_service_identity(
                tenant_key=binding.tenant_id,
                identity="runtime",
                idempotency_key=binding.operation_id,
            ),
        )
        client.assign_owner(namespace, runtime)

    def _bind_discovered_compose_resources(
        self,
        binding: _PrivateBinding,
        planned_resources: tuple[ComposeResource, ...],
    ) -> None:
        discovery_failed = False
        try:
            discovered_resources = self._compose.discover_resources(
                project_name=binding.project_name,
                environment=self._compose_environment(binding),
            )
        except Exception:
            discovered_resources = ()
            discovery_failed = True
        if discovery_failed:
            raise CatalogProviderError(
                "OpenMetadata resource discovery failed",
                classification="transient",
            )
        for resource_kind in ("container", "volume", "network"):
            planned_for_kind = sorted(
                (
                    resource
                    for resource in planned_resources
                    if resource.resource_kind == resource_kind
                ),
                key=lambda resource: resource.identifier,
            )
            discovered_for_kind = sorted(
                (
                    resource
                    for resource in discovered_resources
                    if resource.resource_kind == resource_kind and resource.identifier
                ),
                key=lambda resource: resource.identifier,
            )
            if len(planned_for_kind) != len(discovered_for_kind):
                raise CatalogProviderError(
                    "OpenMetadata resource discovery failed",
                    classification="transient",
                )
            catalog_kind = _catalog_kind_for_compose(resource_kind)
            for planned, discovered in zip(
                planned_for_kind,
                discovered_for_kind,
                strict=True,
            ):
                resource_id = _resource_id(binding, catalog_kind, planned.identifier)
                recorded = self._repository.load_resource(binding.tenant_id, resource_id)
                if recorded.creation_state == "planned":
                    self._repository.mark_resource_created(
                        binding.tenant_id,
                        resource_id,
                        provider_ref=discovered.identifier,
                    )
                elif (
                    recorded.creation_state not in {"created", "validated"}
                    or recorded.provider_ref != discovered.identifier
                ):
                    raise CatalogProviderError(
                        "OpenMetadata resource discovery failed",
                        classification="transient",
                    )

    def _ensure_openmetadata_resource(
        self,
        binding: _PrivateBinding,
        client: _Client,
        *,
        resource_kind: CatalogResourceKind,
        planned_ref: str,
        collection: str,
        operation: Callable[[], CatalogObjectRef],
    ) -> CatalogObjectRef:
        return self._ensure_openmetadata_resources(
            binding,
            client,
            resources=(
                _PlannedOpenMetadataResource(
                    resource_kind=resource_kind,
                    planned_ref=planned_ref,
                    collection=collection,
                ),
            ),
            operation=operation,
        )

    def _ensure_openmetadata_resources(
        self,
        binding: _PrivateBinding,
        client: _Client,
        *,
        resources: tuple[_PlannedOpenMetadataResource, ...],
        operation: Callable[[], CatalogObjectRef],
    ) -> CatalogObjectRef:
        planned_resources = tuple(
            (
                resource,
                self._plan_resource(
                    binding,
                    resource_kind=resource.resource_kind,
                    planned_ref=resource.planned_ref,
                ),
            )
            for resource in resources
        )
        before = {
            (discovered.collection, discovered.identifier)
            for discovered in client.discovered_resources()
        }
        try:
            result = operation()
        except BaseException:
            with suppress(Exception):
                self._bind_new_openmetadata_resources(
                    binding=binding,
                    planned_resources=planned_resources,
                    before=before,
                    discovered_after=client.discovered_resources(),
                )
            raise
        self._bind_new_openmetadata_resources(
            binding=binding,
            planned_resources=planned_resources,
            before=before,
            discovered_after=client.discovered_resources(),
        )
        return result

    def _bind_new_openmetadata_resources(
        self,
        *,
        binding: _PrivateBinding,
        planned_resources: tuple[tuple[_PlannedOpenMetadataResource, PrivateCatalogResource], ...],
        before: set[tuple[str, str]],
        discovered_after: tuple[_DiscoveredProviderResource, ...],
    ) -> None:
        for resource_spec, planned_resource in planned_resources:
            if planned_resource.creation_state != "planned":
                if not any(
                    candidate.collection == resource_spec.collection
                    and candidate.identifier == planned_resource.provider_ref
                    for candidate in discovered_after
                ):
                    raise CatalogProviderError(
                        "OpenMetadata resource discovery failed",
                        classification="transient",
                    )
                continue
            discovered = tuple(
                candidate
                for candidate in discovered_after
                if candidate.collection == resource_spec.collection
                and (candidate.collection, candidate.identifier) not in before
                and candidate.identifier
            )
            if len(discovered) != 1:
                raise CatalogProviderError(
                    "OpenMetadata resource discovery failed",
                    classification="transient",
                )
            self._repository.mark_resource_created(
                binding.tenant_id,
                planned_resource.resource_id,
                provider_ref=discovered[0].identifier,
            )

    @staticmethod
    def _provisioning_step[Result](
        *, failure_message: str, operation: Callable[[], Result]
    ) -> Result:
        failure_classification: CatalogFailureClassification | None = None
        try:
            result = operation()
        except CatalogProviderError as error:
            failure_classification = error.classification
        except Exception:
            failure_classification = "transient"
        else:
            return result
        if failure_classification is not None:
            raise CatalogProviderError(failure_message, classification=failure_classification)
        raise RuntimeError("provisioning step did not produce a result")

    def _plan_resource(
        self,
        binding: _PrivateBinding,
        *,
        resource_kind: CatalogResourceKind,
        planned_ref: str,
    ) -> PrivateCatalogResource:
        resource_id = _resource_id(binding, resource_kind, planned_ref)
        try:
            return self._repository.load_resource(binding.tenant_id, resource_id)
        except KeyError:
            pass
        now = self._clock()
        resource = PrivateCatalogResource(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            resource_id=resource_id,
            resource_kind=resource_kind,
            provider_ref=f"planned:{planned_ref}",
            creation_state="planned",
            retention_deadline=now + timedelta(hours=1),
            cleanup_status="not_started",
            created_at=now,
            cleaned_at=None,
        )
        self._repository.record_resource(resource)
        return resource

    def _cleanup_planned_resource(
        self,
        binding: _PrivateBinding,
        resource_id: str,
        *,
        client: _Client,
    ) -> None:
        resource = self._repository.load_resource(binding.tenant_id, resource_id)
        failure = self._cleanup_resource(binding, resource, client=client)
        if failure is not None:
            raise failure

    def _cleanup_resource(
        self,
        binding: _PrivateBinding,
        resource: PrivateCatalogResource,
        *,
        client: _Client | None = None,
    ) -> CatalogProviderError | None:
        if resource.cleanup_status == "complete":
            return None
        if resource.cleanup_status not in {"not_started", "in_progress"}:
            return CatalogProviderError(
                "OpenMetadata resource cleanup is already terminal",
                classification="permanent",
            )
        if resource.cleanup_status == "not_started":
            try:
                self._repository.begin_cleanup(binding.tenant_id, resource.resource_id)
            except Exception:
                return CatalogProviderError(
                    "OpenMetadata cleanup state could not be recorded",
                    classification="transient",
                )
        if resource.creation_state not in {"created", "validated"}:
            failure = CatalogProviderError(
                "OpenMetadata resource cleanup could not be verified",
                classification="transient",
            )
            with suppress(Exception):
                self._repository.fail_cleanup(
                    binding.tenant_id,
                    resource.resource_id,
                    status="unknown",
                    failure_classification="unknown",
                )
            return failure
        selected_client = client
        try:
            if _openmetadata_collection(resource.resource_kind) is not None:
                selected_client = selected_client or self._client_for(binding)
            self._remove_exact_resource(binding, resource, client=selected_client)
        except CatalogProviderError as error:
            if error.classification not in {"transient", "throttled"}:
                with suppress(Exception):
                    self._repository.fail_cleanup(
                        binding.tenant_id,
                        resource.resource_id,
                        status="failed",
                        failure_classification=error.classification,
                    )
            return error
        try:
            absent = self._exact_resource_is_absent(binding, resource, client=selected_client)
        except CatalogProviderError:
            absent = None
        if absent is None:
            failure = CatalogProviderError(
                "OpenMetadata resource cleanup could not be verified",
                classification="transient",
            )
            with suppress(Exception):
                self._repository.fail_cleanup(
                    binding.tenant_id,
                    resource.resource_id,
                    status="unknown",
                    failure_classification="unknown",
                )
            return failure
        if not absent:
            failure = CatalogProviderError(
                "OpenMetadata exact resource cleanup failed",
                classification="transient",
            )
            with suppress(Exception):
                self._repository.fail_cleanup(
                    binding.tenant_id,
                    resource.resource_id,
                    status="failed",
                    failure_classification=failure.classification,
                )
            return failure
        try:
            self._repository.complete_cleanup(
                binding.tenant_id,
                resource.resource_id,
                cleaned_at=self._clock(),
            )
        except Exception:
            return CatalogProviderError(
                "OpenMetadata cleanup state could not be recorded",
                classification="transient",
            )
        return None

    def _remove_exact_resource(
        self,
        binding: _PrivateBinding,
        resource: PrivateCatalogResource,
        *,
        client: _Client | None,
    ) -> None:
        compose_kind = _compose_kind_for_catalog(resource.resource_kind)
        collection = _openmetadata_collection(resource.resource_kind)
        failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            if compose_kind is not None:
                self._compose.remove_resource(
                    resource_kind=compose_kind,
                    identifier=resource.provider_ref,
                    environment=self._compose_environment(binding),
                )
                return
            if collection is not None and client is not None:
                client.delete_recorded_resource(
                    collection=collection,
                    identifier=resource.provider_ref,
                )
                return
            if resource.resource_kind is CatalogResourceKind.BACKUP_ARTIFACT:
                Path(resource.provider_ref).unlink(missing_ok=True)
                return
        except CatalogProviderError as error:
            failure = (str(error), error.classification)
        except Exception:
            failure = ("OpenMetadata exact resource cleanup failed", "transient")
        if failure is not None:
            raise CatalogProviderError(failure[0], classification=failure[1])
        raise CatalogProviderError(
            "OpenMetadata recorded resource type is unsupported",
            classification="permanent",
        )

    def _exact_resource_is_absent(
        self,
        binding: _PrivateBinding,
        resource: PrivateCatalogResource,
        *,
        client: _Client | None,
    ) -> bool | None:
        compose_kind = _compose_kind_for_catalog(resource.resource_kind)
        collection = _openmetadata_collection(resource.resource_kind)
        failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            if compose_kind is not None:
                return self._compose.resource_is_absent(
                    resource_kind=compose_kind,
                    identifier=resource.provider_ref,
                    environment=self._compose_environment(binding),
                )
            if collection is not None and client is not None:
                return client.recorded_resource_is_absent(
                    collection=collection,
                    identifier=resource.provider_ref,
                )
            if resource.resource_kind is CatalogResourceKind.BACKUP_ARTIFACT:
                return not Path(resource.provider_ref).exists()
        except CatalogProviderError as error:
            failure = (str(error), error.classification)
        except Exception:
            failure = ("OpenMetadata resource cleanup could not be verified", "transient")
        if failure is not None:
            raise CatalogProviderError(failure[0], classification=failure[1])
        raise CatalogProviderError(
            "OpenMetadata recorded resource type is unsupported",
            classification="permanent",
        )

    def _remember_binding(self, binding: _PrivateBinding) -> None:
        self._bindings_by_operation[
            (binding.tenant_id, binding.binding_id, binding.operation_id)
        ] = binding
        self._bindings_by_handle[binding.resource_handle] = binding

    def _default_client(self, binding: _PrivateBinding) -> OpenMetadataClient:
        secrets_bundle = self._secrets_for(binding)
        return OpenMetadataClient(
            settings=OpenMetadataSettings(
                base_url="http://127.0.0.1:8585",
            ),
            credentials=_OpenMetadataCredentials(
                username="admin@open-metadata.org",
                password=secrets_bundle.admin_password,
                runtime_password=secrets_bundle.runtime_password,
                administrator_password=secrets_bundle.administrator_password,
            ),
        )

    def _provisioning_client(
        self,
        binding: _PrivateBinding,
        *,
        prefer_rotated_password: bool,
    ) -> tuple[_Client, bool]:
        if self._client_factory is not None:
            return self._client_factory(binding), True
        secrets_bundle = self._secrets_for(binding)
        if prefer_rotated_password:
            rotated_client = self._default_client(binding)
            try:
                rotated_client.health()
            except CatalogProviderError as error:
                if error.classification != "authentication":
                    raise CatalogProviderError(
                        str(error),
                        classification=error.classification,
                    ) from None
            else:
                return rotated_client, False
        bootstrap_client = OpenMetadataClient(
            settings=OpenMetadataSettings(
                base_url="http://127.0.0.1:8585",
            ),
            credentials=_OpenMetadataCredentials(
                username="admin@open-metadata.org",
                password=self._bootstrap_admin_password(),
                runtime_password=secrets_bundle.runtime_password,
                administrator_password=secrets_bundle.administrator_password,
            ),
        )
        return bootstrap_client, True

    def _client_for(self, binding: _PrivateBinding) -> _Client:
        if self._client_factory is not None:
            return self._client_factory(binding)
        return self._default_client(binding)

    def _runtime_client(self, binding: _PrivateBinding) -> OpenMetadataClient:
        secrets_bundle = self._secrets_for(binding)
        return OpenMetadataClient(
            settings=OpenMetadataSettings(
                base_url="http://127.0.0.1:8585",
            ),
            credentials=_OpenMetadataCredentials(
                username=_service_identity_name(binding.tenant_id, "runtime")
                + "@open-metadata.invalid",
                password=secrets_bundle.runtime_password,
                runtime_password=secrets_bundle.runtime_password,
                administrator_password=secrets_bundle.administrator_password,
            ),
        )

    def _ensure_secret_reference(self, secret_reference: str) -> None:
        if self._secret_store is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        secret_failure = False
        try:
            self._secret_store.ensure(secret_reference)
        except Exception:
            secret_failure = True
        if secret_failure:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )

    def _secrets_for(self, binding: _PrivateBinding) -> OpenMetadataOperationSecrets:
        if self._secret_store is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        secret_failure = False
        try:
            secrets_bundle = self._secret_store.resolve(binding.secret_reference)
        except Exception:
            secrets_bundle = None
            secret_failure = True
        if secret_failure or secrets_bundle is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        return secrets_bundle

    def _bootstrap_admin_password(self) -> SecretStr:
        if self._secret_store is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        secret_failure = False
        try:
            password = self._secret_store.bootstrap_admin_password()
        except Exception:
            password = None
            secret_failure = True
        if secret_failure or password is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        return password

    def _delete_secret_reference(self, secret_reference: str) -> None:
        if self._secret_store is None:
            raise CatalogProviderError(
                "OpenMetadata private credentials are unavailable", classification="transient"
            )
        deletion_failed = False
        try:
            self._secret_store.delete(secret_reference)
        except Exception:
            deletion_failed = True
        if deletion_failed:
            raise CatalogProviderError(
                "OpenMetadata private credentials could not be retired",
                classification="transient",
            )

    def _compose_environment(self, binding: _PrivateBinding) -> dict[str, str]:
        return self._secrets_for(binding).compose_environment()

    @staticmethod
    def _run_compose(operation: Callable[[], None]) -> None:
        failure: tuple[str, CatalogFailureClassification] | None = None
        try:
            operation()
        except CatalogProviderError as error:
            failure = (str(error), error.classification)
        except Exception:
            failure = ("OpenMetadata Compose operation failed", "transient")
        if failure is not None:
            raise CatalogProviderError(failure[0], classification=failure[1])


@contextmanager
def _exclusive_provisioning_lock(*, tenant_id: str, binding_id: str) -> Iterator[None]:
    lock_key = (tenant_id, binding_id)
    with _LOCAL_PROVISIONING_LOCKS_GUARD:
        local_lock = _LOCAL_PROVISIONING_LOCKS.setdefault(lock_key, Lock())
    with local_lock, _cross_process_provisioning_lock(tenant_id=tenant_id, binding_id=binding_id):
        yield


@contextmanager
def _cross_process_provisioning_lock(*, tenant_id: str, binding_id: str) -> Iterator[None]:
    lock_directory = Path(tempfile.gettempdir()).resolve() / "pillarmesh-openmetadata-locks"
    _prepare_private_directory(lock_directory)
    lock_name = digest(
        {
            "domain": "pillarmesh-openmetadata-provisioning-lock-v1",
            "tenant_id": tenant_id,
            "binding_id": binding_id,
        }
    )
    descriptor = os.open(
        lock_directory / lock_name,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with suppress(Exception):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with suppress(Exception):
            os.close(descriptor)


def _prepare_private_directory(directory: Path) -> None:
    _reject_symlink_components(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_components(directory)
    if not directory.is_dir():
        raise RuntimeError("private secret path is not a directory")
    os.chmod(directory, 0o700, follow_symlinks=False)
    if stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) != 0o700:
        raise RuntimeError("private secret directory permissions are invalid")


def _reject_symlink_components(path: Path) -> None:
    components = (*tuple(reversed(path.parents)), path)
    for component in components:
        if component.is_symlink():
            raise RuntimeError("private secret path contains a symlink")


def _atomic_private_write(destination: Path, payload: bytes) -> None:
    temporary = destination.with_name(f".{secrets.token_urlsafe(24)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600, follow_symlinks=False)
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        if stat.S_IMODE(destination.stat(follow_symlinks=False).st_mode) != 0o600:
            raise RuntimeError("private secret file permissions are invalid")
        _sync_directory(destination.parent)
    except BaseException:
        with suppress(Exception):
            temporary.unlink(missing_ok=True)
        raise


def _read_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        file_status = os.fstat(stream.fileno())
        if not stat.S_ISREG(file_status.st_mode):
            raise RuntimeError("private secret path is not a regular file")
        if stat.S_IMODE(file_status.st_mode) != 0o600:
            raise RuntimeError("private secret file permissions are invalid")
        return stream.read()


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secret_storage_error() -> RuntimeError:
    return RuntimeError("OpenMetadata operation secret storage is unavailable")


def _project_name(operation_id: str) -> str:
    return (
        "pillarmesh-openmetadata-"
        + digest({"domain": "pillarmesh-openmetadata-compose-v1", "operation_id": operation_id})[
            :16
        ]
    )


def _resource_handle(operation_id: str) -> str:
    return (
        "om-handle-"
        + digest({"domain": "pillarmesh-openmetadata-handle-v1", "operation_id": operation_id})[:24]
    )


def _operation_secret_reference(tenant_id: str, binding_id: str, operation_id: str) -> str:
    return (
        "om-secret-"
        + digest(
            {
                "domain": "pillarmesh-openmetadata-operation-secret-v1",
                "tenant_id": tenant_id,
                "binding_id": binding_id,
                "operation_id": operation_id,
            }
        )[:32]
    )


def _new_operation_secrets() -> OpenMetadataOperationSecrets:
    return OpenMetadataOperationSecrets(
        admin_password=_openmetadata_account_password(),
        runtime_password=_openmetadata_account_password(),
        administrator_password=_openmetadata_account_password(),
        mysql_root_password=_random_secret(),
        database_password=_random_secret(),
        airflow_database_password=_random_secret(),
    )


def _random_secret() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


def _openmetadata_account_password() -> SecretStr:
    random_tail_length = _OPENMETADATA_ACCOUNT_PASSWORD_MAXIMUM_LENGTH - len(
        _OPENMETADATA_ACCOUNT_PASSWORD_PREFIX
    )
    return SecretStr(
        _OPENMETADATA_ACCOUNT_PASSWORD_PREFIX + secrets.token_urlsafe(32)[:random_tail_length]
    )


def _start_docker_process(
    command: list[str],
    *,
    env: Mapping[str, str],
    stdin: int | IO[bytes] | None,
    stdout: int,
    stderr: int,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, env=env, stdin=stdin, stdout=stdout, stderr=stderr)


def _binding_from_operation(operation: PrivateCatalogOperation) -> _PrivateBinding:
    return _PrivateBinding(
        tenant_id=operation.tenant_id,
        binding_id=operation.binding_id,
        operation_id=operation.operation_id,
        project_name=operation.project_name,
        resource_handle=operation.resource_handle,
        secret_reference=operation.secret_reference,
    )


def _resource_id(binding: _PrivateBinding, kind: CatalogResourceKind, provider_ref: str) -> str:
    return (
        "om-resource-"
        + digest(
            {
                "domain": "pillarmesh-openmetadata-resource-v1",
                "operation_id": binding.operation_id,
                "resource_kind": kind.value,
                "provider_ref": provider_ref,
            }
        )[:24]
    )


def _catalog_kind_for_compose(resource_kind: str) -> CatalogResourceKind:
    catalog_kind = {
        "container": CatalogResourceKind.COMPOSE_CONTAINER,
        "volume": CatalogResourceKind.COMPOSE_VOLUME,
        "network": CatalogResourceKind.COMPOSE_NETWORK,
    }.get(resource_kind)
    if catalog_kind is None:
        raise CatalogProviderError(
            "OpenMetadata resource discovery failed",
            classification="transient",
        )
    return catalog_kind


def _compose_kind_for_catalog(
    resource_kind: CatalogResourceKind,
) -> ComposeResourceKind | None:
    compose_kinds: dict[CatalogResourceKind, ComposeResourceKind] = {
        CatalogResourceKind.COMPOSE_CONTAINER: "container",
        CatalogResourceKind.COMPOSE_VOLUME: "volume",
        CatalogResourceKind.COMPOSE_NETWORK: "network",
    }
    return compose_kinds.get(resource_kind)


def _openmetadata_collection(resource_kind: CatalogResourceKind) -> str | None:
    return {
        CatalogResourceKind.TENANT_NAMESPACE: "glossaries",
        CatalogResourceKind.SERVICE_ACCOUNT: "users",
        CatalogResourceKind.CATALOG_GLOSSARY_TERM: "glossaryTerms",
        CatalogResourceKind.CATALOG_CLASSIFICATION: "classifications",
        CatalogResourceKind.CATALOG_POLICY: "policies",
        CatalogResourceKind.CATALOG_ROLE: "roles",
        CatalogResourceKind.CATALOG_TAG: "tags",
        CatalogResourceKind.CATALOG_LINEAGE: "lineage",
    }.get(resource_kind)


def _cleanup_order(resource: PrivateCatalogResource) -> tuple[int, str]:
    priorities = {
        CatalogResourceKind.CATALOG_LINEAGE: 0,
        CatalogResourceKind.CATALOG_GLOSSARY_TERM: 1,
        CatalogResourceKind.CATALOG_TAG: 2,
        CatalogResourceKind.CATALOG_CLASSIFICATION: 3,
        CatalogResourceKind.TENANT_NAMESPACE: 4,
        CatalogResourceKind.SERVICE_ACCOUNT: 5,
        CatalogResourceKind.CATALOG_ROLE: 6,
        CatalogResourceKind.CATALOG_POLICY: 7,
        CatalogResourceKind.BACKUP_ARTIFACT: 8,
        CatalogResourceKind.COMPOSE_CONTAINER: 9,
        CatalogResourceKind.COMPOSE_VOLUME: 10,
        CatalogResourceKind.COMPOSE_NETWORK: 11,
    }
    priority = priorities.get(resource.resource_kind, 12)
    return priority, resource.resource_id


def _service_identity_name(tenant_id: str, identity: str) -> str:
    return (
        "pm-"
        + digest(
            {
                "domain": "pillarmesh-openmetadata-v1",
                "tenant_key": tenant_id,
                "identity": identity,
            }
        )[:24]
    )


def _mysql_shell_dump_command() -> str:
    return (
        f"rm -rf -- {_MYSQL_DUMP_DIRECTORY} && "
        'mysqlsh --js --user=root --password="$MYSQL_ROOT_PASSWORD" '
        f"--socket={_MYSQL_SOCKET} "
        '--execute=\'util.dumpSchemas(["openmetadata_db"], '
        f'"{_MYSQL_DUMP_DIRECTORY}", {{threads: 1}})\' '
        ">/dev/null && "
        f"exec tar --create --directory=/tmp {Path(_MYSQL_DUMP_DIRECTORY).name}"
    )


def _mysql_shell_restore_command() -> str:
    mysql = 'mysql --user=root --password="$MYSQL_ROOT_PASSWORD"'
    return (
        f"rm -rf -- {_MYSQL_DUMP_DIRECTORY} && "
        "tar --extract --directory=/tmp && "
        f'{mysql} --execute "DROP DATABASE IF EXISTS openmetadata_db; '
        'SET GLOBAL local_infile=ON" >/dev/null 2>&1 && '
        'mysqlsh --js --user=root --password="$MYSQL_ROOT_PASSWORD" '
        f"--socket={_MYSQL_SOCKET} "
        f"--execute='util.loadDump(\"{_MYSQL_DUMP_DIRECTORY}\", {{threads: 1}})' "
        ">/dev/null 2>&1; "
        "task3_restore_status=$?; "
        f'{mysql} --execute "SET GLOBAL local_infile=OFF" >/dev/null 2>&1 || true; '
        f"rm -rf -- {_MYSQL_DUMP_DIRECTORY}; "
        'exit "$task3_restore_status"'
    )
