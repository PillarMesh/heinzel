"""The compiled journey's catalog authorities, proven without an engine or a catalog.

The journey itself needs PostgreSQL and OpenMetadata, so it only runs live. Every agreement the
catalog adapter enforces, though, is pure Python over the journey's own identifiers: the contract
digest, the product reference, the warehouse binding, the engine kind, and the freshness
accounting. Those are checked here, at the values the journey uses, so the live run is left to
prove the one thing it alone can — that the provider publishes over the wire.

The negative cases are the point. A journey that passes a hand-written contract digest cannot
publish, and the test that says so is the reason the journey's digest is computed.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_contract_model import ArtifactReference, digest
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from heinzel_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
)
from heinzel_runtime import MaterializationRequest
from heinzel_runtime.product_materialization import (
    CatalogPublicationError,
    ProductMaterializationReceipt,
)
from heinzel_warehouse_control import WarehouseBindingState

from tests.integration.compiled_journey_catalog_authorities import (
    AUTHORITY_RECORDED_AT,
    DIMENSION_COLUMN,
    MEASURE_COLUMN,
    JourneyProductAuthorities,
    compose_journey_catalog,
    journey_product_authorities,
)
from tests.integration.test_postgresql_product_materialization_live import _land_rows

# The identifiers the compiled journey materializes under. A disagreement between these and the
# journey's own constants is what the live run would discover late; keeping them named here is what
# makes the offline proof about the journey rather than about an invented product.
_TENANT = "tenant-live-a"
_PRODUCT_ID = "product_revenue"
_BINDING_ID = "warehouse-live-a"
_BINDING_REVISION = 1
_NAMESPACE = "consumption"
_RELATION = "product_revenue"
_DATABASE = "heinzel"

# Stands in for the journey's LAND receipt digest, which is produced live.
_LANDING_RECEIPT_DIGEST = "9" * 64


class _RecordingProvider:
    """A publication provider that records what it was asked to publish."""

    provider_kind: Literal["openmetadata"] = "openmetadata"

    def __init__(self) -> None:
        self.product: CatalogProductDefinition | None = None
        self.table: CatalogNativeTableDefinition | None = None

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        self.product = definition
        return self._product_observation(definition)

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        assert self.product is not None
        return self._product_observation(self.product)

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self.table = definition
        return self._table_observation(definition)

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
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


def _authorities() -> JourneyProductAuthorities:
    return journey_product_authorities(
        tenant_id=_TENANT,
        product_id=_PRODUCT_ID,
        warehouse_binding_id=_BINDING_ID,
        warehouse_binding_revision=_BINDING_REVISION,
        namespace=_NAMESPACE,
        relation_name=_RELATION,
    )


def _catalog_binding(
    *, state: CatalogBindingState = CatalogBindingState.READY, tenant_id: str = _TENANT
) -> CatalogBinding:
    return CatalogBinding(
        binding_id="catalog-live-a",
        tenant_id=tenant_id,
        capability_profile_digest="8" * 64,
        lifecycle_state=state,
        revision=3,
        created_at=AUTHORITY_RECORDED_AT,
        updated_at=AUTHORITY_RECORDED_AT,
        provisioned_at=AUTHORITY_RECORDED_AT,
    )


def _physical_plan(authorities: JourneyProductAuthorities) -> ProductPhysicalPlan:
    statement = (
        f'SELECT "{DIMENSION_COLUMN}", SUM("amount") AS "{MEASURE_COLUMN}" '
        f'FROM "raw"."sales" GROUP BY "{DIMENSION_COLUMN}"'
    )
    return ProductPhysicalPlan(
        compiler_version="compiler-1",
        legality_rule_id="PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE",
        legality_rule_version="1",
        tenant_id=_TENANT,
        product_id=_PRODUCT_ID,
        product_revision=1,
        contract_ref=authorities.contract.contract_id,
        contract_revision=authorities.contract.version,
        contract_digest=authorities.contract_digest,
        iir_digest="4" * 64,
        provider="postgresql",
        warehouse_binding_id=_BINDING_ID,
        warehouse_binding_revision=_BINDING_REVISION,
        provider_observation_digest="5" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="sales",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="6" * 64,
            landing_receipt_digest=_LANDING_RECEIPT_DIGEST,
            observed_source_schema_digest="7" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="amount", json_field="amount", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(namespace=_NAMESPACE, relation_name=_RELATION),
        emitted_statement=statement,
        statement_digest=digest(statement),
        output_columns=(DIMENSION_COLUMN, MEASURE_COLUMN),
        expected_output_schema_digest="c" * 64,
        decimal_output_checks=(Decimal57OutputCheck(column_name=MEASURE_COLUMN),),
    )


def _request(
    authorities: JourneyProductAuthorities, *, contract_digest: str | None = None
) -> MaterializationRequest:
    plan = _physical_plan(authorities)
    return MaterializationRequest(
        run_id="run-compiled-journey-1",
        tenant_id=_TENANT,
        product_id=_PRODUCT_ID,
        product_revision=1,
        product_generation=1,
        retention_seconds=3600,
        contract_digest=contract_digest or authorities.contract_digest,
        physical_plan=plan,
        physical_plan_digest=digest(plan),
        compiled_model_digest="d" * 64,
        input_generation_digests=(_LANDING_RECEIPT_DIGEST,),
        input_cardinality_evidence_digest="e" * 64,
        expected_output_schema_digest="c" * 64,
    )


def _receipt(request: MaterializationRequest) -> ProductMaterializationReceipt:
    committed_at = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)
    return ProductMaterializationReceipt(
        run_id=request.run_id,
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        product_revision=request.product_revision,
        product_generation=request.product_generation,
        contract_digest=request.contract_digest,
        input_generation_digests=request.input_generation_digests,
        input_cardinality_evidence_digest=request.input_cardinality_evidence_digest,
        execution_authorization_digest="1" * 64,
        legality_decision_digest="2" * 64,
        physical_plan_digest=request.physical_plan_digest,
        compiled_model_digest=request.compiled_model_digest,
        output_schema_digest=request.expected_output_schema_digest,
        output_row_count=2,
        provider_commit_reference="commit-compiled-journey-1",
        dbt_manifest_digest="3" * 64,
        dbt_run_results_digest="4" * 64,
        lineage_digest="5" * 64,
        quality_assertion_count=3,
        quality_disposition="passed",
        magnitude_asserted_columns=(MEASURE_COLUMN,),
        committed_at=committed_at,
        retained_until=committed_at + timedelta(seconds=request.retention_seconds),
    )


def test_the_journey_identifiers_publish_through_the_composed_catalog(tmp_path: Path) -> None:
    authorities = _authorities()
    provider = _RecordingProvider()
    composed = compose_journey_catalog(
        authorities=authorities,
        catalog_binding=_catalog_binding(),
        publication_provider=provider,
        database_name=_DATABASE,
        landing_receipt_digest=_LANDING_RECEIPT_DIGEST,
        state_directory=tmp_path,
    )
    request = _request(authorities)

    publication_id = composed.catalog.publish(request, _receipt(request))

    assert publication_id
    assert provider.product is not None
    published = provider.product
    assert published.tenant_id == _TENANT
    assert tuple(column.name for column in published.columns) == (
        DIMENSION_COLUMN,
        MEASURE_COLUMN,
    )
    assert published.owner_refs == ("owner:revenue",)
    assert published.generation == 1
    assert published.lineage_sources[0].source_ref == "source-compiled-journey-sales"
    # The handle the live journey reads the publication back by. Proving it resolves here means
    # the live run is only asked to show that the provider stored what it was given.
    recorded = composed.publication_repository.definition_for_reference(
        tenant_id=_TENANT,
        product_ref=ArtifactReference(
            artifact_id=authorities.contract.destination_product.product_name,
            version=authorities.contract.version,
            digest=digest(authorities.contract.destination_product),
        ),
    )
    assert recorded == published


def test_the_published_table_is_the_consumption_relation_the_journey_materializes(
    tmp_path: Path,
) -> None:
    authorities = _authorities()
    provider = _RecordingProvider()
    composed = compose_journey_catalog(
        authorities=authorities,
        catalog_binding=_catalog_binding(),
        publication_provider=provider,
        database_name=_DATABASE,
        landing_receipt_digest=_LANDING_RECEIPT_DIGEST,
        state_directory=tmp_path,
    )
    request = _request(authorities)

    composed.catalog.publish(request, _receipt(request))

    assert provider.table is not None
    warehouse = provider.table.warehouse
    assert warehouse.database_name == _DATABASE
    assert warehouse.schema_name == _NAMESPACE
    assert warehouse.table_name == _RELATION
    assert warehouse.warehouse_binding_revision == _BINDING_REVISION


def test_a_hand_written_contract_digest_cannot_publish(tmp_path: Path) -> None:
    # The digest the journey carried before its contract was real. No literal can equal the digest
    # of an approved contract, so this is the case that forces the journey to compute it.
    authorities = _authorities()
    provider = _RecordingProvider()
    composed = compose_journey_catalog(
        authorities=authorities,
        catalog_binding=_catalog_binding(),
        publication_provider=provider,
        database_name=_DATABASE,
        landing_receipt_digest=_LANDING_RECEIPT_DIGEST,
        state_directory=tmp_path,
    )
    request = _request(authorities, contract_digest="2" * 64)

    with pytest.raises(ValueError, match="does not match approved product authority"):
        composed.catalog.publish(request, _receipt(request))

    assert authorities.contract_digest != "2" * 64
    assert provider.product is None


def test_freshness_that_omits_the_landing_generation_cannot_publish(tmp_path: Path) -> None:
    authorities = _authorities()
    provider = _RecordingProvider()
    composed = compose_journey_catalog(
        authorities=authorities,
        catalog_binding=_catalog_binding(),
        publication_provider=provider,
        database_name=_DATABASE,
        # A freshness observation about some other generation than the one materialized.
        landing_receipt_digest="b" * 64,
        state_directory=tmp_path,
    )
    request = _request(authorities)

    with pytest.raises(ValueError, match="source freshness does not match"):
        composed.catalog.publish(request, _receipt(request))

    assert provider.product is None


def test_a_warehouse_binding_that_is_not_ready_cannot_publish(tmp_path: Path) -> None:
    authorities = _authorities()
    not_ready = authorities.warehouse_binding.model_copy(
        update={"lifecycle_state": WarehouseBindingState.SUSPENDED}
    )
    provider = _RecordingProvider()
    composed = compose_journey_catalog(
        authorities=JourneyProductAuthorities(
            contract=authorities.contract,
            semantic_version=authorities.semantic_version,
            query_binding_declaration=authorities.query_binding_declaration,
            query_binding_approval=authorities.query_binding_approval,
            definition_authority=authorities.definition_authority,
            warehouse_binding=not_ready,
        ),
        catalog_binding=_catalog_binding(),
        publication_provider=provider,
        database_name=_DATABASE,
        landing_receipt_digest=_LANDING_RECEIPT_DIGEST,
        state_directory=tmp_path,
    )
    request = _request(authorities)

    with pytest.raises(CatalogPublicationError, match="managed warehouse binding is not ready"):
        composed.catalog.publish(request, _receipt(request))

    assert provider.product is None


def test_the_journey_lands_its_generation_under_its_own_contract_identity() -> None:
    """The landing helper's defaults are not this journey's identity, so it must pass its own.

    The cardinality resolver requires the landed receipt's ``contract_ref`` and the
    acknowledgement's ``contract_digest`` to equal the ones resolved against. Those agreements are
    already held by the resolver's own tests; what they cannot catch is a journey that resolves
    against one identity and lands under another, because the landing happens live. This reads the
    call instead.

    It is a source-level check because the failure it prevents cost a full live run to find:
    `ProductInputCardinalityAuthorityError: contract digest mismatch`, raised after PostgreSQL had
    been provisioned, rows acquired and landed, and the compiler run.
    """

    journey = ast.parse(
        pathlib.Path(
            "tests/integration/test_postgresql_compiled_product_journey_live.py"
        ).read_text(encoding="utf-8")
    )
    calls = [
        node
        for node in ast.walk(journey)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_land_rows"
    ]

    assert len(calls) == 1
    passed = {
        keyword.arg: keyword.value.id
        for keyword in calls[0].keywords
        if isinstance(keyword.value, ast.Name)
    }
    assert passed.get("contract_ref") == "_CONTRACT_REF"
    assert passed.get("contract_digest") == "_CONTRACT_DIGEST"


def test_the_landing_helper_defaults_are_not_the_journey_identity() -> None:
    # Why the explicit arguments above are load-bearing rather than decoration: the defaults are a
    # hand-written placeholder, and no literal can equal the digest of an approved contract.
    defaults = inspect.signature(_land_rows).parameters

    assert defaults["contract_digest"].default == "2" * 64
    assert defaults["contract_digest"].default != _authorities().contract_digest
    assert defaults["contract_ref"].default != _authorities().contract.contract_id
