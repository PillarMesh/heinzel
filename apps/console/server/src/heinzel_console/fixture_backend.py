from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Never

from heinzel_request_management.intake import RequestDigestMismatch
from pydantic import ValidationError

from .auth import TrustedActorContext
from .backend import AuthorizedDownload, AuthorizedLink, PreviewContent
from .contracts import (
    AccessLifecycleView,
    AccessRevocationCommand,
    AcquisitionReceiptsView,
    AcquisitionReceiptView,
    AcquisitionRunNowCommand,
    ActorDisplayView,
    ActorRole,
    AdmissionCommand,
    AdmissionView,
    AnswerResultPageView,
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
    DecisionCommand,
    DisplayReferenceView,
    EvidenceContextView,
    EvidenceView,
    ImpactApproverView,
    ImpactItemView,
    ImpactView,
    InboxView,
    IncidentRecoveryCommand,
    IncidentsView,
    IncidentView,
    OperationFailureView,
    OperationView,
    OwnDecisionView,
    ProcessPackageCommand,
    ProcessPackageView,
    ProductIntentApprovalCommand,
    ProductIntentApprovalView,
    ProposalPreparationCommand,
    RecordedDecisionView,
    RecoveryAction,
    RequestClarificationCommand,
    RequestDetailView,
    RequesterRequestView,
    RequestProposalView,
    RequestWithdrawalCommand,
    ResetCommand,
    RetryOperationCommand,
    ReviewView,
    RunsView,
    SelectableAnswerTermsView,
    SessionView,
    SetupView,
    SourceRegistrationCommand,
    WarehouseBindingCommand,
    WarehouseBindingView,
    WorkspaceView,
    setup_snapshot_digest,
)
from .errors import ConsoleConflict, ConsoleNotFound, ConsoleUnavailable
from .fixture_data import (
    BLOCKED_REQUEST_DIGEST,
    FIXTURE_TENANT_ID,
    FIXTURE_WORKSPACE_ID,
    FixtureSeed,
    build_fixture_seed,
    conversation_digest,
    fixture_clock,
)
from .governed_adapters import console_error_for
from .request_intake import request_intake_content

# A requester may withdraw only before any work is admitted for the request.
_WITHDRAWABLE_STATES = frozenset(
    {"submitted", "clarifying", "investigating", "proposed", "awaiting_approval"}
)


@dataclass(frozen=True, slots=True)
class _DomainCommandIdentity:
    workspace_id: str
    resource_id: str
    expected_revision: int
    digest: str


type _FixtureCommand = (
    AdmissionCommand
    | WarehouseBindingCommand
    | ProcessPackageCommand
    | DecisionCommand
    | CreateRequestCommand
    | ConversationMessageCommand
    | ClarifiedOutcomeAcceptanceCommand
    | RequestWithdrawalCommand
    | RetryOperationCommand
    | ResetCommand
)
type _FixtureResult = (
    OperationView
    | ReviewView
    | RequestDetailView
    | RequesterRequestView
    | ConversationView
    | ClarifiedOutcomeView
    | SetupView
)


@dataclass(frozen=True, slots=True)
class _DomainReplay:
    command: _FixtureCommand
    result: _FixtureResult


@dataclass(frozen=True, slots=True)
class _ResetReplay:
    owner_principal: str
    command: ResetCommand
    result: SetupView


@dataclass(frozen=True, slots=True)
class _MonotonicSequence:
    next_value: int = 1

    def allocate(self, prefix: str) -> tuple[str, _MonotonicSequence]:
        return (
            f"{prefix}-{self.next_value:04d}",
            _MonotonicSequence(next_value=self.next_value + 1),
        )


@dataclass(frozen=True, slots=True)
class _ResourceAuthority:
    owner_principal: str
    role: ActorRole


@dataclass(frozen=True, slots=True)
class _RetryBinding:
    tenant_id: str
    owner_principal: str
    role: ActorRole
    operation_id: str
    revision: int
    operation_digest: str
    retry_eligible: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _FixtureState:
    setup: SetupView
    sequence: _MonotonicSequence
    reset_sequence: _MonotonicSequence
    reset_principal: str
    reset_replays: dict[str, _ResetReplay]
    domain_replays: dict[_DomainCommandIdentity, _DomainReplay]
    operations: dict[str, OperationView]
    operation_authorities: dict[str, _ResourceAuthority]
    retry_bindings: dict[str, _RetryBinding]
    reviews: dict[str, ReviewView]
    request_details: dict[str, RequestDetailView]
    requester_requests: dict[str, RequesterRequestView]
    request_owners: dict[str, str]
    clarified_outcomes: dict[str, ClarifiedOutcomeView]


