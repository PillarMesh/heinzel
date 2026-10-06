export type ConsoleEnvelopeAccessLifecycleView = ConsoleEnvelope_AccessLifecycleView_
export type CanRevoke = boolean
export type UtcDatetime = string
export type Revision = number
export type State = "pending" | "active" | "expired" | "revocation_pending" | "revoked" | "failed"
export type NonEmptyText = string
export type PublicId = string
export type DataProvenance = "demo_fixture" | "governed_local"
export type ActiveRole = "requester" | "data_architect"
export type ExpectedRevision = number
export type Reason = string
export type ConsoleEnvelopeAcquisitionReceiptView = ConsoleEnvelope_AcquisitionReceiptView_
export type AcquisitionModeView = "snapshot" | "incremental" | "reconciliation"
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_NonEmptyText_ = [NonEmptyText, ...NonEmptyText[]]
export type AcquisitionOutcomeView =
  "prepared" | "acknowledged" | "no_valid_plan" | "resynchronization_required" | "failed"
export type AcquisitionReasonCodeView =
  | "acquisition_mode_not_admitted"
  | "authorization_denied"
  | "contract_invalid"
  | "contract_not_activated"
  | "encoded_byte_ceiling_exceeded"
  | "encoded_byte_ceiling_not_admitted"
  | "integrity_failure"
  | "logical_object_not_admitted"
  | "physical_delete_capture_unsupported"
  | "provider_unavailable"
  | "rate_limited"
  | "record_ceiling_exceeded"
  | "record_ceiling_not_admitted"
  | "source_binding_authority_stale"
  | "source_binding_not_admitted"
  | "source_drift"
  | "source_observation_not_admitted"
  | "stale_checkpoint"
  | "stripe_event_cursor_expired"
  | "stripe_event_overlap_gap"
export type JsonTuple_AcquisitionReasonCodeView_ = AcquisitionReasonCodeView[]
export type ConsoleEnvelopeAcquisitionReceiptsView = ConsoleEnvelope_AcquisitionReceiptsView_
export type JsonTuple_AcquisitionReceiptView_ = AcquisitionReceiptView[]
export type ActiveRole1 = "data_architect" | "data_owner"
export type ActorRole =
  "requester" | "data_architect" | "data_owner" | "policy_approver" | "budget_approver"
export type ExpectedRevision1 = number
export type ReviewedDigest = string
export type ConsoleEnvelopeAnswerResultPageView = ConsoleEnvelope_AnswerResultPageView_
export type JsonTuple_Literal_Sort___Filter___ = ("sort" | "filter")[]
export type AnswerResultValueType = "boolean" | "decimal" | "integer" | "string" | "timestamp"
export type JsonTuple_AnswerResultColumnView_ = AnswerResultColumnView[]
export type FreshnessState = "current" | "stale" | "unknown" | "not_applicable"
export type OpaqueToken = string
export type RowCount = number
export type AnswerResultCell = string | number | boolean | UtcDatetime | null
export type JsonTuple_AnswerResultCell_ = AnswerResultCell[]
export type JsonTuple_JsonTuple_AnswerResultCell__ = JsonTuple_AnswerResultCell_[]
export type AnswerResultStatus = "available" | "expired" | "failed"
export type Digest = string
export type ConsoleEnvelopeCatalogAssetView = ConsoleEnvelope_CatalogAssetView_
export type JsonTuple_NonEmptyText_ = NonEmptyText[]
export type ConsoleEnvelopeCatalogAssetsView = ConsoleEnvelope_CatalogAssetsView_
export type JsonTuple_CatalogAssetView_ = CatalogAssetView[]
export type ActiveRole2 = "requester"
export type Decision = "approve" | "request_changes"
export type ExpectedRevision2 = number
export type ConsoleEnvelopeClarifiedOutcomeView = ConsoleEnvelope_ClarifiedOutcomeView_
export type Accepted = boolean
export type Revision1 = number
export type ExpectedRevision3 = number
export type ConsoleEnvelopeConversationView = ConsoleEnvelope_ConversationView_
export type AuthorRole = ActorRole | "heinzel" | null
export type JsonTuple_ConversationMessageView_ = ConversationMessageView[]
export type Revision2 = number
export type ActiveRole3 = "requester"
export type ExpectedRevision4 = 1
export type RequestInput = StakeholderQuestionInput | DataAccessRequestInput
export type Kind = "stakeholder_question"
export type AccessMode = "query" | "dashboard" | "export"
export type Kind1 = "data_access"
export type ConsoleEnvelopeDashboardView = ConsoleEnvelope_DashboardView_
export type DashboardAccessViewState = "active" | "workspace_role"
export type LifecycleState = "active" | "archived"
export type CapabilityState = "ready" | "blocked" | "degraded" | "not_delivered"
export type Version = number
export type ConsoleEnvelopeDashboardsView = ConsoleEnvelope_DashboardsView_
export type JsonTuple_DashboardView_ = DashboardView[]
export type ConsoleEnvelopeDataProductView = ConsoleEnvelope_DataProductView_
export type CatalogRevision = number | null
export type ColumnCount = number | null
export type Generation = number | null
export type ProductRevision = number | null
export type PublicationStatus = "published" | "pending"
export type SourceCount = number | null
export type Version1 = number
export type ConsoleEnvelopeDataProductsView = ConsoleEnvelope_DataProductsView_
export type JsonTuple_DataProductView_ = DataProductView[]
export type Decision1 = "approve" | "reject" | "request_changes"
export type ExpectedRevision5 = number
export type ReviewedDigest1 = string
export type RecoveryAction =
  "correct_input" | "reauthenticate" | "reload" | "retry" | "contact_support" | "none"
