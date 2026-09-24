from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_bi_control import (
    DashboardContract,
    DashboardContractSigner,
    SignedDashboardContract,
    SQLiteDashboardContractRepository,
)
from heinzel_contract_model import ArtifactReference, FreshnessRequirement, canonical_bytes


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest="a" * 64)


def _contract(*, owner: str = "principal:finance-owner") -> DashboardContract:
    dimension = _reference("dimension:region")
    return DashboardContract(
        dashboard_id="dashboard:revenue",
        version=1,
        owner=owner,
        audience=("group:finance",),
        data_product_versions=(_reference("product:orders"),),
        metric_versions=(_reference("metric:revenue"),),
        dimensions=(dimension,),
        filters=(),
        visual_intents=("bar",),
        drill_paths=((dimension,),),
        freshness_requirement=FreshnessRequirement(maximum_age_seconds=3_600),
        access_policy=_reference("access-policy:finance"),
        report_delivery_policy=None,
        acceptance_tests=(),
        lifecycle_state="certified",
    )


def _signed(*, owner: str = "principal:finance-owner") -> SignedDashboardContract:
    return DashboardContractSigner("dashboard-key-1", Ed25519PrivateKey.generate()).sign(
        tenant_id="tenant-a", contract=_contract(owner=owner)
    )


def test_repository_stores_and_exactly_replays_an_immutable_contract() -> None:
    repository = SQLiteDashboardContractRepository(":memory:")
    signed = _signed()

    first = repository.store(signed)
    replay = repository.store(signed)

    assert replay == first
    assert (
        repository.read_exact(tenant_id="tenant-a", dashboard_id="dashboard:revenue", version=1)
        == signed
    )


def test_repository_rejects_a_conflicting_identity_replay() -> None:
    repository = SQLiteDashboardContractRepository(":memory:")
    repository.store(_signed())

    with pytest.raises(ValueError, match="conflicting dashboard contract replay"):
        repository.store(_signed(owner="principal:other-owner"))


def test_repository_lookup_does_not_cross_tenant_boundary() -> None:
    repository = SQLiteDashboardContractRepository(":memory:")
    repository.store(_signed())

    assert (
        repository.read_exact(tenant_id="tenant-b", dashboard_id="dashboard:revenue", version=1)
        is None
    )


def test_repository_survives_reopen(tmp_path: Path) -> None:
    database_path = str(tmp_path / "dashboard-contracts.db")
    signed = _signed()
    SQLiteDashboardContractRepository(database_path).store(signed)

    reopened = SQLiteDashboardContractRepository(database_path)

    assert (
        reopened.read_exact(tenant_id="tenant-a", dashboard_id="dashboard:revenue", version=1)
        == signed
    )


def test_repository_rejects_malformed_persisted_payload() -> None:
    repository = SQLiteDashboardContractRepository(":memory:")
    signed = _signed()
    repository.store(signed)
    repository._connection.execute(
        "UPDATE signed_dashboard_contracts_v1 SET payload = ?",
        (b"not-json",),
    )
    repository._connection.commit()

    with pytest.raises(ValueError, match="payload is invalid"):
        repository.read_exact(tenant_id="tenant-a", dashboard_id="dashboard:revenue", version=1)


def test_repository_rejects_an_index_payload_identity_mismatch() -> None:
    repository = SQLiteDashboardContractRepository(":memory:")
    signed = _signed()
    repository.store(signed)
    changed = signed.model_copy(update={"tenant_id": "tenant-b"})
    repository._connection.execute(
        "UPDATE signed_dashboard_contracts_v1 SET payload = ?",
        (canonical_bytes(changed),),
    )
    repository._connection.commit()

    with pytest.raises(ValueError, match="index does not match"):
        repository.read_exact(tenant_id="tenant-a", dashboard_id="dashboard:revenue", version=1)
