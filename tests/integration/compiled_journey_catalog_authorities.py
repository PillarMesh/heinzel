"""Product authorities for the compiled product journey's catalog publication.

The compiled journey materializes the compiler's own guarded statement and then has nothing to
publish it through: it passes a catalog that always refuses, so the run stops at
``publication_pending``. Publishing for real needs the authorities the catalog adapter validates
against — an approved contract, its semantic version, an approved query binding, and a ready
warehouse binding — built at the journey's own identifiers rather than at placeholders.

``AuthoritativeProductCatalog`` refuses a receipt whose
``(tenant_id, product_id, product_revision, contract_digest)`` does not equal
``(contract.tenant_id, contract.destination_product.product_name, contract.version,
digest(contract))``. The last of those is why this module exists and why the journey cannot keep a
hand-written contract digest: no literal can equal the digest of a contract, so the contract has to
be real and the journey's digest has to be computed from it.

Everything here is pure: no engine, no container, no network. The one input that cannot be known
until the journey has run is the landing receipt digest, which the catalog requires the source
freshness observation to agree with, so it is a parameter rather than a constant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heinzel_catalog_control import CatalogBinding
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
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_runtime import AuthoritativeProductCatalog
from heinzel_semantic_registry import (
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationExecutionService,
    ProductCatalogPublicationProvider,
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
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState

# The authorities are approval records, not observations, so they are dated rather than clocked:
# a digest that moved with wall-clock time could not be named by the journey's request.
AUTHORITY_RECORDED_AT = datetime(2026, 9, 14, 12, tzinfo=UTC)

# The journey's product groups a dimension and a summed measure. The measure's column name is the
# one the compiler emits and the one dbt asserts the magnitude of, so it is shared rather than
# restated: a disagreement here would be refused by the query binding authority, not by a test.
DIMENSION_COLUMN = "region"
MEASURE_COLUMN = "total_revenue"


class _WarehouseBindings:
    """A warehouse binding authority over exactly one ready binding."""

    def __init__(self, binding: WarehouseBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if (tenant_id, binding_id) != (self._binding.tenant_id, self._binding.binding_id):
            return None
        return self._binding


class _CatalogBindings:
    """A catalog binding authority over exactly one binding."""

    def __init__(self, binding: CatalogBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if (tenant_id, binding_id) != (self._binding.tenant_id, self._binding.binding_id):
            raise KeyError((tenant_id, binding_id))
        return self._binding


@dataclass(frozen=True)
class JourneyProductAuthorities:
    """The approved records the journey's materialization must agree with to be publishable."""

    contract: ManagedIntegrationContract
    semantic_version: ApprovedSemanticVersion
    query_binding_declaration: ProductQueryBindingDeclaration
    query_binding_approval: ProductQueryBindingApproval
    definition_authority: ProductCatalogDefinitionAuthority
    warehouse_binding: WarehouseBinding

    @property
    def contract_digest(self) -> str:
        """The digest the journey's materialization request must carry."""

        return digest(self.contract)


def journey_product_authorities(
    *,
    tenant_id: str,
    product_id: str,
    warehouse_binding_id: str,
    warehouse_binding_revision: int,
    namespace: str,
    relation_name: str,
) -> JourneyProductAuthorities:
    """Build the approved authorities for one compiled-journey product."""

    semantic_version = ApprovedSemanticVersion(
        semantic_version_id="semantic-compiled-journey-revenue",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-compiled-journey-revenue", version=1, digest="4" * 64
        ),
        candidate_set_digest="5" * 64,
        review_bundle_digest="6" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="An approved reporting region.",
                source_refs=("source-compiled-journey-sales",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(
            SemanticObject(
                object_id="total-revenue",
                name="Total revenue",
                definition="Revenue summed over the approved reporting region.",
                source_refs=("source-compiled-journey-sales",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-compiled-journey",),
        created_at=AUTHORITY_RECORDED_AT,
    )
    contract = ManagedIntegrationContract(
        contract_id="contract-compiled-journey-revenue",
        tenant_id=tenant_id,
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
                source_ref="sales.amount",
                semantic_ref="total-revenue",
                transformation="derived",
            ),
        ),
        integrity_constraints=(),
        destination_product=DestinationProductRequirement(
            product_name=product_id,
            warehouse_binding_id=warehouse_binding_id,
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
        approval_ids=("contract-approval-compiled-journey",),
    )
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
        namespace=namespace,
        relation_name=relation_name,
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=revenue_ref,
                aggregate="sum",
                column_name=MEASURE_COLUMN,
                output_name=MEASURE_COLUMN,
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=region_ref,
                semantic_kind="entity",
                column_name=DIMENSION_COLUMN,
                output_name=DIMENSION_COLUMN,
            ),
        ),
        disclosure_entity_ref=region_ref,
        disclosure_entity_column=DIMENSION_COLUMN,
    )
    approval = ProductQueryBindingApproval(
        approval_id="query-binding-approval-compiled-journey",
        tenant_id=tenant_id,
        product_ref=ArtifactReference(
            artifact_id=contract.destination_product.product_name,
            version=contract.version,
            digest=digest(contract.destination_product),
        ),
        generation=1,
        declaration_digest=digest(declaration),
        authority_ref="owner:revenue",
        actor_id="product-owner-compiled-journey",
        decision="approve",
        created_at=AUTHORITY_RECORDED_AT,
    )
    definition_authority = ProductCatalogDefinitionAuthority(
        name="Current revenue by region",
        description="Approved revenue grouped by reporting region.",
        owner_refs=("owner:revenue",),
        namespace=namespace,
        relation_name=relation_name,
        columns=(
            ProductCatalogColumnAuthority(
                name=DIMENSION_COLUMN,
                type_name="TEXT",
                nullable=False,
                description="An approved reporting region.",
            ),
            ProductCatalogColumnAuthority(
                name=MEASURE_COLUMN,
                type_name="NUMERIC",
                nullable=False,
                description="Revenue summed over the approved reporting region.",
            ),
        ),
    )
    warehouse_binding = WarehouseBinding(
        binding_id=warehouse_binding_id,
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capability_profile_digest="7" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=warehouse_binding_revision,
        created_at=AUTHORITY_RECORDED_AT,
        updated_at=AUTHORITY_RECORDED_AT,
        provisioned_at=AUTHORITY_RECORDED_AT,
    )
    return JourneyProductAuthorities(
        contract=contract,
        semantic_version=semantic_version,
        query_binding_declaration=declaration,
        query_binding_approval=approval,
        definition_authority=definition_authority,
        warehouse_binding=warehouse_binding,
    )