export type ConsoleEnvelopeEvidenceView = ConsoleEnvelope_EvidenceView_
export type ConsoleEnvelopeImpactView = ConsoleEnvelope_ImpactView_
export type JsonTuple_ImpactApproverView_ = ImpactApproverView[]
export type JsonTuple_NonEmptyText_1 = NonEmptyText[]
export type ImpactChangeType =
  | "source_drift"
  | "metric_version_change"
  | "contract_supersession"
  | "generation_failure"
  | "policy_change"
  | "grant_change"
  | "retirement"
export type JsonTuple_ImpactItemView_ = ImpactItemView[]
export type JsonTuple_ImpactItemView_1 = ImpactItemView[]
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
  | "delivered"
  | "no_valid_plan"
  | "cancelled"
  | "failed"
  | "closed"
export type JsonTuple_InboxItemView_ = InboxItemView[]
export type OperationalRecoveryAction =
  "retry_transient_attempt" | "cancel_unstarted_work" | "reconcile_external_effect"
export type ActiveRole4 = "data_architect" | "data_owner"
export type ExpectedRevision6 = number
export type ConsoleEnvelopeIncidentView = ConsoleEnvelope_IncidentView_
export type JsonTuple_OperationalRecoveryAction_ = OperationalRecoveryAction[]
export type IncidentFailureClassification =
  | "transient"
  | "permanent"
  | "ambiguous_outcome"
  | "authorization_denied"
  | "integrity_failure"
  | "conflict"
  | "no_valid_plan"
export type IncidentStage =
  | "request_intake"
  | "contract_activation"
  | "extract"
  | "land"
  | "transform"
  | "catalog_publication"
  | "governed_query"
  | "dashboard_publication"
  | "access_revocation"
export type IncidentKind =
  | "stuck_lease"
  | "source_unavailable"
  | "checkpoint_conflict"
  | "schema_drift"
  | "no_valid_plan"
  | "transform_rejection"
  | "catalog_pending"
  | "query_failure"
  | "dashboard_drift"
  | "revocation_pending"
export type IncidentAutomaticAction = "retry_transient_attempt" | "reconcile_external_effect"
export type RecoveryRecorded = boolean
export type Revision3 = number
export type ConsoleEnvelopeIncidentsView = ConsoleEnvelope_IncidentsView_
export type JsonTuple_IncidentView_ = IncidentView[]
export type ConsoleEnvelopeOperationView = ConsoleEnvelope_OperationView_
export type OperationView = OperationView1
export type Classification = "transient" | "permanent" | "unknown"
export type JsonTuple_RecoveryAction_ = RecoveryAction[]
export type Revision4 = number
export type OperationState = "accepted" | "running" | "succeeded" | "failed" | "outcome_unknown"
export type ActiveRole5 = "data_architect"
export type ExpectedRevision7 = number
export type FileName = string
export type JsonTupleStr_ = string[]
export type Owner = string
export type ProcessName = string
export type SchemaVersion = "1"
export type MediaType = "text/markdown; charset=utf-8"
export type NarrativeMarkdown = string
export type ActiveRole6 = "data_architect"
export type ExpectedRevision8 = number
export type ConsoleEnvelopeProductIntentApprovalView = ConsoleEnvelope_ProductIntentApprovalView_
export type ArtifactId = string
export type Version2 = number
export type IntentRevision = number
export type ExpectedRevision9 = number
export type ExpectedRevision10 = number
export type InScopeSummary = string
export type OutOfScopeSummary = string
export type RestatedRequest = string
export type ConsoleEnvelopeRequestDetailView = ConsoleEnvelope_RequestDetailView_
export type Available = boolean
export type PendingDelivery = boolean
export type Decision2 = "approve" | "reject" | "request_changes"
export type JsonTuple_Decision_ = Decision2[]
export type JsonTuple_DatasetEvidenceView_ = DatasetEvidenceView[]
export type JsonTuple_PublicId_ = PublicId[]
export type JsonTuple_ArtifactReferenceView_ = ArtifactReferenceView[]
export type JsonTuple_NonEmptyText_2 = NonEmptyText[]
export type JsonTuple_LifecycleEventView_ = LifecycleEventView[]
export type PreparationAction = "clarify" | "prepare_access" | "prepare_answer" | "submit_proposal"
export type JsonTuple_PreparationAction_ = PreparationAction[]
export type JsonTuple_NonEmptyText_3 = NonEmptyText[]
export type Approved = boolean
export type ApprovedIntentRevision = number | null
export type JsonTuple_NonEmptyText_4 = NonEmptyText[]
export type ProductFilterOperator = "equals" | "not_equals" | "in" | "greater_than" | "less_than"
export type JsonTuple_ProductIntentFilterView_ = ProductIntentFilterView[]
export type FreshnessSeconds = number
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ProductIntentMeasureView_ = [
  ProductIntentMeasureView,
  ...ProductIntentMeasureView[],
]
export type ProductAggregation = "sum" | "count" | "minimum" | "maximum" | "average"
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ProductDeliveryOutput_ = [
  ProductDeliveryOutput,
  ...ProductDeliveryOutput[],
]
export type ProductDeliveryOutput = "dataset" | "table" | "dashboard"
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ProductIntentSourceCoverageView_ = [
  ProductIntentSourceCoverageView,
  ...ProductIntentSourceCoverageView[],
]
export type Authorized = boolean
export type JsonTuple_NonEmptyText_5 = NonEmptyText[]
export type JsonTuple_NonEmptyText_6 = NonEmptyText[]
export type RequestProposalView =
  StakeholderAnswerProposalView | AccessPreviewProposalView | DisclosureDenialProposalView
