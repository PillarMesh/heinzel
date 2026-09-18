from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from heinzel_contract_model import (
    ArtifactModel,
    ArtifactReference,
    ImpactAdmissionBinding,
    digest,
)
from pydantic import Field, field_validator, model_validator

from .models import DataAccessRequest, InboxRequest, RequestState, StakeholderQuestion

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type AccessMode = Literal["query", "dashboard", "export"]
type AccessGrantEffectSurface = Literal["result", "superset", "warehouse"]
type AccessGrantPermission = Literal["dashboard", "download", "query", "view"]
type DependencyKind = Literal["semantic_change", "data_product_change"]
type FreshnessDisposition = Literal["current", "stale", "unknown", "not_applicable"]
type FulfillmentDecision = Literal["approve", "reject", "request_changes"]
type FulfillmentOutcome = Literal[
    "execution_ready", "delivered", "denial", "dependency", "no_valid_plan", "cancelled"
]


def _timezone_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _require_unique(values: tuple[object, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _require_citations(
    cited: tuple[ArtifactReference, ...],
    admitted: tuple[ArtifactReference, ...],
    citation_name: str,
) -> None:
    admitted_set = set(admitted)
    if any(reference not in admitted_set for reference in cited):
        raise ValueError(f"{citation_name} citation is absent from grounding snapshot")


class FulfillmentGroundingSnapshot(ArtifactModel):
    schema_version: Literal["1"] = "1"
    snapshot_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    catalog_publication_id: str = Field(min_length=1)
    catalog_publication_intent_digest: str = Field(pattern=_DIGEST_PATTERN)
    catalog_round_trip_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    semantic_version_ref: ArtifactReference
    integration_contract_ref: ArtifactReference
    process_package_ref: ArtifactReference
    governed_dataset_refs: tuple[ArtifactReference, ...]
    metric_refs: tuple[ArtifactReference, ...]
    classification_refs: tuple[ArtifactReference, ...]
    lineage_refs: tuple[ArtifactReference, ...]
    freshness_observation_ref: ArtifactReference | None
    quality_observation_refs: tuple[ArtifactReference, ...]
    authorization_policy_ref: ArtifactReference
    data_observation_refs: tuple[ArtifactReference, ...]
    as_of: datetime
    created_at: datetime

    @field_validator("as_of", "created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "timestamp")
        return _timezone_aware_utc(value, field_name)

    @model_validator(mode="after")
    def has_unique_references(self) -> Self:
        for field_name in (
            "governed_dataset_refs",
            "metric_refs",
            "classification_refs",
            "lineage_refs",
            "quality_observation_refs",
            "data_observation_refs",
        ):
            _require_unique(getattr(self, field_name), field_name)
        return self


class FulfillmentPolicySnapshot(ArtifactModel):
    schema_version: Literal["1"] = "1"
    snapshot_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    requester_id: str = Field(min_length=1)
    requester_principal_ref: str = Field(min_length=1)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    approved_policy_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    entitlement_observation_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    classification_rule_refs: tuple[ArtifactReference, ...]
    permitted_data_product_refs: tuple[ArtifactReference, ...]
    permitted_access_modes: tuple[AccessMode, ...]
    maximum_expiry: datetime | None
    policy_authority_classifications: tuple[str, ...]
    observed_at: datetime
    valid_until: datetime

    @field_validator("maximum_expiry", "observed_at", "valid_until")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "timestamp")
        return _timezone_aware_utc(value, field_name)

    @model_validator(mode="after")
    def has_positive_unique_observation_window(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be after observed_at")
        if self.maximum_expiry is not None and self.maximum_expiry < self.observed_at:
            raise ValueError("maximum_expiry must not precede observed_at")
        for field_name in (
            "approved_policy_refs",
            "entitlement_observation_refs",
            "classification_rule_refs",
            "permitted_data_product_refs",
            "permitted_access_modes",
            "policy_authority_classifications",
        ):
            _require_unique(getattr(self, field_name), field_name)
        return self


class StakeholderAnswerDraft(ArtifactModel):
    subject_kind: Literal["stakeholder_answer"] = "stakeholder_answer"
    answer_text: str = Field(min_length=1, max_length=16000)
    governed_dataset_refs: tuple[ArtifactReference, ...]
    metric_refs: tuple[ArtifactReference, ...]
    as_of: datetime
    freshness_disposition: FreshnessDisposition
    material_quality_limitations: tuple[ArtifactReference, ...]
    lineage_refs: tuple[ArtifactReference, ...]
    disclosure_classifications: tuple[ArtifactReference, ...]
    plan_digest: str | None = Field(
        default=None, pattern=_DIGEST_PATTERN, exclude_if=lambda value: value is None
    )

    @field_validator("as_of")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "as_of")


class AccessScopePreview(ArtifactModel):
    subject_kind: Literal["access_scope"] = "access_scope"
    requester_principal_ref: str = Field(min_length=1)
    data_product_ref: ArtifactReference
    access_mode: AccessMode
    requested_fields: tuple[str, ...] = Field(min_length=1)
    effective_object_refs: tuple[ArtifactReference, ...]
    effective_fields: tuple[str, ...]
    excluded_scopes: tuple[str, ...]
    classifications: tuple[ArtifactReference, ...]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "expires_at")


class DisclosureDenial(ArtifactModel):
    subject_kind: Literal["disclosure_denial"] = "disclosure_denial"
    reason_code: str = Field(min_length=1)
    requester_safe_explanation: str = Field(min_length=1, max_length=1000)
    denied_scope_digest: str = Field(pattern=_DIGEST_PATTERN)


type FulfillmentSubject = Annotated[
    StakeholderAnswerDraft | AccessScopePreview | DisclosureDenial,
    Field(discriminator="subject_kind"),
]


class ApprovalRequirement(ArtifactModel):
    authority_ref: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    subject_digest: str = Field(pattern=_DIGEST_PATTERN)


class ClarifiedOutcomeStatement(ArtifactModel):
    schema_version: Literal["1"] = "1"
    statement_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    restated_request: str = Field(min_length=1, max_length=4000)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    in_scope_summary: str = Field(min_length=1, max_length=4000)
    out_of_scope_summary: str = Field(min_length=1, max_length=4000)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")


class FulfillmentProposal(ArtifactModel):
    schema_version: Literal["1"] = "1"
    proposal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    revision: int = Field(ge=1)
    prior_proposal_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    clarified_outcome_digest: str = Field(pattern=_DIGEST_PATTERN)
    grounding_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    impact_admission_binding: ImpactAdmissionBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    subject: FulfillmentSubject
    required_approvals: tuple[ApprovalRequirement, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")

    @model_validator(mode="after")
    def has_consistent_revision_and_requirements(self) -> Self:
        if self.revision == 1 and self.prior_proposal_digest is not None:
            raise ValueError("first proposal revision cannot have a prior proposal digest")
        if self.revision > 1 and self.prior_proposal_digest is None:
            raise ValueError("revised proposal requires a prior proposal digest")
        authorities = tuple(item.authority_ref for item in self.required_approvals)
        _require_unique(authorities, "required_approvals authorities")
        allowed_subject_digests = {self.clarified_outcome_digest, digest(self.subject)}
        if any(
            requirement.subject_digest not in allowed_subject_digests
            for requirement in self.required_approvals
        ):
            raise ValueError("approval requirement names an unrelated subject digest")
        binding = self.impact_admission_binding
        if binding is not None:
            if binding.tenant_id != self.tenant_id:
                raise ValueError("impact admission binding must belong to the proposal tenant")
            if binding.subject.change_subject_digest != digest(self.subject):
                raise ValueError("impact admission binding must name the proposal subject")
            required_authorities = {item.authority_ref for item in self.required_approvals}
            if any(
                requirement.authority_ref not in required_authorities
                for requirement in binding.authority_snapshot.derived_approval_requirements
            ):
                raise ValueError("proposal omits a derived impact approval authority")
        return self

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        request: InboxRequest,
        clarified_outcome: ClarifiedOutcomeStatement,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
        subject: StakeholderAnswerDraft | AccessScopePreview | DisclosureDenial,
        required_approvals: tuple[ApprovalRequirement, ...],
        revision: int,
        created_at: datetime,
        prior_proposal_digest: str | None = None,
    ) -> Self:
        if request.state is not RequestState.INVESTIGATING:
            raise ValueError("proposal requires an investigating request")
        if (
            clarified_outcome.tenant_id != request.tenant_id
            or grounding.tenant_id != request.tenant_id
            or policy.tenant_id != request.tenant_id
        ):
            raise ValueError("proposal inputs must belong to the request tenant")
        if clarified_outcome.request_id != request.request_id:
            raise ValueError("clarified outcome must name the request")
        if clarified_outcome.request_revision > request.revision:
            raise ValueError("clarified outcome revision is ahead of the request")
        if policy.requester_id != request.requester_id:
            raise ValueError("policy snapshot must name the requester")
        cls._validate_subject(request=request, subject=subject, grounding=grounding, policy=policy)

        statement_digest = digest(clarified_outcome)
        requester_requirements = tuple(
            requirement
            for requirement in required_approvals
            if requirement.authority_ref == policy.requester_principal_ref
            and requirement.subject_digest == statement_digest
        )
        if len(requester_requirements) != 1:
            raise ValueError("proposal requires exact clarified outcome acceptance")

        return cls(
            proposal_id=proposal_id,
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            request_revision=request.revision + 1,
            revision=revision,
            prior_proposal_digest=prior_proposal_digest,
            clarified_outcome_digest=statement_digest,
            grounding_snapshot_digest=digest(grounding),
            policy_snapshot_digest=digest(policy),
            subject=subject,
            required_approvals=required_approvals,
            created_at=created_at,
        )

    @staticmethod
    def _validate_subject(
        *,
        request: InboxRequest,
        subject: StakeholderAnswerDraft | AccessScopePreview | DisclosureDenial,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> None:
        if isinstance(subject, StakeholderAnswerDraft):
            if not isinstance(request.payload, StakeholderQuestion):
                raise ValueError("answer subject requires a stakeholder question")
            _require_citations(
                subject.governed_dataset_refs,
                grounding.governed_dataset_refs,
                "governed dataset",
            )
            _require_citations(subject.metric_refs, grounding.metric_refs, "metric")
            _require_citations(subject.lineage_refs, grounding.lineage_refs, "lineage")
            _require_citations(
                subject.material_quality_limitations,
                grounding.quality_observation_refs,
                "quality",
            )
            _require_citations(
                subject.disclosure_classifications,
                grounding.classification_refs,
                "classification",
            )
            if subject.freshness_disposition in ("current", "stale"):
                if grounding.freshness_observation_ref is None:
                    raise ValueError("freshness observation is required for current or stale")
                if not grounding.data_observation_refs:
                    raise ValueError("factual answer requires a data observation")
            if (
                subject.freshness_disposition == "unknown"
                and grounding.freshness_observation_ref is not None
            ):
                raise ValueError("unknown freshness requires a null freshness observation")
            return

        if isinstance(subject, AccessScopePreview):
            if not isinstance(request.payload, DataAccessRequest):
                raise ValueError("access subject requires a data access request")
            if subject.requester_principal_ref != policy.requester_principal_ref:
                raise ValueError("access subject must name the requester principal")
            if subject.data_product_ref.artifact_id != request.payload.data_product_id:
                raise ValueError("access subject must name the requested data product")
            if subject.data_product_ref not in policy.permitted_data_product_refs:
                raise ValueError("access subject names an unpermitted data product")
            if subject.access_mode != request.payload.access_mode:
                raise ValueError("access mode must equal the requested mode")
            if subject.requested_fields != request.payload.requested_fields:
                raise ValueError("preview requested fields must equal the request")
            if not set(subject.effective_fields).issubset(request.payload.requested_fields):
                raise ValueError("effective fields must be a subset of requested fields")
            if subject.expires_at > request.payload.expires_at:
                raise ValueError("preview expiry cannot exceed the request")
            if policy.maximum_expiry is not None and subject.expires_at > policy.maximum_expiry:
                raise ValueError("preview expiry cannot exceed policy")
            _require_citations(
                subject.effective_object_refs,
                grounding.governed_dataset_refs,
                "effective object",
            )
            _require_citations(
                subject.classifications,
                grounding.classification_refs,
                "classification",
            )


class FulfillmentApprovalBinding(ArtifactModel):
    approval_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    subject_digest: str = Field(pattern=_DIGEST_PATTERN)
    actor_id: str = Field(min_length=1)
    authority_ref: str = Field(min_length=1)
    decision: FulfillmentDecision
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")


class AccessGrantEffectTarget(ArtifactModel):
    surface: AccessGrantEffectSurface
    provider_resource_ref: str = Field(min_length=1)


class AccessGrantAdmissionBinding(ArtifactModel):
    grant_id: str = Field(min_length=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_revision: int = Field(gt=0)
    effective_at: datetime
    permissions: tuple[AccessGrantPermission, ...] = Field(min_length=1)
    targets: tuple[AccessGrantEffectTarget, ...] = Field(min_length=1)

    @field_validator("effective_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "effective_at")

    @field_validator("permissions")
    @classmethod
    def permissions_are_canonical(
        cls, value: tuple[AccessGrantPermission, ...]
    ) -> tuple[AccessGrantPermission, ...]:
        _require_unique(value, "access grant permissions")
        return tuple(sorted(value))

    @field_validator("targets")
    @classmethod
    def targets_are_canonical(
        cls, value: tuple[AccessGrantEffectTarget, ...]
    ) -> tuple[AccessGrantEffectTarget, ...]:
        surfaces = tuple(target.surface for target in value)
        _require_unique(surfaces, "access grant target surfaces")
        return tuple(sorted(value, key=lambda target: target.surface))


class FulfillmentAdmissionReceipt(ArtifactModel):
    admission_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
    resulting_request_revision: int = Field(ge=2)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    grounding_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    approval_ids: tuple[str, ...] = Field(min_length=1)
    access_grant_binding: AccessGrantAdmissionBinding | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    admitted_at: datetime
    execution_status: Literal["ready_for_execution"] = "ready_for_execution"

    @field_validator("admitted_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "admitted_at")


class FulfillmentDeliveryReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    delivery_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
    resulting_request_revision: int = Field(ge=3)
    admission_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    answer: StakeholderAnswerDraft
    verification_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    delivered_at: datetime

    @field_validator("delivered_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "delivered_at")

    @model_validator(mode="after")
    def has_unique_verification_references(self) -> Self:
        _require_unique(self.verification_refs, "verification_refs")
        return self


class AccessGrantDeliveryObservation(ArtifactModel):
    grant_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_revision: int = Field(gt=0)
    effective_at: datetime
    expires_at: datetime
    permissions: tuple[AccessGrantPermission, ...] = Field(min_length=1)
    targets: tuple[AccessGrantEffectTarget, ...] = Field(min_length=1)
    effect_receipt_refs: tuple[ArtifactReference, ...] = Field(min_length=1)

    @field_validator("effective_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "access grant timestamp")

    @field_validator("permissions")
    @classmethod
    def delivery_permissions_are_canonical(
        cls, value: tuple[AccessGrantPermission, ...]
    ) -> tuple[AccessGrantPermission, ...]:
        _require_unique(value, "access grant permissions")
        return tuple(sorted(value))

    @field_validator("targets")
    @classmethod
    def delivery_targets_are_canonical(
        cls, value: tuple[AccessGrantEffectTarget, ...]
    ) -> tuple[AccessGrantEffectTarget, ...]:
        surfaces = tuple(target.surface for target in value)
        _require_unique(surfaces, "access grant target surfaces")
        return tuple(sorted(value, key=lambda target: target.surface))

    @field_validator("effect_receipt_refs")
    @classmethod
    def effect_receipts_are_unique(
        cls, value: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        _require_unique(value, "access grant effect receipt references")
        return value

    @model_validator(mode="after")
    def effective_window_is_valid(self) -> Self:
        if self.expires_at <= self.effective_at:
            raise ValueError("access grant expiry must follow its effective time")
        return self


class FulfillmentAccessDeliveryReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    delivery_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
    resulting_request_revision: int = Field(ge=3)
    admission_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    access_mode: Literal["query", "dashboard", "export"]
    fields: tuple[str, ...] = Field(min_length=1)
    effective_at: datetime
    expires_at: datetime
    permissions: tuple[AccessGrantPermission, ...] = Field(min_length=1)
    verification_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    delivered_at: datetime

    @field_validator("effective_at", "expires_at", "delivered_at")
    @classmethod
    def access_delivery_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "access delivery timestamp")

    @field_validator("fields")
    @classmethod
    def access_delivery_fields_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_unique(value, "access delivery fields")
        if any(not field for field in value):
            raise ValueError("access delivery fields must not be empty")
        return tuple(sorted(value))

    @field_validator("permissions")
    @classmethod
    def access_delivery_permissions_are_canonical(
        cls, value: tuple[AccessGrantPermission, ...]
    ) -> tuple[AccessGrantPermission, ...]:
        _require_unique(value, "access delivery permissions")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def access_delivery_is_consistent(self) -> Self:
        if self.expires_at <= self.effective_at:
            raise ValueError("access delivery expiry must follow its effective time")
        _require_unique(self.verification_refs, "verification_refs")
        return self


class DenialDispositionReceipt(ArtifactModel):
    disposition_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
    resulting_request_revision: int = Field(ge=2)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    approval_ids: tuple[str, ...] = Field(min_length=1)
    requester_safe_explanation: str = Field(min_length=1, max_length=1000)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "recorded_at")


class RequestDependency(ArtifactModel):
    dependency_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    parent_request_id: str = Field(min_length=1)
    parent_request_revision: int = Field(ge=1)
    child_request_id: str = Field(min_length=1)
    child_request_revision: int = Field(ge=1)
    kind: DependencyKind
    reason_code: str = Field(min_length=1)
    blocking: Literal[True] = True
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")

    @model_validator(mode="after")
    def is_not_a_self_dependency(self) -> Self:
        if self.parent_request_id == self.child_request_id:
            raise ValueError("request cannot depend on itself")
        return self


class RequestNoValidPlan(ArtifactModel):
    record_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    source_request_revision: int = Field(ge=1)
    resulting_request_revision: int = Field(ge=2)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    constraint_refs: tuple[ArtifactReference, ...]
    smallest_changes: tuple[str, ...] = Field(min_length=1)
    requester_safe_explanation: str | None = Field(default=None, min_length=1, max_length=1000)
    grounding_snapshot_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    policy_snapshot_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")


class FulfillmentEvidenceReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    evidence_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    outcome: FulfillmentOutcome
    proposal_id: str | None = Field(default=None, min_length=1)
    proposal_revision: int | None = Field(default=None, ge=1)
    dependency_id: str | None = Field(default=None, min_length=1)
    authority_refs: tuple[str, ...]
    approval_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    resulting_state: RequestState
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _timezone_aware_utc(value, "created_at")

    @model_validator(mode="after")
    def carries_only_the_outcome_reference(self) -> Self:
        has_proposal = self.proposal_id is not None and self.proposal_revision is not None
        if (self.proposal_id is None) != (self.proposal_revision is None):
            raise ValueError("proposal identity must be complete")
        if self.outcome in ("execution_ready", "delivered", "denial") and not has_proposal:
            raise ValueError("proposal outcome requires a proposal identity")
        if self.outcome == "dependency" and self.dependency_id is None:
            raise ValueError("dependency outcome requires a dependency identity")
        if self.outcome != "dependency" and self.dependency_id is not None:
            raise ValueError("non-dependency outcome cannot name a dependency")
        if self.outcome in ("no_valid_plan", "cancelled") and has_proposal:
            raise ValueError("terminal non-proposal outcome cannot name a proposal")
        return self
