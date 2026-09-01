from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pillarmesh_contract_model import ArtifactModel, ArtifactReference, digest
from pydantic import Field

from .fulfillment_models import (
    AccessScopePreview,
    ApprovalRequirement,
    ClarifiedOutcomeStatement,
    DependencyKind,
    DisclosureDenial,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    StakeholderAnswerDraft,
)
from .fulfillment_protocols import FreshnessEvaluator
from .models import InboxRequest


class ProposalCompilation(ArtifactModel):
    outcome_kind: Literal["proposal"] = "proposal"
    proposal: FulfillmentProposal


class DependencyCompilation(ArtifactModel):
    outcome_kind: Literal["dependency"] = "dependency"
    dependency_kind: DependencyKind
    reason_code: str = Field(min_length=1)
    missing_capability_refs: tuple[str, ...] = Field(min_length=1)


class DenialCompilation(ArtifactModel):
    outcome_kind: Literal["denial"] = "denial"
    proposal: FulfillmentProposal


class NoValidPlanCompilation(ArtifactModel):
    outcome_kind: Literal["no_valid_plan"] = "no_valid_plan"
    reason_codes: tuple[str, ...] = Field(min_length=1)
    constraint_refs: tuple[ArtifactReference, ...]
    smallest_changes: tuple[str, ...] = Field(min_length=1)


type ProposalCompilationResult = Annotated[
    ProposalCompilation | DependencyCompilation | DenialCompilation | NoValidPlanCompilation,
    Field(discriminator="outcome_kind"),
]


