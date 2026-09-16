from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
from pillarmesh_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    CatalogWarehouseHierarchyAuthority,
    ProviderError,
    catalog_warehouse_service_external_key,
)
from pillarmesh_semantic_registry.product_authority import ApprovedProductVersionMetadata
from pillarmesh_semantic_registry.product_catalog_publication import (
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationExecutionService,
    ProductCatalogPublicationIntent,
    ProductCatalogPublicationProviderError,
    ProductCatalogPublicationService,
    SQLiteProductCatalogPublicationRepository,
    product_catalog_definition,
)
from pillarmesh_semantic_registry.query_binding import (
    ApprovedProductQueryBinding,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityService,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteProductQueryBindingRepository,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


def _definition_authority() -> ProductCatalogDefinitionAuthority:
    return ProductCatalogDefinitionAuthority(
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
    )


class _BindingAuthority:
    def __init__(self, binding: CatalogBinding) -> None:
        self.binding = binding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if tenant_id != self.binding.tenant_id or binding_id != self.binding.binding_id:
            raise KeyError((tenant_id, binding_id))
        return self.binding


def _binding(*, tenant_id: str = "tenant-a", revision: int = 3) -> CatalogBinding:
    return CatalogBinding(
        binding_id="catalog-a",
        tenant_id=tenant_id,
        capability_profile_digest="a" * 64,
        lifecycle_state=CatalogBindingState.READY,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW,
    )


def _semantic_version(*, tenant_id: str = "tenant-a") -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-revenue", version=1, digest="b" * 64
        ),
        candidate_set_digest="c" * 64,
        review_bundle_digest="d" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="An approved reporting region.",
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
                definition="Revenue after approved refunds.",
                source_refs=("process-revenue",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-1",),
        created_at=NOW,
    )


def _contract(semantic: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="contract-revenue",
        tenant_id=semantic.tenant_id,
        version=1,
        formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic.semantic_version_id,
            version=semantic.version,
            digest=digest(semantic),
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
            product_name="product:revenue",
            warehouse_binding_id="warehouse-a",
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
        approval_ids=("contract-approval-1",),
    )


def _product_metadata(
    contract: ManagedIntegrationContract, semantic: ApprovedSemanticVersion
) -> ApprovedProductVersionMetadata:
    return ApprovedProductVersionMetadata(
        tenant_id=contract.tenant_id,
        product_ref=ArtifactReference(
            artifact_id=contract.destination_product.product_name,
            version=contract.version,
            digest=digest(contract.destination_product),
        ),
        generation=7,
        contract_ref=ArtifactReference(
            artifact_id=contract.contract_id,
            version=contract.version,
            digest=digest(contract),
        ),
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic.semantic_version_id,
            version=semantic.version,
            digest=digest(semantic),
        ),
        materialization_receipt_ref=ArtifactReference(
            artifact_id="materialization-7", version=7, digest="e" * 64
        ),
        lineage_digest="f" * 64,
        approved_narrative_terms=("Revenue", "Net revenue", "Region"),
        recorded_at=NOW,
    )


def _query_binding(
    contract: ManagedIntegrationContract,
    semantic: ApprovedSemanticVersion,
    metadata: ApprovedProductVersionMetadata,
) -> ApprovedProductQueryBinding:
    entity_ref = ArtifactReference(
        artifact_id=semantic.entities[0].object_id,
        version=semantic.version,
        digest=digest(semantic.entities[0]),
    )
    metric_ref = ArtifactReference(
        artifact_id=semantic.metrics[0].object_id,
        version=semantic.version,
        digest=digest(semantic.metrics[0]),
    )
    declaration = ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="consumption",
        relation_name="product_revenue",
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=metric_ref,
                aggregate="sum",
                column_name="net_revenue",
                output_name="net_revenue",
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=entity_ref,
                semantic_kind="entity",
                column_name="region",
                output_name="region",
            ),
        ),
        disclosure_entity_ref=entity_ref,
        disclosure_entity_column="region",
    )
    approval = ProductQueryBindingApproval(
        approval_id="query-binding-approval-1",
        tenant_id=metadata.tenant_id,
        product_ref=metadata.product_ref,
        generation=metadata.generation,
        declaration_digest=digest(declaration),
        authority_ref="owner:revenue",
        actor_id="product-owner-a",
        decision="approve",
        created_at=NOW,
    )
    return ProductQueryBindingAuthorityService(
        SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: NOW
    ).record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=(approval,),
    )


