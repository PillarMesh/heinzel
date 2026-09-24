from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogControlService,
    CatalogValidationEvidence,
    SQLiteCatalogRepository,
)
from heinzel_catalog_control.repository import (
    CatalogOperationConflictError,
    CatalogResourceCleanupFailureClassification,
    CatalogResourceCleanupStatus,
    CatalogResourceCreationState,
    CatalogResourceKind,
    PrivateCatalogOperation,
    PrivateCatalogResource,
    StaleRevisionError,
    _transaction,
)

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def operation_for(binding: CatalogBinding, *, operation_id: str) -> PrivateCatalogOperation:
    return PrivateCatalogOperation(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        operation_id=operation_id,
        resource_handle=f"handle-{operation_id}",
        project_name=f"project-{operation_id}",
        secret_reference=f"secret-{operation_id}",
        created_at=NOW,
    )


def resource_for(
    binding: CatalogBinding,
    *,
    resource_id: str = "resource-1",
    provider_ref: str = "provider-private-1",
) -> PrivateCatalogResource:
    return PrivateCatalogResource(
        tenant_id=binding.tenant_id,
        binding_id=binding.binding_id,
        resource_id=resource_id,
        resource_kind=CatalogResourceKind.CATALOG_SERVICE,
        provider_ref=provider_ref,
        creation_state="created",
        retention_deadline=NOW + timedelta(days=30),
        cleanup_status="not_started",
        created_at=NOW,
        cleaned_at=None,
    )


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


def advance_to_validating(
    control: CatalogControlService, *, tenant_id: str = "tenant-a"
) -> CatalogBinding:
    binding = control.create_draft(tenant_id=tenant_id)
    for state in (CatalogBindingState.PROVISIONING, CatalogBindingState.VALIDATING):
        binding = control.transition(
            tenant_id, binding.binding_id, state, expected_revision=binding.revision
        )
    return binding


def test_binding_revisions_are_append_only() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = advance_to_validating(control)

    revisions = repository._connection.execute(
        "SELECT revision FROM catalog_bindings WHERE tenant_id = ? AND binding_id = ? "
        "ORDER BY revision",
        ("tenant-a", binding.binding_id),
    ).fetchall()

    assert revisions == [(1,), (2,), (3,)]


def test_repository_rejects_a_noncontiguous_binding_revision() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = repository.create_draft("tenant-a", NOW)
    skipped_revision = binding.model_copy(update={"revision": 3})

    with pytest.raises(StaleRevisionError, match="revision was not advanced"):
        repository.append_transition(skipped_revision, expected_revision=1)

    assert repository.load("tenant-a", binding.binding_id) == binding


def test_repository_compare_and_swap_rejects_a_stale_writer() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = repository.create_draft("tenant-a", NOW)
    provisioning = binding.model_copy(
        update={"lifecycle_state": CatalogBindingState.PROVISIONING, "revision": 2}
    )
    retired = binding.model_copy(
        update={"lifecycle_state": CatalogBindingState.RETIRED, "revision": 2}
    )

    repository.append_transition(provisioning, expected_revision=1)
    with pytest.raises(StaleRevisionError, match="revision was not advanced"):
        repository.append_transition(retired, expected_revision=1)

    assert repository.load("tenant-a", binding.binding_id) == provisioning


def test_cross_tenant_load_is_denied_before_validation_payload_is_read() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = repository.create_draft("tenant-a", NOW)
    repository._connection.execute(
        "UPDATE catalog_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (b"not-json", "tenant-a", binding.binding_id),
    )
    repository._connection.commit()

    with pytest.raises(KeyError, match="another tenant"):
        repository.load("tenant-b", binding.binding_id)


def test_one_operation_claim_per_binding_replays_only_the_same_operation() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    binding = repository.create_draft("tenant-a", NOW)

    assert repository.claim_operation("tenant-a", binding.binding_id, "operation-a") is True
    assert repository.claim_operation("tenant-a", binding.binding_id, "operation-a") is False
    with pytest.raises(CatalogOperationConflictError, match="another operation"):
        repository.claim_operation("tenant-a", binding.binding_id, "operation-b")

    repository.record_operation(operation_for(binding, operation_id="operation-a"), resources=())
    assert (
        repository.load_operation("tenant-a", binding.binding_id, "operation-a").resource_handle
        == "handle-operation-a"
    )


