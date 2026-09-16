from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_semantic_registry import (
    ApprovedProductQueryBinding,
    ProductQueryBindingApproval,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteProductQueryBindingRepository,
)

from tests.acceptance.console_answer_query_binding import (
    DurableProductQueryBindingReader,
    ProductQueryBindingUnavailable,
)

PRODUCT = ArtifactReference(artifact_id="product-revenue", version=1, digest="1" * 64)
METRIC = ArtifactReference(artifact_id="total-revenue", version=1, digest="2" * 64)
DIMENSION = ArtifactReference(artifact_id="region", version=1, digest="3" * 64)


def _binding() -> ApprovedProductQueryBinding:
    metric = ProductQueryMetricBinding(
        semantic_ref=METRIC,
        aggregate="sum",
        column_name="total_revenue",
        output_name="total_revenue",
    )
    dimension = ProductQueryDimensionBinding(
        semantic_ref=DIMENSION,
        semantic_kind="entity",
        column_name="region",
        output_name="region",
    )
    declaration = ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="consumption",
        relation_name="product_revenue",
        metric_bindings=(metric,),
        dimension_bindings=(dimension,),
        disclosure_entity_ref=DIMENSION,
        disclosure_entity_column="region",
    )
    approval = ProductQueryBindingApproval(
        approval_id="binding-approval-1",
        tenant_id="tenant-a",
        product_ref=PRODUCT,
        generation=7,
        declaration_digest=digest(declaration),
        authority_ref="owner:revenue",
        actor_id="product-owner-a",
        decision="approve",
        created_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
    )
    return ApprovedProductQueryBinding(
        tenant_id="tenant-a",
        product_ref=PRODUCT,
        generation=7,
        contract_ref=ArtifactReference(artifact_id="contract-revenue", version=1, digest="4" * 64),
        semantic_version_ref=ArtifactReference(
            artifact_id="semantic-revenue", version=1, digest="5" * 64
        ),
        materialization_receipt_ref=ArtifactReference(
            artifact_id="materialization-7", version=7, digest="6" * 64
        ),
        lineage_digest="7" * 64,
        declaration_digest=digest(declaration),
        approval_digest=digest((approval,)),
        consumption_object_ref=ArtifactReference(
            artifact_id="product-revenue:consumption",
            version=7,
            digest=digest(
                {
                    "domain": "pillarmesh-product-query-consumption-v1",
                    "tenant_id": "tenant-a",
                    "product_ref": PRODUCT,
                    "generation": 7,
                    "materialization_receipt_ref": ArtifactReference(
                        artifact_id="materialization-7", version=7, digest="6" * 64
                    ),
                    "engine_kind": declaration.engine_kind,
                    "namespace": declaration.namespace,
                    "relation_name": declaration.relation_name,
                }
            ),
        ),
        engine_kind=declaration.engine_kind,
        namespace=declaration.namespace,
        relation_name=declaration.relation_name,
        metric_bindings=declaration.metric_bindings,
        dimension_bindings=declaration.dimension_bindings,
        disclosure_entity_ref=declaration.disclosure_entity_ref,
        disclosure_entity_column=declaration.disclosure_entity_column,
        approvals=(approval,),
        recorded_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
    )


def test_reader_projects_exact_owning_binding_into_compiler_models() -> None:
    repository = SQLiteProductQueryBindingRepository(":memory:")
    repository.store(_binding())

    projected = DurableProductQueryBindingReader(repository).read(
        tenant_id="tenant-a",
        product_ref=PRODUCT,
        generation=7,
        metric_refs=(METRIC,),
        dimension_refs=(DIMENSION,),
    )

    assert projected.engine_kind == "postgresql"
    assert projected.contract_ref == _binding().contract_ref
    assert projected.semantic_version_ref == _binding().semantic_version_ref
    assert projected.materialization_receipt_ref == _binding().materialization_receipt_ref
    assert projected.lineage_digest == _binding().lineage_digest
    assert projected.consumption_object.namespace == "consumption"
    assert projected.consumption_object.relation_name == "product_revenue"
    assert projected.product_generation_ref.generation == 7
    assert projected.metrics[0].metric_ref.model_dump(mode="python") == METRIC.model_dump(
        mode="python"
    )
    assert projected.metrics[0].aggregate == "sum"
    assert projected.dimensions[0].dimension_ref.model_dump(mode="python") == DIMENSION.model_dump(
        mode="python"
    )
    assert projected.disclosure_entity_column == "region"


@pytest.mark.parametrize(
    ("tenant_id", "metric_refs"),
    (
        ("tenant-b", (METRIC,)),
        ("tenant-a", (ArtifactReference(artifact_id="margin", version=1, digest="9" * 64),)),
        ("tenant-a", (METRIC, METRIC)),
    ),
)
def test_reader_denies_missing_cross_tenant_or_ambiguous_binding(
    tenant_id: str,
    metric_refs: tuple[ArtifactReference, ...],
) -> None:
    repository = SQLiteProductQueryBindingRepository(":memory:")
    repository.store(_binding())

    with pytest.raises(ProductQueryBindingUnavailable, match="query binding unavailable"):
        DurableProductQueryBindingReader(repository).read(
            tenant_id=tenant_id,
            product_ref=PRODUCT,
            generation=7,
            metric_refs=metric_refs,
            dimension_refs=(DIMENSION,),
        )
