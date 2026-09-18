from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_type_hints

import pytest
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ContractFormationResult,
    ContractFormationStatus,
    InformationKind,
    digest,
)
from heinzel_semantic_registry import (
    ApprovalCompilationInput,
    ApprovedSemanticCompiler,
    AuthorityObservation,
    AuthoritySourceKind,
    CandidateKind,
    CandidateProvenance,
    OntologyReviewBundle,
    OntologyReviewItem,
    SemanticCandidate,
    SemanticCandidateSet,
)
from heinzel_semantic_registry.repository import SemanticVersionRepository

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class _StrictSemanticVersionRepository(SemanticVersionRepository):
    def __init__(self) -> None:
        self.stored_versions: tuple[ApprovedSemanticVersion, ...] = ()

    def store(self, semantic_version: ApprovedSemanticVersion) -> ApprovedSemanticVersion:
        assert semantic_version.tenant_id == "tenant-a"
        assert semantic_version.approval_ids == ("approval-owner",)
        stored = semantic_version.model_copy(update={"semantic_version_id": "semantic-0001"})
        self.stored_versions += (stored,)
        return stored

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion:
        for semantic_version in self.stored_versions:
            if (
                tenant_id,
                semantic_version_id,
                version,
            ) == (
                semantic_version.tenant_id,
                semantic_version.semantic_version_id,
                semantic_version.version,
            ):
                return semantic_version
        raise KeyError((tenant_id, semantic_version_id, version))


def valid_input(*, observation: AuthorityObservation | None = None) -> ApprovalCompilationInput:
    candidate = SemanticCandidate(
        candidate_id="candidate-customer",
        kind=CandidateKind.ENTITY,
        name="Customer",
        proposed_definition="A customer.",
        related_refs=(),
        provenance=CandidateProvenance(
            source_kind="manifest", source_digest="a" * 64, source_path="manifest"
        ),
        confidence=Decimal(1),
    )
    candidate_set = SemanticCandidateSet(
        set_id="set-0001",
        tenant_id="tenant-a",
        revision=1,
        package_id="package-a",
        package_version=1,
        original_digest="a" * 64,
        manifest_digest="b" * 64,
        extractor_id="deterministic-manifest",
        extractor_version="1",
        candidates=(candidate,),
        unresolved_questions=(),
        created_at=NOW,
    )
    current_observation = observation or AuthorityObservation(
        observation_id="observation-0001",
        tenant_id="tenant-a",
        information_kind=InformationKind.IDENTITY,
        source_kind=AuthoritySourceKind.OWNER_DECISION,
        subject_ref="customer",
        assertion="customer_id is the business identity",
        authority_ref="role:business_owner",
        observed_digest="c" * 64,
        observed_at=NOW,
        valid_until=NOW + timedelta(days=1),
    )
    bundle = OntologyReviewBundle(
        bundle_id="bundle-0001",
        tenant_id="tenant-a",
        revision=2,
        candidate_set_digest=digest(candidate_set),
        authority_observation_digests=(digest(current_observation),),
        items=(
            OntologyReviewItem(
                item_id="item-customer",
                candidate_ids=(candidate.candidate_id,),
                authority_resolution_digest="d" * 64,
                required_authority_ref="role:business_owner",
                semantic_revision_digest="e" * 64,
                status="accepted",
            ),
        ),
        required_authority_refs=("role:business_owner",),
        status="approved",
        created_at=NOW,
        updated_at=NOW,
    )
    return ApprovalCompilationInput(
        tenant_id="tenant-a",
        candidate_set=candidate_set,
        review_bundle=bundle,
        authority_observations=(current_observation,),
        approval_ids=("approval-owner",),
        approved_at=NOW,
    )


def test_compiler_binds_current_authority_and_exact_approval_ids() -> None:
    repository = _StrictSemanticVersionRepository()
    compiled = ApprovedSemanticCompiler(repository).compile(valid_input(), now=NOW)

    assert isinstance(compiled, ApprovedSemanticVersion)
    assert compiled.tenant_id == "tenant-a"
    assert compiled.approval_ids == ("approval-owner",)
    assert compiled.authority_bindings[0].observation_digest == digest(
        valid_input().authority_observations[0]
    )


def test_compiler_rejects_stale_authority_observation() -> None:
    stale = (
        valid_input()
        .authority_observations[0]
        .model_copy(
            update={
                "observed_at": NOW - timedelta(days=2),
                "valid_until": NOW - timedelta(days=1),
            }
        )
    )

    repository = _StrictSemanticVersionRepository()
    result = ApprovedSemanticCompiler(repository).compile(valid_input(observation=stale), now=NOW)
    assert isinstance(result, ContractFormationResult)

    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan is not None
    assert result.no_valid_plan.constraints == ("authority:identity:customer:stale",)
    assert repository.stored_versions == ()


def test_compiler_returns_no_valid_plan_for_conflicting_current_authorities() -> None:
    first = valid_input().authority_observations[0]
    conflict = first.model_copy(
        update={"observation_id": "observation-0002", "observed_digest": "f" * 64}
    )

    repository = _StrictSemanticVersionRepository()
    result = ApprovedSemanticCompiler(repository).compile(
        valid_input().model_copy(update={"authority_observations": (first, conflict)}), now=NOW
    )
    assert isinstance(result, ContractFormationResult)

    assert result.status.value == "no_valid_plan"
    assert repository.stored_versions == ()


def test_compiler_returns_no_valid_plan_for_missing_approval() -> None:
    result = ApprovedSemanticCompiler(_StrictSemanticVersionRepository()).compile(
        valid_input().model_copy(update={"approval_ids": ()}), now=NOW
    )
    assert isinstance(result, ContractFormationResult)

    assert result.status.value == "no_valid_plan"


def test_compiler_requires_a_runtime_resolvable_semantic_version_repository_protocol() -> None:
    annotations = get_type_hints(ApprovedSemanticCompiler.__init__)

    assert annotations["repository"] is SemanticVersionRepository


def test_compiler_requires_a_repository_and_returns_only_a_persisted_semantic_version() -> None:
    with pytest.raises(TypeError):
        # The repository is required; refusing this call is the assertion.
        ApprovedSemanticCompiler()  # type: ignore[call-arg]

    repository = _StrictSemanticVersionRepository()
    compiled = ApprovedSemanticCompiler(repository).compile(valid_input(), now=NOW)

    assert isinstance(compiled, ApprovedSemanticVersion)
    assert repository.load("tenant-a", compiled.semantic_version_id, compiled.version) == compiled
