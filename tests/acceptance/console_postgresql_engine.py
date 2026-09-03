"""Wire the real PostgreSQL warehouse provider behind the governed-local console.

`run_console_governed` serves the console against the owning services with a
local-acceptance provider that carries a binding to `ready` with no engine in
existence. This module replaces that provider with `PostgreSQLWarehouseProvider`,
so confirming the warehouse binding in the browser starts a real PostgreSQL
container over TLS, provisions seven separated principal classes, runs their
positive and denial probes, takes a backup, restores it into an isolated second
instance, verifies it, and tears that instance down -- and only then records the
evidence that admits the binding to `ready`.

It still validates under `WarehouseValidationProfile.LOCAL_ACCEPTANCE`, which is
the provider's own choice: encryption at rest is deferred on this profile, so the
run proves the lifecycle and not a production posture. Read
`docs/plan3a/acceptance-run.md` for what that profile does and does not assert.

The operation secrets follow whichever operation the orchestrator actually minted,
which is why the provider is built per operation rather than at startup.
"""

from __future__ import annotations

import base64
import os
import secrets as secrets_module
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

import psycopg
from cryptography.fernet import Fernet
from pillarmesh_provider_postgresql import (
    PostgreSQLBackupCommandBoundary,
    PostgreSQLWarehouseProvider,
    PostgreSQLWarehouseSettings,
)
from pillarmesh_provider_sdk import DockerComposeProcess
from pillarmesh_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseBinding,
    WarehouseFailureClassification,
    WarehouseOperationSecretCapability,
    WarehouseOperationSecretPurpose,
    WarehouseOperationSecrets,
    WarehouseProvider,
    WarehouseProvisionResult,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseRetirementEvidence,
    WarehouseValidationResult,
)
from pillarmesh_warehouse_control import secrets as warehouse_secrets
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from pydantic import SecretStr

from tests.emulators.warehouses.postgresql.init_tls import generate_tls_material

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = (
    _REPOSITORY_ROOT / "tests" / "emulators" / "warehouses" / "postgresql" / "compose.yaml"
)

_COMPOSE_TIMEOUT_SECONDS = 300
_COMPOSE_TERMINATION_GRACE_SECONDS = 5
_PRINCIPAL_PURPOSES = (
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "backup_restore",
    "customer_sql",
    "catalog",
    "bi",
)


class RepositoryResourceRecorder:
    """Record every managed resource in the binding's ledger before it is created.

    Cleanup can only remove what the ledger names, so a resource that is created
    before it is recorded is one nothing can retire. The provider drives this
    protocol; this is the adapter onto warehouse-control's own repository.
    """

    def __init__(
        self,
        repository: SQLiteWarehouseRepository,
        *,
        binding_id: str,
        clock: Callable[[], datetime],
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
            expected_resources, reopened_at=reopened_at
        )

    def mark_created(self, tenant_id: str, resource_id: str, provider_resource_handle: str) -> None:
        resource = self._resource(tenant_id, resource_id)
        if resource.provider_resource_handle != provider_resource_handle:
            raise RuntimeError("provider resource handle changed after planning")
        self._save(resource, creation_state=WarehouseResourceCreationState.CREATED)

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None:
        resource = self._resource(tenant_id, resource_id)
        self._save(resource, creation_state=WarehouseResourceCreationState.AMBIGUOUS)

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

    def _save(
        self,
        resource: PrivateWarehouseResource,
        *,
        creation_state: WarehouseResourceCreationState,
    ) -> None:
        self._repository.save_resource(
            resource.model_copy(
                update={"creation_state": creation_state, "updated_at": self._clock()}
            )
        )

    def _resource(self, tenant_id: str, resource_id: str) -> PrivateWarehouseResource:
        for resource in self._repository.load_resources(tenant_id, self._binding_id):
            if resource.resource_id == resource_id:
                return resource
        raise RuntimeError(f"resource {resource_id!r} is not recorded in the binding ledger")


