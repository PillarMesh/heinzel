from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .auth import TrustedActorContext
from .contracts import (
    AdmissionCommand,
    CatalogAssetView,
    ClarifiedOutcomeAcceptanceCommand,
    ClarifiedOutcomeView,
    ConversationMessageCommand,
    ConversationView,
    CreateRequestCommand,
    DashboardView,
    DataProductView,
    DecisionCommand,
    EvidenceView,
    InboxView,
    OperationView,
    ProcessPackageCommand,
    RequestDetailView,
    RequesterRequestView,
    ResetCommand,
    RetryOperationCommand,
    ReviewView,
    RunsView,
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

    def get_requester_requests(
        self, context: TrustedActorContext
    ) -> tuple[RequesterRequestView, ...]: ...

    def get_conversation(
        self, context: TrustedActorContext, request_id: str
    ) -> ConversationView: ...

    def get_clarified_outcome(
        self, context: TrustedActorContext, request_id: str
    ) -> ClarifiedOutcomeView: ...

    def get_data_product(
        self, context: TrustedActorContext, data_product_id: str
    ) -> DataProductView: ...

    def get_runs(self, context: TrustedActorContext) -> RunsView: ...

    def get_catalog_asset(
        self, context: TrustedActorContext, asset_ref: str
    ) -> CatalogAssetView: ...

    def get_dashboard(self, context: TrustedActorContext, dashboard_ref: str) -> DashboardView: ...

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

    def retry_operation(
        self,
        context: TrustedActorContext,
        operation_id: str,
        command: RetryOperationCommand,
    ) -> OperationView: ...

    def get_preview(self, context: TrustedActorContext, preview_ref: str) -> PreviewContent: ...

    def authorize_link(self, context: TrustedActorContext, link_ref: str) -> AuthorizedLink: ...

    def reset(self, context: TrustedActorContext, command: ResetCommand) -> SetupView: ...
