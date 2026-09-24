from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from heinzel_access_control import (
    ConnectedAuthorityProvenance,
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
    CurrentEntitlementResolver,
    CurrentEntitlementSnapshot,
    EnterpriseEntitlementAssertion,
    EntitlementFilterDomain,
    EntitlementObservationConflict,
    EntitlementObservationRollback,
    EntitlementResolutionDenied,
    EntitlementSnapshotIntegrityError,
    SQLiteEntitlementRepository,
)
from heinzel_contract_model import ArtifactReference

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)


def _reference(identifier: str, *, digest_character: str = "a") -> ArtifactReference:
    return ArtifactReference(
        artifact_id=identifier,
        version=1,
        digest=digest_character * 64,
    )


def _assertion(
    *,
    tenant_id: str = "tenant-a",
    principal_ref: str = "principal:requester-a",
    source_revision: int = 1,
    decision: str = "active",
    effective_at: datetime = NOW - timedelta(minutes=5),
    valid_until: datetime = NOW + timedelta(hours=1),
    permissions: tuple[str, ...] = ("query", "view", "download"),
    source_payload_digest: str = "1" * 64,
    connected_authority_ref: str = "policy-authority:tenant-a",
) -> EnterpriseEntitlementAssertion:
    active = decision == "active"
    return EnterpriseEntitlementAssertion.model_validate(
        {
            "tenant_id": tenant_id,
            "principal_ref": principal_ref,
            "purpose_digest": "2" * 64,
            "decision": decision,
            "product_version_refs": (_reference("product:orders"),) if active else (),
            "semantic_refs": (_reference("metric:revenue", digest_character="b"),)
            if active
            else (),
            "filter_domains": (
                EntitlementFilterDomain(
                    dimension_ref=_reference("dimension:region", digest_character="c"),
                    values=("us", "ca"),
                ),
            )
            if active
            else (),
            "permissions": permissions if active else (),
            "effective_at": effective_at,
            "valid_until": valid_until,
            "provenance": ConnectedAuthorityProvenance(
                connected_authority_ref=connected_authority_ref,
                connection_binding_ref="connection:policy-authority-a",
                source_revision=source_revision,
                source_payload_digest=source_payload_digest,
                authentication_method="signed_response",
                authentication_key_ref="key:policy-authority-a:2026-09",
                authentication_evidence_digest="3" * 64,
                adapter_ref="provider:connected-policy:v1",
            ),
        }
    )


class _Authority:
    def __init__(self, *results: EnterpriseEntitlementAssertion | Exception | None) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, str, str]] = []

    def read_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> EnterpriseEntitlementAssertion | None:
        self.calls.append((tenant_id, principal_ref, purpose_digest))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _resolver(
    repository: SQLiteEntitlementRepository,
    authority: _Authority,
    *,
    clock: datetime = NOW,
) -> CurrentEntitlementResolver:
    return CurrentEntitlementResolver(
        repository=repository,
        connected_authority=authority,
        connected_authority_ref="policy-authority:tenant-a",
        clock=lambda: clock,
    )


def _resolve(resolver: CurrentEntitlementResolver) -> CurrentEntitlementSnapshot:
    return resolver.resolve_current(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        purpose_digest="2" * 64,
    )


def test_exact_replay_preserves_authenticated_provenance_and_semantic_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entitlements.sqlite3"
    assertion = _assertion()
    authority = _Authority(assertion)
    repository = SQLiteEntitlementRepository.open(path)
    first = _resolve(_resolver(repository, authority))
    repository.close()

    reopened = SQLiteEntitlementRepository.open(path)
    replay = _resolve(_resolver(reopened, _Authority(assertion), clock=NOW + timedelta(minutes=10)))

    assert replay == first
    assert authority.calls == [("tenant-a", "principal:requester-a", "2" * 64)]
    assert assertion.assertion_digest() == (
        "3c3f428f0ecd1b1b78d03076a4bf20107d2ee1860b0c50aae445073e59dfe900"
    )
    assert (
        replay.snapshot_digest == "41ca1105643e0e3244b7c25937129daa1a5fd02a1d7d96cca29d4aeaa738e9f2"
    )
    assert (
        replay.snapshot_digest
        == replay.model_copy(
            update={
                "snapshot_id": "snapshot:different",
                "resolved_at": NOW + timedelta(minutes=10),
            }
        ).semantic_digest()
    )
    observations = reopened.list_observations(
        tenant_id="tenant-a",
        connected_authority_ref="policy-authority:tenant-a",
        principal_ref="principal:requester-a",
        purpose_digest="2" * 64,
    )
    assert len(observations) == 1
    assert observations[0].provenance == assertion.provenance
    assert observations[0].assertion_digest() == assertion.assertion_digest()
    reopened.close()


def test_same_source_revision_with_changed_payload_is_rejected(tmp_path: Path) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")
    first = _assertion()
    changed = _assertion(
        permissions=("view",),
        source_payload_digest="4" * 64,
    )
    _resolve(_resolver(repository, _Authority(first)))

    with pytest.raises(EntitlementObservationConflict, match="source revision equivocation"):
        _resolve(_resolver(repository, _Authority(changed)))


