from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogControlService,
    CatalogPersistenceError,
    CatalogValidationEvidence,
    SQLiteCatalogRepository,
)
from heinzel_catalog_control import repository as repository_module
from pydantic import ValidationError

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
LATER = datetime(2026, 8, 20, 12, tzinfo=UTC)


class FaultingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fail_rollback: bool = False,
    ) -> None:
        self._connection = connection
        self._fail_rollback = fail_rollback

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO catalog_bindings"):
            raise sqlite3.OperationalError("catalog write failed")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        if self._fail_rollback:
            raise sqlite3.OperationalError("catalog rollback failed")
        self._connection.rollback()


class IntegrityFailingConnection(FaultingConnection):
    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO catalog_bindings"):
            raise sqlite3.IntegrityError("catalog storage integrity failed")
        return self._connection.execute(sql, parameters)


def service(*, now: datetime = NOW) -> CatalogControlService:
    return CatalogControlService(SQLiteCatalogRepository(":memory:"), clock=lambda: now)


def evidence_for(
    binding: CatalogBinding, *, evidence_id: str = "cve-test"
) -> CatalogValidationEvidence:
    return CatalogValidationEvidence(
        evidence_id=evidence_id,
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        provider_version="1.13.3",
        provider_build_digest="f" * 64,
        provider_image_set_digest="0" * 64,
        positive_probe_digest="b" * 64,
        denial_probe_digest="c" * 64,
        stable_identity_probe_digest="d" * 64,
        backup_probe_digest="e" * 64,
        observed_at=NOW,
    )


def advance_to_validating(control: CatalogControlService) -> CatalogBinding:
    binding = control.create_draft(tenant_id="tenant-a")
    for state in (CatalogBindingState.PROVISIONING, CatalogBindingState.VALIDATING):
        binding = control.transition(
            "tenant-a", binding.binding_id, state, expected_revision=binding.revision
        )
    return binding


def test_ready_requires_successful_positive_and_denial_validation() -> None:
    control = service()
    validating = advance_to_validating(control)

    with pytest.raises(ValueError, match="validation evidence"):
        control.transition(
            "tenant-a",
            validating.binding_id,
            CatalogBindingState.READY,
            expected_revision=validating.revision,
        )


def test_record_validation_admits_ready_and_sets_provisioned_time() -> None:
    control = service()
    validating = advance_to_validating(control)

    ready = control.record_validation(
        tenant_id="tenant-a",
        binding_id=validating.binding_id,
        expected_revision=validating.revision,
        evidence=evidence_for(validating),
    )

    assert ready.lifecycle_state is CatalogBindingState.READY
    assert ready.revision == validating.revision + 1
    assert ready.provisioned_at == NOW
    assert control.get("tenant-a", ready.binding_id) == ready


@pytest.mark.parametrize(
    "evidence_update",
    (
        {"tenant_id": "tenant-b"},
        {"binding_id": "cat-other"},
        {"binding_revision": 2},
    ),
)
def test_validation_evidence_must_match_the_current_binding(
    evidence_update: dict[str, object],
) -> None:
    control = service()
    validating = advance_to_validating(control)
    evidence = CatalogValidationEvidence.model_validate(
        evidence_for(validating).model_dump() | evidence_update
    )

    with pytest.raises(ValueError, match="validation evidence does not match"):
        control.record_validation(
            tenant_id="tenant-a",
            binding_id=validating.binding_id,
            expected_revision=validating.revision,
            evidence=evidence,
        )

    assert control.get("tenant-a", validating.binding_id) == validating


def test_invalid_and_stale_transitions_do_not_advance_the_binding() -> None:
    control = service()
    draft = control.create_draft(tenant_id="tenant-a")
    provisioning = control.transition(
        "tenant-a",
        draft.binding_id,
        CatalogBindingState.PROVISIONING,
        expected_revision=draft.revision,
    )

    with pytest.raises(ValueError, match="binding revision is stale"):
        control.transition(
            "tenant-a",
            draft.binding_id,
            CatalogBindingState.FAILED,
            expected_revision=draft.revision,
        )
    with pytest.raises(ValueError, match="transition provisioning -> retired is not allowed"):
        control.transition(
            "tenant-a",
            provisioning.binding_id,
            CatalogBindingState.RETIRED,
            expected_revision=provisioning.revision,
        )

    assert control.get("tenant-a", draft.binding_id) == provisioning


