"""The certified dashboard contract the demonstration offers its answer to.

A dashboard is published to a contract, and that contract is the authority a publication is
checked against: its product, its metrics, its visual intent and its freshness requirement all have
to match the answer being published. The demonstration had none, so its publishable offering was
empty and the console could only report that there was nothing to publish.

This composes one over the terms the demonstration already publishes -- the approved metric and
dimension of its own product generation -- signs it, and stores it. What it stands in for is the
dashboard designer and the certification that follows them, not the contract: the contract is the
real model, verified by the real verifier before it is ever offered.

The signing key is persisted, unlike the demonstration's other in-process keys. A contract is
committed to SQLite and read back on the next start, so a key generated per run would leave every
stored contract failing verification -- which the publishable offering reports as an integrity
failure rather than an empty list, and rightly.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from heinzel_bi_control import (
    DashboardContract,
    DashboardContractSigner,
    SignedDashboardContract,
    SQLiteDashboardContractRepository,
)
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    FreshnessRequirement,
    digest,
)
from heinzel_request_management import AnswerScopePolicy

from .catalog import demo_dimension_reference, demo_metric_reference
from .collaborators import DEMO_REQUESTER_PRINCIPAL_REF
from .secret_files import create_secret_file, read_secret_file

__all__ = [
    "DASHBOARD_CONTRACT_KEY_FILENAME",
    "DEMO_DASHBOARD_ID",
    "DEMO_DASHBOARD_KEY_ID",
    "DEMO_DASHBOARD_VERSION",
    "demo_access_policy_reference",
    "demo_dashboard_contract",
    "demo_dashboard_contract_keys",
    "resolve_dashboard_contract_key",
    "seed_demo_dashboard_contract",
]

DASHBOARD_CONTRACT_KEY_FILENAME = "dashboard-contract-key"
DEMO_DASHBOARD_ID = "dashboard-demo-revenue"
DEMO_DASHBOARD_VERSION = 1
DEMO_DASHBOARD_KEY_ID = "demo-dashboard-contract-key-1"
# The architect who certified it. The demonstration has no dashboard designer, and naming the
# requester as owner would say their request certified the dashboard it is published to.
DEMO_DASHBOARD_OWNER = "principal:data-architect-demo"
# A year, matching the answer scope policy's own staleness ceiling. The demonstration seeds its
# source in the past, so a tighter requirement here would leave the dashboard refusing an answer the
# policy admitted -- a disagreement about freshness that says nothing about either rule.
_FRESHNESS_SECONDS = 365 * 86_400
_PRIVATE_KEY_BYTES = 32


class DemoDashboardContractError(RuntimeError):
    pass


def resolve_dashboard_contract_key(state_dir: Path) -> Ed25519PrivateKey:
    """The demonstration's dashboard contract signing key, generated on first use.

    Durable before it signs anything, for the reason in this module's docstring.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    key_path = state_dir / DASHBOARD_CONTRACT_KEY_FILENAME
    stored = read_secret_file(key_path, length=_PRIVATE_KEY_BYTES, refuse=_refuse_to_load)
    if stored is not None:
        return Ed25519PrivateKey.from_private_bytes(stored)
    key = Ed25519PrivateKey.generate()
    payload = key.private_bytes_raw()
    try:
        create_secret_file(key_path, payload, refuse=_refuse_to_store)
    except FileExistsError as error:
        # Two consoles over one state directory raced on first use. The loser adopts the winner's
        # key and discards its own, which signed nothing.
        adopted = read_secret_file(key_path, length=_PRIVATE_KEY_BYTES, refuse=_refuse_to_load)
        if adopted is None:
            raise _refuse_to_load() from error
        return Ed25519PrivateKey.from_private_bytes(adopted)
    return key


def demo_access_policy_reference(policy: AnswerScopePolicy) -> ArtifactReference:
    """The answer scope policy the dashboard's access policy names.

    The same policy the answer was admitted under, rather than one written here. A dashboard whose
    access policy named something else would be published under an authority nobody approved for
    this product -- and the demonstration has exactly one approved scope.
    """
    return ArtifactReference(
        artifact_id=policy.policy_id, version=policy.revision, digest=digest(policy)
    )


def demo_dashboard_contract(
    semantic_version: ApprovedSemanticVersion,
    *,
    product_ref: ArtifactReference,
    access_policy_ref: ArtifactReference,
) -> DashboardContract:
    """A certified contract over exactly the product, metric and dimension the answer reads.

    Certified because an uncertified one is refused by the composition, and a draft left here would
    be a contract the offering never names -- which is the state the demonstration was already in.
    """
    dimension_ref = demo_dimension_reference(semantic_version)
    return DashboardContract(
        dashboard_id=DEMO_DASHBOARD_ID,
        version=DEMO_DASHBOARD_VERSION,
        owner=DEMO_DASHBOARD_OWNER,
        audience=(DEMO_REQUESTER_PRINCIPAL_REF,),
        data_product_versions=(product_ref,),
        metric_versions=(demo_metric_reference(semantic_version),),
        dimensions=(dimension_ref,),
        filters=(),
        # A line, because the question is "what is the daily order value" -- a measure over a
        # time dimension, whose job is trend. Drawn as a column chart, three days of a daily
        # series were three slabs the width of a hand, and a fourth day would have made them
        # narrower rather than telling anyone more.
        visual_intents=("line",),
        drill_paths=((dimension_ref,),),
        freshness_requirement=FreshnessRequirement(maximum_age_seconds=_FRESHNESS_SECONDS),
        access_policy=access_policy_ref,
        report_delivery_policy=None,
        acceptance_tests=(),
        lifecycle_state="certified",
    )


def seed_demo_dashboard_contract(
    contracts: SQLiteDashboardContractRepository,
    *,
    tenant_id: str,
    signing_key: Ed25519PrivateKey,
    semantic_version: ApprovedSemanticVersion,
    product_ref: ArtifactReference,
    access_policy_ref: ArtifactReference,
) -> SignedDashboardContract:
    """Store the demonstration's certified contract, or return the one already stored.

    `store` returns an exact replay unchanged and refuses a different payload under the same
    identity, so a second start adopts what the first signed rather than signing a second contract
    over it. That is also what makes a changed contract a loud failure instead of a silent
    divergence between what is stored and what this composes.
    """
    return contracts.store(
        DashboardContractSigner(DEMO_DASHBOARD_KEY_ID, signing_key).sign(
            tenant_id=tenant_id,
            contract=demo_dashboard_contract(
                semantic_version,
                product_ref=product_ref,
                access_policy_ref=access_policy_ref,
            ),
        )
    )


def demo_dashboard_contract_keys(signing_key: Ed25519PrivateKey) -> dict[str, Ed25519PublicKey]:
    """The verifier's trusted keys, under the identifier the stored contract names."""
    return {DEMO_DASHBOARD_KEY_ID: signing_key.public_key()}


def _refuse_to_load() -> DemoDashboardContractError:
    return DemoDashboardContractError(
        "the demonstration's dashboard contract signing key could not be loaded"
    )


def _refuse_to_store() -> DemoDashboardContractError:
    return DemoDashboardContractError(
        "the demonstration's dashboard contract signing key could not be stored"
    )