class DeferredPostgreSQLProvider:
    """Build the real provider per operation, on first use rather than at startup.

    Two identifiers the harness cannot know when the server starts decide how the
    provider is built. The ledger recorder is scoped to a binding, which does not
    exist until someone confirms one in the browser. And the credential capabilities
    are bound to one operation identifier, which the orchestrator mints only when it
    claims the operation. Every method of the provider protocol receives both, which
    is the first moment either is known.

    The operation identifier is not predictable. `next_operation_sequence` allocates
    rather than reads, so a retry after a failed provision -- or a second run against
    the same state directory -- mints a later sequence. Binding the credentials to a
    predicted first sequence made the provider operate under an identifier whose
    secrets it did not hold, and nothing failed, because a secret capability returns
    its stored value without checking the operation. A provider is therefore built,
    and its secrets stored, for whichever operation actually arrives.

    One run still provisions one binding, so a second binding is refused rather than
    quietly given another binding's ledger recorder.
    """

    engine_kind = EngineKind.POSTGRESQL

    def __init__(self, factory: Callable[..., WarehouseProvider]) -> None:
        self._factory = factory
        self._lock = RLock()
        self._binding_id: str | None = None
        self._providers: dict[str, WarehouseProvider] = {}

    def provider_for(self, binding_id: str, operation_id: str) -> WarehouseProvider:
        # Command routes run the backend in a threadpool and `InFlightCommandKeys`
        # serializes only one command identity, so two confirmations carrying
        # different idempotency keys arrive here concurrently.
        with self._lock:
            if self._binding_id is None:
                self._binding_id = binding_id
            elif binding_id != self._binding_id:
                raise RuntimeError(
                    "this harness provisions one warehouse binding per run; "
                    f"{binding_id!r} arrived after {self._binding_id!r}"
                )
            provider = self._providers.get(operation_id)
            if provider is None:
                provider = self._factory(binding_id=binding_id, operation_id=operation_id)
                self._providers[operation_id] = provider
            return provider

    def _for(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvider:
        return self.provider_for(binding.binding_id, operation.operation_id)

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._for(binding, operation).provision(binding, operation)

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._for(binding, operation).reconcile(binding, operation)

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult:
        return self._for(binding, operation).validate(binding, operation, resume=resume)

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._for(binding, operation).suspend(binding, operation)

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        self._for(binding, operation).resume(binding, operation)

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        return self._for(binding, operation).retire(binding, operation)


def private_secret_directories(directory: Path) -> tuple[Path, Path]:
    """The two private directories the provider writes, with symlinks resolved.

    The secret store opens every component of the path with `O_NOFOLLOW`, and the
    provider settings reject a symlinked directory outright. On macOS both `/tmp`
    and `/var` are symlinks and `tempfile.gettempdir()` resolves through `/var`, so
    an unresolved path raises `WarehouseSecretStorageError` -- which the
    orchestrator sanitizes to `invalid_provider_response`, a permanent governed
    failure whose message says nothing about a symlink.
    """
    resolved = directory.resolve()
    private_directory = resolved / "private-operation"
    secret_directory = resolved / "operation-secrets"
    for target in (private_directory, secret_directory):
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.chmod(0o700)
    return private_directory, secret_directory


def run_operation_secrets(*, clock: Callable[[], datetime]) -> WarehouseOperationSecrets:
    """Generate one run's credentials and TLS material.

    Generated once per run and reused for every operation against the same engine.
    A retry mints a new operation, and minting new passwords with it would leave the
    provider holding credentials the already-created container never had.
    """
    material = generate_tls_material(now=clock())
    passwords = tuple(secrets_module.token_urlsafe(24) for _ in _PRINCIPAL_PURPOSES)
    return WarehouseOperationSecrets(
        administration_password=SecretStr(passwords[0]),
        ingestion_runtime_password=SecretStr(passwords[1]),
        transformation_runtime_password=SecretStr(passwords[2]),
        backup_restore_password=SecretStr(passwords[3]),
        customer_sql_probe_password=SecretStr(passwords[4]),
        catalog_password=SecretStr(passwords[5]),
        bi_password=SecretStr(passwords[6]),
        tls_private_key_pem=SecretStr(material.private_key_bundle),
        tls_certificate_pem=SecretStr(material.certificate_bundle),
        backup_encryption_key_b64=SecretStr(
            base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        ),
    )


def build_postgresql_provider(
    *,
    repository: SQLiteWarehouseRepository,
    binding_id: str,
    operation_id: str,
    operation_secrets: WarehouseOperationSecrets,
    directory: Path,
    clock: Callable[[], datetime],
) -> PostgreSQLWarehouseProvider:
    """Compose the real provider for one operation of one binding.

    The credentials never leave `directory`, which is created private. Nothing here
    is a fixture: the passwords become the real roles inside the container and the
    TLS material becomes its real certificate. They are stored under the operation
    the orchestrator actually minted, so the ledger and the secret store name the
    same operation.
    """
    private_directory, secret_directory = private_secret_directories(directory)

    authority = warehouse_secrets._EncryptedDirectoryWarehouseSecretAuthority(
        directory=secret_directory,
        key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    reference = authority.store(operation_id, operation_secrets)

    settings = PostgreSQLWarehouseSettings(
        private_operation_directory=private_directory,
        retention_period=timedelta(hours=1),
        backup_chunk_bytes=32 * 1024,
        capacity_bytes=64 * 1024 * 1024,
    )
    compose = DockerComposeProcess(
        compose_file=COMPOSE_FILE,
        timeout_seconds=_COMPOSE_TIMEOUT_SECONDS,
        termination_grace_seconds=_COMPOSE_TERMINATION_GRACE_SECONDS,
    )
    recorder = RepositoryResourceRecorder(repository, binding_id=binding_id, clock=clock)

    def capability(
        purpose: WarehouseOperationSecretPurpose,
    ) -> WarehouseOperationSecretCapability:
        return authority.operation_capability(reference, operation_id=operation_id, purpose=purpose)

    return PostgreSQLWarehouseProvider(
        settings=settings,
        compose=compose,
        resource_recorder=recorder,
        administration_secret=capability("administration"),
        ingestion_runtime_secret=capability("ingestion_runtime"),
        transformation_runtime_secret=capability("transformation_runtime"),
        customer_sql_secret=capability("customer_sql"),
        catalog_secret=capability("catalog"),
        bi_secret=capability("bi"),
        tls_private_key_secret=capability("tls_private_key"),
        tls_certificate_secret=capability("tls_certificate"),
        backup_commands=PostgreSQLBackupCommandBoundary(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            secrets=authority.backup_command_capability(reference, operation_id=operation_id),
            retirement=authority.backup_retirement_capability(reference, operation_id=operation_id),
            connect=psycopg.connect,
            clock=clock,
        ),
        connect=psycopg.connect,
        clock=clock,
    )


__all__ = [
    "COMPOSE_FILE",
    "DeferredPostgreSQLProvider",
    "RepositoryResourceRecorder",
    "build_postgresql_provider",
    "private_secret_directories",
    "run_operation_secrets",
]