def test_private_resource_vocabulary_covers_every_openmetadata_side_effect() -> None:
    assert {
        CatalogResourceKind.CATALOG_POLICY.value,
        CatalogResourceKind.CATALOG_ROLE.value,
        CatalogResourceKind.CATALOG_TAG.value,
        CatalogResourceKind.CATALOG_LINEAGE.value,
    } == {
        "catalog_policy",
        "catalog_role",
        "catalog_tag",
        "catalog_lineage",
    }


def test_concurrent_operation_claims_admit_exactly_one_operation(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    repository = SQLiteCatalogRepository(str(database_path))
    binding = repository.create_draft("tenant-a", NOW)
    repository._connection.close()
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    outcomes_lock = threading.Lock()

    def claim(operation_id: str) -> None:
        thread_repository = SQLiteCatalogRepository(str(database_path))
        barrier.wait()
        try:
            claimed = thread_repository.claim_operation(
                "tenant-a", binding.binding_id, operation_id
            )
            outcome = "claimed" if claimed else "replayed"
        except CatalogOperationConflictError:
            outcome = "conflict"
        finally:
            thread_repository._connection.close()
        with outcomes_lock:
            outcomes.append((operation_id, outcome))

    threads = tuple(
        threading.Thread(target=claim, args=(operation_id,))
        for operation_id in ("operation-a", "operation-b")
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for _, outcome in outcomes) == ["claimed", "conflict"]
    winner = next(operation_id for operation_id, outcome in outcomes if outcome == "claimed")
    replay_repository = SQLiteCatalogRepository(str(database_path))
    assert replay_repository.claim_operation("tenant-a", binding.binding_id, winner) is False
    replay_repository._connection.close()


def test_existing_operation_rows_are_backfilled_as_binding_claims(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite"
    repository = SQLiteCatalogRepository(str(database_path))
    binding = repository.create_draft("tenant-a", NOW)
    repository.record_operation(operation_for(binding, operation_id="operation-a"), resources=())
    repository._connection.execute("DELETE FROM private_catalog_operation_claims")
    repository._connection.commit()
    repository._connection.close()

    reopened = SQLiteCatalogRepository(str(database_path))

    assert reopened.claim_operation("tenant-a", binding.binding_id, "operation-a") is False
    with pytest.raises(CatalogOperationConflictError, match="another operation"):
        reopened.claim_operation("tenant-a", binding.binding_id, "operation-b")
    reopened._connection.close()


def test_cleanup_rejects_unrecorded_provider_identifier() -> None:
    repository = SQLiteCatalogRepository(":memory:")

    with pytest.raises(KeyError, match="recorded resource"):
        repository.begin_cleanup("tenant-a", "not-in-ledger")


def test_cleanup_accepts_only_exact_recorded_resource_id() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    resource = resource_for(binding)
    repository.record_resource(resource)

    with pytest.raises(KeyError, match="recorded resource"):
        repository.begin_cleanup("tenant-a", resource.provider_ref)

    cleaning = repository.begin_cleanup("tenant-a", resource.resource_id)
    complete = repository.complete_cleanup("tenant-a", resource.resource_id, cleaned_at=NOW)

    assert cleaning.cleanup_status == "in_progress"
    assert complete.cleanup_status == "complete"
    assert complete.cleaned_at == NOW


def test_exact_resource_is_marked_created_only_after_discovery() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    planned = replace(resource_for(binding), creation_state="planned")
    repository.record_resource(planned)

    created = repository.mark_resource_created("tenant-a", planned.resource_id)

    assert created.creation_state == "created"
    assert repository.load_resource("tenant-a", planned.resource_id) == created
    with pytest.raises(ValueError, match="not planned"):
        repository.mark_resource_created("tenant-a", planned.resource_id)

    assert repository.load_resource("tenant-a", planned.resource_id) == created
    assert control.get("tenant-a", binding.binding_id) == binding


def test_discovery_atomically_binds_the_exact_provider_identifier() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    planned = replace(
        resource_for(binding),
        provider_ref="planned:catalog-service",
        creation_state="planned",
    )
    repository.record_resource(planned)

    created = repository.mark_resource_created(
        "tenant-a",
        planned.resource_id,
        provider_ref="exact-provider-id",
    )

    assert created.creation_state == "created"
    assert created.provider_ref == "exact-provider-id"
    assert repository.load_resource("tenant-a", planned.resource_id) == created


@pytest.mark.parametrize(
    ("terminal_status", "failure_classification"),
    [("failed", "transient"), ("unknown", "unknown")],
)
def test_unconfirmed_cleanup_records_its_exact_terminal_status(
    terminal_status: Literal["failed", "unknown"],
    failure_classification: CatalogResourceCleanupFailureClassification,
) -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    resource = resource_for(control.create_draft(tenant_id="tenant-a"))
    repository.record_resource(resource)
    repository.begin_cleanup("tenant-a", resource.resource_id)

    terminal = repository.fail_cleanup(
        "tenant-a",
        resource.resource_id,
        status=terminal_status,
        failure_classification=failure_classification,
    )

    assert terminal.cleanup_status == terminal_status
    assert terminal.cleanup_failure_classification == failure_classification
    assert terminal.cleaned_at is None
    assert repository.load_resource("tenant-a", resource.resource_id) == terminal


def test_cleanup_failure_records_a_sanitized_actionable_classification() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    resource = resource_for(binding)
    repository.record_resource(resource)
    repository.begin_cleanup("tenant-a", resource.resource_id)

    failed = repository.fail_cleanup(
        "tenant-a",
        resource.resource_id,
        status="failed",
        failure_classification="transient",
    )

    assert failed.cleanup_status == "failed"
    assert failed.cleanup_failure_classification == "transient"
    assert repository.load_resource("tenant-a", resource.resource_id) == failed
    assert control.get("tenant-a", binding.binding_id) == binding


def test_cleanup_terminal_state_cannot_be_restarted_or_completed() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    resource = resource_for(control.create_draft(tenant_id="tenant-a"))
    repository.record_resource(resource)
    repository.begin_cleanup("tenant-a", resource.resource_id)
    failed = repository.fail_cleanup(
        "tenant-a",
        resource.resource_id,
        status="unknown",
        failure_classification="unknown",
    )

    with pytest.raises(ValueError, match="terminal"):
        repository.begin_cleanup("tenant-a", resource.resource_id)
    with pytest.raises(ValueError, match="has not started"):
        repository.complete_cleanup("tenant-a", resource.resource_id, cleaned_at=NOW)

    assert repository.load_resource("tenant-a", resource.resource_id) == failed


def test_invalid_cleanup_failure_classification_is_rejected_before_writes() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    resource = resource_for(control.create_draft(tenant_id="tenant-a"))
    repository.record_resource(resource)
    in_progress = repository.begin_cleanup("tenant-a", resource.resource_id)

    with pytest.raises(ValueError, match="failure classification") as error:
        repository.fail_cleanup(
            "tenant-a",
            resource.resource_id,
            status="failed",
            failure_classification=cast(CatalogResourceCleanupFailureClassification, "secret"),
        )

    assert "secret" not in str(error.value)
    assert repository.load_resource("tenant-a", resource.resource_id) == in_progress


def test_existing_private_ledger_gains_cleanup_failure_classification_column(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "catalog.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE private_catalog_resources ("
        "tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "resource_kind TEXT NOT NULL, provider_ref TEXT NOT NULL, creation_state TEXT NOT NULL, "
        "retention_deadline TEXT NOT NULL, cleanup_status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "cleaned_at TEXT, PRIMARY KEY (tenant_id, resource_id))"
    )
    connection.commit()
    connection.close()

    repository = SQLiteCatalogRepository(str(database_path))

    columns = repository._connection.execute(
        "SELECT name FROM pragma_table_info('private_catalog_resources')"
    ).fetchall()
    assert ("cleanup_failure_classification",) in columns


class _OriginalTransitionError(Exception):
    pass


class _RollbackFailingConnection:
    def execute(self, statement: str) -> None:
        del statement

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        raise RuntimeError("rollback failure")


def test_rollback_failure_does_not_mask_the_original_transition_error() -> None:
    connection = cast(sqlite3.Connection, _RollbackFailingConnection())

    with pytest.raises(_OriginalTransitionError, match="original"), _transaction(connection):
        raise _OriginalTransitionError("original")


def test_resource_queries_deny_cross_tenant_access() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    resource = resource_for(control.create_draft(tenant_id="tenant-a"))
    repository.record_resource(resource)

    with pytest.raises(KeyError, match="recorded resource"):
        repository.load_resource("tenant-b", resource.resource_id)
    with pytest.raises(KeyError, match="recorded resource"):
        repository.begin_cleanup("tenant-b", resource.resource_id)


def test_validation_commits_evidence_binding_and_resource_state_together() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = advance_to_validating(control)
    resource = resource_for(binding)
    repository.record_resource(resource)

    ready = control.record_validation(
        tenant_id="tenant-a",
        binding_id=binding.binding_id,
        expected_revision=binding.revision,
        evidence=evidence_for(binding),
    )

    assert ready.lifecycle_state is CatalogBindingState.READY
    assert repository.load_validation("tenant-a", binding.binding_id) == evidence_for(binding)
    assert repository.load_resource("tenant-a", resource.resource_id).creation_state == "validated"


def test_validation_failure_rolls_back_binding_and_resource_state() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    first = advance_to_validating(control)
    repository.record_resource(resource_for(first, resource_id="resource-first"))
    control.record_validation(
        tenant_id="tenant-a",
        binding_id=first.binding_id,
        expected_revision=first.revision,
        evidence=evidence_for(first, evidence_id="cve-shared"),
    )
    second = advance_to_validating(control)
    second_resource = resource_for(second, resource_id="resource-second")
    repository.record_resource(second_resource)

    with pytest.raises(ValueError, match="validation evidence was not recorded"):
        control.record_validation(
            tenant_id="tenant-a",
            binding_id=second.binding_id,
            expected_revision=second.revision,
            evidence=evidence_for(second, evidence_id="cve-shared"),
        )

    assert control.get("tenant-a", second.binding_id) == second
    assert (
        repository.load_resource("tenant-a", second_resource.resource_id).creation_state
        == "created"
    )
    with pytest.raises(KeyError, match="validation evidence"):
        repository.load_validation("tenant-a", second.binding_id)


def _persistence_counts(
    repository: SQLiteCatalogRepository, binding: CatalogBinding
) -> tuple[int, int, int]:
    next_sequence = repository._connection.execute(
        "SELECT next_sequence FROM catalog_sequences WHERE tenant_id = ?",
        (binding.tenant_id,),
    ).fetchone()
    revisions = repository._connection.execute(
        "SELECT COUNT(*) FROM catalog_bindings WHERE tenant_id = ? AND binding_id = ?",
        (binding.tenant_id, binding.binding_id),
    ).fetchone()
    resources = repository._connection.execute(
        "SELECT COUNT(*) FROM private_catalog_resources WHERE tenant_id = ?",
        (binding.tenant_id,),
    ).fetchone()
    assert next_sequence is not None
    assert revisions is not None
    assert resources is not None
    return int(next_sequence[0]), int(revisions[0]), int(resources[0])


def test_invalid_creation_state_is_rejected_before_persistence() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    invalid = replace(
        resource_for(binding),
        creation_state=cast(CatalogResourceCreationState, "arbitrary"),
    )
    before = _persistence_counts(repository, binding)

    with pytest.raises(ValueError, match="creation_state"):
        repository.record_resource(invalid)

    assert before == (2, 1, 0)
    assert _persistence_counts(repository, binding) == before


def test_invalid_cleanup_status_is_rejected_before_persistence() -> None:
    repository = SQLiteCatalogRepository(":memory:")
    control = CatalogControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(tenant_id="tenant-a")
    invalid = replace(
        resource_for(binding),
        cleanup_status=cast(CatalogResourceCleanupStatus, "arbitrary"),
    )
    before = _persistence_counts(repository, binding)

    with pytest.raises(ValueError, match="cleanup_status"):
        repository.record_resource(invalid)

    assert before == (2, 1, 0)
    assert _persistence_counts(repository, binding) == before
