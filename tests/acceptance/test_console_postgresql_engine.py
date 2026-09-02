from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pillarmesh_warehouse_control import (
    EngineKind,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseFailureClassification,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseResourceCleanupStatus,
    WarehouseResourceCreationState,
    WarehouseResourceKind,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from pillarmesh_warehouse_control.service import WarehouseControlService

from tests.acceptance.console_postgresql_engine import (
    DeferredPostgreSQLProvider,
    RepositoryResourceRecorder,
    private_secret_directories,
)

_NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
_TENANT = "tenant-a"
_OPERATION = "wop-" + "0" * 24


def _clock() -> datetime:
    return _NOW


@pytest.fixture
def repository(tmp_path: Path):
    opened = SQLiteWarehouseRepository(str(tmp_path / "warehouse.sqlite3"))
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def binding_id(repository: SQLiteWarehouseRepository) -> str:
    """A real binding: the resource ledger is a child table with a composite key."""
    control = WarehouseControlService(
        repository, clock=_clock, readiness_policy=LocalAcceptanceWarehouseReadinessPolicy()
    )
    draft = control.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    repository.claim_operation(
        PrivateWarehouseOperation(
            tenant_id=_TENANT,
            binding_id=draft.binding_id,
            binding_revision=draft.revision,
            operation_id=_OPERATION,
            operation_kind=WarehouseOperationKind.PROVISION,
            engine_kind=EngineKind.POSTGRESQL,
            status=WarehouseOperationStatus.CLAIMED,
            phase=WarehouseOperationPhase.CLAIMED,
            started_at=_NOW,
            updated_at=_NOW,
        )
    )
    return draft.binding_id


def test_the_provider_is_bound_to_the_operation_that_actually_arrives(
    repository: SQLiteWarehouseRepository,
) -> None:
    """Secrets follow the operation the orchestrator minted, not a predicted one.

    `next_operation_sequence` allocates rather than reads, so the identifier is not
    predictable after the first attempt: a retry following a failed provision, or a
    second run against the same state directory, mints a later sequence. Binding the
    credentials to a guess made them belong to a different operation than the one the
    ledger records, and nothing errors, because a secret capability returns its stored
    value without checking the operation.
    """
    seen: list[str] = []

    def factory(*, binding_id: str, operation_id: str):
        seen.append(operation_id)
        return object()

    deferred = DeferredPostgreSQLProvider(factory)

    first = deferred.provider_for("whb-one", "wop-" + "a" * 24)
    again = deferred.provider_for("whb-one", "wop-" + "a" * 24)
    second = deferred.provider_for("whb-one", "wop-" + "b" * 24)

    assert seen == ["wop-" + "a" * 24, "wop-" + "b" * 24]
    assert first is again
    assert second is not first


def _planned(resource_id: str, binding_id: str) -> PrivateWarehouseResource:
    return PrivateWarehouseResource(
        tenant_id=_TENANT,
        binding_id=binding_id,
        binding_revision=1,
        operation_id=_OPERATION,
        resource_id=resource_id,
        resource_kind=WarehouseResourceKind.COMPOSE_PROJECT,
        provider_resource_handle=f"private://compose/{resource_id}",
        creation_state=WarehouseResourceCreationState.PLANNED,
        cleanup_status=WarehouseResourceCleanupStatus.PENDING,
        retention_deadline=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_the_recorder_records_a_resource_before_it_is_created(
    repository: SQLiteWarehouseRepository, binding_id: str
) -> None:
    recorder = RepositoryResourceRecorder(repository, binding_id=binding_id, clock=_clock)
    resource = _planned("wrs-0000000000000000000000000001", binding_id)

    recorder.record_planned(resource)
    planned = recorder.load_resources(_TENANT, binding_id)
    recorder.mark_created(_TENANT, resource.resource_id, resource.provider_resource_handle)
    created = recorder.load_resources(_TENANT, binding_id)

    assert planned[0].creation_state is WarehouseResourceCreationState.PLANNED
    assert created[0].creation_state is WarehouseResourceCreationState.CREATED


def test_the_recorder_refuses_a_handle_that_changed_after_planning(
    repository: SQLiteWarehouseRepository, binding_id: str
) -> None:
    """Cleanup can only remove what the ledger names, so the handle may not drift."""
    recorder = RepositoryResourceRecorder(repository, binding_id=binding_id, clock=_clock)
    resource = _planned("wrs-0000000000000000000000000002", binding_id)
    recorder.record_planned(resource)

    with pytest.raises(RuntimeError, match="provider resource handle changed"):
        recorder.mark_created(_TENANT, resource.resource_id, "private://compose/something-else")


def test_the_recorder_carries_an_ambiguous_creation_and_a_cleanup_failure(
    repository: SQLiteWarehouseRepository, binding_id: str
) -> None:
    recorder = RepositoryResourceRecorder(repository, binding_id=binding_id, clock=_clock)
    resource = _planned("wrs-0000000000000000000000000003", binding_id)
    recorder.record_planned(resource)

    recorder.mark_ambiguous(_TENANT, resource.resource_id)
    ambiguous = recorder.load_resources(_TENANT, binding_id)[0]
    recorder.record_cleanup(
        _TENANT,
        resource.resource_id,
        WarehouseResourceCleanupStatus.FAILED,
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
    )
    cleaned = recorder.load_resources(_TENANT, binding_id)[0]

    assert ambiguous.creation_state is WarehouseResourceCreationState.AMBIGUOUS
    assert cleaned.cleanup_status is WarehouseResourceCleanupStatus.FAILED
    assert (
        cleaned.cleanup_failure_classification
        is WarehouseFailureClassification.TRANSIENT_UNAVAILABLE
    )


def test_the_provider_is_built_only_once_the_binding_exists(
    repository: SQLiteWarehouseRepository, tmp_path: Path
) -> None:
    """The console creates the binding; the provider needs its identifier to record.

    Constructing the real provider at startup is impossible, because the ledger
    recorder is scoped to a binding that does not exist until someone confirms one
    in the browser. The provider is therefore built on the first lifecycle call,
    which is the first moment the binding is known.
    """
    built: list[str] = []

    def factory(*, binding_id: str, operation_id: str):
        built.append(binding_id)
        return object()

    deferred = DeferredPostgreSQLProvider(factory)
    assert built == []

    first = deferred.provider_for("whb-one", "wop-" + "a" * 24)
    second = deferred.provider_for("whb-one", "wop-" + "a" * 24)

    assert built == ["whb-one"]
    assert first is second


def test_a_second_binding_is_refused_rather_than_silently_rebuilt(
    repository: SQLiteWarehouseRepository,
) -> None:
    """One run provisions one binding; its secrets are bound to one operation."""
    deferred = DeferredPostgreSQLProvider(lambda *, binding_id, operation_id: object())
    deferred.provider_for("whb-one", "wop-" + "a" * 24)

    with pytest.raises(RuntimeError, match="one warehouse binding"):
        deferred.provider_for("whb-two", "wop-" + "b" * 24)


def test_the_secret_directories_contain_no_symlinked_component(tmp_path: Path) -> None:
    """The secret store walks every path component with `O_NOFOLLOW`.

    On macOS both `/tmp` and `/var` are symlinks, and the default state directory
    lives under `tempfile.gettempdir()`, which resolves through `/var`. Handing the
    unresolved path to the secret authority raises
    `WarehouseSecretStorageError`, which the orchestrator classifies as
    `invalid_provider_response` -- a permanent governed failure that says nothing
    about the symlink.
    """
    through_symlink = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    through_symlink.symlink_to(real, target_is_directory=True)

    private, secret = private_secret_directories(through_symlink)

    assert not any(part.is_symlink() for part in (private, *private.parents))
    assert not any(part.is_symlink() for part in (secret, *secret.parents))
    assert private.stat().st_mode & 0o777 == 0o700
    assert secret.stat().st_mode & 0o777 == 0o700


def test_the_deferred_provider_builds_once_under_concurrent_first_calls(
    repository: SQLiteWarehouseRepository,
) -> None:
    """Command routes run the backend in a threadpool.

    `InFlightCommandKeys` serializes one command identity, so two confirmations
    carrying different idempotency keys reach the provider concurrently. An
    unsynchronized check-then-set let both build a provider, storing two sets of
    operation secrets into one directory and starting two compose stacks.
    """
    barrier = threading.Barrier(4)
    built = 0
    built_lock = threading.Lock()

    def factory(*, binding_id: str, operation_id: str):
        nonlocal built
        with built_lock:
            built += 1
        return object()

    deferred = DeferredPostgreSQLProvider(factory)
    results: list[object] = []
    results_lock = threading.Lock()

    def call() -> None:
        barrier.wait()
        provider = deferred.provider_for("whb-one", _OPERATION)
        with results_lock:
            results.append(provider)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert built == 1
    assert len(results) == 4
    assert all(result is results[0] for result in results)