def _freshness(*, tenant_id: str = "tenant-a") -> SourceFreshnessObservation:
    return SourceFreshnessObservation(
        observation_id="freshness-orders-7",
        tenant_id=tenant_id,
        version=1,
        source_ref="source-orders",
        input_generation_digest="1" * 64,
        data_observation_ref=ArtifactReference(
            artifact_id="source-observation-orders", version=1, digest="2" * 64
        ),
        watermark_at=NOW,
        observed_at=NOW,
    )


def _warehouse_hierarchy(
    *, tenant_id: str = "tenant-a", binding_id: str = "warehouse-a"
) -> CatalogWarehouseHierarchyAuthority:
    return CatalogWarehouseHierarchyAuthority(
        tenant_id=tenant_id,
        warehouse_binding_id=binding_id,
        warehouse_binding_revision=4,
        warehouse_provider="postgresql",
        database_service_name=catalog_warehouse_service_external_key(
            tenant_id=tenant_id, warehouse_binding_id=binding_id
        ),
        database_name="pillarmesh",
        schema_name="consumption",
        table_name="product_revenue",
    )


def _inputs() -> tuple[
    CatalogBinding,
    ApprovedProductVersionMetadata,
    ApprovedProductQueryBinding,
    ManagedIntegrationContract,
    ApprovedSemanticVersion,
    SourceFreshnessObservation,
]:
    binding = _binding()
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _product_metadata(contract, semantic)
    query_binding = _query_binding(contract, semantic, metadata)
    return binding, metadata, query_binding, contract, semantic, _freshness()


def _prepared(
    repository: SQLiteProductCatalogPublicationRepository,
) -> ProductCatalogPublicationIntent:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    return ProductCatalogPublicationService(
        repository=repository,
        binding_authority=_BindingAuthority(binding),
    ).prepare(
        binding=binding,
        warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
        definition_authority=_definition_authority(),
        product_metadata=metadata,
        query_binding=query_binding,
        materialization_receipt_ref=metadata.materialization_receipt_ref,
        materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic,
    )


class _RecordingProductCatalogProvider:
    provider_kind: Literal["openmetadata"] = "openmetadata"

    def __init__(self) -> None:
        self.publish_calls: list[CatalogProductDefinition] = []
        self.observe_calls: list[tuple[str, str]] = []
        self.failure: ProviderError | None = None
        self.observation_override: CatalogProductObservation | None = None
        self.table_publish_calls: list[CatalogNativeTableDefinition] = []
        self.table_observe_calls: list[CatalogNativeTableDefinition] = []
        self.table_failure: ProviderError | None = None

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        self.publish_calls.append(definition)
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        return _catalog_observation(definition)

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        self.observe_calls.append((tenant_id, stable_external_key))
        if self.observation_override is not None:
            return self.observation_override
        return _catalog_observation(self.publish_calls[-1])

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self.table_publish_calls.append(definition)
        if self.table_failure is not None:
            failure = self.table_failure
            self.table_failure = None
            raise failure
        return _table_observation(definition)

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self.table_observe_calls.append(definition)
        return _table_observation(definition)


def _catalog_observation(definition: CatalogProductDefinition) -> CatalogProductObservation:
    return CatalogProductObservation(
        tenant_id=definition.tenant_id,
        stable_external_key=definition.stable_external_key,
        definition=definition,
        definition_digest=digest(definition),
        provider_version="1.13.3",
    )


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
        provider_version="1.13.3",
    )