export type JsonTuple_DatasetEvidenceView_1 = DatasetEvidenceView[]
export type Kind2 = "stakeholder_answer"
export type JsonTuple_ArtifactReferenceView_1 = ArtifactReferenceView[]
export type JsonTuple_ArtifactReferenceView_2 = ArtifactReferenceView[]
export type JsonTuple_NonEmptyText_7 = NonEmptyText[]
export type JsonTuple_ArtifactReferenceView_3 = ArtifactReferenceView[]
export type Satisfied = boolean
export type JsonTuple_ProposalApprovalView_ = ProposalApprovalView[]
export type Satisfied1 = boolean
export type JsonTuple_AuthorityStatusView_ = AuthorityStatusView[]
export type JsonTuple_NonEmptyText_8 = NonEmptyText[]
export type JsonTuple_ArtifactReferenceView_4 = ArtifactReferenceView[]
export type JsonTuple_NonEmptyText_9 = NonEmptyText[]
export type JsonTuple_NonEmptyText_10 = NonEmptyText[]
export type JsonTuple_NonEmptyText_11 = NonEmptyText[]
export type Kind3 = "access_preview"
export type JsonTuple_ProposalApprovalView_1 = ProposalApprovalView[]
export type JsonTuple_AuthorityStatusView_1 = AuthorityStatusView[]
export type Kind4 = "disclosure_denial"
export type JsonTuple_ProposalApprovalView_2 = ProposalApprovalView[]
export type JsonTuple_AuthorityStatusView_2 = AuthorityStatusView[]
export type Revision5 = number
export type ActiveRole7 = "requester"
export type ExpectedRevision11 = number
export type ConsoleEnvelopeRequesterRequestView = ConsoleEnvelope_RequesterRequestView_
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_Literal_Dashboard___Download___Query___View___ = [
  "dashboard" | "download" | "query" | "view",
  ...("dashboard" | "download" | "query" | "view")[],
]
export type JsonTuple_ArtifactReferenceView_5 = ArtifactReferenceView[]
export type JsonTuple_ArtifactReferenceView_6 = ArtifactReferenceView[]
export type JsonTuple_ArtifactReferenceView_7 = ArtifactReferenceView[]
export type JsonTuple_ArtifactReferenceView_8 = ArtifactReferenceView[]
export type JsonTuple_OwnDecisionView_ = OwnDecisionView[]
export type ResultPageAvailable = boolean
export type Revision6 = number
export type ConsoleEnvelopeJsonTupleHeinzelConsoleContractsRequesterRequestView =
  ConsoleEnvelope_JsonTuple_RequesterRequestView__
export type JsonTuple_RequesterRequestView_ = RequesterRequestView[]
export type ActiveRole8 = "data_architect"
export type ExpectedRevision12 = number
export type ExpectedRevision13 = number
export type ConsoleEnvelopeReviewView = ConsoleEnvelope_ReviewView_
export type CanDecide = boolean
export type JsonTuple_ConstraintView_ = ConstraintView[]
export type JsonTuple_RecordedDecisionView_ = RecordedDecisionView[]
export type JsonTuple_PublicId_1 = PublicId[]
export type ReviewKind = "meaning" | "data_product" | "activation"
export type Satisfied2 = boolean
export type JsonTuple_AuthorityRequirementView_ = AuthorityRequirementView[]
export type Revision7 = number
/**
 * @minItems 1
 */
export type NonEmptyJsonTuple_ReviewSectionView_ = [ReviewSectionView, ...ReviewSectionView[]]
export type MaterialChange = boolean
export type JsonTuple_ReviewItemView_ = ReviewItemView[]
export type ConsoleEnvelopeRunsView = ConsoleEnvelope_RunsView_
export type AttemptNumber = number
export type Epoch = number
export type FailureClassification = ("transient" | "permanent") | null
export type LeaseExtensions = number
export type Outcome = ("succeeded" | "failed") | null
export type JsonTuple_RunAttemptView_ = RunAttemptView[]
export type ContractRevision = number
export type LeasedRunStatusView =
  "pending" | "leased" | "lease_expired" | "retryable" | "succeeded" | "failed" | "cancelled"
export type TriggerReason = "scheduled" | "run_now" | "backfill" | "retry"
export type JsonTuple_LeasedRunView_ = LeasedRunView[]
export type LeasedRunsAvailable = boolean
export type RunLifecycleState = "created" | "running" | "succeeded" | "failed" | "non_conforming"
export type JsonTuple_RunView_ = RunView[]
export type ConsoleEnvelopeSelectableAnswerTermsView = ConsoleEnvelope_SelectableAnswerTermsView_
export type AnswerTermKind = "dimension" | "metric"
export type JsonTuple_SelectableAnswerTermView_ = SelectableAnswerTermView[]
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
export type SourceAccountModeView = "not_applicable" | "test" | "live"
export type SourceProviderKind = "postgresql" | "stripe"
export type JsonTuple_EnrollableSourceHandleView_ = EnrollableSourceHandleView[]
export type Service = "warehouse" | "openmetadata" | "superset"
export type JsonTuple_ManagedServiceView_ = ManagedServiceView[]
export type JsonTuple_PublicId_2 = PublicId[]
export type Version3 = number
export type Revision8 = number
export type JsonTuple_NonEmptyText_12 = NonEmptyText[]
export type JsonTuple_NonEmptyText_13 = NonEmptyText[]
export type JsonTuple_NonEmptyText_14 = NonEmptyText[]
export type SourceBindingStateView =
  "draft" | "validating" | "ready" | "suspended" | "failed" | "retired"
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
export type ActiveRole9 = "data_architect"
export type ExpectedRevision14 = number
export type ExpectedRevision15 = number
export type ConsoleEnvelopeWorkspaceView = ConsoleEnvelope_WorkspaceView_
export type JsonTuple_CapabilityView_ = CapabilityView[]
export type WorkspaceState = "setup" | "pending_activation" | "active" | "unavailable"

