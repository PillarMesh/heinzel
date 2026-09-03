export type ActorRole =
  "requester" | "data_architect" | "data_owner" | "policy_approver" | "budget_approver"
export type ExpectedRevision = number
export type ReviewedDigest = string
export type ConsoleEnvelopeCatalogAssetView = ConsoleEnvelope_CatalogAssetView_
export type PublicId = string
export type NonEmptyText = string
export type JsonTuple_NonEmptyText_ = NonEmptyText[]
export type DataProvenance = "demo_fixture" | "governed_local"
export type ActiveRole = "requester"
export type Digest = string
export type Decision = "approve" | "request_changes"
export type ExpectedRevision1 = number
export type ConsoleEnvelopeClarifiedOutcomeView = ConsoleEnvelope_ClarifiedOutcomeView_
export type Accepted = boolean
export type Revision = number
export type ExpectedRevision2 = number
export type ConsoleEnvelopeConversationView = ConsoleEnvelope_ConversationView_
export type AuthorRole = ActorRole | "pillarmesh"
export type UtcDatetime = string
export type JsonTuple_ConversationMessageView_ = ConversationMessageView[]
export type Revision1 = number
export type ActiveRole1 = "requester"
export type ExpectedRevision3 = 1
export type RequestInput = StakeholderQuestionInput | DataAccessRequestInput
export type Kind = "stakeholder_question"
export type AccessMode = "query" | "dashboard" | "export"
export type Kind1 = "data_access"
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_NonEmptyText_ = [NonEmptyText, ...NonEmptyText[]]
export type ConsoleEnvelopeDashboardView = ConsoleEnvelope_DashboardView_
export type CapabilityState = "ready" | "blocked" | "degraded" | "not_delivered"
export type ConsoleEnvelopeDataProductView = ConsoleEnvelope_DataProductView_
export type Version = number
export type Decision1 = "approve" | "reject" | "request_changes"
export type ExpectedRevision4 = number
export type ReviewedDigest1 = string
export type RecoveryAction =
  "correct_input" | "reauthenticate" | "reload" | "retry" | "contact_support" | "none"
export type ConsoleEnvelopeEvidenceView = ConsoleEnvelope_EvidenceView_
export type ConsoleEnvelopeInboxView = ConsoleEnvelope_InboxView_
export type RequestKind = "stakeholder_question" | "data_access"
export type RiskLevel = "low" | "medium" | "high" | "critical"
export type RequestState =
  | "submitted"
  | "clarifying"
  | "investigating"
  | "proposed"
  | "awaiting_approval"
  | "execution_ready"
  | "denied"
  | "closed"
export type JsonTuple_InboxItemView_ = InboxItemView[]
export type ConsoleEnvelopeOperationView = ConsoleEnvelope_OperationView_
export type OperationView = OperationView1
export type Classification = "transient" | "permanent" | "unknown"
export type JsonTuple_RecoveryAction_ = RecoveryAction[]
export type OpaqueToken = string
export type Revision2 = number
export type OperationState = "accepted" | "running" | "succeeded" | "failed" | "outcome_unknown"
export type ExpectedRevision5 = number
export type MediaType =
  "application/pdf" | "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
