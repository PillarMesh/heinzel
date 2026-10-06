from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel, ArtifactReference
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .fulfillment_models import FreshnessDisposition, StakeholderAnswerDraft
from .models import QuestionTermSelection

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type AnswerIntentKind = Literal["definition", "metric_value"]
type AnswerInterpreterKind = Literal["form", "model"]
type AnswerValidationOutcome = Literal[
    "admitted",
    "clarification_required",
    "denied",
    "dependency_required",
    "review_required",
    "no_valid_plan",
]
type AnswerValidationReason = Literal[
    "authority_conflict",
    "ambiguous_reference",
    "unknown_candidate_reference",
    "filter_value_outside_domain",
    "not_entitled",
    "product_not_answer_enabled",
    "outside_policy_scope",
    "time_window_exceeded",
    "stale_product",
    "quality_blocked",
]
type FilterOperator = Literal["equals", "in"]
type OrderDirection = Literal["ascending", "descending"]


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _unique(values: tuple[object, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


class FilterCandidate(ArtifactModel):
    dimension_ref: str = Field(min_length=1)
    operator: FilterOperator
    values: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def values_are_unique(self) -> Self:
        _unique(self.values, "filter values")
        if self.operator == "equals" and len(self.values) != 1:
            raise ValueError("equals requires exactly one filter value")
        return self


class TimeWindowCandidate(ArtifactModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def window_is_positive(self) -> Self:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")
        return self


class OrderCandidate(ArtifactModel):
    reference: str = Field(min_length=1)
    direction: OrderDirection


class AnswerIntentCandidate(ArtifactModel):
    intent_kind: AnswerIntentKind
    metric_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    filters: tuple[FilterCandidate, ...]
    time_window: TimeWindowCandidate | None
    ordering: tuple[OrderCandidate, ...]
    row_limit: int = Field(gt=0)


class AnswerQuestion(ArtifactModel):
    """What an interpreter is asked to interpret.

    `question_digest` binds the exact request content this reading is about; `selection` carries
    the governed terms that content named, so an interpreter resolves a structured choice rather
    than prose it cannot read. It is optional and excluded when absent, because a question
    recorded before the builder existed carries none, and an interpreter that needs one says so
    itself rather than substituting terms nobody chose.
    """

    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(gt=0)
    question_digest: str = Field(pattern=_DIGEST_PATTERN)
    interpreter: AnswerInterpreterKind
    interpreter_ref: str = Field(min_length=1)
    selection: QuestionTermSelection | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class AnswerQuestionIntent(ArtifactModel):
    schema_version: Literal["1"] = "1"
    intent_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(gt=0)
    question_digest: str = Field(pattern=_DIGEST_PATTERN)
    intent_kind: AnswerIntentKind
    metric_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    filters: tuple[FilterCandidate, ...]
    time_window: TimeWindowCandidate | None
    ordering: tuple[OrderCandidate, ...]
    row_limit: int = Field(gt=0)
    interpreter: AnswerInterpreterKind
    interpreter_ref: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class BoundFilter(ArtifactModel):
    dimension_ref: ArtifactReference
    operator: FilterOperator
    values: tuple[str, ...] = Field(min_length=1)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AnswerProductGenerationReference(_StrictModel):
    product_ref: ArtifactReference
    generation: int = Field(ge=1)


class AnswerIntentValidation(ArtifactModel):
    schema_version: Literal["2"] = "2"
    validation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(gt=0)
    intent_digest: str = Field(pattern=_DIGEST_PATTERN)
    semantic_version_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(gt=0)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    bound_metric_versions: tuple[ArtifactReference, ...]
    bound_dimensions: tuple[ArtifactReference, ...]
    bound_filters: tuple[BoundFilter, ...]
    restatement: str = Field(min_length=1, max_length=16_000)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...]
    outcome: AnswerValidationOutcome
    reason_codes: tuple[AnswerValidationReason, ...]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class AnswerQuestionValidationResult(ArtifactModel):
    intent: AnswerQuestionIntent
    validation: AnswerIntentValidation
    restatement_confirmation_required: bool


type AnswerQueryValue = str | int | float | Decimal | bool | datetime | None
type AnswerQueryValueType = Literal["boolean", "decimal", "integer", "string", "timestamp"]
type AnswerAdmissionKind = Literal["policy", "reviewed"]
type AnswerNarrativeSource = Literal["model", "template"]


class AnswerQueryColumnEvidence(_StrictModel):
    name: str = Field(min_length=1)
    value_type: AnswerQueryValueType


class AnswerExecutionEvidence(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    principal_class: Literal["answer_runtime"] = "answer_runtime"
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    attempt: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime
    outcome: Literal[
        "succeeded",
        "ceiling_exceeded",
        "timed_out",
        "aborted",
        "generation_unavailable",
        "provider_failed",
    ]
    provider_error_classification: (
        Literal[
            "retryable",
            "throttled",
            "authorization",
            "permanent",
            "ambiguous",
            "transient_transport",
            "transient_unavailable",
            "ambiguous_outcome",
            "authorization_denied",
            "statement_rejected",
            "invalid_provider_response",
            "integrity_failure",
            "permanent_configuration",
            "resynchronization_required",
        ]
        | None
    ) = None
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    suppressed_group_count: int = Field(ge=0)
    result_schema_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    result_ref: str | None = None
    freshness_observation_ref: str = Field(min_length=1)
    quality_observation_ref: str = Field(min_length=1)

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))


class AnswerResultEvidence(_StrictModel):
    schema_version: Literal["1"] = "1"
    result_ref: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    columns: tuple[AnswerQueryColumnEvidence, ...] = Field(min_length=1)
    rows: tuple[tuple[AnswerQueryValue, ...], ...]
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    result_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))


