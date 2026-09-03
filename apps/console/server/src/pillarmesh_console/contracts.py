from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

PUBLIC_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
FIXTURE_EVIDENCE_POLICY_MARKER = "x-pillarmesh-fixture-evidence-policy"
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
type ReviewKind = Literal["meaning", "data_product", "activation"]
type RequestKind = Literal["stakeholder_question", "data_access"]
type RequestState = Literal[
    "submitted",
    "clarifying",
    "investigating",
    "proposed",
    "awaiting_approval",
    "execution_ready",
    "denied",
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
type AccessMode = Literal["query", "dashboard", "export"]
type RecoveryAction = Literal[
    "correct_input", "reauthenticate", "reload", "retry", "contact_support", "none"
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
        json_schema_extra={"x-pillarmesh-console-envelope": True},
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
    source_ref: PublicId
    source_type: Literal["postgresql", "stripe"]
    display_name: NonEmptyText
    state: CapabilityState
    intended_checks: JsonTuple[NonEmptyText] = Field(default=())
    denied_checks: JsonTuple[NonEmptyText] = Field(default=())


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
    author_role: ActorRole | Literal["pillarmesh"]
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


class RequesterRequestView(StrictModel):
    request_id: PublicId
    kind: RequestKind
    state: RequestState
    title: NonEmptyText
    requested_outcome: NonEmptyText
    revision: int = Field(ge=1)
    updated_at: UtcDatetime
    own_decisions: JsonTuple[OwnDecisionView] = Field(default=())
    clarified_outcome: ClarifiedOutcomeView | None = None
    denial_explanation: NonEmptyText | None = None


class DatasetEvidenceView(StrictModel):
    dataset_ref: PublicId
    display_name: NonEmptyText


class AuthorityStatusView(StrictModel):
    role: ActorRole
    reason: NonEmptyText
    satisfied: bool


class StakeholderAnswerProposalView(StrictModel):
    kind: Literal["stakeholder_answer"]
    purpose: NonEmptyText
    candidate: NonEmptyText
    metric_version: NonEmptyText
    as_of: UtcDatetime
    freshness: FreshnessState
    quality_limitations: JsonTuple[NonEmptyText] = Field(default=())
    datasets: JsonTuple[DatasetEvidenceView] = Field(default=())
    lineage_summary: NonEmptyText
    authorization_summary: NonEmptyText
    required_authorities: JsonTuple[AuthorityStatusView] = Field(default=())


class AccessPreviewProposalView(StrictModel):
    kind: Literal["access_preview"]
    purpose: NonEmptyText
    data_product_ref: PublicId
    access_mode: AccessMode
    requested_fields: NonEmptyJsonTuple[NonEmptyText]
    effective_scope: JsonTuple[NonEmptyText] = Field(default=())
    exclusions: JsonTuple[NonEmptyText] = Field(default=())
    expires_at: UtcDatetime
    intended_checks: JsonTuple[NonEmptyText] = Field(default=())
    denied_checks: JsonTuple[NonEmptyText] = Field(default=())
    authority_summary: NonEmptyText
    required_authorities: JsonTuple[AuthorityStatusView] = Field(default=())


type RequestProposalView = Annotated[
    StakeholderAnswerProposalView | AccessPreviewProposalView, Field(discriminator="kind")
]


class LifecycleEventView(StrictModel):
    event_id: PublicId
    state: RequestState
    summary: NonEmptyText
    occurred_at: UtcDatetime


class EvidenceContextView(StrictModel):
    datasets: JsonTuple[DatasetEvidenceView] = Field(default=())
    metric_versions: JsonTuple[NonEmptyText] = Field(default=())
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
    data_product_id: PublicId
    display_name: NonEmptyText
    state: CapabilityState
    summary: NonEmptyText
    version: int = Field(ge=1)


class RunView(StrictModel):
    run_id: PublicId
    data_product_ref: PublicId
    state: OperationState
    summary: NonEmptyText
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None


class RunsView(StrictModel):
    runs: JsonTuple[RunView] = Field(default=())


class CatalogAssetView(StrictModel):
    asset_ref: PublicId
    display_name: NonEmptyText
    definition: NonEmptyText
    owner: NonEmptyText
    classifications: JsonTuple[NonEmptyText] = Field(default=())
    lineage_summary: NonEmptyText
    link_ref: PublicId | None = None


class DashboardView(StrictModel):
    dashboard_ref: PublicId
    display_name: NonEmptyText
    state: CapabilityState
    summary: NonEmptyText
    preview_ref: PublicId | None = None
    link_ref: PublicId | None = None


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


class ProcessPackageCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    package_digest: Digest
    active_role: ActorRole
    file_name: NonEmptyText
    media_type: Literal[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]


class DecisionCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_role: ActorRole
    decision: Literal["approve", "reject", "request_changes"]


class AdmissionCommand(StrictModel):
    """Admit the exact proposal the architect reviewed.

    `reviewed_digest` is the proposal digest the browser displayed, so an admission
    cannot be applied to a proposal that changed after it was read.
    """

    expected_revision: int = Field(ge=1)
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_role: ActorRole


class StakeholderQuestionInput(StrictModel):
    kind: Literal["stakeholder_question"]
    purpose: NonEmptyText
    question: NonEmptyText


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


class RetryOperationCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    operation_digest: Digest
    retry_token: OpaqueToken
    active_role: ActorRole


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
    requester_requests_response: ConsoleEnvelope[JsonTuple[RequesterRequestView]]
    requester_request_response: ConsoleEnvelope[RequesterRequestView]
    conversation_response: ConsoleEnvelope[ConversationView]
    clarified_outcome_response: ConsoleEnvelope[ClarifiedOutcomeView]
    data_product_response: ConsoleEnvelope[DataProductView]
    runs_response: ConsoleEnvelope[RunsView]
    catalog_asset_response: ConsoleEnvelope[CatalogAssetView]
    dashboard_response: ConsoleEnvelope[DashboardView]
    evidence_response: ConsoleEnvelope[EvidenceView]
    operation_response: ConsoleEnvelope[OperationView]
    error_response: ConsoleErrorEnvelope
    warehouse_binding_command: WarehouseBindingCommand
    process_package_command: ProcessPackageCommand
    decision_command: DecisionCommand
    admission_command: AdmissionCommand
    create_request_command: CreateRequestCommand
    conversation_message_command: ConversationMessageCommand
    clarified_outcome_acceptance_command: ClarifiedOutcomeAcceptanceCommand
    retry_operation_command: RetryOperationCommand
    reset_command: ResetCommand
