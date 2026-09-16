from __future__ import annotations

import secrets
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_contract_model import (
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
from pillarmesh_contract_service import SourceFreshnessObservation
from pillarmesh_provider_openmetadata import OpenMetadataProductCatalogProvider
from pillarmesh_provider_sdk import (
    CatalogWarehouseHierarchyAuthority,
    catalog_warehouse_service_external_key,
)
from pillarmesh_semantic_registry import (
    ApprovedProductQueryBinding,
    ApprovedProductVersionMetadata,
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationExecutionService,
    ProductCatalogPublicationIntent,
    ProductCatalogPublicationService,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityService,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteProductCatalogPublicationRepository,
    SQLiteProductQueryBindingRepository,
)
from starlette.testclient import TestClient

from tests.acceptance.console_native_answer_fixture import (
    PRODUCT as NATIVE_PRODUCT,
)
from tests.acceptance.console_native_answer_fixture import (
    TENANT as NATIVE_TENANT,
)
from tests.acceptance.console_native_answer_fixture import (
    fresh_native_answer_deployment,
)
from tests.acceptance.run_console_governed import REQUESTER
from tests.integration.openmetadata_live_harness import LocalOpenMetadata

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


class _BindingAuthority:
    def __init__(self, binding: CatalogBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if tenant_id != self._binding.tenant_id or binding_id != self._binding.binding_id:
            raise KeyError((tenant_id, binding_id))
        return self._binding


def _semantic_version(tenant_id: str) -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-live-revenue",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-live-revenue", version=1, digest="a" * 64
        ),
        candidate_set_digest="b" * 64,
        review_bundle_digest="c" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="An approved reporting region.",
                source_refs=("process-live-revenue",),
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
                definition="Revenue after approved refunds.",
                source_refs=("process-live-revenue",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-live",),
        created_at=_NOW,
    )


def _contract(semantic_version: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="contract-live-revenue",
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
                source_ref="invoice.total",
                semantic_ref="net-revenue",
                transformation="derived",
            ),
        ),
        integrity_constraints=(),
        destination_product=DestinationProductRequirement(
            product_name="product:live-revenue",
            warehouse_binding_id="warehouse-live",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=3_600),
        quality=QualityPolicy(required_constraint_ids=()),
        trigger_policy=TriggerRequirement(run_now_allowed=True),
        access_policy=AccessPolicy(
            classification_refs=(), required_approver_refs=("owner:revenue",)
        ),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("contract-approval-live",),
    )


def _product_metadata(
    contract: ManagedIntegrationContract,
    semantic_version: ApprovedSemanticVersion,
) -> ApprovedProductVersionMetadata:
    return ApprovedProductVersionMetadata(
        tenant_id=contract.tenant_id,
        product_ref=ArtifactReference(
            artifact_id=contract.destination_product.product_name,
            version=contract.version,
            digest=digest(contract.destination_product),
        ),
        generation=1,
        contract_ref=ArtifactReference(
            artifact_id=contract.contract_id,
            version=contract.version,
            digest=digest(contract),
        ),
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        materialization_receipt_ref=ArtifactReference(
            artifact_id="materialization-live-revenue", version=1, digest="d" * 64
        ),
        lineage_digest="e" * 64,
        approved_narrative_terms=("Revenue", "Net revenue", "Region"),
        recorded_at=_NOW,
    )


def _query_binding(
    contract: ManagedIntegrationContract,
    semantic_version: ApprovedSemanticVersion,
    product_metadata: ApprovedProductVersionMetadata,
) -> ApprovedProductQueryBinding:
    region_ref = ArtifactReference(
        artifact_id=semantic_version.entities[0].object_id,
        version=semantic_version.version,
        digest=digest(semantic_version.entities[0]),
    )
    revenue_ref = ArtifactReference(
        artifact_id=semantic_version.metrics[0].object_id,
        version=semantic_version.version,
        digest=digest(semantic_version.metrics[0]),
    )
    declaration = ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="consumption",
        relation_name="product_revenue",
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=revenue_ref,
                aggregate="sum",
                column_name="net_revenue",
                output_name="net_revenue",
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=region_ref,
                semantic_kind="entity",
                column_name="region",
                output_name="region",
            ),
        ),
        disclosure_entity_ref=region_ref,
        disclosure_entity_column="region",
    )
    approval = ProductQueryBindingApproval(
        approval_id="query-binding-approval-live",
        tenant_id=product_metadata.tenant_id,
        product_ref=product_metadata.product_ref,
        generation=product_metadata.generation,
        declaration_digest=digest(declaration),
        authority_ref="owner:revenue",
        actor_id="product-owner-live",
        decision="approve",
        created_at=_NOW,
    )
    return ProductQueryBindingAuthorityService(
        SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: _NOW
    ).record(
        contract=contract,
        semantic_version=semantic_version,
        product_metadata=product_metadata,
        declaration=declaration,
        approvals=(approval,),
    )


