from __future__ import annotations

from datetime import UTC, datetime

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
    MaterializedProductEvidence,
    ProductVersionAuthorityService,
    SQLiteApprovedProductVersionRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _reference(identifier: str, value: object) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=1, digest=digest(value))


def _semantic_version(*, tenant_id: str = "tenant-a") -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-revenue",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="process-revenue", version=1, digest="1" * 64
        ),
        candidate_set_digest="2" * 64,
        review_bundle_digest="3" * 64,
        entities=(
            SemanticObject(
                object_id="invoice",
                name="Invoice",
                definition="An issued customer invoice.",
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
                definition="Gross revenue less approved refunds.",
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


def _evidence(
    contract: ManagedIntegrationContract,
    *,
    tenant_id: str | None = None,
    lineage_digest: str = "4" * 64,
) -> MaterializedProductEvidence:
    product_ref = ArtifactReference(
        artifact_id=contract.destination_product.product_name,
        version=contract.version,
        digest=digest(contract.destination_product),
    )
    return MaterializedProductEvidence(
        tenant_id=tenant_id or contract.tenant_id,
        product_ref=product_ref,
        generation=7,
        contract_digest=digest(contract),
        materialization_receipt_ref=ArtifactReference(
            artifact_id="run-7", version=7, digest="5" * 64
        ),
        lineage_digest=lineage_digest,
        materialized_at=NOW,
    )


def test_product_version_authority_is_derived_from_exact_approved_inputs() -> None:
    repository = SQLiteApprovedProductVersionRepository(":memory:")
    service = ProductVersionAuthorityService(repository)
    semantic = _semantic_version()
    contract = _contract(semantic)

    stored = service.record(
        contract=contract,
        semantic_version=semantic,
        materialization=_evidence(contract),
    )

    assert (
        repository.read_current(tenant_id="tenant-a", product_ref=stored.product_ref, generation=7)
        == stored
    )
    assert stored.contract_ref == _reference(contract.contract_id, contract)
    assert stored.semantic_version_ref == _reference(semantic.semantic_version_id, semantic)
    assert stored.lineage_digest == "4" * 64
    assert stored.approved_narrative_terms == (
        "product:revenue",
        "Invoice",
        "An issued customer invoice.",
        "Net revenue",
        "Gross revenue less approved refunds.",
    )


def test_product_version_authority_replays_exactly_and_rejects_changed_lineage() -> None:
    repository = SQLiteApprovedProductVersionRepository(":memory:")
    service = ProductVersionAuthorityService(repository)
    semantic = _semantic_version()
    contract = _contract(semantic)
    evidence = _evidence(contract)

    first = service.record(contract=contract, semantic_version=semantic, materialization=evidence)
    replay = service.record(contract=contract, semantic_version=semantic, materialization=evidence)

    assert replay.model_dump_json() == first.model_dump_json()
    with pytest.raises(ValueError, match=r"product version authority.*immutable"):
        service.record(
            contract=contract,
            semantic_version=semantic,
            materialization=_evidence(contract, lineage_digest="6" * 64),
        )


def test_product_version_authority_denies_cross_tenant_materialization() -> None:
    repository = SQLiteApprovedProductVersionRepository(":memory:")
    semantic = _semantic_version()
    contract = _contract(semantic)

    with pytest.raises(ValueError, match="materialization does not match"):
        ProductVersionAuthorityService(repository).record(
            contract=contract,
            semantic_version=semantic,
            materialization=_evidence(contract, tenant_id="tenant-b"),
        )

    assert (
        repository.read_current(
            tenant_id="tenant-b",
            product_ref=_evidence(contract).product_ref,
            generation=7,
        )
        is None
    )


def test_product_version_authority_denies_unapproved_semantics() -> None:
    repository = SQLiteApprovedProductVersionRepository(":memory:")
    semantic = _semantic_version().model_copy(update={"approval_ids": ()})
    contract = _contract(semantic)

    with pytest.raises(ValueError, match="approved semantic version"):
        ProductVersionAuthorityService(repository).record(
            contract=contract,
            semantic_version=semantic,
            materialization=_evidence(contract),
        )
