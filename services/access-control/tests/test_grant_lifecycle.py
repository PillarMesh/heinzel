from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_access_control import (
    AccessEffectReceipt,
    AccessGrant,
    AccessGrantAuthorizationService,
    AccessGrantConflict,
    AccessGrantDenied,
    AccessGrantIntegrityError,
    CurrentEntitlementSnapshot,
    SQLiteAccessGrantRepository,
)
from pillarmesh_contract_model import ArtifactReference, canonical_bytes, digest

NOW = datetime(2026, 9, 12, 21, tzinfo=UTC)
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=1, digest="a" * 64)
ADMISSION = ArtifactReference(artifact_id="admission:access-1", version=1, digest="b" * 64)
PURPOSE = "Review regional revenue"


def _entitlement(**changes: object) -> CurrentEntitlementSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "entitlement-snapshot-1",
        "snapshot_digest": "pending",
        "tenant_id": "tenant-a",
        "principal_ref": "principal:requester-a",
        "purpose_digest": digest(PURPOSE),
        "connected_authority_ref": "authority:policy-a",
        "source_revision": 4,
        "source_payload_digest": "c" * 64,
        "observation_id": "observation-4",
        "product_version_refs": (PRODUCT,),
        "semantic_refs": (PRODUCT,),
        "filter_domains": (),
        "permissions": ("dashboard", "download", "query", "view"),
        "effective_at": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1),
        "resolved_at": NOW,
    }
    values.update(changes)
    values["snapshot_digest"] = digest(
        {
            "schema_version": "1",
            "tenant_id": values["tenant_id"],
            "principal_ref": values["principal_ref"],
            "purpose_digest": values["purpose_digest"],
            "connected_authority_ref": values["connected_authority_ref"],
            "source_revision": values["source_revision"],
            "source_payload_digest": values["source_payload_digest"],
            "product_version_refs": values["product_version_refs"],
            "semantic_refs": values["semantic_refs"],
            "filter_domains": values["filter_domains"],
            "permissions": values["permissions"],
            "effective_at": values["effective_at"],
            "valid_until": values["valid_until"],
        }
    )
    return CurrentEntitlementSnapshot.model_validate(values)


def _grant(**changes: object) -> AccessGrant:
    values: dict[str, object] = {
        "grant_id": "grant-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "revision": 1,
        "state": "active",
        "principal_ref": "principal:requester-a",
        "purpose": PURPOSE,
        "purpose_digest": digest(PURPOSE),
        "data_product_version_ref": PRODUCT,
        "fields": ("region", "revenue"),
        "classification_refs": (),
        "access_mode": "dashboard",
        "permissions": ("dashboard", "download", "view"),
        "effective_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=30),
        "policy_revision": 3,
        "admission_receipt_ref": ADMISSION,
        "entitlement_snapshot_digest": _entitlement().snapshot_digest,
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW - timedelta(minutes=5),
    }
    values.update(changes)
    return AccessGrant.model_validate(values)


class _Entitlements:
    def __init__(self, snapshot: CurrentEntitlementSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot:
        self.calls += 1
        assert (tenant_id, principal_ref, purpose_digest) == (
            "tenant-a",
            "principal:requester-a",
            digest(PURPOSE),
        )
        return self.snapshot


def _repository() -> SQLiteAccessGrantRepository:
    return SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))


def test_active_grant_rechecks_current_entitlement_for_each_disclosure() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)
    entitlements = _Entitlements(_entitlement())
    service = AccessGrantAuthorizationService(
        grants=repository, entitlements=entitlements, clock=lambda: NOW
    )

    first = service.authorize(
        tenant_id="tenant-a",
        grant_id="grant-1",
        principal_ref="principal:requester-a",
        purpose=PURPOSE,
        permission="dashboard",
        product_version_ref=PRODUCT,
    )
    second = service.authorize(
        tenant_id="tenant-a",
        grant_id="grant-1",
        principal_ref="principal:requester-a",
        purpose=PURPOSE,
        permission="view",
        product_version_ref=PRODUCT,
    )

    assert first == second == _grant()
    assert entitlements.calls == 2


@pytest.mark.parametrize(
    ("grant", "reason"),
    (
        (_grant(state="revocation_pending"), "not active"),
        (_grant(expires_at=NOW), "expired"),
        (_grant(effective_at=NOW + timedelta(seconds=1)), "not effective"),
    ),
)
def test_revocation_or_time_boundary_denies_before_provider_cleanup(
    grant: AccessGrant, reason: str
) -> None:
    repository = _repository()
    repository.append(grant, expected_current_revision=0)
    service = AccessGrantAuthorizationService(
        grants=repository, entitlements=_Entitlements(_entitlement()), clock=lambda: NOW
    )

    with pytest.raises(AccessGrantDenied, match=reason):
        service.authorize(
            tenant_id="tenant-a",
            grant_id=grant.grant_id,
            principal_ref=grant.principal_ref,
            purpose=grant.purpose,
            permission="view",
            product_version_ref=PRODUCT,
        )