export interface ConsoleApiSchema {
  access_lifecycle_response: ConsoleEnvelopeAccessLifecycleView
  access_revocation_command: AccessRevocationCommand
  acquisition_receipt_response: ConsoleEnvelopeAcquisitionReceiptView
  acquisition_receipts_response: ConsoleEnvelopeAcquisitionReceiptsView
  acquisition_run_now_command: AcquisitionRunNowCommand
  admission_command: AdmissionCommand
  answer_result_response: ConsoleEnvelopeAnswerResultPageView
  catalog_asset_response: ConsoleEnvelopeCatalogAssetView
  catalog_assets_response: ConsoleEnvelopeCatalogAssetsView
  clarified_outcome_acceptance_command: ClarifiedOutcomeAcceptanceCommand
  clarified_outcome_response: ConsoleEnvelopeClarifiedOutcomeView
  conversation_message_command: ConversationMessageCommand
  conversation_response: ConsoleEnvelopeConversationView
  create_request_command: CreateRequestCommand
  dashboard_response: ConsoleEnvelopeDashboardView
  dashboards_response: ConsoleEnvelopeDashboardsView
  data_product_response: ConsoleEnvelopeDataProductView
  data_products_response: ConsoleEnvelopeDataProductsView
  decision_command: DecisionCommand
  error_response: ConsoleErrorEnvelope
  evidence_response: ConsoleEnvelopeEvidenceView
  impact_response: ConsoleEnvelopeImpactView
  inbox_response: ConsoleEnvelopeInboxView
  incident_recovery_command: IncidentRecoveryCommand
  incident_response: ConsoleEnvelopeIncidentView
  incidents_response: ConsoleEnvelopeIncidentsView
  operation_response: ConsoleEnvelopeOperationView
  process_package_command: ProcessPackageCommand
  product_intent_approval_command: ProductIntentApprovalCommand
  product_intent_approval_response: ConsoleEnvelopeProductIntentApprovalView
  proposal_preparation_command: ProposalPreparationCommand
  request_clarification_command: RequestClarificationCommand
  request_detail_response: ConsoleEnvelopeRequestDetailView
  request_withdrawal_command: RequestWithdrawalCommand
  requester_request_response: ConsoleEnvelopeRequesterRequestView
  requester_requests_response: ConsoleEnvelopeJsonTupleHeinzelConsoleContractsRequesterRequestView
  reset_command: ResetCommand
  retry_operation_command: RetryOperationCommand
  review_response: ConsoleEnvelopeReviewView
  runs_response: ConsoleEnvelopeRunsView
  selectable_answer_terms_response: ConsoleEnvelopeSelectableAnswerTermsView
  session_response: ConsoleEnvelopeSessionView
  setup_response: ConsoleEnvelopeSetupView
  source_registration_command: SourceRegistrationCommand
  warehouse_binding_command: WarehouseBindingCommand
  workspace_response: ConsoleEnvelopeWorkspaceView
}
export interface ConsoleEnvelope_AccessLifecycleView_ {
  data: AccessLifecycleView
  meta: ApiMeta
}
export interface AccessLifecycleView {
  can_revoke: CanRevoke
  effective_at: UtcDatetime
  expires_at: UtcDatetime
  revision: Revision
  state: State
  summary: NonEmptyText
  title: NonEmptyText
}
export interface ApiMeta {
  correlation_id: PublicId
  data_provenance: DataProvenance
}
export interface AccessRevocationCommand {
  active_role: ActiveRole
  expected_revision: ExpectedRevision
  reason: Reason
}
export interface ConsoleEnvelope_AcquisitionReceiptView_ {
  data: AcquisitionReceiptView
  meta: ApiMeta
}
/**
 * An acquisition receipt as the runtime recorded it.
 *
 * Failed and governed-refusal receipts are projected beside successful ones. The
 * receipt model carries `outcome` and `reason_codes` precisely so a refusal is
 * publishable, and a refusal an operator cannot see is one they cannot act on.
 *
 * Every identifier here is a reference an owning service allocated, and the
 * console shows the reference rather than inventing a display name for it -- the
 * rule `DataProductView` already follows. They are typed as text rather than as
 * `PublicId` because that vocabulary admits neither the separators these
 * references use nor anything the owning model does not itself constrain:
 * narrowing further would reject a receipt the owner considers valid, and the
 * console would report a capability it cannot serve.
 *
 * The recovery state -- both receipt references and both checkpoint revisions --
 * is deliberately absent. It says where the runtime is in its own protocol, which
 * is not something an operator reads.
 */
export interface AcquisitionReceiptView {
  acquisition_mode: AcquisitionModeView
  contract_ref: NonEmptyText
  created_at: UtcDatetime
  evidence_id: NonEmptyText
  logical_object_refs: NonEmptyJsonTuple_NonEmptyText_
  outcome: AcquisitionOutcomeView
  reason_codes?: JsonTuple_AcquisitionReasonCodeView_
  source_binding_ref: NonEmptyText
}
export interface ConsoleEnvelope_AcquisitionReceiptsView_ {
  data: AcquisitionReceiptsView
  meta: ApiMeta
}
export interface AcquisitionReceiptsView {
  receipts?: JsonTuple_AcquisitionReceiptView_
}
/**
 * Ask the acquisition owner to prepare one run of an activated contract.
 *
 * The command carries no contract revision, binding, schema, or checkpoint. The
 * acquisition application resolves those current authorities from `contract_ref`.
 */