def test_prepares_and_persists_exact_product_catalog_publication_authority() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    service = ProductCatalogPublicationService(
        repository=repository,
        binding_authority=_BindingAuthority(binding),
    )

    intent = service.prepare(
        binding=binding,
        warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
        definition_authority=_definition_authority(),
        product_metadata=metadata,
        query_binding=query_binding,
        materialization_receipt_ref=metadata.materialization_receipt_ref,
        materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic,
    )

    assert repository.load(tenant_id="tenant-a", operation_id=intent.operation_id) == intent
    assert intent.catalog_binding_revision == 3
    assert intent.product_metadata == metadata
    assert intent.query_binding == query_binding
    assert intent.source_freshness_observation_refs == (
        ArtifactReference(
            artifact_id=freshness.observation_id,
            version=freshness.version,
            digest=digest(freshness),
        ),
    )


def test_repository_can_opt_into_worker_thread_reads(tmp_path: Path) -> None:
    repository = SQLiteProductCatalogPublicationRepository(
        str(tmp_path / "publications.sqlite3"), check_same_thread=False
    )
    intent = _prepared(repository)

    with ThreadPoolExecutor(max_workers=1) as executor:
        loaded = executor.submit(
            repository.load,
            tenant_id=intent.tenant_id,
            operation_id=intent.operation_id,
        ).result()

    assert loaded == intent
    repository.close()


def test_exact_publication_authority_replays_without_creating_another_record() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    service = ProductCatalogPublicationService(
        repository=repository,
        binding_authority=_BindingAuthority(binding),
    )
    first = service.prepare(
        binding=binding,
        warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
        definition_authority=_definition_authority(),
        product_metadata=metadata,
        query_binding=query_binding,
        materialization_receipt_ref=metadata.materialization_receipt_ref,
        materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic,
    )
    replay = service.prepare(
        binding=binding,
        warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
        definition_authority=_definition_authority(),
        product_metadata=metadata,
        query_binding=query_binding,
        materialization_receipt_ref=metadata.materialization_receipt_ref,
        materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic,
    )

    assert replay.model_dump_json() == first.model_dump_json()
    count = repository._connection.execute(
        "SELECT COUNT(*) FROM product_catalog_publication_intents"
    ).fetchone()
    assert count == (1,)


