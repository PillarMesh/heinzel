from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from heinzel_semantic_registry import (
    ApprovedProductQueryBinding,
    ApprovedProductVersionMetadata,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityError,
    ProductQueryBindingAuthorityService,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteProductQueryBindingRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _semantic_version() -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id="tenant-a",
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-revenue", version=1, digest="1" * 64
        ),
        candidate_set_digest="2" * 64,
        review_bundle_digest="3" * 64,
        entities=(
            SemanticObject(
                object_id="region",
                name="Region",
                definition="The sales region.",
                source_refs=("sales.region",),
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
                definition="The sum of approved revenue values.",
                source_refs=("sales.revenue",),
            ),
        ),
        classifications=(),
        authority_bindings=(),
        approval_ids=("semantic-approval-1",),
        created_at=NOW,
    )


def _reference(value: SemanticObject, *, semantic: ApprovedSemanticVersion) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=value.object_id,
        version=semantic.version,
        digest=digest(value),
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
                source_ref="sales.revenue",
                semantic_ref="total-revenue",
                transformation="derived",
            ),
        ),
        integrity_constraints=(),
        destination_product=DestinationProductRequirement(
            product_name="product-revenue",
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


def _metadata(
    contract: ManagedIntegrationContract,
    semantic: ApprovedSemanticVersion,
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
            artifact_id="materialization-7", version=7, digest="4" * 64
        ),
        lineage_digest="5" * 64,
        approved_narrative_terms=("Total revenue", "Region"),
        recorded_at=NOW,
    )


def _declaration(semantic: ApprovedSemanticVersion) -> ProductQueryBindingDeclaration:
    return ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="consumption",
        relation_name="product_revenue",
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=_reference(semantic.metrics[0], semantic=semantic),
                aggregate="sum",
                column_name="total_revenue",
                output_name="total_revenue",
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=_reference(semantic.entities[0], semantic=semantic),
                semantic_kind="entity",
                column_name="region",
                output_name="region",
            ),
        ),
        disclosure_entity_ref=_reference(semantic.entities[0], semantic=semantic),
        disclosure_entity_column="region",
    )


def _approvals(
    metadata: ApprovedProductVersionMetadata,
    declaration: ProductQueryBindingDeclaration,
) -> tuple[ProductQueryBindingApproval, ...]:
    return (
        ProductQueryBindingApproval(
            approval_id="query-binding-approval-1",
            tenant_id=metadata.tenant_id,
            product_ref=metadata.product_ref,
            generation=metadata.generation,
            declaration_digest=digest(declaration),
            authority_ref="owner:revenue",
            actor_id="product-owner-a",
            decision="approve",
            created_at=NOW,
        ),
    )


def test_query_binding_is_derived_from_exact_approved_product_authority(tmp_path: Path) -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    database_path = tmp_path / "query-bindings.sqlite3"
    repository = SQLiteProductQueryBindingRepository(str(database_path))
    declaration = _declaration(semantic)

    stored = ProductQueryBindingAuthorityService(repository, clock=lambda: NOW).record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=_approvals(metadata, declaration),
    )

    repository.close()
    reopened = SQLiteProductQueryBindingRepository(str(database_path))
    try:
        assert (
            reopened.read_current(
                tenant_id="tenant-a",
                product_ref=metadata.product_ref,
                generation=7,
            )
            == stored
        )
    finally:
        reopened.close()
    assert stored.contract_ref == metadata.contract_ref
    assert stored.semantic_version_ref == metadata.semantic_version_ref
    assert stored.materialization_receipt_ref == metadata.materialization_receipt_ref
    assert stored.lineage_digest == metadata.lineage_digest
    assert stored.consumption_object_ref == ArtifactReference(
        artifact_id="product-revenue:consumption",
        version=7,
        digest=digest(
            {
                "domain": "heinzel-product-query-consumption-v1",
                "tenant_id": "tenant-a",
                "product_ref": metadata.product_ref,
                "generation": 7,
                "materialization_receipt_ref": metadata.materialization_receipt_ref,
                "engine_kind": "postgresql",
                "namespace": "consumption",
                "relation_name": "product_revenue",
            }
        ),
    )