export interface AcquisitionRunNowCommand {
  acquisition_mode: AcquisitionModeView
  active_role: ActiveRole1
  contract_ref: NonEmptyText
  trigger_window: NonEmptyText
}
/**
 * Admit the exact proposal the architect reviewed.
 *
 * `reviewed_digest` is the proposal digest the browser displayed, so an admission
 * cannot be applied to a proposal that changed after it was read.
 */
export interface AdmissionCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision1
  reviewed_digest: ReviewedDigest
}
export interface ConsoleEnvelope_AnswerResultPageView_ {
  data: AnswerResultPageView
  meta: ApiMeta
}
export interface AnswerResultPageView {
  answer_text: NonEmptyText | null
  as_of: UtcDatetime | null
  columns?: JsonTuple_AnswerResultColumnView_
  freshness: FreshnessState | null
  next_cursor?: OpaqueToken | null
  request_id: PublicId
  row_count: RowCount
  rows?: JsonTuple_JsonTuple_AnswerResultCell__
  status: AnswerResultStatus
  technical_details?: AnswerResultTechnicalDetailsView | null
  title: NonEmptyText
}
export interface AnswerResultColumnView {
  allowed_operations?: JsonTuple_Literal_Sort___Filter___
  label: NonEmptyText
  name: NonEmptyText
  value_type: AnswerResultValueType
}
export interface AnswerResultTechnicalDetailsView {
  execution_receipt_id: NonEmptyText
  plan_digest: Digest
  result_digest: Digest
  result_schema_digest: Digest
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
  owner?: NonEmptyText | null
}
export interface ConsoleEnvelope_CatalogAssetsView_ {
  data: CatalogAssetsView
  meta: ApiMeta
}
export interface CatalogAssetsView {
  assets?: JsonTuple_CatalogAssetView_
}
export interface ClarifiedOutcomeAcceptanceCommand {
  active_role: ActiveRole2
  clarified_outcome_digest: Digest
  decision: Decision
  expected_revision: ExpectedRevision2
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
  revision: Revision1
  statement_digest: Digest
}
export interface ConversationMessageCommand {
  active_role: ActorRole
  body: NonEmptyText
  conversation_digest: Digest
  expected_revision: ExpectedRevision3
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
  revision: Revision2
}
export interface ConversationMessageView {
  author_label: NonEmptyText
  author_role: AuthorRole
  body: NonEmptyText
  created_at: UtcDatetime
  message_id: PublicId
}
export interface CreateRequestCommand {
  active_role: ActiveRole3
  expected_revision: ExpectedRevision4
  request: RequestInput
  request_digest: Digest
  title: NonEmptyText
}
export interface StakeholderQuestionInput {
  kind: Kind
  purpose: NonEmptyText
  question: NonEmptyText
  selection?: QuestionTermSelectionInput | null
}
/**
 * The governed terms a requester composed their question from.
 *
 * Mirrors request-management's `QuestionTermSelection`, minus its `schema_version`: the console
 * does not let a browser choose which version of that artifact it is writing. The model the
 * payload is built with supplies it, and `request_intake_content` rebuilds the payload through
 * that model, so a selection that artifact would reject never reaches the request store.
 */
export interface QuestionTermSelectionInput {
  dimension_refs: NonEmptyJsonTuple_NonEmptyText_
  metric_ref: NonEmptyText
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
  access_state: DashboardAccessViewState
  as_of: UtcDatetime
  dashboard_ref: PublicId
  display_name: NonEmptyText
  freshness: FreshnessState
  lifecycle_state: LifecycleState
  link_ref?: PublicId | null
  preview_ref?: PublicId | null
  published_at: UtcDatetime
  state: CapabilityState
  summary: NonEmptyText
  version: Version
}
export interface ConsoleEnvelope_DashboardsView_ {
  data: DashboardsView
  meta: ApiMeta
}
export interface DashboardsView {
  dashboards?: JsonTuple_DashboardView_
}
export interface ConsoleEnvelope_DataProductView_ {
  data: DataProductView
  meta: ApiMeta
}
/**
 * A policy-permitted product enriched only by its owning publication definition.
 */
export interface DataProductView {
  catalog_revision?: CatalogRevision
  column_count?: ColumnCount
  data_product_id: PublicId
  description?: NonEmptyText | null
  freshness_observed_at?: UtcDatetime | null
  generation?: Generation
  name?: NonEmptyText | null
  namespace?: NonEmptyText | null
  product_revision?: ProductRevision
  publication_status: PublicationStatus
  relation_name?: NonEmptyText | null
  source_count?: SourceCount
  version: Version1
}
export interface ConsoleEnvelope_DataProductsView_ {
  data: DataProductsView
  meta: ApiMeta
}
export interface DataProductsView {
  products?: JsonTuple_DataProductView_
}
/**
 * One decision, against the exact revision and content the browser displayed.
 *
 * `review_item_id` and `revised_content` carry what `decide_item` already accepts
 * and this command previously could not express, so a bundle with several undecided
 * items and a change request with replacement wording both had to be refused. They
 * are optional because the same command decides a fulfillment request, where
 * neither has any meaning; that route refuses them rather than ignoring them.
 */
