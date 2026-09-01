from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FieldMapping,
    FreshnessRequirement,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    canonical_bytes,
    digest,
)
from pillarmesh_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from pillarmesh_request_management import InboxRequest, RequestState
from pillarmesh_request_management.models import StakeholderQuestion
from pillarmesh_semantic_registry import (
    FulfillmentAuthorityObservation,
    SemanticFulfillmentSnapshotAdapter,
    SQLiteCatalogPublicationRepository,
)
from pillarmesh_semantic_registry.publication import CatalogPublicationReceipt, publication_intent

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str, *, payload: object | None = None) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=artifact_id,
        version=1,
        digest=DIGEST if payload is None else digest(payload),
    )


def semantic_version() -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id="tenant-a",
        version=1,
        process_package_ref=reference("process-revenue"),
        candidate_set_digest=DIGEST,
        review_bundle_digest=DIGEST,
        entities=(
            SemanticObject(
                object_id="invoice",
                name="Invoice",
                definition="An issued customer invoice.",
                source_refs=("process-revenue",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(
            SemanticObject(
                object_id="net-revenue",
                name="Net revenue",
                definition="Gross revenue less approved refunds.",
                source_refs=("process-revenue",),
            ),
        ),
        classifications=(
            SemanticObject(
                object_id="finance",
                name="Finance",
                definition="Finance-controlled information.",
                source_refs=("process-revenue",),
            ),
        ),
        authority_bindings=(),
        approval_ids=("approval-owner",),
        created_at=NOW,
    )


def contract(version: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="contract-revenue",
        tenant_id="tenant-a",
        version=1,
        formation_status="ready_to_activate",
        semantic_version_ref=reference(version.semantic_version_id, payload=version),
        source_observation_refs=(),
        mappings=(
            FieldMapping(
                source_ref="invoice.total",
                semantic_ref="net-revenue",
                transformation="derived",
            ),
        ),
        integrity_constraints=(),
        destination_product=DestinationProductRequirement(
            product_name="product-revenue",
            warehouse_binding_id="warehouse-a",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=3600),
        quality=QualityPolicy(required_constraint_ids=()),
        trigger_policy=TriggerRequirement(run_now_allowed=True),
        access_policy=AccessPolicy(
            classification_refs=("finance",),
            required_approver_refs=("owner:product-revenue",),
        ),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("approval-owner",),
    )


def request(*, tenant_id: str = "tenant-a") -> InboxRequest:
    return InboxRequest(
        request_id="request-1",
        tenant_id=tenant_id,
        requester_id="requester-a",
        payload=StakeholderQuestion(purpose="monthly close", question="What is net revenue?"),
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def product_reference(integration_contract: ManagedIntegrationContract) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=integration_contract.destination_product.product_name,
        version=integration_contract.version,
        digest=digest(integration_contract.destination_product),
    )


def authority(
    publication_id: str,
    integration_contract: ManagedIntegrationContract,
    *,
    tenant_id: str = "tenant-a",
    valid_until: datetime = NOW + timedelta(hours=1),
) -> FulfillmentAuthorityObservation:
    policy_ref = reference("policy-finance")
    observed_at = NOW if valid_until > NOW else valid_until - timedelta(hours=1)
    return FulfillmentAuthorityObservation(
        tenant_id=tenant_id,
        catalog_publication_id=publication_id,
        requester_id="requester-a",
        requester_principal_ref="principal:requester-a",
        purpose_digest=digest("monthly close"),
        authorization_policy_ref=policy_ref,
        approved_policy_refs=(policy_ref,),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(reference("classification-rule-1"),),
        permitted_data_product_refs=(product_reference(integration_contract),),
        permitted_access_modes=("query",),
        maximum_expiry=NOW + timedelta(days=7),
        policy_authority_classifications=("finance",),
        freshness_observation_ref=reference("freshness-1"),
        quality_observation_refs=(reference("quality-1"),),
        data_observation_refs=(reference("data-1"),),
        observed_at=observed_at,
        valid_until=valid_until,
    )


class StaticAuthorityResolver:
    def __init__(self, observation: FulfillmentAuthorityObservation) -> None:
        self.observation = observation

    def resolve(self, *, tenant_id: str, request: InboxRequest) -> FulfillmentAuthorityObservation:
        return self.observation


def published_repository() -> tuple[
    SQLiteCatalogPublicationRepository,
    CatalogPublicationReceipt,
    ManagedIntegrationContract,
]:
    repository = SQLiteCatalogPublicationRepository(":memory:")
    version = semantic_version()
    integration_contract = contract(version)
    intent = publication_intent(
        binding=CatalogBinding(
            binding_id="catalog-a",
            tenant_id="tenant-a",
            capability_profile_digest=DIGEST,
            lifecycle_state=CatalogBindingState.READY,
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            provisioned_at=NOW,
        ),
        semantic_version=version,
        contract=integration_contract,
    )
    repository.store_intent(
        intent=intent,
        semantic_version=version,
        contract=integration_contract,
    )
    provider_reference = CatalogObjectRef(
        tenant_key="tenant-a",
        stable_identity="private-provider-object-123",
        normalized_digest=DIGEST,
    )
    provider_observation = CatalogObjectSnapshot(
        tenant_key="tenant-a",
        stable_identity=provider_reference.stable_identity,
        logical_identity="private-logical-object",
        object_kind="namespace",
        normalized_payload={"private_provider_id": "secret-provider-456"},
        normalized_digest=digest({"private_provider_id": "secret-provider-456"}),
    )
    receipt = CatalogPublicationReceipt(
        publication_id="publication-revenue",
        tenant_id="tenant-a",
        intent_digest=digest(intent),
        provider_version="private-provider-version",
        published_refs=(reference(version.semantic_version_id, payload=version),),
        round_trip_observation_digest=digest((provider_observation,)),
        published_at=NOW,
    )
    repository.store_receipt(
        intent=intent,
        receipt=receipt,
        references=(provider_reference,),
        observations=(provider_observation,),
    )
    return repository, receipt, integration_contract


def test_exact_publication_resolves_to_immutable_public_snapshots() -> None:
    repository, receipt, integration_contract = published_repository()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority(receipt.publication_id, integration_contract)
        ),
        clock=lambda: NOW,
    )

    result = adapter.resolve(tenant_id="tenant-a", request=request())

    assert isinstance(result, tuple)
    grounding, policy = result
    assert grounding.catalog_publication_id == receipt.publication_id
    assert grounding.semantic_version_ref.artifact_id == "semantic-revenue"
    assert grounding.integration_contract_ref.artifact_id == "contract-revenue"
    assert grounding.metric_refs[0].artifact_id == "net-revenue"
    assert grounding.classification_refs[0].artifact_id == "finance"
    assert policy.permitted_data_product_refs == grounding.governed_dataset_refs
    serialized = canonical_bytes(result)
    assert b"private-provider-object-123" not in serialized
    assert b"secret-provider-456" not in serialized
    assert b"private-provider-version" not in serialized