_RETRY_TOKEN = "retry_" + ("7" * 58)
_RESET_REPLAY_LIMIT = 16
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SYNTHETIC_PREVIEW_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _owner_principal(tenant_id: str, actor_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\x00{actor_id}".encode()).hexdigest()
    return f"principal-{digest[:32]}"


def _with_entry[Key, Value](source: dict[Key, Value], key: Key, value: Value) -> dict[Key, Value]:
    updated = dict(source)
    updated[key] = value
    return updated


def _satisfy_authority(
    proposal: RequestProposalView | None,
    role: ActorRole,
    decision: str,
) -> RequestProposalView | None:
    """Record an approval against the authority the deciding role holds.

    The demonstration's required-roles list is what tells the architect which
    authorities a proposal still needs. An approval that never marks one satisfied
    leaves the list saying `Not recorded` forever, and admission could then never
    honestly become available.
    """
    if proposal is None or decision != "approve":
        return proposal
    authorities = getattr(proposal, "required_authorities", ())
    if not any(authority.role == role and not authority.satisfied for authority in authorities):
        return proposal
    return proposal.model_copy(
        update={
            "required_authorities": tuple(
                authority.model_copy(update={"satisfied": True})
                if authority.role == role
                else authority
                for authority in authorities
            )
        }
    )


class FixtureConsoleBackend:
    fixture_mode = True

    def __init__(
        self,
        *,
        seed: FixtureSeed | None = None,
        clock: Callable[[], datetime] = fixture_clock,
        sequence_factory: Callable[[], _MonotonicSequence] = _MonotonicSequence,
    ) -> None:
        self._seed = seed or build_fixture_seed()
        self._clock = clock
        self._sequence_factory = sequence_factory
        self._lock = RLock()
        self._state = self._new_state()

    def _new_state(
        self,
        *,
        setup: SetupView | None = None,
        reset_sequence: _MonotonicSequence | None = None,
        reset_principal: str | None = None,
        reset_replays: dict[str, _ResetReplay] | None = None,
    ) -> _FixtureState:
        retry_digest = "9" * 64
        retryable_operation = OperationView(
            operation_id="operation-retryable",
            revision=1,
            state="failed",
            phase="fixture_validation",
            summary="Synthetic transient failure available for retry testing.",
            failure=OperationFailureView(
                code="fixture-transient-failure",
                classification="transient",
                safe_message="The synthetic operation encountered a transient failure.",
            ),
            recovery_actions=("retry",),
            operation_digest=retry_digest,
            retry_token=_RETRY_TOKEN,
        )
        architect_principal = _owner_principal(FIXTURE_TENANT_ID, "actor-architect")
        requester_principal = _owner_principal(FIXTURE_TENANT_ID, "actor-requester")
        retry_binding = _RetryBinding(
            tenant_id=FIXTURE_TENANT_ID,
            owner_principal=architect_principal,
            role="data_architect",
            operation_id=retryable_operation.operation_id,
            revision=retryable_operation.revision,
            operation_digest=retry_digest,
            retry_eligible=True,
            expires_at=fixture_clock() + timedelta(days=1),
        )
        return _FixtureState(
            setup=setup or self._seed.setup,
            sequence=self._sequence_factory(),
            reset_sequence=reset_sequence or _MonotonicSequence(),
            reset_principal=reset_principal or architect_principal,
            reset_replays={} if reset_replays is None else reset_replays,
            domain_replays={},
            operations={retryable_operation.operation_id: retryable_operation},
            operation_authorities={
                retryable_operation.operation_id: _ResourceAuthority(
                    owner_principal=architect_principal,
                    role="data_architect",
                )
            },
            retry_bindings={_RETRY_TOKEN: retry_binding},
            reviews=dict(self._seed.reviews),
            request_details=dict(self._seed.request_details),
            requester_requests={
                request.request_id: request for request in self._seed.requester_requests
            },
            request_owners={
                request.request_id: requester_principal for request in self._seed.requester_requests
            },
            clarified_outcomes=dict(self._seed.clarified_outcomes),
        )

    @staticmethod
    def _conflict(
        code: str,
        safe_message: str,
        recovery_action: RecoveryAction = "reload",
    ) -> Never:
        raise ConsoleConflict(
            code=code,
            safe_message=safe_message,
            recovery_action=recovery_action,
        )

    @staticmethod
    def _command_identity(
        *,
        resource_id: str,
        expected_revision: int,
        digest: str,
    ) -> _DomainCommandIdentity:
        return _DomainCommandIdentity(
            workspace_id=FIXTURE_WORKSPACE_ID,
            resource_id=resource_id,
            expected_revision=expected_revision,
            digest=digest,
        )

    def _replay_result[Result: _FixtureResult](
        self,
        identity: _DomainCommandIdentity,
        command: _FixtureCommand,
        result_type: type[Result],
    ) -> Result | None:
        replay = self._state.domain_replays.get(identity)
        if replay is None:
            return None
        if replay.command != command or not isinstance(replay.result, result_type):
            self._conflict(
                "command_identity_mismatch",
                "The command identity was already used with different input.",
            )
        return replay.result

    def _replays_with(
        self,
        identity: _DomainCommandIdentity,
        command: _FixtureCommand,
        result: _FixtureResult,
    ) -> dict[_DomainCommandIdentity, _DomainReplay]:
        replay = _DomainReplay(command=command, result=result)
        return _with_entry(self._state.domain_replays, identity, replay)

    def _commit(self, next_state: _FixtureState) -> None:
        self._state = next_state

    def _updated_setup(self, updates: Mapping[str, object]) -> SetupView:
        payload = self._state.setup.model_dump() | dict(updates)
        return SetupView.model_validate(payload | {"setup_digest": setup_snapshot_digest(payload)})

    def _reset_setup(self, reset_token: str) -> SetupView:
        payload = self._seed.setup.model_dump() | {"reset_token": reset_token}
        return SetupView.model_validate(payload | {"setup_digest": setup_snapshot_digest(payload)})

    def _reset_replays_with(
        self,
        reset_token: str,
        replay: _ResetReplay,
    ) -> dict[str, _ResetReplay]:
        updated = _with_entry(self._state.reset_replays, reset_token, replay)
        while len(updated) > _RESET_REPLAY_LIMIT:
            del updated[next(iter(updated))]
        return updated

    @staticmethod
    def _require_command_role(
        context: TrustedActorContext,
        command_role: ActorRole,
    ) -> None:
        if command_role != context.active_role:
            raise ConsoleNotFound()

    def _authorize(
        self, context: TrustedActorContext, allowed_roles: tuple[ActorRole, ...]
    ) -> None:
        if (
            context.tenant_id != FIXTURE_TENANT_ID
            or context.active_role not in context.roles
            or context.active_role not in allowed_roles
        ):
            raise ConsoleNotFound()

    @staticmethod
    def _principal(context: TrustedActorContext) -> str:
        return _owner_principal(context.tenant_id, context.actor_id)

    def _authorize_request_owner(self, context: TrustedActorContext, request_id: str) -> None:
        if context.active_role == "requester" and self._state.request_owners.get(
            request_id
        ) != self._principal(context):
            raise ConsoleNotFound()

    @staticmethod
    def _review_roles(review: ReviewView) -> tuple[ActorRole, ...]:
        return tuple(authority.role for authority in review.required_authorities)

    def get_workspace(self, context: TrustedActorContext) -> WorkspaceView:
        self._authorize(context, context.roles)
        return self._seed.workspace

    def get_session(self, context: TrustedActorContext, csrf_token: str) -> SessionView:
        self._authorize(context, context.roles)
        role_labels = {
            "requester": "Riley Requester",
            "data_architect": "Dana Architect",
            "data_owner": "Morgan Data Owner",
            "policy_approver": "Parker Policy Approver",
            "budget_approver": "Bailey Budget Approver",
        }
        return SessionView(
            actor=ActorDisplayView(display_name=role_labels[context.active_role]),
            roles=context.roles,
            active_role=context.active_role,
            tenant=DisplayReferenceView(ref="tenant-demo", display_name="Northwind Demo"),
            workspace=DisplayReferenceView(
                ref="workspace-revenue",
                display_name="Revenue to cash",
            ),
            csrf_token=csrf_token,
        )

    def get_setup(self, context: TrustedActorContext) -> SetupView:
        self._authorize(context, ("data_architect",))
        with self._lock:
            return self._state.setup

    def get_inbox(self, context: TrustedActorContext) -> InboxView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        return self._seed.inbox

    def get_review(self, context: TrustedActorContext, review_id: str) -> ReviewView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        with self._lock:
            review = self._state.reviews.get(review_id)
            if review is None or context.active_role not in self._review_roles(review):
                raise ConsoleNotFound()
            return review

    def get_request_detail(
        self, context: TrustedActorContext, request_id: str
    ) -> RequestDetailView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        with self._lock:
            detail = self._state.request_details.get(request_id)
        if detail is None:
            raise ConsoleNotFound()
        return detail

    def get_request_impact(self, context: TrustedActorContext, request_id: str) -> ImpactView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        if request_id != "request-answer":
            raise ConsoleNotFound()
        validated = (
            ImpactItemView(
                impact_handle="impact-a0f5bfb7aa7ea54f5123",
                label="Revenue overview",
                asset_type="Dashboard",
                owner_label="Revenue data owner",
            ),
        )
        possible = (
            ()
            if context.active_role == "data_owner"
            else (
                ImpactItemView(
                    impact_handle="impact-e071e4af36bcc08ea76c",
                    label="Quarterly forecast",
                    asset_type="Report",
                    owner_label="Finance data owner",
                ),
            )
        )
        return ImpactView(
            request_id=request_id,
            change_type="metric_version_change",
            subject_label="Net revenue v2",
            analyzed_at=fixture_clock(),
            validated_impacts=validated,
            possible_impacts=possible,
            affected_owners=tuple(sorted({item.owner_label for item in (*validated, *possible)})),
            added_approvers=(
                ImpactApproverView(
                    authority_label="Revenue data owner",
                    reason="Approval required for a validated dependency.",
                ),
                ImpactApproverView(
                    authority_label="Executive data owner",
                    reason="Approval required for a validated dependency.",
                ),
            ),
        )

    def get_requester_requests(
        self, context: TrustedActorContext
    ) -> tuple[RequesterRequestView, ...]:
        self._authorize(context, ("requester",))
        principal = self._principal(context)
        with self._lock:
            return tuple(
                request
                for request_id, request in self._state.requester_requests.items()
                if self._state.request_owners.get(request_id) == principal
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
        self._authorize_request_owner(context, request_id)
        raise ConsoleNotFound()

    def download_answer_result(
        self, context: TrustedActorContext, request_id: str
    ) -> AuthorizedDownload:
        self._authorize(context, ("requester",))
        self._authorize_request_owner(context, request_id)
        raise ConsoleNotFound()

    def get_conversation(self, context: TrustedActorContext, request_id: str) -> ConversationView:
        self._authorize(
            context,
            ("requester", "data_architect", "data_owner", "policy_approver"),
        )
        with self._lock:
            detail = self._state.request_details.get(request_id)
            self._authorize_request_owner(context, request_id)
            if detail is None:
                raise ConsoleNotFound()
            return detail.conversation

    def get_clarified_outcome(
        self, context: TrustedActorContext, request_id: str
    ) -> ClarifiedOutcomeView:
        self._authorize(context, ("requester", "data_architect"))
        with self._lock:
            outcome = self._state.clarified_outcomes.get(request_id)
            self._authorize_request_owner(context, request_id)
            if outcome is None:
                raise ConsoleNotFound()
            return outcome

    def get_data_product(
        self, context: TrustedActorContext, data_product_id: str
    ) -> DataProductView:
        self._authorize(context, context.roles)
        data_product = self._seed.data_products.get(data_product_id)
        if data_product is None:
            raise ConsoleNotFound()
        return data_product

    def get_data_products(self, context: TrustedActorContext) -> DataProductsView:
        self._authorize(context, context.roles)
        return DataProductsView(
            products=tuple(
                self._seed.data_products[product_id]
                for product_id in sorted(self._seed.data_products)
            )
        )

    def get_runs(self, context: TrustedActorContext) -> RunsView:
        self._authorize(context, ("data_architect", "data_owner"))
        return self._seed.runs

    def get_incidents(self, context: TrustedActorContext) -> IncidentsView:
        self._authorize(context, ("data_architect", "data_owner"))
        return IncidentsView()

    def get_acquisition_receipts(self, context: TrustedActorContext) -> AcquisitionReceiptsView:
        self._authorize(context, ("data_architect", "data_owner"))
        return self._seed.acquisition_receipts

    def run_acquisition_now(
        self, context: TrustedActorContext, command: AcquisitionRunNowCommand
    ) -> AcquisitionReceiptView:
        self._authorize(context, ("data_architect", "data_owner"))
        self._require_command_role(context, command.active_role)
        raise ConsoleUnavailable(
            code="capability_not_delivered",
            safe_message="Source acquisition is not delivered in demo mode.",
            recovery_action="none",
        )

    def get_selectable_answer_terms(
        self, context: TrustedActorContext
    ) -> SelectableAnswerTermsView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        return self._seed.selectable_answer_terms

    def get_catalog_asset(self, context: TrustedActorContext, asset_ref: str) -> CatalogAssetView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        asset = self._seed.catalog_assets.get(asset_ref)
        if asset is None:
            raise ConsoleNotFound()
        return asset

    def get_catalog_assets(self, context: TrustedActorContext) -> CatalogAssetsView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        return CatalogAssetsView(
            assets=tuple(
                self._seed.catalog_assets[asset_ref]
                for asset_ref in sorted(self._seed.catalog_assets)
            )
        )

    def get_dashboard(self, context: TrustedActorContext, dashboard_ref: str) -> DashboardView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        dashboard = self._seed.dashboards.get(dashboard_ref)
        if dashboard is None:
            raise ConsoleNotFound()
        return dashboard

    def get_dashboards(self, context: TrustedActorContext) -> DashboardsView:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        return DashboardsView(
            dashboards=tuple(
                self._seed.dashboards[dashboard_ref]
                for dashboard_ref in sorted(self._seed.dashboards)
            )
        )

    def get_evidence(self, context: TrustedActorContext, evidence_ref: str) -> EvidenceView:
        self._authorize(
            context,
            ("requester", "data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        raise ConsoleUnavailable(
            code="fixture_evidence_unavailable",
            safe_message="Authoritative evidence is unavailable in fixture mode.",
            recovery_action="none",
        )

    def get_operation(self, context: TrustedActorContext, operation_id: str) -> OperationView:
        self._authorize(
            context,
            ("requester", "data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        with self._lock:
            operation = self._state.operations.get(operation_id)
            authority = self._state.operation_authorities.get(operation_id)
            if (
                operation is None
                or authority is None
                or authority.owner_principal != self._principal(context)
                or authority.role != context.active_role
            ):
                raise ConsoleNotFound()
            if operation.retry_token is not None:
                binding = self._state.retry_bindings.get(operation.retry_token)
                if binding is None or self._clock() >= binding.expires_at:
                    return OperationView.model_validate(
                        operation.model_dump()
                        | {
                            "operation_digest": None,
                            "retry_token": None,
                            "recovery_actions": (),
                        }
                    )
            return operation

    def get_preview(self, context: TrustedActorContext, preview_ref: str) -> PreviewContent:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        if preview_ref != "preview-dashboard-revenue":
            raise ConsoleNotFound()
        return PreviewContent(
            body=_SYNTHETIC_PREVIEW_PNG,
            media_type="image/png",
        )

    def authorize_link(self, context: TrustedActorContext, link_ref: str) -> AuthorizedLink:
        self._authorize(context, ("requester", "data_architect", "data_owner"))
        locations = {
            "link-catalog-revenue": "/demo/catalog/revenue",
            "link-dashboard-revenue": "/demo/dashboards/revenue",
        }
        location = locations.get(link_ref)
        if location is None:
            raise ConsoleNotFound()
        return AuthorizedLink(location=location)

    def confirm_warehouse_binding(
        self, context: TrustedActorContext, command: WarehouseBindingCommand
    ) -> OperationView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)

        identity = self._command_identity(
            resource_id="warehouse-binding",
            expected_revision=command.expected_revision,
            digest=command.reviewed_digest,
        )
        with self._lock:
            existing_binding = self._state.setup.warehouse_binding
            if existing_binding is not None and existing_binding.engine != command.engine:
                raise ConsoleConflict(
                    code="immutable_warehouse_binding",
                    safe_message="The warehouse binding is immutable after confirmation.",
                    recovery_action="none",
                )

            replay = self._replay_result(identity, command, OperationView)
            if replay is not None:
                return replay

            if (
                command.expected_revision != self._state.setup.revision
                or command.reviewed_digest != self._state.setup.setup_digest
            ):
                raise ConsoleConflict(
                    code="stale_revision",
                    safe_message="The setup changed; reload before confirming the warehouse.",
                    recovery_action="reload",
                )

            operation_id, next_sequence = self._state.sequence.allocate("operation")
            operation = OperationView(
                operation_id=operation_id,
                revision=1,
                state="accepted",
                phase="warehouse_provisioning",
                summary="Warehouse provisioning was accepted for deterministic fixture processing.",
                evidence_ref=None,
                recovery_actions=(),
            )
            binding = WarehouseBindingView(
                binding_ref="binding-warehouse",
                engine=command.engine,
                region=command.region,
                capacity=command.capacity,
                state="blocked",
            )
            setup = self._updated_setup(
                {
                    "revision": self._state.setup.revision + 1,
                    "warehouse_binding": binding,
                }
            )
            authority = _ResourceAuthority(
                owner_principal=self._principal(context),
                role=context.active_role,
            )
            next_state = replace(
                self._state,
                setup=setup,
                sequence=next_sequence,
                domain_replays=self._replays_with(identity, command, operation),
                operations=_with_entry(self._state.operations, operation.operation_id, operation),
                operation_authorities=_with_entry(
                    self._state.operation_authorities,
                    operation.operation_id,
                    authority,
                ),
            )
            self._commit(next_state)
            return operation

    def submit_process_package(
        self, context: TrustedActorContext, command: ProcessPackageCommand
    ) -> OperationView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id="process-package",
            expected_revision=command.expected_revision,
            digest=command.package_digest,
        )
        with self._lock:
            replay = self._replay_result(identity, command, OperationView)
            if replay is not None:
                return replay
            if command.expected_revision != self._state.setup.revision:
                self._conflict(
                    "stale_revision",
                    "The setup changed; reload before submitting the process package.",
                )
            if self._state.setup.warehouse_binding is None:
                self._conflict(
                    "warehouse_binding_required",
                    "Confirm a warehouse before submitting a process package.",
                    "none",
                )

            operation_id, next_sequence = self._state.sequence.allocate("operation")
            operation = OperationView(
                operation_id=operation_id,
                revision=1,
                state="accepted",
                phase="process_package_validation",
                summary="Process package validation was accepted for fixture processing.",
                recovery_actions=(),
            )
            process_package = ProcessPackageView(
                package_ref="package-revenue-to-cash",
                version=1,
                content_digest=command.package_digest,
                state="blocked",
                candidate_summary="Synthetic candidate pending deterministic fixture validation.",
            )
            setup = self._updated_setup(
                {
                    "revision": self._state.setup.revision + 1,
                    "process_package": process_package,
                }
            )
            authority = _ResourceAuthority(
                owner_principal=self._principal(context),
                role=context.active_role,
            )
            next_state = replace(
                self._state,
                setup=setup,
                sequence=next_sequence,
                domain_replays=self._replays_with(identity, command, operation),
                operations=_with_entry(self._state.operations, operation.operation_id, operation),
                operation_authorities=_with_entry(
                    self._state.operation_authorities,
                    operation.operation_id,
                    authority,
                ),
            )
            self._commit(next_state)
            return operation

    def register_source(
        self, context: TrustedActorContext, command: SourceRegistrationCommand
    ) -> OperationView:
        """Refused: registering a source is a real connection being probed.

        The fixture console has no connection broker and no enrolled connection, so there is
        nothing here to register and nothing to probe. Recording a registration from seeded data
        would claim a source had been validated against a server this console never reached.
        """
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        raise ConsoleUnavailable(
            code="capability_not_delivered",
            safe_message="Registering a source requires a governed workspace.",
            recovery_action="none",
        )

    def decide_review(
        self, context: TrustedActorContext, review_id: str, command: DecisionCommand
    ) -> ReviewView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"review:{review_id}",
            expected_revision=command.expected_revision,
            digest=command.reviewed_digest,
        )
        with self._lock:
            review = self._state.reviews.get(review_id)
            if review is None or context.active_role not in self._review_roles(review):
                raise ConsoleNotFound()
            replay = self._replay_result(identity, command, ReviewView)
            if replay is not None:
                return replay
            if command.expected_revision != review.revision:
                self._conflict("stale_revision", "The review changed; reload before deciding.")
            if command.reviewed_digest != review.reviewed_digest:
                self._conflict(
                    "stale_digest", "The reviewed artifact changed; reload before deciding."
                )

            decided_at = self._clock()
            decided = ReviewView.model_validate(
                review.model_dump()
                | {
                    "revision": review.revision + 1,
                    "decisions": (
                        *review.decisions,
                        RecordedDecisionView(
                            role=context.active_role,
                            decision=command.decision,
                            decided_at=decided_at,
                        ),
                    ),
                    "can_decide": False,
                }
            )
            next_state = replace(
                self._state,
                reviews=_with_entry(self._state.reviews, review_id, decided),
                domain_replays=self._replays_with(identity, command, decided),
            )
            self._commit(next_state)
            return decided

    def decide_request(
        self, context: TrustedActorContext, request_id: str, command: DecisionCommand
    ) -> RequestDetailView:
        self._authorize(context, ("data_architect", "data_owner", "policy_approver"))
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"request-decision:{request_id}",
            expected_revision=command.expected_revision,
            digest=command.reviewed_digest,
        )
        with self._lock:
            replay = self._replay_result(identity, command, RequestDetailView)
            if replay is not None:
                return replay
            detail = self._state.request_details.get(request_id)
            if detail is None:
                raise ConsoleNotFound()
            if command.expected_revision != detail.revision:
                self._conflict("stale_revision", "The request changed; reload before deciding.")
            if request_id == "request-blocked-acceptance":
                outcome = self._state.clarified_outcomes[request_id]
                if not outcome.accepted and command.reviewed_digest == BLOCKED_REQUEST_DIGEST:
                    self._conflict(
                        "requester_acceptance_required",
                        "Requester acceptance is required before this action.",
                    )
            if detail.proposal_digest != command.reviewed_digest:
                self._conflict(
                    "stale_digest",
                    "The reviewed proposal changed; reload before deciding.",
                )

            state_by_decision = {
                "approve": "awaiting_approval",
                "reject": "denied",
                "request_changes": "clarifying",
            }
            decided_at = self._clock()
            decided = RequestDetailView.model_validate(
                detail.model_dump()
                | {
                    "state": state_by_decision[command.decision],
                    "revision": detail.revision + 1,
                    "available_actions": (),
                    "proposal": _satisfy_authority(
                        detail.proposal, context.active_role, command.decision
                    ),
                }
            )
            decided = decided.model_copy(update={"admission": self._admission_for(decided)})
            requester = self._state.requester_requests.get(request_id)
            requester_requests = self._state.requester_requests
            if requester is not None:
                requester_projection = RequesterRequestView.model_validate(
                    requester.model_dump()
                    | {
                        "state": decided.state,
                        "revision": decided.revision,
                        "updated_at": decided_at,
                        "own_decisions": (
                            *requester.own_decisions,
                            OwnDecisionView(
                                decision=command.decision,
                                subject_label=detail.title,
                                created_at=decided_at,
                            ),
                        ),
                    }
                )
                requester_requests = _with_entry(
                    self._state.requester_requests,
                    request_id,
                    requester_projection,
                )
            next_state = replace(
                self._state,
                request_details=_with_entry(self._state.request_details, request_id, decided),
                requester_requests=requester_requests,
                domain_replays=self._replays_with(identity, command, decided),
            )
            self._commit(next_state)
            return decided

    def approve_product_intent(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ProductIntentApprovalCommand,
    ) -> ProductIntentApprovalView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        raise ConsoleNotFound()

    @staticmethod
    def _admission_for(detail: RequestDetailView) -> AdmissionView | None:
        """Admission follows the proposal's own declared authorities.

        Offering it while an authority the same screen lists as unrecorded is still
        outstanding would claim unimplemented answer-delivery behaviour as live, which
        the console must never do.
        """
        if detail.state != "awaiting_approval" or detail.proposal is None:
            return None
        authorities = detail.proposal.required_authorities
        outstanding = tuple(authority for authority in authorities if not authority.satisfied)
        if not outstanding:
            return AdmissionView(available=True)
        return AdmissionView(
            available=False,
            blocking_reason=(
                f"{len(outstanding)} of {len(authorities)} required approvals are not recorded."
            ),
        )

    def clarify_request(
        self, context: TrustedActorContext, request_id: str, command: RequestClarificationCommand
    ) -> RequestDetailView:
        self._authorize(context, ("data_architect",))
        raise ConsoleUnavailable(
            code="fixture_preparation_unavailable",
            safe_message="Proposal preparation requires the governed local service.",
            recovery_action="none",
        )

    def prepare_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView:
        self._authorize(context, ("data_architect",))
        raise ConsoleUnavailable(
            code="fixture_preparation_unavailable",
            safe_message="Proposal preparation requires the governed local service.",
            recovery_action="none",
        )

    def submit_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView:
        self._authorize(context, ("data_architect",))
        raise ConsoleUnavailable(
            code="fixture_preparation_unavailable",
            safe_message="Proposal preparation requires the governed local service.",
            recovery_action="none",
        )

    def admit_request(
        self, context: TrustedActorContext, request_id: str, command: AdmissionCommand
    ) -> RequestDetailView:
        """Admit an approved proposal to execution in the demonstration."""
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        # Every other command records its identity so a retry after an unknown outcome
        # returns the recorded result rather than a stale-revision conflict, which is
        # what the console's reconciliation path depends on.
        identity = self._command_identity(
            resource_id=f"request-admission:{request_id}",
            expected_revision=command.expected_revision,
            digest=command.reviewed_digest,
        )
        with self._lock:
            replay = self._replay_result(identity, command, RequestDetailView)
            if replay is not None:
                return replay
            detail = self._state.request_details.get(request_id)
            if detail is None:
                raise ConsoleNotFound()
            if detail.revision != command.expected_revision:
                self._conflict("stale_revision", "The request changed; reload before admitting it.")
            if detail.proposal_digest != command.reviewed_digest:
                self._conflict(
                    "stale_digest", "The reviewed proposal changed; reload before admitting it."
                )
            if detail.admission is None or not detail.admission.available:
                self._conflict(
                    "admission_unavailable",
                    "Every required approval must be recorded before admission.",
                )
            admitted = RequestDetailView.model_validate(
                detail.model_dump()
                | {
                    "state": "execution_ready",
                    "revision": detail.revision + 1,
                    "available_actions": (),
                    "admission": None,
                }
            )
            self._commit(
                replace(
                    self._state,
                    request_details=_with_entry(self._state.request_details, request_id, admitted),
                    domain_replays=self._replays_with(identity, command, admitted),
                )
            )
            return admitted

    def create_request(
        self, context: TrustedActorContext, command: CreateRequestCommand
    ) -> RequesterRequestView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        try:
            request_intake_content(command).verify_digest(command.request_digest)
        except (RequestDigestMismatch, ValidationError) as error:
            raise console_error_for(error) from error
        identity = self._command_identity(
            resource_id=f"request-create:{context.actor_id}",
            expected_revision=command.expected_revision,
            digest=command.request_digest,
        )
        with self._lock:
            replay = self._replay_result(identity, command, RequesterRequestView)
            if replay is not None:
                return replay
            created_at = self._clock()
            request_id, next_sequence = self._state.sequence.allocate("request")
            created = RequesterRequestView(
                request_id=request_id,
                kind=command.request.kind,
                state="submitted",
                title=command.title,
                requested_outcome=command.request.purpose,
                revision=1,
                updated_at=created_at,
                question=(
                    command.request.question
                    if command.request.kind == "stakeholder_question"
                    else None
                ),
            )
            conversation = ConversationView(
                request_id=request_id,
                revision=1,
                conversation_digest=conversation_digest(request_id, 1, ()),
            )
            detail = RequestDetailView(
                request_id=request_id,
                kind=command.request.kind,
                state="submitted",
                title=command.title,
                purpose=command.request.purpose,
                revision=1,
                conversation=conversation,
                evidence=EvidenceContextView(
                    freshness="unknown",
                    quality_summary="No proposal or evidence exists yet.",
                    lineage_summary="Lineage is pending investigation.",
                    authorization_summary="Authority requirements are pending investigation.",
                    evidence_refs=(),
                ),
            )
            next_state = replace(
                self._state,
                sequence=next_sequence,
                requester_requests=_with_entry(self._state.requester_requests, request_id, created),
                request_details=_with_entry(self._state.request_details, request_id, detail),
                request_owners=_with_entry(
                    self._state.request_owners,
                    request_id,
                    self._principal(context),
                ),
                domain_replays=self._replays_with(identity, command, created),
            )
            self._commit(next_state)
            return created

    def append_conversation_message(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ConversationMessageCommand,
    ) -> ConversationView:
        self._authorize(
            context,
            ("requester", "data_architect", "data_owner", "policy_approver"),
        )
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"conversation:{request_id}",
            expected_revision=command.expected_revision,
            digest=command.conversation_digest,
        )
        with self._lock:
            detail = self._state.request_details.get(request_id)
            self._authorize_request_owner(context, request_id)
            if detail is None:
                raise ConsoleNotFound()
            replay = self._replay_result(identity, command, ConversationView)
            if replay is not None:
                return replay
            conversation = detail.conversation
            if command.expected_revision != conversation.revision:
                self._conflict(
                    "stale_revision",
                    "The conversation changed; reload before posting a message.",
                )
            if command.conversation_digest != conversation.conversation_digest:
                self._conflict(
                    "stale_digest",
                    "The conversation changed; reload before posting a message.",
                )
            created_at = self._clock()
            message_id, next_sequence = self._state.sequence.allocate("message")
            message = ConversationMessageView(
                message_id=message_id,
                author_label=context.active_role.replace("_", " ").title(),
                author_role=context.active_role,
                body=command.body,
                created_at=created_at,
            )
            appended = (*conversation.messages, message)
            updated = ConversationView.model_validate(
                conversation.model_dump()
                | {
                    "revision": conversation.revision + 1,
                    "conversation_digest": conversation_digest(
                        request_id, conversation.revision + 1, appended
                    ),
                    "messages": appended,
                    "awaiting_role": None,
                }
            )
            updated_detail = RequestDetailView.model_validate(
                detail.model_dump() | {"conversation": updated}
            )
            next_state = replace(
                self._state,
                sequence=next_sequence,
                request_details=_with_entry(
                    self._state.request_details,
                    request_id,
                    updated_detail,
                ),
                domain_replays=self._replays_with(identity, command, updated),
            )
            self._commit(next_state)
            return updated

    def accept_clarified_outcome(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ClarifiedOutcomeAcceptanceCommand,
    ) -> ClarifiedOutcomeView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"clarified-outcome:{request_id}",
            expected_revision=command.expected_revision,
            digest=command.clarified_outcome_digest,
        )
        with self._lock:
            outcome = self._state.clarified_outcomes.get(request_id)
            detail = self._state.request_details.get(request_id)
            requester = self._state.requester_requests.get(request_id)
            self._authorize_request_owner(context, request_id)
            if outcome is None or detail is None or requester is None:
                raise ConsoleNotFound()
            replay = self._replay_result(identity, command, ClarifiedOutcomeView)
            if replay is not None:
                return replay
            # Withdrawal advances the request but not the outcome, so the outcome revision alone
            # would let a stale acceptance bring a closed request back to life.
            if requester.state not in _WITHDRAWABLE_STATES:
                self._conflict(
                    "request_closed",
                    "This request is closed and accepts no further decision.",
                )
            if command.expected_revision != outcome.revision:
                self._conflict(
                    "stale_revision",
                    "The clarified outcome changed; reload before responding.",
                )
            if command.clarified_outcome_digest != outcome.statement_digest:
                self._conflict(
                    "stale_digest",
                    "The clarified outcome changed; reload before responding.",
                )
            updated_at = self._clock()
            next_revision = max(outcome.revision, detail.revision, requester.revision) + 1
            accepted = command.decision == "approve"
            updated = ClarifiedOutcomeView.model_validate(
                outcome.model_dump()
                | {
                    "revision": next_revision,
                    "accepted": command.decision == "approve",
                }
            )
            requester_projection = RequesterRequestView.model_validate(
                requester.model_dump()
                | {
                    "state": "proposed" if accepted else "clarifying",
                    "revision": next_revision,
                    "clarified_outcome": updated,
                    "updated_at": updated_at,
                }
            )
            request_detail = RequestDetailView.model_validate(
                detail.model_dump()
                | {
                    "state": "proposed" if accepted else "clarifying",
                    "revision": next_revision,
                    "available_actions": (
                        ("approve", "reject", "request_changes") if accepted else ()
                    ),
                }
            )
            next_state = replace(
                self._state,
                clarified_outcomes=_with_entry(
                    self._state.clarified_outcomes,
                    request_id,
                    updated,
                ),
                requester_requests=_with_entry(
                    self._state.requester_requests,
                    request_id,
                    requester_projection,
                ),
                request_details=_with_entry(
                    self._state.request_details,
                    request_id,
                    request_detail,
                ),
                domain_replays=self._replays_with(identity, command, updated),
            )
            self._commit(next_state)
            return updated

    def withdraw_request(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: RequestWithdrawalCommand,
    ) -> RequesterRequestView:
        self._authorize(context, ("requester",))
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"withdrawal:{request_id}",
            expected_revision=command.expected_revision,
            digest=hashlib.sha256(f"withdrawal:{request_id}".encode()).hexdigest(),
        )
        with self._lock:
            requester = self._state.requester_requests.get(request_id)
            detail = self._state.request_details.get(request_id)
            self._authorize_request_owner(context, request_id)
            if requester is None or detail is None:
                raise ConsoleNotFound()
            replay = self._replay_result(identity, command, RequesterRequestView)
            if replay is not None:
                return replay
            # Withdrawing is idempotent: a retry after success reports the withdrawn request
            # rather than a failure the requester cannot act on.
            if requester.state == "cancelled":
                return requester
            if requester.state not in _WITHDRAWABLE_STATES:
                self._conflict(
                    "withdrawal_unavailable",
                    "This request has already reached an outcome and cannot be withdrawn.",
                )
            if command.expected_revision != requester.revision:
                self._conflict("stale_revision", "The request changed; reload before withdrawing.")
            next_revision = max(detail.revision, requester.revision) + 1
            withdrawn = RequesterRequestView.model_validate(
                requester.model_dump()
                | {"state": "cancelled", "revision": next_revision, "updated_at": self._clock()}
            )
            withdrawn_detail = RequestDetailView.model_validate(
                detail.model_dump()
                | {
                    "state": "cancelled",
                    "revision": next_revision,
                    "available_actions": (),
                    "preparation_actions": (),
                }
            )
            next_state = replace(
                self._state,
                requester_requests=_with_entry(
                    self._state.requester_requests, request_id, withdrawn
                ),
                request_details=_with_entry(
                    self._state.request_details, request_id, withdrawn_detail
                ),
                domain_replays=self._replays_with(identity, command, withdrawn),
            )
            self._commit(next_state)
            return withdrawn

    def revoke_access(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: AccessRevocationCommand,
    ) -> AccessLifecycleView:
        raise ConsoleUnavailable(
            code="capability_not_delivered",
            safe_message="Manual access revocation requires a governed workspace.",
            recovery_action="none",
        )

    def retry_operation(
        self,
        context: TrustedActorContext,
        operation_id: str,
        command: RetryOperationCommand,
    ) -> OperationView:
        self._authorize(
            context,
            ("data_architect", "data_owner", "policy_approver", "budget_approver"),
        )
        self._require_command_role(context, command.active_role)
        identity = self._command_identity(
            resource_id=f"operation-retry:{operation_id}",
            expected_revision=command.expected_revision,
            digest=command.operation_digest,
        )
        with self._lock:
            binding = self._state.retry_bindings.get(command.retry_token)
            if (
                binding is None
                or binding.tenant_id != context.tenant_id
                or binding.owner_principal != self._principal(context)
                or binding.role != context.active_role
                or binding.operation_id != operation_id
            ):
                raise ConsoleNotFound()
            replay = self._replay_result(identity, command, OperationView)
            if replay is not None:
                return replay
            operation = self._state.operations.get(operation_id)
            if operation is None:
                raise ConsoleNotFound()
            if not binding.retry_eligible or self._clock() >= binding.expires_at:
                raise ConsoleNotFound()
            if command.expected_revision != binding.revision:
                self._conflict("stale_revision", "The operation changed; reload before retrying.")
            if command.operation_digest != binding.operation_digest:
                self._conflict("stale_digest", "The operation changed; reload before retrying.")
            if (
                operation.operation_digest != binding.operation_digest
                or operation.retry_token != command.retry_token
                or operation.failure is None
                or operation.failure.classification != "transient"
            ):
                self._conflict(
                    "operation_not_retryable",
                    "This operation does not admit a retry.",
                    "none",
                )

            retry_id, next_sequence = self._state.sequence.allocate("operation")
            retry = OperationView(
                operation_id=retry_id,
                revision=1,
                state="accepted",
                phase="retry_reconciliation",
                summary="The fixture retry was accepted for reconciliation.",
                recovery_actions=(),
            )
            retired = OperationView.model_validate(
                operation.model_dump()
                | {
                    "revision": operation.revision + 1,
                    "operation_digest": None,
                    "retry_token": None,
                    "recovery_actions": (),
                }
            )
            retired_binding = replace(binding, retry_eligible=False)
            operations = dict(self._state.operations)
            operations[operation_id] = retired
            operations[retry.operation_id] = retry
            operation_authorities = _with_entry(
                self._state.operation_authorities,
                retry.operation_id,
                _ResourceAuthority(
                    owner_principal=self._principal(context),
                    role=context.active_role,
                ),
            )
            next_state = replace(
                self._state,
                sequence=next_sequence,
                operations=operations,
                operation_authorities=operation_authorities,
                retry_bindings=_with_entry(
                    self._state.retry_bindings,
                    command.retry_token,
                    retired_binding,
                ),
                domain_replays=self._replays_with(identity, command, retry),
            )
            self._commit(next_state)
            return retry

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
        raise ConsoleNotFound()

    def reset(self, context: TrustedActorContext, command: ResetCommand) -> SetupView:
        self._authorize(context, ("data_architect",))
        self._require_command_role(context, command.active_role)
        with self._lock:
            principal = self._principal(context)
            replay = self._state.reset_replays.get(command.reset_token)
            if replay is not None:
                if replay.owner_principal != principal:
                    raise ConsoleNotFound()
                if replay.command != command:
                    self._conflict(
                        "command_identity_mismatch",
                        "The reset token was already used with different input.",
                    )
                return replay.result
            if principal != self._state.reset_principal:
                raise ConsoleNotFound()
            if command.reset_token != self._state.setup.reset_token:
                self._conflict(
                    "stale_reset_token", "The setup changed; reload before resetting it."
                )
            if command.expected_revision != self._state.setup.revision:
                self._conflict("stale_revision", "The setup changed; reload before resetting it.")
            if command.setup_digest != self._state.setup.setup_digest:
                self._conflict("stale_digest", "The setup changed; reload before resetting it.")
            next_reset_token, next_reset_sequence = self._state.reset_sequence.allocate(
                "reset_token_fixture_sequence"
            )
            next_setup = self._reset_setup(next_reset_token)
            reset_replays = self._reset_replays_with(
                command.reset_token,
                _ResetReplay(owner_principal=principal, command=command, result=next_setup),
            )
            next_state = self._new_state(
                setup=next_setup,
                reset_sequence=next_reset_sequence,
                reset_principal=principal,
                reset_replays=reset_replays,
            )
            self._commit(next_state)
            return next_setup