export interface DecisionCommand {
  active_role: ActorRole
  decision: Decision1
  expected_revision: ExpectedRevision5
  review_item_id?: NonEmptyText | null
  reviewed_digest: ReviewedDigest1
  revised_content?: NonEmptyText | null
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
export interface ConsoleEnvelope_ImpactView_ {
  data: ImpactView
  meta: ApiMeta
}
export interface ImpactView {
  added_approvers?: JsonTuple_ImpactApproverView_
  affected_owners?: JsonTuple_NonEmptyText_1
  analyzed_at: UtcDatetime
  change_type: ImpactChangeType
  possible_impacts?: JsonTuple_ImpactItemView_
  request_id: PublicId
  subject_label: NonEmptyText
  validated_impacts?: JsonTuple_ImpactItemView_1
}
export interface ImpactApproverView {
  authority_label: NonEmptyText
  reason: NonEmptyText
}
export interface ImpactItemView {
  asset_type: NonEmptyText
  impact_handle: PublicId
  label: NonEmptyText
  owner_label: NonEmptyText
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
export interface IncidentRecoveryCommand {
  action: OperationalRecoveryAction
  active_role: ActiveRole4
  expected_revision: ExpectedRevision6
  reason: NonEmptyText
}
export interface ConsoleEnvelope_IncidentView_ {
  data: IncidentView
  meta: ApiMeta
}
export interface IncidentView {
  allowed_operator_actions?: JsonTuple_OperationalRecoveryAction_
  classification: IncidentFailureClassification
  failed_stage: IncidentStage
  incident_id: NonEmptyText
  kind: IncidentKind
  last_successful_stage?: IncidentStage | null
  next_automatic_action?: IncidentAutomaticAction | null
  opened_at: UtcDatetime
  recovery_recorded?: RecoveryRecorded
  revision: Revision3
  updated_at: UtcDatetime
  user_impact: NonEmptyText
}
export interface ConsoleEnvelope_IncidentsView_ {
  data: IncidentsView
  meta: ApiMeta
}
export interface IncidentsView {
  incidents?: JsonTuple_IncidentView_
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
  revision: Revision4
  state: OperationState
  summary?: string
}
export interface OperationFailureView {
  classification: Classification
  code: PublicId
  safe_message: NonEmptyText
}
export interface ProcessPackageCommand {
  active_role: ActiveRole5
  expected_revision: ExpectedRevision7
  file_name: FileName
  manifest: BusinessProcessManifestCommand
  media_type: MediaType
  narrative_markdown: NarrativeMarkdown
  package_digest: Digest
}
export interface BusinessProcessManifestCommand {
  entities: JsonTupleStr_
  events: JsonTupleStr_
  outcomes: JsonTupleStr_
  owner: Owner
  participants: JsonTupleStr_
  process_name: ProcessName
  rules: JsonTupleStr_
  schema_version?: SchemaVersion
  source_references: JsonTupleStr_
  states: JsonTupleStr_
  unresolved_questions: JsonTupleStr_
}
export interface ProductIntentApprovalCommand {
  active_role: ActiveRole6
  expected_revision: ExpectedRevision8
  reviewed_digest: Digest
}
export interface ConsoleEnvelope_ProductIntentApprovalView_ {
  data: ProductIntentApprovalView
  meta: ApiMeta
}
export interface ProductIntentApprovalView {
  approval_id: NonEmptyText
  approved_at: UtcDatetime
  approved_by: NonEmptyText
  artifact_reference: ArtifactReferenceView
  intent_digest: Digest
  intent_revision: IntentRevision
}
export interface ArtifactReferenceView {
  artifact_id: ArtifactId
  digest: Digest
  version: Version2
}
export interface ProposalPreparationCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision9
}
export interface RequestClarificationCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision10
  in_scope_summary: InScopeSummary
  out_of_scope_summary: OutOfScopeSummary
  restated_request: RestatedRequest
}
export interface ConsoleEnvelope_RequestDetailView_ {
  data: RequestDetailView
  meta: ApiMeta
}
export interface RequestDetailView {
  access_lifecycle?: AccessLifecycleView | null
  admission?: AdmissionView | null
  available_actions?: JsonTuple_Decision_
  conversation: ConversationView
  evidence: EvidenceContextView
  kind: RequestKind
  lifecycle?: JsonTuple_LifecycleEventView_
  preparation_actions?: JsonTuple_PreparationAction_
  preparation_notes?: JsonTuple_NonEmptyText_3
  product_intent?: ProductIntentReviewView | null
  proposal?: RequestProposalView | null
  proposal_digest?: Digest | null
  purpose: NonEmptyText
  question?: NonEmptyText | null
  request_id: PublicId
  revision: Revision5
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
  pending_delivery?: PendingDelivery
}
export interface EvidenceContextView {
  as_of?: UtcDatetime | null
  authorization_summary: NonEmptyText
  datasets?: JsonTuple_DatasetEvidenceView_
  evidence_refs?: JsonTuple_PublicId_
  freshness: FreshnessState
  lineage_summary: NonEmptyText
  metric_references?: JsonTuple_ArtifactReferenceView_
  metric_versions?: JsonTuple_NonEmptyText_2
  quality_summary: NonEmptyText
}
export interface DatasetEvidenceView {
  artifact_reference?: ArtifactReferenceView | null
  dataset_ref: PublicId
  display_name: NonEmptyText
}
export interface LifecycleEventView {
  event_id: PublicId
  occurred_at: UtcDatetime
  state: RequestState
  summary: NonEmptyText
}
export interface ProductIntentReviewView {
  approved: Approved
  approved_intent_revision?: ApprovedIntentRevision
  business_outcome: NonEmptyText
  dimensions?: JsonTuple_NonEmptyText_4
  filters?: JsonTuple_ProductIntentFilterView_
  freshness_seconds: FreshnessSeconds
  grain: NonEmptyJsonTuple_NonEmptyText_
  measures: NonEmptyJsonTuple_ProductIntentMeasureView_
  outputs: NonEmptyJsonTuple_ProductDeliveryOutput_
  reviewed_digest: Digest
  source_coverage: NonEmptyJsonTuple_ProductIntentSourceCoverageView_
  title: NonEmptyText
  unresolved_constraints?: JsonTuple_NonEmptyText_6
}
export interface ProductIntentFilterView {
  dimension_ref: NonEmptyText
  operator: ProductFilterOperator
  value: NonEmptyText
}
export interface ProductIntentMeasureView {
  aggregation: ProductAggregation
  metric_ref: NonEmptyText
}
export interface ProductIntentSourceCoverageView {
  authorized: Authorized
  covered_fields?: JsonTuple_NonEmptyText_5
  source_ref: NonEmptyText
}
export interface StakeholderAnswerProposalView {
  as_of: UtcDatetime
  authorization_summary: NonEmptyText
  candidate: NonEmptyText
  datasets?: JsonTuple_DatasetEvidenceView_1
  freshness: FreshnessState
  kind: Kind2
  lineage_references?: JsonTuple_ArtifactReferenceView_1
  lineage_summary: NonEmptyText
  metric_references?: JsonTuple_ArtifactReferenceView_2
  metric_version: NonEmptyText
  purpose: NonEmptyText
  quality_limitations?: JsonTuple_NonEmptyText_7
  quality_references?: JsonTuple_ArtifactReferenceView_3
  required_approvals?: JsonTuple_ProposalApprovalView_
  required_authorities?: JsonTuple_AuthorityStatusView_
}
export interface ProposalApprovalView {
  authority_label?: NonEmptyText | null
  authority_ref: NonEmptyText
  reason: NonEmptyText
  satisfied: Satisfied
}
export interface AuthorityStatusView {
  reason: NonEmptyText
  role: ActorRole
  satisfied: Satisfied1
}
export interface AccessPreviewProposalView {
  access_mode: AccessMode
  authority_summary: NonEmptyText
  data_product_ref: PublicId
  data_product_reference?: ArtifactReferenceView | null
  denied_checks?: JsonTuple_NonEmptyText_8
  effective_object_references?: JsonTuple_ArtifactReferenceView_4
  effective_scope?: JsonTuple_NonEmptyText_9
  exclusions?: JsonTuple_NonEmptyText_10
  expires_at: UtcDatetime
  intended_checks?: JsonTuple_NonEmptyText_11
  kind: Kind3
  purpose: NonEmptyText
  requested_fields: NonEmptyJsonTuple_NonEmptyText_
  required_approvals?: JsonTuple_ProposalApprovalView_1
  required_authorities?: JsonTuple_AuthorityStatusView_1
}
export interface DisclosureDenialProposalView {
  explanation: NonEmptyText
  kind: Kind4
  reason_code: NonEmptyText
  required_approvals?: JsonTuple_ProposalApprovalView_2
  required_authorities?: JsonTuple_AuthorityStatusView_2
}
/**
 * A requester withdraws their own request before any work has been admitted for it.
 */