def test_repository_rejects_conflicting_replay_for_the_same_operation() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = ProductCatalogPublicationService(
        repository=repository,
        binding_authority=_BindingAuthority(binding),
    ).prepare(
        binding=binding,
        warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
        definition_authority=_definition_authority(),
        product_metadata=metadata,
        query_binding=query_binding,
        materialization_receipt_ref=metadata.materialization_receipt_ref,
        materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
        source_freshness_observations=(freshness,),
        contract=contract,
        semantic_version=semantic,
    )
    changed_freshness = freshness.model_copy(
        update={"watermark_at": datetime(2026, 9, 14, 11, tzinfo=UTC)}
    )
    conflicting = intent.model_copy(
        update={
            "source_freshness_observations": (changed_freshness,),
            "source_freshness_observation_refs": (
                ArtifactReference(
                    artifact_id=changed_freshness.observation_id,
                    version=changed_freshness.version,
                    digest=digest(changed_freshness),
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="publication authority is immutable"):
        repository.store(conflicting)


def test_product_catalog_publication_denies_cross_tenant_freshness_authority() -> None:
    binding, metadata, query_binding, contract, semantic, _ = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")

    with pytest.raises(ValueError, match="share one tenant"):
        ProductCatalogPublicationService(
            repository=repository,
            binding_authority=_BindingAuthority(binding),
        ).prepare(
            binding=binding,
            warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
            definition_authority=_definition_authority(),
            product_metadata=metadata,
            query_binding=query_binding,
            materialization_receipt_ref=metadata.materialization_receipt_ref,
            materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
            source_freshness_observations=(_freshness(tenant_id="tenant-b"),),
            contract=contract,
            semantic_version=semantic,
        )

    assert repository._connection.execute(
        "SELECT COUNT(*) FROM product_catalog_publication_intents"
    ).fetchone() == (0,)


def test_product_catalog_publication_rejects_another_warehouse_binding() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    other_hierarchy = _warehouse_hierarchy(binding_id="warehouse-other")

    with pytest.raises(ValueError, match="warehouse hierarchy"):
        ProductCatalogPublicationService(
            repository=repository,
            binding_authority=_BindingAuthority(binding),
        ).prepare(
            binding=binding,
            warehouse_hierarchy=other_hierarchy,
            definition_authority=_definition_authority(),
            product_metadata=metadata,
            query_binding=query_binding,
            materialization_receipt_ref=metadata.materialization_receipt_ref,
            materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
            source_freshness_observations=(freshness,),
            contract=contract,
            semantic_version=semantic,
        )

    assert repository._connection.execute(
        "SELECT COUNT(*) FROM product_catalog_publication_intents"
    ).fetchone() == (0,)


def test_product_catalog_publication_rejects_a_stale_catalog_binding_revision() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    current_binding = _binding(revision=binding.revision + 1)

    with pytest.raises(ValueError, match="binding revision is stale"):
        ProductCatalogPublicationService(
            repository=repository,
            binding_authority=_BindingAuthority(current_binding),
        ).prepare(
            binding=binding,
            warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
            definition_authority=_definition_authority(),
            product_metadata=metadata,
            query_binding=query_binding,
            materialization_receipt_ref=metadata.materialization_receipt_ref,
            materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
            source_freshness_observations=(freshness,),
            contract=contract,
            semantic_version=semantic,
        )

    assert repository._connection.execute(
        "SELECT COUNT(*) FROM product_catalog_publication_intents"
    ).fetchone() == (0,)


def test_publication_authority_rejects_columns_outside_the_approved_query_binding() -> None:
    binding, metadata, query_binding, contract, semantic, freshness = _inputs()
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    definition_authority = _definition_authority().model_copy(
        update={
            "columns": (
                ProductCatalogColumnAuthority(
                    name="invented_column", type_name="TEXT", nullable=True
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="definition does not match approved query binding"):
        ProductCatalogPublicationService(
            repository=repository,
            binding_authority=_BindingAuthority(binding),
        ).prepare(
            binding=binding,
            warehouse_hierarchy=_warehouse_hierarchy(tenant_id=binding.tenant_id),
            definition_authority=definition_authority,
            product_metadata=metadata,
            query_binding=query_binding,
            materialization_receipt_ref=metadata.materialization_receipt_ref,
            materialization_receipt_digest=metadata.materialization_receipt_ref.digest,
            source_freshness_observations=(freshness,),
            contract=contract,
            semantic_version=semantic,
        )

    assert repository.load(tenant_id="tenant-a", operation_id="0" * 64) is None


def test_execution_publishes_and_persists_the_exact_round_trip_observation() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()

    receipt = ProductCatalogPublicationExecutionService(
        repository=repository, provider=provider, clock=lambda: NOW
    ).publish(intent=intent)

    definition = product_catalog_definition(intent)
    assert tuple(column.name for column in definition.columns) == ("region", "net_revenue")
    assert provider.publish_calls == [definition]
    assert provider.observe_calls == [(intent.tenant_id, definition.stable_external_key)]
    assert receipt.intent_digest == digest(intent)
    assert receipt.definition_digest == digest(definition)
    assert receipt.product_ref == intent.product_metadata.product_ref
    assert receipt.generation == intent.product_metadata.generation
    assert (
        repository.load_receipt(tenant_id=intent.tenant_id, operation_id=intent.operation_id)
        == receipt
    )
    assert repository.load_observation(
        tenant_id=intent.tenant_id, operation_id=intent.operation_id
    ) == _catalog_observation(definition)

    assert (
        repository.read_for_product_generation(
            tenant_id=intent.tenant_id,
            product_ref=intent.product_metadata.product_ref,
            generation=intent.product_metadata.generation,
        )
        == receipt
    )


def test_product_generation_lookup_returns_none_until_publication_is_complete() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)

    assert (
        repository.read_for_product_generation(
            tenant_id=intent.tenant_id,
            product_ref=intent.product_metadata.product_ref,
            generation=intent.product_metadata.generation,
        )
        is None
    )


def test_execution_replay_returns_the_durable_receipt_without_provider_effects() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()
    service = ProductCatalogPublicationExecutionService(
        repository=repository, provider=provider, clock=lambda: NOW
    )

    first = service.publish(intent=intent)
    replay = service.publish(intent=intent)

    assert replay == first
    assert len(provider.publish_calls) == 1
    assert len(provider.observe_calls) == 1
    assert len(provider.table_publish_calls) == 1
    assert len(provider.table_observe_calls) == 1


def test_definition_reader_returns_only_an_exact_terminal_publication() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    product_ref = intent.product_metadata.product_ref

    assert (
        repository.definition_for_reference(tenant_id=intent.tenant_id, product_ref=product_ref)
        is None
    )

    ProductCatalogPublicationExecutionService(
        repository=repository,
        provider=_RecordingProductCatalogProvider(),
        clock=lambda: NOW,
    ).publish(intent=intent)

    assert repository.definition_for_reference(
        tenant_id=intent.tenant_id, product_ref=product_ref
    ) == product_catalog_definition(intent)
    assert (
        repository.definition_for_reference(tenant_id="tenant-b", product_ref=product_ref) is None
    )
    assert (
        repository.definition_for_reference(
            tenant_id=intent.tenant_id,
            product_ref=product_ref.model_copy(update={"digest": "f" * 64}),
        )
        is None
    )


def test_transient_table_failure_retries_from_durable_authority_without_receipt() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()
    provider.table_failure = ProviderError("catalog unavailable", "transient_unavailable")
    service = ProductCatalogPublicationExecutionService(
        repository=repository, provider=provider, clock=lambda: NOW
    )

    with pytest.raises(ProductCatalogPublicationProviderError) as failure:
        service.publish(intent=intent)

    assert failure.value.classification == "transient_unavailable"
    assert (
        repository.load_native_table_definition(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
        is not None
    )
    assert (
        repository.load_receipt(tenant_id=intent.tenant_id, operation_id=intent.operation_id)
        is None
    )

    receipt = service.publish(intent=intent)

    assert receipt.native_table_observation_digest == digest(
        repository.load_native_table_observation(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
    )
    assert len(provider.publish_calls) == 2
    assert len(provider.table_publish_calls) == 2


def test_transient_catalog_failure_retries_the_persisted_definition() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()
    provider.failure = ProviderError("catalog unavailable", "transient_unavailable")
    service = ProductCatalogPublicationExecutionService(
        repository=repository, provider=provider, clock=lambda: NOW
    )

    with pytest.raises(ProductCatalogPublicationProviderError) as failure:
        service.publish(intent=intent)

    assert failure.value.classification == "transient_unavailable"
    assert repository.load_definition(
        tenant_id=intent.tenant_id, operation_id=intent.operation_id
    ) == product_catalog_definition(intent)
    assert (
        repository.load_receipt(tenant_id=intent.tenant_id, operation_id=intent.operation_id)
        is None
    )

    receipt = service.publish(intent=intent)

    assert receipt.operation_id == intent.operation_id
    assert provider.publish_calls == [
        product_catalog_definition(intent),
        product_catalog_definition(intent),
    ]


def test_execution_rejects_a_changed_definition_authority_before_provider_effect() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()
    changed = intent.model_copy(
        update={
            "definition_authority": intent.definition_authority.model_copy(
                update={"description": "A different description."}
            )
        }
    )

    with pytest.raises(ValueError, match="operation identity does not match"):
        ProductCatalogPublicationExecutionService(
            repository=repository, provider=provider, clock=lambda: NOW
        ).publish(intent=changed)

    assert provider.publish_calls == []


def test_execution_rejects_round_trip_substitution_without_storing_a_receipt() -> None:
    repository = SQLiteProductCatalogPublicationRepository(":memory:")
    intent = _prepared(repository)
    provider = _RecordingProductCatalogProvider()
    definition = product_catalog_definition(intent)
    substituted = definition.model_copy(update={"description": "Provider-side substitution."})
    provider.observation_override = _catalog_observation(substituted)

    with pytest.raises(ProductCatalogPublicationProviderError) as failure:
        ProductCatalogPublicationExecutionService(
            repository=repository, provider=provider, clock=lambda: NOW
        ).publish(intent=intent)

    assert failure.value.classification == "invalid_provider_response"
    assert (
        repository.load_receipt(tenant_id=intent.tenant_id, operation_id=intent.operation_id)
        is None
    )
