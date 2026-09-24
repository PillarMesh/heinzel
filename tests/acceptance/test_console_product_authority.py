from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_compiler import ProductGenerationReference, QueryReference
from heinzel_contract_model import ArtifactReference
from heinzel_contract_model import digest as artifact_digest
from heinzel_contract_service import (
    SourceFreshnessObservation,
    SQLiteSourceFreshnessObservationRepository,
)
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from heinzel_runtime import (
    AnswerProductGenerationReference,
    MaterializationObservation,
    MaterializationRequest,
    ProductInputCardinalityEvidence,
    ProductInputReceiptCardinality,
    ProductMaterializationAdmission,
    ProductMaterializationReceipt,
    ProductMaterializationRunner,
    QueryGenerationState,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from heinzel_semantic_registry import (
    ApprovedProductVersionMetadata,
    SQLiteApprovedProductVersionRepository,
)

from tests.acceptance.console_product_authority import DurableProductAnswerAuthorityReader

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
TENANT = "tenant-a"
INPUT_GENERATION = "1" * 64
PRODUCT = QueryReference(artifact_id="product:revenue", version=4, digest="2" * 64)
GENERATION = ProductGenerationReference(product_ref=PRODUCT, generation=7)
_AUTHORIZATION_SIGNER = ProductExecutionAuthorizationSigner.generate("runtime-authority-1")
_LEGALITY_DECISION_DIGEST = "a" * 64


def _physical_plan() -> ProductPhysicalPlan:
    statement = 'SELECT 1 AS "revenue_total"'
    return ProductPhysicalPlan(
        compiler_version="compiler-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id=TENANT,
        product_id=PRODUCT.artifact_id,
        product_revision=PRODUCT.version,
        contract_ref="contract-a",
        contract_revision=1,
        contract_digest="7" * 64,
        iir_digest="4" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=1,
        provider_observation_digest="5" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="revenue",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="9" * 64,
            landing_receipt_digest=INPUT_GENERATION,
            observed_source_schema_digest="6" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="amount", json_field="amount", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(namespace="products", relation_name="revenue"),
        emitted_statement=statement,
        statement_digest=artifact_digest(statement),
        output_columns=("revenue_total",),
        expected_output_schema_digest="3" * 64,
        decimal_output_checks=(Decimal57OutputCheck(column_name="revenue_total"),),
    )


class _Warehouse:
    def __init__(
        self,
        *,
        quality_disposition: str = "passed",
        quality_assertion_count: int = 2,
    ) -> None:
        self._quality_disposition = quality_disposition
        self._quality_assertion_count = quality_assertion_count

    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        del request
        return MaterializationObservation.model_validate(
            {
                "provider_commit_reference": "warehouse-commit-7",
                "output_schema_digest": "3" * 64,
                "output_row_count": 10,
                "dbt_manifest_digest": "4" * 64,
                "dbt_run_results_digest": "5" * 64,
                "lineage_digest": "6" * 64,
                "quality_assertion_count": self._quality_assertion_count,
                "quality_disposition": self._quality_disposition,
            }
        )

    def switch_consumption_view(
        self, request: MaterializationRequest, observation: MaterializationObservation
    ) -> None:
        del request, observation


class _Catalog:
    def publish(
        self, request: MaterializationRequest, receipt: ProductMaterializationReceipt
    ) -> str:
        del request, receipt
        return "publication:revenue:4"


class _GenerationAuthority:
    def __init__(self, state: QueryGenerationState | None = None) -> None:
        self._state = state or QueryGenerationState(addressable=True, current_generation=7)

    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState:
        del reference
        return self._state


def _materializations(
    *, quality_disposition: str = "passed", quality_assertion_count: int = 2
) -> ProductMaterializationRunner:
    physical_plan = _physical_plan()
    receipt_cardinality = ProductInputReceiptCardinality(
        generation_id="9" * 64,
        receipt_digest=INPUT_GENERATION,
        record_count=10,
    )
    cardinality_evidence = ProductInputCardinalityEvidence(
        tenant_id=TENANT,
        contract_ref="contract-a",
        contract_revision=1,
        contract_digest="7" * 64,
        product_plan_digest=artifact_digest(physical_plan),
        relation_ref="raw_revenue",
        generation_ids=(receipt_cardinality.generation_id,),
        receipts=(receipt_cardinality,),
        total_contributing_row_ceiling=10,
        policy_maximum_contributing_rows=10,
        maximum_scaled_sum=10 * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=NOW,
    )
    cardinality_repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    cardinality_evidence_digest = cardinality_repository.record(cardinality_evidence)
    admission = ProductMaterializationAdmission(
        legality_decision_digest=_LEGALITY_DECISION_DIGEST,
        cardinality_evidence_digest=cardinality_evidence_digest,
        signed_execution_authorization=_AUTHORIZATION_SIGNER.sign(
            physical_plan=physical_plan,
            legality_decision_digest=_LEGALITY_DECISION_DIGEST,
            cardinality_evidence_digest=cardinality_evidence_digest,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        ),
    )
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(
            quality_disposition=quality_disposition,
            quality_assertion_count=quality_assertion_count,
        ),
        catalog=_Catalog(),
        cardinality_evidence_reader=cardinality_repository,
        execution_authorization_verifier=ProductExecutionAuthorizationVerifier(
            {"runtime-authority-1": _AUTHORIZATION_SIGNER.public_key}
        ),
        clock=lambda: NOW,
    )
    runner.materialize(
        MaterializationRequest(
            run_id="run-7",
            tenant_id=TENANT,
            product_id=PRODUCT.artifact_id,
            product_revision=PRODUCT.version,
            product_generation=GENERATION.generation,
            retention_seconds=3_600,
            contract_digest="7" * 64,
            physical_plan=physical_plan,
            physical_plan_digest=artifact_digest(physical_plan),
            compiled_model_digest="8" * 64,
            input_generation_digests=(INPUT_GENERATION,),
            input_cardinality_evidence_digest=cardinality_evidence_digest,
            expected_output_schema_digest="3" * 64,
        ),
        admission=admission,
    )
    return runner