export interface RequestWithdrawalCommand {
  active_role: ActiveRole7
  expected_revision: ExpectedRevision11
}
export interface ConsoleEnvelope_RequesterRequestView_ {
  data: RequesterRequestView
  meta: ApiMeta
}
export interface RequesterRequestView {
  access_lifecycle?: AccessLifecycleView | null
  clarified_outcome?: ClarifiedOutcomeView | null
  delivered_access?: DeliveredAccessView | null
  delivered_answer?: DeliveredAnswerView | null
  denial_explanation?: NonEmptyText | null
  kind: RequestKind
  no_valid_plan_explanation?: NonEmptyText | null
  own_decisions?: JsonTuple_OwnDecisionView_
  question?: NonEmptyText | null
  request_id: PublicId
  requested_outcome: NonEmptyText
  result_page_available?: ResultPageAvailable
  revision: Revision6
  state: RequestState
  title: NonEmptyText
  updated_at: UtcDatetime
}
export interface DeliveredAccessView {
  access_mode: AccessMode
  effective_at: UtcDatetime
  expires_at: UtcDatetime
  fields: NonEmptyJsonTuple_NonEmptyText_
  permissions: NonEmptyJsonTuple_Literal_Dashboard___Download___Query___View___
}
export interface DeliveredAnswerView {
  answer_text: NonEmptyText
  as_of: UtcDatetime
  datasets?: JsonTuple_ArtifactReferenceView_5
  delivery_ref: PublicId
  freshness: FreshnessState
  lineage?: JsonTuple_ArtifactReferenceView_6
  metrics?: JsonTuple_ArtifactReferenceView_7
  quality_limitations?: JsonTuple_ArtifactReferenceView_8
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
  active_role: ActiveRole8
  expected_revision: ExpectedRevision12
  reset_token: OpaqueToken
  setup_digest: Digest
}
export interface RetryOperationCommand {
  active_role: ActorRole
  expected_revision: ExpectedRevision13
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
  revision: Revision7
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
  satisfied: Satisfied2
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
  leased_runs?: JsonTuple_LeasedRunView_
  leased_runs_available?: LeasedRunsAvailable
  runs?: JsonTuple_RunView_
}
/**
 * A run as state owns it: identity from its canonical intent, and every attempt.
 *
 * `status` is state's own reading at `observed_at`. The console offers no action here;
 * retry and cancellation stay with the incident recovery flow that owns them.
 */
export interface LeasedRunView {
  attempts?: JsonTuple_RunAttemptView_
  contract_id: NonEmptyText
  contract_revision: ContractRevision
  last_durable_boundary_ref?: NonEmptyText | null
  observed_at: UtcDatetime
  run_id: NonEmptyText
  status: LeasedRunStatusView
  trigger_reason: TriggerReason
  window_ends_at: UtcDatetime
  window_starts_at: UtcDatetime
}
/**
 * One state-owned attempt: its lease, and the outcome and boundary it recorded, if any.
 */