def _publication_intent(
    *, binding: CatalogBinding, repository: SQLiteProductCatalogPublicationRepository
) -> ProductCatalogPublicationIntent:
    semantic_version = _semantic_version(binding.tenant_id)
    contract = _contract(semantic_version)
    product_metadata = _product_metadata(contract, semantic_version)
    query_binding = _query_binding(contract, semantic_version, product_metadata)
    freshness = SourceFreshnessObservation(
        observation_id="freshness-live-revenue",
        tenant_id=binding.tenant_id,
        version=1,
        source_ref="source-live-orders",
        input_generation_digest="f" * 64,
        data_observation_ref=ArtifactReference(
            artifact_id="source-observation-live-orders", version=1, digest="1" * 64
        ),
        watermark_at=_NOW,
        observed_at=_NOW,
    )
    return ProductCatalogPublicationService(
        repository=repository,
        binding_authority=_BindingAuthority(binding),
    ).prepare(
        binding=binding,
        warehouse_hierarchy=CatalogWarehouseHierarchyAuthority(
            tenant_id=binding.tenant_id,
            warehouse_binding_id=contract.destination_product.warehouse_binding_id,
            warehouse_binding_revision=1,
            warehouse_provider="postgresql",
            database_service_name=catalog_warehouse_service_external_key(
                tenant_id=binding.tenant_id,
                warehouse_binding_id=contract.destination_product.warehouse_binding_id,
            ),
            database_name="pillarmesh",
            schema_name="consumption",
            table_name="product_revenue",
        ),
        definition_authority=ProductCatalogDefinitionAuthority(
            name="Current revenue by region",
            description="Approved revenue grouped by reporting region.",
            owner_refs=("owner:revenue",),
            namespace="consumption",
            relation_name="product_revenue",
            columns=(
                ProductCatalogColumnAuthority(
                    name="region",
                    type_name="TEXT",
                    nullable=False,
                    description="An approved reporting region.",
                ),
                ProductCatalogColumnAuthority(
                    name="net_revenue",
                    type_name="NUMERIC",
                    nullable=False,
                    description="Revenue after approved refunds.",
                ),
            ),
        ),
        product_metadata=product_metadata,
        query_binding=query_binding,
        materialization_receipt_ref=product_metadata.materialization_receipt_ref,
        materialization_receipt_digest=product_metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic_version,
    )


@pytest.fixture
def local_openmetadata(tmp_path: Path) -> Generator[LocalOpenMetadata]:
    local = LocalOpenMetadata(tmp_path)
    try:
        yield local
    finally:
        local.cleanup()