def _metadata(
    materializations: ProductMaterializationRunner,
    *,
    tenant_id: str = TENANT,
    product_ref: QueryReference = PRODUCT,
    lineage_digest: str = "6" * 64,
) -> ApprovedProductVersionMetadata:
    receipt = materializations.read_receipt(
        tenant_id=TENANT,
        product_id=PRODUCT.artifact_id,
        product_revision=PRODUCT.version,
        product_generation=GENERATION.generation,
    )
    assert receipt is not None
    return ApprovedProductVersionMetadata(
        tenant_id=tenant_id,
        product_ref=ArtifactReference.model_validate(product_ref.model_dump(mode="python")),
        generation=GENERATION.generation,
        contract_ref=ArtifactReference(
            artifact_id="contract:revenue", version=1, digest=receipt.contract_digest
        ),
        semantic_version_ref=ArtifactReference(
            artifact_id="semantic:revenue", version=1, digest="a" * 64
        ),
        materialization_receipt_ref=ArtifactReference(
            artifact_id=receipt.run_id,
            version=receipt.product_generation,
            digest=artifact_digest(receipt),
        ),
        lineage_digest=lineage_digest,
        approved_narrative_terms=("revenue", "region"),
        recorded_at=receipt.committed_at,
    )


def _freshness(
    *, tenant_id: str = TENANT, watermark_at: datetime = NOW - timedelta(minutes=5)
) -> SQLiteSourceFreshnessObservationRepository:
    repository = SQLiteSourceFreshnessObservationRepository(":memory:")
    repository.store(
        SourceFreshnessObservation(
            observation_id="freshness-7",
            tenant_id=tenant_id,
            version=1,
            source_ref="source:finance",
            input_generation_digest=INPUT_GENERATION,
            data_observation_ref=ArtifactReference(
                artifact_id="acquisition-generation-7",
                version=1,
                digest="9" * 64,
            ),
            watermark_at=watermark_at,
            observed_at=NOW,
        )
    )
    return repository


