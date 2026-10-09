"""Provision the demonstration's warehouse through warehouse-control, rather than beside it.

The demonstration's default path is given a warehouse: `HEINZEL_DEMO_WAREHOUSE_DSN` names a
database somebody else started, `demo/warehouse.py` provisions schemas and roles inside it, and
warehouse-control never sees it -- so the console reports the managed-warehouse capability as
`not_delivered`, which is the honest answer about a database no governing service owns.

This module is the other path, selected by `HEINZEL_DEMO_WAREHOUSE_CONTROL`. It composes the
owning service with the real provider: `WarehouseControlService` holds the binding,
`WarehouseLifecycleOrchestrator` drives it from `draft` to `ready`, and
`PostgreSQLWarehouseProvider` creates the warehouse by driving the Compose project shipped at
`deploy/quickstart/warehouse-control/compose.yaml` through `DockerComposeProcess`. The binding
the console then reports is one warehouse-control made, validated and recorded evidence for.

**It is opt-in because it cannot be the default.** Driving Compose means this process issues
`docker` commands, so the console needs a reachable Docker daemon -- and a console in a
container needs that daemon's socket bind-mounted into it, which is host-level access. The
existing path needs neither. See `deploy/quickstart/README.md` and `docs/demonstration-gaps.md`.

This is also the warehouse the demonstration answers from: `_administration_dsn` is how, and
`DemoConsole` composes its governed answer over it. ADR-0003 makes a Heinzel-operated data plane
mandatory for every tenant, so the path that produces a binding warehouse-control owns is the one
whose warehouse should be answering.

Credentials are minted per start and held in this process, never written to the state
directory, which is the posture `demo/warehouse.py` already takes with the warehouse's role
passwords. That has two consequences worth stating plainly rather than discovering, both because
provisioning rotates the administering login to the `administration` secret this start minted.

A start that fails part-way through provisioning leaves a warehouse whose administration password
went with the process that created it, and the next start refuses to adopt it instead of
connecting with credentials it never had. `_resumable_binding` is where that refusal lives.

A start that finds a `ready` binding does adopt it, because a warehouse that exists is still worth
reporting -- but it holds no credential that warehouse accepts either, so `administration_dsn` is
`None` and the console reports every answer capability as not delivered. That is the same answer it
gives with no warehouse at all, and it is why the demonstration's answer on this path is shown by
discarding the state directory and the Compose project together rather than by restarting.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Final

import psycopg
import psycopg.conninfo
from heinzel_provider_postgresql import (
    POSTGRESQL_WAREHOUSE_ADMINISTRATION_ROLE,
    POSTGRESQL_WAREHOUSE_DATABASE_NAME,
    PostgreSQLBackupCommandBoundary,
    PostgreSQLWarehouseProvider,
    PostgreSQLWarehouseSettings,
)
from heinzel_provider_sdk import CommandRunner, DockerComposeProcess
from heinzel_warehouse_control import (
    EngineKind,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseResource,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseFailureClassification,
    WarehouseLifecycleOrchestrator,
    WarehouseOperationSecretCapability,
    WarehouseOperationSecretPurpose,
    WarehouseOperationSecrets,
    WarehouseProviderError,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseSecretAuthority,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository
from pydantic import SecretStr

from ..governed_adapters import InMemoryWorkspaceBindingDirectory, WarehouseControlBindingReader
from .publication import DEMO_TENANT_ID
from .warehouse_custody import (
    new_warehouse_operation_id,
    open_demo_warehouse_secret_authority,
)
from .warehouse_tls import generate_demo_warehouse_tls_material

__all__ = [
    "DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE",
    "DEMO_WAREHOUSE_REGION",
    "DemoManagedWarehouse",
    "ManagedWarehouseOption",
    "ManagedWarehouseRefused",
    "provision_demo_managed_warehouse",
]

# The Compose project the provider drives, resolved from this file rather than from a working
# directory: the console is started from wherever the operator happens to be. Six parents up is
# the repository root, which holds for a checkout and for the quickstart image alike, because
# the image copies `apps/` to the same depth under `/src`.
DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE: Final = (
    Path(__file__).resolve().parents[6]
    / "deploy"
    / "quickstart"
    / "warehouse-control"
    / "compose.yaml"
)

# The region the binding records. `WarehouseBinding.deployment_mode` admits only
# `heinzel_cloud`, so the region is the only field that can say where this warehouse actually
# is, and a container on the operator's own machine is `local`.
DEMO_WAREHOUSE_REGION: Final = "local"

# Long enough for `docker compose up` to pull an image it has not seen on a slow connection,
# which is the first thing this path does on a fresh machine.
_COMPOSE_TIMEOUT_SECONDS: Final = 300.0
_COMPOSE_TERMINATION_GRACE_SECONDS: Final = 5.0

# The warehouse the demonstration creates is discarded with the demonstration, so it is
# retained for an hour rather than for a deployment's retention window.
_RETENTION_PERIOD: Final = timedelta(hours=1)
_BACKUP_CHUNK_BYTES: Final = 32 * 1024
_CAPACITY_BYTES: Final = 64 * 1024 * 1024

# The entropy behind one principal's password and behind the backup encryption key. Each becomes
# a real credential inside the warehouse this path creates, so these are not fixture sizes.
_PASSWORD_BYTES: Final = 24
_BACKUP_ENCRYPTION_KEY_BYTES: Final = 32

# The binding this workspace uses, recorded before anything is created so that a start which
# dies mid-provision comes back to the same binding -- and so the resources that binding owns
# are always attributable to it. A second draft would create a second warehouse.
_BINDING_RECORD_NAME: Final = "binding.json"
_BINDING_RECORD_KEY: Final = "warehouse_binding_id"
# The operation its secrets are stored under, and the reference the store returned for them.
# Recorded with the binding because the three are only useful together: a binding nothing can
# administer is the state a restart used to come back to.
_OPERATION_RECORD_KEY: Final = "warehouse_operation_id"
_SECRET_RECORD_KEY: Final = "warehouse_secret_reference"

# What this path can do with a binding it finds already recorded. `READY` is the resume the
# demonstration is built around: `WarehouseLifecycleOrchestrator.provision` closes it as a
# completed replay without calling the provider at all, so no credential is needed. `DRAFT` is
# a binding nothing was created for yet. Every other state is a provisioning that stopped
# part-way, and this process cannot finish it, because the credentials belonged to the one that
# started it.
_RESUMABLE_STATES: Final = frozenset({WarehouseBindingState.DRAFT, WarehouseBindingState.READY})


class ManagedWarehouseRefused(RuntimeError):
    """The demonstration could not provision a warehouse through warehouse-control.

    A `RuntimeError`, so the `heinzel-console` command reports the message and exits
    `_UNAVAILABLE` rather than printing a traceback at an operator who has a setting to
    change.
    """


@dataclass(frozen=True, slots=True)
class ManagedWarehouseOption:
    """What the demonstration drives Compose and PostgreSQL with.

    The three seams exist because the live path cannot be exercised without a Docker daemon:
    `run` is the one `DockerComposeProcess` already offers for standing in for the process it
    starts, `connect` is the driver, and `executable_lookup` is how `docker` is found. A test
    supplies all three; a demonstration supplies none and gets the real ones.
    """

    compose_file: Path = DEMO_WAREHOUSE_CONTROL_COMPOSE_FILE
    run: CommandRunner | None = None
    # `Callable[..., object]` is the provider's own connect signature: it is given the driver and
    # treats what comes back as opaque. Narrowing it here would claim a contract the provider
    # does not hold its caller to.
    connect: Callable[..., object] = psycopg.connect
    executable_lookup: Callable[[str], str | None] = shutil.which


class DemoManagedWarehouse:
    """A warehouse-control binding the console can report, and the stores behind it."""

    def __init__(
        self,
        *,
        binding: WarehouseBinding,
        bindings: WarehouseControlBindingReader,
        repository: SQLiteWarehouseRepository,
        administration_dsn: str | None,
        internal_hostname: str,
        private_directory: Path | None,
    ) -> None:
        self.binding = binding
        self.bindings = bindings
        # The name this warehouse answers to on its own container network, and the directory
        # holding the authority and client certificate a connection to it must present. A BI tool
        # in another container reaches it by these two and not by the published loopback port,
        # which from inside any container means that container itself.
        #
        # The directory is the provider's and is named rather than copied: a second copy would be
        # a second thing to keep in step with a certificate minted per start. `None` when this
        # start did not provision the warehouse and so holds neither.
        self.internal_hostname = internal_hostname
        self.private_directory = private_directory
        #
        # How this process administers the warehouse, when this process is the one that created
        # it. `None` otherwise, which is not a degraded form of the same thing: provisioning
        # rotates the administering login's password to the `administration` operation secret,
        # this demonstration mints that secret per start and stores it nowhere, so a start that
        # adopted a warehouse an earlier start created holds no credential it will accept. A
        # fabricated one would turn a refused connection into a wrong password.
        #
        # Not in a repr because it carries that password: `DemoManagedWarehouse` defines none,
        # and this is the reason not to add one.
        self.administration_dsn = administration_dsn
        self._repository = repository

    def close(self) -> None:
        """Release the binding store. The warehouse itself is left running.

        Stopping it would be a lifecycle decision warehouse-control owns and this console was
        never asked to make: the binding records a warehouse that exists, and a console exiting
        is not that warehouse being retired. `docker compose down --volumes` on the project
        named in the refusal messages is how an operator removes it.
        """
        self._repository.close()

    def __enter__(self) -> DemoManagedWarehouse:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _RepositoryResourceRecorder:
    """Record every managed resource in the binding's ledger before it is created.

    Cleanup can only remove what the ledger names, so a resource created before it is recorded
    is one nothing can retire. `SQLiteWarehouseRepository` persists resources but does not offer
    this protocol's planning and marking vocabulary, so this is the adapter between them.
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
            # The handle is what cleanup addresses the resource by. A handle that changed
            # between planning and creation means the ledger names something else.
            raise ManagedWarehouseRefused(
                "the warehouse provider changed a resource handle after planning it, so the "
                "binding's ledger no longer names what was created"
            )
        self._save(resource, creation_state=WarehouseResourceCreationState.CREATED)

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None:
        self._save(
            self._resource(tenant_id, resource_id),
            creation_state=WarehouseResourceCreationState.AMBIGUOUS,
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
        raise ManagedWarehouseRefused(
            "the warehouse provider named a resource the binding's ledger does not hold"
        )


def provision_demo_managed_warehouse(
    state_dir: Path,
    *,
    option: ManagedWarehouseOption | None = None,
    clock: Callable[[], datetime],
) -> DemoManagedWarehouse:
    """Bring a warehouse-control binding to `ready`, creating the warehouse if there is none.

    Every failure leaves `ManagedWarehouseRefused` naming what is missing. The orchestrator
    classifies a provider failure and records it against the operation, and a classification is
    not something to show an operator who is looking for the setting to change -- so each one is
    translated here, once, into the sentence that says what to do.
    """
    selected = ManagedWarehouseOption() if option is None else option
    compose_file = _require_compose_file(selected.compose_file)
    _require_docker(selected)
    directory = _private_directory(state_dir / "warehouse-control")
    repository = SQLiteWarehouseRepository(
        connection=sqlite3.connect(str(directory / "bindings.sqlite3"), check_same_thread=False)
    )
    try:
        return _provision(
            directory,
            compose_file=compose_file,
            option=selected,
            repository=repository,
            clock=clock,
        )
    except BaseException:
        # Best-effort clean-up: a failure to close must not replace the failure to provision.
        with suppress(Exception):
            repository.close()
        raise


def _provision(
    directory: Path,
    *,
    compose_file: Path,
    option: ManagedWarehouseOption,
    repository: SQLiteWarehouseRepository,
    clock: Callable[[], datetime],
) -> DemoManagedWarehouse:
    control = WarehouseControlService(
        repository,
        clock=clock,
        # The provider validates under `LOCAL_ACCEPTANCE`, which defers encryption at rest.
        # `ProductionWarehouseReadinessPolicy` refuses that evidence, so naming it here would
        # produce a binding stuck in `validating` with evidence nothing admits.
        readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
    )
    record = directory / _BINDING_RECORD_NAME
    authority = open_demo_warehouse_secret_authority(directory)
    binding = _resumable_binding(control, record=record)
    if binding is None:
        binding = control.create_draft(
            tenant_id=DEMO_TENANT_ID,
            engine_kind=EngineKind.POSTGRESQL,
            region=DEMO_WAREHOUSE_REGION,
            capacity_profile="mvp-fixed",
        )
        _write_record(
            record,
            _WarehouseRecord(
                binding_id=binding.binding_id, operation_id=None, secret_reference=None
            ),
        )
    bindings = InMemoryWorkspaceBindingDirectory()
    bindings.bind_warehouse(tenant_id=DEMO_TENANT_ID, binding_id=binding.binding_id)
    reader = WarehouseControlBindingReader(service=control, directory=bindings)
    if binding.lifecycle_state is WarehouseBindingState.READY:
        # Adopted from an earlier start. Its secrets are in the store that start put them in,
        # so the warehouse is administrable from here -- which is the whole point of keeping
        # them. A binding recorded before this demonstration kept any says so by holding no
        # operation, and is reportable and unreachable exactly as it was.
        return _resumed(
            binding,
            directory,
            authority=authority,
            bindings=reader,
            compose_file=compose_file,
            option=option,
            record=record,
            repository=repository,
            clock=clock,
        )
    internal_hostname = PostgreSQLWarehouseProvider.internal_hostname(binding)
    operation_secrets = _operation_secrets(clock=clock, internal_hostnames=(internal_hostname,))
    # Stored before the warehouse is created, so that a start which dies during provisioning
    # leaves its credentials recoverable rather than leaving a container nothing can reach.
    operation_id = new_warehouse_operation_id()
    secret_reference = authority.store(operation_id, operation_secrets)
    _write_record(
        record,
        _WarehouseRecord(
            binding_id=binding.binding_id,
            operation_id=operation_id,
            secret_reference=secret_reference,
        ),
    )
    provider = _build_provider(
        directory,
        compose_file=compose_file,
        option=option,
        repository=repository,
        binding_id=binding.binding_id,
        authority=authority,
        operation_id=operation_id,
        secret_reference=secret_reference,
        clock=clock,
    )
    orchestrator = WarehouseLifecycleOrchestrator(
        control=control,
        repository=repository,
        provider=provider,
        clock=clock,
    )
    ready = _provisioned(orchestrator, binding, compose_file=compose_file)
    return DemoManagedWarehouse(
        binding=ready,
        bindings=reader,
        repository=repository,
        administration_dsn=_administration_dsn(
            provider,
            ready,
            administration=authority.operation_capability(
                secret_reference, operation_id=operation_id, purpose="administration"
            ),
        ),
        internal_hostname=internal_hostname,
        private_directory=provider.connection_target(ready).root_certificate.parent,
    )


def _resumed(
    binding: WarehouseBinding,
    directory: Path,
    *,
    authority: WarehouseSecretAuthority,
    bindings: WarehouseControlBindingReader,
    compose_file: Path,
    option: ManagedWarehouseOption,
    record: Path,
    repository: SQLiteWarehouseRepository,
    clock: Callable[[], datetime],
) -> DemoManagedWarehouse:
    """A warehouse an earlier start provisioned, administered through the secrets it kept.

    The coordinates come from the provider, which owns the private file layout and the port it
    allocated, and reads both back off this same directory. The credentials come from the
    store. Neither is re-derived here, so a resumed warehouse is reached exactly as a fresh one
    is and nothing about this path can drift from that one.

    A binding recorded before the demonstration kept any secrets names no operation. There is
    then nothing to administer it with and nothing to be done about that from here, so it is
    reported and left unreachable, which is what it was before.
    """
    written = _recorded(record)
    internal_hostname = PostgreSQLWarehouseProvider.internal_hostname(binding)
    if written is None or written.operation_id is None or written.secret_reference is None:
        return DemoManagedWarehouse(
            binding=binding,
            bindings=bindings,
            repository=repository,
            administration_dsn=None,
            internal_hostname=internal_hostname,
            private_directory=None,
        )
    provider = _build_provider(
        directory,
        compose_file=compose_file,
        option=option,
        repository=repository,
        binding_id=binding.binding_id,
        authority=authority,
        operation_id=written.operation_id,
        secret_reference=written.secret_reference,
        clock=clock,
    )
    try:
        target = provider.connection_target(binding)
    except KeyError:
        # `ready` without the port the provisioning allocated: the binding store survived and
        # the provider's own directory did not. Refusing here would stop a console that can
        # still report the binding, which is what it could do before any of this.
        return DemoManagedWarehouse(
            binding=binding,
            bindings=bindings,
            repository=repository,
            administration_dsn=None,
            internal_hostname=internal_hostname,
            private_directory=None,
        )
    return DemoManagedWarehouse(
        binding=binding,
        bindings=bindings,
        repository=repository,
        administration_dsn=_administration_dsn(
            provider,
            binding,
            administration=authority.operation_capability(
                written.secret_reference,
                operation_id=written.operation_id,
                purpose="administration",
            ),
        ),
        internal_hostname=internal_hostname,
        private_directory=target.root_certificate.parent,
    )


def _administration_dsn(
    provider: PostgreSQLWarehouseProvider,
    binding: WarehouseBinding,
    *,
    administration: WarehouseOperationSecretCapability,
) -> str:
    """Administer the warehouse this provider provisioned, as the login it rotated.

    The coordinates come from the provider, which allocated the loopback port and owns the
    private file layout the TLS material sits in. The password does not: provisioning rotates
    the administering login to the `administration` operation secret, so the secret this start
    minted is already the one the warehouse will accept, and nothing has to read it back out of
    a file the provider keeps private.

    TLS is not optional here. The warehouse's own `pg_hba.conf` serves `hostssl` with
    `clientcert=verify-ca` and rejects plaintext, so every parameter below is a precondition of
    connecting at all rather than a hardening choice this demonstration is making.
    """
    target = provider.connection_target(binding)
    return psycopg.conninfo.make_conninfo(
        host=target.host,
        port=str(target.port),
        dbname=POSTGRESQL_WAREHOUSE_DATABASE_NAME,
        user=POSTGRESQL_WAREHOUSE_ADMINISTRATION_ROLE,
        password=administration.resolve().get_secret_value(),
        sslmode="verify-full",
        sslrootcert=str(target.root_certificate),
        sslcert=str(target.client_certificate),
        sslkey=str(target.client_private_key),
    )


def _provisioned(
    orchestrator: WarehouseLifecycleOrchestrator,
    binding: WarehouseBinding,
    *,
    compose_file: Path,
) -> WarehouseBinding:
    """Drive the binding to `ready`, translating a classified failure into advice."""
    try:
        ready = orchestrator.provision(
            binding.tenant_id, binding.binding_id, expected_revision=binding.revision
        )
    except WarehouseProviderError as failure:
        raise ManagedWarehouseRefused(
            "warehouse-control could not create the demonstration's warehouse: the "
            f"{failure.operation} step failed and was classified {failure.classification.value}. "
            f"{_advice(failure.classification)} The Compose project is {compose_file}."
        ) from failure
    except RuntimeError as refused:
        # `WarehouseAdmissionError`, `WarehouseOperationConflictError` and the persistence
        # errors are all `RuntimeError`. Each is a statement about the binding rather than
        # about Docker, so it is reported as itself rather than given Docker advice.
        raise ManagedWarehouseRefused(
            "warehouse-control refused the demonstration's warehouse binding: "
            f"{refused}. Remove the state directory to start a fresh demonstration."
        ) from refused
    if ready.lifecycle_state is not WarehouseBindingState.READY:
        # Unreachable while `provision` returns only an admitted binding; it guards an
        # orchestrator that grows a path returning before admission, which would otherwise
        # leave the console reporting a warehouse nothing validated.
        raise ManagedWarehouseRefused(
            "warehouse-control returned a binding that is "
            f"{ready.lifecycle_state.value} rather than ready"
        )
    return ready


def _advice(classification: WarehouseFailureClassification) -> str:
    """What an operator can do about one classified provisioning failure."""
    if classification in {
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
    }:
        return (
            "This is what an unreachable Docker daemon looks like: check that the daemon is "
            "running and that this process can reach its socket -- a console in a container "
            "needs that socket bind-mounted into it, which is host-level access. Unset "
            "HEINZEL_DEMO_WAREHOUSE_CONTROL to run the demonstration on its own warehouse "
            "instead."
        )
    return (
        "The Docker daemon answered and refused, so this is the project or the machine rather "
        "than the socket: check the daemon's logs, that the pinned PostgreSQL image can be "
        "pulled, and that no earlier warehouse of this demonstration is still running."
    )


def _resumable_binding(
    control: WarehouseControlService, *, record: Path
) -> WarehouseBinding | None:
    """The binding a previous start recorded, if this start may carry on with it.

    `None` means there is none to resume and a draft should be created. A binding in any state
    this process cannot carry on from is refused rather than worked around: see the module
    docstring for why a part-way provisioning is not resumable here.
    """
    written = _recorded(record)
    if written is None:
        return None
    binding_id = written.binding_id
    try:
        binding = control.get(DEMO_TENANT_ID, binding_id)
    except KeyError:
        raise ManagedWarehouseRefused(
            f"{record} names warehouse binding {binding_id}, which the binding store beside it "
            "does not hold. Remove the state directory to start a fresh demonstration."
        ) from None
    if binding.lifecycle_state not in _RESUMABLE_STATES:
        raise ManagedWarehouseRefused(
            f"the demonstration's warehouse binding is {binding.lifecycle_state.value}: a "
            "previous start did not finish provisioning it, and this start holds none of the "
            "credentials that warehouse was created with. Remove any container, network and "
            "volume that start left behind with `docker compose --project-name <project> down "
            "--volumes`, then remove the state directory and start again."
        )
    return binding


@dataclass(frozen=True, slots=True)
class _WarehouseRecord:
    """What a previous start wrote down about the warehouse it made.

    `operation_id` and `secret_reference` are absent for a binding recorded before this
    demonstration kept its secrets, and for a draft that has not been provisioned yet. Absent
    means the same thing in both: there is nothing stored to administer this warehouse with.
    """

    binding_id: str
    operation_id: str | None
    secret_reference: str | None


def _recorded(record: Path) -> _WarehouseRecord | None:
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ManagedWarehouseRefused(
            f"{record} does not hold a readable warehouse binding record. Remove the state "
            "directory to start a fresh demonstration."
        ) from None
    if not isinstance(payload, dict):
        raise ManagedWarehouseRefused(f"{record} does not hold a warehouse binding record")
    binding_id = payload.get(_BINDING_RECORD_KEY)
    if not isinstance(binding_id, str) or not binding_id:
        raise ManagedWarehouseRefused(f"{record} names no warehouse binding")
    operation_id = payload.get(_OPERATION_RECORD_KEY)
    secret_reference = payload.get(_SECRET_RECORD_KEY)
    return _WarehouseRecord(
        binding_id=binding_id,
        operation_id=operation_id if isinstance(operation_id, str) and operation_id else None,
        secret_reference=(
            secret_reference if isinstance(secret_reference, str) and secret_reference else None
        ),
    )


def _write_record(record: Path, written: _WarehouseRecord) -> None:
    """Write the record, flushed before the thing it describes is created.

    Replaced rather than appended to: the draft is recorded before provisioning, and the
    operation its secrets went to is only known once they have been stored, so the same file is
    written twice. A torn write is caught by `_recorded` and reported as a state directory to
    discard, which is the same answer either time.
    """
    payload: dict[str, str] = {_BINDING_RECORD_KEY: written.binding_id}
    if written.operation_id is not None:
        payload[_OPERATION_RECORD_KEY] = written.operation_id
    if written.secret_reference is not None:
        payload[_SECRET_RECORD_KEY] = written.secret_reference
    record.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _require_compose_file(compose_file: Path) -> Path:
    """The Compose project the provider drives, refusing a path that holds none.

    Checked before anything is opened, because the alternative is a warehouse binding created
    for a project `docker compose` cannot read. A symlink is refused as well: this file decides
    what the demonstration runs as root inside a container, so it has to be the one in the
    repository rather than wherever a link points.
    """
    if compose_file.is_symlink() or not compose_file.is_file():
        raise ManagedWarehouseRefused(
            f"the demonstration's warehouse Compose project is not a file at {compose_file}. "
            "This path provisions through warehouse-control, which drives that project, so it "
            "has to be run from a checkout that carries deploy/quickstart/warehouse-control."
        )
    return compose_file


def _require_docker(option: ManagedWarehouseOption) -> None:
    """Refuse up front when `docker` is not on PATH.

    The compose boundary would classify a missing executable as a transient unavailability and
    the orchestrator would record a resumable failure against a binding, which reads as a
    warehouse that might come back. A missing command will not come back on its own.
    """
    if option.executable_lookup("docker") is None:
        raise ManagedWarehouseRefused(
            "provisioning through warehouse-control drives Docker Compose, and no `docker` "
            "executable is on PATH. Install Docker, or unset HEINZEL_DEMO_WAREHOUSE_CONTROL to "
            "run the demonstration on its own warehouse instead."
        )


def _private_directory(directory: Path) -> Path:
    """An owner-only directory the provider will accept as its private operation directory.

    `PostgreSQLWarehouseSettings` refuses a relative or symlinked path and the provider refuses
    one whose mode is not 0700, because it writes TLS keys and a bootstrap password there. The
    mode is set rather than assumed: `mkdir` applies the process umask, so a directory created
    under the default umask is group-readable and the provisioning would fail at its first
    private write.
    """
    resolved = directory.resolve()
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved.chmod(0o700)
    return resolved


def _operation_secrets(
    *, clock: Callable[[], datetime], internal_hostnames: tuple[str, ...]
) -> WarehouseOperationSecrets:
    """One start's eight principal passwords, its TLS material and its backup key.

    Validated by `WarehouseOperationSecrets` itself, which is the model warehouse-control's own
    secret store persists, so a field that moves is a failure here rather than a provider
    holding a credential nothing minted.

    `internal_hostnames` go into the server certificate, for a client that reaches the warehouse
    across a container network instead of through the published loopback port. They are known
    before the warehouse exists because they derive from the binding, which is why the certificate
    can cover them without a second provisioning.
    """
    material = generate_demo_warehouse_tls_material(
        now=clock(), internal_hostnames=internal_hostnames
    )
    return WarehouseOperationSecrets(
        administration_password=_password(),
        ingestion_runtime_password=_password(),
        transformation_runtime_password=_password(),
        answer_runtime_password=_password(),
        backup_restore_password=_password(),
        customer_sql_probe_password=_password(),
        catalog_password=_password(),
        bi_password=_password(),
        tls_private_key_pem=SecretStr(material.private_key_bundle),
        tls_certificate_pem=SecretStr(material.certificate_bundle),
        backup_encryption_key_b64=SecretStr(
            base64.urlsafe_b64encode(os.urandom(_BACKUP_ENCRYPTION_KEY_BYTES)).decode("ascii")
        ),
    )


def _password() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(_PASSWORD_BYTES))


