"""The demonstration's product authority is the real one, assembled over its own stores.

These need no warehouse. What they ask is whether the authorities the governed answer reads
would accept the demonstration's data: whether its columns name approved terms, whether its
approval is the one the contract requires, and whether the catalog provider's round trip holds.
A warehouse is needed only to produce a receipt to publish, and the live suite does that.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_console.demo.catalog import (
    DEMO_DIMENSION_TERM_ID,
    DEMO_METRIC_TERM_ID,
    DemoProductCatalogProvider,
    compose_demo_product_catalog,
    demo_catalog_definition_authority,
    demo_query_binding_declaration,
    demo_source_freshness_observation,
    demo_warehouse_binding,
)
from heinzel_console.demo.materialization import DEMO_GROUP_COLUMN, DEMO_MEASURE_COLUMN
from heinzel_console.demo.publication import DemoPublication, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import digest
from heinzel_semantic_registry import (
    ProductCatalogDefinitionAuthority,
    ProductQueryBindingDeclaration,
)

_NOW = datetime(2026, 9, 13, tzinfo=UTC)
# When the source these observations describe was last written. Before `_NOW`, because an
# observation reads a source that was already there.
_WATERMARK = _NOW - timedelta(hours=1)
_NAMESPACE = "contract_demo"
_RELATION = "orders_daily_g1"
_RECEIPT_DIGEST = "a" * 64
_GENERATION_ID = "d" * 64


@pytest.fixture(name="stores")
def _stores(tmp_path: Path) -> Iterator[DemoStores]:
    stores = DemoStores(tmp_path / "state")
    try:
        yield stores
    finally:
        stores.close()


@pytest.fixture(name="published")
def _published(stores: DemoStores) -> DemoPublication:
    return build_demo_publication(stores, clock=lambda: _NOW)


def _declaration(published: DemoPublication) -> ProductQueryBindingDeclaration:
    return demo_query_binding_declaration(
        published.semantic_version,
        namespace=_NAMESPACE,
        relation_name=_RELATION,
        group_column=DEMO_GROUP_COLUMN,
        measure_column=DEMO_MEASURE_COLUMN,
    )


def test_the_bound_columns_name_terms_the_publication_approves(published: DemoPublication) -> None:
    """A binding naming an unpublished term is refused, so the two must be checked together."""
    declaration = _declaration(published)

    metric_ids = [binding.semantic_ref.artifact_id for binding in declaration.metric_bindings]
    dimension_ids = [binding.semantic_ref.artifact_id for binding in declaration.dimension_bindings]
    assert metric_ids == [DEMO_METRIC_TERM_ID]
    assert dimension_ids == [DEMO_DIMENSION_TERM_ID]

    # The reference carries the term's own digest, so a reworded definition is a different
    # reference and the binding stops matching rather than silently describing the old meaning.
    metric = next(
        term for term in published.semantic_version.metrics if term.object_id == DEMO_METRIC_TERM_ID
    )
    assert declaration.metric_bindings[0].semantic_ref.digest == digest(metric)


def test_a_term_the_publication_does_not_carry_is_refused(published: DemoPublication) -> None:
    """The guard is here rather than left to the authority, which reports it far from the cause."""
    narrowed = published.semantic_version.model_copy(update={"metrics": ()})

    with pytest.raises(ValueError, match="publishes no term"):
        demo_query_binding_declaration(
            narrowed,
            namespace=_NAMESPACE,
            relation_name=_RELATION,
            group_column=DEMO_GROUP_COLUMN,
            measure_column=DEMO_MEASURE_COLUMN,
        )


def test_the_catalog_definition_documents_exactly_the_bound_columns(
    published: DemoPublication,
) -> None:
    """The publication intent refuses any other set, so an unbound column cannot ship."""
    declaration = _declaration(published)
    definition = demo_catalog_definition_authority(
        namespace=_NAMESPACE,
        relation_name=_RELATION,
        group_column=DEMO_GROUP_COLUMN,
        measure_column=DEMO_MEASURE_COLUMN,
    )

    assert isinstance(definition, ProductCatalogDefinitionAuthority)
    assert {column.name for column in definition.columns} == {
        *(binding.column_name for binding in declaration.metric_bindings),
        *(binding.column_name for binding in declaration.dimension_bindings),
    }
    assert (definition.namespace, definition.relation_name) == (
        declaration.namespace,
        declaration.relation_name,
    )


def test_the_declaration_names_its_one_dimension_as_the_disclosure_entity(
    published: DemoPublication,
) -> None:
    """A row of this product is about a day, so the day is what a disclosure decision is made on."""
    declaration = _declaration(published)

    assert declaration.disclosure_entity_column == DEMO_GROUP_COLUMN
    assert declaration.disclosure_entity_ref == declaration.dimension_bindings[0].semantic_ref


def test_the_freshness_observation_is_bound_to_the_landed_generation() -> None:
    """The answer path looks the observation up by its generation digest, so that is the binding."""
    observation = demo_source_freshness_observation(
        landing_receipt_digest=_RECEIPT_DIGEST,
        generation_id=_GENERATION_ID,
        watermark_at=_WATERMARK,
        observed_at=_NOW,
    )

    assert observation.input_generation_digest == _RECEIPT_DIGEST
    assert observation.data_observation_ref.artifact_id == _GENERATION_ID
    assert observation.watermark_at == _WATERMARK


def test_a_watermark_after_its_reading_is_refused() -> None:
    """Seeded data dated into the future would publish a product claiming to be ahead of now."""
    with pytest.raises(ValueError, match="follows its observation"):
        demo_source_freshness_observation(
            landing_receipt_digest=_RECEIPT_DIGEST,
            generation_id=_GENERATION_ID,
            watermark_at=_WATERMARK,
            observed_at=_WATERMARK - timedelta(seconds=1),
        )


def test_the_catalog_provider_observes_nothing_it_was_not_given() -> None:
    """A provider that answered for an unpublished key would make the round trip prove nothing.

    The round trip itself is asserted by the live suite, which has a real definition to publish:
    `ProductCatalogPublicationExecutionService` refuses the publication outright unless what it
    reads back is identical to what it published.
    """
    provider = DemoProductCatalogProvider()

    with pytest.raises(KeyError):
        provider.observe(tenant_id="tenant-demo", stable_external_key="nothing-published-yet")


def test_the_composed_catalog_stores_the_freshness_it_publishes(
    stores: DemoStores, published: DemoPublication
) -> None:
    """A reference to an observation nobody stored is one the answer path cannot follow."""
    observation = demo_source_freshness_observation(
        landing_receipt_digest=_RECEIPT_DIGEST,
        generation_id=_GENERATION_ID,
        watermark_at=_WATERMARK,
        observed_at=_NOW,
    )

    compose_demo_product_catalog(
        stores=stores,
        contract=published.contract,
        semantic_version=published.semantic_version,
        database_name="heinzel",
        namespace=_NAMESPACE,
        relation_name=_RELATION,
        group_column=DEMO_GROUP_COLUMN,
        measure_column=DEMO_MEASURE_COLUMN,
        generation=1,
        freshness_observation=observation,
        clock=lambda: _NOW,
    )

    assert (
        stores.source_freshness.read_for_generation(
            tenant_id=published.contract.tenant_id,
            input_generation_digest=_RECEIPT_DIGEST,
        )
        == observation
    )


def test_the_warehouse_binding_claims_only_the_warehouse_the_contract_names(
    published: DemoPublication,
) -> None:
    """The publication refuses a binding that is not the contract's, so it cannot drift."""
    binding = demo_warehouse_binding(now=_NOW)

    assert binding.binding_id == published.contract.destination_product.warehouse_binding_id
    assert binding.engine_kind.value in published.contract.destination_product.supported_engines
