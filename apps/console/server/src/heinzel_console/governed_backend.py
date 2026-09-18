"""Governed-local console read projections.

`GovernedConsoleBackend` composes immutable read models from the owning services'
public interfaces. A capability with no real owning implementation reports
`not_delivered` and its route fails closed; a failing repository reports a typed
degraded or unavailable state. Neither path substitutes demo content, because a
console that answers with fixtures after a real read fails is worse than one that
answers with nothing.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import re
import secrets
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Literal, Never

from heinzel_access_control import AccessGrant, AccessGrantDenied, AccessGrantIntegrityError
from heinzel_bi_control import DashboardPublication
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import BusinessProcessManifest
from heinzel_evidence import AcquisitionEvidenceReceipt
from heinzel_request_management import (
    ArchitectRequestView,
    ConversationEntry,
    FulfillmentApprovalBinding,
    FulfillmentProposal,
    GovernedAnswer,
    GovernedAnswerNotVisible,
    InboxRequest,
    ProductIntentCandidate,
    ProductIntentNoValidPlan,
    RequestState,
    ReviewerRequestView,
)
from heinzel_request_management import RequesterRequestView as ServiceRequesterRequestView
from heinzel_request_management.fulfillment_models import (
    AccessScopePreview,
    DenialDispositionReceipt,
    DisclosureDenial,
    FulfillmentAdmissionReceipt,
    StakeholderAnswerDraft,
)
from heinzel_request_management.models import DataAccessRequest, StakeholderQuestion
from heinzel_runtime import AnswerExecutionReceipt, AnswerResultSnapshot, QueryResultNotFound
from heinzel_semantic_registry import CatalogPublicationRepository, OntologyReviewBundle
from heinzel_semantic_registry.review import ReviewItemDecision
from heinzel_state import (
    ExternalEffectRecoveryUnavailableError,
    IncidentConflictError,
    IncidentIntegrityError,
    IncidentNotFoundError,
    IncidentPersistenceError,
    IncidentRecord,
    RecoveryActionNotAllowedError,
    RecoveryCommand,
    StaleIncidentRevisionError,
)
from heinzel_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseOperationStatus,
)

from .auth import TrustedActorContext
from .backend import AuthorizedDownload
from .contracts import (
    AccessLifecycleView,
    AccessPreviewProposalView,
    AccessRevocationCommand,
    AcquisitionReceiptsView,
    AcquisitionReceiptView,
    AcquisitionRunNowCommand,
    ActorDisplayView,
    ActorRole,
    AdmissionCommand,
    AdmissionView,
    AnswerResultColumnView,
    AnswerResultPageView,
    ArtifactReferenceView,
    CapabilityState,
    CapabilityView,
    CatalogAssetsView,
    CatalogAssetView,
    ClarifiedOutcomeAcceptanceCommand,
    ClarifiedOutcomeView,
    ConversationMessageCommand,
    ConversationMessageView,
    ConversationView,
    CreateRequestCommand,
    DashboardsView,
    DashboardView,
    DataProductsView,
    DataProductView,
    DatasetEvidenceView,
    Decision,
    DecisionCommand,
    DeliveredAccessView,
    DeliveredAnswerView,
    DisclosureDenialProposalView,
    DisplayReferenceView,
    EvidenceContextView,
    EvidenceView,
    FreshnessState,
    ImpactView,
    InboxItemView,
    InboxView,
    IncidentRecoveryCommand,
    IncidentsView,
    IncidentView,
    LeasedRunView,
    LifecycleEventView,
    OperationalRecoveryAction,
    OperationFailureView,
    OperationState,
    OperationView,
    OwnDecisionView,
    PreparationAction,
    ProcessPackageCommand,
    ProcessPackageView,
    ProductIntentApprovalCommand,
    ProductIntentApprovalView,
    ProductIntentFilterView,
    ProductIntentMeasureView,
    ProductIntentReviewView,
    ProductIntentSourceCoverageView,
    ProposalApprovalView,
    ProposalPreparationCommand,
    RequestClarificationCommand,
    RequestDetailView,
    RequesterRequestView,
    RequestKind,
    RequestProposalView,
    RequestWithdrawalCommand,
    ResetCommand,
    RetryOperationCommand,
    ReviewItemView,
    ReviewSectionView,
    ReviewView,
    RiskLevel,
    RunAttemptView,
    RunsView,
    RunView,
    SessionView,
    SetupStage,
    SetupStageState,
    SetupStageView,
    SetupView,
    StakeholderAnswerProposalView,
    WarehouseBindingCommand,
    WarehouseBindingView,
    WarehouseOptionView,
    WorkspaceState,
    WorkspaceView,
    setup_snapshot_digest,
)
from .contracts import (
    RequestState as ConsoleRequestState,
)
from .errors import (
    ConsoleConflict,
    ConsoleError,
    ConsoleInvalidRequest,
    ConsoleNotFound,
    ConsoleUnavailable,
)
from .governed_adapters import (
    AccessGrantCommands,
    AccessGrantReader,
    AccessGrantRevocationCommands,
    AcquisitionRunNowCommands,
    AnswerDownloadReceipt,
    AnswerDownloadReceiptWriter,
    AnswerResultReader,
    CatalogBindingReader,
    CatalogSearchHealthReader,
    DashboardPublicationReader,
    DataProductReferenceReader,
    FulfillmentAccessExecutionCommands,
    FulfillmentDecisionCommands,
    FulfillmentExecutionCommands,
    FulfillmentPreparationCommands,
    FulfillmentViewReader,
    GovernedWorkspaceIdentity,
    IncidentRecoveryCommands,
    ProcessPackageCommands,
    ProductIntentApprovalCommands,
    ProductIntentReviewReader,
    ProductPublicationDefinitionReader,
    RequestImpactReader,
    RequestInboxReader,
    RequestIntakeCommands,
    SemanticReviewCommands,
    SemanticReviewReader,
    TenantAcquisitionReceiptReader,
    TenantIncidentReader,
    TenantRunLifecycleReader,
    TenantRunReader,
    VerifiedAnswerReader,
    WarehouseBindingReader,
    WarehouseConfirmation,
    WarehouseLifecycleCommands,
    WarehouseOperationReader,
    WorkspaceActorDirectory,
    WorkspacePrincipalDirectory,
    classify_downstream_failure,
    console_error_for,
)
from .operation_handles import (
    OperationHandleRecord,
    OperationHandleRepository,
    PublicOperationStatus,
    mint_console_handle,
    serialize_operation,
)
from .request_intake import request_intake_content

CAPABILITY_NOT_DELIVERED = "capability_not_delivered"

_SETUP_STAGES: tuple[SetupStage, ...] = (
    "foundation",
    "managed_services",
    "sources",
    "business_process",
    "meaning",
    "data_product",
    "activation",
)
_UNDELIVERED_STAGES: frozenset[SetupStage] = frozenset(
    {"sources", "meaning", "data_product", "activation"}
)
_REQUEST_STATES: dict[RequestState, ConsoleRequestState] = {
    RequestState.SUBMITTED: "submitted",
    RequestState.CLARIFYING: "clarifying",
    RequestState.INVESTIGATING: "investigating",
    RequestState.PROPOSED: "proposed",
    RequestState.AWAITING_APPROVAL: "awaiting_approval",
    RequestState.EXECUTING: "execution_ready",
    RequestState.VERIFYING: "execution_ready",
    RequestState.DELIVERED: "delivered",
    RequestState.MONITORING: "delivered",
    RequestState.REJECTED: "denied",
    RequestState.NO_VALID_PLAN: "no_valid_plan",
    RequestState.CANCELLED: "cancelled",
    RequestState.FAILED: "failed",
    RequestState.RETIRED: "closed",
}
_BLOCKED_REASONS: dict[RequestState, str] = {
    RequestState.NO_VALID_PLAN: "No valid plan was recorded for this request.",
    RequestState.REJECTED: "The request was denied by the owning authority.",
    RequestState.FAILED: "The request failed before it reached a delivered outcome.",
}
# A requester may withdraw only before any work is admitted. Request-management itself would
# cancel an executing or delivered request, so the console holds the narrower line.
_WITHDRAWABLE_STATES: frozenset[RequestState] = frozenset(
    {
        RequestState.SUBMITTED,
        RequestState.CLARIFYING,
        RequestState.INVESTIGATING,
        RequestState.PROPOSED,
        RequestState.AWAITING_APPROVAL,
    }
)
_BINDING_STATES: dict[WarehouseBindingState, CapabilityState] = {
    WarehouseBindingState.DRAFT: "blocked",
    WarehouseBindingState.PROVISIONING: "blocked",
    WarehouseBindingState.VALIDATING: "blocked",
    WarehouseBindingState.READY: "ready",
    WarehouseBindingState.FAILED: "blocked",
    WarehouseBindingState.SUSPENDED: "degraded",
    WarehouseBindingState.RETIRING: "blocked",
    WarehouseBindingState.RETIRED: "blocked",
}
_OPERATION_STATES: dict[WarehouseOperationStatus, OperationState] = {
    WarehouseOperationStatus.CLAIMED: "accepted",
    WarehouseOperationStatus.RUNNING: "running",
    WarehouseOperationStatus.RECONCILING: "outcome_unknown",
    WarehouseOperationStatus.SUCCEEDED: "succeeded",
    WarehouseOperationStatus.FAILED: "failed",
}
_TRANSIENT_CLASSIFICATIONS = frozenset(
    {
        WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
        WarehouseFailureClassification.THROTTLED,
    }
)
_DECISIONS: frozenset[str] = frozenset({"approve", "reject", "request_changes"})
_MAX_RESULT_PAGE_SIZE = 500
_SAFE_FILENAME_CHARACTER = re.compile(r"[^a-z0-9]+")


def _supported_incident_actions(
    record: IncidentRecord,
) -> tuple[OperationalRecoveryAction, ...]:
    actions: list[OperationalRecoveryAction] = []
    for action in record.allowed_operator_actions:
        if (
            action == "retry_transient_attempt"
            or action == "cancel_unstarted_work"
            or action == "reconcile_external_effect"
        ):
            actions.append(action)
    return tuple(actions)


# `request_changes` maps to the owning revise decision, which requires replacement
# wording; the command carries it now, and a request without it is still refused.
_REVIEW_DECISIONS: dict[Decision, ReviewItemDecision] = {
    "approve": ReviewItemDecision.ACCEPT,
    "reject": ReviewItemDecision.REJECT,
    "request_changes": ReviewItemDecision.REVISE,
}
_BINDING_OPERATION_STATES: dict[WarehouseBindingState, OperationState] = {
    WarehouseBindingState.DRAFT: "accepted",
    WarehouseBindingState.PROVISIONING: "running",
    WarehouseBindingState.VALIDATING: "running",
    WarehouseBindingState.READY: "succeeded",
    WarehouseBindingState.FAILED: "failed",
    WarehouseBindingState.SUSPENDED: "succeeded",
    WarehouseBindingState.RETIRING: "running",
    WarehouseBindingState.RETIRED: "succeeded",
}


def _not_delivered(dependency: str) -> ConsoleUnavailable:
    return ConsoleUnavailable(
        code=CAPABILITY_NOT_DELIVERED,
        safe_message=f"This capability is not delivered in governed-local mode. It depends on "
        f"{dependency}.",
        recovery_action="none",
    )


def _catalog_classification_label(
    reference: str,
    *,
    contract_digest: str,
    semantic_names: dict[str, str],
) -> str:
    if reference == contract_digest:
        return "Governed by approved contract"
    return semantic_names.get(reference, "Governed classification")


def _engine_label(engine: EngineKind) -> str:
    """The engine's name as an architect writes it.

    `EngineKind` values are identifiers; rendering one as the choice's label put
    `postgresql` in front of the person making an immutable decision. Naming an
    engine claims no authority over it.

    A `match` rather than a mapping so that adding a member to `EngineKind` is a type
    error here. A dictionary subscript would instead raise `KeyError` out of
    `get_setup`, outside `_guarded`, and take the whole setup read down with an
    internal error rather than losing one option.
    """
    match engine:
        case EngineKind.POSTGRESQL:
            return "PostgreSQL"
        case EngineKind.CLICKHOUSE:
            return "ClickHouse"


class GovernedConsoleBackend:
    def __init__(
        self,
        *,
        identity: GovernedWorkspaceIdentity,
        operation_handles: OperationHandleRepository,
        warehouse_bindings: WarehouseBindingReader | None = None,
        warehouse_operations: WarehouseOperationReader | None = None,
        catalog_bindings: CatalogBindingReader | None = None,
        catalog_search_health: CatalogSearchHealthReader | None = None,
        catalog_publications: CatalogPublicationRepository | None = None,
        semantic_reviews: SemanticReviewReader | None = None,
        requests: RequestInboxReader | None = None,
        fulfillment: FulfillmentViewReader | None = None,
        runs: TenantRunReader | None = None,
        run_lifecycle: TenantRunLifecycleReader | None = None,
        incidents: TenantIncidentReader | None = None,
        impact_reader: RequestImpactReader | None = None,
        incident_recovery_commands: IncidentRecoveryCommands | None = None,
        acquisition_receipts: TenantAcquisitionReceiptReader | None = None,
        acquisition_commands: AcquisitionRunNowCommands | None = None,
        data_products: DataProductReferenceReader | None = None,
        product_publications: ProductPublicationDefinitionReader | None = None,
        dashboards: DashboardPublicationReader | None = None,
        data_access_intake_available: bool = True,
        actors: WorkspaceActorDirectory | None = None,
        principals: WorkspacePrincipalDirectory | None = None,
        warehouse_commands: WarehouseLifecycleCommands | None = None,
        request_commands: RequestIntakeCommands | None = None,
        fulfillment_commands: FulfillmentDecisionCommands | None = None,
        fulfillment_execution_commands: FulfillmentExecutionCommands | None = None,
        fulfillment_access_execution_commands: FulfillmentAccessExecutionCommands | None = None,
        access_grant_commands: AccessGrantCommands | None = None,
        access_grants: AccessGrantReader | None = None,
        access_revocation_commands: AccessGrantRevocationCommands | None = None,
        fulfillment_preparation_commands: FulfillmentPreparationCommands | None = None,
        product_intent_reviews: ProductIntentReviewReader | None = None,
        product_intent_commands: ProductIntentApprovalCommands | None = None,
        process_package_commands: ProcessPackageCommands | None = None,
        semantic_review_commands: SemanticReviewCommands | None = None,
        answer_results: AnswerResultReader | None = None,
        verified_answers: VerifiedAnswerReader | None = None,
        answer_downloads: AnswerDownloadReceiptWriter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        answer_download_identifiers: Callable[[], str] = lambda: (
            f"download-{secrets.token_hex(16)}"
        ),
        reset_tokens: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._identity = identity
        self._operation_handles = operation_handles
        self._warehouse_bindings = warehouse_bindings
        self._warehouse_operations = warehouse_operations
        self._catalog_bindings = catalog_bindings
        self._catalog_search_health = catalog_search_health
        self._catalog_publications = catalog_publications
        self._semantic_reviews = semantic_reviews
        self._requests = requests
        self._fulfillment = fulfillment
        self._runs = runs
        self._run_lifecycle = run_lifecycle
        self._incidents = incidents
        self._impact_reader = impact_reader
        self._incident_recovery_commands = incident_recovery_commands
        self._acquisition_receipts = acquisition_receipts
        self._acquisition_commands = acquisition_commands
        self._data_products = data_products
        self._product_publications = product_publications
        self._dashboards = dashboards
        self._data_access_intake_available = data_access_intake_available
        self._actors = actors
        self._principals = principals
        self._warehouse_commands = warehouse_commands
        self._request_commands = request_commands
        self._fulfillment_commands = fulfillment_commands
        self._fulfillment_execution_commands = fulfillment_execution_commands
        self._fulfillment_access_execution_commands = fulfillment_access_execution_commands
        self._access_grant_commands = access_grant_commands
        self._access_grants = access_grants
        self._access_revocation_commands = access_revocation_commands
        self._fulfillment_preparation_commands = fulfillment_preparation_commands
        self._product_intent_reviews = product_intent_reviews
        self._product_intent_commands = product_intent_commands
        self._process_package_commands = process_package_commands
        self._semantic_review_commands = semantic_review_commands
        self._answer_results = answer_results
        self._verified_answers = verified_answers
        self._answer_downloads = answer_downloads
        self._clock = clock
        self._answer_download_identifiers = answer_download_identifiers
        self._reset_tokens = reset_tokens

    @property
    def fixture_mode(self) -> bool:
        return False

    def get_session(self, context: TrustedActorContext, csrf_token: str) -> SessionView:
        return SessionView(
            actor=ActorDisplayView(
                display_name=self._actor_label(context.tenant_id, context.actor_id)
            ),
            roles=context.roles,
            active_role=context.active_role,
            tenant=DisplayReferenceView(
                ref=self._identity.tenant_ref,
                display_name=self._identity.tenant_display_name,
            ),
            workspace=DisplayReferenceView(
                ref=self._identity.workspace_ref,
                display_name=self._identity.workspace_display_name,
            ),
            csrf_token=csrf_token,
        )

    def get_workspace(self, context: TrustedActorContext) -> WorkspaceView:
        warehouse_state, warehouse_detail = self._warehouse_capability(context)
        catalog_state, catalog_detail = self._catalog_capability(context)
        request_state, request_detail = self._request_capability(context)
        review_state: CapabilityState = (
            "ready" if self._semantic_reviews is not None else "not_delivered"
        )
        access_delivery_available = all(
            command is not None
            for command in (
                self._request_commands,
                self._fulfillment_preparation_commands,
                self._fulfillment_commands,
                self._access_grant_commands,
                self._fulfillment_access_execution_commands,
            )
        )
        operation_recovery_available = (
            self._incidents is not None and self._incident_recovery_commands is not None
        )
        capabilities: list[CapabilityView] = [
            CapabilityView(
                capability_id="warehouse-binding",
                label="Managed warehouse",
                state=warehouse_state,
                detail=warehouse_detail,
                dependency=(
                    "warehouse-control read wiring" if warehouse_state == "not_delivered" else None
                ),
            ),
            CapabilityView(
                capability_id="catalog-binding",
                label="Managed catalog",
                state=catalog_state,
                detail=catalog_detail,
                dependency=(
                    "catalog-control read wiring" if catalog_state == "not_delivered" else None
                ),
            ),
            CapabilityView(
                capability_id="semantic-review",
                label="Meaning review",
                state=review_state,
                detail=(
                    "Ontology review bundles are read from the semantic registry."
                    if review_state == "ready"
                    else "No semantic review reader is wired."
                ),
                dependency=(
                    "semantic-registry review wiring" if review_state == "not_delivered" else None
                ),
            ),
            CapabilityView(
                capability_id="request-fulfillment",
                label="Requests and fulfillment",
                state=request_state,
                detail=request_detail,
                dependency=(
                    "request-management read wiring" if request_state == "not_delivered" else None
                ),
            ),
            CapabilityView(
                capability_id="request-conversation",
                label="Request conversation",
                state="ready" if self._request_commands is not None else "not_delivered",
                detail=(
                    "The thread is read from request-management in append order."
                    if self._request_commands is not None
                    else "No request-management conversation interface is wired."
                ),
                dependency=(
                    None
                    if self._request_commands is not None
                    else "request-management conversation wiring"
                ),
            ),
            CapabilityView(
                capability_id="data-product-runs",
                label="Data products and runs",
                state=(
                    "ready"
                    if self._runs is not None and self._data_products is not None
                    else "not_delivered"
                ),
                detail=(
                    "Runs are listed by deriving the tenant through its activated "
                    "contracts, and a data product is shown as the reference its "
                    "governing policy permitted."
                    if self._runs is not None and self._data_products is not None
                    else "No owning service publishes tenant-scoped data products or runs."
                ),
                dependency=(
                    None
                    if self._runs is not None and self._data_products is not None
                    else "a tenant-scoped data product and run read interface"
                ),
            ),
            CapabilityView(
                capability_id="acquisition-evidence",
                label="Acquisition evidence",
                state="ready" if self._acquisition_receipts is not None else "not_delivered",
                detail=(
                    "Receipts the acquisition runtime recorded are listed for the "
                    "tenant that owns them, refusals included."
                    if self._acquisition_receipts is not None
                    else "No owning service retains the receipts an acquisition records."
                ),
                dependency=(
                    None
                    if self._acquisition_receipts is not None
                    else "a durable acquisition evidence store"
                ),
            ),
            CapabilityView(
                capability_id="source-acquisition",
                label="Source acquisition",
                state="ready" if self._acquisition_commands is not None else "not_delivered",
                detail=(
                    "An activated contract can be run now through the acquisition application."
                    if self._acquisition_commands is not None
                    else "No acquisition application command interface is wired."
                ),
                dependency=(
                    None
                    if self._acquisition_commands is not None
                    else "a composed acquisition application"
                ),
            ),
            CapabilityView(
                capability_id="catalog-asset-preview",
                label="Catalog asset preview",
                state="ready" if self._catalog_publications is not None else "not_delivered",
                detail=(
                    "Published catalog assets can be listed and opened with their "
                    "governed meaning, ownership, classification, and lineage summary."
                    if self._catalog_publications is not None
                    else "No catalog asset read interface is wired."
                ),
                dependency=(
                    None
                    if self._catalog_publications is not None
                    else "a catalog asset read interface over published references"
                ),
            ),
            CapabilityView(
                capability_id="analyst-dashboard",
                label="Analyst dashboards",
                state="ready" if self._dashboards is not None else "not_delivered",
                detail=(
                    "Published dashboards are read from BI control after provider receipt."
                    if self._dashboards is not None
                    else "No BI control publication read interface is wired."
                ),
                dependency=(
                    None
                    if self._dashboards is not None
                    else "a provider-receipted BI control publication read interface"
                ),
            ),
            CapabilityView(
                capability_id="process-package",
                label="Business process package",
                state="ready" if self._process_package_commands is not None else "not_delivered",
                detail=(
                    "UTF-8 Markdown narratives and strict manifests are persisted "
                    "by contract service."
                    if self._process_package_commands is not None
                    else "No contract process-package command interface is wired."
                ),
                dependency=(
                    None
                    if self._process_package_commands is not None
                    else "command delegation to contract process-package service"
                ),
            ),
            CapabilityView(
                capability_id="data-access-intake",
                label="Data access requests",
                state=(
                    "ready"
                    if self._data_access_intake_available and access_delivery_available
                    else "not_delivered"
                ),
                detail=(
                    "Approved access is applied, verified, delivered, and expires automatically."
                    if self._data_access_intake_available and access_delivery_available
                    else "Data access requests are not accepted: grant application, expiry, "
                    "and revocation are not delivered."
                ),
                dependency=(
                    None
                    if self._data_access_intake_available and access_delivery_available
                    else "an owning service that applies, expires, and revokes access grants"
                ),
            ),
            CapabilityView(
                capability_id="operation-retry",
                label="Operation recovery",
                state="ready" if operation_recovery_available else "not_delivered",
                detail=(
                    "State-owned incidents expose only recovery actions valid for the current "
                    "revision."
                    if operation_recovery_available
                    else "Incident recovery requires both the authoritative incident reader and "
                    "its command service."
                ),
                dependency=(
                    None
                    if operation_recovery_available
                    else "state-owned incident read and recovery command wiring"
                ),
            ),
        ]
        degraded = any(capability.state == "degraded" for capability in capabilities)
        state: WorkspaceState = "unavailable" if degraded else self._workspace_state(capabilities)
        return WorkspaceView(
            workspace=DisplayReferenceView(
                ref=self._identity.workspace_ref,
                display_name=self._identity.workspace_display_name,
            ),
            state=state,
            capabilities=tuple(capabilities),
            recovery_message=(
                "A governing service is temporarily unavailable. Reload to retry."
                if degraded
                else None
            ),
        )

    def get_setup(self, context: TrustedActorContext) -> SetupView:
        self._authorize(context, ("data_architect",))
        binding = self._require_warehouse_binding_reader(context)
        catalog_reader = self._catalog_bindings
        catalog_binding = (
            None
            if catalog_reader is None
            else self._guarded(lambda: catalog_reader.current_binding(context.tenant_id))
        )
        process_package_commands = self._process_package_commands
        process_package = (
            None
            if process_package_commands is None
            else self._guarded(lambda: process_package_commands.latest(context.tenant_id))
        )
        stage_states = self._stage_states(
            binding,
            catalog_binding,
            process_package_available=process_package_commands is not None,
            process_package_recorded=process_package is not None,
        )
        active_stage = next(
            (stage for stage in _SETUP_STAGES if stage_states[stage] == "current"),
            next(
                (stage for stage in _SETUP_STAGES if stage_states[stage] != "complete"),
                _SETUP_STAGES[-1],
            ),
        )
        payload: dict[str, object] = {
            "workspace_ref": self._identity.workspace_ref,
            "revision": binding.revision if binding is not None else 1,
            "reset_token": self._reset_tokens(),
            "active_stage": active_stage,
            "stages": tuple(
                SetupStageView(
                    stage=stage,
                    label=stage.replace("_", " ").capitalize(),
                    state=stage_states[stage],
                    detail=(
                        "No governed implementation is wired for this stage."
                        if stage in _UNDELIVERED_STAGES
                        else None
                    ),
                )
                for stage in _SETUP_STAGES
            ),
            "warehouse_options": tuple(
                WarehouseOptionView(
                    engine=engine.value,
                    label=_engine_label(engine),
                    supported_region="Fixed at confirmation",
                    fixed_capacity="mvp-fixed",
                )
                for engine in EngineKind
            ),
            "warehouse_binding": (
                None
                if binding is None
                else WarehouseBindingView(
                    binding_ref=binding.binding_id,
                    engine=binding.engine_kind.value,
                    region=binding.region,
                    capacity=binding.capacity_profile,
                    state=_BINDING_STATES[binding.lifecycle_state],
                )
            ),
            "process_package": (
                None
                if process_package is None
                else ProcessPackageView(
                    package_ref=process_package.receipt.package_id,
                    version=process_package.receipt.version,
                    content_digest=process_package.receipt.original_digest,
                    state="ready",
                    candidate_summary=process_package.manifest.process_name,
                )
            ),
        }
        unbound = SetupView.model_validate(payload | {"setup_digest": "0" * 64})
        return SetupView.model_validate(
            unbound.model_dump(mode="python") | {"setup_digest": setup_snapshot_digest(unbound)}
        )

    def get_review(self, context: TrustedActorContext, review_id: str) -> ReviewView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        reader = self._semantic_reviews
        if reader is None:
            raise _not_delivered("semantic-registry review wiring")
        bundle = self._guarded(lambda: reader.load_review_bundle(context.tenant_id, review_id))
        return self._review_view(context, bundle)

    def get_inbox(self, context: TrustedActorContext) -> InboxView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        requests = self._require_requests(context)
        items = tuple(
            item for item in (self._inbox_item(request) for request in requests) if item is not None
        )
        return InboxView(items=items)

    def get_request_detail(
        self, context: TrustedActorContext, request_id: str
    ) -> RequestDetailView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        request = self._visible_request(context, request_id)
        if context.active_role != "data_architect":
            return self._reviewer_request_detail_view(context, request)
        return self._request_detail_view(context, request)

    def get_request_impact(self, context: TrustedActorContext, request_id: str) -> ImpactView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        reader = self._impact_reader
        if reader is None:
            raise _not_delivered("an authorization-filtered impact read interface")
        impact = self._guarded(
            lambda: reader.get_request_impact(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                active_role=context.active_role,
                request_id=request_id,
            )
        )
        if impact is None or impact.request_id != request_id:
            raise ConsoleNotFound()
        return impact

    def get_requester_requests(
        self, context: TrustedActorContext
    ) -> tuple[RequesterRequestView, ...]:
        self._authorize(context, ("requester",))
        requests = self._require_requests(context)
        self._require_fulfillment()
        return tuple(
            self._requester_request(context, request)
            for request in requests
            if request.requester_id == context.actor_id
        )

    def get_answer_result(
        self,
        context: TrustedActorContext,
        request_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 100,
    ) -> AnswerResultPageView:
        self._authorize(context, ("requester",))
        request, answer, receipt = self._answer_result_authority(context, request_id)
        if receipt.outcome != "succeeded":
            return AnswerResultPageView(
                request_id=request.request_id,
                title=self._request_title(request),
                answer_text=None if answer is None else answer.narrative,
                status="failed",
                freshness=None if answer is None else answer.freshness_disposition,
                as_of=None if answer is None else answer.as_of,
                row_count=receipt.row_count,
            )
        if (
            answer is None
            or request.state is not RequestState.DELIVERED
            or receipt.result_ref is None
        ):
            raise ConsoleNotFound()
        try:
            snapshot = self._answer_results_or_fail().read_result(
                context.tenant_id, receipt.result_ref
            )
        except QueryResultNotFound:
            return AnswerResultPageView(
                request_id=request.request_id,
                title=self._request_title(request),
                answer_text=answer.narrative,
                status="expired",
                freshness=answer.freshness_disposition,
                as_of=answer.as_of,
                row_count=receipt.row_count,
            )
        except Exception as error:
            raise console_error_for(error) from error
        if (
            snapshot.tenant_id != context.tenant_id
            or snapshot.request_id != request_id
            or snapshot.plan_digest != receipt.plan_digest
            or snapshot.result_ref != receipt.result_ref
            or snapshot.result_digest != receipt.result_digest
            or snapshot.result_schema_digest != receipt.result_schema_digest
            or snapshot.row_count != receipt.row_count
            or snapshot.byte_count != receipt.byte_count
            or snapshot.product_generation_refs != receipt.product_generation_refs
        ):
            raise ConsoleUnavailable(
                code="answer_result_integrity_failure",
                safe_message="The answer result could not be verified.",
                recovery_action="contact_support",
            )
        if not 1 <= page_size <= _MAX_RESULT_PAGE_SIZE:
            raise ConsoleInvalidRequest(
                code="invalid_page_size",
                safe_message=f"Page size must be between 1 and {_MAX_RESULT_PAGE_SIZE}.",
                recovery_action="correct_input",
                field="page_size",
            )
        offset = self._result_offset(cursor, snapshot.result_digest)
        if offset > snapshot.row_count:
            raise ConsoleInvalidRequest(
                code="invalid_result_cursor",
                safe_message="The result page cursor is invalid.",
                recovery_action="correct_input",
                field="cursor",
            )
        end = min(offset + page_size, snapshot.row_count)
        next_cursor = (
            self._result_cursor(snapshot.result_digest, end) if end < snapshot.row_count else None
        )
        return AnswerResultPageView(
            request_id=request.request_id,
            title=self._request_title(request),
            answer_text=answer.narrative,
            status="available",
            freshness=answer.freshness_disposition,
            as_of=answer.as_of,
            row_count=receipt.row_count,
            columns=tuple(
                AnswerResultColumnView(
                    name=column.name,
                    label=column.name.replace("_", " ").strip().title(),
                    value_type=column.value_type,
                    allowed_operations=(),
                )
                for column in snapshot.columns
            ),
            rows=snapshot.rows[offset:end],
            next_cursor=next_cursor,
        )

    def download_answer_result(
        self, context: TrustedActorContext, request_id: str
    ) -> AuthorizedDownload:
        request, answer, receipt = self._answer_result_authority(
            context, request_id, permission="download"
        )
        if (
            receipt.outcome != "succeeded"
            or receipt.result_ref is None
            or answer.result_ref is None
            or request.state is not RequestState.DELIVERED
        ):
            raise ConsoleNotFound()
        try:
            snapshot = self._answer_results_or_fail().read_result(
                context.tenant_id, receipt.result_ref
            )
        except QueryResultNotFound as error:
            raise ConsoleNotFound() from error
        except Exception as error:
            raise console_error_for(error) from error
        if (
            snapshot.tenant_id != context.tenant_id
            or snapshot.request_id != request_id
            or snapshot.plan_digest != receipt.plan_digest
            or snapshot.result_ref != receipt.result_ref
            or snapshot.result_digest != receipt.result_digest
            or snapshot.result_schema_digest != receipt.result_schema_digest
            or snapshot.row_count != receipt.row_count
            or snapshot.byte_count != receipt.byte_count
            or snapshot.product_generation_refs != receipt.product_generation_refs
        ):
            raise ConsoleUnavailable(
                code="answer_result_integrity_failure",
                safe_message="The answer result could not be verified.",
                recovery_action="contact_support",
            )
        body = self._csv(snapshot)
        writer = self._answer_downloads
        if writer is None:
            raise _not_delivered("durable answer download receipts")
        download = AnswerDownloadReceipt(
            download_id=self._answer_download_identifiers(),
            tenant_id=context.tenant_id,
            request_id=request_id,
            actor_id=context.actor_id,
            result_digest=snapshot.result_digest,
            row_count=snapshot.row_count,
            created_at=self._clock(),
        )
        try:
            writer.record(download)
        except Exception as error:
            raise ConsoleUnavailable(
                code="answer_download_unavailable",
                safe_message="The answer download could not be recorded.",
                recovery_action="retry",
            ) from error
        return AuthorizedDownload(
            body=body,
            media_type="text/csv",
            filename=self._download_filename(self._request_title(request)),
            result_digest=snapshot.result_digest,
        )

    def _answer_result_authority(
        self,
        context: TrustedActorContext,
        request_id: str,
        *,
        permission: Literal["view", "download"] = "view",
    ) -> tuple[InboxRequest, GovernedAnswer, AnswerExecutionReceipt]:
        self._authorize(context, ("requester",))
        requests = self._require_requests(context)
        request = next(
            (
                candidate
                for candidate in requests
                if candidate.request_id == request_id and candidate.requester_id == context.actor_id
            ),
            None,
        )
        if request is None:
            raise ConsoleNotFound()
        if self._verified_answers is None:
            raise _not_delivered("current governed answer authorization")
        try:
            read = (
                self._verified_answers.read_for_download
                if permission == "download"
                else self._verified_answers.read_for_request
            )
            answer = read(
                tenant_id=context.tenant_id,
                requester_id=context.actor_id,
                request_id=request_id,
            )
        except GovernedAnswerNotVisible as error:
            raise ConsoleNotFound() from error
        execution = self._guarded(
            lambda: self._answer_results_or_fail().load_execution(context.tenant_id, request_id)
        )
        if execution is None:
            raise ConsoleNotFound()
        receipt = execution[1]
        if (
            answer.tenant_id != context.tenant_id
            or answer.request_id != request_id
            or answer.execution_receipt_ref != receipt.receipt_id
            or answer.result_ref != receipt.result_ref
            or answer.result_digest != receipt.result_digest
            or digest(answer.product_generation_refs) != digest(receipt.product_generation_refs)
            or receipt.tenant_id != context.tenant_id
            or receipt.request_id != request_id
        ):
            raise ConsoleNotFound()
        return request, answer, receipt

    def _answer_results_or_fail(self) -> AnswerResultReader:
        if self._answer_results is None:
            raise _not_delivered("the tenant answer result store")
        return self._answer_results

    @staticmethod
    def _result_cursor(result_digest: str, offset: int) -> str:
        return base64.urlsafe_b64encode(f"{result_digest}:{offset}".encode()).decode().rstrip("=")

    @staticmethod
    def _result_offset(cursor: str | None, result_digest: str) -> int:
        if cursor is None:
            return 0
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            bound_digest, raw_offset = decoded.rsplit(":", 1)
            if (
                bound_digest != result_digest
                or not raw_offset.isascii()
                or not raw_offset.isdigit()
            ):
                raise ValueError
            return int(raw_offset)
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            raise ConsoleInvalidRequest(
                code="invalid_result_cursor",
                safe_message="The result page cursor is invalid.",
                recovery_action="correct_input",
                field="cursor",
            ) from error

    @staticmethod
    def _download_filename(title: str) -> str:
        ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        stem = _SAFE_FILENAME_CHARACTER.sub("-", ascii_title.lower()).strip("-")[:80]
        return f"{stem or 'answer-result'}.csv"

    @staticmethod
    def _csv(snapshot: AnswerResultSnapshot) -> bytes:
        def decimal_cell(value: str) -> str:
            try:
                number = Decimal(value)
            except InvalidOperation:
                number = Decimal("NaN")
            if not number.is_finite():
                raise ConsoleUnavailable(
                    code="answer_result_integrity_failure",
                    safe_message="The answer result could not be verified.",
                    recovery_action="contact_support",
                )
            return str(number)

        def text_cell(value: str) -> str:
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(
                ("\t", "\r", "\n")
            ):
                return f"'{value}"
            return value

        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(text_cell(column.name) for column in snapshot.columns)
        for row in snapshot.rows:
            writer.writerow(
                value.isoformat()
                if isinstance(value, datetime)
                else str(value)
                if isinstance(value, (Decimal, bool))
                else ""
                if value is None
                else decimal_cell(value)
                if isinstance(value, str) and column.value_type == "decimal"
                else text_cell(value)
                if isinstance(value, str)
                else value
                for column, value in zip(snapshot.columns, row, strict=True)
            )
        return stream.getvalue().encode("utf-8")

    def get_conversation(self, context: TrustedActorContext, request_id: str) -> ConversationView:
        self._authorize(
            context,
            ("requester", "data_architect", "data_owner", "policy_approver"),
        )
        request = self._visible_request(context, request_id)
        return self._conversation_view(request, self._conversation_entries(context, request_id))

    def get_clarified_outcome(
        self, context: TrustedActorContext, request_id: str
    ) -> ClarifiedOutcomeView:
        self._authorize(context, ("requester", "data_architect"))
        requests = self._require_requests(context)
        self._require_fulfillment()
        for request in requests:
            if request.request_id == request_id and request.requester_id == context.actor_id:
                outcome = self._clarified_outcome(context, request)
                if outcome is None:
                    raise ConsoleNotFound()
                return outcome
        raise ConsoleNotFound()

    def get_data_product(
        self, context: TrustedActorContext, data_product_id: str
    ) -> DataProductView:
        self._authorize(context, context.roles)
        if self._data_products is None:
            raise _not_delivered("a tenant-scoped data product read interface")
        permitted = [
            reference
            for reference in self._data_products.permitted_references(context.tenant_id)
            if reference.artifact_id == data_product_id
        ]
        if not permitted:
            raise ConsoleNotFound()
        # Policy snapshots accumulate over a tenant's history, so one product is
        # permitted at several versions. The newest is the one a reader means.
        newest = max(permitted, key=lambda reference: reference.version)
        return self._data_product_view(context, newest)

    def get_data_products(self, context: TrustedActorContext) -> DataProductsView:
        self._authorize(context, context.roles)
        if self._data_products is None:
            raise _not_delivered("a tenant-scoped data product read interface")
        newest_by_id: dict[str, ArtifactReference] = {}
        for reference in self._data_products.permitted_references(context.tenant_id):
            current = newest_by_id.get(reference.artifact_id)
            if current is None or reference.version > current.version:
                newest_by_id[reference.artifact_id] = reference
        return DataProductsView(
            products=tuple(
                self._data_product_view(context, reference)
                for reference in sorted(newest_by_id.values(), key=lambda item: item.artifact_id)
            )
        )

    def _data_product_view(
        self, context: TrustedActorContext, reference: ArtifactReference
    ) -> DataProductView:
        reader = self._product_publications
        if reader is None:
            definition = None
        else:
            definition = reader.definition_for_reference(
                tenant_id=context.tenant_id, product_ref=reference
            )
        if definition is None:
            return DataProductView(
                data_product_id=reference.artifact_id,
                version=reference.version,
                publication_status="pending",
            )
        if (
            definition.tenant_id != context.tenant_id
            or definition.product_id != reference.artifact_id
            or definition.product_revision != reference.version
        ):
            raise ConsoleNotFound()
        return DataProductView(
            data_product_id=reference.artifact_id,
            version=reference.version,
            publication_status="published",
            name=definition.name,
            description=definition.description,
            product_revision=definition.product_revision,
            generation=definition.generation,
            catalog_revision=definition.catalog_revision,
            namespace=definition.namespace,
            relation_name=definition.relation_name,
            column_count=len(definition.columns),
            source_count=len(definition.lineage_sources),
            freshness_observed_at=max(source.observed_at for source in definition.lineage_sources),
        )

    def get_runs(self, context: TrustedActorContext) -> RunsView:
        self._authorize(context, ("data_architect", "data_owner"))
        if self._runs is None:
            raise _not_delivered("a tenant-scoped run read interface")
        records = self._runs.list_runs(context.tenant_id)
        leased_runs, leased_runs_available = self._read_leased_runs(context.tenant_id)
        return RunsView(
            runs=tuple(
                RunView(
                    run_id=record.run_id,
                    contract_digest=record.contract_digest,
                    state=record.state,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
                for record in records
            ),
            leased_runs=leased_runs,
            leased_runs_available=leased_runs_available,
        )

    def _read_leased_runs(self, tenant_id: str) -> tuple[tuple[LeasedRunView, ...], bool]:
        """Read state-owned runs without letting that optional read fail the whole listing."""
        try:
            return self._leased_runs(tenant_id), True
        except ConsoleError:
            return (), False

    def _leased_runs(self, tenant_id: str) -> tuple[LeasedRunView, ...]:
        reader = self._run_lifecycle
        if reader is None:
            return ()
        return tuple(
            LeasedRunView(
                run_id=snapshot.run.run_id,
                contract_id=snapshot.run.intent.contract_id,
                contract_revision=snapshot.run.intent.contract_revision,
                trigger_reason=snapshot.run.intent.reason,
                window_starts_at=snapshot.run.intent.trigger_window.starts_at,
                window_ends_at=snapshot.run.intent.trigger_window.ends_at,
                status=snapshot.status,
                last_durable_boundary_ref=snapshot.last_durable_boundary_ref,
                attempts=tuple(
                    RunAttemptView(
                        attempt_number=item.claim.attempt_number,
                        epoch=item.claim.epoch,
                        worker_ref=item.claim.worker_id,
                        claimed_at=item.claim.claimed_at,
                        lease_expires_at=item.lease_expires_at,
                        lease_extensions=item.lease_extensions,
                        outcome=None if item.completion is None else item.completion.outcome,
                        failure_classification=(
                            None
                            if item.completion is None
                            else item.completion.failure_classification
                        ),
                        durable_boundary_ref=(
                            None
                            if item.completion is None
                            else item.completion.durable_boundary_ref
                        ),
                        completed_at=(
                            None if item.completion is None else item.completion.completed_at
                        ),
                    )
                    for item in snapshot.attempts
                ),
                observed_at=snapshot.observed_at,
            )
            for snapshot in self._guarded(lambda: reader.describe_runs(tenant_id))
        )

    def get_incidents(self, context: TrustedActorContext) -> IncidentsView:
        self._authorize(context, ("data_architect", "data_owner"))
        reader = self._incidents
        if reader is None:
            raise _not_delivered("a tenant-scoped incident read interface")
        records = self._guarded(lambda: reader.list_current(context.tenant_id))
        projected: list[IncidentView] = []
        for record in records:
            public_reference = mint_console_handle()
            self._operation_handles.store(
                OperationHandleRecord(
                    tenant_id=context.tenant_id,
                    console_handle=public_reference,
                    capability_kind="incident_recovery",
                    private_identity=record.incident_id,
                )
            )
            projected.append(self._incident_view(reader, record, public_reference))
        return IncidentsView(incidents=tuple(projected))

    def recover_incident(
        self,
        context: TrustedActorContext,
        incident_id: str,
        command: IncidentRecoveryCommand,
        *,
        idempotency_key: str,
    ) -> IncidentView:
        self._authorize(context, ("data_architect", "data_owner"))
        self._require_command_role(context, command.active_role)
        reader = self._incidents
        recovery = self._incident_recovery_commands
        if reader is None or recovery is None:
            raise _not_delivered("state-owned incident recovery wiring")
        handle = self._operation_handles.load(
            tenant_id=context.tenant_id, console_handle=incident_id
        )
        if handle is None or handle.capability_kind != "incident_recovery":
            raise ConsoleNotFound()
        normalized_reason = command.reason.strip()
        command_identity = digest(
            {
                "tenant_id": context.tenant_id,
                "actor_id": context.actor_id,
                "idempotency_key": idempotency_key,
                "incident_id": handle.private_identity,
                "expected_incident_revision": command.expected_revision,
                "action": command.action,
                "reason": normalized_reason,
            }
        )
        owner_command = RecoveryCommand(
            command_id=f"console-recovery-{command_identity}",
            tenant_id=context.tenant_id,
            incident_id=handle.private_identity,
            expected_incident_revision=command.expected_revision,
            action=command.action,
            actor_id=context.actor_id,
            reason=normalized_reason,
        )
        try:
            recovery.execute(owner_command)
            record = next(
                item
                for item in reader.list_current(context.tenant_id)
                if item.incident_id == handle.private_identity
            )
        except IncidentNotFoundError:
            raise ConsoleNotFound() from None
        except StopIteration:
            raise ConsoleNotFound() from None
        except StaleIncidentRevisionError:
            self._stale("The incident changed. Reload it and review the new revision.")
        except (RecoveryActionNotAllowedError, IncidentConflictError):
            raise ConsoleConflict(
                code="recovery_unavailable",
                safe_message="This recovery action is no longer available. Reload the incident.",
                recovery_action="reload",
            ) from None
        except IncidentIntegrityError:
            raise ConsoleUnavailable(
                code="downstream_integrity",
                safe_message="The governing service returned state the console cannot trust.",
                recovery_action="contact_support",
            ) from None
        except IncidentPersistenceError:
            raise ConsoleUnavailable(
                code="downstream_unavailable",
                safe_message="The governing service is temporarily unavailable.",
                recovery_action="retry",
            ) from None
        except ExternalEffectRecoveryUnavailableError:
            raise ConsoleUnavailable(
                code="downstream_unavailable",
                safe_message="The external effect could not be reconciled yet.",
                recovery_action="retry",
            ) from None
        return self._incident_view(reader, record, incident_id)

    def _incident_view(
        self,
        reader: TenantIncidentReader,
        record: IncidentRecord,
        public_reference: str,
    ) -> IncidentView:
        evidence = self._guarded(
            lambda: reader.list_recovery_evidence(record.tenant_id, record.incident_id)
        )
        recovery_recorded = any(item.incident_revision == record.revision for item in evidence)
        actions = () if recovery_recorded else _supported_incident_actions(record)
        return IncidentView(
            incident_id=public_reference,
            revision=record.revision,
            kind=record.kind,
            classification=record.classification,
            last_successful_stage=record.last_successful_stage,
            failed_stage=record.failed_stage,
            user_impact=record.user_impact,
            next_automatic_action=(None if recovery_recorded else record.next_automatic_action),
            allowed_operator_actions=actions,
            opened_at=record.opened_at,
            updated_at=record.updated_at,
            recovery_recorded=recovery_recorded,
        )

    def get_acquisition_receipts(self, context: TrustedActorContext) -> AcquisitionReceiptsView:
        # The same role set the fixture backend enforces. Governed mode must never be
        # more permissive than the demo that stands in for it.
        self._authorize(context, ("data_architect", "data_owner"))
        if self._acquisition_receipts is None:
            raise _not_delivered("a durable acquisition evidence store")
        receipts = self._acquisition_receipts.list_acquisition_receipts(context.tenant_id)
        return AcquisitionReceiptsView(
            receipts=tuple(self._acquisition_receipt_view(receipt) for receipt in receipts)
        )

    def run_acquisition_now(
        self, context: TrustedActorContext, command: AcquisitionRunNowCommand
    ) -> AcquisitionReceiptView:
        self._authorize(context, ("data_architect", "data_owner"))
        self._require_command_role(context, command.active_role)
        commands = self._acquisition_commands
        if commands is None:
            raise _not_delivered("command delegation to the acquisition application")
        result = self._guarded(
            lambda: commands.run_now(
                tenant_id=context.tenant_id,
                contract_ref=command.contract_ref,
                trigger_window=command.trigger_window,
                acquisition_mode=command.acquisition_mode,
            )
        )
        return self._acquisition_receipt_view(result.evidence)

    @staticmethod
    def _acquisition_receipt_view(receipt: AcquisitionEvidenceReceipt) -> AcquisitionReceiptView:
        return AcquisitionReceiptView(
            evidence_id=receipt.evidence_id,
            contract_ref=receipt.contract_ref,
            source_binding_ref=receipt.source_binding_ref,
            acquisition_mode=receipt.acquisition_mode,
            logical_object_refs=receipt.logical_object_refs,
            outcome=receipt.outcome,
            reason_codes=receipt.reason_codes,
            created_at=receipt.created_at,
        )

    def get_catalog_asset(self, context: TrustedActorContext, asset_ref: str) -> CatalogAssetView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        assets = self.get_catalog_assets(context).assets
        asset = next((item for item in assets if item.asset_ref == asset_ref), None)
        if asset is None:
            raise ConsoleNotFound()
        return asset

    def get_catalog_assets(self, context: TrustedActorContext) -> CatalogAssetsView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        publications = self._catalog_publications
        if publications is None:
            raise _not_delivered("a catalog asset read interface over published references")
        assets_by_identity: dict[str, CatalogAssetView] = {}
        for receipt in publications.list_publications(tenant_id=context.tenant_id):
            intent, _, _ = publications.load_publication(
                tenant_id=context.tenant_id, publication_id=receipt.publication_id
            )
            observations = publications.load_observations(
                tenant_id=context.tenant_id, operation_id=intent.operation_id
            )
            semantic_version, _ = publications.load_inputs(
                tenant_id=context.tenant_id, operation_id=intent.operation_id
            )
            semantic_classification_names = {
                classification.object_id: classification.name
                for classification in semantic_version.classifications
            }
            glossary_by_identity = {
                observation.logical_identity: observation
                for observation in observations
                if observation.object_kind == "glossary_term"
            }
            for identity, semantic_object in zip(
                intent.semantic_identities, intent.semantic_objects, strict=True
            ):
                if identity in assets_by_identity:
                    continue
                glossary = glossary_by_identity.get(identity)
                owner = (
                    glossary.normalized_payload.get("owner_ref") if glossary is not None else None
                )
                if owner is not None and not isinstance(owner, str):
                    raise ValueError("persisted catalog glossary owner is invalid")
                classifications = tuple(
                    sorted(
                        {
                            _catalog_classification_label(
                                classification,
                                contract_digest=intent.contract_digest,
                                semantic_names=semantic_classification_names,
                            )
                            for item in observations
                            if item.object_kind == "classification"
                            and item.normalized_payload.get("subject_ref") == identity
                            and isinstance(
                                classification := item.normalized_payload.get("classification_ref"),
                                str,
                            )
                        }
                    )
                )
                lineage_count = sum(
                    1
                    for item in observations
                    if item.object_kind == "lineage"
                    and identity
                    in (
                        item.normalized_payload.get("from_ref"),
                        item.normalized_payload.get("to_ref"),
                    )
                )
                asset_digest = digest({"tenant_id": context.tenant_id, "identity": identity})
                asset_ref = f"asset-{asset_digest[:24]}"
                assets_by_identity[identity] = CatalogAssetView(
                    asset_ref=asset_ref,
                    display_name=semantic_object.name,
                    definition=semantic_object.definition,
                    owner=owner,
                    classifications=classifications,
                    lineage_summary=(
                        "No governed lineage relationships are published."
                        if lineage_count == 0
                        else f"{lineage_count} governed lineage relationship"
                        f"{'s' if lineage_count != 1 else ''} published."
                    ),
                )
        return CatalogAssetsView(
            assets=tuple(sorted(assets_by_identity.values(), key=lambda item: item.display_name))
        )

    def get_dashboard(self, context: TrustedActorContext, dashboard_ref: str) -> DashboardView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        for dashboard in self.get_dashboards(context).dashboards:
            if dashboard.dashboard_ref == dashboard_ref:
                return dashboard
        raise ConsoleNotFound()

    def get_dashboards(self, context: TrustedActorContext) -> DashboardsView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        reader = self._dashboards
        if reader is None:
            raise _not_delivered("a provider-receipted BI control publication read interface")
        publications = self._guarded(lambda: reader.list_publications(context.tenant_id))
        if context.active_role == "requester":
            publications = self._requester_dashboard_publications(context, publications)
        return DashboardsView(
            dashboards=tuple(
                DashboardView(
                    dashboard_ref=self._dashboard_reference(
                        context.tenant_id, publication.dashboard_id, publication.version
                    ),
                    display_name=publication.title,
                    version=publication.version,
                    lifecycle_state=publication.lifecycle_state,
                    as_of=publication.as_of,
                    freshness=publication.freshness_disposition,
                    access_state=(
                        "active" if context.active_role == "requester" else "workspace_role"
                    ),
                    published_at=publication.published_at,
                    state=("ready" if publication.lifecycle_state == "active" else "blocked"),
                    summary=(
                        f"Revision {publication.version} was published by the managed BI provider "
                        f"on {publication.published_at:%B %-d, %Y}."
                        if publication.lifecycle_state == "active"
                        else f"Revision {publication.version} is archived."
                    ),
                )
                for publication in publications
            )
        )

    def _requester_dashboard_publications(
        self,
        context: TrustedActorContext,
        publications: tuple[DashboardPublication, ...],
    ) -> tuple[DashboardPublication, ...]:
        grants = self._access_grants
        commands = self._access_grant_commands
        principals = self._principals
        requests = self._requests
        if grants is None or commands is None or principals is None or requests is None:
            raise ConsoleNotFound()
        principal_ref = principals.principal_ref(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            role="requester",
        )
        if principal_ref is None:
            raise ConsoleNotFound()

        access_grants: list[AccessGrant] = []
        inbox = self._guarded(lambda: requests.list_inbox(context.tenant_id))
        for request in inbox:
            if (
                request.requester_id != context.actor_id
                or not isinstance(request.payload, DataAccessRequest)
                or request.payload.access_mode != "dashboard"
            ):
                continue
            grant = self._guarded(
                partial(grants.load_current_for_request, context.tenant_id, request.request_id)
            )
            if (
                grant is not None
                and grant.tenant_id == context.tenant_id
                and grant.request_id == request.request_id
                and grant.principal_ref == principal_ref
            ):
                access_grants.append(grant)

        visible: list[DashboardPublication] = []
        for publication in publications:
            if publication.lifecycle_state != "active":
                continue
            for grant in access_grants:
                if grant.data_product_version_ref != publication.data_product_version_ref:
                    continue
                try:
                    authorized = commands.authorize(
                        tenant_id=context.tenant_id,
                        grant_id=grant.grant_id,
                        principal_ref=principal_ref,
                        purpose=grant.purpose,
                        permission="dashboard",
                        product_version_ref=publication.data_product_version_ref,
                    )
                except AccessGrantDenied:
                    continue
                except Exception as error:
                    raise console_error_for(error) from error
                if (
                    authorized.grant_id == grant.grant_id
                    and authorized.tenant_id == context.tenant_id
                    and authorized.request_id == grant.request_id
                    and authorized.principal_ref == principal_ref
                    and authorized.purpose == grant.purpose
                    and authorized.data_product_version_ref == publication.data_product_version_ref
                    and authorized.state == "active"
                    and "dashboard" in authorized.permissions
                ):
                    visible.append(publication)
                    break
        return tuple(visible)

    @staticmethod
    def _dashboard_reference(tenant_id: str, dashboard_id: str, version: int) -> str:
        identity_digest = digest(
            {
                "domain": "heinzel-console-dashboard-reference-v1",
                "tenant_id": tenant_id,
                "dashboard_id": dashboard_id,
                "version": version,
            }
        )
        return f"dashboard-{identity_digest[:24]}"

    def get_evidence(self, context: TrustedActorContext, evidence_ref: str) -> EvidenceView:
        requests = self._require_requests(context)
        fulfillment = self._require_fulfillment()
        for request in requests:
            try:
                view = fulfillment.architect_view(
                    tenant_id=context.tenant_id,
                    request_id=request.request_id,
                    actor_id=context.actor_id,
                )
            except Exception as error:
                if classify_downstream_failure(error) == "not_visible":
                    continue
                raise console_error_for(error) from error
            for receipt in view.evidence:
                if receipt.evidence_id == evidence_ref:
                    return EvidenceView(
                        evidence_ref=receipt.evidence_id,
                        summary=(
                            f"Fulfillment outcome {receipt.outcome} resolved the request to "
                            f"{receipt.resulting_state.value}."
                        ),
                        occurred_at=receipt.created_at,
                        correlation_id=receipt.request_id,
                    )
        raise ConsoleNotFound()

    def get_operation(self, context: TrustedActorContext, operation_id: str) -> OperationView:
        record = self._operation_handles.load(
            tenant_id=context.tenant_id, console_handle=operation_id
        )
        if record is None:
            # An unknown handle and another tenant's handle report identically, so a
            # holder of a leaked handle cannot learn that it exists.
            raise ConsoleNotFound()
        if record.capability_kind == "warehouse_binding":
            # The lifecycle finished, so no private operation remains live. The
            # binding's own state is then the only truthful status to project.
            binding = self._require_warehouse_binding_reader(context)
            if binding is None or binding.binding_id != record.private_identity:
                raise ConsoleNotFound()
            return serialize_operation(
                console_handle=record.console_handle,
                status=PublicOperationStatus(
                    state=_BINDING_OPERATION_STATES[binding.lifecycle_state],
                    phase=binding.lifecycle_state.value,
                    summary=(f"The managed warehouse binding is {binding.lifecycle_state.value}."),
                    revision=binding.revision,
                ),
            )
        if record.capability_kind == "incident_recovery":
            # Incident handles are valid only at the incident recovery boundary. Answering
            # through the generic operation route would disclose that a probed handle exists.
            raise ConsoleNotFound()
        if record.capability_kind != "warehouse_lifecycle":
            raise _not_delivered("acquisition operation read wiring")
        reader = self._warehouse_operations
        if reader is None:
            raise _not_delivered("warehouse-control operation read wiring")
        operation = self._guarded(
            lambda: reader.load_operation(
                tenant_id=context.tenant_id, private_identity=record.private_identity
            )
        )
        if operation is None:
            raise ConsoleNotFound()
        return serialize_operation(
            console_handle=record.console_handle,
            status=self._operation_status(operation),
        )

    def confirm_warehouse_binding(
        self, context: TrustedActorContext, command: WarehouseBindingCommand
    ) -> OperationView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        commands = self._warehouse_commands
        if commands is None:
            raise _not_delivered("command delegation to warehouse-control")
        # A binding is created by this command, so warehouse-control has no prior
        # revision to guard here. The setup revision and digest the browser sends
        # describe a console projection, not an owning domain identity, and the
        # console must not treat its own projection as replay authority.
        confirmation = self._guarded(
            lambda: commands.confirm_binding(
                tenant_id=context.tenant_id,
                engine=command.engine,
                region=command.region,
                capacity=command.capacity,
            )
        )
        return self._confirmed_operation(context, confirmation)

    def submit_process_package(
        self, context: TrustedActorContext, command: ProcessPackageCommand
    ) -> OperationView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        setup = self.get_setup(context)
        self._require_current_revision(command.expected_revision, setup.revision)
        narrative = command.narrative_markdown.encode("utf-8")
        if hashlib.sha256(narrative).hexdigest() != command.package_digest:
            raise ConsoleInvalidRequest(
                code="process_package_digest_mismatch",
                safe_message="The Markdown narrative does not match its submitted digest.",
                recovery_action="correct_input",
                field="package_digest",
            )
        commands = self._process_package_commands
        if commands is None:
            raise _not_delivered("command delegation to contract process-package service")
        manifest = BusinessProcessManifest.model_validate(command.manifest.model_dump())
        receipt = self._guarded(
            lambda: commands.upload(
                context.tenant_id,
                narrative,
                command.media_type,
                manifest,
                context.actor_id,
            )
        )
        return OperationView(
            operation_id=f"process-package-{digest(receipt)[:32]}",
            revision=receipt.version,
            state="succeeded",
            phase="process_package_persisted",
            summary="Business process package saved.",
        )

    def decide_review(
        self, context: TrustedActorContext, review_id: str, command: DecisionCommand
    ) -> ReviewView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        reader = self._semantic_reviews
        commands = self._semantic_review_commands
        if reader is None or commands is None:
            raise _not_delivered("command delegation to semantic-registry review")
        bundle = self._guarded(lambda: reader.load_review_bundle(context.tenant_id, review_id))
        if command.expected_revision != bundle.revision:
            self._stale("The review bundle changed. Reload it before deciding.")
        if command.reviewed_digest != bundle.candidate_set_digest:
            self._stale("The reviewed candidate set changed. Reload it before deciding.")
        decision = _REVIEW_DECISIONS[command.decision]
        if decision is ReviewItemDecision.REVISE and command.revised_content is None:
            raise ConsoleInvalidRequest(
                code="review_revision_content_required",
                safe_message="Requesting changes needs the replacement wording.",
                recovery_action="correct_input",
                field="revised_content",
            )
        if decision is not ReviewItemDecision.REVISE and command.revised_content is not None:
            raise ConsoleInvalidRequest(
                code="review_revision_content_not_admitted",
                safe_message="Only a change request carries replacement wording.",
                recovery_action="correct_input",
                field="revised_content",
            )
        item_id = self._review_item_to_decide(bundle, command.review_item_id)
        updated = self._guarded(
            lambda: commands.decide_item(
                tenant_id=context.tenant_id,
                bundle_id=bundle.bundle_id,
                item_id=item_id,
                decision=decision,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
                revised_content=command.revised_content,
            )
        )
        return self._review_view(context, updated)

    @staticmethod
    def _review_item_to_decide(bundle: OntologyReviewBundle, named: str | None) -> str:
        """The one undecided item this decision applies to.

        A decision is applied to a single item by the owning transaction, so a bundle
        with several undecided items needs the command to say which. Naming an item
        that is already decided, or one this bundle never held, is refused rather
        than resolved to something nearby.
        """
        pending = tuple(item for item in bundle.items if item.status == "pending")
        if named is None:
            if len(pending) != 1:
                raise ConsoleInvalidRequest(
                    code="review_item_required",
                    safe_message=(
                        "This review has more than one undecided item and the command "
                        "names none of them."
                    ),
                    recovery_action="correct_input",
                    field="review_item_id",
                )
            return pending[0].item_id
        if named not in {item.item_id for item in pending}:
            raise ConsoleInvalidRequest(
                code="review_item_not_pending",
                safe_message="That review item is not awaiting a decision.",
                recovery_action="reload",
                field="review_item_id",
            )
        return named

    def decide_request(
        self, context: TrustedActorContext, request_id: str, command: DecisionCommand
    ) -> RequestDetailView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        self._require_command_role(context, command.active_role)
        # `DecisionCommand` also decides an ontology review, where these two carry
        # meaning. A fulfillment decision has no review item and no wording to revise,
        # so they are refused rather than ignored: silently dropping a field a caller
        # sent is how a caller comes to believe it was applied.
        if command.review_item_id is not None or command.revised_content is not None:
            raise ConsoleInvalidRequest(
                code="review_fields_not_admitted",
                safe_message="A fulfillment decision carries no review item or wording.",
                recovery_action="correct_input",
                field="review_item_id" if command.review_item_id is not None else "revised_content",
            )
        commands = self._require_fulfillment_commands()
        authority_ref = self._principal_ref(context)
        request = self._visible_request(context, request_id)
        self._require_current_revision(command.expected_revision, request.revision)
        if context.active_role == "data_architect":
            architect_view = self._architect_view(context, request_id)
            self._require_held_requirement(
                architect_view.proposals, authority_ref, command.reviewed_digest
            )
        else:
            reviewer_view = self._reviewer_view(context, request_id, authority_ref)
            if reviewer_view.requirement.subject_digest != command.reviewed_digest:
                self._stale("The reviewed proposal changed. Reload it before deciding.")
        self._guarded(
            lambda: commands.record_approval(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                authority_ref=authority_ref,
                subject_digest=command.reviewed_digest,
                decision=command.decision,
                expected_revision=command.expected_revision,
            )
        )
        return self.get_request_detail(context, request_id)

    def approve_product_intent(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ProductIntentApprovalCommand,
    ) -> ProductIntentApprovalView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        request = self._visible_request(context, request_id)
        self._require_current_revision(command.expected_revision, request.revision)
        candidate = self._product_intent_candidate(context, request)
        if candidate is None or digest(candidate.intent) != command.reviewed_digest:
            self._stale("The typed product intent changed. Reload it before approving.")
        if candidate.unresolved_constraints or any(
            not coverage.authorized for coverage in candidate.source_coverage
        ):
            raise ConsoleConflict(
                code="product_intent_unresolved",
                safe_message="Resolve every product intent requirement before approval.",
                recovery_action="correct_input",
            )
        commands = self._product_intent_commands
        if commands is None:
            raise _not_delivered("typed product intent approval")
        result = self._guarded(
            lambda: commands.approve(
                tenant_id=context.tenant_id,
                request_id=request_id,
                request_revision=request.revision,
                approved_by=context.actor_id,
                intent=candidate.intent,
                authority_refs=candidate.authority_refs,
            )
        )
        if isinstance(result, ProductIntentNoValidPlan):
            raise ConsoleConflict(
                code="product_intent_no_valid_plan",
                safe_message="The typed product intent has unresolved governed constraints.",
                recovery_action="correct_input",
            )
        return ProductIntentApprovalView(
            approval_id=result.approval_id,
            intent_revision=result.intent_revision,
            intent_digest=result.intent_digest,
            artifact_reference=self._artifact_view(result.artifact_reference),
            approved_by=result.approved_by,
            approved_at=result.approved_at,
        )

    def _preparation_commands(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ProposalPreparationCommand,
        action: PreparationAction,
    ) -> FulfillmentPreparationCommands:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        commands = self._fulfillment_preparation_commands
        if commands is None:
            raise _not_delivered("proposal preparation delegation to request-management")
        request = self._visible_request(context, request_id)
        self._require_current_revision(command.expected_revision, request.revision)
        view = self._architect_view(context, request_id)
        if action not in self._preparation_actions(request, view):
            raise ConsoleConflict(
                code="preparation_unavailable",
                safe_message="This preparation action is unavailable in the current request state.",
                recovery_action="reload",
            )
        return commands

    def clarify_request(
        self, context: TrustedActorContext, request_id: str, command: RequestClarificationCommand
    ) -> RequestDetailView:
        commands = self._preparation_commands(context, request_id, command, "clarify")
        self._guarded(
            lambda: commands.clarify_outcome(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
                restated_request=command.restated_request,
                in_scope_summary=command.in_scope_summary,
                out_of_scope_summary=command.out_of_scope_summary,
            )
        )
        return self._request_detail_view(context, self._visible_request(context, request_id))

    def prepare_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView:
        if self._fulfillment_preparation_commands is None:
            raise _not_delivered("request preparation through request-management")
        request = self._visible_request(context, request_id)
        action: PreparationAction = (
            "prepare_access" if isinstance(request.payload, DataAccessRequest) else "prepare_answer"
        )
        commands = self._preparation_commands(context, request_id, command, action)
        if action == "prepare_access":
            self._guarded(
                lambda: commands.propose_access(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id=context.actor_id,
                    expected_revision=command.expected_revision,
                )
            )
        else:
            self._guarded(
                lambda: commands.propose_answer(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id=context.actor_id,
                    expected_revision=command.expected_revision,
                )
            )
        return self._request_detail_view(context, self._visible_request(context, request_id))

    def submit_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView:
        commands = self._preparation_commands(context, request_id, command, "submit_proposal")
        self._guarded(
            lambda: commands.submit_proposal(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
            )
        )
        return self._request_detail_view(context, self._visible_request(context, request_id))

    def _preparation_actions(
        self, request: InboxRequest, view: ArchitectRequestView
    ) -> tuple[PreparationAction, ...]:
        if self._fulfillment_preparation_commands is None:
            return ()
        if not isinstance(request.payload, (DataAccessRequest, StakeholderQuestion)):
            return ()
        if request.state in (RequestState.SUBMITTED, RequestState.CLARIFYING):
            return ("clarify",)
        if view.dependencies:
            return ()
        if request.state is RequestState.INVESTIGATING and view.clarified_outcomes:
            return (
                "prepare_access"
                if isinstance(request.payload, DataAccessRequest)
                else "prepare_answer",
            )
        if request.state is RequestState.PROPOSED and view.proposals:
            return ("submit_proposal",)
        return ()

    def admit_request(
        self, context: TrustedActorContext, request_id: str, command: AdmissionCommand
    ) -> RequestDetailView:
        """Admit the reviewed proposal to execution through the owning transaction.

        The console checks only what it can see - the actor's role, the revision, and
        that the digest the browser displayed is still the proposal's. Whether every
        required approval is recorded against that exact proposal is the fulfillment
        service's judgement, not the console's, and it refuses the admission itself
        when one is missing.
        """
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        commands = self._require_fulfillment_commands()
        request = self._visible_request(context, request_id)
        self._require_current_revision(command.expected_revision, request.revision)
        detail = self._request_detail_view(context, request)
        if command.reviewed_digest != detail.proposal_digest:
            self._stale("The proposal changed. Reload it before admitting it.")
        if detail.admission is None or not detail.admission.available:
            # Declining to submit is not a second authority: the console can already
            # see the approvals it projected, and the fulfillment service classifies a
            # missing one as an authority failure, which the console must report as
            # not-visible. Sending it anyway would answer a plainly visible request
            # with a 404.
            raise ConsoleConflict(
                code="admission_unavailable",
                safe_message=(
                    detail.admission.blocking_reason
                    if detail.admission is not None and detail.admission.blocking_reason is not None
                    else "This proposal cannot be admitted yet."
                ),
                recovery_action="reload",
            )
        if request.state is not RequestState.EXECUTING:
            # An executing request was admitted already and only its delivery is pending, so
            # the same command retries delivery rather than recording a second admission.
            self._guarded(
                lambda: commands.admit(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id=context.actor_id,
                    expected_revision=command.expected_revision,
                )
            )
        admitted = self._visible_request(context, request_id)
        if (
            admitted.state is RequestState.EXECUTING
            and isinstance(admitted.payload, StakeholderQuestion)
            and self._fulfillment_execution_commands is not None
        ):
            execution_commands = self._fulfillment_execution_commands
            self._guarded(
                lambda: execution_commands.execute_answer(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id="heinzel-runtime",
                    expected_revision=admitted.revision,
                )
            )
        if admitted.state is RequestState.EXECUTING and isinstance(
            admitted.payload, DataAccessRequest
        ):
            access_commands = self._access_grant_commands
            delivery_commands = self._fulfillment_access_execution_commands
            fulfillment = self._require_fulfillment()
            if access_commands is None or delivery_commands is None:
                raise _not_delivered("access grant application and verified delivery")
            architect_view = self._guarded(
                lambda: fulfillment.architect_view(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id=context.actor_id,
                )
            )
            binding = (
                architect_view.admissions[-1].access_grant_binding
                if architect_view.admissions
                else None
            )
            if binding is None:
                raise ConsoleConflict(
                    code="access_admission_unavailable",
                    safe_message="The approved access terms are unavailable.",
                    recovery_action="reload",
                )
            self._guarded(
                lambda: access_commands.apply(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    grant_id=binding.grant_id,
                )
            )
            self._guarded(
                lambda: delivery_commands.execute_access(
                    tenant_id=context.tenant_id,
                    request_id=request_id,
                    actor_id="heinzel-access-control",
                    expected_revision=admitted.revision,
                )
            )
        # Re-project rather than describe the receipt: admission may have superseded
        # the proposal or produced `No Valid Plan` instead, and the request's own
        # state is what says which.
        return self._request_detail_view(context, self._visible_request(context, request_id))

    def create_request(
        self, context: TrustedActorContext, command: CreateRequestCommand
    ) -> RequesterRequestView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        commands = self._require_request_commands()
        request_input = self._guarded(lambda: request_intake_content(command)).payload
        if request_input.request_type == "stakeholder_question":
            created = self._guarded(
                lambda: commands.submit_question(
                    tenant_id=context.tenant_id,
                    requester_id=context.actor_id,
                    title=command.title,
                    request_digest=command.request_digest,
                    purpose=request_input.purpose,
                    question=request_input.question,
                )
            )
        else:
            if not self._data_access_intake_available:
                raise ConsoleUnavailable(
                    code=CAPABILITY_NOT_DELIVERED,
                    safe_message=(
                        "Data access requests are not available in this workspace yet because "
                        "grant application, expiry, and revocation are not delivered."
                    ),
                    recovery_action="none",
                )
            created = self._guarded(
                lambda: commands.submit_access_request(
                    tenant_id=context.tenant_id,
                    requester_id=context.actor_id,
                    title=command.title,
                    request_digest=command.request_digest,
                    purpose=request_input.purpose,
                    data_product_id=request_input.data_product_id,
                    requested_fields=tuple(request_input.requested_fields),
                    access_mode=request_input.access_mode,
                    expires_at=request_input.expires_at,
                )
            )
        kind = self._request_kind(created)
        if kind is None:
            raise _not_delivered("a console vocabulary for this request kind")
        return RequesterRequestView(
            request_id=created.request_id,
            kind=kind,
            state=_REQUEST_STATES[created.state],
            title=self._request_title(created),
            requested_outcome=created.payload.purpose,
            revision=created.revision,
            updated_at=created.updated_at,
            question=(
                created.payload.question
                if isinstance(created.payload, StakeholderQuestion)
                else None
            ),
        )

    def append_conversation_message(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ConversationMessageCommand,
    ) -> ConversationView:
        self._authorize(context, ("requester", "data_architect", "data_owner", "policy_approver"))
        self._require_command_role(context, command.active_role)
        commands = self._require_request_commands()
        request = self._visible_request(context, request_id)
        entries = self._conversation_entries(context, request_id)
        current = self._conversation_view(request, entries)
        self._require_current_revision(command.expected_revision, current.revision)
        if command.conversation_digest != current.conversation_digest:
            self._stale("The conversation changed. Reload it before posting a message.")
        self._guarded(
            lambda: commands.append_conversation(
                context.tenant_id,
                request_id,
                context.actor_id,
                command.body,
                expected_revision=command.expected_revision,
                author_role=context.active_role,
            )
        )
        return self._conversation_view(
            self._visible_request(context, request_id),
            self._conversation_entries(context, request_id),
        )

    def accept_clarified_outcome(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ClarifiedOutcomeAcceptanceCommand,
    ) -> ClarifiedOutcomeView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        commands = self._require_fulfillment_commands()
        authority_ref = self._principal_ref(context)
        outcome = self.get_clarified_outcome(context, request_id)
        self._require_current_revision(command.expected_revision, outcome.revision)
        if command.clarified_outcome_digest != outcome.statement_digest:
            self._stale("The clarified outcome changed. Reload it before responding.")
        self._guarded(
            lambda: commands.record_approval(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                authority_ref=authority_ref,
                subject_digest=command.clarified_outcome_digest,
                decision=command.decision,
                expected_revision=command.expected_revision,
            )
        )
        return self.get_clarified_outcome(context, request_id)

    def withdraw_request(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: RequestWithdrawalCommand,
    ) -> RequesterRequestView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        commands = self._require_fulfillment_commands()
        request = next(
            (
                item
                for item in self._require_requests(context)
                if item.request_id == request_id and item.requester_id == context.actor_id
            ),
            None,
        )
        if request is None:
            raise ConsoleNotFound()
        if request.state is RequestState.CANCELLED:
            # Withdrawing is idempotent: a retry after a committed cancellation reports the
            # withdrawn request rather than a failure the requester cannot act on.
            return self._requester_request(context, request)
        if request.state not in _WITHDRAWABLE_STATES:
            # Request-management would cancel an executing or delivered request; the console
            # refuses first, because withdrawing work already admitted is not the requester's call.
            raise ConsoleConflict(
                code="withdrawal_unavailable",
                safe_message="This request has already reached an outcome and cannot be withdrawn.",
                recovery_action="none",
            )
        self._require_current_revision(command.expected_revision, request.revision)
        self._guarded(
            lambda: commands.cancel(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
            )
        )
        withdrawn = next(
            item for item in self._require_requests(context) if item.request_id == request_id
        )
        return self._requester_request(context, withdrawn)

    def revoke_access(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: AccessRevocationCommand,
    ) -> AccessLifecycleView:
        self._authorize(context, ("requester", "data_architect"))
        self._require_command_role(context, command.active_role)
        reader = self._requests
        if reader is None:
            raise _not_delivered("request-management read wiring")
        request = self._guarded(lambda: reader.get(context.tenant_id, request_id))
        if context.active_role == "requester" and request.requester_id != context.actor_id:
            raise ConsoleNotFound()
        if not isinstance(request.payload, DataAccessRequest):
            raise ConsoleNotFound()
        commands = self._access_revocation_commands
        if commands is None:
            raise _not_delivered("access-control manual revocation")
        grant = self._guarded(
            lambda: commands.revoke_for_request(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
                reason=command.reason,
            )
        )
        return self._access_lifecycle_view(context, request, grant)

    def retry_operation(
        self,
        context: TrustedActorContext,
        operation_id: str,
        command: RetryOperationCommand,
    ) -> Never:
        # A governed operation projection never carries a retry token, because no
        # owning service issues one. Accepting a retry here would make the console
        # the replay authority for someone else's transaction.
        raise _not_delivered("a public retry authority issued by an owning service")

    def get_preview(self, context: TrustedActorContext, preview_ref: str) -> Never:
        raise _not_delivered("a governed preview render interface")

    def authorize_link(self, context: TrustedActorContext, link_ref: str) -> Never:
        raise _not_delivered("server-issued managed-service deep links")

    def reset(self, context: TrustedActorContext, command: ResetCommand) -> Never:
        # Governed-local state is authoritative; nothing may reset it from the console.
        raise _not_delivered("no reset exists outside demo mode")

    def _guarded[Value](self, read: Callable[[], Value]) -> Value:
        try:
            return read()
        except ConsoleError:
            raise
        except KeyError as error:
            # The owning services signal "not yours, or not there" with KeyError
            # from their tenant-qualified loads. Translating it here, at the one
            # boundary where that is the documented contract, keeps an accidental
            # KeyError deeper in console code from silently becoming a 404.
            raise ConsoleNotFound() from error
        except Exception as error:
            raise console_error_for(error) from error

    def _require_warehouse_binding_reader(
        self, context: TrustedActorContext
    ) -> WarehouseBinding | None:
        reader = self._warehouse_bindings
        if reader is None:
            raise _not_delivered("warehouse-control read wiring")
        return self._guarded(lambda: reader.current_binding(context.tenant_id))

    def _require_requests(self, context: TrustedActorContext) -> tuple[InboxRequest, ...]:
        reader = self._requests
        if reader is None:
            raise _not_delivered("request-management read wiring")
        return self._guarded(lambda: reader.list_inbox(context.tenant_id))

    def _require_fulfillment(self) -> FulfillmentViewReader:
        reader = self._fulfillment
        if reader is None:
            raise _not_delivered("request-management fulfillment read wiring")
        return reader

    def _require_request_commands(self) -> RequestIntakeCommands:
        commands = self._request_commands
        if commands is None:
            raise _not_delivered("command delegation to request-management")
        return commands

    def _require_fulfillment_commands(self) -> FulfillmentDecisionCommands:
        commands = self._fulfillment_commands
        if commands is None:
            raise _not_delivered("command delegation to the fulfillment service")
        return commands

    @staticmethod
    def _authorize(context: TrustedActorContext, allowed_roles: tuple[ActorRole, ...]) -> None:
        if context.active_role not in context.roles or context.active_role not in allowed_roles:
            raise ConsoleNotFound()

    @staticmethod
    def _require_command_role(context: TrustedActorContext, command_role: ActorRole) -> None:
        # The browser's declared role is a claim about the trusted context, never a
        # grant. A mismatch answers exactly as an unknown resource does.
        if command_role != context.active_role:
            raise ConsoleNotFound()

    @staticmethod
    def _stale(safe_message: str) -> Never:
        raise ConsoleConflict(
            code="stale_revision", safe_message=safe_message, recovery_action="reload"
        )

    def _require_current_revision(self, expected_revision: int, current_revision: int) -> None:
        if expected_revision != current_revision:
            self._stale("The resource changed. Reload it and review the new revision.")

    def _principal_ref(self, context: TrustedActorContext) -> str:
        directory = self._principals
        if directory is None:
            raise _not_delivered("a workspace principal directory")
        principal_ref = directory.principal_ref(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            role=context.active_role,
        )
        if principal_ref is None:
            # Reporting "you hold no principal here" identically to "no such
            # resource" keeps the answer non-enumerating.
            raise ConsoleNotFound()
        return principal_ref

    def _visible_request(self, context: TrustedActorContext, request_id: str) -> InboxRequest:
        commands = self._require_request_commands()
        request = self._guarded(lambda: commands.get(context.tenant_id, request_id))
        if context.active_role == "requester" and request.requester_id != context.actor_id:
            raise ConsoleNotFound()
        return request

    def _conversation_entries(
        self, context: TrustedActorContext, request_id: str
    ) -> tuple[ConversationEntry, ...]:
        commands = self._require_request_commands()
        return self._guarded(lambda: commands.list_conversation(context.tenant_id, request_id))

    @staticmethod
    def _conversation_digest(
        request_id: str, revision: int, entries: Iterable[ConversationEntry]
    ) -> str:
        """Digest the exact thread a reply is written against.

        The identity, the revision, and every entry id in order are what change when
        someone else posts first, so a reply written against a stale thread cannot be
        mistaken for one written against the current thread.
        """
        identifiers = tuple(entry.entry_id for entry in entries)
        payload = "\x00".join((request_id, str(revision), *identifiers))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _conversation_view(
        self, request: InboxRequest, entries: tuple[ConversationEntry, ...]
    ) -> ConversationView:
        messages = tuple(
            ConversationMessageView(
                message_id=entry.entry_id,
                author_label=self._actor_label(request.tenant_id, entry.actor_id),
                author_role=entry.author_role,
                body=entry.body,
                created_at=entry.created_at,
            )
            for entry in entries
        )
        return ConversationView(
            request_id=request.request_id,
            revision=request.revision,
            conversation_digest=self._conversation_digest(
                request.request_id, request.revision, entries
            ),
            messages=messages,
        )

    def _actor_label(self, tenant_id: str, actor_id: str) -> str:
        directory = self._actors
        if directory is None:
            return actor_id
        return directory.display_name(tenant_id=tenant_id, actor_id=actor_id) or actor_id

    def _authority_label(self, view: ArchitectRequestView, authority_ref: str) -> str | None:
        """A reviewer-readable name for an approving authority, or None when none is known.

        The requester's own principal is named through the deployment's actor directory. A role
        authority names its role. Any other principal stays unnamed rather than guessed, and the
        reference itself is always projected alongside for the owning service's check.
        """
        tenant_id = view.request.tenant_id
        requester_id = view.request.requester_id
        principals = self._principals
        if (
            principals is not None
            and self._actors is not None
            and principals.principal_ref(
                tenant_id=tenant_id, actor_id=requester_id, role="requester"
            )
            == authority_ref
        ):
            return self._actors.display_name(tenant_id=tenant_id, actor_id=requester_id)
        role_prefix = "role:"
        if authority_ref.startswith(role_prefix) and len(authority_ref) > len(role_prefix):
            return authority_ref[len(role_prefix) :].replace("_", " ").capitalize()
        return None

    def _architect_view(
        self, context: TrustedActorContext, request_id: str
    ) -> ArchitectRequestView:
        fulfillment = self._require_fulfillment()
        return self._guarded(
            lambda: fulfillment.architect_view(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
            )
        )

    def _reviewer_view(
        self,
        context: TrustedActorContext,
        request_id: str,
        authority_ref: str,
    ) -> ReviewerRequestView:
        fulfillment = self._require_fulfillment()
        return self._guarded(
            lambda: fulfillment.reviewer_view(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                authority_ref=authority_ref,
            )
        )

    def _reviewer_request_detail_view(
        self,
        context: TrustedActorContext,
        request: InboxRequest,
    ) -> RequestDetailView:
        kind = self._request_kind(request)
        if kind is None:
            raise _not_delivered("a console vocabulary for this request kind")
        authority_ref = self._principal_ref(context)
        view = self._reviewer_view(context, request.request_id, authority_ref)
        subject = view.subject
        if subject is None:
            raise ConsoleNotFound()
        satisfied = any(decision.decision == "approve" for decision in view.own_decisions)
        approval = ProposalApprovalView(
            authority_ref=authority_ref,
            authority_label=self._reviewer_authority_label(authority_ref),
            reason=view.requirement.reason_code,
            satisfied=satisfied,
        )
        authorization_summary = (
            "Your required approval is recorded."
            if satisfied
            else "Your approval is required for this proposal."
        )
        proposal, quality_summary, freshness = self._reviewer_proposal(
            request=request,
            subject=subject,
            approval=approval,
            authorization_summary=authorization_summary,
        )
        history = self._guarded(
            lambda: self._require_request_commands().list_transition_history(
                context.tenant_id, request.request_id
            )
        )
        return RequestDetailView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            purpose=request.payload.purpose,
            revision=request.revision,
            proposal_digest=view.requirement.subject_digest,
            proposal=proposal,
            conversation=self._conversation_view(
                request, self._conversation_entries(context, request.request_id)
            ),
            lifecycle=tuple(
                LifecycleEventView(
                    event_id=event.event_id,
                    state=_REQUEST_STATES[event.to_state],
                    summary=(
                        f"The request moved from {event.from_state.value} to "
                        f"{event.to_state.value}."
                    ),
                    occurred_at=event.created_at,
                )
                for event in history
            ),
            evidence=EvidenceContextView(
                freshness=freshness,
                quality_summary=quality_summary,
                lineage_summary="Only the governed proposal references are shown.",
                authorization_summary=authorization_summary,
            ),
            available_actions=(
                ("approve", "reject", "request_changes")
                if request.state is RequestState.AWAITING_APPROVAL and not view.own_decisions
                else ()
            ),
        )

    @classmethod
    def _reviewer_proposal(
        cls,
        *,
        request: InboxRequest,
        subject: StakeholderAnswerDraft | AccessScopePreview | DisclosureDenial,
        approval: ProposalApprovalView,
        authorization_summary: str,
    ) -> tuple[RequestProposalView, str, FreshnessState]:
        if isinstance(subject, AccessScopePreview):
            return (
                AccessPreviewProposalView(
                    kind="access_preview",
                    purpose=request.payload.purpose,
                    data_product_ref=f"artifact-{digest(subject.data_product_ref)}",
                    data_product_reference=cls._artifact_view(subject.data_product_ref),
                    effective_object_references=tuple(
                        cls._artifact_view(item) for item in subject.effective_object_refs
                    ),
                    access_mode=subject.access_mode,
                    requested_fields=subject.requested_fields,
                    effective_scope=subject.effective_fields,
                    exclusions=subject.excluded_scopes,
                    expires_at=subject.expires_at,
                    authority_summary=authorization_summary,
                    required_approvals=(approval,),
                ),
                (
                    f"{len(subject.effective_fields)} field(s) remain in scope and "
                    f"{len(subject.excluded_scopes)} scope(s) were excluded."
                ),
                "not_applicable",
            )
        if isinstance(subject, StakeholderAnswerDraft):
            return (
                StakeholderAnswerProposalView(
                    kind="stakeholder_answer",
                    purpose=request.payload.purpose,
                    candidate=subject.answer_text,
                    metric_version=(
                        "See exact metric references" if subject.metric_refs else "No metric cited"
                    ),
                    metric_references=tuple(
                        cls._artifact_view(item) for item in subject.metric_refs
                    ),
                    lineage_references=tuple(
                        cls._artifact_view(item) for item in subject.lineage_refs
                    ),
                    quality_references=tuple(
                        cls._artifact_view(item) for item in subject.material_quality_limitations
                    ),
                    as_of=subject.as_of,
                    freshness=subject.freshness_disposition,
                    lineage_summary="Only the governed references shown here are in scope.",
                    authorization_summary=authorization_summary,
                    required_approvals=(approval,),
                ),
                "Review the governed answer and cited references.",
                subject.freshness_disposition,
            )
        return (
            DisclosureDenialProposalView(
                kind="disclosure_denial",
                explanation=subject.requester_safe_explanation,
                reason_code=subject.reason_code,
                required_approvals=(approval,),
            ),
            "Review the proposed disclosure denial.",
            "not_applicable",
        )

    @staticmethod
    def _reviewer_authority_label(authority_ref: str) -> str:
        role_prefix = "role:"
        if authority_ref.startswith(role_prefix):
            return authority_ref[len(role_prefix) :].replace("_", " ").capitalize()
        return "Required approver"

    def _require_held_requirement(
        self,
        proposals: tuple[FulfillmentProposal, ...],
        authority_ref: str,
        reviewed_digest: str,
    ) -> None:
        if not proposals or not any(
            requirement.authority_ref == authority_ref
            and requirement.subject_digest == reviewed_digest
            for requirement in proposals[-1].required_approvals
        ):
            self._stale("The reviewed proposal changed. Reload it before deciding.")

    def _request_detail_view(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> RequestDetailView:
        kind = self._request_kind(request)
        if kind is None:
            raise _not_delivered("a console vocabulary for this request kind")
        commands = self._require_request_commands()
        view = self._architect_view(context, request.request_id)
        history = self._guarded(
            lambda: commands.list_transition_history(context.tenant_id, request.request_id)
        )
        proposal = view.proposals[-1] if view.proposals else None
        authority_ref = (
            self._principals.principal_ref(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                role=context.active_role,
            )
            if self._principals is not None
            else None
        )
        requirement = None
        if proposal is not None and authority_ref is not None:
            requirement = next(
                (
                    item
                    for item in proposal.required_approvals
                    if item.authority_ref == authority_ref
                ),
                None,
            )
        decided = any(
            approval.actor_id == context.actor_id and approval.authority_ref == authority_ref
            for approval in view.approvals
        )
        reviewed_proposal_digest = (
            requirement.subject_digest
            if requirement is not None
            else (
                digest(proposal.subject)
                if proposal is not None and context.active_role == "data_architect"
                else None
            )
        )
        return RequestDetailView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            purpose=request.payload.purpose,
            revision=request.revision,
            proposal_digest=reviewed_proposal_digest,
            proposal=self._proposal_view(
                view, lambda authority_ref: self._authority_label(view, authority_ref)
            ),
            conversation=self._conversation_view(
                request, self._conversation_entries(context, request.request_id)
            ),
            lifecycle=tuple(
                LifecycleEventView(
                    event_id=event.event_id,
                    state=_REQUEST_STATES[event.to_state],
                    summary=(
                        f"The request moved from {event.from_state.value} to "
                        f"{event.to_state.value}."
                    ),
                    occurred_at=event.created_at,
                )
                for event in history
            ),
            evidence=self._evidence_context(view),
            available_actions=(
                ("approve", "reject", "request_changes")
                if request.state is RequestState.AWAITING_APPROVAL
                and requirement is not None
                and not decided
                else ()
            ),
            admission=self._admission_view(
                request,
                proposal,
                view.approvals,
                delivery_retry_available=self._fulfillment_execution_commands is not None,
            ),
            preparation_actions=self._preparation_actions(request, view),
            preparation_notes=self._preparation_notes(view),
            question=(
                request.payload.question
                if isinstance(request.payload, StakeholderQuestion)
                else None
            ),
            product_intent=self._product_intent_review_view(context, request),
            access_lifecycle=self._access_lifecycle(context, request),
        )

    def _product_intent_candidate(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> ProductIntentCandidate | None:
        reader = self._product_intent_reviews
        if reader is None:
            return None
        candidate = self._guarded(
            lambda: reader.current_candidate(context.tenant_id, request.request_id)
        )
        if candidate is None:
            return None
        if (
            candidate.tenant_id != context.tenant_id
            or candidate.request_id != request.request_id
            or candidate.request_revision != request.revision
            or candidate.intent.request_id != request.request_id
        ):
            return None
        return candidate

    def _product_intent_review_view(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> ProductIntentReviewView | None:
        candidate = self._product_intent_candidate(context, request)
        if candidate is None:
            return None
        commands = self._product_intent_commands
        approvals = (
            ()
            if commands is None
            else self._guarded(
                lambda: commands.list_for_request(context.tenant_id, request.request_id)
            )
        )
        matching_approval = next(
            (
                approval
                for approval in reversed(approvals)
                if approval.request_revision == request.revision
                and approval.intent_digest == digest(candidate.intent)
            ),
            None,
        )
        intent = candidate.intent
        return ProductIntentReviewView(
            reviewed_digest=digest(intent),
            approved=matching_approval is not None,
            approved_intent_revision=(
                None if matching_approval is None else matching_approval.intent_revision
            ),
            title=intent.title,
            business_outcome=intent.business_outcome,
            source_coverage=tuple(
                ProductIntentSourceCoverageView(
                    source_ref=coverage.source_ref,
                    covered_fields=coverage.covered_fields,
                    authorized=coverage.authorized,
                )
                for coverage in candidate.source_coverage
            ),
            grain=intent.grain.keys,
            measures=tuple(
                ProductIntentMeasureView(
                    metric_ref=measure.metric_ref,
                    aggregation=measure.aggregation,
                )
                for measure in intent.measures
            ),
            dimensions=tuple(dimension.dimension_ref for dimension in intent.dimensions),
            filters=tuple(
                ProductIntentFilterView(
                    dimension_ref=filter_intent.dimension_ref,
                    operator=filter_intent.operator,
                    value=filter_intent.value,
                )
                for filter_intent in intent.filters
            ),
            freshness_seconds=intent.freshness.maximum_age_seconds,
            outputs=intent.delivery.outputs,
            unresolved_constraints=candidate.unresolved_constraints,
        )

    @staticmethod
    def _preparation_notes(view: ArchitectRequestView) -> tuple[str, ...]:
        notes = tuple(
            f"Blocked on {dependency.kind}: {dependency.reason_code}."
            for dependency in view.dependencies
        )
        if view.no_valid_plans:
            refusal = view.no_valid_plans[-1]
            if refusal.resulting_request_revision == view.request.revision:
                notes += tuple(f"No Valid Plan: {reason}." for reason in refusal.reason_codes)
                notes += tuple(f"Required change: {change}" for change in refusal.smallest_changes)
        return notes

    @staticmethod
    def _artifact_view(reference: ArtifactReference) -> ArtifactReferenceView:
        return ArtifactReferenceView(
            artifact_id=reference.artifact_id, version=reference.version, digest=reference.digest
        )

    @classmethod
    def _dataset_view(cls, reference: ArtifactReference) -> DatasetEvidenceView:
        # The display key binds the entire reference; it is never a catalog lookup key.
        return DatasetEvidenceView(
            dataset_ref=f"artifact-{digest(reference)}",
            display_name=f"{reference.artifact_id} (version {reference.version})",
            artifact_reference=cls._artifact_view(reference),
        )

    @staticmethod
    def _approval_views(
        view: ArchitectRequestView,
        label_for: Callable[[str], str | None] = lambda _: None,
    ) -> tuple[ProposalApprovalView, ...]:
        if not view.proposals:
            return ()
        proposal = view.proposals[-1]
        receipts: tuple[FulfillmentAdmissionReceipt | DenialDispositionReceipt, ...] = (
            *view.admissions,
            *view.denials,
        )
        disposition = next(
            (
                receipt
                for receipt in receipts
                if receipt.tenant_id == view.request.tenant_id
                and receipt.request_id == view.request.request_id
                and receipt.proposal_id == proposal.proposal_id
                and receipt.proposal_revision == proposal.revision
                and receipt.proposal_digest == digest(proposal)
                and receipt.resulting_request_revision <= view.request.revision
            ),
            None,
        )
        approval_revision = (
            view.request.revision if disposition is None else disposition.source_request_revision
        )
        return tuple(
            ProposalApprovalView(
                authority_ref=requirement.authority_ref,
                authority_label=label_for(requirement.authority_ref),
                reason=requirement.reason_code,
                satisfied=sum(
                    1
                    for approval in view.approvals
                    if approval.tenant_id == view.request.tenant_id
                    and approval.request_id == view.request.request_id
                    and approval.request_revision == approval_revision
                    and (disposition is None or approval.approval_id in disposition.approval_ids)
                    and approval.proposal_id == proposal.proposal_id
                    and approval.proposal_revision == proposal.revision
                    and approval.proposal_digest == digest(proposal)
                    and approval.authority_ref == requirement.authority_ref
                    and approval.subject_digest == requirement.subject_digest
                    and approval.decision == "approve"
                )
                == 1,
            )
            for requirement in proposal.required_approvals
        )

    @classmethod
    def _proposal_view(
        cls,
        view: ArchitectRequestView,
        label_for: Callable[[str], str | None] = lambda _: None,
    ) -> RequestProposalView | None:
        if not view.proposals:
            return None
        proposal = view.proposals[-1]
        approvals = cls._approval_views(view, label_for)
        evidence = cls._evidence_context(view)
        subject = proposal.subject
        if isinstance(subject, StakeholderAnswerDraft):
            return StakeholderAnswerProposalView(
                kind="stakeholder_answer",
                purpose=view.request.payload.purpose,
                candidate=subject.answer_text,
                metric_version="See exact metric references"
                if subject.metric_refs
                else "No metric cited",
                metric_references=tuple(cls._artifact_view(item) for item in subject.metric_refs),
                lineage_references=tuple(cls._artifact_view(item) for item in subject.lineage_refs),
                quality_references=tuple(
                    cls._artifact_view(item) for item in subject.material_quality_limitations
                ),
                as_of=subject.as_of,
                freshness=subject.freshness_disposition,
                datasets=evidence.datasets,
                lineage_summary=evidence.lineage_summary,
                authorization_summary=evidence.authorization_summary,
                required_approvals=approvals,
            )
        if isinstance(subject, AccessScopePreview):
            return AccessPreviewProposalView(
                kind="access_preview",
                purpose=view.request.payload.purpose,
                data_product_ref=f"artifact-{digest(subject.data_product_ref)}",
                data_product_reference=cls._artifact_view(subject.data_product_ref),
                effective_object_references=tuple(
                    cls._artifact_view(item) for item in subject.effective_object_refs
                ),
                access_mode=subject.access_mode,
                requested_fields=subject.requested_fields,
                effective_scope=subject.effective_fields,
                exclusions=subject.excluded_scopes,
                expires_at=subject.expires_at,
                authority_summary=evidence.authorization_summary,
                required_approvals=approvals,
            )
        return DisclosureDenialProposalView(
            kind="disclosure_denial",
            explanation=subject.requester_safe_explanation,
            reason_code=subject.reason_code,
            required_approvals=approvals,
        )

    @staticmethod
    def _admission_view(
        request: InboxRequest,
        proposal: FulfillmentProposal | None,
        approvals: tuple[FulfillmentApprovalBinding, ...],
        delivery_retry_available: bool = False,
    ) -> AdmissionView | None:
        """Whether the fulfillment service would accept an admission now.

        This mirrors the bindings `FulfillmentService.admit` requires of each
        approval, and it has to mirror all of them. Matching on the authority alone
        left the action advertised after any revision bump - posting a clarification
        message is enough - and the service then refused with an authority failure,
        which the console must report as not-visible. The architect was offered a
        command that answered `404` on a request they were looking at.

        The one binding not mirrored is the service's role check: whether the actor
        who recorded an approval still holds that authority is the resolver's
        judgement, and duplicating it here would make the console a second authority
        over someone else's transaction.
        """
        if (
            request.state is RequestState.EXECUTING
            and proposal is not None
            and isinstance(proposal.subject, StakeholderAnswerDraft)
            and delivery_retry_available
        ):
            # Admission committed but delivery did not. Without this the request sat in
            # `executing` with no action at all: admission refuses a request past approval, and
            # nothing else reaches the delivery step again.
            return AdmissionView(available=True, pending_delivery=True)
        if request.state is not RequestState.AWAITING_APPROVAL or proposal is None:
            return None
        proposal_digest = digest(proposal)
        outstanding = tuple(
            requirement
            for requirement in proposal.required_approvals
            if sum(
                1
                for approval in approvals
                if approval.request_revision == request.revision
                and approval.proposal_id == proposal.proposal_id
                and approval.proposal_revision == proposal.revision
                and approval.proposal_digest == proposal_digest
                and approval.authority_ref == requirement.authority_ref
                and approval.subject_digest == requirement.subject_digest
                and approval.decision == "approve"
            )
            != 1
        )
        if not outstanding:
            return AdmissionView(available=True)
        return AdmissionView(
            available=False,
            blocking_reason=(
                f"{len(outstanding)} of {len(proposal.required_approvals)} required "
                "approvals are not recorded against this revision of the proposal."
            ),
        )

    @classmethod
    def _evidence_context(cls, view: ArchitectRequestView) -> EvidenceContextView:
        proposal = view.proposals[-1] if view.proposals else None
        freshness: FreshnessState = "unknown"
        as_of = None
        quality_summary = "No proposal has been compiled for this request yet."
        lineage_summary = "No governed lineage has been cited yet."
        if proposal is not None:
            subject = proposal.subject
            if isinstance(subject, StakeholderAnswerDraft):
                freshness = subject.freshness_disposition
                as_of = subject.as_of
                quality_summary = (
                    f"{len(subject.material_quality_limitations)} material quality "
                    f"limitation(s) were cited."
                )
                lineage_summary = (
                    f"{len(subject.lineage_refs)} lineage reference(s) and "
                    f"{len(subject.governed_dataset_refs)} governed dataset(s) were cited."
                )
            elif isinstance(subject, AccessScopePreview):
                freshness = "not_applicable"
                quality_summary = (
                    f"{len(subject.effective_fields)} field(s) remain in scope and "
                    f"{len(subject.excluded_scopes)} scope(s) were excluded."
                )
                lineage_summary = (
                    f"{len(subject.effective_object_refs)} governed object(s) were cited."
                )
            else:
                freshness = "not_applicable"
                quality_summary = "The compiled outcome is a disclosure denial."
                lineage_summary = "A denial cites no governed lineage."
        required = 0 if proposal is None else len(proposal.required_approvals)
        answer = (
            proposal.subject
            if proposal is not None and isinstance(proposal.subject, StakeholderAnswerDraft)
            else None
        )
        return EvidenceContextView(
            datasets=()
            if answer is None
            else tuple(cls._dataset_view(item) for item in answer.governed_dataset_refs),
            metric_references=()
            if answer is None
            else tuple(cls._artifact_view(item) for item in answer.metric_refs),
            as_of=as_of,
            freshness=freshness,
            quality_summary=quality_summary,
            lineage_summary=lineage_summary,
            authorization_summary=(
                f"{sum(item.satisfied for item in cls._approval_views(view))} of {required} "
                "required approval(s) are recorded for this proposal."
            ),
            evidence_refs=tuple(receipt.evidence_id for receipt in view.evidence),
        )

    def _confirmed_operation(
        self, context: TrustedActorContext, confirmation: WarehouseConfirmation
    ) -> OperationView:
        classification = confirmation.failure_classification
        failure = None
        state = _BINDING_OPERATION_STATES[confirmation.lifecycle_state]
        if classification is not None:
            failure = OperationFailureView(
                code=classification.value,
                classification=(
                    "transient"
                    if classification in _TRANSIENT_CLASSIFICATIONS
                    else "unknown"
                    if classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME
                    else "permanent"
                ),
                safe_message="The warehouse provider reported a governed failure.",
            )
            # A transient or ambiguous provider failure leaves the lifecycle
            # unresolved. Recording it as a terminal failure would turn a retryable
            # condition into a verdict about the provider.
            if failure.classification == "transient":
                state = "running"
            elif failure.classification == "unknown":
                state = "outcome_unknown"
            else:
                state = "failed"
        console_handle = mint_console_handle()
        self._operation_handles.store(
            OperationHandleRecord(
                tenant_id=context.tenant_id,
                console_handle=console_handle,
                capability_kind=(
                    "warehouse_binding"
                    if confirmation.operation_identity is None
                    else "warehouse_lifecycle"
                ),
                private_identity=(
                    confirmation.binding_id
                    if confirmation.operation_identity is None
                    else confirmation.operation_identity.encode()
                ),
            )
        )
        return serialize_operation(
            console_handle=console_handle,
            status=PublicOperationStatus(
                state=state,
                phase=confirmation.lifecycle_state.value,
                summary=(f"The managed warehouse binding is {confirmation.lifecycle_state.value}."),
                revision=confirmation.binding_revision,
                failure=failure,
            ),
        )

    def _warehouse_capability(self, context: TrustedActorContext) -> tuple[CapabilityState, str]:
        if self._warehouse_bindings is None:
            return "not_delivered", "No warehouse-control read interface is wired."
        try:
            binding = self._warehouse_bindings.current_binding(context.tenant_id)
        except Exception as error:
            if classify_downstream_failure(error) != "unavailable":
                raise
            return "degraded", "The warehouse control service is temporarily unreadable."
        if binding is None:
            return "blocked", "This workspace has not confirmed a warehouse binding."
        return (
            _BINDING_STATES[binding.lifecycle_state],
            f"The managed warehouse binding is {binding.lifecycle_state.value}.",
        )

    def _catalog_capability(self, context: TrustedActorContext) -> tuple[CapabilityState, str]:
        if self._catalog_bindings is None:
            return "not_delivered", "No catalog-control read interface is wired."
        try:
            binding = self._catalog_bindings.current_binding(context.tenant_id)
        except Exception as error:
            if classify_downstream_failure(error) != "unavailable":
                raise
            return "degraded", "The catalog control service is temporarily unreadable."
        if binding is None:
            return "blocked", "This workspace has not confirmed a catalog binding."
        if binding.lifecycle_state is not CatalogBindingState.READY:
            return "blocked", f"The managed catalog binding is {binding.lifecycle_state.value}."
        if self._catalog_search_health is None:
            return "degraded", "The managed catalog is ready, but search health is not configured."
        try:
            search_ready = self._catalog_search_health.search_ready(context.tenant_id)
        except Exception:
            return "degraded", "The managed catalog search service is temporarily unreadable."
        if not search_ready:
            return "degraded", "The managed catalog is ready, but search is unhealthy."
        return "ready", "The managed catalog binding and search projection are ready."

    def _request_capability(self, context: TrustedActorContext) -> tuple[CapabilityState, str]:
        if self._requests is None or self._fulfillment is None:
            return "not_delivered", "No request-management read interface is wired."
        try:
            self._requests.list_inbox(context.tenant_id)
        except Exception as error:
            if classify_downstream_failure(error) != "unavailable":
                raise
            return "degraded", "The request management service is temporarily unreadable."
        return "ready", "Requests and fulfillment projections are read from their owning service."

    @staticmethod
    def _workspace_state(capabilities: list[CapabilityView]) -> WorkspaceState:
        warehouse = next(
            (item for item in capabilities if item.capability_id == "warehouse-binding"), None
        )
        if warehouse is not None and warehouse.state == "ready":
            return "active"
        return "setup"

    @staticmethod
    def _stage_states(
        binding: WarehouseBinding | None,
        catalog_binding: CatalogBinding | None,
        *,
        process_package_available: bool,
        process_package_recorded: bool,
    ) -> dict[SetupStage, SetupStageState]:
        foundation: SetupStageState = (
            "complete"
            if binding is not None and binding.lifecycle_state is WarehouseBindingState.READY
            else "current"
        )
        managed: SetupStageState = "not_started" if foundation != "complete" else "current"
        if catalog_binding is None:
            managed = "blocked"
        elif catalog_binding.lifecycle_state is CatalogBindingState.READY:
            managed = "complete"
        states: dict[SetupStage, SetupStageState] = {
            "foundation": foundation,
            "managed_services": managed,
        }
        for stage in _UNDELIVERED_STAGES:
            states[stage] = "blocked"
        prerequisites_ready = foundation == "complete" and managed == "complete"
        states["business_process"] = (
            "complete"
            if process_package_recorded
            else "current"
            if process_package_available and prerequisites_ready
            else "blocked"
        )
        return states

    def _review_view(
        self, context: TrustedActorContext, bundle: OntologyReviewBundle
    ) -> ReviewView:
        items = tuple(
            ReviewItemView(label=item.item_id, value=item.status, material_change=False)
            for item in bundle.items
        )
        return ReviewView(
            review_id=bundle.bundle_id,
            kind="meaning",
            title="Meaning review",
            summary=f"The ontology review bundle is {bundle.status}.",
            revision=bundle.revision,
            reviewed_digest=bundle.candidate_set_digest,
            sections=(
                ReviewSectionView(
                    section_id="review-items",
                    title="Review items",
                    summary=f"{len(items)} item(s) await a recorded decision.",
                    items=items,
                ),
            ),
            can_decide=context.active_role == "data_architect" and bundle.status == "open",
        )

    @staticmethod
    def _request_kind(request: InboxRequest) -> RequestKind | None:
        if isinstance(request.payload, StakeholderQuestion):
            return "stakeholder_question"
        if isinstance(request.payload, DataAccessRequest):
            return "data_access"
        # Schema-semantic and data-product change requests have no console vocabulary,
        # so they are never projected rather than being relabelled as something else.
        return None

    @staticmethod
    def _request_title(request: InboxRequest) -> str:
        if request.title is not None:
            return request.title
        if isinstance(request.payload, StakeholderQuestion):
            return request.payload.question
        if isinstance(request.payload, DataAccessRequest):
            return request.payload.data_product_id
        return request.request_id

    @staticmethod
    def _request_risk(request: InboxRequest) -> RiskLevel:
        # No owning service publishes a risk verdict. This is a console presentation
        # classification by request kind and carries no service authority.
        return "high" if isinstance(request.payload, DataAccessRequest) else "medium"

    def _inbox_item(self, request: InboxRequest) -> InboxItemView | None:
        kind = self._request_kind(request)
        if kind is None:
            return None
        return InboxItemView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            purpose=request.payload.purpose,
            risk=self._request_risk(request),
            blocked_reason=_BLOCKED_REASONS.get(request.state),
        )

    def _service_requester_view(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> ServiceRequesterRequestView:
        fulfillment = self._require_fulfillment()
        return self._guarded(
            lambda: fulfillment.requester_view(
                tenant_id=context.tenant_id,
                request_id=request.request_id,
                actor_id=context.actor_id,
            )
        )

    def _requester_request(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> RequesterRequestView:
        view = self._service_requester_view(context, request)
        kind = self._request_kind(request)
        if kind is None:
            raise _not_delivered("a console vocabulary for this request kind")
        access_lifecycle = self._access_lifecycle(context, request)
        return RequesterRequestView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            requested_outcome=request.payload.purpose,
            revision=request.revision,
            updated_at=request.updated_at,
            result_page_available=self._result_page_available(context, request),
            own_decisions=tuple(
                OwnDecisionView(
                    decision=self._decision(decision.decision),
                    subject_label=decision.lifecycle,
                    created_at=decision.created_at,
                )
                for decision in view.own_decisions
                if decision.decision in _DECISIONS
            ),
            clarified_outcome=self._outcome_view(request, view),
            question=(
                request.payload.question
                if isinstance(request.payload, StakeholderQuestion)
                else None
            ),
            denial_explanation=view.denial_explanation,
            no_valid_plan_explanation=view.no_valid_plan_explanation,
            delivered_answer=(
                None
                if view.delivered_answer is None or view.delivery_id is None
                else DeliveredAnswerView(
                    answer_text=view.delivered_answer.answer_text,
                    as_of=view.delivered_answer.as_of,
                    freshness=view.delivered_answer.freshness_disposition,
                    datasets=tuple(
                        self._artifact_view(item)
                        for item in view.delivered_answer.governed_dataset_refs
                    ),
                    metrics=tuple(
                        self._artifact_view(item) for item in view.delivered_answer.metric_refs
                    ),
                    lineage=tuple(
                        self._artifact_view(item) for item in view.delivered_answer.lineage_refs
                    ),
                    quality_limitations=tuple(
                        self._artifact_view(item)
                        for item in view.delivered_answer.material_quality_limitations
                    ),
                    delivery_ref=view.delivery_id,
                )
            ),
            delivered_access=(
                None
                if view.delivered_access is None
                or access_lifecycle is None
                or access_lifecycle.state != "active"
                else DeliveredAccessView(
                    access_mode=view.delivered_access.access_mode,
                    fields=view.delivered_access.fields,
                    effective_at=view.delivered_access.effective_at,
                    expires_at=view.delivered_access.expires_at,
                    permissions=view.delivered_access.permissions,
                )
            ),
            access_lifecycle=access_lifecycle,
        )

    def _access_lifecycle(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> AccessLifecycleView | None:
        reader = self._access_grants
        if reader is None or not isinstance(request.payload, DataAccessRequest):
            return None
        grant = self._guarded(
            lambda: reader.load_current_for_request(context.tenant_id, request.request_id)
        )
        if grant is None:
            return None
        return self._access_lifecycle_view(context, request, grant)

    def _access_lifecycle_view(
        self,
        context: TrustedActorContext,
        request: InboxRequest,
        grant: AccessGrant,
    ) -> AccessLifecycleView:
        if grant.tenant_id != context.tenant_id or grant.request_id != request.request_id:
            raise console_error_for(
                AccessGrantIntegrityError("access grant returned a different scope")
            )
        title, summary = self._access_lifecycle_copy(grant)
        return AccessLifecycleView(
            state=grant.state,
            title=title,
            summary=summary,
            effective_at=grant.effective_at,
            expires_at=grant.expires_at,
            revision=grant.revision,
            can_revoke=grant.state == "active",
        )

    @staticmethod
    def _access_lifecycle_copy(grant: AccessGrant) -> tuple[str, str]:
        if grant.state == "pending":
            return (
                "Access is being set up",
                "Your approved access is being applied to each destination.",
            )
        if grant.state == "active":
            return (
                "Access is active",
                f"Your approved access is available until {grant.expires_at.isoformat()}.",
            )
        if grant.state == "expired":
            return (
                "Access has expired",
                "This access can no longer be used while destination cleanup finishes.",
            )
        if grant.state == "revocation_pending":
            return (
                "Access removal is in progress",
                "Your access is already unavailable while destination cleanup finishes.",
            )
        if grant.state == "revoked":
            return (
                "Access has been removed",
                "This access can no longer be used.",
            )
        action = "setup" if grant.failed_action == "apply" else "removal"
        return (
            f"Access {action} needs attention",
            "Access remains unavailable while an administrator resolves the failure.",
        )

    def _result_page_available(self, context: TrustedActorContext, request: InboxRequest) -> bool:
        if (
            request.state is not RequestState.DELIVERED
            or self._verified_answers is None
            or self._answer_results is None
        ):
            return False
        try:
            _, answer, receipt = self._answer_result_authority(context, request.request_id)
        except ConsoleNotFound:
            return False
        return receipt.outcome == "succeeded" and answer.result_ref is not None

    def _clarified_outcome(
        self, context: TrustedActorContext, request: InboxRequest
    ) -> ClarifiedOutcomeView | None:
        return self._outcome_view(request, self._service_requester_view(context, request))

    @staticmethod
    def _outcome_view(
        request: InboxRequest, view: ServiceRequesterRequestView
    ) -> ClarifiedOutcomeView | None:
        if not view.clarified_outcomes:
            return None
        statement = view.clarified_outcomes[-1]
        accepted = any(
            decision.lifecycle == "fulfillment" and decision.decision == "approve"
            for decision in view.own_decisions
        )
        return ClarifiedOutcomeView(
            request_id=statement.request_id,
            # The acceptance command is written against the request's live revision,
            # which is what the fulfillment service compares, not the revision the
            # statement was drafted at.
            revision=view.revision,
            statement_digest=digest(statement),
            restated_request=statement.restated_request,
            purpose=request.payload.purpose,
            in_scope_summary=statement.in_scope_summary,
            out_of_scope_summary=statement.out_of_scope_summary,
            accepted=accepted,
        )

    @staticmethod
    def _decision(decision: str) -> Decision:
        if decision == "approve":
            return "approve"
        if decision == "reject":
            return "reject"
        return "request_changes"

    @staticmethod
    def _operation_status(operation: PrivateWarehouseOperation) -> PublicOperationStatus:
        classification = operation.failure_classification
        failure = None
        if classification is not None:
            failure = OperationFailureView(
                code=classification.value,
                classification=(
                    "transient"
                    if classification in _TRANSIENT_CLASSIFICATIONS
                    else "unknown"
                    if classification is WarehouseFailureClassification.AMBIGUOUS_OUTCOME
                    else "permanent"
                ),
                safe_message="The warehouse operation reported a governed failure.",
            )
        return PublicOperationStatus(
            state=_OPERATION_STATES[operation.status],
            phase=operation.phase.value,
            summary=(
                f"The warehouse {operation.operation_kind.value} operation is "
                f"{operation.status.value}."
            ),
            revision=operation.binding_revision,
            failure=failure,
        )


__all__ = [
    "CAPABILITY_NOT_DELIVERED",
    "GovernedConsoleBackend",
]
