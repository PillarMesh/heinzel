from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    AuthorityBinding,
    ContractFormationResult,
    ContractFormationStatus,
    ContractNoValidPlan,
    InformationKind,
    SemanticObject,
    SemanticRule,
    SemanticRuleKind,
    digest,
)
from pydantic import ConfigDict, Field, field_validator

from .models import AuthorityObservation, CandidateKind, SemanticCandidateSet
from .repository import SemanticVersionRepository
from .review import OntologyReviewBundle


class ApprovalCompilationInput(ArtifactModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, revalidate_instances="never"
    )
    tenant_id: str = Field(min_length=1)
    candidate_set: SemanticCandidateSet
    review_bundle: OntologyReviewBundle
    authority_observations: tuple[AuthorityObservation, ...]
    approval_ids: tuple[str, ...]
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("approved_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class ApprovedSemanticCompiler:
    def __init__(self, repository: SemanticVersionRepository) -> None:
        self._repository = repository

    def compile(
        self, compilation_input: ApprovalCompilationInput, *, now: datetime
    ) -> ApprovedSemanticVersion | ContractFormationResult:
        constraints = self._governed_constraints(compilation_input, now)
        if constraints:
            return ContractFormationResult(
                status=ContractFormationStatus.NO_VALID_PLAN,
                contract=None,
                no_valid_plan=ContractNoValidPlan(
                    constraints=constraints,
                    smallest_changes=("resolve current governing semantic inputs",),
                ),
            )
        candidate_by_id = {
            candidate.candidate_id: candidate
            for candidate in compilation_input.candidate_set.candidates
        }
        accepted_ids = tuple(
            sorted(
                {
                    candidate_id
                    for item in compilation_input.review_bundle.items
                    if item.status in {"accepted", "revised", "merged"}
                    for candidate_id in item.candidate_ids
                }
            )
        )
        candidates = tuple(candidate_by_id[candidate_id] for candidate_id in accepted_ids)

        entities: list[SemanticObject] = []
        events: list[SemanticObject] = []
        states: list[SemanticObject] = []
        relationships: list[SemanticObject] = []
        metrics: list[SemanticObject] = []
        classifications: list[SemanticObject] = []
        identity_rules: list[SemanticRule] = []
        semantic_constraints: list[SemanticRule] = []
        for candidate in candidates:
            if candidate.kind is CandidateKind.IDENTITY_RULE:
                identity_rules.append(
                    SemanticRule(
                        rule_id=candidate.candidate_id,
                        kind=SemanticRuleKind.IDENTITY,
                        expression=candidate.proposed_definition or candidate.name,
                        source_refs=(candidate.candidate_id,),
                    )
                )
            elif candidate.kind is CandidateKind.INTEGRITY_CONSTRAINT:
                semantic_constraints.append(
                    SemanticRule(
                        rule_id=candidate.candidate_id,
                        kind=SemanticRuleKind.INTEGRITY_CONSTRAINT,
                        expression=candidate.proposed_definition or candidate.name,
                        source_refs=(candidate.candidate_id,),
                    )
                )
            else:
                semantic_object = SemanticObject(
                    object_id=candidate.candidate_id,
                    name=candidate.name,
                    definition=candidate.proposed_definition or candidate.name,
                    source_refs=(candidate.candidate_id,),
                )
                {
                    CandidateKind.ENTITY: entities,
                    CandidateKind.EVENT: events,
                    CandidateKind.STATE: states,
                    CandidateKind.RELATIONSHIP: relationships,
                    CandidateKind.METRIC: metrics,
                    CandidateKind.CLASSIFICATION: classifications,
                }[candidate.kind].append(semantic_object)

        observation_by_digest = {
            digest(observation): observation
            for observation in compilation_input.authority_observations
        }
        authority_bindings = tuple(
            AuthorityBinding(
                information_kind=observation.information_kind,
                subject_ref=observation.subject_ref,
                authority_ref=observation.authority_ref,
                observation_digest=observation_digest,
            )
            for observation_digest, observation in sorted(observation_by_digest.items())
        )
        semantic_identity = (
            compilation_input.tenant_id,
            compilation_input.candidate_set.manifest_digest,
            digest(compilation_input.review_bundle),
            tuple(sorted(compilation_input.approval_ids)),
        )
        semantic_version = ApprovedSemanticVersion(
            semantic_version_id=f"semantic-{digest(semantic_identity)[:24]}",
            tenant_id=compilation_input.tenant_id,
            version=1,
            process_package_ref=ArtifactReference(
                artifact_id=compilation_input.candidate_set.package_id,
                version=compilation_input.candidate_set.package_version,
                digest=compilation_input.candidate_set.original_digest,
            ),
            candidate_set_digest=digest(compilation_input.candidate_set),
            review_bundle_digest=digest(compilation_input.review_bundle),
            entities=tuple(sorted(entities, key=lambda item: item.object_id)),
            events=tuple(sorted(events, key=lambda item: item.object_id)),
            states=tuple(sorted(states, key=lambda item: item.object_id)),
            relationships=tuple(sorted(relationships, key=lambda item: item.object_id)),
            identity_rules=tuple(sorted(identity_rules, key=lambda item: item.rule_id)),
            constraints=tuple(sorted(semantic_constraints, key=lambda item: item.rule_id)),
            metrics=tuple(sorted(metrics, key=lambda item: item.object_id)),
            classifications=tuple(sorted(classifications, key=lambda item: item.object_id)),
            authority_bindings=authority_bindings,
            approval_ids=tuple(sorted(compilation_input.approval_ids)),
            created_at=compilation_input.approved_at,
        )
        return self._repository.store(semantic_version)

    def _governed_constraints(
        self, compilation_input: ApprovalCompilationInput, now: datetime
    ) -> tuple[str, ...]:
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            return ("clock:not_utc",)
        if compilation_input.tenant_id != compilation_input.candidate_set.tenant_id:
            return ("candidate_set:tenant_mismatch",)
        bundle = compilation_input.review_bundle
        if bundle.tenant_id != compilation_input.tenant_id:
            return ("review_bundle:tenant_mismatch",)
        if bundle.status != "approved":
            return ("review_bundle:not_approved",)
        if digest(compilation_input.candidate_set) != bundle.candidate_set_digest:
            return ("review_bundle:candidate_set_mismatch",)
        if not compilation_input.approval_ids:
            return ("approval:missing",)
        if len(compilation_input.approval_ids) != len(set(compilation_input.approval_ids)):
            return ("approval:duplicate",)
        if any(item.status not in {"accepted", "revised", "merged"} for item in bundle.items):
            return ("review_item:not_terminal",)
        current = now.astimezone(UTC)
        observations = compilation_input.authority_observations
        authorities: dict[tuple[InformationKind, str], str] = {}
        constraints: list[str] = []
        for observation in observations:
            if (
                observation.valid_until <= observation.observed_at
                or observation.valid_until <= current
            ):
                constraints.append(
                    f"authority:{observation.information_kind}:{observation.subject_ref}:stale"
                )
                continue
            key = (observation.information_kind, observation.subject_ref)
            prior = authorities.get(key)
            if prior is not None and prior != observation.observed_digest:
                constraints.append(
                    f"authority:{observation.information_kind}:{observation.subject_ref}:conflicting"
                )
            authorities[key] = observation.observed_digest
        if any(
            digest(observation) not in bundle.authority_observation_digests
            for observation in observations
        ):
            constraints.append("authority:not_bound_to_review_bundle")
        if any(observation.observed_at > current for observation in observations):
            constraints.append("authority:observed_in_future")
        return tuple(sorted(set(constraints)))
