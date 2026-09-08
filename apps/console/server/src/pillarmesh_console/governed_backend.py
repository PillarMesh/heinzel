"""Governed-local console read projections.

`GovernedConsoleBackend` composes immutable read models from the owning services'
public interfaces. A capability with no real owning implementation reports
`not_delivered` and its route fails closed; a failing repository reports a typed
degraded or unavailable state. Neither path substitutes demo content, because a
console that answers with fixtures after a real read fails is worse than one that
answers with nothing.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Never

from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    ArchitectRequestView,
    ConversationEntry,
    FulfillmentApprovalBinding,
    FulfillmentProposal,
    InboxRequest,
    RequestState,
)
from pillarmesh_request_management import RequesterRequestView as ServiceRequesterRequestView
from pillarmesh_request_management.fulfillment_models import (
    AccessScopePreview,
    StakeholderAnswerDraft,
)
from pillarmesh_request_management.models import DataAccessRequest, StakeholderQuestion
from pillarmesh_semantic_registry import OntologyReviewBundle
from pillarmesh_semantic_registry.review import ReviewItemDecision
from pillarmesh_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseOperationStatus,
)

from .auth import TrustedActorContext
from .contracts import (
    AcquisitionReceiptsView,
    AcquisitionReceiptView,
    ActorDisplayView,
    ActorRole,
    AdmissionCommand,
    AdmissionView,
    CapabilityState,
    CapabilityView,
    CatalogAssetView,
    ClarifiedOutcomeAcceptanceCommand,
    ClarifiedOutcomeView,
    ConversationMessageCommand,
    ConversationMessageView,
    ConversationView,
    CreateRequestCommand,
    DashboardView,
    DataProductView,
    Decision,
    DecisionCommand,
    DisplayReferenceView,
    EvidenceContextView,
    EvidenceView,
    FreshnessState,
    InboxItemView,
    InboxView,
    LifecycleEventView,
    OperationFailureView,
    OperationState,
    OperationView,
    OwnDecisionView,
    ProcessPackageCommand,
    RequestDetailView,
    RequesterRequestView,
    RequestKind,
    ResetCommand,
    RetryOperationCommand,
    ReviewItemView,
    ReviewSectionView,
    ReviewView,
    RiskLevel,
    RunsView,
    RunView,
    SessionView,
    SetupStage,
    SetupStageState,
    SetupStageView,
    SetupView,
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
    CatalogBindingReader,
    DataProductReferenceReader,
    FulfillmentDecisionCommands,
    FulfillmentViewReader,
    GovernedWorkspaceIdentity,
    RequestInboxReader,
    RequestIntakeCommands,
    SemanticReviewCommands,
    SemanticReviewReader,
    TenantAcquisitionReceiptReader,
    TenantRunReader,
    WarehouseBindingReader,
    WarehouseConfirmation,
    WarehouseLifecycleCommands,
    WarehouseOperationReader,
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
    {"sources", "business_process", "meaning", "data_product", "activation"}
)
_REQUEST_STATES: dict[RequestState, ConsoleRequestState] = {
    RequestState.SUBMITTED: "submitted",
    RequestState.CLARIFYING: "clarifying",
    RequestState.INVESTIGATING: "investigating",
    RequestState.PROPOSED: "proposed",
    RequestState.AWAITING_APPROVAL: "awaiting_approval",
    RequestState.EXECUTING: "execution_ready",
    RequestState.VERIFYING: "execution_ready",
    RequestState.DELIVERED: "closed",
    RequestState.MONITORING: "closed",
    RequestState.REJECTED: "denied",
    RequestState.NO_VALID_PLAN: "closed",
    RequestState.CANCELLED: "closed",
    RequestState.FAILED: "closed",
    RequestState.RETIRED: "closed",
}
_BLOCKED_REASONS: dict[RequestState, str] = {
    RequestState.NO_VALID_PLAN: "No valid plan was recorded for this request.",
    RequestState.REJECTED: "The request was denied by the owning authority.",
    RequestState.FAILED: "The request failed before it reached a delivered outcome.",
}
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


@dataclass(frozen=True, slots=True)
class _Capability:
    capability_id: str
    label: str
    dependency: str
    detail: str


_UNDELIVERED_CAPABILITIES: tuple[_Capability, ...] = (
    _Capability(
        capability_id="process-package",
        label="Business process package",
        dependency="a process-package command that carries the narrative document",
        detail=(
            "The submission command carries no document bytes and names a media type "
            "the owning upload transaction does not accept."
        ),
    ),
    _Capability(
        capability_id="analyst-dashboard",
        label="Analyst dashboards",
        dependency="the governed Superset embedding surface",
        detail="Dashboard embedding remains an outstanding analyst-surface obligation.",
    ),
    _Capability(
        capability_id="catalog-asset-preview",
        label="Catalog asset preview",
        dependency="a catalog asset read interface over published references",
        detail="Publication receipts name references but carry no reviewable asset detail.",
    ),
    _Capability(
        capability_id="source-acquisition",
        label="Source acquisition",
        dependency="a composed acquisition runtime",
        detail=(
            "Receipts an acquisition records are now retained and readable, but "
            "nothing here can perform one: the runtime is composed only in tests."
        ),
    ),
    _Capability(
        capability_id="operation-retry",
        label="Operation retry",
        dependency="a public retry authority issued by an owning service",
        detail=(
            "No owning service publishes a retry token, and the console must not "
            "become a second replay authority by minting one."
        ),
    ),
)


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
        semantic_reviews: SemanticReviewReader | None = None,
        requests: RequestInboxReader | None = None,
        fulfillment: FulfillmentViewReader | None = None,
        runs: TenantRunReader | None = None,
        acquisition_receipts: TenantAcquisitionReceiptReader | None = None,
        data_products: DataProductReferenceReader | None = None,
        principals: WorkspacePrincipalDirectory | None = None,
        warehouse_commands: WarehouseLifecycleCommands | None = None,
        request_commands: RequestIntakeCommands | None = None,
        fulfillment_commands: FulfillmentDecisionCommands | None = None,
        semantic_review_commands: SemanticReviewCommands | None = None,
        reset_tokens: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._identity = identity
        self._operation_handles = operation_handles
        self._warehouse_bindings = warehouse_bindings
        self._warehouse_operations = warehouse_operations
        self._catalog_bindings = catalog_bindings
        self._semantic_reviews = semantic_reviews
        self._requests = requests
        self._fulfillment = fulfillment
        self._runs = runs
        self._acquisition_receipts = acquisition_receipts
        self._data_products = data_products
        self._principals = principals
        self._warehouse_commands = warehouse_commands
        self._request_commands = request_commands
        self._fulfillment_commands = fulfillment_commands
        self._semantic_review_commands = semantic_review_commands
        self._reset_tokens = reset_tokens

    @property
    def fixture_mode(self) -> bool:
        return False

    def get_session(self, context: TrustedActorContext, csrf_token: str) -> SessionView:
        return SessionView(
            actor=ActorDisplayView(display_name=context.actor_id),
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
        ]
        capabilities.extend(
            CapabilityView(
                capability_id=capability.capability_id,
                label=capability.label,
                state="not_delivered",
                detail=capability.detail,
                dependency=capability.dependency,
            )
            for capability in _UNDELIVERED_CAPABILITIES
        )
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
        stage_states = self._stage_states(binding, catalog_binding)
        active_stage = next(
            (stage for stage in _SETUP_STAGES if stage_states[stage] != "complete"),
            _SETUP_STAGES[-1],
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
        self._authorize(context, ("data_architect",))
        request = self._visible_request(context, request_id)
        return self._request_detail_view(context, request)

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
        return DataProductView(
            data_product_id=newest.artifact_id,
            artifact_digest=newest.digest,
            version=newest.version,
        )

    def get_runs(self, context: TrustedActorContext) -> RunsView:
        self._authorize(context, ("data_architect", "data_owner"))
        if self._runs is None:
            raise _not_delivered("a tenant-scoped run read interface")
        records = self._runs.list_runs(context.tenant_id)
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
            )
        )

    def get_acquisition_receipts(self, context: TrustedActorContext) -> AcquisitionReceiptsView:
        # The same role set the fixture backend enforces. Governed mode must never be
        # more permissive than the demo that stands in for it.
        self._authorize(context, ("data_architect", "data_owner"))
        if self._acquisition_receipts is None:
            raise _not_delivered("a durable acquisition evidence store")
        receipts = self._acquisition_receipts.list_acquisition_receipts(context.tenant_id)
        return AcquisitionReceiptsView(
            receipts=tuple(
                AcquisitionReceiptView(
                    evidence_id=receipt.evidence_id,
                    contract_ref=receipt.contract_ref,
                    source_binding_ref=receipt.source_binding_ref,
                    acquisition_mode=receipt.acquisition_mode,
                    logical_object_refs=receipt.logical_object_refs,
                    outcome=receipt.outcome,
                    reason_codes=receipt.reason_codes,
                    created_at=receipt.created_at,
                )
                for receipt in receipts
            )
        )

    def get_catalog_asset(self, context: TrustedActorContext, asset_ref: str) -> CatalogAssetView:
        raise _not_delivered("a catalog asset read interface over published references")

    def get_dashboard(self, context: TrustedActorContext, dashboard_ref: str) -> DashboardView:
        raise _not_delivered("the governed Superset embedding surface")

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
    ) -> Never:
        # The owning upload transaction needs the narrative bytes and a business
        # process manifest. This command carries neither, and its media types are
        # not the one that transaction accepts, so there is nothing to delegate.
        raise _not_delivered("a process-package command that carries the narrative document")

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
        self._authorize(context, ("data_architect",))
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
        architect_view = self._architect_view(context, request_id)
        self._require_current_revision(command.expected_revision, request.revision)
        self._require_held_requirement(
            architect_view.proposals, authority_ref, command.reviewed_digest
        )
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
        return self._request_detail_view(context, self._visible_request(context, request_id))

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
        self._guarded(
            lambda: commands.admit(
                tenant_id=context.tenant_id,
                request_id=request_id,
                actor_id=context.actor_id,
                expected_revision=command.expected_revision,
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
        request_input = command.request
        if request_input.kind == "stakeholder_question":
            created = self._guarded(
                lambda: commands.submit_question(
                    tenant_id=context.tenant_id,
                    requester_id=context.actor_id,
                    title=command.title,
                    purpose=request_input.purpose,
                    question=request_input.question,
                )
            )
        else:
            created = self._guarded(
                lambda: commands.submit_access_request(
                    tenant_id=context.tenant_id,
                    requester_id=context.actor_id,
                    title=command.title,
                    purpose=request_input.purpose,
                    data_product_id=request_input.data_product_ref,
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
                author_label=entry.actor_id,
                # Request-management records an actor, not a role. This distinguishes
                # the requester from everyone else and carries no service authority.
                author_role=(
                    "requester" if entry.actor_id == request.requester_id else "data_architect"
                ),
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
        return RequestDetailView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            purpose=request.payload.purpose,
            revision=request.revision,
            proposal_digest=None if requirement is None else requirement.subject_digest,
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
                if requirement is not None and not decided
                else ()
            ),
            admission=self._admission_view(request, proposal, view.approvals),
        )

    @staticmethod
    def _admission_view(
        request: InboxRequest,
        proposal: FulfillmentProposal | None,
        approvals: tuple[FulfillmentApprovalBinding, ...],
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

    @staticmethod
    def _evidence_context(view: ArchitectRequestView) -> EvidenceContextView:
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
        return EvidenceContextView(
            as_of=as_of,
            freshness=freshness,
            quality_summary=quality_summary,
            lineage_summary=lineage_summary,
            authorization_summary=(
                f"{len(view.approvals)} of {required} required approval(s) are recorded."
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
        return (
            "ready" if binding.lifecycle_state is CatalogBindingState.READY else "blocked",
            f"The managed catalog binding is {binding.lifecycle_state.value}.",
        )

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
        binding: WarehouseBinding | None, catalog_binding: CatalogBinding | None
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
        return RequesterRequestView(
            request_id=request.request_id,
            kind=kind,
            state=_REQUEST_STATES[request.state],
            title=self._request_title(request),
            requested_outcome=request.payload.purpose,
            revision=request.revision,
            updated_at=request.updated_at,
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
            denial_explanation=view.denial_explanation,
        )

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