class AnswerScanCeilingEvidence(_StrictModel):
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class AnswerPlanCeilingsEvidence(_StrictModel):
    row_limit: int = Field(gt=0)
    scan: AnswerScanCeilingEvidence
    period_scan: AnswerScanCeilingEvidence


class AnswerPlanEvidence(_StrictModel):
    tenant_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...] = Field(min_length=1)
    minimum_group_size: int = Field(gt=0)
    statement: str = Field(min_length=1)
    ceilings: AnswerPlanCeilingsEvidence


class AnswerAdmissionEvidence(_StrictModel):
    admission_ref: str = Field(min_length=1)
    admission_kind: AnswerAdmissionKind
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    admission_request_revision: int = Field(ge=1)
    verifying_request_revision: int = Field(ge=3)
    validation_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    policy_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    reviewed_answer: StakeholderAnswerDraft | None = None

    @model_validator(mode="after")
    def reviewed_admission_has_answer(self) -> Self:
        if self.admission_kind == "reviewed" and self.reviewed_answer is None:
            raise ValueError("reviewed admission requires its approved answer draft")
        if self.admission_kind == "policy" and self.reviewed_answer is not None:
            raise ValueError("policy admission cannot carry a reviewed answer draft")
        return self


class AnswerDeliveryAuthorization(_StrictModel):
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    requester_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    policy_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_current: bool
    entitlement_current: bool
    valid_until: datetime
    row_ceiling: int = Field(gt=0)
    byte_ceiling: int = Field(gt=0)
    minimum_group_size: int = Field(gt=0)
    freshness_observation_ref: str = Field(min_length=1)
    quality_observation_ref: str = Field(min_length=1)
    freshness_disposition: FreshnessDisposition
    metric_version_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    material_quality_limitations: tuple[ArtifactReference, ...]
    lineage_refs: tuple[ArtifactReference, ...]
    as_of: datetime
    restatement: str = Field(min_length=1, max_length=4000)
    approved_narrative_terms: tuple[str, ...]

    @field_validator("valid_until", "as_of")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))


class DeliverGovernedAnswerCommand(_StrictModel):
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    requester_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    admission_ref: str = Field(min_length=1)
    execution_receipt_ref: str = Field(min_length=1)
    model_narrative: str | None = Field(default=None, min_length=1, max_length=16000)
    refreshes_answer_ref: str | None = Field(default=None, min_length=1)


class ExecuteGovernedAnswerWorkflowCommand(_StrictModel):
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    requester_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    admission_ref: str = Field(min_length=1)
    model_narrative: str | None = Field(default=None, min_length=1, max_length=16000)
    refreshes_answer_ref: str | None = Field(default=None, min_length=1)


class GovernedAnswer(_StrictModel):
    schema_version: Literal["1"] = "1"
    answer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    intent_kind: Literal["definition", "metric_value"] = "metric_value"
    restatement: str = Field(min_length=1, max_length=4000)
    admission_ref: str = Field(min_length=1)
    execution_receipt_ref: str | None = Field(default=None, min_length=1)
    metric_version_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    product_generation_refs: tuple[AnswerProductGenerationReference, ...]
    as_of: datetime
    freshness_disposition: FreshnessDisposition
    material_quality_limitations: tuple[ArtifactReference, ...]
    lineage_refs: tuple[ArtifactReference, ...]
    narrative: str = Field(min_length=1, max_length=16000)
    narrative_source: AnswerNarrativeSource
    result_ref: str | None = Field(default=None, min_length=1)
    result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    refreshes_answer_ref: str | None = Field(default=None, min_length=1)
    delivered_at: datetime

    @model_validator(mode="after")
    def evidence_matches_intent_kind(self) -> Self:
        execution_fields = (self.execution_receipt_ref, self.result_ref, self.result_digest)
        if self.intent_kind == "metric_value" and (
            any(value is None for value in execution_fields) or not self.product_generation_refs
        ):
            raise ValueError("metric value answer requires execution and result evidence")
        if self.intent_kind == "definition" and (
            any(value is not None for value in execution_fields) or self.product_generation_refs
        ):
            raise ValueError("definition answer cannot carry execution or result evidence")
        return self

    @field_validator("as_of", "delivered_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))