class FulfillmentPolicyCompiler:
    def __init__(self, *, freshness_evaluator: FreshnessEvaluator) -> None:
        self._freshness_evaluator = freshness_evaluator

    def compile_answer(
        self,
        *,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        candidate: StakeholderAnswerDraft,
        proposal_revision: int,
        created_at: datetime,
        prior_proposal_digest: str | None = None,
    ) -> ProposalCompilationResult:
        authority_failure = self._validate_snapshot_authority(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            created_at=created_at,
        )
        if authority_failure is not None:
            return authority_failure

        missing_metrics = self._missing_references(candidate.metric_refs, grounding.metric_refs)
        missing_lineage = self._missing_references(candidate.lineage_refs, grounding.lineage_refs)
        if missing_metrics or missing_lineage:
            return DependencyCompilation(
                dependency_kind="semantic_change",
                reason_code="missing_approved_semantics",
                missing_capability_refs=tuple(
                    sorted(
                        f"semantic:{reference.artifact_id}"
                        for reference in (*missing_metrics, *missing_lineage)
                    )
                ),
            )

        missing_datasets = self._missing_references(
            candidate.governed_dataset_refs, grounding.governed_dataset_refs
        )
        if missing_datasets:
            return DependencyCompilation(
                dependency_kind="data_product_change",
                reason_code="missing_governed_data_product",
                missing_capability_refs=tuple(
                    sorted(
                        f"data_product:{reference.artifact_id}" for reference in missing_datasets
                    )
                ),
            )
        missing_quality = self._missing_references(
            candidate.material_quality_limitations, grounding.quality_observation_refs
        )
        if missing_quality:
            return DependencyCompilation(
                dependency_kind="data_product_change",
                reason_code="missing_quality_observation",
                missing_capability_refs=tuple(
                    sorted(f"quality:{reference.artifact_id}" for reference in missing_quality)
                ),
            )
        if candidate.freshness_disposition in ("current", "stale") and (
            grounding.freshness_observation_ref is None or not grounding.data_observation_refs
        ):
            return DependencyCompilation(
                dependency_kind="data_product_change",
                reason_code="missing_factual_observation",
                missing_capability_refs=("freshness:data_observation",),
            )
        derived_freshness = self._freshness_evaluator.derive(grounding)
        if candidate.freshness_disposition != derived_freshness:
            return self._no_valid_plan(
                "freshness_claim_mismatch",
                "Regenerate the candidate from the admitted freshness observation.",
            )
        if self._missing_references(
            candidate.disclosure_classifications, grounding.classification_refs
        ):
            return self._no_valid_plan(
                "classification_authority_unadmitted",
                "Publish the classification through the approved catalog authority.",
            )

        entitled_products = set(policy.permitted_data_product_refs)
        if any(reference not in entitled_products for reference in candidate.governed_dataset_refs):
            return self._compile_denial(
                request=request,
                clarified_outcome=clarified_outcome,
                grounding=grounding,
                policy=policy,
                denied_scope=candidate.governed_dataset_refs,
                classified=bool(candidate.disclosure_classifications),
                proposal_revision=proposal_revision,
                prior_proposal_digest=prior_proposal_digest,
                created_at=created_at,
            )

        normalized = candidate.model_copy(
            update={
                "governed_dataset_refs": self._sorted_references(candidate.governed_dataset_refs),
                "metric_refs": self._sorted_references(candidate.metric_refs),
                "material_quality_limitations": self._sorted_references(
                    candidate.material_quality_limitations
                ),
                "lineage_refs": self._sorted_references(candidate.lineage_refs),
                "disclosure_classifications": self._sorted_references(
                    candidate.disclosure_classifications
                ),
            }
        )
        authorities = ["role:data_engineering_architect"]
        if self._requires_policy_authority(candidate.disclosure_classifications, policy):
            authorities.append("role:policy_authority")
        proposal = self._proposal(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            subject=normalized,
            authority_refs=tuple(authorities),
            proposal_revision=proposal_revision,
            prior_proposal_digest=prior_proposal_digest,
            created_at=created_at,
        )
        return ProposalCompilation(proposal=proposal)

    def compile_access(
        self,
        *,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        preview: AccessScopePreview,
        data_product_owner_authority_ref: str,
        proposal_revision: int,
        created_at: datetime,
        prior_proposal_digest: str | None = None,
    ) -> ProposalCompilationResult:
        authority_failure = self._validate_snapshot_authority(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            created_at=created_at,
        )
        if authority_failure is not None:
            return authority_failure
        if not data_product_owner_authority_ref:
            return self._no_valid_plan(
                "data_product_owner_unresolved",
                "Bind one approved data-product owner authority.",
            )
        if preview.data_product_ref not in grounding.governed_dataset_refs:
            return DependencyCompilation(
                dependency_kind="data_product_change",
                reason_code="missing_governed_data_product",
                missing_capability_refs=(f"data_product:{preview.data_product_ref.artifact_id}",),
            )
        entitled = (
            preview.data_product_ref in policy.permitted_data_product_refs
            and preview.access_mode in policy.permitted_access_modes
        )
        if not entitled:
            return self._compile_denial(
                request=request,
                clarified_outcome=clarified_outcome,
                grounding=grounding,
                policy=policy,
                denied_scope=(preview.data_product_ref,),
                classified=bool(preview.classifications),
                proposal_revision=proposal_revision,
                prior_proposal_digest=prior_proposal_digest,
                created_at=created_at,
            )

        normalized = preview.model_copy(
            update={
                "effective_object_refs": self._sorted_references(preview.effective_object_refs),
                "effective_fields": tuple(sorted(preview.effective_fields)),
                "excluded_scopes": tuple(sorted(preview.excluded_scopes)),
                "classifications": self._sorted_references(preview.classifications),
            }
        )
        authorities = [data_product_owner_authority_ref]
        if self._requires_policy_authority(preview.classifications, policy):
            authorities.append("role:policy_authority")
        try:
            proposal = self._proposal(
                request=request,
                clarified_outcome=clarified_outcome,
                grounding=grounding,
                policy=policy,
                subject=normalized,
                authority_refs=tuple(authorities),
                proposal_revision=proposal_revision,
                prior_proposal_digest=prior_proposal_digest,
                created_at=created_at,
            )
        except ValueError:
            return self._no_valid_plan(
                "access_scope_invalid",
                "Regenerate a least-privilege subset of the exact request.",
            )
        return ProposalCompilation(proposal=proposal)

    def _compile_denial(
        self,
        *,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        denied_scope: tuple[ArtifactReference, ...],
        classified: bool,
        proposal_revision: int,
        prior_proposal_digest: str | None,
        created_at: datetime,
    ) -> DenialCompilation:
        denial = DisclosureDenial(
            reason_code="disclosure_not_entitled",
            requester_safe_explanation=(
                "The requested disclosure is not available for this purpose."
            ),
            denied_scope_digest=digest(self._sorted_references(denied_scope)),
        )
        authorities = ["role:data_engineering_architect"]
        if classified:
            authorities.append("role:policy_authority")
        proposal = self._proposal(
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            subject=denial,
            authority_refs=tuple(authorities),
            proposal_revision=proposal_revision,
            prior_proposal_digest=prior_proposal_digest,
            created_at=created_at,
        )
        return DenialCompilation(proposal=proposal)

    @staticmethod
    def _proposal(
        *,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        subject: StakeholderAnswerDraft | AccessScopePreview | DisclosureDenial,
        authority_refs: tuple[str, ...],
        proposal_revision: int,
        prior_proposal_digest: str | None,
        created_at: datetime,
    ) -> FulfillmentProposal:
        subject_digest = digest(subject)
        requirements = [
            ApprovalRequirement(
                authority_ref=policy.requester_principal_ref,
                reason_code="clarified_outcome_acceptance",
                subject_digest=digest(clarified_outcome),
            )
        ]
        requirements.extend(
            ApprovalRequirement(
                authority_ref=authority_ref,
                reason_code=FulfillmentPolicyCompiler._reason_code(authority_ref),
                subject_digest=subject_digest,
            )
            for authority_ref in sorted(set(authority_refs))
        )
        requirements.sort(key=lambda item: item.authority_ref)
        return FulfillmentProposal.create(
            proposal_id="pending",
            request=request,
            clarified_outcome=clarified_outcome,
            grounding=grounding,
            policy=policy,
            subject=subject,
            required_approvals=tuple(requirements),
            revision=proposal_revision,
            prior_proposal_digest=prior_proposal_digest,
            created_at=created_at,
        )

    @staticmethod
    def _validate_snapshot_authority(
        *,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        created_at: datetime,
    ) -> NoValidPlanCompilation | None:
        if (
            request.tenant_id != clarified_outcome.tenant_id
            or request.tenant_id != grounding.tenant_id
            or request.tenant_id != policy.tenant_id
        ):
            return FulfillmentPolicyCompiler._no_valid_plan(
                "tenant_authority_mismatch",
                "Resolve every authority artifact in the request tenant.",
            )
        if policy.purpose_digest != clarified_outcome.purpose_digest:
            return FulfillmentPolicyCompiler._no_valid_plan(
                "purpose_authority_mismatch",
                "Re-resolve policy for the accepted purpose.",
            )
        if policy.valid_until <= created_at:
            return FulfillmentPolicyCompiler._no_valid_plan(
                "policy_snapshot_expired",
                "Re-resolve policy from current entitlement observations.",
            )
        return None

    @staticmethod
    def _requires_policy_authority(
        classifications: tuple[ArtifactReference, ...],
        policy: FulfillmentPolicySnapshot,
    ) -> bool:
        governed = set(policy.policy_authority_classifications)
        return any(reference.artifact_id in governed for reference in classifications)

    @staticmethod
    def _missing_references(
        cited: tuple[ArtifactReference, ...], admitted: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        admitted_set = set(admitted)
        return tuple(reference for reference in cited if reference not in admitted_set)

    @staticmethod
    def _sorted_references(
        references: tuple[ArtifactReference, ...],
    ) -> tuple[ArtifactReference, ...]:
        return tuple(
            sorted(
                references,
                key=lambda item: (item.artifact_id, item.version, item.digest),
            )
        )

    @staticmethod
    def _no_valid_plan(reason_code: str, smallest_change: str) -> NoValidPlanCompilation:
        return NoValidPlanCompilation(
            reason_codes=(reason_code,),
            constraint_refs=(),
            smallest_changes=(smallest_change,),
        )

    @staticmethod
    def _reason_code(authority_ref: str) -> str:
        if authority_ref == "role:data_engineering_architect":
            return "architect_review"
        if authority_ref == "role:policy_authority":
            return "policy_review"
        return "data_product_owner_review"
