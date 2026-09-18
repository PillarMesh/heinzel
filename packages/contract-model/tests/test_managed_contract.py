from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    AuthorityBinding,
    ContractConstraint,
    ContractFormationResult,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FieldMapping,
    FreshnessRequirement,
    InformationKind,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    SemanticRule,
    SemanticRuleKind,
    TriggerRequirement,
    digest,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def approved_semantics() -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-0001",
        tenant_id="tenant-a",
        version=1,
        process_package_ref=ArtifactReference(artifact_id="package-a", version=1, digest="a" * 64),
        candidate_set_digest="b" * 64,
        review_bundle_digest="c" * 64,
        entities=(
            SemanticObject(
                object_id="customer",
                name="Customer",
                definition="A customer.",
                source_refs=("candidate-customer",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(
            SemanticRule(
                rule_id="identity-customer",
                kind=SemanticRuleKind.IDENTITY,
                expression="customer_id",
                source_refs=("candidate-customer",),
            ),
        ),
        constraints=(),
        metrics=(),
        classifications=(),
        authority_bindings=(
            AuthorityBinding(
                information_kind=InformationKind.IDENTITY,
                subject_ref="customer",
                authority_ref="role:business_owner",
                observation_digest="d" * 64,
            ),
        ),
        approval_ids=("approval-owner",),
        created_at=NOW,
    )


def managed_contract() -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="managed-contract-0001",
        tenant_id="tenant-a",
        version=1,
        formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
        semantic_version_ref=ArtifactReference(
            artifact_id="semantic-0001", version=1, digest=digest(approved_semantics())
        ),
        source_observation_refs=(
            ArtifactReference(artifact_id="source-observation", version=1, digest="e" * 64),
        ),
        mappings=(
            FieldMapping(
                source_ref="source.customer_id",
                semantic_ref="customer.customer_id",
                transformation="identity",
            ),
        ),
        integrity_constraints=(
            ContractConstraint(
                constraint_id="identity-customer",
                kind="identity",
                expression="customer_id is unique",
            ),
        ),
        destination_product=DestinationProductRequirement(
            product_name="Customer warehouse",
            warehouse_binding_id="warehouse-a",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=3600),
        quality=QualityPolicy(required_constraint_ids=("identity-customer",)),
        trigger_policy=TriggerRequirement(run_now_allowed=False),
        access_policy=AccessPolicy(
            classification_refs=("classification:internal",),
            required_approver_refs=("role:business_owner",),
        ),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("approval-owner",),
    )


def test_managed_contract_v2_keeps_m0_contract_model_unchanged() -> None:
    contract = managed_contract()

    assert contract.schema_version == "2"
    assert contract.formation_status is ContractFormationStatus.READY_TO_ACTIVATE
    assert "tenant_id" not in __import__("heinzel_contract_model").IntegrationContract.model_fields


def test_managed_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ManagedIntegrationContract.model_validate(
            managed_contract().model_dump() | {"endpoint": "secret"}
        )


def test_approved_semantics_requires_utc_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        approved_semantics().model_copy(update={"created_at": datetime(2026, 8, 21, 12)})


def test_formation_result_rejects_a_contract_for_no_valid_plan() -> None:
    with pytest.raises(ValidationError, match="no-valid-plan result requires only constraints"):
        ContractFormationResult(
            status=ContractFormationStatus.NO_VALID_PLAN,
            contract=managed_contract(),
            no_valid_plan=None,
        )