def test_query_binding_reader_classifies_corrupt_persisted_authority() -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    repository = SQLiteProductQueryBindingRepository(":memory:")
    declaration = _declaration(semantic)
    ProductQueryBindingAuthorityService(repository, clock=lambda: NOW).record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=_approvals(metadata, declaration),
    )
    repository._connection.execute(
        "UPDATE approved_product_query_bindings SET payload = ?",
        (b"not-json",),
    )
    repository._connection.commit()

    with pytest.raises(ProductQueryBindingAuthorityError, match="invalid"):
        repository.read_current(
            tenant_id="tenant-a", product_ref=metadata.product_ref, generation=7
        )


def test_query_binding_replays_exactly_and_rejects_changed_relation() -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    repository = SQLiteProductQueryBindingRepository(":memory:")
    service = ProductQueryBindingAuthorityService(repository, clock=lambda: NOW)
    declaration = _declaration(semantic)
    approvals = _approvals(metadata, declaration)

    first = service.record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=approvals,
    )
    replay = service.record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=approvals,
    )

    assert replay.model_dump_json() == first.model_dump_json()
    changed_declaration = declaration.model_copy(update={"relation_name": "other_product"})
    with pytest.raises(ValueError, match="query binding authority is immutable"):
        service.record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=metadata,
            declaration=changed_declaration,
            approvals=_approvals(metadata, changed_declaration),
        )
    changed_metadata = metadata.model_copy(update={"lineage_digest": "6" * 64})
    with pytest.raises(ValueError, match="query binding authority is immutable"):
        service.record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=changed_metadata,
            declaration=declaration,
            approvals=_approvals(changed_metadata, declaration),
        )
    changed_approvals = (approvals[0].model_copy(update={"actor_id": "different-product-owner"}),)
    with pytest.raises(ValueError, match="query binding authority is immutable"):
        service.record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=metadata,
            declaration=declaration,
            approvals=changed_approvals,
        )


def test_query_binding_repository_revalidates_copied_payloads() -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    declaration = _declaration(semantic)
    repository = SQLiteProductQueryBindingRepository(":memory:")
    stored = ProductQueryBindingAuthorityService(repository, clock=lambda: NOW).record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=_approvals(metadata, declaration),
    )

    with pytest.raises(ValueError, match="declaration digest"):
        repository.store(stored.model_copy(update={"relation_name": "tampered_relation"}))


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ("cross_tenant", "approved product authority"),
        ("unapproved", "explicit approval authority"),
        ("foreign_approval", "explicit approval authority"),
        ("wrong_approver", "required owner approval authority"),
        ("metric_as_dimension", "semantic kind"),
        ("metric_unknown", "metric.*semantic kind"),
        ("unsupported_engine", "contract does not support"),
        ("wrong_product_ref", "approved product authority"),
        ("wrong_contract_ref", "approved product authority"),
        ("wrong_semantic_ref", "approved product authority"),
        ("unapproved_contract", "approved contract"),
        ("unapproved_semantic", "approved contract"),
    ),
)
def test_query_binding_denies_unowned_or_unapproved_declarations(
    change: str,
    message: str,
) -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    declaration = _declaration(semantic)
    if change == "cross_tenant":
        metadata = metadata.model_copy(update={"tenant_id": "tenant-b"})
    elif change == "metric_as_dimension":
        declaration = declaration.model_copy(
            update={
                "dimension_bindings": (
                    *declaration.dimension_bindings,
                    ProductQueryDimensionBinding(
                        semantic_ref=ArtifactReference(
                            artifact_id="unknown-state", version=1, digest="7" * 64
                        ),
                        semantic_kind="state",
                        column_name="unknown_state",
                        output_name="unknown_state",
                    ),
                ),
            }
        )
    elif change == "metric_unknown":
        declaration = declaration.model_copy(
            update={
                "metric_bindings": (
                    ProductQueryMetricBinding(
                        semantic_ref=ArtifactReference(
                            artifact_id="unknown-metric", version=1, digest="7" * 64
                        ),
                        aggregate="sum",
                        column_name="unknown_metric",
                        output_name="unknown_metric",
                    ),
                )
            }
        )
    elif change == "unsupported_engine":
        declaration = declaration.model_copy(update={"engine_kind": "clickhouse"})
    elif change == "wrong_product_ref":
        metadata = metadata.model_copy(
            update={
                "product_ref": ArtifactReference(
                    artifact_id="other-product", version=1, digest="8" * 64
                )
            }
        )
    elif change == "wrong_contract_ref":
        metadata = metadata.model_copy(
            update={
                "contract_ref": ArtifactReference(
                    artifact_id="other-contract", version=1, digest="8" * 64
                )
            }
        )
    elif change == "wrong_semantic_ref":
        metadata = metadata.model_copy(
            update={
                "semantic_version_ref": ArtifactReference(
                    artifact_id="other-semantic", version=1, digest="8" * 64
                )
            }
        )
    elif change == "unapproved_contract":
        contract = contract.model_copy(update={"approval_ids": ()})
        metadata = _metadata(contract, semantic)
    elif change == "unapproved_semantic":
        semantic = semantic.model_copy(update={"approval_ids": ()})
        contract = _contract(semantic)
        metadata = _metadata(contract, semantic)
    approvals = () if change == "unapproved" else _approvals(metadata, declaration)
    if change == "foreign_approval":
        approvals = (approvals[0].model_copy(update={"tenant_id": "tenant-b"}),)
    elif change == "wrong_approver":
        approvals = (approvals[0].model_copy(update={"authority_ref": "owner:other"}),)

    with pytest.raises(ValueError, match=message):
        ProductQueryBindingAuthorityService(
            SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: NOW
        ).record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=metadata,
            declaration=declaration,
            approvals=approvals,
        )