def _reader(
    *,
    materializations: ProductMaterializationRunner | None = None,
    freshness: SQLiteSourceFreshnessObservationRepository | None = None,
    metadata: ApprovedProductVersionMetadata | None = None,
    generations: _GenerationAuthority | None = None,
) -> DurableProductAnswerAuthorityReader:
    materialization_reader = materializations or _materializations()
    metadata_repository = SQLiteApprovedProductVersionRepository(":memory:")
    metadata_repository.store(metadata or _metadata(materialization_reader))
    return DurableProductAnswerAuthorityReader(
        materializations=materialization_reader,
        freshness=freshness or _freshness(),
        product_metadata=metadata_repository,
        generations=generations or _GenerationAuthority(),
        clock=lambda: NOW,
    )


def test_product_authority_uses_measured_watermark_and_actual_dbt_quality() -> None:
    authority = _reader().read_current(tenant_id=TENANT, product_generation_refs=(GENERATION,))

    assert authority is not None
    assert authority.product_generation_refs == (GENERATION,)
    assert authority.as_of == NOW - timedelta(minutes=5)
    assert authority.as_of != NOW
    assert authority.freshness_disposition == "current"
    assert authority.quality_blocked is False
    assert authority.material_quality_limitations == ()
    assert authority.lineage_refs == (
        ArtifactReference(
            artifact_id="product:revenue:lineage",
            version=7,
            digest="6" * 64,
        ),
    )
    assert authority.approved_narrative_terms == ("revenue", "region")
    assert authority.freshness_observation_ref
    assert authority.quality_observation_ref


@pytest.mark.parametrize(
    ("quality_disposition", "quality_assertion_count"),
    (("not_asserted", 0), ("passed", 0)),
)
def test_product_authority_denies_quality_without_actual_assertions(
    quality_disposition: str, quality_assertion_count: int
) -> None:
    authority = _reader(
        materializations=_materializations(
            quality_disposition=quality_disposition,
            quality_assertion_count=quality_assertion_count,
        )
    ).read_current(tenant_id=TENANT, product_generation_refs=(GENERATION,))

    assert authority is None


def test_product_authority_denies_missing_source_watermark() -> None:
    authority = _reader(
        freshness=SQLiteSourceFreshnessObservationRepository(":memory:")
    ).read_current(tenant_id=TENANT, product_generation_refs=(GENERATION,))

    assert authority is None


def test_product_authority_exposes_limited_dbt_quality_as_a_blocked_limitation() -> None:
    authority = _reader(
        materializations=_materializations(quality_disposition="limited", quality_assertion_count=2)
    ).read_current(tenant_id=TENANT, product_generation_refs=(GENERATION,))

    assert authority is not None
    assert authority.quality_blocked is True
    assert authority.material_quality_limitations == (
        ArtifactReference(
            artifact_id="product:revenue:dbt-run-results",
            version=7,
            digest="5" * 64,
        ),
    )


@pytest.mark.parametrize(
    "reader",
    (
        _reader(metadata=_metadata(_materializations(), tenant_id="tenant-b")),
        _reader(metadata=_metadata(_materializations(), lineage_digest="a" * 64)),
        _reader(
            metadata=_metadata(_materializations()).model_copy(
                update={
                    "materialization_receipt_ref": ArtifactReference(
                        artifact_id="run-7", version=7, digest="b" * 64
                    )
                }
            )
        ),
        _reader(
            generations=_GenerationAuthority(
                QueryGenerationState(addressable=False, current_generation=8)
            )
        ),
    ),
)
def test_product_authority_denies_mismatched_or_unavailable_product_evidence(
    reader: DurableProductAnswerAuthorityReader,
) -> None:
    assert reader.read_current(tenant_id=TENANT, product_generation_refs=(GENERATION,)) is None


def test_product_authority_denies_cross_tenant_read_without_enumeration() -> None:
    reader = _reader()

    assert reader.read_current(tenant_id="tenant-b", product_generation_refs=(GENERATION,)) is None
