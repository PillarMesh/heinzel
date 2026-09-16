from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pillarmesh_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    SQLiteCatalogRepository,
)
from pillarmesh_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FreshnessRequirement,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    digest,
)
from pillarmesh_contract_service import SourceFreshnessObservation
from pillarmesh_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from pillarmesh_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    ProviderError,
)
from pillarmesh_runtime import (
    AuthoritativeProductCatalog,
    MaterializationAuthorityError,
    MaterializationObservation,
    MaterializationRequest,
    ProductCatalogCompositionConfig,
    ProductMaterializationAdmission,
    ProductMaterializationRunner,
    compose_authoritative_product_catalog,
)
from pillarmesh_runtime.product_input_cardinality import (
    ProductInputCardinalityEvidence,
    ProductInputReceiptCardinality,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from pillarmesh_semantic_registry import (
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationExecutionService,
    ProductCatalogPublicationService,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityService,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    ProductVersionAuthorityService,
    SQLiteApprovedProductVersionRepository,
    SQLiteProductCatalogPublicationRepository,
    SQLiteProductQueryBindingRepository,
)
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_AUTHORIZATION_SIGNER = ProductExecutionAuthorizationSigner.generate("runtime-authority-1")
_LEGALITY_DECISION_DIGEST = "8" * 64


class _Warehouse:
    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        return MaterializationObservation(
            provider_commit_reference="commit-postgresql-1",
            output_schema_digest=request.expected_output_schema_digest,
            output_row_count=2,
            dbt_manifest_digest="1" * 64,
            dbt_run_results_digest="2" * 64,
            lineage_digest="3" * 64,
            quality_assertion_count=2,
            quality_disposition="passed",
        )

    def switch_consumption_view(
        self, request: MaterializationRequest, observation: MaterializationObservation
    ) -> None:
        del request, observation


class _WarehouseBindings:
    def __init__(self, binding: WarehouseBinding) -> None:
        self.binding = binding

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if (tenant_id, binding_id) != (self.binding.tenant_id, self.binding.binding_id):
            return None
        return self.binding


class _CatalogBindings:
    def __init__(self, binding: CatalogBinding) -> None:
        self.binding = binding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if (tenant_id, binding_id) != (self.binding.tenant_id, self.binding.binding_id):
            raise KeyError((tenant_id, binding_id))
        return self.binding


class _CatalogProvider:
    provider_kind: Literal["openmetadata"] = "openmetadata"

    def __init__(self) -> None:
        self.failure: ProviderError | None = None
        self.product: CatalogProductDefinition | None = None
        self.table: CatalogNativeTableDefinition | None = None

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        if self.failure is not None:
            raise self.failure
        self.product = definition
        return self._product_observation(definition)

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        assert self.product is not None
        assert (tenant_id, stable_external_key) == (
            self.product.tenant_id,
            self.product.stable_external_key,
        )
        return self._product_observation(self.product)

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self.table = definition
        return self._table_observation(definition)

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        assert definition == self.table
        return self._table_observation(definition)

    @staticmethod
    def _product_observation(definition: CatalogProductDefinition) -> CatalogProductObservation:
        return CatalogProductObservation(
            tenant_id=definition.tenant_id,
            stable_external_key=definition.stable_external_key,
            definition=definition,
            definition_digest=digest(definition),
            provider_version="1.10.7",
        )

    @staticmethod
    def _table_observation(
        definition: CatalogNativeTableDefinition,
    ) -> CatalogNativeTableObservation:
        warehouse = definition.warehouse
        return CatalogNativeTableObservation(
            definition=definition,
            definition_digest=digest(definition),
            table_fully_qualified_name=".".join(
                (
                    warehouse.database_service_name,
                    warehouse.database_name,
                    warehouse.schema_name,
                    warehouse.table_name,
                )
            ),
            provider_version="1.10.7",
        )


def _authorities() -> tuple[
    ManagedIntegrationContract,
    ApprovedSemanticVersion,
    ProductQueryBindingDeclaration,
    ProductQueryBindingApproval,
]:
    semantic = ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id="tenant-a",
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-revenue", version=1, digest="4" * 64
        ),
        candidate_set_digest="5" * 64,
        review_bundle_digest="6" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="An approved reporting region.",
                source_refs=("source-sales",),
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
                source_refs=("source-sales",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-1",),
        created_at=NOW,
    )
    contract = ManagedIntegrationContract(
        contract_id="contract-revenue",
        tenant_id="tenant-a",
        version=1,
        formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic.semantic_version_id,
            version=semantic.version,
            digest=digest(semantic),
        ),
        source_observation_refs=(),
        mappings=(),
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
            classification_refs=(), required_approver_refs=("owner:revenue",)
        ),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("contract-approval-1",),
    )
    region_ref = ArtifactReference(
        artifact_id="region", version=1, digest=digest(semantic.entities[0])
    )
    revenue_ref = ArtifactReference(
        artifact_id="net-revenue", version=1, digest=digest(semantic.metrics[0])
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
    product_ref = ArtifactReference(
        artifact_id=contract.destination_product.product_name,
        version=contract.version,
        digest=digest(contract.destination_product),
    )
    approval = ProductQueryBindingApproval(
        approval_id="query-binding-approval-1",
        tenant_id="tenant-a",
        product_ref=product_ref,
        generation=1,
        declaration_digest=digest(declaration),
        authority_ref="owner:revenue",
        actor_id="product-owner-a",
        decision="approve",
        created_at=NOW,
    )
    return contract, semantic, declaration, approval


def _catalog(
    *, warehouse_state: WarehouseBindingState = WarehouseBindingState.READY
) -> tuple[
    AuthoritativeProductCatalog, _CatalogProvider, SQLiteProductCatalogPublicationRepository
]:
    contract, semantic, declaration, approval = _authorities()
    warehouse_binding = WarehouseBinding(
        binding_id="warehouse-a",
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capability_profile_digest="7" * 64,
        lifecycle_state=warehouse_state,
        revision=4,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW if warehouse_state is WarehouseBindingState.READY else None,
    )
    catalog_binding = CatalogBinding(
        binding_id="catalog-a",
        tenant_id="tenant-a",
        capability_profile_digest="8" * 64,
        lifecycle_state=CatalogBindingState.READY,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW,
    )
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    provider = _CatalogProvider()
    catalog = AuthoritativeProductCatalog(
        warehouse_bindings=_WarehouseBindings(warehouse_binding),
        catalog_binding=catalog_binding,
        publication_service=ProductCatalogPublicationService(
            repository=repository,
            binding_authority=_CatalogBindings(catalog_binding),
        ),
        publication_execution=ProductCatalogPublicationExecutionService(
            repository=repository,
            provider=provider,
            clock=lambda: NOW,
        ),
        product_version_authority=ProductVersionAuthorityService(
            SQLiteApprovedProductVersionRepository(":memory:")
        ),
        query_binding_authority=ProductQueryBindingAuthorityService(
            SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: NOW
        ),
        contract=contract,
        semantic_version=semantic,
        definition_authority=ProductCatalogDefinitionAuthority(
            name="Current revenue by region",
            description="Approved revenue grouped by reporting region.",
            owner_refs=("owner:revenue",),
            namespace="consumption",
            relation_name="product_revenue",
            columns=(
                ProductCatalogColumnAuthority(name="region", type_name="TEXT", nullable=False),
                ProductCatalogColumnAuthority(
                    name="net_revenue", type_name="NUMERIC", nullable=False
                ),
            ),
        ),
        query_binding_declaration=declaration,
        query_binding_approvals=(approval,),
        source_freshness_observations=(
            SourceFreshnessObservation(
                observation_id="freshness-sales-1",
                tenant_id="tenant-a",
                version=1,
                source_ref="source-sales",
                input_generation_digest="9" * 64,
                data_observation_ref=ArtifactReference(
                    artifact_id="source-observation-1", version=1, digest="a" * 64
                ),
                watermark_at=NOW,
                observed_at=NOW,
            ),
        ),
        database_name="pillarmesh",
    )
    return catalog, provider, repository


def _physical_plan() -> ProductPhysicalPlan:
    contract, _, _, _ = _authorities()
    statement = 'SELECT 1 AS "revenue_total"'
    return ProductPhysicalPlan(
        compiler_version="compiler-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        contract_ref=contract.contract_id,
        contract_revision=contract.version,
        contract_digest=digest(contract),
        iir_digest="4" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=4,
        provider_observation_digest="5" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="revenue",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="6" * 64,
            landing_receipt_digest="9" * 64,
            observed_source_schema_digest="7" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="amount",
                    json_field="amount",
                    scalar_type="decimal",
                ),
            ),
        ),
        target=ProductTarget(namespace="products", relation_name="revenue"),
        emitted_statement=statement,
        statement_digest=digest(statement),
        output_columns=("revenue_total",),
        expected_output_schema_digest="c" * 64,
        decimal_output_checks=(Decimal57OutputCheck(column_name="revenue_total"),),
    )


