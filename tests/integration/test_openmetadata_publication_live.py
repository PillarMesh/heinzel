from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest
from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    ContractConstraint,
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
from pillarmesh_provider_openmetadata import (
    CatalogProviderError,
    GlossaryTermPayload,
    OpenMetadataClient,
    OpenMetadataPublicationProvider,
    OpenMetadataSettings,
)
from pillarmesh_provider_openmetadata.client import _OpenMetadataCredentials
from pillarmesh_request_management import (
    RequestManagementService,
    RequestState,
    SQLiteRequestRepository,
)
from pillarmesh_semantic_registry import (
    SemanticPublicationService,
    SQLiteCatalogPublicationRepository,
    SQLiteSemanticRepository,
    SQLiteSemanticVersionRepository,
)
from pydantic import SecretStr

_NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class _CreatedResource(Protocol):
    @property
    def resource_kind(self) -> str: ...

    @property
    def identifier(self) -> str: ...

    @property
    def collection(self) -> str: ...


def _semantic_version(tenant_id: str) -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="pending",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="package-live", version=1, digest="a" * 64
        ),
        candidate_set_digest="b" * 64,
        review_bundle_digest="c" * 64,
        entities=(
            SemanticObject(
                object_id="customer",
                name="Customer",
                definition="A party that purchases a product.",
                source_refs=("package-live",),
            ),
            SemanticObject(
                object_id="order",
                name="Order",
                definition="A confirmed request to purchase a product.",
                source_refs=("package-live",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(),
        classifications=(),
        authority_bindings=(),
        approval_ids=("approval-live-owner",),
        created_at=_NOW,
    )


def _contract(semantic_version: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="contract-" + digest(semantic_version)[:24],
        tenant_id=semantic_version.tenant_id,
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
                source_ref="customer_id", semantic_ref="customer", transformation="identity"
            ),
        ),
        integrity_constraints=(
            ContractConstraint(
                constraint_id="customer-key", kind="identity", expression="customer_id"
            ),
        ),
        destination_product=DestinationProductRequirement(
            product_name="live-catalog",
            warehouse_binding_id="warehouse-live",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=3600),
        quality=QualityPolicy(required_constraint_ids=("customer-key",)),
        trigger_policy=TriggerRequirement(run_now_allowed=True),
        access_policy=AccessPolicy(classification_refs=(), required_approver_refs=("role:owner",)),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("approval-live-owner",),
    )


@pytest.mark.live
@pytest.mark.emulator
def test_openmetadata_publication_round_trip_is_executable_when_emulator_is_explicitly_enabled(
    tmp_path: Path,
) -> None:
    if os.environ.get("PILLARMESH_OPENMETADATA_EMULATOR") != "1":
        pytest.skip("Set PILLARMESH_OPENMETADATA_EMULATOR=1 after starting the local emulator.")

    tenant_a = "task8-" + secrets.token_hex(8)
    tenant_b = tenant_a + "-other"
    credentials = _OpenMetadataCredentials(
        username="admin@open-metadata.org",
        password=SecretStr(os.environ["PILLARMESH_OPENMETADATA_BOOTSTRAP_ADMIN_PASSWORD"]),
        runtime_password=SecretStr(secrets.token_urlsafe(24)),
        administrator_password=SecretStr(secrets.token_urlsafe(24)),
    )
    settings = OpenMetadataSettings(base_url="http://127.0.0.1:8585")
    client = OpenMetadataClient(settings=settings, credentials=credentials)
    provider = OpenMetadataPublicationProvider(client)
    publication_repository = SQLiteCatalogPublicationRepository(
        str(tmp_path / "publication.sqlite")
    )
    semantic_repository = SQLiteSemanticRepository(str(tmp_path / "semantic.sqlite"))
    semantic_version_repository = SQLiteSemanticVersionRepository(
        str(tmp_path / "semantic-version.sqlite")
    )
    request_service = RequestManagementService(
        SQLiteRequestRepository.open(str(tmp_path / "requests.sqlite")), clock=lambda: _NOW
    )
    service = SemanticPublicationService(
        repository=publication_repository,
        provider=provider,
        semantic_repository=semantic_repository,
        semantic_version_repository=semantic_version_repository,
        request_service=request_service,
        clock=lambda: _NOW,
    )
    created_resources: tuple[_CreatedResource, ...] = ()

    try:
        semantic_version = semantic_version_repository.store(_semantic_version(tenant_a))
        contract = _contract(semantic_version)
        binding = CatalogBinding(
            binding_id="binding-" + tenant_a,
            tenant_id=tenant_a,
            capability_profile_digest="d" * 64,
            lifecycle_state=CatalogBindingState.READY,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
            provisioned_at=_NOW,
        )

        receipt = service.publish(
            binding=binding, semantic_version=semantic_version, contract=contract
        )
        intent, _, references = publication_repository.load_publication(
            tenant_id=tenant_a, publication_id=receipt.publication_id
        )
        created_resources = client.discovered_resources()
        assert any(resource.resource_kind == "user" for resource in created_resources)

        fresh_client = OpenMetadataClient(settings=settings, credentials=credentials)
        fresh_provider = OpenMetadataPublicationProvider(fresh_client)
        for reference in references:
            original_observation = provider.observe(tenant_id=tenant_a, reference=reference)
            fresh_observation = fresh_provider.observe(tenant_id=tenant_a, reference=reference)
            assert fresh_observation == original_observation

        assert (
            service.publish(binding=binding, semantic_version=semantic_version, contract=contract)
            == receipt
        )

        client.ensure_glossary_term(
            tenant_key=tenant_a,
            identity=intent.semantic_identities[0],
            payload=GlossaryTermPayload(
                name=semantic_version.entities[0].name,
                definition="A materially narrowed customer definition for drift detection.",
                owner_ref="runtime",
                provenance_ref=digest(contract),
            ),
            idempotency_key="edit-" + secrets.token_hex(16),
        )
        proposal = service.observe_drift(tenant_id=tenant_a, publication_id=receipt.publication_id)

        assert proposal is not None
        assert proposal.auto_applied is False
        assert proposal.request.state is RequestState.INVESTIGATING

        with pytest.raises(KeyError):
            publication_repository.load_publication(
                tenant_id=tenant_b, publication_id=receipt.publication_id
            )
        with pytest.raises(CatalogProviderError):
            fresh_provider.observe(tenant_id=tenant_b, reference=references[0])
    finally:
        if not created_resources:
            created_resources = client.discovered_resources()
        for resource in reversed(created_resources):
            client.delete_recorded_resource(
                collection=resource.collection, identifier=resource.identifier
            )
        for resource in created_resources:
            assert client.recorded_resource_is_absent(
                collection=resource.collection, identifier=resource.identifier
            )