export type ConsoleEnvelopeRequestDetailView = ConsoleEnvelope_RequestDetailView_
export type Available = boolean
export type Decision2 = "approve" | "reject" | "request_changes"
export type JsonTuple_Decision_ = Decision2[]
export type JsonTuple_DatasetEvidenceView_ = DatasetEvidenceView[]
export type JsonTuple_PublicId_ = PublicId[]
export type FreshnessState = "current" | "stale" | "unknown" | "not_applicable"
export type JsonTuple_NonEmptyText_1 = NonEmptyText[]
export type JsonTuple_LifecycleEventView_ = LifecycleEventView[]
export type RequestProposalView = StakeholderAnswerProposalView | AccessPreviewProposalView
export type JsonTuple_DatasetEvidenceView_1 = DatasetEvidenceView[]
export type Kind2 = "stakeholder_answer"
export type JsonTuple_NonEmptyText_2 = NonEmptyText[]
export type Satisfied = boolean
export type JsonTuple_AuthorityStatusView_ = AuthorityStatusView[]
export type JsonTuple_NonEmptyText_3 = NonEmptyText[]
export type JsonTuple_NonEmptyText_4 = NonEmptyText[]
export type JsonTuple_NonEmptyText_5 = NonEmptyText[]
export type JsonTuple_NonEmptyText_6 = NonEmptyText[]
export type Kind3 = "access_preview"
export type JsonTuple_AuthorityStatusView_1 = AuthorityStatusView[]
export type Revision3 = number
export type ConsoleEnvelopeRequesterRequestView = ConsoleEnvelope_RequesterRequestView_
export type JsonTuple_OwnDecisionView_ = OwnDecisionView[]
export type Revision4 = number
export type ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView =
  ConsoleEnvelope_JsonTuple_RequesterRequestView__
export type JsonTuple_RequesterRequestView_ = RequesterRequestView[]
export type ActiveRole2 = "data_architect"
export type ExpectedRevision6 = number
export type ExpectedRevision7 = number
export type ConsoleEnvelopeReviewView = ConsoleEnvelope_ReviewView_
export type CanDecide = boolean
export type JsonTuple_ConstraintView_ = ConstraintView[]
export type JsonTuple_RecordedDecisionView_ = RecordedDecisionView[]
export type JsonTuple_PublicId_1 = PublicId[]
export type ReviewKind = "meaning" | "data_product" | "activation"
export type Satisfied1 = boolean
export type JsonTuple_AuthorityRequirementView_ = AuthorityRequirementView[]
export type Revision5 = number
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ReviewSectionView_ = [ReviewSectionView, ...ReviewSectionView[]]
export type MaterialChange = boolean
export type JsonTuple_ReviewItemView_ = ReviewItemView[]
export type ConsoleEnvelopeRunsView = ConsoleEnvelope_RunsView_
export type JsonTuple_RunView_ = RunView[]
export type ConsoleEnvelopeSessionView = ConsoleEnvelope_SessionView_
export type SessionView = SessionView1
export type CsrfToken = string
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ActorRole_ = [ActorRole, ...ActorRole[]]
export type ConsoleEnvelopeSetupView = ConsoleEnvelope_SetupView_
export type SetupStage =
  | "foundation"
  | "managed_services"
  | "sources"
  | "business_process"
  | "meaning"
  | "data_product"
  | "activation"
export type Service = "warehouse" | "openmetadata" | "superset"
export type JsonTuple_ManagedServiceView_ = ManagedServiceView[]
export type JsonTuple_PublicId_2 = PublicId[]
export type Version1 = number
export type Revision6 = number
export type JsonTuple_NonEmptyText_7 = NonEmptyText[]
export type JsonTuple_NonEmptyText_8 = NonEmptyText[]
export type SourceType = "postgresql" | "stripe"
export type JsonTuple_SourceConnectionView_ = SourceConnectionView[]
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_SetupStageView_ = [SetupStageView, ...SetupStageView[]]
export type SetupStageState = "not_started" | "current" | "blocked" | "complete"
export type WarehouseEngine = "postgresql" | "clickhouse"
export type Immutable = true
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_WarehouseOptionView_ = [WarehouseOptionView, ...WarehouseOptionView[]]
export type ExpectedRevision8 = number
export type ConsoleEnvelopeWorkspaceView = ConsoleEnvelope_WorkspaceView_
export type JsonTuple_CapabilityView_ = CapabilityView[]
export type WorkspaceState = "setup" | "pending_activation" | "active" | "unavailable"