def test_changed_entitlement_digest_denies_stale_grant() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)
    current = _entitlement(source_revision=5, source_payload_digest="d" * 64)
    service = AccessGrantAuthorizationService(
        grants=repository, entitlements=_Entitlements(current), clock=lambda: NOW
    )

    with pytest.raises(AccessGrantDenied, match="entitlement changed"):
        service.authorize(
            tenant_id="tenant-a",
            grant_id="grant-1",
            principal_ref="principal:requester-a",
            purpose=PURPOSE,
            permission="view",
            product_version_ref=PRODUCT,
        )


def test_append_is_exact_replay_and_rejects_conflicting_revision() -> None:
    repository = _repository()
    original = _grant()

    assert repository.append(original, expected_current_revision=0) == original
    assert repository.append(original, expected_current_revision=0) == original

    with pytest.raises(AccessGrantConflict, match="conflicts"):
        repository.append(
            original.model_copy(update={"permissions": ("view",)}),
            expected_current_revision=0,
        )


def test_cross_tenant_lookup_is_non_enumerating() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)

    with pytest.raises(AccessGrantDenied, match="unavailable"):
        AccessGrantAuthorizationService(
            grants=repository, entitlements=_Entitlements(_entitlement()), clock=lambda: NOW
        ).authorize(
            tenant_id="tenant-b",
            grant_id="grant-1",
            principal_ref="principal:requester-a",
            purpose=PURPOSE,
            permission="view",
            product_version_ref=PRODUCT,
        )


def test_current_grant_for_request_is_tenant_scoped() -> None:
    repository = _repository()
    grant = _grant()
    repository.append(grant, expected_current_revision=0)

    assert repository.load_current_for_request("tenant-a", "request-1") == grant
    assert repository.load_current_for_request("tenant-b", "request-1") is None
    assert repository.load_current_for_request("tenant-a", "request-missing") is None


def test_multiple_current_grants_for_one_request_fail_closed() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)
    repository.append(_grant(grant_id="grant-2"), expected_current_revision=0)

    with pytest.raises(AccessGrantIntegrityError, match="multiple current access grants"):
        repository.load_current_for_request("tenant-a", "request-1")


def test_corrupt_current_index_payload_fails_closed() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteAccessGrantRepository(connection)
    repository.append(_grant(), expected_current_revision=0)
    foreign_payload = canonical_bytes(_grant(tenant_id="tenant-b"))
    connection.execute(
        "UPDATE access_grant_revisions SET payload = ? WHERE tenant_id = ? AND grant_id = ?",
        (foreign_payload, "tenant-a", "grant-1"),
    )

    with pytest.raises(AccessGrantIntegrityError, match="index"):
        repository.load_current("tenant-a", "grant-1")


def _effect(**changes: object) -> AccessEffectReceipt:
    values: dict[str, object] = {
        "effect_id": "effect-warehouse-1",
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "surface": "warehouse",
        "action": "apply",
        "attempt": 1,
        "outcome": "transient_failure",
        "provider_receipt_digest": "e" * 64,
        "recorded_at": NOW,
    }
    values.update(changes)
    return AccessEffectReceipt.model_validate(values)


def test_partial_provider_effects_resume_only_missing_surfaces() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)
    warehouse_failure = _effect()
    result_success = _effect(
        effect_id="effect-result-1",
        surface="result",
        outcome="succeeded",
    )

    assert repository.record_effect(warehouse_failure) == warehouse_failure
    assert repository.record_effect(result_success) == result_success
    assert repository.successful_effect_surfaces("tenant-a", "grant-1", 1, action="apply") == (
        "result",
    )

    warehouse_success = _effect(
        effect_id="effect-warehouse-2",
        attempt=2,
        outcome="succeeded",
    )
    repository.record_effect(warehouse_success)

    assert repository.successful_effect_surfaces("tenant-a", "grant-1", 1, action="apply") == (
        "result",
        "warehouse",
    )


def test_provider_effect_receipt_is_exact_replay() -> None:
    repository = _repository()
    repository.append(_grant(), expected_current_revision=0)
    receipt = _effect()

    assert repository.record_effect(receipt) == receipt
    assert repository.record_effect(receipt) == receipt

    with pytest.raises(AccessGrantConflict, match="effect receipt"):
        repository.record_effect(receipt.model_copy(update={"outcome": "permanent_failure"}))
