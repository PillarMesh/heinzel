from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    AuthorityBinding,
    ContractFormationInput,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FreshnessRequirement,
    InformationKind,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    digest,
)
from heinzel_contract_service import (
    FormationDecisionBinding,
    FormationReferenceLoader,
    FormationReviewBundle,
    FormationReviewItem,
    IntegrationContractFormationService,
    ProcessPackageReceipt,
)
from heinzel_semantic_registry import AuthorityObservation, AuthoritySourceKind

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _formation_input(*, semantic_reference: ArtifactReference) -> ContractFormationInput:
    return ContractFormationInput(
        tenant_id="tenant-a",
        semantic_version_ref=semantic_reference,
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
        identity_rules=(("customer", "customer_id"),),
        required_approval_ids=("approval-owner",),
    )


def _authority_observation() -> AuthorityObservation:
    return AuthorityObservation(
        observation_id="observation-0001",
        tenant_id="tenant-a",
        information_kind=InformationKind.IDENTITY,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="customer",
        assertion="customer_id is the business identity",
        authority_ref="role:business_owner",
        observed_digest="a" * 64,
        observed_at=NOW,
        valid_until=NOW + timedelta(days=1),
    )


def _semantic_version() -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-0001",
        tenant_id="tenant-a",
        version=1,
        process_package_ref=ArtifactReference(
            artifact_id="package-0001", version=1, digest="c" * 64
        ),
        candidate_set_digest="d" * 64,
        review_bundle_digest="e" * 64,
        entities=(
            SemanticObject(
                object_id="customer", name="Customer", definition="A customer.", source_refs=()
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(),
        classifications=(),
        authority_bindings=(
            AuthorityBinding(
                information_kind=InformationKind.IDENTITY,
                subject_ref="customer",
                authority_ref="role:business_owner",
                observation_digest=digest(_authority_observation()),
            ),
        ),
        approval_ids=("approval-owner",),
        created_at=NOW,
    )


class _Loader(FormationReferenceLoader):
    def load_semantic_version(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ApprovedSemanticVersion | None:
        version = _semantic_version()
        return (
            version
            if tenant_id == version.tenant_id
            and reference
            == ArtifactReference(
                artifact_id=version.semantic_version_id,
                version=version.version,
                digest=digest(version),
            )
            else None
        )

    def load_process_receipt(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ProcessPackageReceipt | None:
        if tenant_id != "tenant-a" or reference.artifact_id != "package-0001":
            return None
        return ProcessPackageReceipt(
            package_id="package-0001",
            tenant_id="tenant-a",
            version=1,
            media_type="text/markdown; charset=utf-8",
            original_digest="c" * 64,
            manifest_digest="a" * 64,
            manifest_source_digest="f" * 64,
            uploader_id="owner",
            received_at=NOW,
        )

    def has_source_observation(self, tenant_id: str, reference: ArtifactReference) -> bool:
        return tenant_id == "tenant-a" and reference.artifact_id == "source-0001"

    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> AuthorityObservation | None:
        if tenant_id != "tenant-a" or observation_digest != digest(_authority_observation()):
            return None
        return _authority_observation()

    def load_review_bundle(
        self, tenant_id: str, review_bundle_digest: str
    ) -> FormationReviewBundle | None:
        if tenant_id != "tenant-a" or review_bundle_digest != "e" * 64:
            return None
        return FormationReviewBundle(
            tenant_id="tenant-a",
            items=(
                FormationReviewItem(
                    semantic_revision_digest="e" * 64,
                    required_authority_ref="role:business_owner",
                    status="accepted",
                ),
            ),
        )

    def load_decision_binding(
        self, tenant_id: str, approval_id: str
    ) -> FormationDecisionBinding | None:
        return (
            FormationDecisionBinding(
                approval_id=approval_id,
                subject_digest="e" * 64,
                authority_ref="role:business_owner",
            )
            if tenant_id == "tenant-a" and approval_id == "approval-owner"
            else None
        )


class _WrongAuthorityObservationLoader(_Loader):
    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> AuthorityObservation | None:
        return AuthorityObservation(
            observation_id="observation-0001",
            tenant_id="tenant-b",
            information_kind=InformationKind.IDENTITY,
            source_kind=AuthoritySourceKind.OWNER_DECISION,
            subject_ref="customer",
            assertion="customer_id is the business identity",
            authority_ref="role:business_owner",
            observed_digest="a" * 64,
            observed_at=NOW,
            valid_until=NOW + timedelta(days=1),
        )


class _WrongDecisionAuthorityLoader(_Loader):
    def load_decision_binding(
        self, tenant_id: str, approval_id: str
    ) -> FormationDecisionBinding | None:
        binding = super().load_decision_binding(tenant_id, approval_id)
        return binding.model_copy(update={"authority_ref": "role:unrelated"}) if binding else None


class _StaleAuthorityObservationLoader(_Loader):
    def __init__(
        self, semantic_version: ApprovedSemanticVersion, stale_observation: AuthorityObservation
    ) -> None:
        self._semantic_version = semantic_version
        self._stale_observation = stale_observation

    def load_semantic_version(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ApprovedSemanticVersion | None:
        expected_reference = ArtifactReference(
            artifact_id=self._semantic_version.semantic_version_id,
            version=self._semantic_version.version,
            digest=digest(self._semantic_version),
        )
        return (
            self._semantic_version
            if tenant_id == "tenant-a" and reference == expected_reference
            else None
        )

    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> AuthorityObservation | None:
        if tenant_id != "tenant-a" or observation_digest != digest(self._stale_observation):
            return None
        return self._stale_observation


class _ConflictingAuthorityObservationLoader(_Loader):
    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> AuthorityObservation | None:
        observation = super().load_authority_observation(tenant_id, observation_digest)
        return observation.model_copy(update={"observed_digest": "f" * 64}) if observation else None


def test_formation_rejects_authority_observation_that_is_not_the_exact_tenant_binding() -> None:
    semantic_version = _semantic_version()
    semantic_reference = ArtifactReference(
        artifact_id=semantic_version.semantic_version_id,
        version=semantic_version.version,
        digest=digest(semantic_version),
    )

    result = IntegrationContractFormationService(
        _WrongAuthorityObservationLoader(), clock=lambda: NOW
    ).form(_formation_input(semantic_reference=semantic_reference))

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("authority:identity:customer:not_exact",)


def test_formation_rejects_decision_binding_with_the_wrong_required_authority() -> None:
    semantic_version = _semantic_version()
    semantic_reference = ArtifactReference(
        artifact_id=semantic_version.semantic_version_id,
        version=semantic_version.version,
        digest=digest(semantic_version),
    )

    result = IntegrationContractFormationService(
        _WrongDecisionAuthorityLoader(), clock=lambda: NOW
    ).form(_formation_input(semantic_reference=semantic_reference))

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("approval:" + "e" * 64 + ":not_exact",)


def test_formation_never_readies_fabricated_or_cross_tenant_references() -> None:
    fabricated_reference = ArtifactReference(
        artifact_id="semantic-0001",
        version=1,
        digest="0" * 64,
    )

    result = IntegrationContractFormationService(_Loader(), clock=lambda: NOW).form(
        _formation_input(semantic_reference=fabricated_reference)
    )

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.contract is None
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("semantic_version_ref:not_persisted",)


def test_formation_requires_a_governed_reference_loader() -> None:
    with pytest.raises(TypeError):
        IntegrationContractFormationService()


def test_formation_returns_an_attributable_no_valid_plan_for_stale_authority() -> None:
    stale_observation = _authority_observation().model_copy(update={"valid_until": NOW})
    semantic_version = _semantic_version().model_copy(
        update={
            "authority_bindings": (
                AuthorityBinding(
                    information_kind=InformationKind.IDENTITY,
                    subject_ref="customer",
                    authority_ref="role:business_owner",
                    observation_digest=digest(stale_observation),
                ),
            )
        }
    )
    semantic_reference = ArtifactReference(
        artifact_id=semantic_version.semantic_version_id,
        version=semantic_version.version,
        digest=digest(semantic_version),
    )

    result = IntegrationContractFormationService(
        _StaleAuthorityObservationLoader(semantic_version, stale_observation), clock=lambda: NOW
    ).form(_formation_input(semantic_reference=semantic_reference))

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("authority:identity:customer:stale",)