def _authorization_verifier() -> ProductExecutionAuthorizationVerifier:
    return ProductExecutionAuthorizationVerifier(
        {"runtime-authority-1": _AUTHORIZATION_SIGNER.public_key}
    )


def _admission() -> ProductMaterializationAdmission:
    plan = _physical_plan()
    cardinality_evidence_digest = digest(_cardinality_evidence())
    return ProductMaterializationAdmission(
        legality_decision_digest=_LEGALITY_DECISION_DIGEST,
        cardinality_evidence_digest=cardinality_evidence_digest,
        signed_execution_authorization=_AUTHORIZATION_SIGNER.sign(
            physical_plan=plan,
            legality_decision_digest=_LEGALITY_DECISION_DIGEST,
            cardinality_evidence_digest=cardinality_evidence_digest,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        ),
    )


def _cardinality_evidence() -> ProductInputCardinalityEvidence:
    contract, _, _, _ = _authorities()
    receipt = ProductInputReceiptCardinality(
        generation_id="8" * 64,
        receipt_digest="9" * 64,
        record_count=3,
    )
    return ProductInputCardinalityEvidence(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=7,
        contract_digest=digest(contract),
        product_plan_digest=digest(_physical_plan()),
        relation_ref="raw_revenue",
        generation_ids=(receipt.generation_id,),
        receipts=(receipt,),
        total_contributing_row_ceiling=3,
        policy_maximum_contributing_rows=100,
        maximum_scaled_sum=3 * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=NOW,
    )