export interface RunAttemptView {
  attempt_number: AttemptNumber
  claimed_at: UtcDatetime
  completed_at?: UtcDatetime | null
  durable_boundary_ref?: NonEmptyText | null
  epoch: Epoch
  failure_classification?: FailureClassification
  lease_expires_at: UtcDatetime
  lease_extensions?: LeaseExtensions
  outcome?: Outcome
  worker_ref: NonEmptyText
}
/**
 * A run as the evidence store witnessed it.
 *
 * `state` carries the store's own vocabulary rather than the shared
 * `OperationState`, because the two do not map without loss: a `non_conforming`
 * run is a known outcome, and the nearest shared value, `outcome_unknown`, would
 * report a witnessed non-conformance as ignorance.
 *
 * The timestamps are the record's own `created_at` and `updated_at`. Renaming
 * them to `started_at` and `completed_at` would assert a lifecycle meaning the
 * stored fields do not carry.
 */
export interface RunView {
  contract_digest: Digest
  created_at: UtcDatetime
  run_id: PublicId
  state: RunLifecycleState
  updated_at: UtcDatetime
}
export interface ConsoleEnvelope_SelectableAnswerTermsView_ {
  data: SelectableAnswerTermsView
  meta: ApiMeta
}
/**
 * Exactly the terms this tenant's publication carries, in canonical order.
 *
 * An empty tuple is a publication that carries no approved term, which is distinct from the
 * read refusing: a console with nothing published answers that it has nothing to offer.
 */
export interface SelectableAnswerTermsView {
  terms?: JsonTuple_SelectableAnswerTermView_
}
/**
 * One approved term a stakeholder question may be composed from.
 *
 * `term_ref` is the term's own canonical identifier -- what an answer intent names and what the
 * query binding resolves to a column -- rather than a console-side label, and `approved_version`
 * is the exact approved revision it resolves to. Offering a name without its version would offer
 * a meaning that could have changed since the publication approved it.
 *
 * `term_ref` is text rather than a console public identifier because the publication names its
 * own terms: a console that refused to display a term whose identifier did not match its own
 * pattern would hide a term the semantic layer will happily resolve.
 */
export interface SelectableAnswerTermView {
  approved_version: ArtifactReferenceView
  kind: AnswerTermKind
  term_ref: NonEmptyText
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
  enrollable_sources?: JsonTuple_EnrollableSourceHandleView_
  managed_services?: JsonTuple_ManagedServiceView_
  pending_review_refs?: JsonTuple_PublicId_2
  process_package?: ProcessPackageView | null
  reset_token: OpaqueToken
  revision: Revision8
  setup_digest: Digest
  sources?: JsonTuple_SourceConnectionView_
  stages: NonEmptyJsonTuple_SetupStageView_
  warehouse_binding?: WarehouseBindingView | null
  warehouse_options: NonEmptyJsonTuple_WarehouseOptionView_
  workspace_ref: PublicId
}
/**
 * One connection an operator enrolled that no binding names yet.
 *
 * This is an offer to register, not a connection: a handle, the provider that would read it,
 * the account mode the credential behind it is for, and the logical objects the deployment
 * declares for that handle. No endpoint, no credential and no reference to either -- the
 * console never receives a connection detail, and registering does not send it one.
 *
 * The declared objects come from the deployment's own declaration rather than from the
 * browser, because the probe requires the declaration it validates against to equal the
 * binding's approved objects: a set typed into a form would be refused by the probe at best,
 * and would be an unapproved declaration reaching a registration at worst.
 */
export interface EnrollableSourceHandleView {
  account_mode: SourceAccountModeView
  connection_handle: NonEmptyText
  declared_object_refs: NonEmptyJsonTuple_NonEmptyText_
  source_type: SourceProviderKind
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
  version: Version3
}
/**
 * One source the connection broker holds a binding for, as an architect reads it.
 *
 * `connection_handle` is the name an operator enrolled the connection under, and is the whole
 * of what the console knows about reaching the source: the connection detail itself is held by
 * whatever secret custody the deployment injected into the broker, and never travels here.
 * The handle is free text rather than a console public identifier because the deployment names
 * its own handles.
 *
 * `capability_authority_digest` is present exactly when the binding is `ready`, because
 * `record_validation` is the only writer of that state and it requires the two-probe evidence
 * this digest comes from. So the digest is the console's evidence that the source was probed,
 * rather than a claim this projection makes about it.
 */
export interface SourceConnectionView {
  account_mode?: SourceAccountModeView | null
  approved_object_refs?: JsonTuple_NonEmptyText_12
  capability_authority_digest?: Digest | null
  connection_handle?: NonEmptyText | null
  denied_checks?: JsonTuple_NonEmptyText_13
  display_name: NonEmptyText
  intended_checks?: JsonTuple_NonEmptyText_14
  lifecycle_state?: SourceBindingStateView | null
  source_ref: PublicId
  source_type: SourceProviderKind
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
/**
 * Register the source behind one already enrolled connection handle.
 *
 * The handle is the only subject the browser names. Everything the broker needs beyond it --
 * the provider kind, the account mode and the approved objects -- is taken from the offering
 * the server read, so the browser cannot widen a declaration or name a provider for a handle
 * the deployment declared differently. There is deliberately no connection field of any kind:
 * a browser form that accepted a connection string would be a credential-handling surface, and
 * `DemoSourceSecretStore.enroll_connection` is an operator action that happens before any
 * binding exists.
 */
export interface SourceRegistrationCommand {
  active_role: ActiveRole9
  connection_handle: NonEmptyText
  expected_revision: ExpectedRevision14
}
export interface WarehouseBindingCommand {
  active_role: ActorRole
  capacity: NonEmptyText
  engine: WarehouseEngine
  expected_revision: ExpectedRevision15
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