def test_query_binding_rejects_unbound_consumption_identity() -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    declaration = _declaration(semantic)
    repository = SQLiteProductQueryBindingRepository(":memory:")
    stored = ProductQueryBindingAuthorityService(repository, clock=lambda: NOW).record(
        contract=contract,
        semantic_version=semantic,
        product_metadata=metadata,
        declaration=declaration,
        approvals=_approvals(metadata, declaration),
    )

    with pytest.raises(ValueError, match="consumption object reference"):
        repository.store(
            stored.model_copy(
                update={
                    "consumption_object_ref": stored.consumption_object_ref.model_copy(
                        update={"digest": "9" * 64}
                    )
                }
            )
        )


def test_query_binding_rejects_approval_created_after_record() -> None:
    semantic = _semantic_version()
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    declaration = _declaration(semantic)
    approvals = (
        _approvals(metadata, declaration)[0].model_copy(
            update={"created_at": datetime(2026, 9, 12, 13, tzinfo=UTC)}
        ),
    )

    with pytest.raises(ValueError, match=r"approval.*after"):
        ProductQueryBindingAuthorityService(
            SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: NOW
        ).record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=metadata,
            declaration=declaration,
            approvals=approvals,
        )


def test_query_binding_requires_an_entity_for_disclosure_suppression() -> None:
    state = SemanticObject(
        object_id="invoice-state",
        name="Invoice state",
        definition="The approved invoice lifecycle state.",
        source_refs=("sales.state",),
    )
    semantic = _semantic_version().model_copy(update={"states": (state,)})
    contract = _contract(semantic)
    metadata = _metadata(contract, semantic)
    state_ref = _reference(state, semantic=semantic)
    declaration = _declaration(semantic).model_copy(
        update={
            "dimension_bindings": (
                ProductQueryDimensionBinding(
                    semantic_ref=state_ref,
                    semantic_kind="state",
                    column_name="invoice_state",
                    output_name="invoice_state",
                ),
            ),
            "disclosure_entity_ref": state_ref,
            "disclosure_entity_column": "invoice_state",
        }
    )

    with pytest.raises(ValueError, match=r"disclosure.*entity"):
        ProductQueryBindingAuthorityService(
            SQLiteProductQueryBindingRepository(":memory:"), clock=lambda: NOW
        ).record(
            contract=contract,
            semantic_version=semantic,
            product_metadata=metadata,
            declaration=declaration,
            approvals=_approvals(metadata, declaration),
        )


def test_query_binding_models_reject_unknown_fields() -> None:
    semantic = _semantic_version()

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ProductQueryBindingDeclaration.model_validate(
            _declaration(semantic).model_dump(mode="python") | {"capability": True},
            strict=True,
        )

    assert ApprovedProductQueryBinding.model_fields["schema_version"].default == "1"