def _request() -> MaterializationRequest:
    contract, _, _, _ = _authorities()
    return MaterializationRequest(
        run_id="run-1",
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        product_generation=1,
        retention_seconds=3600,
        contract_digest=digest(contract),
        physical_plan=_physical_plan(),
        physical_plan_digest=digest(_physical_plan()),
        compiled_model_digest="b" * 64,
        input_generation_digests=("9" * 64,),
        input_cardinality_evidence_digest=digest(_cardinality_evidence()),
        expected_output_schema_digest="c" * 64,
    )


def _cardinality_repository() -> SQLiteProductInputCardinalityEvidenceRepository:
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    repository.record(_cardinality_evidence())
    return repository


def test_committed_materialization_publishes_into_the_ready_warehouse_hierarchy() -> None:
    catalog, provider, repository = _catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    result = runner.materialize(_request(), admission=_admission())

    assert result.publication_pending is False
    assert result.publication_ref is not None
    assert provider.product is not None
    assert provider.product.materialization_receipt_digest == digest(result.receipt)
    assert provider.table is not None
    assert provider.table.warehouse.warehouse_binding_id == "warehouse-a"
    assert provider.table.warehouse.warehouse_binding_revision == 4
    assert provider.table.warehouse.warehouse_provider == "postgresql"
    persisted = repository.load_definition(
        tenant_id="tenant-a", operation_id=provider.product.idempotency_key
    )
    assert persisted == provider.product
    intent = repository.load(tenant_id="tenant-a", operation_id=provider.product.idempotency_key)
    assert intent is not None
    assert intent.product_metadata.materialization_receipt_ref.artifact_id == "run-1"


def test_non_ready_warehouse_keeps_the_committed_product_pending_for_retry() -> None:
    catalog, provider, _ = _catalog(warehouse_state=WarehouseBindingState.VALIDATING)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    result = runner.materialize(_request(), admission=_admission())

    assert result.publication_pending is True
    assert result.publication_ref is None
    assert provider.product is None


def test_catalog_provider_failure_keeps_the_committed_product_pending() -> None:
    catalog, provider, _ = _catalog()
    provider.failure = ProviderError("catalog unavailable", classification="transient_unavailable")
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    result = runner.materialize(_request(), admission=_admission())

    assert result.publication_pending is True
    assert result.publication_ref is None


def test_mismatched_materialization_request_is_rejected_before_catalog_effects() -> None:
    catalog, provider, _ = _catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    with pytest.raises(MaterializationAuthorityError, match="does not match"):
        runner.materialize(
            _request().model_copy(update={"product_id": "other-product"}), admission=_admission()
        )

    assert provider.product is None


