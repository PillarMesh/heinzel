from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from ..contracts import (
    AcquisitionReceiptsView,
    AnswerResultPageView,
    CatalogAssetsView,
    CatalogAssetView,
    ClarifiedOutcomeView,
    ConsoleEnvelope,
    ConversationView,
    DashboardsView,
    DashboardView,
    DataProductsView,
    DataProductView,
    EvidenceView,
    ImpactView,
    InboxView,
    IncidentsView,
    JsonTuple,
    OperationView,
    RequestDetailView,
    RequesterRequestView,
    ReviewView,
    RunsView,
    SessionView,
    SetupView,
    WorkspaceView,
)
from ..errors import ConsoleUnavailable
from . import (
    RouteDependencies,
    correlation_id,
    envelope_response,
    normalized_https_origin,
    path_parameter,
    trusted_context,
)

_SESSION_RESPONSE = TypeAdapter(ConsoleEnvelope[SessionView])
_WORKSPACE_RESPONSE = TypeAdapter(ConsoleEnvelope[WorkspaceView])
_SETUP_RESPONSE = TypeAdapter(ConsoleEnvelope[SetupView])
_REVIEW_RESPONSE = TypeAdapter(ConsoleEnvelope[ReviewView])
_INBOX_RESPONSE = TypeAdapter(ConsoleEnvelope[InboxView])
_REQUEST_DETAIL_RESPONSE = TypeAdapter(ConsoleEnvelope[RequestDetailView])
_IMPACT_RESPONSE = TypeAdapter(ConsoleEnvelope[ImpactView])
_REQUESTER_REQUESTS_RESPONSE = TypeAdapter(ConsoleEnvelope[JsonTuple[RequesterRequestView]])
_CONVERSATION_RESPONSE = TypeAdapter(ConsoleEnvelope[ConversationView])
_CLARIFIED_OUTCOME_RESPONSE = TypeAdapter(ConsoleEnvelope[ClarifiedOutcomeView])
_DATA_PRODUCT_RESPONSE = TypeAdapter(ConsoleEnvelope[DataProductView])
_DATA_PRODUCTS_RESPONSE = TypeAdapter(ConsoleEnvelope[DataProductsView])
_RUNS_RESPONSE = TypeAdapter(ConsoleEnvelope[RunsView])
_INCIDENTS_RESPONSE = TypeAdapter(ConsoleEnvelope[IncidentsView])
_ACQUISITION_RECEIPTS_RESPONSE = TypeAdapter(ConsoleEnvelope[AcquisitionReceiptsView])
_ANSWER_RESULT_RESPONSE = TypeAdapter(ConsoleEnvelope[AnswerResultPageView])
_CATALOG_ASSET_RESPONSE = TypeAdapter(ConsoleEnvelope[CatalogAssetView])
_CATALOG_ASSETS_RESPONSE = TypeAdapter(ConsoleEnvelope[CatalogAssetsView])
_DASHBOARD_RESPONSE = TypeAdapter(ConsoleEnvelope[DashboardView])
_DASHBOARDS_RESPONSE = TypeAdapter(ConsoleEnvelope[DashboardsView])
_EVIDENCE_RESPONSE = TypeAdapter(ConsoleEnvelope[EvidenceView])
_OPERATION_RESPONSE = TypeAdapter(ConsoleEnvelope[OperationView])
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_routes(dependencies: RouteDependencies) -> list[Route]:
    async def session(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        token = dependencies.csrf_tokens.issue(context)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_session(context, token),
            _SESSION_RESPONSE,
        )

    async def workspace(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_workspace(context),
            _WORKSPACE_RESPONSE,
        )

    async def setup(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request, dependencies, dependencies.backend.get_setup(context), _SETUP_RESPONSE
        )

    async def review(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        review_id = path_parameter(request, "review_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_review(context, review_id),
            _REVIEW_RESPONSE,
        )

    async def inbox(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request, dependencies, dependencies.backend.get_inbox(context), _INBOX_RESPONSE
        )

    async def request_detail(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_request_detail(context, request_id),
            _REQUEST_DETAIL_RESPONSE,
        )

    async def request_impact(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_request_impact(context, request_id),
            _IMPACT_RESPONSE,
        )

    async def requester_requests(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_requester_requests(context),
            _REQUESTER_REQUESTS_RESPONSE,
        )

    async def incidents(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_incidents(context),
            _INCIDENTS_RESPONSE,
        )

    async def conversation(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_conversation(context, request_id),
            _CONVERSATION_RESPONSE,
        )

    async def answer_result(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        cursor = request.query_params.get("cursor")
        raw_page_size = request.query_params.get("page_size", "100")
        try:
            page_size = int(raw_page_size)
        except ValueError as error:
            from ..errors import ConsoleInvalidRequest

            raise ConsoleInvalidRequest(
                code="invalid_page_size",
                safe_message="Page size must be an integer.",
                recovery_action="correct_input",
                field="page_size",
            ) from error
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_answer_result(
                context, request_id, cursor=cursor, page_size=page_size
            ),
            _ANSWER_RESULT_RESPONSE,
        )

    async def answer_result_download(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        content = dependencies.backend.download_answer_result(context, request_id)
        response = StreamingResponse(iter((content.body,)), media_type=content.media_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{content.filename}"'
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def clarified_outcome(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        request_id = path_parameter(request, "request_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_clarified_outcome(context, request_id),
            _CLARIFIED_OUTCOME_RESPONSE,
        )

    async def data_product(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        data_product_id = path_parameter(request, "data_product_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_data_product(context, data_product_id),
            _DATA_PRODUCT_RESPONSE,
        )

    async def data_products(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_data_products(context),
            _DATA_PRODUCTS_RESPONSE,
        )

    async def runs(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request, dependencies, dependencies.backend.get_runs(context), _RUNS_RESPONSE
        )

    async def acquisition_receipts(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_acquisition_receipts(context),
            _ACQUISITION_RECEIPTS_RESPONSE,
        )

    async def catalog_asset(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        asset_ref = path_parameter(request, "asset_ref")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_catalog_asset(context, asset_ref),
            _CATALOG_ASSET_RESPONSE,
        )

    async def catalog_assets(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_catalog_assets(context),
            _CATALOG_ASSETS_RESPONSE,
        )

    async def dashboard(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        dashboard_ref = path_parameter(request, "dashboard_ref")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_dashboard(context, dashboard_ref),
            _DASHBOARD_RESPONSE,
        )

    async def dashboards(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_dashboards(context),
            _DASHBOARDS_RESPONSE,
        )

    async def evidence(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        evidence_ref = path_parameter(request, "evidence_ref")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_evidence(context, evidence_ref),
            _EVIDENCE_RESPONSE,
        )

    async def operation(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        operation_id = path_parameter(request, "operation_id")
        return envelope_response(
            request,
            dependencies,
            dependencies.backend.get_operation(context, operation_id),
            _OPERATION_RESPONSE,
        )

    async def preview(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        preview_ref = path_parameter(request, "preview_ref")
        content = dependencies.backend.get_preview(context, preview_ref)
        if content.media_type != "image/png" or not content.body.startswith(_PNG_SIGNATURE):
            raise ConsoleUnavailable(
                code="unsafe_preview_response",
                safe_message="The preview is unavailable.",
                recovery_action="none",
            )
        response = Response(content.body, media_type=content.media_type)
        response.headers["X-Correlation-ID"] = correlation_id(request)
        response.headers["X-Heinzel-Data-Provenance"] = dependencies.provenance
        return response

    async def link(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        link_ref = path_parameter(request, "link_ref")
        authorized_link = dependencies.backend.authorize_link(context, link_ref)
        location = authorized_link.location
        try:
            parsed_location = urlsplit(location)
        except ValueError:
            parsed_location = None
        is_relative_path = bool(
            parsed_location is not None
            and location.startswith("/")
            and not location.startswith("//")
            and "\\" not in location
            and not any(character.isspace() for character in location)
            and "#" not in location
            and not parsed_location.scheme
            and not parsed_location.netloc
        )
        target_origin = normalized_https_origin(location)
        is_configured_external_target = bool(
            parsed_location is not None
            and "#" not in location
            and target_origin is not None
            and target_origin == dependencies.managed_link_origin
        )
        if not is_relative_path and not is_configured_external_target:
            raise ConsoleUnavailable(
                code="unsafe_link_response",
                safe_message="The authorized link is unavailable.",
                recovery_action="none",
            )
        response = RedirectResponse(location, status_code=307)
        response.headers["X-Correlation-ID"] = correlation_id(request)
        response.headers["X-Heinzel-Data-Provenance"] = dependencies.provenance
        return response

    return [
        Route("/api/v1/session", session, methods=["GET"]),
        Route("/api/v1/workspace", workspace, methods=["GET"]),
        Route("/api/v1/setup", setup, methods=["GET"]),
        Route("/api/v1/reviews/{review_id}", review, methods=["GET"]),
        Route("/api/v1/inbox", inbox, methods=["GET"]),
        Route("/api/v1/inbox/{request_id}", request_detail, methods=["GET"]),
        Route("/api/v1/inbox/{request_id}/impact", request_impact, methods=["GET"]),
        Route("/api/v1/requests/mine", requester_requests, methods=["GET"]),
        Route("/api/v1/requests/{request_id}/conversation", conversation, methods=["GET"]),
        Route("/api/v1/requests/{request_id}/result", answer_result, methods=["GET"]),
        Route(
            "/api/v1/requests/{request_id}/result.csv",
            answer_result_download,
            methods=["GET"],
        ),
        Route(
            "/api/v1/requests/{request_id}/clarified-outcome",
            clarified_outcome,
            methods=["GET"],
        ),
        Route("/api/v1/data-products", data_products, methods=["GET"]),
        Route("/api/v1/data-products/{data_product_id}", data_product, methods=["GET"]),
        Route("/api/v1/runs", runs, methods=["GET"]),
        Route("/api/v1/incidents", incidents, methods=["GET"]),
        Route("/api/v1/acquisition-receipts", acquisition_receipts, methods=["GET"]),
        Route("/api/v1/catalog", catalog_assets, methods=["GET"]),
        Route("/api/v1/catalog/{asset_ref}", catalog_asset, methods=["GET"]),
        Route("/api/v1/dashboards", dashboards, methods=["GET"]),
        Route("/api/v1/dashboards/{dashboard_ref}", dashboard, methods=["GET"]),
        Route("/api/v1/evidence/{evidence_ref}", evidence, methods=["GET"]),
        Route("/api/v1/operations/{operation_id}", operation, methods=["GET"]),
        Route("/api/v1/previews/{preview_ref}", preview, methods=["GET"]),
        Route("/api/v1/links/{link_ref}", link, methods=["GET"]),
    ]
