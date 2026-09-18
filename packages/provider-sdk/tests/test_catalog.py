from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk import (
    CatalogColumn,
    CatalogLineageSource,
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogWarehouseHierarchyAuthority,
    catalog_product_external_key,
    catalog_warehouse_service_external_key,
)
from pydantic import ValidationError


def _definition(**changes: object) -> CatalogProductDefinition:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "idempotency_key": "a" * 64,
        "stable_external_key": catalog_product_external_key(
            tenant_id="tenant-a", product_id="revenue", product_revision=2, generation=7
        ),
        "catalog_binding_id": "catalog-a",
        "catalog_revision": 3,
        "product_id": "revenue",
        "product_revision": 2,
        "generation": 7,
        "name": "Current revenue by region",
        "description": "Approved revenue grouped by region.",
        "owner_refs": ("owner:revenue",),
        "namespace": "analytics",
        "relation_name": "revenue_by_region",
        "columns": (
            CatalogColumn(name="region", type_name="TEXT", nullable=False),
            CatalogColumn(name="revenue", type_name="NUMERIC", nullable=False),
        ),
        "lineage_sources": (
            CatalogLineageSource(
                source_ref="source:orders",
                freshness_observation_ref=ArtifactReference(
                    artifact_id="freshness:orders", version=8, digest="b" * 64
                ),
                freshness_observation_digest="b" * 64,
                watermark_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
                observed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            ),
        ),
        "contract_digest": "c" * 64,
        "semantic_version_digest": "d" * 64,
        "materialization_receipt_ref": ArtifactReference(
            artifact_id="receipt:revenue", version=7, digest="e" * 64
        ),
        "materialization_receipt_digest": "e" * 64,
        "publication_authority_digest": "f" * 64,
    }
    values.update(changes)
    return CatalogProductDefinition.model_validate(values)


def test_catalog_product_definition_carries_complete_publication_authority() -> None:
    definition = _definition()

    assert definition.columns[1].name == "revenue"
    assert definition.lineage_sources[0].freshness_observation_ref.version == 8
    assert definition.catalog_revision == 3
    assert definition.generation == 7
    assert definition.materialization_receipt_digest == "e" * 64


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("stable_external_key", "pm-product-" + "0" * 24, "stable external key"),
        (
            "columns",
            (
                CatalogColumn(name="region", type_name="TEXT", nullable=False),
                CatalogColumn(name="region", type_name="NUMERIC", nullable=False),
            ),
            "column names must be unique",
        ),
        (
            "lineage_sources",
            (
                CatalogLineageSource(
                    source_ref="source:orders",
                    freshness_observation_ref=ArtifactReference(
                        artifact_id="freshness:orders", version=8, digest="b" * 64
                    ),
                    freshness_observation_digest="b" * 64,
                    watermark_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
                    observed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
                ),
                CatalogLineageSource(
                    source_ref="source:orders",
                    freshness_observation_ref=ArtifactReference(
                        artifact_id="freshness:orders", version=9, digest="1" * 64
                    ),
                    freshness_observation_digest="1" * 64,
                    watermark_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
                    observed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
                ),
            ),
            "lineage source references must be unique",
        ),
    ),
)
def test_catalog_product_definition_rejects_identity_and_shape_ambiguity(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _definition(**{field: value})


def test_catalog_product_external_key_is_tenant_scoped_and_deterministic() -> None:
    first = catalog_product_external_key(
        tenant_id="tenant-a", product_id="revenue", product_revision=2, generation=7
    )

    assert first == catalog_product_external_key(
        tenant_id="tenant-a", product_id="revenue", product_revision=2, generation=7
    )
    assert first != catalog_product_external_key(
        tenant_id="tenant-b", product_id="revenue", product_revision=2, generation=7
    )


def _warehouse(**changes: object) -> CatalogWarehouseHierarchyAuthority:
    tenant_id = str(changes.get("tenant_id", "tenant-a"))
    binding_id = str(changes.get("warehouse_binding_id", "warehouse-a"))
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "warehouse_binding_id": binding_id,
        "warehouse_binding_revision": 4,
        "warehouse_provider": "postgresql",
        "database_service_name": catalog_warehouse_service_external_key(
            tenant_id=tenant_id, warehouse_binding_id=binding_id
        ),
        "database_name": "heinzel",
        "schema_name": "analytics",
        "table_name": "revenue_by_region",
    }
    values.update(changes)
    return CatalogWarehouseHierarchyAuthority.model_validate(values)


def test_native_catalog_table_binds_product_to_exact_warehouse_hierarchy() -> None:
    table = CatalogNativeTableDefinition(product=_definition(), warehouse=_warehouse())
    observation = CatalogNativeTableObservation(
        definition=table,
        definition_digest=digest(table),
        table_fully_qualified_name=(
            f"{table.warehouse.database_service_name}.heinzel.analytics.revenue_by_region"
        ),
        provider_version="1.13.3",
    )

    assert observation.definition.warehouse.warehouse_binding_revision == 4
    assert observation.definition.product.columns[1].name == "revenue"


def test_catalog_warehouse_service_rejects_an_unscoped_global_name() -> None:
    with pytest.raises(ValidationError, match="does not match binding authority"):
        _warehouse(database_service_name="postgresql")


@pytest.mark.parametrize(
    ("product", "warehouse", "message"),
    (
        (_definition(), _warehouse(tenant_id="tenant-b"), "share one tenant"),
        (_definition(namespace="private"), _warehouse(), "match warehouse hierarchy"),
        (_definition(relation_name="other_table"), _warehouse(), "match warehouse hierarchy"),
    ),
)
def test_native_catalog_table_rejects_mismatched_warehouse_authority(
    product: CatalogProductDefinition,
    warehouse: CatalogWarehouseHierarchyAuthority,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        CatalogNativeTableDefinition(product=product, warehouse=warehouse)
