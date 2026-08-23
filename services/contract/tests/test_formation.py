from __future__ import annotations

from datetime import UTC, datetime

from pillarmesh_contract_model import (
    AccessPolicy,
    ArtifactReference,
    ContractFormationInput,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FreshnessRequirement,
    QualityPolicy,
    TriggerRequirement,
    Unknown,
    digest,
)
from pillarmesh_contract_service import IntegrationContractFormationService

from .test_formation_governance import _Loader, _semantic_version

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def valid_formation_input(
    *, identity_rule: str | Unknown = "customer_id"
) -> ContractFormationInput:
    semantic_version = _semantic_version()
    return ContractFormationInput(
        tenant_id="tenant-a",
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        source_observation_refs=(
            ArtifactReference(artifact_id="source-0001", version=1, digest="b" * 64),
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
        identity_rules=(("customer", identity_rule),),
        required_approval_ids=("approval-owner",),
    )


def service() -> IntegrationContractFormationService:
    return IntegrationContractFormationService(_Loader(), clock=lambda: NOW)


def test_contract_binds_exact_semantic_and_approval_versions() -> None:
    formation_input = valid_formation_input()
    result = service().form(formation_input)

    assert result.status is ContractFormationStatus.READY_TO_ACTIVATE
    assert result.contract is not None
    assert (
        result.contract.semantic_version_ref.digest == formation_input.semantic_version_ref.digest
    )
    assert result.contract.approval_ids == ("approval-owner",)


def test_unknown_required_identity_returns_no_valid_plan() -> None:
    result = service().form(valid_formation_input(identity_rule=Unknown(reason="owner unresolved")))

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.execution_occurred is False
    assert result.no_valid_plan.constraints == ("identity_rule:customer",)


def test_missing_exact_approval_needs_approval_without_a_contract() -> None:
    result = service().form(valid_formation_input().model_copy(update={"approval_ids": ()}))

    assert result.status is ContractFormationStatus.NEEDS_APPROVAL
    assert result.contract is None
    assert result.missing_approval_refs == ("approval-owner",)


def test_contract_requires_a_current_persisted_source_observation() -> None:
    result = service().form(
        valid_formation_input().model_copy(update={"source_observation_refs": ()})
    )

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("source_observation_ref:required",)


def test_formation_is_deterministic_without_a_timestamp() -> None:
    formation_input = valid_formation_input()

    assert digest(service().form(formation_input)) == digest(service().form(formation_input))