@pytest.mark.live
@pytest.mark.emulator
def test_managed_openmetadata_publishes_and_observes_fresh_product_authority(
    local_openmetadata: LocalOpenMetadata, tmp_path: Path
) -> None:
    tenant_id = "product-live-" + secrets.token_hex(8)
    managed_binding = local_openmetadata.provision_and_validate(tenant_id)
    repository = SQLiteProductCatalogPublicationRepository(
        str(tmp_path / "product-catalog-publication.sqlite")
    )
    intent = _publication_intent(binding=managed_binding.binding, repository=repository)
    publishing_client = local_openmetadata.administrator_client(managed_binding)
    service = ProductCatalogPublicationExecutionService(
        repository=repository,
        provider=OpenMetadataProductCatalogProvider(publishing_client),
        clock=lambda: _NOW,
    )

    receipt = service.publish(intent=intent)
    resources_after_publication = publishing_client.discovered_resources()
    replay = service.publish(intent=intent)
    fresh_client = local_openmetadata.administrator_client(managed_binding)
    definition = repository.load_definition(tenant_id=tenant_id, operation_id=intent.operation_id)
    assert definition is not None
    observed = OpenMetadataProductCatalogProvider(fresh_client).observe(
        tenant_id=tenant_id,
        stable_external_key=definition.stable_external_key,
    )
    native_table_definition = repository.load_native_table_definition(
        tenant_id=tenant_id, operation_id=intent.operation_id
    )
    assert native_table_definition is not None
    observed_table = OpenMetadataProductCatalogProvider(fresh_client).observe_native_table(
        native_table_definition
    )

    assert replay == receipt
    assert publishing_client.discovered_resources() == resources_after_publication
    assert observed.definition_digest == receipt.definition_digest
    assert observed.definition.generation == 1
    assert observed.definition.catalog_revision == managed_binding.binding.revision
    assert observed.definition.owner_refs == ("owner:revenue",)
    assert tuple(column.name for column in observed.definition.columns) == (
        "region",
        "net_revenue",
    )
    assert observed.definition.lineage_sources[0].source_ref == "source-live-orders"
    assert observed.definition.materialization_receipt_ref.version == 1
    assert observed_table.definition_digest == receipt.native_table_definition_digest
    assert digest(observed_table) == receipt.native_table_observation_digest
    assert observed_table.table_fully_qualified_name == receipt.table_fully_qualified_name
    assert observed_table.definition.product.columns == observed.definition.columns

    cleaned = local_openmetadata.cleanup_client_resources(publishing_client)
    assert {collection for collection, _ in cleaned} == {
        "databaseServices",
        "databases",
        "databaseSchemas",
        "dataProducts",
        "domains",
        "glossaryTerms",
        "tables",
    }
    retired = local_openmetadata.retire(managed_binding)
    assert retired.binding.lifecycle_state is CatalogBindingState.RETIRED
    assert local_openmetadata.resource_ledger(retired).all_cleaned


@pytest.mark.live
@pytest.mark.emulator
def test_native_postgresql_delivery_is_published_to_openmetadata_and_projected_to_console(
    local_openmetadata: LocalOpenMetadata,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_binding = local_openmetadata.provision_and_validate(NATIVE_TENANT)
    publishing_client = local_openmetadata.administrator_client(managed_binding)

    with fresh_native_answer_deployment(
        tmp_path / "native-openmetadata",
        monkeypatch=monkeypatch,
        catalog_binding=managed_binding.binding,
        catalog_provider=OpenMetadataProductCatalogProvider(publishing_client),
    ) as native:
        with TestClient(native.deployment.build_app(actor=REQUESTER)) as client:
            product = client.get(f"/api/v1/data-products/{NATIVE_PRODUCT.artifact_id}")
            result = client.get(f"/api/v1/requests/{native.request_id}/result")

        definition = native.product_publications.definition_for_reference(
            tenant_id=NATIVE_TENANT,
            product_ref=NATIVE_PRODUCT,
        )
        assert definition is not None
        observed = OpenMetadataProductCatalogProvider(
            local_openmetadata.administrator_client(managed_binding)
        ).observe(
            tenant_id=NATIVE_TENANT,
            stable_external_key=definition.stable_external_key,
        )

        assert product.status_code == 200
        assert product.json()["data"]["name"] == "Current revenue by region"
        assert result.status_code == 200
        assert result.json()["data"]["rows"] == [["east", "99.00"], ["west", "30.00"]]
        assert observed.definition == definition

    cleaned = local_openmetadata.cleanup_client_resources(publishing_client)
    assert {collection for collection, _ in cleaned} == {
        "databaseServices",
        "databases",
        "databaseSchemas",
        "dataProducts",
        "domains",
        "glossaryTerms",
        "tables",
    }
    retired = local_openmetadata.retire(managed_binding)
    assert retired.binding.lifecycle_state is CatalogBindingState.RETIRED
    assert local_openmetadata.resource_ledger(retired).all_cleaned
