from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Literal, Self

from heinzel_state import (
    IncidentAutomaticAction,
    IncidentFailureClassification,
    IncidentKind,
    IncidentStage,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

PUBLIC_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
FIXTURE_EVIDENCE_POLICY_MARKER = "x-heinzel-fixture-evidence-policy"
FIXTURE_EVIDENCE_POLICIES: Mapping[str, JsonSchemaValue] = MappingProxyType(
    {
        "evidence_ref": {"enum": [None, ""]},
        "evidence_refs": {"type": "array", "maxItems": 0},
    }
)

type PublicId = Annotated[str, Field(pattern=PUBLIC_ID_PATTERN)]
type Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]
type OpaqueToken = Annotated[str, Field(min_length=32, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]
type NonEmptyText = Annotated[str, Field(min_length=1, max_length=16_000)]
type DataProvenance = Literal["demo_fixture", "governed_local"]
type ActorRole = Literal[
    "requester", "data_architect", "data_owner", "policy_approver", "budget_approver"
]
type CapabilityState = Literal["ready", "blocked", "degraded", "not_delivered"]
type OperationState = Literal["accepted", "running", "succeeded", "failed", "outcome_unknown"]
# Mirrors the evidence store's `RunState` exactly; see `RunView`.
type RunLifecycleState = Literal["created", "running", "succeeded", "failed", "non_conforming"]
# Mirrors state's `RunLifecycleStatus`; a contract test pins the two together.
type LeasedRunStatusView = Literal[
    "pending", "leased", "lease_expired", "retryable", "succeeded", "failed", "cancelled"
]
# Mirror the acquisition evidence receipt's own vocabularies. The console does not
# depend on the provider SDK that declares `acquisition_mode`, so these are restated
# rather than imported, and a test pins each one to the owning model's annotation so
# a mirror that drifts fails there instead of rejecting a receipt at a live read.
type AcquisitionModeView = Literal["snapshot", "incremental", "reconciliation"]
type AcquisitionOutcomeView = Literal[
    "prepared",
    "acknowledged",
    "no_valid_plan",
    "resynchronization_required",
    "failed",
]
type AcquisitionReasonCodeView = Literal[
    "acquisition_mode_not_admitted",
    "authorization_denied",
    "contract_invalid",
    "contract_not_activated",
    "encoded_byte_ceiling_exceeded",
    "encoded_byte_ceiling_not_admitted",
    "integrity_failure",
    "logical_object_not_admitted",
    "physical_delete_capture_unsupported",
    "provider_unavailable",
    "rate_limited",
    "record_ceiling_exceeded",
    "record_ceiling_not_admitted",
    "source_binding_authority_stale",
    "source_binding_not_admitted",
    "source_drift",
    "source_observation_not_admitted",
    "stale_checkpoint",
    "stripe_event_cursor_expired",
    "stripe_event_overlap_gap",
]
type ReviewKind = Literal["meaning", "data_product", "activation"]
type RequestKind = Literal["stakeholder_question", "data_access"]
# Terminal outcomes stay distinct: a requester must be able to tell an answer from a refusal.
type RequestState = Literal[
    "submitted",
    "clarifying",
    "investigating",
    "proposed",
    "awaiting_approval",
    "execution_ready",
    "denied",
    "delivered",
    "no_valid_plan",
    "cancelled",
    "failed",
    "closed",
]
type Decision = Literal["approve", "reject", "request_changes"]
type SetupStage = Literal[
    "foundation",
    "managed_services",
    "sources",
    "business_process",
    "meaning",
    "data_product",
    "activation",
]
type SetupStageState = Literal["not_started", "current", "blocked", "complete"]
type WarehouseEngine = Literal["postgresql", "clickhouse"]
type WorkspaceState = Literal["setup", "pending_activation", "active", "unavailable"]
type RiskLevel = Literal["low", "medium", "high", "critical"]
type FreshnessState = Literal["current", "stale", "unknown", "not_applicable"]
type DashboardAccessViewState = Literal["active", "workspace_role"]
type AnswerResultStatus = Literal["available", "expired", "failed"]
type AnswerResultValueType = Literal["boolean", "decimal", "integer", "string", "timestamp"]
type AccessMode = Literal["query", "dashboard", "export"]
# Mirrors `BoundSemanticReference.kind`: the only two kinds of approved term an answer intent can
# name. A test pins the two together, so a third kind fails there rather than being dropped from
# what a requester is offered.
type AnswerTermKind = Literal["dimension", "metric"]
# Mirror the connection broker's own source vocabularies: which provider reads a source, which
# kind of account the credential is for, and where the binding stands in its lifecycle. Restated
# rather than imported, as the acquisition vocabularies above are, and a contract test pins each
# one to the owning model so a mirror that drifts fails there instead of dropping a registered
# source out of what an architect is shown.
type SourceProviderKind = Literal["postgresql", "stripe"]
type SourceAccountModeView = Literal["not_applicable", "test", "live"]
type SourceBindingStateView = Literal[
    "draft", "validating", "ready", "suspended", "failed", "retired"
]
type ProductAggregation = Literal["sum", "count", "minimum", "maximum", "average"]
type ProductFilterOperator = Literal["equals", "not_equals", "in", "greater_than", "less_than"]
type ProductDeliveryOutput = Literal["dataset", "table", "dashboard"]
type RecoveryAction = Literal[
    "correct_input", "reauthenticate", "reload", "retry", "contact_support", "none"
]
type OperationalRecoveryAction = Literal[
    "retry_transient_attempt",
    "cancel_unstarted_work",
    "reconcile_external_effect",
]
type ImpactChangeType = Literal[
    "source_drift",
    "metric_version_change",
    "contract_supersession",
    "generation_failure",
    "policy_change",
    "grant_change",
    "retirement",
]


_RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _parse_json_datetime(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or _RFC3339_DATETIME_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp must be an RFC 3339 string or datetime object")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError("timestamp must be a valid RFC 3339 datetime") from error


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("timestamp must normalize to UTC within years 1 through 9999") from error


type UtcDatetime = Annotated[
    datetime, BeforeValidator(_parse_json_datetime), AfterValidator(_as_utc)
]
type AnswerResultCell = str | int | float | bool | Decimal | UtcDatetime | None


def _json_array_to_tuple(value: object) -> object:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise ValueError("value must be a JSON array or tuple")


type JsonTuple[Item] = Annotated[tuple[Item, ...], BeforeValidator(_json_array_to_tuple)]
type NonEmptyJsonTuple[Item] = Annotated[
    tuple[Item, ...], Field(min_length=1), BeforeValidator(_json_array_to_tuple)
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ApiMeta(StrictModel):
    data_provenance: DataProvenance
    correlation_id: PublicId


def _is_non_empty_evidence_field(field_name: object, field_value: object) -> bool:
    return (
        isinstance(field_name, str)
        and field_name in FIXTURE_EVIDENCE_POLICIES
        and field_value not in (None, (), [], {}, set(), frozenset(), "")
    )


def _contains_authoritative_evidence(value: object, seen: set[int] | None = None) -> bool:
    if seen is None:
        seen = set()

    if isinstance(value, (BaseModel, Mapping, tuple, list, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)

    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            field_value = getattr(value, field_name)
            if _is_non_empty_evidence_field(field_name, field_value):
                return True
            if _contains_authoritative_evidence(field_value, seen):
                return True
        return False
    if isinstance(value, Mapping):
        for field_name, field_value in value.items():
            if _is_non_empty_evidence_field(field_name, field_value):
                return True
            if _contains_authoritative_evidence(
                field_name, seen
            ) or _contains_authoritative_evidence(field_value, seen):
                return True
        return False
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_authoritative_evidence(item, seen) for item in value)
    return not (value is None or isinstance(value, (str, int, float, bool, datetime)))


class ConsoleEnvelope[ResponseData](StrictModel):
    model_config = ConfigDict(
        json_schema_extra={"x-heinzel-console-envelope": True},
    )

    meta: ApiMeta
    data: ResponseData

    @model_validator(mode="after")
    def fixture_data_is_not_authoritative_evidence(self) -> Self:
        if self.meta.data_provenance == "demo_fixture" and _contains_authoritative_evidence(
            self.data
        ):
            raise ValueError("fixture responses cannot carry evidence")
        return self


class ActorDisplayView(StrictModel):
    display_name: NonEmptyText


class DisplayReferenceView(StrictModel):
    ref: PublicId
    display_name: NonEmptyText


class SessionView(StrictModel):
    actor: ActorDisplayView
    roles: NonEmptyJsonTuple[ActorRole]
    active_role: ActorRole
    tenant: DisplayReferenceView
    workspace: DisplayReferenceView
    csrf_token: str = Field(min_length=32, max_length=256)

    @model_validator(mode="after")
    def active_role_is_held(self) -> Self:
        if self.active_role not in self.roles:
            raise ValueError("active role must be one of the session roles")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema["allOf"] = [
            {
                "if": {
                    "properties": {"active_role": {"const": role}},
                    "required": ["active_role"],
                },
                "then": {
                    "properties": {"roles": {"type": "array", "contains": {"const": role}}},
                    "required": ["roles"],
                },
            }
            for role in (
                "requester",
                "data_architect",
                "data_owner",
                "policy_approver",
                "budget_approver",
            )
        ]
        return schema


class CapabilityView(StrictModel):
    capability_id: PublicId
    label: NonEmptyText
    state: CapabilityState
    detail: NonEmptyText
    dependency: NonEmptyText | None = None


class WorkspaceView(StrictModel):
    workspace: DisplayReferenceView
    state: WorkspaceState
    capabilities: JsonTuple[CapabilityView] = Field(default=())
    recovery_message: NonEmptyText | None = None


class SetupStageView(StrictModel):
    stage: SetupStage
    label: NonEmptyText
    state: SetupStageState
    detail: NonEmptyText | None = None


class WarehouseOptionView(StrictModel):
    engine: WarehouseEngine
    label: NonEmptyText
    supported_region: NonEmptyText
    fixed_capacity: NonEmptyText


class WarehouseBindingView(StrictModel):
    binding_ref: PublicId
    engine: WarehouseEngine
    region: NonEmptyText
    capacity: NonEmptyText
    state: CapabilityState
    immutable: Literal[True] = True


class ManagedServiceView(StrictModel):
    service: Literal["warehouse", "openmetadata", "superset"]
    label: NonEmptyText
    state: CapabilityState
    detail: NonEmptyText
    validation_summary: NonEmptyText | None = None


class SourceConnectionView(StrictModel):
    """One source the connection broker holds a binding for, as an architect reads it.

    `connection_handle` is the name an operator enrolled the connection under, and is the whole
    of what the console knows about reaching the source: the connection detail itself is held by
    whatever secret custody the deployment injected into the broker, and never travels here.
    The handle is free text rather than a console public identifier because the deployment names
    its own handles.

    `capability_authority_digest` is present exactly when the binding is `ready`, because
    `record_validation` is the only writer of that state and it requires the two-probe evidence
    this digest comes from. So the digest is the console's evidence that the source was probed,
    rather than a claim this projection makes about it.
    """

    source_ref: PublicId
    source_type: SourceProviderKind
    display_name: NonEmptyText
    state: CapabilityState
    lifecycle_state: SourceBindingStateView | None = None
    connection_handle: NonEmptyText | None = None
    account_mode: SourceAccountModeView | None = None
    approved_object_refs: JsonTuple[NonEmptyText] = Field(default=())
    capability_authority_digest: Digest | None = None
    intended_checks: JsonTuple[NonEmptyText] = Field(default=())
    denied_checks: JsonTuple[NonEmptyText] = Field(default=())


class EnrollableSourceHandleView(StrictModel):
    """One connection an operator enrolled that no binding names yet.

    This is an offer to register, not a connection: a handle, the provider that would read it,
    the account mode the credential behind it is for, and the logical objects the deployment
    declares for that handle. No endpoint, no credential and no reference to either -- the
    console never receives a connection detail, and registering does not send it one.

    The declared objects come from the deployment's own declaration rather than from the
    browser, because the probe requires the declaration it validates against to equal the
    binding's approved objects: a set typed into a form would be refused by the probe at best,
    and would be an unapproved declaration reaching a registration at worst.
    """

    connection_handle: NonEmptyText
    source_type: SourceProviderKind
    account_mode: SourceAccountModeView
    declared_object_refs: NonEmptyJsonTuple[NonEmptyText]


class ProcessPackageView(StrictModel):
    package_ref: PublicId
    version: int = Field(ge=1)
    content_digest: Digest
    state: CapabilityState
    candidate_summary: NonEmptyText


class SetupView(StrictModel):
    workspace_ref: PublicId
    revision: int = Field(ge=1)
    setup_digest: Digest
    reset_token: OpaqueToken
    active_stage: SetupStage
    stages: NonEmptyJsonTuple[SetupStageView]
    warehouse_options: NonEmptyJsonTuple[WarehouseOptionView]
    warehouse_binding: WarehouseBindingView | None = None
    managed_services: JsonTuple[ManagedServiceView] = Field(default=())
    sources: JsonTuple[SourceConnectionView] = Field(default=())
    enrollable_sources: JsonTuple[EnrollableSourceHandleView] = Field(default=())
    process_package: ProcessPackageView | None = None
    pending_review_refs: JsonTuple[PublicId] = Field(default=())


def _canonical_setup_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_setup_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        canonical: dict[str, object] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ValueError("setup snapshot keys must be strings")
            # `setup_digest` is the output. `reset_token` is an authorization
            # capability the server mints per read, so including it would make the
            # digest identify the moment of the read rather than the setup state,
            # and no command guarded by it could ever be satisfied.
            if key not in {"setup_digest", "reset_token"}:
                canonical[key] = _canonical_setup_value(nested_value)
        return canonical
    if isinstance(value, (tuple, list)):
        return [_canonical_setup_value(item) for item in value]
    return value


def setup_snapshot_digest(snapshot: SetupView | Mapping[str, object]) -> str:
    canonical = json.dumps(
        _canonical_setup_value(snapshot),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class ReviewItemView(StrictModel):
    label: NonEmptyText
    value: NonEmptyText
    material_change: bool = False


class ReviewSectionView(StrictModel):
    section_id: PublicId
    title: NonEmptyText
    summary: NonEmptyText | None = None
    items: JsonTuple[ReviewItemView] = Field(default=())


class AuthorityRequirementView(StrictModel):
    role: ActorRole
    reason: NonEmptyText
    subject_digest: Digest
    satisfied: bool


class RecordedDecisionView(StrictModel):
    role: ActorRole
    decision: Decision
    decided_at: UtcDatetime


class ConstraintView(StrictModel):
    code: PublicId
    summary: NonEmptyText
    responsible_role: ActorRole
    permitted_next_action: NonEmptyText


class ReviewView(StrictModel):
    review_id: PublicId
    kind: ReviewKind
    title: NonEmptyText
    summary: NonEmptyText
    revision: int = Field(ge=1)
    reviewed_digest: Digest
    sections: NonEmptyJsonTuple[ReviewSectionView]
    required_authorities: JsonTuple[AuthorityRequirementView] = Field(default=())
    decisions: JsonTuple[RecordedDecisionView] = Field(default=())
    constraints: JsonTuple[ConstraintView] = Field(default=())
    evidence_refs: JsonTuple[PublicId] = Field(
        default=(),
        json_schema_extra={
            FIXTURE_EVIDENCE_POLICY_MARKER: FIXTURE_EVIDENCE_POLICIES["evidence_refs"]
        },
    )
    can_decide: bool


class InboxItemView(StrictModel):
    request_id: PublicId
    kind: RequestKind
    state: RequestState
    title: NonEmptyText
    purpose: NonEmptyText
    risk: RiskLevel
    deadline: UtcDatetime | None = None
    blocked_reason: NonEmptyText | None = None


class InboxView(StrictModel):
    items: JsonTuple[InboxItemView] = Field(default=())
    selected_request_id: PublicId | None = None


class ConversationMessageView(StrictModel):
    message_id: PublicId
    author_label: NonEmptyText
    author_role: ActorRole | Literal["heinzel"] | None
    body: NonEmptyText
    created_at: UtcDatetime


class ConversationView(StrictModel):
    request_id: PublicId
    revision: int = Field(ge=1)
    # ConversationMessageCommand requires `conversation_digest`; without it here
    # the browser has nothing to send and cannot post a reply at all.
    conversation_digest: Digest
    messages: JsonTuple[ConversationMessageView] = Field(default=())
    awaiting_role: ActorRole | None = None


class ClarifiedOutcomeView(StrictModel):
    request_id: PublicId
    revision: int = Field(ge=1)
    statement_digest: Digest
    restated_request: NonEmptyText
    purpose: NonEmptyText
    in_scope_summary: NonEmptyText
    out_of_scope_summary: NonEmptyText
    accepted: bool


class OwnDecisionView(StrictModel):
    decision: Decision
    subject_label: NonEmptyText
    created_at: UtcDatetime


class DeliveredAccessView(StrictModel):
    access_mode: AccessMode
    fields: NonEmptyJsonTuple[NonEmptyText]
    effective_at: UtcDatetime
    expires_at: UtcDatetime
    permissions: NonEmptyJsonTuple[Literal["dashboard", "download", "query", "view"]]


class AccessLifecycleView(StrictModel):
    state: Literal["pending", "active", "expired", "revocation_pending", "revoked", "failed"]
    title: NonEmptyText
    summary: NonEmptyText
    effective_at: UtcDatetime
    expires_at: UtcDatetime
    revision: int = Field(ge=1)
    can_revoke: bool


class RequesterRequestView(StrictModel):
    request_id: PublicId
    kind: RequestKind
    state: RequestState
    title: NonEmptyText
    requested_outcome: NonEmptyText
    revision: int = Field(ge=1)
    updated_at: UtcDatetime
    result_page_available: bool = False
    own_decisions: JsonTuple[OwnDecisionView] = Field(default=())
    clarified_outcome: ClarifiedOutcomeView | None = None
    question: NonEmptyText | None = None
    denial_explanation: NonEmptyText | None = None
    no_valid_plan_explanation: NonEmptyText | None = None
    delivered_answer: DeliveredAnswerView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    delivered_access: DeliveredAccessView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    access_lifecycle: AccessLifecycleView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ArtifactReferenceView(StrictModel):
    artifact_id: str
    version: int = Field(ge=1)
    digest: Digest


class DeliveredAnswerView(StrictModel):
    answer_text: NonEmptyText
    as_of: UtcDatetime
    freshness: FreshnessState
    datasets: JsonTuple[ArtifactReferenceView] = Field(default=())
    metrics: JsonTuple[ArtifactReferenceView] = Field(default=())
    lineage: JsonTuple[ArtifactReferenceView] = Field(default=())
    quality_limitations: JsonTuple[ArtifactReferenceView] = Field(default=())
    delivery_ref: PublicId


class AnswerResultColumnView(StrictModel):
    name: NonEmptyText
    label: NonEmptyText
    value_type: AnswerResultValueType
    allowed_operations: JsonTuple[Literal["sort", "filter"]] = Field(default=())


class AnswerResultTechnicalDetailsView(StrictModel):
    execution_receipt_id: NonEmptyText
    plan_digest: Digest
    result_digest: Digest
    result_schema_digest: Digest


class AnswerResultPageView(StrictModel):
    request_id: PublicId
    title: NonEmptyText
    answer_text: NonEmptyText | None
    status: AnswerResultStatus
    freshness: FreshnessState | None
    as_of: UtcDatetime | None
    row_count: int = Field(ge=0)
    columns: JsonTuple[AnswerResultColumnView] = Field(default=())
    rows: JsonTuple[JsonTuple[AnswerResultCell]] = Field(default=())
    next_cursor: OpaqueToken | None = None
    technical_details: AnswerResultTechnicalDetailsView | None = None

    @model_validator(mode="after")
    def unavailable_results_never_expose_snapshot_content(self) -> Self:
        if self.status != "available" and (
            self.columns or self.rows or self.next_cursor or self.technical_details is not None
        ):
            raise ValueError("unavailable results cannot expose snapshot content")
        return self


class ProposalApprovalView(StrictModel):
    authority_ref: NonEmptyText
    reason: NonEmptyText
    satisfied: bool
    # What a reviewer reads. The reference stays authoritative; the label is presentation only and
    # is absent when the deployment cannot name the authority.
    authority_label: NonEmptyText | None = None


class DatasetEvidenceView(StrictModel):
    dataset_ref: PublicId
    display_name: NonEmptyText
    artifact_reference: ArtifactReferenceView | None = None


class AuthorityStatusView(StrictModel):
    role: ActorRole
    reason: NonEmptyText
    satisfied: bool


class StakeholderAnswerProposalView(StrictModel):
    kind: Literal["stakeholder_answer"]
    purpose: NonEmptyText
    candidate: NonEmptyText
    metric_version: NonEmptyText
    metric_references: JsonTuple[ArtifactReferenceView] = Field(default=())
    lineage_references: JsonTuple[ArtifactReferenceView] = Field(default=())
    quality_references: JsonTuple[ArtifactReferenceView] = Field(default=())
    as_of: UtcDatetime
    freshness: FreshnessState
    quality_limitations: JsonTuple[NonEmptyText] = Field(default=())
    datasets: JsonTuple[DatasetEvidenceView] = Field(default=())
    lineage_summary: NonEmptyText
    authorization_summary: NonEmptyText
    required_authorities: JsonTuple[AuthorityStatusView] = Field(default=())
    required_approvals: JsonTuple[ProposalApprovalView] = Field(default=())


class DisclosureDenialProposalView(StrictModel):
    kind: Literal["disclosure_denial"]
    explanation: NonEmptyText
    reason_code: NonEmptyText
    required_authorities: JsonTuple[AuthorityStatusView] = Field(default=())
    required_approvals: JsonTuple[ProposalApprovalView] = Field(default=())


class AccessPreviewProposalView(StrictModel):
    kind: Literal["access_preview"]
    purpose: NonEmptyText
    data_product_ref: PublicId
    data_product_reference: ArtifactReferenceView | None = None
    effective_object_references: JsonTuple[ArtifactReferenceView] = Field(default=())
    access_mode: AccessMode
    requested_fields: NonEmptyJsonTuple[NonEmptyText]
    effective_scope: JsonTuple[NonEmptyText] = Field(default=())
    exclusions: JsonTuple[NonEmptyText] = Field(default=())
    expires_at: UtcDatetime
    intended_checks: JsonTuple[NonEmptyText] = Field(default=())
    denied_checks: JsonTuple[NonEmptyText] = Field(default=())
    authority_summary: NonEmptyText
    required_authorities: JsonTuple[AuthorityStatusView] = Field(default=())
    required_approvals: JsonTuple[ProposalApprovalView] = Field(default=())


type RequestProposalView = Annotated[
    StakeholderAnswerProposalView | AccessPreviewProposalView | DisclosureDenialProposalView,
    Field(discriminator="kind"),
]


class LifecycleEventView(StrictModel):
    event_id: PublicId
    state: RequestState
    summary: NonEmptyText
    occurred_at: UtcDatetime


class EvidenceContextView(StrictModel):
    datasets: JsonTuple[DatasetEvidenceView] = Field(default=())
    metric_versions: JsonTuple[NonEmptyText] = Field(default=())
    metric_references: JsonTuple[ArtifactReferenceView] = Field(default=())
    as_of: UtcDatetime | None = None
    freshness: FreshnessState
    quality_summary: NonEmptyText
    lineage_summary: NonEmptyText
    authorization_summary: NonEmptyText
    evidence_refs: JsonTuple[PublicId] = Field(
        default=(),
        json_schema_extra={
            FIXTURE_EVIDENCE_POLICY_MARKER: FIXTURE_EVIDENCE_POLICIES["evidence_refs"]
        },
    )


class AdmissionView(StrictModel):
    """Whether the approved proposal can be admitted to execution, and why not.

    Admission is a separate owning transaction from a decision: it requires every
    required approval to be recorded against the exact proposal, and it is what moves
    a request past `awaiting_approval`. The console projects availability and the
    reason rather than a bare flag, because a disabled action with no stated cause is
    the thing this console exists not to do.
    """

    available: bool
    blocking_reason: NonEmptyText | None = None
    # True when the proposal was already admitted but its delivery did not complete. The same
    # command then retries the delivery instead of admitting a second time. Omitted when false,
    # so every admission projection that predates it is unchanged on the wire.
    pending_delivery: bool = Field(default=False, exclude_if=lambda value: value is False)


class ProductIntentSourceCoverageView(StrictModel):
    source_ref: NonEmptyText
    covered_fields: JsonTuple[NonEmptyText] = Field(default=())
    authorized: bool


class ProductIntentMeasureView(StrictModel):
    metric_ref: NonEmptyText
    aggregation: ProductAggregation


class ProductIntentFilterView(StrictModel):
    dimension_ref: NonEmptyText
    operator: ProductFilterOperator
    value: NonEmptyText


class ProductIntentReviewView(StrictModel):
    reviewed_digest: Digest
    approved: bool
    approved_intent_revision: int | None = Field(default=None, ge=1)
    title: NonEmptyText
    business_outcome: NonEmptyText
    source_coverage: NonEmptyJsonTuple[ProductIntentSourceCoverageView]
    grain: NonEmptyJsonTuple[NonEmptyText]
    measures: NonEmptyJsonTuple[ProductIntentMeasureView]
    dimensions: JsonTuple[NonEmptyText] = Field(default=())
    filters: JsonTuple[ProductIntentFilterView] = Field(default=())
    freshness_seconds: int = Field(gt=0)
    outputs: NonEmptyJsonTuple[ProductDeliveryOutput]
    unresolved_constraints: JsonTuple[NonEmptyText] = Field(default=())

    @model_validator(mode="after")
    def approval_revision_matches_status(self) -> Self:
        if self.approved != (self.approved_intent_revision is not None):
            raise ValueError("approved intent revision must be present exactly when approved")
        return self


type PreparationAction = Literal["clarify", "prepare_access", "prepare_answer", "submit_proposal"]


class RequestDetailView(StrictModel):
    request_id: PublicId
    kind: RequestKind
    state: RequestState
    title: NonEmptyText
    purpose: NonEmptyText
    revision: int = Field(ge=1)
    proposal_digest: Digest | None = None
    proposal: RequestProposalView | None = None
    conversation: ConversationView
    lifecycle: JsonTuple[LifecycleEventView] = Field(default=())
    evidence: EvidenceContextView
    available_actions: JsonTuple[Decision] = Field(default=())
    admission: AdmissionView | None = None
    preparation_actions: JsonTuple[PreparationAction] = Field(default=())
    preparation_notes: JsonTuple[NonEmptyText] = Field(default=())
    question: NonEmptyText | None = None
    product_intent: ProductIntentReviewView | None = None
    access_lifecycle: AccessLifecycleView | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ImpactItemView(StrictModel):
    impact_handle: PublicId
    label: NonEmptyText
    asset_type: NonEmptyText
    owner_label: NonEmptyText


class ImpactApproverView(StrictModel):
    authority_label: NonEmptyText
    reason: NonEmptyText


class ImpactView(StrictModel):
    request_id: PublicId
    change_type: ImpactChangeType
    subject_label: NonEmptyText
    analyzed_at: UtcDatetime
    validated_impacts: JsonTuple[ImpactItemView] = Field(default=())
    possible_impacts: JsonTuple[ImpactItemView] = Field(default=())
    affected_owners: JsonTuple[NonEmptyText] = Field(default=())
    added_approvers: JsonTuple[ImpactApproverView] = Field(default=())


class OperationFailureView(StrictModel):
    code: PublicId
    classification: Literal["transient", "permanent", "unknown"]
    safe_message: NonEmptyText


class OperationView(StrictModel):
    operation_id: PublicId
    revision: int = Field(ge=1)
    state: OperationState
    phase: NonEmptyText = "operation"
    summary: NonEmptyText = "Operation status available"
    evidence_ref: PublicId | None = Field(
        default=None,
        json_schema_extra={
            FIXTURE_EVIDENCE_POLICY_MARKER: FIXTURE_EVIDENCE_POLICIES["evidence_ref"]
        },
    )
    failure: OperationFailureView | None = None
    recovery_actions: JsonTuple[RecoveryAction] = Field(default=())
    operation_digest: Digest | None = None
    retry_token: OpaqueToken | None = None

    @model_validator(mode="after")
    def retry_capability_is_complete_and_transient(self) -> Self:
        has_retry_authority = self.operation_digest is not None and self.retry_token is not None
        if (self.operation_digest is None) != (self.retry_token is None):
            raise ValueError("operation digest and retry token must be issued together")
        if ("retry" in self.recovery_actions) != has_retry_authority:
            raise ValueError("retry recovery requires public retry authority")
        if has_retry_authority and (
            self.failure is None or self.failure.classification != "transient"
        ):
            raise ValueError("retry authority requires a transient operation failure")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema = handler(core_schema)
        authority = {
            "properties": {
                "operation_digest": {"type": "string"},
                "retry_token": {"type": "string"},
                "failure": {
                    "type": "object",
                    "properties": {"classification": {"const": "transient"}},
                    "required": ["classification"],
                },
                "recovery_actions": {
                    "type": "array",
                    "contains": {"const": "retry"},
                },
            },
            "required": [
                "operation_digest",
                "retry_token",
                "failure",
                "recovery_actions",
            ],
        }
        no_authority = {
            "properties": {
                "operation_digest": {"type": "null"},
                "retry_token": {"type": "null"},
                "recovery_actions": {
                    "type": "array",
                    "not": {"contains": {"const": "retry"}},
                },
            }
        }
        schema["allOf"] = [
            {
                "if": {
                    "properties": {"operation_digest": {"type": "string"}},
                    "required": ["operation_digest"],
                },
                "then": authority,
                "else": no_authority,
            },
            {
                "if": {
                    "properties": {
                        "recovery_actions": {
                            "type": "array",
                            "contains": {"const": "retry"},
                        }
                    },
                    "required": ["recovery_actions"],
                },
                "then": authority,
            },
        ]
        return schema


class DataProductView(StrictModel):
    """A policy-permitted product enriched only by its owning publication definition."""

    data_product_id: PublicId
    version: int = Field(ge=1)
    publication_status: Literal["published", "pending"]
    name: NonEmptyText | None = None
    description: NonEmptyText | None = None
    product_revision: int | None = Field(default=None, ge=1)
    generation: int | None = Field(default=None, ge=1)
    catalog_revision: int | None = Field(default=None, ge=1)
    namespace: NonEmptyText | None = None
    relation_name: NonEmptyText | None = None
    column_count: int | None = Field(default=None, ge=1)
    source_count: int | None = Field(default=None, ge=1)
    freshness_observed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def publication_metadata_matches_status(self) -> Self:
        publication_values = (
            self.name,
            self.description,
            self.product_revision,
            self.generation,
            self.catalog_revision,
            self.namespace,
            self.relation_name,
            self.column_count,
            self.source_count,
            self.freshness_observed_at,
        )
        if self.publication_status == "published" and any(
            value is None for value in publication_values
        ):
            raise ValueError("published data products require complete publication metadata")
        if self.publication_status == "pending" and any(
            value is not None for value in publication_values
        ):
            raise ValueError("pending data products cannot assert publication metadata")
        return self


class DataProductsView(StrictModel):
    products: JsonTuple[DataProductView] = Field(default=())


class RunView(StrictModel):
    """A run as the evidence store witnessed it.

    `state` carries the store's own vocabulary rather than the shared
    `OperationState`, because the two do not map without loss: a `non_conforming`
    run is a known outcome, and the nearest shared value, `outcome_unknown`, would
    report a witnessed non-conformance as ignorance.

    The timestamps are the record's own `created_at` and `updated_at`. Renaming
    them to `started_at` and `completed_at` would assert a lifecycle meaning the
    stored fields do not carry.
    """

    run_id: PublicId
    contract_digest: Digest
    state: RunLifecycleState
    created_at: UtcDatetime
    updated_at: UtcDatetime


class RunAttemptView(StrictModel):
    """One state-owned attempt: its lease, and the outcome and boundary it recorded, if any."""

    attempt_number: int = Field(ge=1)
    epoch: int = Field(ge=1)
    worker_ref: NonEmptyText
    claimed_at: UtcDatetime
    # The claim's own expiry extended by any renewals: the expiry fencing actually uses.
    lease_expires_at: UtcDatetime
    lease_extensions: int = Field(default=0, ge=0)
    outcome: Literal["succeeded", "failed"] | None = None
    failure_classification: Literal["transient", "permanent"] | None = None
    durable_boundary_ref: NonEmptyText | None = None
    completed_at: UtcDatetime | None = None


class LeasedRunView(StrictModel):
    """A run as state owns it: identity from its canonical intent, and every attempt.

    `status` is state's own reading at `observed_at`. The console offers no action here;
    retry and cancellation stay with the incident recovery flow that owns them.
    """

    run_id: NonEmptyText
    contract_id: NonEmptyText
    contract_revision: int = Field(ge=1)
    trigger_reason: Literal["scheduled", "run_now", "backfill", "retry"]
    window_starts_at: UtcDatetime
    window_ends_at: UtcDatetime
    status: LeasedRunStatusView
    last_durable_boundary_ref: NonEmptyText | None = None
    attempts: JsonTuple[RunAttemptView] = Field(default=())
    observed_at: UtcDatetime


class RunsView(StrictModel):
    runs: JsonTuple[RunView] = Field(default=())
    leased_runs: JsonTuple[LeasedRunView] = Field(default=())
    # False when a composed state run read failed. The witnessed runs above are still
    # served, and an empty `leased_runs` is then not a claim that no runs exist.
    leased_runs_available: bool = True


class IncidentView(StrictModel):
    incident_id: NonEmptyText
    revision: int = Field(ge=1)
    kind: IncidentKind
    classification: IncidentFailureClassification
    last_successful_stage: IncidentStage | None = None
    failed_stage: IncidentStage
    user_impact: NonEmptyText
    next_automatic_action: IncidentAutomaticAction | None = None
    allowed_operator_actions: JsonTuple[OperationalRecoveryAction] = Field(default=())
    opened_at: UtcDatetime
    updated_at: UtcDatetime
    recovery_recorded: bool = False


class IncidentsView(StrictModel):
    incidents: JsonTuple[IncidentView] = Field(default=())


class AcquisitionReceiptView(StrictModel):
    """An acquisition receipt as the runtime recorded it.

    Failed and governed-refusal receipts are projected beside successful ones. The
    receipt model carries `outcome` and `reason_codes` precisely so a refusal is
    publishable, and a refusal an operator cannot see is one they cannot act on.

    Every identifier here is a reference an owning service allocated, and the
    console shows the reference rather than inventing a display name for it -- the
    rule `DataProductView` already follows. They are typed as text rather than as
    `PublicId` because that vocabulary admits neither the separators these
    references use nor anything the owning model does not itself constrain:
    narrowing further would reject a receipt the owner considers valid, and the
    console would report a capability it cannot serve.

    The recovery state -- both receipt references and both checkpoint revisions --
    is deliberately absent. It says where the runtime is in its own protocol, which
    is not something an operator reads.
    """

    evidence_id: NonEmptyText
    contract_ref: NonEmptyText
    source_binding_ref: NonEmptyText
    acquisition_mode: AcquisitionModeView
    logical_object_refs: NonEmptyJsonTuple[NonEmptyText]
    outcome: AcquisitionOutcomeView
    reason_codes: JsonTuple[AcquisitionReasonCodeView] = Field(default=())
    created_at: UtcDatetime


class AcquisitionReceiptsView(StrictModel):
    receipts: JsonTuple[AcquisitionReceiptView] = Field(default=())


class AcquisitionRunNowCommand(StrictModel):
    """Ask the acquisition owner to prepare one run of an activated contract.

    The command carries no contract revision, binding, schema, or checkpoint. The
    acquisition application resolves those current authorities from `contract_ref`.
    """

    active_role: Literal["data_architect", "data_owner"]
    contract_ref: NonEmptyText
    trigger_window: NonEmptyText
    acquisition_mode: AcquisitionModeView


class CatalogAssetView(StrictModel):
    asset_ref: PublicId
    display_name: NonEmptyText
    definition: NonEmptyText
    owner: NonEmptyText | None = None
    classifications: JsonTuple[NonEmptyText] = Field(default=())
    lineage_summary: NonEmptyText
    link_ref: PublicId | None = None


class CatalogAssetsView(StrictModel):
    assets: JsonTuple[CatalogAssetView] = Field(default=())


class SelectableAnswerTermView(StrictModel):
    """One approved term a stakeholder question may be composed from.

    `term_ref` is the term's own canonical identifier -- what an answer intent names and what the
    query binding resolves to a column -- rather than a console-side label, and `approved_version`
    is the exact approved revision it resolves to. Offering a name without its version would offer
    a meaning that could have changed since the publication approved it.

    `term_ref` is text rather than a console public identifier because the publication names its
    own terms: a console that refused to display a term whose identifier did not match its own
    pattern would hide a term the semantic layer will happily resolve.
    """

    term_ref: NonEmptyText
    kind: AnswerTermKind
    approved_version: ArtifactReferenceView


class SelectableAnswerTermsView(StrictModel):
    """Exactly the terms this tenant's publication carries, in canonical order.

    An empty tuple is a publication that carries no approved term, which is distinct from the
    read refusing: a console with nothing published answers that it has nothing to offer.
    """

    terms: JsonTuple[SelectableAnswerTermView] = Field(default=())


class DashboardView(StrictModel):
    dashboard_ref: PublicId
    display_name: NonEmptyText
    version: int = Field(ge=1)
    lifecycle_state: Literal["active", "archived"]
    as_of: UtcDatetime
    freshness: FreshnessState
    access_state: DashboardAccessViewState
    published_at: UtcDatetime
    state: CapabilityState
    summary: NonEmptyText
    preview_ref: PublicId | None = None
    link_ref: PublicId | None = None


class DashboardsView(StrictModel):
    dashboards: JsonTuple[DashboardView] = Field(default=())


class EvidenceView(StrictModel):
    evidence_ref: PublicId = Field(
        json_schema_extra={
            FIXTURE_EVIDENCE_POLICY_MARKER: FIXTURE_EVIDENCE_POLICIES["evidence_ref"]
        }
    )
    summary: NonEmptyText
    occurred_at: UtcDatetime
    correlation_id: PublicId


class WarehouseBindingCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    reviewed_digest: Digest
    active_role: ActorRole
    engine: WarehouseEngine
    region: NonEmptyText
    capacity: NonEmptyText


class BusinessProcessManifestCommand(StrictModel):
    schema_version: Literal["1"] = "1"
    process_name: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    participants: JsonTuple[str]
    outcomes: JsonTuple[str]
    entities: JsonTuple[str]
    events: JsonTuple[str]
    states: JsonTuple[str]
    rules: JsonTuple[str]
    source_references: JsonTuple[str]
    unresolved_questions: JsonTuple[str]


class ProcessPackageCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    package_digest: Digest
    active_role: Literal["data_architect"]
    file_name: str = Field(min_length=4, max_length=255, pattern=r"^.+\.md$")
    media_type: Literal["text/markdown; charset=utf-8"]
    narrative_markdown: str = Field(min_length=1, max_length=1_000_000)
    manifest: BusinessProcessManifestCommand


class SourceRegistrationCommand(StrictModel):
    """Register the source behind one already enrolled connection handle.

    The handle is the only subject the browser names. Everything the broker needs beyond it --
    the provider kind, the account mode and the approved objects -- is taken from the offering
    the server read, so the browser cannot widen a declaration or name a provider for a handle
    the deployment declared differently. There is deliberately no connection field of any kind:
    a browser form that accepted a connection string would be a credential-handling surface, and
    `DemoSourceSecretStore.enroll_connection` is an operator action that happens before any
    binding exists.
    """

    expected_revision: int = Field(ge=1)
    active_role: Literal["data_architect"]
    connection_handle: NonEmptyText


class DecisionCommand(StrictModel):
    """One decision, against the exact revision and content the browser displayed.

    `review_item_id` and `revised_content` carry what `decide_item` already accepts
    and this command previously could not express, so a bundle with several undecided
    items and a change request with replacement wording both had to be refused. They
    are optional because the same command decides a fulfillment request, where
    neither has any meaning; that route refuses them rather than ignoring them.
    """

    expected_revision: int = Field(ge=1)
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_role: ActorRole
    decision: Literal["approve", "reject", "request_changes"]
    review_item_id: NonEmptyText | None = None
    revised_content: NonEmptyText | None = None


class ProductIntentApprovalCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    reviewed_digest: Digest
    active_role: Literal["data_architect"]


class ProductIntentApprovalView(StrictModel):
    approval_id: NonEmptyText
    intent_revision: int = Field(ge=1)
    intent_digest: Digest
    artifact_reference: ArtifactReferenceView
    approved_by: NonEmptyText
    approved_at: UtcDatetime


class ProposalPreparationCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    active_role: ActorRole


class RequestClarificationCommand(ProposalPreparationCommand):
    restated_request: str = Field(min_length=1, max_length=4000)
    in_scope_summary: str = Field(min_length=1, max_length=4000)
    out_of_scope_summary: str = Field(min_length=1, max_length=4000)


class AdmissionCommand(StrictModel):
    """Admit the exact proposal the architect reviewed.

    `reviewed_digest` is the proposal digest the browser displayed, so an admission
    cannot be applied to a proposal that changed after it was read.
    """

    expected_revision: int = Field(ge=1)
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_role: ActorRole


class QuestionTermSelectionInput(StrictModel):
    """The governed terms a requester composed their question from.

    Mirrors request-management's `QuestionTermSelection`, minus its `schema_version`: the console
    does not let a browser choose which version of that artifact it is writing. The model the
    payload is built with supplies it, and `request_intake_content` rebuilds the payload through
    that model, so a selection that artifact would reject never reaches the request store.
    """

    metric_ref: NonEmptyText
    dimension_refs: NonEmptyJsonTuple[NonEmptyText]


class StakeholderQuestionInput(StrictModel):
    kind: Literal["stakeholder_question"]
    purpose: NonEmptyText
    question: NonEmptyText
    selection: QuestionTermSelectionInput | None = None


class DataAccessRequestInput(StrictModel):
    kind: Literal["data_access"]
    purpose: NonEmptyText
    data_product_ref: PublicId
    requested_fields: NonEmptyJsonTuple[NonEmptyText]
    access_mode: AccessMode
    expires_at: UtcDatetime


type RequestInput = Annotated[
    StakeholderQuestionInput | DataAccessRequestInput, Field(discriminator="kind")
]


class CreateRequestCommand(StrictModel):
    expected_revision: Literal[1]
    request_digest: Digest
    active_role: Literal["requester"]
    title: NonEmptyText
    request: RequestInput


class ConversationMessageCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    conversation_digest: Digest
    active_role: ActorRole
    body: NonEmptyText


class ClarifiedOutcomeAcceptanceCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    clarified_outcome_digest: Digest
    active_role: Literal["requester"]
    decision: Literal["approve", "request_changes"]


class RequestWithdrawalCommand(StrictModel):
    """A requester withdraws their own request before any work has been admitted for it."""

    expected_revision: int = Field(ge=1)
    active_role: Literal["requester"]


class AccessRevocationCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    active_role: Literal["requester", "data_architect"]
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def reason_explains_revocation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("access revocation reason cannot be blank")
        return value


class RetryOperationCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    operation_digest: Digest
    retry_token: OpaqueToken
    active_role: ActorRole


class IncidentRecoveryCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    action: OperationalRecoveryAction
    active_role: Literal["data_architect", "data_owner"]
    reason: NonEmptyText

    @field_validator("reason")
    @classmethod
    def reason_explains_the_action(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recovery reason cannot be blank")
        return value


class ResetCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    setup_digest: Digest
    reset_token: OpaqueToken
    active_role: Literal["data_architect"]


class ApiError(StrictModel):
    code: PublicId
    safe_message: NonEmptyText
    recovery_action: RecoveryAction
    field: NonEmptyText | None = None


class ConsoleErrorEnvelope(StrictModel):
    meta: ApiMeta
    error: ApiError


class ConsoleApiSchema(StrictModel):
    session_response: ConsoleEnvelope[SessionView]
    workspace_response: ConsoleEnvelope[WorkspaceView]
    setup_response: ConsoleEnvelope[SetupView]
    review_response: ConsoleEnvelope[ReviewView]
    inbox_response: ConsoleEnvelope[InboxView]
    request_detail_response: ConsoleEnvelope[RequestDetailView]
    impact_response: ConsoleEnvelope[ImpactView]
    requester_requests_response: ConsoleEnvelope[JsonTuple[RequesterRequestView]]
    requester_request_response: ConsoleEnvelope[RequesterRequestView]
    access_lifecycle_response: ConsoleEnvelope[AccessLifecycleView]
    conversation_response: ConsoleEnvelope[ConversationView]
    clarified_outcome_response: ConsoleEnvelope[ClarifiedOutcomeView]
    data_products_response: ConsoleEnvelope[DataProductsView]
    data_product_response: ConsoleEnvelope[DataProductView]
    catalog_assets_response: ConsoleEnvelope[CatalogAssetsView]
    selectable_answer_terms_response: ConsoleEnvelope[SelectableAnswerTermsView]
    runs_response: ConsoleEnvelope[RunsView]
    incidents_response: ConsoleEnvelope[IncidentsView]
    incident_response: ConsoleEnvelope[IncidentView]
    acquisition_receipts_response: ConsoleEnvelope[AcquisitionReceiptsView]
    acquisition_receipt_response: ConsoleEnvelope[AcquisitionReceiptView]
    catalog_asset_response: ConsoleEnvelope[CatalogAssetView]
    dashboard_response: ConsoleEnvelope[DashboardView]
    dashboards_response: ConsoleEnvelope[DashboardsView]
    evidence_response: ConsoleEnvelope[EvidenceView]
    operation_response: ConsoleEnvelope[OperationView]
    product_intent_approval_response: ConsoleEnvelope[ProductIntentApprovalView]
    answer_result_response: ConsoleEnvelope[AnswerResultPageView]
    error_response: ConsoleErrorEnvelope
    warehouse_binding_command: WarehouseBindingCommand
    process_package_command: ProcessPackageCommand
    source_registration_command: SourceRegistrationCommand
    decision_command: DecisionCommand
    product_intent_approval_command: ProductIntentApprovalCommand
    admission_command: AdmissionCommand
    request_clarification_command: RequestClarificationCommand
    proposal_preparation_command: ProposalPreparationCommand
    create_request_command: CreateRequestCommand
    conversation_message_command: ConversationMessageCommand
    clarified_outcome_acceptance_command: ClarifiedOutcomeAcceptanceCommand
    request_withdrawal_command: RequestWithdrawalCommand
    access_revocation_command: AccessRevocationCommand
    retry_operation_command: RetryOperationCommand
    incident_recovery_command: IncidentRecoveryCommand
    acquisition_run_now_command: AcquisitionRunNowCommand
    reset_command: ResetCommand