def test_unknown_publication_returns_closed_resolution_failure() -> None:
    repository, _, integration_contract = published_repository()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority("publication-unknown", integration_contract)
        ),
        clock=lambda: NOW,
    )

    result = adapter.resolve(tenant_id="tenant-a", request=request())

    assert not isinstance(result, tuple)
    assert result.reason_codes == ("publication_not_found",)


def test_cross_tenant_authority_is_refused_before_publication_lookup() -> None:
    repository, receipt, integration_contract = published_repository()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority(receipt.publication_id, integration_contract, tenant_id="tenant-b")
        ),
        clock=lambda: NOW,
    )

    result = adapter.resolve(tenant_id="tenant-a", request=request())

    assert not isinstance(result, tuple)
    assert result.reason_codes == ("authority_tenant_mismatch",)


def test_expired_entitlement_observation_fails_closed() -> None:
    repository, receipt, integration_contract = published_repository()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority(
                receipt.publication_id,
                integration_contract,
                valid_until=NOW - timedelta(seconds=1),
            )
        ),
        clock=lambda: NOW,
    )

    result = adapter.resolve(tenant_id="tenant-a", request=request())

    assert not isinstance(result, tuple)
    assert result.reason_codes == ("entitlement_expired",)


def test_tampered_publication_intent_digest_fails_closed() -> None:
    repository, receipt, integration_contract = published_repository()
    repository._connection.execute(
        "UPDATE catalog_publication_receipts SET payload = ? WHERE publication_id = ?",
        (
            canonical_bytes(receipt.model_copy(update={"intent_digest": "f" * 64})),
            receipt.publication_id,
        ),
    )
    repository._connection.commit()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority(receipt.publication_id, integration_contract)
        ),
        clock=lambda: NOW,
    )

    result = adapter.resolve(tenant_id="tenant-a", request=request())

    assert not isinstance(result, tuple)
    assert result.reason_codes == ("publication_integrity_failure",)


def test_changed_private_catalog_observation_cannot_change_existing_snapshot() -> None:
    repository, receipt, integration_contract = published_repository()
    adapter = SemanticFulfillmentSnapshotAdapter(
        publication_repository=repository,
        authority_resolver=StaticAuthorityResolver(
            authority(receipt.publication_id, integration_contract)
        ),
        clock=lambda: NOW,
    )
    mutable_live_catalog_state = {"private_owner": "owner-a"}

    first = adapter.resolve(tenant_id="tenant-a", request=request())
    mutable_live_catalog_state["private_owner"] = "owner-b"
    second = adapter.resolve(tenant_id="tenant-a", request=request())

    assert first == second
    assert mutable_live_catalog_state == {"private_owner": "owner-b"}
