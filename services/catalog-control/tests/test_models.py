from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import heinzel_catalog_control as catalog_control
import pytest
from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogValidationEvidence,
)
from heinzel_contract_model import canonical_bytes, digest
from pydantic import ValidationError

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def binding_payload() -> dict[str, object]:
    return {
        "binding_id": "cat-test",
        "tenant_id": "tenant-a",
        "capability_profile_digest": "a" * 64,
        "lifecycle_state": CatalogBindingState.DRAFT,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def evidence_payload() -> dict[str, object]:
    return {
        "evidence_id": "cve-test",
        "tenant_id": "tenant-a",
        "binding_id": "cat-test",
        "binding_revision": 3,
        "provider_version": "1.13.3",
        "provider_build_digest": "f" * 64,
        "provider_image_set_digest": "0" * 64,
        "positive_probe_digest": "b" * 64,
        "denial_probe_digest": "c" * 64,
        "stable_identity_probe_digest": "d" * 64,
        "backup_probe_digest": "e" * 64,
        "observed_at": NOW,
    }


def test_catalog_binding_rejects_unknown_fields_and_non_utc_time() -> None:
    with pytest.raises(ValidationError):
        CatalogBinding.model_validate(binding_payload() | {"endpoint": "http://secret"})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        CatalogBinding.model_validate(binding_payload() | {"created_at": datetime.now()})


@pytest.mark.parametrize(
    ("field", "timestamp"),
    (
        ("created_at", datetime(2026, 8, 19, 12)),
        ("updated_at", datetime(2026, 8, 19, 12)),
        ("provisioned_at", datetime(2026, 8, 19, 12)),
        ("created_at", datetime(2026, 8, 19, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("updated_at", datetime(2026, 8, 19, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("provisioned_at", datetime(2026, 8, 19, 12, tzinfo=timezone(timedelta(hours=1)))),
    ),
)
def test_catalog_binding_rejects_every_non_utc_durable_timestamp(
    field: str, timestamp: datetime
) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        CatalogBinding.model_validate(binding_payload() | {field: timestamp})


def test_validation_evidence_is_strict_private_identifier_free_and_utc() -> None:
    CatalogValidationEvidence.model_validate(evidence_payload())

    assert tuple(CatalogValidationEvidence.model_fields) == (
        "evidence_id",
        "tenant_id",
        "binding_id",
        "binding_revision",
        "provider_version",
        "provider_build_digest",
        "provider_image_set_digest",
        "positive_probe_digest",
        "denial_probe_digest",
        "stable_identity_probe_digest",
        "backup_probe_digest",
        "observed_at",
    )
    with pytest.raises(ValidationError):
        CatalogValidationEvidence.model_validate(
            evidence_payload() | {"provider_object_id": "private-id"}
        )
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        CatalogValidationEvidence.model_validate(
            evidence_payload() | {"observed_at": datetime(2026, 8, 19, 12)}
        )


def test_catalog_binding_uses_canonical_digest_compatible_serialization() -> None:
    binding = CatalogBinding.model_validate(binding_payload())
    payload = canonical_bytes(binding)

    assert CatalogBinding.model_validate_json(payload) == binding
    assert hashlib.sha256(payload).hexdigest() == digest(binding)


def test_private_catalog_resource_is_not_exported_from_package_root() -> None:
    assert "PrivateCatalogResource" not in catalog_control.__all__
    assert not hasattr(catalog_control, "PrivateCatalogResource")


def test_catalog_binding_fields_are_exact_and_ordered() -> None:
    assert tuple(CatalogBinding.model_fields) == (
        "schema_version",
        "binding_id",
        "tenant_id",
        "provider_kind",
        "deployment_mode",
        "capability_profile_digest",
        "lifecycle_state",
        "revision",
        "created_at",
        "updated_at",
        "provisioned_at",
    )