def test_lower_source_revision_is_rejected_as_rollback(tmp_path: Path) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")
    newest = _assertion(source_revision=2, source_payload_digest="4" * 64)
    stale = _assertion(source_revision=1)
    _resolve(_resolver(repository, _Authority(newest)))

    with pytest.raises(EntitlementObservationRollback, match="source revision rollback"):
        _resolve(_resolver(repository, _Authority(stale)))


@pytest.mark.parametrize(
    ("assertion", "reason"),
    [
        (_assertion(decision="revoked", source_revision=2), "entitlement_revoked"),
        (_assertion(valid_until=NOW), "entitlement_expired"),
    ],
)
def test_revoked_or_expired_authority_fails_closed_and_is_persisted(
    tmp_path: Path,
    assertion: EnterpriseEntitlementAssertion,
    reason: str,
) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")

    with pytest.raises(EntitlementResolutionDenied) as error:
        _resolve(_resolver(repository, _Authority(assertion)))

    assert error.value.reason_code == reason
    assert (
        repository.load_latest_observation(
            tenant_id="tenant-a",
            connected_authority_ref="policy-authority:tenant-a",
            principal_ref="principal:requester-a",
            purpose_digest="2" * 64,
        ).decision
        == assertion.decision
    )


@pytest.mark.parametrize(
    "foreign",
    [
        _assertion(tenant_id="tenant-b"),
        _assertion(principal_ref="principal:requester-b"),
        _assertion(connected_authority_ref="policy-authority:tenant-b"),
    ],
)
def test_cross_tenant_or_principal_authority_is_denied_without_persistence(
    tmp_path: Path,
    foreign: EnterpriseEntitlementAssertion,
) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")

    with pytest.raises(EntitlementResolutionDenied) as error:
        _resolve(_resolver(repository, _Authority(foreign)))

    assert error.value.reason_code == "authority_scope_mismatch"
    assert repository.count_observations() == 0


@pytest.mark.parametrize(
    ("authority_result", "reason"),
    [
        (None, "authority_missing"),
        (ConnectedPolicyAuthorityUnavailable("offline"), "authority_unavailable"),
    ],
)
def test_missing_or_unavailable_connected_authority_never_uses_stored_authority(
    tmp_path: Path,
    authority_result: EnterpriseEntitlementAssertion | Exception | None,
    reason: str,
) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")
    _resolve(_resolver(repository, _Authority(_assertion())))

    with pytest.raises(EntitlementResolutionDenied) as error:
        _resolve(_resolver(repository, _Authority(authority_result)))

    assert error.value.reason_code == reason
    assert repository.count_observations() == 1


def test_invalid_authenticated_response_is_denied_without_persistence(tmp_path: Path) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")

    with pytest.raises(EntitlementResolutionDenied) as error:
        _resolve(
            _resolver(
                repository,
                _Authority(ConnectedPolicyAuthorityIntegrityError("invalid signature")),
            )
        )

    assert error.value.reason_code == "authority_invalid"
    assert repository.count_observations() == 0


def test_entitlement_is_current_at_effective_time_and_denied_before_it(tmp_path: Path) -> None:
    repository = SQLiteEntitlementRepository.open(tmp_path / "entitlements.sqlite3")
    effective_now = _assertion(effective_at=NOW)

    assert _resolve(_resolver(repository, _Authority(effective_now))).effective_at == NOW

    future = _assertion(
        source_revision=2,
        source_payload_digest="4" * 64,
        effective_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(EntitlementResolutionDenied) as error:
        _resolve(_resolver(repository, _Authority(future)))

    assert error.value.reason_code == "entitlement_not_yet_effective"


class _RollbackFailingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)

    def rollback(self) -> None:
        raise sqlite3.OperationalError("rollback failed")


def test_rollback_cleanup_never_replaces_revision_conflict(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "entitlements.sqlite3")
    repository = SQLiteEntitlementRepository(connection)
    repository.record_observation(_assertion(), recorded_at=NOW)
    repository_with_failing_cleanup = SQLiteEntitlementRepository(
        cast(sqlite3.Connection, _RollbackFailingConnection(connection))
    )

    with pytest.raises(EntitlementObservationConflict, match="source revision equivocation"):
        repository_with_failing_cleanup.record_observation(
            _assertion(permissions=("view",), source_payload_digest="4" * 64),
            recorded_at=NOW,
        )


def test_snapshot_replay_rejects_corrupt_scope_index(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "entitlements.sqlite3")
    repository = SQLiteEntitlementRepository(connection)
    observation = repository.record_observation(_assertion(), recorded_at=NOW)
    repository.record_snapshot(observation, resolved_at=NOW)
    connection.execute(
        "UPDATE entitlement_snapshots SET tenant_id = ?",
        ("tenant-b",),
    )
    connection.commit()

    with pytest.raises(EntitlementSnapshotIntegrityError, match="snapshot index"):
        repository.record_snapshot(observation, resolved_at=NOW + timedelta(minutes=1))
