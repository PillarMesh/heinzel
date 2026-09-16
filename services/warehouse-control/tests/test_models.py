from datetime import UTC, datetime, timedelta, timezone

import pytest
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehousePrincipalClass,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pydantic import BaseModel, ValidationError


def test_answer_runtime_is_a_distinct_warehouse_principal_class() -> None:
    assert WarehousePrincipalClass.ANSWER_RUNTIME.value == "answer_runtime"


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def binding(**updates: object) -> WarehouseBinding:
    values: dict[str, object] = {
        "binding_id": "whb-test",
        "tenant_id": "tenant-a",
        "engine_kind": EngineKind.POSTGRESQL,
        "region": "us-west",
        "capacity_profile": "mvp-fixed",
        "capability_profile_digest": "a" * 64,
        "lifecycle_state": WarehouseBindingState.DRAFT,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return WarehouseBinding.model_validate(values)


def validation_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "evidence_id": "wev-test",
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 2,
        "validation_profile": WarehouseValidationProfile.PRODUCTION,
        "engine_kind": EngineKind.POSTGRESQL,
        "engine_version": "17.5",
        "engine_build_digest": "a" * 64,
        "engine_image_digest": "b" * 64,
        "principal_profile_digest": "c" * 64,
        "namespace_grant_matrix_digest": "d" * 64,
        "tls_probe_digest": "e" * 64,
        "network_isolation_probe_digest": "f" * 64,
        "encryption_at_rest_evidence_digest": "0" * 64,
        "encryption_at_rest_disposition": EncryptionAtRestDisposition.PROVEN,
        "positive_probe_digest": "1" * 64,
        "denial_probe_digest": "2" * 64,
        "ledger_probe_digest": "3" * 64,
        "monitoring_probe_digest": "4" * 64,
        "capacity_alert_probe_digest": "5" * 64,
        "backup_artifact_digest": "6" * 64,
        "restore_verification_digest": "7" * 64,
        "restore_cleanup_digest": "8" * 64,
        "observed_at": NOW,
    }
    values.update(updates)
    return values


def resume_validation_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "evidence_id": "wrv-test",
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 3,
        "engine_kind": EngineKind.POSTGRESQL,
        "engine_version": "17.5",
        "engine_build_digest": "a" * 64,
        "engine_image_digest": "b" * 64,
        "tls_probe_digest": "c" * 64,
        "network_isolation_probe_digest": "d" * 64,
        "monitoring_probe_digest": "e" * 64,
        "positive_probe_digest": "f" * 64,
        "denial_probe_digest": "0" * 64,
        "storage_integrity_probe_digest": "1" * 64,
        "observed_at": NOW,
    }
    values.update(updates)
    return values


def restore_verification_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "verification_id": "wrv-test",
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 2,
        "engine_kind": EngineKind.POSTGRESQL,
        "source_backup_artifact_digest": "a" * 64,
        "representative_data_digest": "b" * 64,
        "schema_metadata_digest": "c" * 64,
        "principal_profile_digest": "d" * 64,
        "integrity_marker_digest": "e" * 64,
        "query_behavior_digest": "f" * 64,
        "verified_at": NOW,
    }
    values.update(updates)
    return values


def retirement_payload(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "evidence_id": "wre-test",
        "tenant_id": "tenant-a",
        "binding_id": "whb-test",
        "binding_revision": 4,
        "resource_inventory_digest": "a" * 64,
        "cleanup_disposition_digest": "b" * 64,
        "retention_policy_digest": "c" * 64,
        "completed_resource_count": 3,
        "retained_resource_count": 1,
        "cleanup_failed_resource_count": 0,
        "observed_at": NOW,
    }
    values.update(updates)
    return values