export interface ConsoleApiSchema {
  admission_command: AdmissionCommand
  catalog_asset_response: ConsoleEnvelopeCatalogAssetView
  clarified_outcome_acceptance_command: ClarifiedOutcomeAcceptanceCommand
  clarified_outcome_response: ConsoleEnvelopeClarifiedOutcomeView
  conversation_message_command: ConversationMessageCommand
  conversation_response: ConsoleEnvelopeConversationView
  create_request_command: CreateRequestCommand
  dashboard_response: ConsoleEnvelopeDashboardView
  data_product_response: ConsoleEnvelopeDataProductView
  decision_command: DecisionCommand
  error_response: ConsoleErrorEnvelope
  evidence_response: ConsoleEnvelopeEvidenceView
  inbox_response: ConsoleEnvelopeInboxView
  operation_response: ConsoleEnvelopeOperationView
  process_package_command: ProcessPackageCommand
  request_detail_response: ConsoleEnvelopeRequestDetailView
  requester_request_response: ConsoleEnvelopeRequesterRequestView
  requester_requests_response: ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView
  reset_command: ResetCommand
  retry_operation_command: RetryOperationCommand
  review_response: ConsoleEnvelopeReviewView
  runs_response: ConsoleEnvelopeRunsView
  session_response: ConsoleEnvelopeSessionView
  setup_response: ConsoleEnvelopeSetupView
  warehouse_binding_command: WarehouseBindingCommand
  workspace_response: ConsoleEnvelopeWorkspaceView
}
/**
 * Admit the exact proposal the architect reviewed.
 *
 * `reviewed_digest` is the proposal digest the browser displayed, so an admission
 * cannot be applied to a proposal that changed after it was read.
 */