@dataclass(frozen=True)
class ComposedJourneyCatalog:
    """The composed catalog and the repository that holds what it published.

    The repository is returned rather than kept private because the published definition's
    ``stable_external_key`` is the only handle by which the catalog can be asked what it now holds,
    and a publication nobody can look up again is not evidence of anything.
    """

    catalog: AuthoritativeProductCatalog
    publication_repository: SQLiteProductCatalogPublicationRepository


def compose_journey_catalog(
    *,
    authorities: JourneyProductAuthorities,
    catalog_binding: CatalogBinding,
    publication_provider: ProductCatalogPublicationProvider,
    database_name: str,
    landing_receipt_digest: str,
    state_directory: Path,
    clock: Callable[[], datetime] = lambda: AUTHORITY_RECORDED_AT,
) -> ComposedJourneyCatalog:
    """Wire the journey's authorities to a catalog provider.

    The source freshness observation carries ``landing_receipt_digest`` because the catalog
    refuses a publication whose freshness does not account for every input generation the
    materialization consumed. That digest is produced by the journey's own LAND receipt, so it is
    passed in rather than invented here.
    """

    publication_repository = SQLiteProductCatalogPublicationRepository(
        str(state_directory / "product-catalog-publication.sqlite")
    )
    catalog = AuthoritativeProductCatalog(
        warehouse_bindings=_WarehouseBindings(authorities.warehouse_binding),
        catalog_binding=catalog_binding,
        publication_service=ProductCatalogPublicationService(
            repository=publication_repository,
            binding_authority=_CatalogBindings(catalog_binding),
        ),
        publication_execution=ProductCatalogPublicationExecutionService(
            repository=publication_repository,
            provider=publication_provider,
            clock=clock,
        ),
        product_version_authority=ProductVersionAuthorityService(
            SQLiteApprovedProductVersionRepository(
                str(state_directory / "approved-product-versions.sqlite")
            )
        ),
        query_binding_authority=ProductQueryBindingAuthorityService(
            SQLiteProductQueryBindingRepository(
                str(state_directory / "product-query-bindings.sqlite")
            ),
            clock=clock,
        ),
        contract=authorities.contract,
        semantic_version=authorities.semantic_version,
        definition_authority=authorities.definition_authority,
        query_binding_declaration=authorities.query_binding_declaration,
        query_binding_approvals=(authorities.query_binding_approval,),
        source_freshness_observations=(
            SourceFreshnessObservation(
                observation_id="freshness-compiled-journey-sales",
                tenant_id=authorities.contract.tenant_id,
                version=1,
                source_ref="source-compiled-journey-sales",
                input_generation_digest=landing_receipt_digest,
                data_observation_ref=ArtifactReference(
                    artifact_id="source-observation-compiled-journey-sales",
                    version=1,
                    digest="a" * 64,
                ),
                watermark_at=AUTHORITY_RECORDED_AT,
                observed_at=AUTHORITY_RECORDED_AT,
            ),
        ),
        database_name=database_name,
    )
    return ComposedJourneyCatalog(catalog=catalog, publication_repository=publication_repository)