@pytest.mark.parametrize(
    ("field", "timestamp"),
    (
        ("created_at", datetime(2026, 8, 17, 12)),
        ("updated_at", datetime(2026, 8, 17, 12)),
        ("provisioned_at", datetime(2026, 8, 17, 12)),
        ("created_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("updated_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("provisioned_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
    ),
)
def test_binding_rejects_non_utc_durable_timestamps(field: str, timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        binding(**{field: timestamp})


def test_production_validation_cannot_defer_storage_encryption() -> None:
    with pytest.raises(ValidationError):
        WarehouseValidationEvidence.model_validate(
            validation_payload(
                encryption_at_rest_disposition="deferred_local_acceptance",
            )
        )


def test_local_validation_cannot_claim_proven_storage_encryption() -> None:
    with pytest.raises(ValidationError):
        WarehouseValidationEvidence.model_validate(
            validation_payload(
                validation_profile="local_acceptance",
            )
        )


@pytest.mark.parametrize(
    ("artifact", "payload", "timestamp_field"),
    (
        (WarehouseValidationEvidence, validation_payload(), "observed_at"),
        (WarehouseResumeValidationEvidence, resume_validation_payload(), "observed_at"),
        (WarehouseRestoreVerification, restore_verification_payload(), "verified_at"),
        (WarehouseRetirementEvidence, retirement_payload(), "observed_at"),
    ),
)
def test_warehouse_evidence_rejects_naive_timestamps(
    artifact: type[BaseModel], payload: dict[str, object], timestamp_field: str
) -> None:
    payload[timestamp_field] = datetime(2026, 8, 17, 12)

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        artifact.model_validate(payload)


def test_validation_evidence_rejects_unknown_fields_and_invalid_digests() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WarehouseValidationEvidence.model_validate(validation_payload(unexpected="value"))

    with pytest.raises(ValidationError, match="engine_build_digest"):
        WarehouseValidationEvidence.model_validate(
            validation_payload(engine_build_digest="invalid")
        )


@pytest.mark.parametrize(
    ("artifact", "payload"),
    (
        (WarehouseValidationEvidence, validation_payload()),
        (WarehouseResumeValidationEvidence, resume_validation_payload()),
    ),
)
def test_warehouse_evidence_rejects_engine_version_banners(
    artifact: type[BaseModel], payload: dict[str, object]
) -> None:
    payload["engine_version"] = "PostgreSQL 17.5 at localhost:5432"

    with pytest.raises(ValidationError, match="engine_version"):
        artifact.model_validate(payload)


@pytest.mark.parametrize(
    ("artifact", "payload"),
    (
        (WarehouseValidationEvidence, validation_payload()),
        (WarehouseResumeValidationEvidence, resume_validation_payload()),
    ),
)
def test_warehouse_evidence_rejects_hostname_shaped_engine_version_suffixes(
    artifact: type[BaseModel], payload: dict[str, object]
) -> None:
    payload["engine_version"] = "17+db-prod.acme.internal"

    with pytest.raises(ValidationError, match="engine_version"):
        artifact.model_validate(payload)


@pytest.mark.parametrize("engine_version", ("18.6", "25.8.32.4", "18.6-lts"))
@pytest.mark.parametrize(
    ("artifact", "payload"),
    (
        (WarehouseValidationEvidence, validation_payload()),
        (WarehouseResumeValidationEvidence, resume_validation_payload()),
    ),
)
def test_warehouse_evidence_accepts_release_engine_version_tokens(
    artifact: type[BaseModel], payload: dict[str, object], engine_version: str
) -> None:
    payload["engine_version"] = engine_version

    assert artifact.model_validate(payload).engine_version == engine_version


@pytest.mark.parametrize(
    ("artifact", "payload"),
    (
        (WarehouseValidationEvidence, validation_payload()),
        (WarehouseResumeValidationEvidence, resume_validation_payload()),
    ),
)
def test_warehouse_evidence_rejects_engine_versions_longer_than_64_characters(
    artifact: type[BaseModel], payload: dict[str, object]
) -> None:
    payload["engine_version"] = "17." + "1" * 62

    with pytest.raises(ValidationError, match="engine_version"):
        artifact.model_validate(payload)


@pytest.mark.parametrize(
    "count_field",
    ("completed_resource_count", "retained_resource_count", "cleanup_failed_resource_count"),
)
def test_retirement_evidence_rejects_negative_resource_counts(count_field: str) -> None:
    with pytest.raises(ValidationError, match=count_field):
        WarehouseRetirementEvidence.model_validate(retirement_payload(**{count_field: -1}))
