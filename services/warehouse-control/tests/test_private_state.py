from datetime import UTC, datetime, timedelta, timezone

import pytest
from heinzel_warehouse_control import (
    EngineKind,
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
from pydantic import ValidationError

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
RETENTION_DEADLINE = NOW + timedelta(days=30)


def operation_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 2,
        "operation_id": "wop-test",
        "operation_kind": WarehouseOperationKind.PROVISION,
        "engine_kind": EngineKind.POSTGRESQL,
        "status": WarehouseOperationStatus.CLAIMED,
        "phase": WarehouseOperationPhase.CLAIMED,
        "provider_resource_handle": None,
        "failure_classification": None,
        "started_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return values


def resource_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 2,
        "operation_id": "wop-test",
        "resource_id": "whr-test",
        "resource_kind": WarehouseResourceKind.COMPOSE_PROJECT,
        "provider_resource_handle": "compose-project-test",
        "parent_resource_handle": None,
        "creation_state": WarehouseResourceCreationState.PLANNED,
        "retention_deadline": RETENTION_DEADLINE,
        "cleanup_status": WarehouseResourceCleanupStatus.PENDING,
        "cleanup_failure_classification": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return values


def test_private_operation_is_frozen_and_rejects_unknown_fields() -> None:
    operation = PrivateWarehouseOperation.model_validate(operation_payload())

    with pytest.raises(ValidationError, match="frozen"):
        operation.status = WarehouseOperationStatus.RUNNING

    with pytest.raises(ValidationError, match="extra_forbidden"):
        PrivateWarehouseOperation.model_validate(operation_payload(secret="must-not-persist"))


@pytest.mark.parametrize(
    "field",
    ("tenant_id", "binding_id", "operation_id"),
)
@pytest.mark.parametrize("value", ("", "   "))
def test_private_operation_rejects_empty_identity(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        PrivateWarehouseOperation.model_validate(operation_payload(**{field: value}))


@pytest.mark.parametrize("binding_revision", (0, -1))
def test_private_operation_requires_an_owned_binding_revision(binding_revision: int) -> None:
    with pytest.raises(ValidationError, match="binding_revision"):
        PrivateWarehouseOperation.model_validate(
            operation_payload(binding_revision=binding_revision)
        )


@pytest.mark.parametrize(
    ("field", "timestamp"),
    (
        ("started_at", datetime(2026, 8, 24, 12)),
        ("updated_at", datetime(2026, 8, 24, 12)),
        ("started_at", datetime(2026, 8, 24, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("updated_at", datetime(2026, 8, 24, 12, tzinfo=timezone(timedelta(hours=1)))),
    ),
)
def test_private_operation_rejects_non_utc_timestamps(field: str, timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        PrivateWarehouseOperation.model_validate(operation_payload(**{field: timestamp}))


def test_private_operation_rejects_an_update_before_it_started() -> None:
    with pytest.raises(ValidationError, match="updated_at"):
        PrivateWarehouseOperation.model_validate(
            operation_payload(updated_at=NOW - timedelta(seconds=1))
        )


def test_failed_private_operation_requires_a_failure_classification() -> None:
    with pytest.raises(ValidationError, match="failure classification"):
        PrivateWarehouseOperation.model_validate(
            operation_payload(status=WarehouseOperationStatus.FAILED)
        )


@pytest.mark.parametrize(
    "status",
    (
        WarehouseOperationStatus.CLAIMED,
        WarehouseOperationStatus.RUNNING,
        WarehouseOperationStatus.RECONCILING,
        WarehouseOperationStatus.SUCCEEDED,
    ),
)
def test_non_failed_private_operation_rejects_a_failure_classification(
    status: WarehouseOperationStatus,
) -> None:
    with pytest.raises(ValidationError, match="failure classification"):
        PrivateWarehouseOperation.model_validate(
            operation_payload(
                status=status,
                failure_classification=(WarehouseFailureClassification.PERMANENT_CONFIGURATION),
            )
        )


def test_resource_kind_is_the_exact_closed_vocabulary() -> None:
    assert tuple(kind.value for kind in WarehouseResourceKind) == (
        "compose_project",
        "warehouse_container",
        "private_network",
        "warehouse_data_volume",
        "private_directory",
        "credential_file",
        "host_port_file",
        "tls_private_key",
        "tls_certificate",
        "backup_artifact",
        "backup_staging_file",
        "backup_encryption_key",
        "backup_retirement_journal",
        "restore_compose_project",
        "restore_container",
        "restore_private_network",
        "restore_data_volume",
        "restore_verification_receipt",
    )

    with pytest.raises(ValidationError, match="resource_kind"):
        PrivateWarehouseResource.model_validate(resource_payload(resource_kind="nearby_volume"))
    with pytest.raises(ValidationError, match="resource_kind"):
        PrivateWarehouseResource.model_validate(resource_payload(resource_kind="hba_configuration"))


@pytest.mark.parametrize(
    "field",
    ("tenant_id", "binding_id", "operation_id", "resource_id", "provider_resource_handle"),
)
@pytest.mark.parametrize("value", ("", "   "))
def test_private_resource_rejects_empty_identity_or_handle(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        PrivateWarehouseResource.model_validate(resource_payload(**{field: value}))


def test_private_resource_rejects_empty_optional_parent_handle() -> None:
    with pytest.raises(ValidationError, match="parent_resource_handle"):
        PrivateWarehouseResource.model_validate(resource_payload(parent_resource_handle=" "))


def test_private_resource_cas_revision_is_durable_but_not_semantic_state() -> None:
    resource = PrivateWarehouseResource.model_validate(resource_payload())
    advanced = resource.model_copy(update={"state_revision": 1})

    assert advanced == resource
    assert hash(advanced) == hash(resource)
    assert advanced.model_dump()["state_revision"] == 1


@pytest.mark.parametrize(
    "timestamp",
    (
        datetime(2026, 9, 23, 12),
        datetime(2026, 9, 23, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_private_resource_rejects_non_utc_retention_deadline(timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        PrivateWarehouseResource.model_validate(resource_payload(retention_deadline=timestamp))


def test_private_resource_rejects_a_retention_deadline_before_creation() -> None:
    with pytest.raises(ValidationError, match="retention_deadline"):
        PrivateWarehouseResource.model_validate(
            resource_payload(retention_deadline=NOW - timedelta(seconds=1))
        )


def test_failed_resource_cleanup_requires_a_failure_classification() -> None:
    with pytest.raises(ValidationError, match="failure classification"):
        PrivateWarehouseResource.model_validate(
            resource_payload(cleanup_status=WarehouseResourceCleanupStatus.FAILED)
        )


def test_non_failed_resource_cleanup_rejects_a_failure_classification() -> None:
    with pytest.raises(ValidationError, match="failure classification"):
        PrivateWarehouseResource.model_validate(
            resource_payload(
                cleanup_status=WarehouseResourceCleanupStatus.COMPLETE,
                cleanup_failure_classification=WarehouseFailureClassification.INTEGRITY_FAILURE,
            )
        )
