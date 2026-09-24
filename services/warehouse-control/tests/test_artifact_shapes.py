"""Pin the exact shapes and closed vocabularies of the warehouse durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field or vocabulary member changes that contract, so it must be a deliberate edit that updates
these tables as well.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseBinding,
    WarehouseFailureClassification,
    WarehousePrincipalClass,
    WarehouseProvider,
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pydantic import BaseModel


@pytest.mark.parametrize(
    ("artifact", "fields"),
    (
        pytest.param(
            WarehouseValidationEvidence,
            (
                "schema_version",
                "evidence_id",
                "tenant_id",
                "binding_id",
                "binding_revision",
                "validation_profile",
                "engine_kind",
                "engine_version",
                "engine_build_digest",
                "engine_image_digest",
                "principal_profile_digest",
                "namespace_grant_matrix_digest",
                "tls_probe_digest",
                "network_isolation_probe_digest",
                "encryption_at_rest_evidence_digest",
                "encryption_at_rest_disposition",
                "positive_probe_digest",
                "denial_probe_digest",
                "ledger_probe_digest",
                "monitoring_probe_digest",
                "capacity_alert_probe_digest",
                "backup_artifact_digest",
                "restore_verification_digest",
                "restore_cleanup_digest",
                "observed_at",
            ),
            id="WarehouseValidationEvidence",
        ),
        pytest.param(
            WarehouseResumeValidationEvidence,
            (
                "schema_version",
                "evidence_id",
                "tenant_id",
                "binding_id",
                "binding_revision",
                "engine_kind",
                "engine_version",
                "engine_build_digest",
                "engine_image_digest",
                "tls_probe_digest",
                "network_isolation_probe_digest",
                "monitoring_probe_digest",
                "positive_probe_digest",
                "denial_probe_digest",
                "storage_integrity_probe_digest",
                "observed_at",
            ),
            id="WarehouseResumeValidationEvidence",
        ),
        pytest.param(
            WarehouseRestoreVerification,
            (
                "schema_version",
                "verification_id",
                "tenant_id",
                "binding_id",
                "binding_revision",
                "engine_kind",
                "source_backup_artifact_digest",
                "representative_data_digest",
                "schema_metadata_digest",
                "principal_profile_digest",
                "integrity_marker_digest",
                "query_behavior_digest",
                "verified_at",
            ),
            id="WarehouseRestoreVerification",
        ),
        pytest.param(
            WarehouseRetirementEvidence,
            (
                "schema_version",
                "evidence_id",
                "tenant_id",
                "binding_id",
                "binding_revision",
                "resource_inventory_digest",
                "cleanup_disposition_digest",
                "retention_policy_digest",
                "completed_resource_count",
                "retained_resource_count",
                "cleanup_failed_resource_count",
                "observed_at",
            ),
            id="WarehouseRetirementEvidence",
        ),
        pytest.param(
            WarehouseBinding,
            (
                "schema_version",
                "binding_id",
                "tenant_id",
                "engine_kind",
                "deployment_mode",
                "region",
                "capacity_profile",
                "capability_profile_digest",
                "lifecycle_state",
                "revision",
                "created_at",
                "updated_at",
                "provisioned_at",
            ),
            id="WarehouseBinding",
        ),
    ),
)
def test_durable_artifact_fields_are_exact_and_ordered(
    artifact: type[BaseModel], fields: tuple[str, ...]
) -> None:
    assert tuple(artifact.model_fields) == fields


@pytest.mark.parametrize(
    ("vocabulary", "members"),
    (
        pytest.param(EngineKind, ("postgresql", "clickhouse"), id="EngineKind"),
        pytest.param(
            WarehouseValidationProfile,
            ("local_acceptance", "production"),
            id="WarehouseValidationProfile",
        ),
        pytest.param(
            EncryptionAtRestDisposition,
            ("proven", "deferred_local_acceptance"),
            id="EncryptionAtRestDisposition",
        ),
        pytest.param(
            WarehousePrincipalClass,
            (
                "administration",
                "ingestion_runtime",
                "transformation_runtime",
                "answer_runtime",
                "backup_restore",
                "customer_sql",
                "catalog",
                "bi",
            ),
            id="WarehousePrincipalClass",
        ),
        pytest.param(
            WarehouseFailureClassification,
            (
                "transient_transport",
                "transient_unavailable",
                "throttled",
                "ambiguous_outcome",
                "authorization_denied",
                "statement_rejected",
                "invalid_provider_response",
                "integrity_failure",
                "permanent_configuration",
            ),
            id="WarehouseFailureClassification",
        ),
    ),
)
def test_closed_warehouse_vocabularies_are_exact_and_ordered(
    vocabulary: type[StrEnum], members: tuple[str, ...]
) -> None:
    assert tuple(member.value for member in vocabulary) == members


@pytest.mark.parametrize(
    ("artifact", "field_name", "vocabulary"),
    (
        (WarehouseBinding, "engine_kind", EngineKind),
        (WarehouseValidationEvidence, "validation_profile", WarehouseValidationProfile),
        (WarehouseValidationEvidence, "engine_kind", EngineKind),
        (
            WarehouseValidationEvidence,
            "encryption_at_rest_disposition",
            EncryptionAtRestDisposition,
        ),
        (WarehouseResumeValidationEvidence, "engine_kind", EngineKind),
        (WarehouseRestoreVerification, "engine_kind", EngineKind),
    ),
)
def test_evidence_fields_are_typed_by_their_closed_vocabulary(
    artifact: type[BaseModel], field_name: str, vocabulary: type[StrEnum]
) -> None:
    assert artifact.model_fields[field_name].annotation is vocabulary


@pytest.mark.parametrize(
    "artifact",
    (
        WarehouseBinding,
        WarehouseValidationEvidence,
        WarehouseResumeValidationEvidence,
        WarehouseRestoreVerification,
        WarehouseRetirementEvidence,
    ),
)
def test_warehouse_artifacts_default_to_schema_version_one(artifact: type[BaseModel]) -> None:
    assert artifact.model_fields["schema_version"].default == "1"


@pytest.mark.parametrize(
    "artifact", (WarehouseValidationEvidence, WarehouseResumeValidationEvidence)
)
def test_engine_version_is_a_bounded_release_token(artifact: type[BaseModel]) -> None:
    metadata = artifact.model_fields["engine_version"].metadata

    assert [getattr(item, "pattern", None) for item in metadata if hasattr(item, "pattern")] == [
        r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z]{1,32})?$"
    ]
    assert [
        getattr(item, "max_length", None) for item in metadata if hasattr(item, "max_length")
    ] == [64]


def test_warehouse_provider_operations_are_exact_and_ordered() -> None:
    assert tuple(
        name
        for name, member in vars(WarehouseProvider).items()
        if not name.startswith("_") and callable(member)
    ) == ("provision", "reconcile", "validate", "suspend", "resume", "retire")
