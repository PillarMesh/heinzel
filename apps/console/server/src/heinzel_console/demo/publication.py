"""The catalog publication the demonstration console publishes for itself.

This is demonstration data, not a real product. The orders product, its source and its
approvals are illustrative; nothing here describes a customer, a warehouse or a deployment.

The console's fulfillment service cannot resolve a snapshot for a tenant whose publication
store is empty, so the demonstration builds its own publication from the real artifact
types rather than borrowing one from the test suite. Nothing in this package imports from
`tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FieldMapping,
    FreshnessRequirement,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    digest,
)
from heinzel_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from heinzel_semantic_registry.publication import CatalogPublicationReceipt, publication_intent

from .stores import DemoStores

__all__ = ["DEMO_TENANT_ID", "DemoPublication", "build_demo_publication"]

DEMO_TENANT_ID = "tenant-demo"
_SEMANTIC_VERSION_ID = "semantic-orders"
_CONTRACT_ID = "contract-orders"
_CATALOG_BINDING_ID = "catalog-demo"
_PUBLICATION_ID = "publication-orders-daily"
_PRODUCT_NAME = "orders_daily"
_SOURCE_NAME = "customer_orders"
_PROCESS_PACKAGE_ID = "process-orders"
_PROVIDER_VERSION = "demonstration-catalog-1"

# The demonstration may be left running for days, so its authority observation is valid for
# a year from the clock's time rather than the hour a test fixture needs.
_AUTHORITY_VALIDITY = timedelta(days=365)


def _demo_digest(purpose: str) -> str:
    """A 64-character hex digest derived from the demonstration's own content.

    Digest-shaped fields are derived rather than written as literals so that nothing in the
    demonstration reads as a real, meaningful digest of a real artifact.
    """
    return digest({"heinzel_demo": purpose, "tenant_id": DEMO_TENANT_ID})


@dataclass(frozen=True)
class DemoPublication:
    """The demonstration's published product, and the artifacts that describe it.

    This is demonstration data, not a real product. `valid_until` is the moment the
    demonstration's fulfillment authority observation stops being valid.
    """

    tenant_id: str
    catalog_binding_id: str
    semantic_version: ApprovedSemanticVersion
    contract: ManagedIntegrationContract
    receipt: CatalogPublicationReceipt
    valid_until: datetime


def _semantic_version(*, created_at: datetime) -> ApprovedSemanticVersion:
    """The demonstration's approved meaning: one entity, one metric, one classification."""
    return ApprovedSemanticVersion(
        semantic_version_id=_SEMANTIC_VERSION_ID,
        tenant_id=DEMO_TENANT_ID,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id=_PROCESS_PACKAGE_ID,
            version=1,
            digest=_demo_digest("process-package"),
        ),
        candidate_set_digest=_demo_digest("candidate-set"),
        review_bundle_digest=_demo_digest("review-bundle"),
        entities=(
            SemanticObject(
                object_id="order",
                name="Order",
                definition="A confirmed customer order.",
                source_refs=(_SOURCE_NAME,),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(
            SemanticObject(
                object_id="daily-order-count",
                name="Daily order count",
                definition="Confirmed customer orders per calendar day.",
                source_refs=(_SOURCE_NAME,),
            ),
        ),
        classifications=(
            SemanticObject(
                object_id="commercial",
                name="Commercial",
                definition="Commercially sensitive information.",
                source_refs=(_SOURCE_NAME,),
            ),
        ),
        authority_bindings=(),
        approval_ids=("approval-demo-owner",),
        created_at=created_at,
    )


def _contract(semantic_version: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    """The demonstration's managed integration contract over that meaning."""
    return ManagedIntegrationContract(
        contract_id=_CONTRACT_ID,
        tenant_id=DEMO_TENANT_ID,
        version=1,
        formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        source_observation_refs=(),
        mappings=(
            FieldMapping(
                source_ref=f"{_SOURCE_NAME}.order_id",
                semantic_ref="daily-order-count",
                transformation="derived",
            ),
        ),
        integrity_constraints=(),
        destination_product=DestinationProductRequirement(
            product_name=_PRODUCT_NAME,
            warehouse_binding_id="warehouse-demo",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=86400),
        quality=QualityPolicy(required_constraint_ids=()),
        trigger_policy=TriggerRequirement(run_now_allowed=True),
        access_policy=AccessPolicy(
            classification_refs=("commercial",),
            required_approver_refs=(f"owner:{_PRODUCT_NAME}",),
        ),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("approval-demo-owner",),
    )


def _existing_publication(
    stores: DemoStores, *, receipt: CatalogPublicationReceipt, valid_until: datetime
) -> DemoPublication:
    """Describe a publication the state directory already holds, storing nothing."""
    intent, stored_receipt, _ = stores.publications.load_publication(
        tenant_id=DEMO_TENANT_ID, publication_id=receipt.publication_id
    )
    semantic_version, contract = stores.publications.load_inputs(
        tenant_id=DEMO_TENANT_ID, operation_id=intent.operation_id
    )
    return DemoPublication(
        tenant_id=DEMO_TENANT_ID,
        catalog_binding_id=intent.catalog_binding_id,
        semantic_version=semantic_version,
        contract=contract,
        receipt=stored_receipt,
        valid_until=valid_until,
    )


def build_demo_publication(stores: DemoStores, *, clock: Callable[[], datetime]) -> DemoPublication:
    """Publish the demonstration's catalog publication, once, into `stores`.

    This is demonstration data, not a real product. The publication is what lets the
    console's fulfillment service resolve a snapshot at all; without one it refuses every
    request for the tenant. A state directory that already holds a publication is described
    rather than written to again, so that a second run of the demonstration is a no-op.
    """
    now = clock()
    valid_until = now + _AUTHORITY_VALIDITY
    published = stores.publications.list_publications(tenant_id=DEMO_TENANT_ID)
    if published:
        return _existing_publication(stores, receipt=published[0], valid_until=valid_until)

    semantic_version = _semantic_version(created_at=now)
    contract = _contract(semantic_version)
    intent = publication_intent(
        binding=CatalogBinding(
            binding_id=_CATALOG_BINDING_ID,
            tenant_id=DEMO_TENANT_ID,
            capability_profile_digest=_demo_digest("capability-profile"),
            lifecycle_state=CatalogBindingState.READY,
            revision=1,
            created_at=now,
            updated_at=now,
            provisioned_at=now,
        ),
        semantic_version=semantic_version,
        contract=contract,
    )
    stores.publications.store_intent(
        intent=intent, semantic_version=semantic_version, contract=contract
    )
    normalized_payload = {"demo_object": _PRODUCT_NAME}
    observation = CatalogObjectSnapshot(
        tenant_key=DEMO_TENANT_ID,
        stable_identity=f"demo-object-{_PRODUCT_NAME}",
        logical_identity=f"demo.{_PRODUCT_NAME}",
        object_kind="namespace",
        normalized_payload=normalized_payload,
        normalized_digest=digest(normalized_payload),
    )
    reference = CatalogObjectRef(
        tenant_key=DEMO_TENANT_ID,
        stable_identity=observation.stable_identity,
        normalized_digest=observation.normalized_digest,
    )
    receipt = CatalogPublicationReceipt(
        publication_id=_PUBLICATION_ID,
        tenant_id=DEMO_TENANT_ID,
        intent_digest=digest(intent),
        provider_version=_PROVIDER_VERSION,
        published_refs=(
            ArtifactReference(
                artifact_id=semantic_version.semantic_version_id,
                version=semantic_version.version,
                digest=digest(semantic_version),
            ),
        ),
        round_trip_observation_digest=digest((observation,)),
        published_at=now,
    )
    stored_receipt = stores.publications.store_receipt(
        intent=intent,
        receipt=receipt,
        references=(reference,),
        observations=(observation,),
    )
    return DemoPublication(
        tenant_id=DEMO_TENANT_ID,
        catalog_binding_id=intent.catalog_binding_id,
        semantic_version=semantic_version,
        contract=contract,
        receipt=stored_receipt,
        valid_until=valid_until,
    )