export interface AdmissionCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision
  reviewed_digest: ReviewedDigest
}
export interface ConsoleEnvelope_CatalogAssetView_ {
  data: CatalogAssetView
  meta: ApiMeta
}
export interface CatalogAssetView {
  asset_ref: PublicId
  classifications?: JsonTuple_NonEmptyText_
  definition: NonEmptyText
  display_name: NonEmptyText
  lineage_summary: NonEmptyText
  link_ref?: PublicId | null
  owner: NonEmptyText
}
export interface ApiMeta {
  correlation_id: PublicId
  data_provenance: DataProvenance
}
export interface ClarifiedOutcomeAcceptanceCommand {
  active_role: ActiveRole
  clarified_outcome_digest: Digest
  decision: Decision
  expected_revision: ExpectedRevision1
}
export interface ConsoleEnvelope_ClarifiedOutcomeView_ {
  data: ClarifiedOutcomeView
  meta: ApiMeta
}
export interface ClarifiedOutcomeView {
  accepted: Accepted
  in_scope_summary: NonEmptyText
  out_of_scope_summary: NonEmptyText
  purpose: NonEmptyText
  request_id: PublicId
  restated_request: NonEmptyText
  revision: Revision
  statement_digest: Digest
}
export interface ConversationMessageCommand {
  active_role: ActorRole
  body: NonEmptyText
  conversation_digest: Digest
  expected_revision: ExpectedRevision2
}
export interface ConsoleEnvelope_ConversationView_ {
  data: ConversationView
  meta: ApiMeta
}
export interface ConversationView {
  awaiting_role?: ActorRole | null
  conversation_digest: Digest
  messages?: JsonTuple_ConversationMessageView_
  request_id: PublicId
  revision: Revision1
}
export interface ConversationMessageView {
  author_label: NonEmptyText
  author_role: AuthorRole
  body: NonEmptyText
  created_at: UtcDatetime
  message_id: PublicId
}
export interface CreateRequestCommand {
  active_role: ActiveRole1
  expected_revision: ExpectedRevision3
  request: RequestInput
  request_digest: Digest
  title: NonEmptyText
}
export interface StakeholderQuestionInput {
  kind: Kind
  purpose: NonEmptyText
  question: NonEmptyText
}
export interface DataAccessRequestInput {
  access_mode: AccessMode
  data_product_ref: PublicId
  expires_at: UtcDatetime
  kind: Kind1
  purpose: NonEmptyText
  requested_fields: NonEmptyJsonTuple_NonEmptyText_
}
export interface ConsoleEnvelope_DashboardView_ {
  data: DashboardView
  meta: ApiMeta
}
export interface DashboardView {
  dashboard_ref: PublicId
  display_name: NonEmptyText
  link_ref?: PublicId | null
  preview_ref?: PublicId | null
  state: CapabilityState
  summary: NonEmptyText
}
export interface ConsoleEnvelope_DataProductView_ {
  data: DataProductView
  meta: ApiMeta
}
export interface DataProductView {
  data_product_id: PublicId
  display_name: NonEmptyText
  state: CapabilityState
  summary: NonEmptyText
  version: Version
}
export interface DecisionCommand {
  active_role: ActorRole
  decision: Decision1
  expected_revision: ExpectedRevision4
  reviewed_digest: ReviewedDigest1
}
export interface ConsoleErrorEnvelope {
  error: ApiError
  meta: ApiMeta
}
export interface ApiError {
  code: PublicId
  field?: NonEmptyText | null
  recovery_action: RecoveryAction
  safe_message: NonEmptyText
}
export interface ConsoleEnvelope_EvidenceView_ {
  data: EvidenceView
  meta: ApiMeta
}
export interface EvidenceView {
  correlation_id: PublicId
  evidence_ref: PublicId
  occurred_at: UtcDatetime
  summary: NonEmptyText
}
export interface ConsoleEnvelope_InboxView_ {
  data: InboxView
  meta: ApiMeta
}
export interface InboxView {
  items?: JsonTuple_InboxItemView_
  selected_request_id?: PublicId | null
}
export interface InboxItemView {
  blocked_reason?: NonEmptyText | null
  deadline?: UtcDatetime | null
  kind: RequestKind
  purpose: NonEmptyText
  request_id: PublicId
  risk: RiskLevel
  state: RequestState
  title: NonEmptyText
}
export interface ConsoleEnvelope_OperationView_ {
  data: OperationView
  meta: ApiMeta
}
export interface OperationView1 {
  evidence_ref?: PublicId | null
  failure?: OperationFailureView | null
  operation_digest?: Digest | null
  operation_id: PublicId
  phase?: string
  recovery_actions?: JsonTuple_RecoveryAction_
  retry_token?: OpaqueToken | null
  revision: Revision2
  state: OperationState
  summary?: string
}
export interface OperationFailureView {
  classification: Classification
  code: PublicId
  safe_message: NonEmptyText
}
export interface ProcessPackageCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision5
  file_name: NonEmptyText
  media_type: MediaType
  package_digest: Digest
}
export interface ConsoleEnvelope_RequestDetailView_ {
  data: RequestDetailView
  meta: ApiMeta
}
export interface RequestDetailView {
  admission?: AdmissionView | null
  available_actions?: JsonTuple_Decision_
  conversation: ConversationView
  evidence: EvidenceContextView
  kind: RequestKind
  lifecycle?: JsonTuple_LifecycleEventView_
  proposal?: RequestProposalView | null
  proposal_digest?: Digest | null
  purpose: NonEmptyText
  request_id: PublicId
  revision: Revision3
  state: RequestState
  title: NonEmptyText
}
/**
 * Whether the approved proposal can be admitted to execution, and why not.
 *
 * Admission is a separate owning transaction from a decision: it requires every
 * required approval to be recorded against the exact proposal, and it is what moves
 * a request past `awaiting_approval`. The console projects availability and the
 * reason rather than a bare flag, because a disabled action with no stated cause is
 * the thing this console exists not to do.
 */