def test_production_composition_resolves_catalog_binding_from_its_authority(
    tmp_path: Path,
) -> None:
    contract, semantic, declaration, approval = _authorities()
    warehouse_binding = WarehouseBinding(
        binding_id="warehouse-a",
        tenant_id="tenant-a",
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capability_profile_digest="7" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW,
    )
    warehouse_bindings = SQLiteWarehouseRepository(str(tmp_path / "warehouse-control.db"))
    warehouse_bindings.save(warehouse_binding)
    catalog_bindings = SQLiteCatalogRepository(str(tmp_path / "catalog-control.db"))
    draft_catalog_binding = catalog_bindings.create_draft("tenant-a", NOW)
    catalog_binding = draft_catalog_binding.model_copy(
        update={
            "lifecycle_state": CatalogBindingState.READY,
            "revision": 2,
            "updated_at": NOW,
            "provisioned_at": NOW,
        }
    )
    catalog_bindings.append_transition(catalog_binding, expected_revision=1)
    publication_repository = SQLiteProductCatalogPublicationRepository(
        str(tmp_path / "catalog-publications.db")
    )
    provider = _CatalogProvider()

    catalog = compose_authoritative_product_catalog(
        config=ProductCatalogCompositionConfig(
            catalog_binding_id=catalog_binding.binding_id,
            database_name="pillarmesh",
        ),
        warehouse_bindings=warehouse_bindings,
        catalog_bindings=catalog_bindings,
        publication_repository=publication_repository,
        publication_provider=provider,
        product_version_repository=SQLiteApprovedProductVersionRepository(
            str(tmp_path / "product-versions.db")
        ),
        query_binding_repository=SQLiteProductQueryBindingRepository(
            str(tmp_path / "query-bindings.db")
        ),
        contract=contract,
        semantic_version=semantic,
        definition_authority=ProductCatalogDefinitionAuthority(
            name="Current revenue by region",
            description="Approved revenue grouped by reporting region.",
            owner_refs=("owner:revenue",),
            namespace="consumption",
            relation_name="product_revenue",
            columns=(
                ProductCatalogColumnAuthority(name="region", type_name="TEXT", nullable=False),
                ProductCatalogColumnAuthority(
                    name="net_revenue", type_name="NUMERIC", nullable=False
                ),
            ),
        ),
        query_binding_declaration=declaration,
        query_binding_approvals=(approval,),
        source_freshness_observations=(
            SourceFreshnessObservation(
                observation_id="freshness-sales-1",
                tenant_id="tenant-a",
                version=1,
                source_ref="source-sales",
                input_generation_digest="9" * 64,
                data_observation_ref=ArtifactReference(
                    artifact_id="source-observation-1", version=1, digest="a" * 64
                ),
                watermark_at=NOW,
                observed_at=NOW,
            ),
        ),
        clock=lambda: NOW,
    )
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    result = runner.materialize(_request(), admission=_admission())

    assert result.publication_pending is False
    assert provider.product is not None
    assert provider.product.catalog_binding_id == catalog_binding.binding_id
    intent = publication_repository.load(
        tenant_id="tenant-a", operation_id=provider.product.idempotency_key
    )
    assert intent is not None
    assert intent.catalog_binding_revision == catalog_binding.revision


def test_production_composition_rejects_a_catalog_binding_owned_by_another_tenant() -> None:
    contract, semantic, declaration, approval = _authorities()
    other_tenant_binding = CatalogBinding(
        binding_id="catalog-a",
        tenant_id="tenant-b",
        capability_profile_digest="8" * 64,
        lifecycle_state=CatalogBindingState.READY,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW,
    )

    with pytest.raises(ValueError, match="catalog binding authority is unavailable"):
        compose_authoritative_product_catalog(
            config=ProductCatalogCompositionConfig(
                catalog_binding_id="catalog-a",
                database_name="pillarmesh",
            ),
            warehouse_bindings=_WarehouseBindings(
                WarehouseBinding(
                    binding_id="warehouse-a",
                    tenant_id="tenant-a",
                    engine_kind=EngineKind.POSTGRESQL,
                    region="local",
                    capability_profile_digest="7" * 64,
                    lifecycle_state=WarehouseBindingState.READY,
                    revision=4,
                    created_at=NOW,
                    updated_at=NOW,
                    provisioned_at=NOW,
                )
            ),
            catalog_bindings=_CatalogBindings(other_tenant_binding),
            publication_repository=SQLiteProductCatalogPublicationRepository(":memory:"),
            publication_provider=_CatalogProvider(),
            product_version_repository=SQLiteApprovedProductVersionRepository(":memory:"),
            query_binding_repository=SQLiteProductQueryBindingRepository(":memory:"),
            contract=contract,
            semantic_version=semantic,
            definition_authority=ProductCatalogDefinitionAuthority(
                name="Current revenue by region",
                description="Approved revenue grouped by reporting region.",
                owner_refs=("owner:revenue",),
                namespace="consumption",
                relation_name="product_revenue",
                columns=(
                    ProductCatalogColumnAuthority(name="region", type_name="TEXT", nullable=False),
                ),
            ),
            query_binding_declaration=declaration,
            query_binding_approvals=(approval,),
            source_freshness_observations=(),
            clock=lambda: NOW,
        )
