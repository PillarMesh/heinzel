from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .auth import TrustedActorContext
from .contracts import (
    AccessLifecycleView,
    AccessRevocationCommand,
    AcquisitionReceiptsView,
    AcquisitionReceiptView,
    AcquisitionRunNowCommand,
    AdmissionCommand,
    AnswerResultPageView,
    CatalogAssetsView,
    CatalogAssetView,
    ClarifiedOutcomeAcceptanceCommand,
    ClarifiedOutcomeView,
    ConversationMessageCommand,
    ConversationView,
    CreateRequestCommand,
    DashboardsView,
    DashboardView,
    DataProductsView,
    DataProductView,
    DecisionCommand,
    EvidenceView,
    ImpactView,
    InboxView,
    IncidentRecoveryCommand,
    IncidentsView,
    IncidentView,
    OperationView,
    ProcessPackageCommand,
    ProductIntentApprovalCommand,
    ProductIntentApprovalView,
    ProposalPreparationCommand,
    RequestClarificationCommand,
    RequestDetailView,
    RequesterRequestView,
    RequestWithdrawalCommand,
    ResetCommand,
    RetryOperationCommand,
    ReviewView,
    RunsView,
    SelectableAnswerTermsView,
    SessionView,
    SetupView,
    WarehouseBindingCommand,
    WorkspaceView,
)


@dataclass(frozen=True, slots=True)
class PreviewContent:
    body: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class AuthorizedLink:
    location: str


@dataclass(frozen=True, slots=True)
class AuthorizedDownload:
    body: bytes
    media_type: str
    filename: str
    result_digest: str


class ConsoleBackend(Protocol):
    @property
    def fixture_mode(self) -> bool: ...

    def get_session(self, context: TrustedActorContext, csrf_token: str) -> SessionView: ...

    def get_workspace(self, context: TrustedActorContext) -> WorkspaceView: ...

    def get_setup(self, context: TrustedActorContext) -> SetupView: ...

    def get_review(self, context: TrustedActorContext, review_id: str) -> ReviewView: ...

    def get_inbox(self, context: TrustedActorContext) -> InboxView: ...

    def get_request_detail(
        self, context: TrustedActorContext, request_id: str
    ) -> RequestDetailView: ...

    def get_request_impact(self, context: TrustedActorContext, request_id: str) -> ImpactView: ...

    def get_requester_requests(
        self, context: TrustedActorContext
    ) -> tuple[RequesterRequestView, ...]: ...

    def get_answer_result(
        self,
        context: TrustedActorContext,
        request_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 100,
    ) -> AnswerResultPageView: ...

    def download_answer_result(
        self, context: TrustedActorContext, request_id: str
    ) -> AuthorizedDownload: ...

    def get_conversation(
        self, context: TrustedActorContext, request_id: str
    ) -> ConversationView: ...

    def get_clarified_outcome(
        self, context: TrustedActorContext, request_id: str
    ) -> ClarifiedOutcomeView: ...

    def get_data_product(
        self, context: TrustedActorContext, data_product_id: str
    ) -> DataProductView: ...

    def get_data_products(self, context: TrustedActorContext) -> DataProductsView: ...

    def get_runs(self, context: TrustedActorContext) -> RunsView: ...

    def get_incidents(self, context: TrustedActorContext) -> IncidentsView: ...

    def get_acquisition_receipts(self, context: TrustedActorContext) -> AcquisitionReceiptsView: ...

    def run_acquisition_now(
        self, context: TrustedActorContext, command: AcquisitionRunNowCommand
    ) -> AcquisitionReceiptView: ...

    def get_selectable_answer_terms(
        self, context: TrustedActorContext
    ) -> SelectableAnswerTermsView: ...

    def get_catalog_asset(
        self, context: TrustedActorContext, asset_ref: str
    ) -> CatalogAssetView: ...

    def get_catalog_assets(self, context: TrustedActorContext) -> CatalogAssetsView: ...

    def get_dashboard(self, context: TrustedActorContext, dashboard_ref: str) -> DashboardView: ...

    def get_dashboards(self, context: TrustedActorContext) -> DashboardsView: ...

    def get_evidence(self, context: TrustedActorContext, evidence_ref: str) -> EvidenceView: ...

    def get_operation(self, context: TrustedActorContext, operation_id: str) -> OperationView: ...

    def confirm_warehouse_binding(
        self, context: TrustedActorContext, command: WarehouseBindingCommand
    ) -> OperationView: ...

    def submit_process_package(
        self, context: TrustedActorContext, command: ProcessPackageCommand
    ) -> OperationView: ...

    def decide_review(
        self, context: TrustedActorContext, review_id: str, command: DecisionCommand
    ) -> ReviewView: ...

    def decide_request(
        self, context: TrustedActorContext, request_id: str, command: DecisionCommand
    ) -> RequestDetailView: ...

    def approve_product_intent(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ProductIntentApprovalCommand,
    ) -> ProductIntentApprovalView: ...

    def clarify_request(
        self, context: TrustedActorContext, request_id: str, command: RequestClarificationCommand
    ) -> RequestDetailView: ...

    def prepare_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView: ...

    def submit_request_proposal(
        self, context: TrustedActorContext, request_id: str, command: ProposalPreparationCommand
    ) -> RequestDetailView: ...

    def admit_request(
        self, context: TrustedActorContext, request_id: str, command: AdmissionCommand
    ) -> RequestDetailView: ...

    def create_request(
        self, context: TrustedActorContext, command: CreateRequestCommand
    ) -> RequesterRequestView: ...

    def append_conversation_message(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ConversationMessageCommand,
    ) -> ConversationView: ...

    def accept_clarified_outcome(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: ClarifiedOutcomeAcceptanceCommand,
    ) -> ClarifiedOutcomeView: ...

    def withdraw_request(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: RequestWithdrawalCommand,
    ) -> RequesterRequestView: ...

    def revoke_access(
        self,
        context: TrustedActorContext,
        request_id: str,
        command: AccessRevocationCommand,
    ) -> AccessLifecycleView: ...

    def retry_operation(
        self,
        context: TrustedActorContext,
        operation_id: str,
        command: RetryOperationCommand,
    ) -> OperationView: ...

    def recover_incident(
        self,
        context: TrustedActorContext,
        incident_id: str,
        command: IncidentRecoveryCommand,
        *,
        idempotency_key: str,
    ) -> IncidentView: ...

    def get_preview(self, context: TrustedActorContext, preview_ref: str) -> PreviewContent: ...

    def authorize_link(self, context: TrustedActorContext, link_ref: str) -> AuthorizedLink: ...

    def reset(self, context: TrustedActorContext, command: ResetCommand) -> SetupView: ...
