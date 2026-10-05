"""The product authority the demonstration publishes a committed generation into.

A materialized generation nobody published is not consumable: the governed answer reads the
approved product version, its approved query binding and the catalog publication, and refuses
when any of the three is missing. This module composes those three authorities over the
demonstration's own stores, so the generation the materialization commits becomes one the
answer path can resolve.

Everything here is the owning service's own composition: `compose_authoritative_product_catalog`
does the wiring and the checking, and what this module supplies is the demonstration's data --
which columns mean which approved terms, who approved the binding, and what the source
watermark was. Two things are deliberately local stand-ins, and are named as such below: the
catalog provider, and the binding authorities that would otherwise be control-plane services.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from heinzel_catalog_control import CatalogBinding
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    ManagedIntegrationContract,
    SemanticObject,
    digest,
)
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
)
from heinzel_runtime import (
    AuthoritativeProductCatalog,
    ProductCatalogCompositionConfig,
    compose_authoritative_product_catalog,
)
from heinzel_semantic_registry import (
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductQueryBindingApproval,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState

from .publication import (
    DEMO_PRODUCT_NAME,
    DEMO_SOURCE_NAME,
    DEMO_TENANT_ID,
    DEMO_WAREHOUSE_BINDING_ID,
    demo_catalog_binding,
    demo_placeholder_digest,
)
from .stores import DemoStores

__all__ = [
    "DEMO_DIMENSION_TERM_ID",
    "DEMO_METRIC_TERM_ID",
    "DemoProductCatalogProvider",
    "compose_demo_product_catalog",
    "demo_catalog_definition_authority",
    "demo_dimension_reference",
    "demo_metric_reference",
    "demo_query_binding_declaration",
    "demo_semantic_version_reference",
    "demo_source_freshness_observation",
    "demo_warehouse_binding",
]

# The approved terms the product's two columns stand for. These are `object_id`s of the
# demonstration's semantic version: the query binding authority refuses a binding naming
# anything that is not a term of the version the contract approves, so a rename there and
# not here is a refusal rather than a mismatch nobody notices.
DEMO_METRIC_TERM_ID = "daily-order-value"
DEMO_DIMENSION_TERM_ID = "order_day"

_PROVIDER_VERSION = "demonstration-product-catalog-1"
_APPROVAL_ID = "query-binding-approval-demo-1"


def _term_reference(
    objects: tuple[SemanticObject, ...], object_id: str, version: int
) -> ArtifactReference:
    """The reference the query binding authority will compare against, for one approved term."""
    for item in objects:
        if item.object_id == object_id:
            return ArtifactReference(artifact_id=object_id, version=version, digest=digest(item))
    raise ValueError(f"the demonstration's semantic version publishes no term {object_id!r}")


def demo_semantic_version_reference(
    semantic_version: ApprovedSemanticVersion,
) -> ArtifactReference:
    """The reference the contract binds and every answer authority compares against."""
    return ArtifactReference(
        artifact_id=semantic_version.semantic_version_id,
        version=semantic_version.version,
        digest=digest(semantic_version),
    )


def demo_metric_reference(semantic_version: ApprovedSemanticVersion) -> ArtifactReference:
    """The approved metric the product measures and the answer is asked for."""
    return _term_reference(semantic_version.metrics, DEMO_METRIC_TERM_ID, semantic_version.version)


def demo_dimension_reference(semantic_version: ApprovedSemanticVersion) -> ArtifactReference:
    """The approved dimension the product groups by and the answer is broken down over."""
    return _term_reference(
        semantic_version.entities, DEMO_DIMENSION_TERM_ID, semantic_version.version
    )


def demo_query_binding_declaration(
    semantic_version: ApprovedSemanticVersion,
    *,
    namespace: str,
    relation_name: str,
    group_column: str,
    measure_column: str,
) -> ProductQueryBindingDeclaration:
    """Which column answers which approved term, for the product's one generation.

    `namespace` and `relation_name` are where the materialization put the product, not a name
    chosen here: the publication intent refuses a declaration that names a relation other than
    the one the catalog definition and the warehouse hierarchy agree on.
    """
    dimension_ref = demo_dimension_reference(semantic_version)
    return ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace=namespace,
        relation_name=relation_name,
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=demo_metric_reference(semantic_version),
                aggregate="sum",
                column_name=measure_column,
                output_name=measure_column,
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=dimension_ref,
                semantic_kind="entity",
                column_name=group_column,
                output_name=group_column,
            ),
        ),
        # The day is what a row of this product is about, so it is the entity a disclosure
        # decision is made against. The product has one dimension, so there is no other
        # candidate; a product with more would need the owner to say which.
        disclosure_entity_ref=dimension_ref,
        disclosure_entity_column=group_column,
    )


def demo_catalog_definition_authority(
    *, namespace: str, relation_name: str, group_column: str, measure_column: str
) -> ProductCatalogDefinitionAuthority:
    """What the catalog says this product is, in the words a reader of the catalog sees.

    The column set is checked against the query binding's rather than merely described: the
    publication intent refuses a definition whose columns are not exactly the bound ones, so a
    column documented here and bound nowhere cannot be published.
    """
    return ProductCatalogDefinitionAuthority(
        name="Daily order value",
        description="Confirmed customer order value, summed per calendar day.",
        owner_refs=(f"owner:{DEMO_PRODUCT_NAME}",),
        namespace=namespace,
        relation_name=relation_name,
        columns=(
            ProductCatalogColumnAuthority(
                name=group_column,
                type_name="TEXT",
                nullable=True,
                description="The calendar day the orders were placed on.",
            ),
            ProductCatalogColumnAuthority(
                name=measure_column,
                type_name="NUMERIC",
                nullable=True,
                description="The summed value of the orders placed that day.",
            ),
        ),
    )


def demo_source_freshness_observation(
    *,
    landing_receipt_digest: str,
    generation_id: str,
    watermark_at: datetime,
    observed_at: datetime,
) -> SourceFreshnessObservation:
    """How current the landed source was, bound to the one generation the product reads.

    The answer path refuses a generation it can find no freshness observation for, so this is
    not decoration: without it the demonstration materializes a product and then declines to
    answer over it.

    `watermark_at` is the demonstration's seeded data's own watermark, not the moment it was
    read. The two differ by however long the demonstration has existed, which is exactly what a
    watermark is for.
    """
    if watermark_at > observed_at:
        raise ValueError(
            "the demonstration's source watermark follows its observation: the seeded data is "
            "dated after the clock reading it"
        )
    return SourceFreshnessObservation(
        observation_id=digest(
            {
                "domain": "heinzel-demonstration-source-freshness-v1",
                "source_ref": DEMO_SOURCE_NAME,
                "input_generation_digest": landing_receipt_digest,
            }
        ),
        tenant_id=DEMO_TENANT_ID,
        version=1,
        source_ref=DEMO_SOURCE_NAME,
        input_generation_digest=landing_receipt_digest,
        data_observation_ref=ArtifactReference(
            artifact_id=generation_id, version=1, digest=landing_receipt_digest
        ),
        watermark_at=watermark_at,
        observed_at=observed_at,
    )


def demo_warehouse_binding(*, now: datetime) -> WarehouseBinding:
    """The managed warehouse binding the demonstration's product lives in.

    A local stand-in for what warehouse control would own. It is `READY` and PostgreSQL because
    the demonstration provisioned exactly that cluster itself; the publication refuses a binding
    that is neither, so this cannot claim a warehouse the demonstration does not have.
    """
    return WarehouseBinding(
        binding_id=DEMO_WAREHOUSE_BINDING_ID,
        tenant_id=DEMO_TENANT_ID,
        engine_kind=EngineKind.POSTGRESQL,
        region="local",
        capability_profile_digest=demo_placeholder_digest("warehouse-capability-profile"),
        lifecycle_state=WarehouseBindingState.READY,
        revision=1,
        created_at=now,
        updated_at=now,
        provisioned_at=now,
    )


@dataclass(frozen=True, slots=True)
class _StaticCatalogBindingAuthority:
    """The one catalog binding the demonstration has, in the shape the composition reads."""

    binding: CatalogBinding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if tenant_id != self.binding.tenant_id or binding_id != self.binding.binding_id:
            raise KeyError((tenant_id, binding_id))
        return self.binding


@dataclass(frozen=True, slots=True)
class _StaticWarehouseBindingAuthority:
    """The one warehouse binding the demonstration has, in the shape the composition reads."""

    binding: WarehouseBinding

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if tenant_id != self.binding.tenant_id or binding_id != self.binding.binding_id:
            return None
        return self.binding


class DemoProductCatalogProvider:
    """The demonstration's catalog provider: it holds what it was given and hands it back.

    The publication requires a round trip -- what the provider was told to publish must be
    readable back from it, identically -- so a provider is not optional even with no catalog
    service to talk to. This one makes that round trip true by construction, which proves the
    publication's own accounting rather than any provider's behaviour.

    It is memory-resident on purpose. The durable record of the publication is the receipt in
    `DemoStores.product_publications`, and the execution service returns that receipt without
    reaching a provider at all once it exists, so restarting the demonstration over an existing
    state directory does not need this to remember anything.
    """

    # The only kind the artifacts admit, and not what this is. See `demo_catalog_binding`.
    provider_kind: Literal["openmetadata"] = "openmetadata"

    def __init__(self) -> None:
        self._product: CatalogProductDefinition | None = None
        self._table: CatalogNativeTableDefinition | None = None

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        self._product = definition
        return self._product_observation(definition)

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        definition = self._product
        if (
            definition is None
            or definition.tenant_id != tenant_id
            or definition.stable_external_key != stable_external_key
        ):
            raise KeyError((tenant_id, stable_external_key))
        return self._product_observation(definition)

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self._table = definition
        return self._table_observation(definition)

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        if self._table != definition:
            raise KeyError(definition.warehouse.table_name)
        return self._table_observation(definition)

    @staticmethod
    def _product_observation(definition: CatalogProductDefinition) -> CatalogProductObservation:
        return CatalogProductObservation(
            tenant_id=definition.tenant_id,
            stable_external_key=definition.stable_external_key,
            definition=definition,
            definition_digest=digest(definition),
            provider_version=_PROVIDER_VERSION,
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
            provider_version=_PROVIDER_VERSION,
        )


def compose_demo_product_catalog(
    *,
    stores: DemoStores,
    contract: ManagedIntegrationContract,
    semantic_version: ApprovedSemanticVersion,
    database_name: str,
    namespace: str,
    relation_name: str,
    group_column: str,
    measure_column: str,
    generation: int,
    freshness_observation: SourceFreshnessObservation,
    clock: Callable[[], datetime],
) -> AuthoritativeProductCatalog:
    """Wire the demonstration's product authorities into the catalog it publishes through.

    The result is a `MaterializationCatalog`: the materialization runner calls it once, with the
    request and the receipt it just committed, and it records the approved product version, the
    approved query binding and the catalog publication under that one generation. Those are the
    three the governed answer reads, and the materialization is not consumable without them.

    The freshness observation is stored here rather than left to the caller, because the
    publication names it and the answer path reads it back by generation digest: an observation
    that existed only inside this composition would publish a reference to nothing.
    """
    now = clock()
    catalog_binding = demo_catalog_binding(now=now)
    declaration = demo_query_binding_declaration(
        semantic_version,
        namespace=namespace,
        relation_name=relation_name,
        group_column=group_column,
        measure_column=measure_column,
    )
    stores.source_freshness.store(freshness_observation)
    return compose_authoritative_product_catalog(
        config=ProductCatalogCompositionConfig(
            catalog_binding_id=catalog_binding.binding_id,
            database_name=database_name,
        ),
        warehouse_bindings=_StaticWarehouseBindingAuthority(demo_warehouse_binding(now=now)),
        catalog_bindings=_StaticCatalogBindingAuthority(catalog_binding),
        publication_repository=stores.product_publications,
        publication_provider=DemoProductCatalogProvider(),
        product_version_repository=stores.product_versions,
        query_binding_repository=stores.query_bindings,
        contract=contract,
        semantic_version=semantic_version,
        definition_authority=demo_catalog_definition_authority(
            namespace=namespace,
            relation_name=relation_name,
            group_column=group_column,
            measure_column=measure_column,
        ),
        query_binding_declaration=declaration,
        query_binding_approvals=(
            ProductQueryBindingApproval(
                approval_id=_APPROVAL_ID,
                tenant_id=contract.tenant_id,
                product_ref=ArtifactReference(
                    artifact_id=contract.destination_product.product_name,
                    version=contract.version,
                    digest=digest(contract.destination_product),
                ),
                generation=generation,
                declaration_digest=digest(declaration),
                # The approver the contract requires. A binding approved by anyone else is
                # refused, so this cannot stand in for an approval the contract does not name.
                authority_ref=f"owner:{DEMO_PRODUCT_NAME}",
                actor_id="product-owner-demo-1",
                decision="approve",
                created_at=now,
            ),
        ),
        source_freshness_observations=(freshness_observation,),
        clock=clock,
    )