def test_one_tenant_cannot_read_or_move_another_tenants_binding() -> None:
    control = service()
    binding = control.create_draft(tenant_id="tenant-a")

    with pytest.raises(KeyError, match="belongs to another tenant"):
        control.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        control.transition(
            "tenant-b",
            binding.binding_id,
            CatalogBindingState.PROVISIONING,
            expected_revision=binding.revision,
        )

    assert control.get("tenant-a", binding.binding_id) == binding


def test_binding_identity_uses_tenant_sequence_and_not_the_clock() -> None:
    first = service(now=NOW).create_draft(tenant_id="tenant-a")
    second = service(now=LATER).create_draft(tenant_id="tenant-a")

    assert first.binding_id == second.binding_id
    assert first.created_at != second.created_at


def test_one_repository_allocates_distinct_binding_identities() -> None:
    control = service()

    first = control.create_draft(tenant_id="tenant-a")
    second = control.create_draft(tenant_id="tenant-a")

    assert first.binding_id != second.binding_id


@pytest.mark.parametrize(
    "clock_value",
    (
        datetime(2026, 8, 19, 12),
        datetime(2026, 8, 19, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_create_draft_rejects_non_utc_clock_before_persisting(clock_value: datetime) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: clock_value)

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        control.create_draft(tenant_id="tenant-a")
    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.load("tenant-a", "cat-unallocated")

    recovered = CatalogControlService(repository, clock=lambda: NOW).create_draft(
        tenant_id="tenant-a"
    )
    baseline = service().create_draft(tenant_id="tenant-a")
    assert recovered.binding_id == baseline.binding_id


def test_create_draft_translates_a_closed_sqlite_connection() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    repository._connection.close()

    with pytest.raises(CatalogPersistenceError) as captured:
        control.create_draft(tenant_id="tenant-a")

    assert captured.value.operation == "create catalog draft"
    assert isinstance(captured.value.__cause__, sqlite3.ProgrammingError)


def test_get_translates_a_closed_sqlite_connection() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    repository._connection.close()

    with pytest.raises(CatalogPersistenceError) as captured:
        control.get("tenant-a", binding.binding_id)

    assert captured.value.operation == "load catalog binding"
    assert isinstance(captured.value.__cause__, sqlite3.ProgrammingError)


def test_transition_translates_an_injected_sqlite_operational_error() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    repository._connection = FaultingConnection(repository._connection)

    with pytest.raises(CatalogPersistenceError) as captured:
        control.transition(
            "tenant-a",
            binding.binding_id,
            CatalogBindingState.PROVISIONING,
            expected_revision=binding.revision,
        )

    assert captured.value.operation == "append catalog binding revision"
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    assert str(captured.value.__cause__) == "catalog write failed"
    assert control.get("tenant-a", binding.binding_id) == binding


def test_rollback_failure_cannot_replace_the_original_storage_error() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    repository._connection = FaultingConnection(repository._connection, fail_rollback=True)

    with pytest.raises(CatalogPersistenceError) as captured:
        control.transition(
            "tenant-a",
            binding.binding_id,
            CatalogBindingState.PROVISIONING,
            expected_revision=binding.revision,
        )

    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    assert str(captured.value.__cause__) == "catalog write failed"


def test_repository_initialization_translates_sqlite_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(database_path: str) -> sqlite3.Connection:
        raise sqlite3.OperationalError(f"cannot open {database_path}")

    monkeypatch.setattr(repository_module.sqlite3, "connect", fail_connect)

    with pytest.raises(CatalogPersistenceError) as captured:
        SQLiteCatalogRepository("unavailable.db")

    assert captured.value.operation == "initialize catalog repository"
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)


def test_record_validation_translates_unrelated_sqlite_integrity_error() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = advance_to_validating(control)
    repository._connection = IntegrityFailingConnection(repository._connection)

    with pytest.raises(CatalogPersistenceError) as captured:
        control.record_validation(
            tenant_id="tenant-a",
            binding_id=binding.binding_id,
            expected_revision=binding.revision,
            evidence=evidence_for(binding),
        )

    assert captured.value.operation == "record catalog validation"
    assert isinstance(captured.value.__cause__, sqlite3.IntegrityError)