def _build_provider(
    directory: Path,
    *,
    compose_file: Path,
    option: ManagedWarehouseOption,
    repository: SQLiteWarehouseRepository,
    binding_id: str,
    authority: WarehouseSecretAuthority,
    operation_id: str,
    secret_reference: str,
    clock: Callable[[], datetime],
) -> PostgreSQLWarehouseProvider:
    """Compose the real provider over the demonstration's Compose project.

    Nothing here is a fixture. The passwords become the warehouse's real roles, the TLS
    material becomes its real certificate, and the Compose project is the one in `deploy/`.

    Every secret arrives as a capability the store issues for one purpose, which is what the
    provider's boundary takes and why nothing downstream ever holds the set. It also means this
    composes identically whether the secrets were minted a moment ago or by a start that has
    since exited -- a resumed warehouse is administered the same way a fresh one is.
    """
    settings = PostgreSQLWarehouseSettings(
        private_operation_directory=directory,
        retention_period=_RETENTION_PERIOD,
        backup_chunk_bytes=_BACKUP_CHUNK_BYTES,
        capacity_bytes=_CAPACITY_BYTES,
    )
    compose = _compose_process(compose_file, option=option)
    recorder = _RepositoryResourceRecorder(repository, binding_id=binding_id, clock=clock)

    def purpose(name: WarehouseOperationSecretPurpose) -> WarehouseOperationSecretCapability:
        return authority.operation_capability(
            secret_reference, operation_id=operation_id, purpose=name
        )

    return PostgreSQLWarehouseProvider(
        settings=settings,
        compose=compose,
        resource_recorder=recorder,
        administration_secret=purpose("administration"),
        ingestion_runtime_secret=purpose("ingestion_runtime"),
        transformation_runtime_secret=purpose("transformation_runtime"),
        answer_runtime_secret=purpose("answer_runtime"),
        customer_sql_secret=purpose("customer_sql"),
        catalog_secret=purpose("catalog"),
        bi_secret=purpose("bi"),
        tls_private_key_secret=purpose("tls_private_key"),
        tls_certificate_secret=purpose("tls_certificate"),
        backup_commands=PostgreSQLBackupCommandBoundary(
            settings=settings,
            compose=compose,
            resource_recorder=recorder,
            secrets=authority.backup_command_capability(
                secret_reference, operation_id=operation_id
            ),
            retirement=authority.backup_retirement_capability(
                secret_reference, operation_id=operation_id
            ),
            connect=option.connect,
            clock=clock,
        ),
        connect=option.connect,
        clock=clock,
    )


def _compose_process(compose_file: Path, *, option: ManagedWarehouseOption) -> DockerComposeProcess:
    """`DockerComposeProcess` over this project, with the caller's process starter if it gave one.

    The starter is passed only when it was supplied, so the default stays
    `DockerComposeProcess`'s own rather than a copy of it that could drift.
    """
    if option.run is None:
        return DockerComposeProcess(
            compose_file=compose_file,
            timeout_seconds=_COMPOSE_TIMEOUT_SECONDS,
            termination_grace_seconds=_COMPOSE_TERMINATION_GRACE_SECONDS,
        )
    return DockerComposeProcess(
        compose_file=compose_file,
        run=option.run,
        timeout_seconds=_COMPOSE_TIMEOUT_SECONDS,
        termination_grace_seconds=_COMPOSE_TERMINATION_GRACE_SECONDS,
    )