export interface AdmissionView {
  available: Available
  blocking_reason?: NonEmptyText | null
}
export interface EvidenceContextView {
  as_of?: UtcDatetime | null
  authorization_summary: NonEmptyText
  datasets?: JsonTuple_DatasetEvidenceView_
  evidence_refs?: JsonTuple_PublicId_
  freshness: FreshnessState
  lineage_summary: NonEmptyText
  metric_versions?: JsonTuple_NonEmptyText_1
  quality_summary: NonEmptyText
}
export interface DatasetEvidenceView {
  dataset_ref: PublicId
  display_name: NonEmptyText
}
export interface LifecycleEventView {
  event_id: PublicId
  occurred_at: UtcDatetime
  state: RequestState
  summary: NonEmptyText
}
export interface StakeholderAnswerProposalView {
  as_of: UtcDatetime
  authorization_summary: NonEmptyText
  candidate: NonEmptyText
  datasets?: JsonTuple_DatasetEvidenceView_1
  freshness: FreshnessState
  kind: Kind2
  lineage_summary: NonEmptyText
  metric_version: NonEmptyText
  purpose: NonEmptyText
  quality_limitations?: JsonTuple_NonEmptyText_2
  required_authorities?: JsonTuple_AuthorityStatusView_
}
export interface AuthorityStatusView {
  reason: NonEmptyText
  role: ActorRole
  satisfied: Satisfied
}
export interface AccessPreviewProposalView {
  access_mode: AccessMode
  authority_summary: NonEmptyText
  data_product_ref: PublicId
  denied_checks?: JsonTuple_NonEmptyText_3
  effective_scope?: JsonTuple_NonEmptyText_4
  exclusions?: JsonTuple_NonEmptyText_5
  expires_at: UtcDatetime
  intended_checks?: JsonTuple_NonEmptyText_6
  kind: Kind3
  purpose: NonEmptyText
  requested_fields: NonEmptyJsonTuple_NonEmptyText_
  required_authorities?: JsonTuple_AuthorityStatusView_1
}
export interface ConsoleEnvelope_RequesterRequestView_ {
  data: RequesterRequestView
  meta: ApiMeta
}
export interface RequesterRequestView {
  clarified_outcome?: ClarifiedOutcomeView | null
  denial_explanation?: NonEmptyText | null
  kind: RequestKind
  own_decisions?: JsonTuple_OwnDecisionView_
  request_id: PublicId
  requested_outcome: NonEmptyText
  revision: Revision4
  state: RequestState
  title: NonEmptyText
  updated_at: UtcDatetime
}
export interface OwnDecisionView {
  created_at: UtcDatetime
  decision: Decision2
  subject_label: NonEmptyText
}
export interface ConsoleEnvelope_JsonTuple_RequesterRequestView__ {
  data: JsonTuple_RequesterRequestView_
  meta: ApiMeta
}
export interface ResetCommand {
  active_role: ActiveRole2
  expected_revision: ExpectedRevision6
  reset_token: OpaqueToken
  setup_digest: Digest
}
export interface RetryOperationCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision7
  operation_digest: Digest
  retry_token: OpaqueToken
}
export interface ConsoleEnvelope_ReviewView_ {
  data: ReviewView
  meta: ApiMeta
}
export interface ReviewView {
  can_decide: CanDecide
  constraints?: JsonTuple_ConstraintView_
  decisions?: JsonTuple_RecordedDecisionView_
  evidence_refs?: JsonTuple_PublicId_1
  kind: ReviewKind
  required_authorities?: JsonTuple_AuthorityRequirementView_
  review_id: PublicId
  reviewed_digest: Digest
  revision: Revision5
  sections: NonEmptyJsonTuple_ReviewSectionView_
  summary: NonEmptyText
  title: NonEmptyText
}
export interface ConstraintView {
  code: PublicId
  permitted_next_action: NonEmptyText
  responsible_role: ActorRole
  summary: NonEmptyText
}
export interface RecordedDecisionView {
  decided_at: UtcDatetime
  decision: Decision2
  role: ActorRole
}
export interface AuthorityRequirementView {
  reason: NonEmptyText
  role: ActorRole
  satisfied: Satisfied1
  subject_digest: Digest
}
export interface ReviewSectionView {
  items?: JsonTuple_ReviewItemView_
  section_id: PublicId
  summary?: NonEmptyText | null
  title: NonEmptyText
}
export interface ReviewItemView {
  label: NonEmptyText
  material_change?: MaterialChange
  value: NonEmptyText
}
export interface ConsoleEnvelope_RunsView_ {
  data: RunsView
  meta: ApiMeta
}
export interface RunsView {
  runs?: JsonTuple_RunView_
}
export interface RunView {
  completed_at?: UtcDatetime | null
  data_product_ref: PublicId
  run_id: PublicId
  started_at?: UtcDatetime | null
  state: OperationState
  summary: NonEmptyText
}
export interface ConsoleEnvelope_SessionView_ {
  data: SessionView
  meta: ApiMeta
}
export interface SessionView1 {
  active_role: ActorRole
  actor: ActorDisplayView
  csrf_token: CsrfToken
  roles: NonEmptyJsonTuple_ActorRole_
  tenant: DisplayReferenceView
  workspace: DisplayReferenceView
}
export interface ActorDisplayView {
  display_name: NonEmptyText
}
export interface DisplayReferenceView {
  display_name: NonEmptyText
  ref: PublicId
}
export interface ConsoleEnvelope_SetupView_ {
  data: SetupView
  meta: ApiMeta
}
export interface SetupView {
  active_stage: SetupStage
  managed_services?: JsonTuple_ManagedServiceView_
  pending_review_refs?: JsonTuple_PublicId_2
  process_package?: ProcessPackageView | null
  reset_token: OpaqueToken
  revision: Revision6
  setup_digest: Digest
  sources?: JsonTuple_SourceConnectionView_
  stages: NonEmptyJsonTuple_SetupStageView_
  warehouse_binding?: WarehouseBindingView | null
  warehouse_options: NonEmptyJsonTuple_WarehouseOptionView_
  workspace_ref: PublicId
}
export interface ManagedServiceView {
  detail: NonEmptyText
  label: NonEmptyText
  service: Service
  state: CapabilityState
  validation_summary?: NonEmptyText | null
}
export interface ProcessPackageView {
  candidate_summary: NonEmptyText
  content_digest: Digest
  package_ref: PublicId
  state: CapabilityState
  version: Version1
}
export interface SourceConnectionView {
  denied_checks?: JsonTuple_NonEmptyText_7
  display_name: NonEmptyText
  intended_checks?: JsonTuple_NonEmptyText_8
  source_ref: PublicId
  source_type: SourceType
  state: CapabilityState
}
export interface SetupStageView {
  detail?: NonEmptyText | null
  label: NonEmptyText
  stage: SetupStage
  state: SetupStageState
}
export interface WarehouseBindingView {
  binding_ref: PublicId
  capacity: NonEmptyText
  engine: WarehouseEngine
  immutable?: Immutable
  region: NonEmptyText
  state: CapabilityState
}
export interface WarehouseOptionView {
  engine: WarehouseEngine
  fixed_capacity: NonEmptyText
  label: NonEmptyText
  supported_region: NonEmptyText
}
export interface WarehouseBindingCommand {
  active_role: ActorRole
  capacity: NonEmptyText
  engine: WarehouseEngine
  expected_revision: ExpectedRevision8
  region: NonEmptyText
  reviewed_digest: Digest
}
export interface ConsoleEnvelope_WorkspaceView_ {
  data: WorkspaceView
  meta: ApiMeta
}
export interface WorkspaceView {
  capabilities?: JsonTuple_CapabilityView_
  recovery_message?: NonEmptyText | null
  state: WorkspaceState
  workspace: DisplayReferenceView
}
export interface CapabilityView {
  capability_id: PublicId
  dependency?: NonEmptyText | null
  detail: NonEmptyText
  label: NonEmptyText
  state: CapabilityState
}
