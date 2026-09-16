from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_bi_control import (
    DashboardContract,
    DashboardContractSigner,
    DashboardContractVerifier,
    InvalidDashboardContract,
    SignedDashboardContract,
)
from pillarmesh_contract_model import ArtifactReference, FreshnessRequirement, canonical_bytes
from pydantic import ValidationError


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest="a" * 64)


def _contract() -> DashboardContract:
    dimension = _reference("dimension:region")
    return DashboardContract(
        dashboard_id="dashboard:revenue",
        version=1,
        owner="principal:finance-owner",
        audience=("group:finance",),
        data_product_versions=(_reference("product:orders"),),
        metric_versions=(_reference("metric:revenue"),),
        dimensions=(dimension,),
        filters=(_reference("filter:completed-orders"),),
        visual_intents=("bar",),
        drill_paths=((dimension,),),
        freshness_requirement=FreshnessRequirement(maximum_age_seconds=3_600),
        access_policy=_reference("access-policy:finance"),
        report_delivery_policy=None,
        acceptance_tests=(_reference("acceptance:revenue-total"),),
        lifecycle_state="certified",
    )


def test_signer_binds_tenant_and_contract_digest() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = DashboardContractSigner("dashboard-key-1", private_key)
    signed = signer.sign(tenant_id="tenant-a", contract=_contract())

    verified = DashboardContractVerifier({"dashboard-key-1": private_key.public_key()}).verify(
        signed
    )

    assert verified == _contract()
    assert signed.tenant_id == "tenant-a"


def test_signed_envelope_rejects_a_changed_contract_digest() -> None:
    signed = DashboardContractSigner("dashboard-key-1", Ed25519PrivateKey.generate()).sign(
        tenant_id="tenant-a", contract=_contract()
    )

    with pytest.raises(ValidationError, match="digest"):
        SignedDashboardContract.model_validate(
            signed.model_dump(mode="python") | {"contract_digest": "b" * 64}
        )


def test_verifier_rejects_cross_tenant_replay() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = DashboardContractSigner("dashboard-key-1", private_key).sign(
        tenant_id="tenant-a", contract=_contract()
    )
    replayed = signed.model_copy(update={"tenant_id": "tenant-b"})

    with pytest.raises(InvalidDashboardContract, match="signature"):
        DashboardContractVerifier({"dashboard-key-1": private_key.public_key()}).verify(replayed)


def test_verifier_rejects_a_changed_key_identifier_even_when_the_key_is_duplicated() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = DashboardContractSigner("dashboard-key-1", private_key).sign(
        tenant_id="tenant-a", contract=_contract()
    )
    replayed = signed.model_copy(update={"key_id": "dashboard-key-2"})

    with pytest.raises(InvalidDashboardContract, match="signature"):
        DashboardContractVerifier(
            {
                "dashboard-key-1": private_key.public_key(),
                "dashboard-key-2": private_key.public_key(),
            }
        ).verify(replayed)


def test_verifier_rejects_a_signature_from_another_artifact_domain() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = DashboardContractSigner("dashboard-key-1", private_key).sign(
        tenant_id="tenant-a", contract=_contract()
    )
    wrong_signature = base64.b64encode(
        private_key.sign(
            canonical_bytes(
                {
                    "domain": "pillarmesh-governed-query-plan-v1",
                    "tenant_id": signed.tenant_id,
                    "contract_digest": signed.contract_digest,
                }
            )
        )
    ).decode("ascii")

    with pytest.raises(InvalidDashboardContract, match="signature"):
        DashboardContractVerifier({"dashboard-key-1": private_key.public_key()}).verify(
            signed.model_copy(update={"signature": wrong_signature})
        )


@pytest.mark.parametrize("signature", ("", "not-base64!", "AAAA"))
def test_verifier_rejects_malformed_signatures(signature: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = DashboardContractSigner("dashboard-key-1", private_key).sign(
        tenant_id="tenant-a", contract=_contract()
    )

    with pytest.raises(InvalidDashboardContract):
        DashboardContractVerifier({"dashboard-key-1": private_key.public_key()}).verify(
            signed.model_copy(update={"signature": signature})
        )
