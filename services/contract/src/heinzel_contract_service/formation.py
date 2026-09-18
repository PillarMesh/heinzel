from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ContractConstraint,
    ContractFormationInput,
    ContractFormationResult,
    ContractFormationStatus,
    ContractNoValidPlan,
    FieldMapping,
    ManagedIntegrationContract,
    Unknown,
    digest,
)

from .process_models import ProcessPackageReceipt


class FormationDecisionBinding(ArtifactModel):
    approval_id: str
    subject_digest: str
    authority_ref: str


class FormationAuthorityObservation(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def information_kind(self) -> object: ...

    @property
    def subject_ref(self) -> str: ...

    @property
    def authority_ref(self) -> str: ...

    @property
    def valid_until(self) -> datetime: ...

    @property
    def observed_at(self) -> datetime: ...


class FormationReviewItem(ArtifactModel):
    semantic_revision_digest: str
    required_authority_ref: str
    status: str


class FormationReviewBundle(ArtifactModel):
    tenant_id: str
    items: tuple[FormationReviewItem, ...]


class FormationReferenceLoader(Protocol):
    def load_semantic_version(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ApprovedSemanticVersion | None: ...

    def load_process_receipt(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ProcessPackageReceipt | None: ...

    def has_source_observation(self, tenant_id: str, reference: ArtifactReference) -> bool: ...

    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> FormationAuthorityObservation | None: ...

    def load_review_bundle(
        self, tenant_id: str, review_bundle_digest: str
    ) -> FormationReviewBundle | None: ...

    def load_decision_binding(
        self, tenant_id: str, approval_id: str
    ) -> FormationDecisionBinding | None: ...


class IntegrationContractFormationService:
    def __init__(
        self,
        loader: FormationReferenceLoader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._loader = loader
        self._clock = clock or (lambda: datetime.now(UTC))

    def form(self, formation_input: ContractFormationInput) -> ContractFormationResult:
        unknown_constraints = tuple(
            f"identity_rule:{subject_ref}"
            for subject_ref, rule in formation_input.identity_rules
            if isinstance(rule, Unknown)
        )
        if unknown_constraints:
            return ContractFormationResult(
                status=ContractFormationStatus.NO_VALID_PLAN,
                contract=None,
                no_valid_plan=ContractNoValidPlan(
                    constraints=unknown_constraints,
                    smallest_changes=("resolve required identity authority",),
                ),
            )

        missing_approval_refs = tuple(
            approval_id
            for approval_id in formation_input.required_approval_ids
            if approval_id not in formation_input.approval_ids
        )
        if missing_approval_refs:
            return ContractFormationResult(
                status=ContractFormationStatus.NEEDS_APPROVAL,
                contract=None,
                no_valid_plan=None,
                missing_approval_refs=missing_approval_refs,
            )

        constraints = self._reference_constraints(formation_input)
        if constraints:
            return ContractFormationResult(
                status=ContractFormationStatus.NO_VALID_PLAN,
                contract=None,
                no_valid_plan=ContractNoValidPlan(
                    constraints=constraints,
                    smallest_changes=("supply current persisted governed references",),
                ),
            )

        mappings = tuple(
            FieldMapping(
                source_ref=rule,
                semantic_ref=f"{subject_ref}.{rule}",
                transformation="identity",
            )
            for subject_ref, rule in formation_input.identity_rules
            if isinstance(rule, str)
        )
        integrity_constraints = tuple(
            ContractConstraint(
                constraint_id=constraint_id,
                kind="identity",
                expression=constraint_id,
            )
            for constraint_id in formation_input.quality.required_constraint_ids
        )
        contract = ManagedIntegrationContract(
            contract_id=f"managed-{digest(formation_input)[:24]}",
            tenant_id=formation_input.tenant_id,
            version=1,
            semantic_version_ref=formation_input.semantic_version_ref,
            source_observation_refs=formation_input.source_observation_refs,
            mappings=mappings,
            integrity_constraints=integrity_constraints,
            destination_product=formation_input.destination_product,
            freshness=formation_input.freshness,
            quality=formation_input.quality,
            trigger_policy=formation_input.trigger_policy,
            access_policy=formation_input.access_policy,
            evidence_policy=formation_input.evidence_policy,
            failure_policy=formation_input.failure_policy,
            approval_ids=formation_input.approval_ids,
            formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
        )
        return ContractFormationResult(
            status=ContractFormationStatus.READY_TO_ACTIVATE,
            contract=contract,
            no_valid_plan=None,
        )

    def _reference_constraints(self, formation_input: ContractFormationInput) -> tuple[str, ...]:
        semantic_version = self._loader.load_semantic_version(
            formation_input.tenant_id, formation_input.semantic_version_ref
        )
        if semantic_version is None:
            return ("semantic_version_ref:not_persisted",)
        expected_semantic_ref = ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        )
        if formation_input.semantic_version_ref != expected_semantic_ref:
            return ("semantic_version_ref:not_exact",)
        receipt = self._loader.load_process_receipt(
            formation_input.tenant_id, semantic_version.process_package_ref
        )
        if receipt is None or receipt.tenant_id != formation_input.tenant_id:
            return ("process_package_ref:not_persisted",)
        constraints = (
            [] if formation_input.source_observation_refs else ["source_observation_ref:required"]
        )
        constraints.extend(
            "source_observation_ref:not_persisted"
            for reference in formation_input.source_observation_refs
            if not self._loader.has_source_observation(formation_input.tenant_id, reference)
        )
        now = self._clock()
        authority_digests: dict[tuple[object, str], set[str]] = {}
        for binding in semantic_version.authority_bindings:
            observation = self._loader.load_authority_observation(
                formation_input.tenant_id, binding.observation_digest
            )
            if (
                observation is None
                or observation.tenant_id != formation_input.tenant_id
                or digest(observation) != binding.observation_digest
                or observation.information_kind != binding.information_kind
                or observation.subject_ref != binding.subject_ref
                or observation.authority_ref != binding.authority_ref
            ):
                constraints.append(
                    f"authority:{binding.information_kind}:{binding.subject_ref}:not_exact"
                )
                continue
            if observation.observed_at > now:
                constraints.append(
                    f"authority:{binding.information_kind}:{binding.subject_ref}:observed_in_future"
                )
                continue
            if observation.valid_until <= now:
                constraints.append(
                    f"authority:{binding.information_kind}:{binding.subject_ref}:stale"
                )
                continue
            authority_digests.setdefault(
                (binding.information_kind, binding.subject_ref), set()
            ).add(binding.observation_digest)
        for (information_kind, subject_ref), observation_digests in authority_digests.items():
            if len(observation_digests) > 1:
                constraints.append(f"authority:{information_kind}:{subject_ref}:conflicting")
        review_bundle = self._loader.load_review_bundle(
            formation_input.tenant_id, semantic_version.review_bundle_digest
        )
        if review_bundle is None or review_bundle.tenant_id != formation_input.tenant_id:
            constraints.append("review_bundle:not_persisted")
            return tuple(sorted(constraints))
        required_items = tuple(
            item for item in review_bundle.items if item.status in {"accepted", "revised", "merged"}
        )
        if tuple(sorted(formation_input.approval_ids)) != tuple(
            sorted(semantic_version.approval_ids)
        ):
            constraints.append("approval_ids:not_exact")
        bindings: list[FormationDecisionBinding] = []
        for approval_id in formation_input.approval_ids:
            decision_binding = self._loader.load_decision_binding(
                formation_input.tenant_id, approval_id
            )
            if decision_binding is None or decision_binding.approval_id != approval_id:
                constraints.append(f"approval:{approval_id}:not_exact")
            else:
                bindings.append(decision_binding)
        for item in required_items:
            if not any(
                binding.subject_digest == item.semantic_revision_digest
                and binding.authority_ref == item.required_authority_ref
                for binding in bindings
            ):
                constraints.append(f"approval:{item.semantic_revision_digest}:not_exact")
        return tuple(sorted(constraints))
