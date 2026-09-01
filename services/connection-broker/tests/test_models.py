from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pillarmesh_connection_broker import (
    PrivateSourceCapability,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def binding() -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id="source-binding-a",
        tenant_id="tenant-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.DRAFT,
        approved_object_refs=("invoice", "order"),
        capability_profile_digest=None,
        source_observation_ref=None,
        credential_revision=1,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_source_binding_is_frozen_strict_versioned_and_canonical() -> None:
    model = binding()

    assert model.schema_version == "1"
    assert tuple(SourceConnectionBinding.model_fields) == (
        "schema_version",
        "binding_id",
        "tenant_id",
        "provider_kind",
        "connection_handle",
        "account_mode",
        "lifecycle_state",
        "approved_object_refs",
        "capability_profile_digest",
        "source_observation_ref",
        "credential_revision",
        "revision",
        "created_at",
        "updated_at",
    )
    with pytest.raises(ValidationError):
        SourceConnectionBinding.model_validate(model.model_dump() | {"private_endpoint": "db"})
    with pytest.raises(ValidationError, match="frozen"):
        model.tenant_id = "tenant-b"  # type: ignore[misc]


@pytest.mark.parametrize(
    "update",
    (
        {"approved_object_refs": ("order", "invoice")},
        {"approved_object_refs": ("order", "order")},
        {"created_at": datetime(2026, 9, 1, 12)},
        {"updated_at": datetime(2026, 9, 1, 12, tzinfo=timezone(timedelta(hours=1)))},
    ),
)
def test_source_binding_rejects_noncanonical_objects_and_timestamps(
    update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SourceConnectionBinding.model_validate(binding().model_dump() | update)


def test_validation_evidence_requires_positive_denial_and_exact_utc() -> None:
    evidence = SourceBindingValidationEvidence(
        evidence_id="source-validation-a",
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        binding_revision=2,
        credential_revision=1,
        provider_kind="postgresql",
        positive_probe_succeeded=True,
        positive_probe_digest="1" * 64,
        denial_probe_succeeded=True,
        denial_probe_digest="2" * 64,
        source_observation_ref="source-observation-a",
        capability_profile_digest="3" * 64,
        observed_at=NOW,
    )

    assert evidence.schema_version == "1"
    for update in (
        {"positive_probe_succeeded": False},
        {"denial_probe_succeeded": False},
        {"observed_at": datetime(2026, 9, 1, 12)},
    ):
        with pytest.raises(ValidationError):
            SourceBindingValidationEvidence.model_validate(evidence.model_dump() | update)


def test_private_capability_hides_endpoint_and_credential_references() -> None:
    capability = PrivateSourceCapability(
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        credential_revision=1,
        endpoint_reference=f"endpoint-ref:{'a' * 64}",
        credential_reference=f"credential-ref:{'b' * 64}",
    )

    assert "endpoint-ref" not in repr(capability)
    assert "credential-ref" not in repr(capability)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("endpoint_reference", "postgresql://user:secret@database.example/source"),
        ("credential_reference", "sk_live_secret_canary"),
    ),
)
def test_private_capability_rejects_raw_endpoint_and_credential_material(
    field: str,
    value: str,
) -> None:
    payload = {
        "tenant_id": "tenant-a",
        "binding_id": "source-binding-a",
        "provider_kind": "postgresql",
        "connection_handle": "connection-handle-a",
        "account_mode": "not_applicable",
        "credential_revision": 1,
        "endpoint_reference": f"endpoint-ref:{'a' * 64}",
        "credential_reference": f"credential-ref:{'b' * 64}",
    }

    with pytest.raises(ValidationError):
        PrivateSourceCapability.model_validate(payload | {field: value})
