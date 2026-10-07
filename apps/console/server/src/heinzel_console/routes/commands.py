from __future__ import annotations

import json
from collections.abc import Callable
from typing import Never

from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ..auth import TrustedActorContext
from ..contracts import (
    AccessLifecycleView,
    AccessRevocationCommand,
    AcquisitionReceiptView,
    AcquisitionRunNowCommand,
    AdmissionCommand,
    ClarifiedOutcomeAcceptanceCommand,
    ClarifiedOutcomeView,
    ConsoleEnvelope,
    ConversationMessageCommand,
    ConversationView,
    CreateRequestCommand,
    DashboardPublicationCommand,
    DecisionCommand,
    IncidentRecoveryCommand,
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
    SetupView,
    SourceRegistrationCommand,
    WarehouseBindingCommand,
)
from ..errors import ConsoleInvalidRequest
from . import (
    RouteDependencies,
    envelope_response,
    path_parameter,
    trusted_context,
)

_OPERATION_RESPONSE = TypeAdapter(ConsoleEnvelope[OperationView])
_INCIDENT_RESPONSE = TypeAdapter(ConsoleEnvelope[IncidentView])
_REVIEW_RESPONSE = TypeAdapter(ConsoleEnvelope[ReviewView])
_REQUEST_DETAIL_RESPONSE = TypeAdapter(ConsoleEnvelope[RequestDetailView])
_REQUESTER_REQUEST_RESPONSE = TypeAdapter(ConsoleEnvelope[RequesterRequestView])
_CONVERSATION_RESPONSE = TypeAdapter(ConsoleEnvelope[ConversationView])
_CLARIFIED_OUTCOME_RESPONSE = TypeAdapter(ConsoleEnvelope[ClarifiedOutcomeView])
_SETUP_RESPONSE = TypeAdapter(ConsoleEnvelope[SetupView])
_PRODUCT_INTENT_APPROVAL_RESPONSE = TypeAdapter(ConsoleEnvelope[ProductIntentApprovalView])
_ACQUISITION_RECEIPT_RESPONSE = TypeAdapter(ConsoleEnvelope[AcquisitionReceiptView])
_ACCESS_LIFECYCLE_RESPONSE = TypeAdapter(ConsoleEnvelope[AccessLifecycleView])


def _invalid_request(*, code: str, safe_message: str, field: str | None = None) -> Never:
    raise ConsoleInvalidRequest(
        code=code,
        safe_message=safe_message,
        recovery_action="correct_input",
        field=field,
    )


async def _parse_command[CommandModel: BaseModel](
    request: Request, model: type[CommandModel]
) -> CommandModel:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        _invalid_request(code="invalid_request", safe_message="The JSON body is invalid.")
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        first_error = error.errors(include_input=False, include_url=False)[0]
        location = first_error.get("loc", ())
        field = str(location[0]) if location else None
        _invalid_request(
            code="invalid_request",
            safe_message="One or more command fields are invalid.",
            field=field,
        )


def _validate_command_request(
    request: Request,
    dependencies: RouteDependencies,
    context: TrustedActorContext,
) -> str:
    origin = request.headers.get("origin")
    if origin != dependencies.allowed_origin:
        _invalid_request(
            code="same_origin_required",
            safe_message="The command must come from the same origin.",
        )

    csrf_token = request.headers.get("x-csrf-token")
    if csrf_token is None:
        _invalid_request(code="csrf_required", safe_message="A CSRF token is required.")
    if not dependencies.csrf_tokens.matches(context, csrf_token):
        _invalid_request(code="csrf_invalid", safe_message="The CSRF token is invalid.")

    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key is None or not (8 <= len(idempotency_key) <= 256):
        _invalid_request(
            code="idempotency_key_required",
            safe_message="A valid Idempotency-Key is required.",
        )
    return idempotency_key


async def _invoke_command[Result: BaseModel](
    dependencies: RouteDependencies,
    context: TrustedActorContext,
    idempotency_key: str,
    command: Callable[[], Result],
) -> Result:
    def serialized_call() -> Result:
        with dependencies.in_flight_commands.serialize(context, idempotency_key):
            return command()

    return await run_in_threadpool(serialized_call)


def _command_status(result: BaseModel) -> int:
    if isinstance(result, OperationView) and result.state in {
        "accepted",
        "running",
        "outcome_unknown",
    }:
        return 202
    return 200


def command_routes(dependencies: RouteDependencies) -> list[Route]:
    async def acquisition_run_now(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, AcquisitionRunNowCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.run_acquisition_now(context, command),
        )
        return envelope_response(request, dependencies, result, _ACQUISITION_RECEIPT_RESPONSE)

    async def warehouse_binding(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, WarehouseBindingCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.confirm_warehouse_binding(context, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _OPERATION_RESPONSE,
            status_code=_command_status(result),
        )

    async def process_package(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, ProcessPackageCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.submit_process_package(context, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _OPERATION_RESPONSE,
            status_code=_command_status(result),
        )

    async def register_source(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, SourceRegistrationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.register_source(context, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _OPERATION_RESPONSE,
            status_code=_command_status(result),
        )

    async def publish_dashboard(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, DashboardPublicationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.publish_dashboard(context, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _OPERATION_RESPONSE,
            status_code=_command_status(result),
        )

    async def review_decision(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        review_id = path_parameter(request, "review_id")
        command = await _parse_command(request, DecisionCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.decide_review(context, review_id, command),
        )
        return envelope_response(request, dependencies, result, _REVIEW_RESPONSE)

    async def request_decision(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, DecisionCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.decide_request(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUEST_DETAIL_RESPONSE)

    async def product_intent_approval(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, ProductIntentApprovalCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.approve_product_intent(context, request_id, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _PRODUCT_INTENT_APPROVAL_RESPONSE,
        )

    async def request_clarification(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, RequestClarificationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.clarify_request(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUEST_DETAIL_RESPONSE)

    async def request_proposal(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, ProposalPreparationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.prepare_request_proposal(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUEST_DETAIL_RESPONSE)

    async def request_proposal_submission(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, ProposalPreparationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.submit_request_proposal(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUEST_DETAIL_RESPONSE)

    async def request_admission(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, AdmissionCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.admit_request(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUEST_DETAIL_RESPONSE)

    async def create_request(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, CreateRequestCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.create_request(context, command),
        )
        return envelope_response(request, dependencies, result, _REQUESTER_REQUEST_RESPONSE)

    async def withdraw_request(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, RequestWithdrawalCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.withdraw_request(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _REQUESTER_REQUEST_RESPONSE)

    async def revoke_access(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, AccessRevocationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.revoke_access(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _ACCESS_LIFECYCLE_RESPONSE)

    async def append_message(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, ConversationMessageCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.append_conversation_message(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _CONVERSATION_RESPONSE)

    async def accept_outcome(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        request_id = path_parameter(request, "request_id")
        command = await _parse_command(request, ClarifiedOutcomeAcceptanceCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.accept_clarified_outcome(context, request_id, command),
        )
        return envelope_response(request, dependencies, result, _CLARIFIED_OUTCOME_RESPONSE)

    async def retry_operation(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        operation_id = path_parameter(request, "operation_id")
        command = await _parse_command(request, RetryOperationCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.retry_operation(context, operation_id, command),
        )
        return envelope_response(
            request,
            dependencies,
            result,
            _OPERATION_RESPONSE,
            status_code=_command_status(result),
        )

    async def recover_incident(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        incident_id = path_parameter(request, "incident_id")
        command = await _parse_command(request, IncidentRecoveryCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.recover_incident(
                context, incident_id, command, idempotency_key=key
            ),
        )
        return envelope_response(request, dependencies, result, _INCIDENT_RESPONSE)

    async def reset(request: Request) -> Response:
        context = trusted_context(request, dependencies)
        key = _validate_command_request(request, dependencies, context)
        command = await _parse_command(request, ResetCommand)
        result = await _invoke_command(
            dependencies,
            context,
            key,
            lambda: dependencies.backend.reset(context, command),
        )
        return envelope_response(request, dependencies, result, _SETUP_RESPONSE)

    routes = [
        Route("/api/v1/acquisitions/run-now", acquisition_run_now, methods=["POST"]),
        Route("/api/v1/inbox/{request_id}/clarification", request_clarification, methods=["POST"]),
        Route("/api/v1/inbox/{request_id}/proposal", request_proposal, methods=["POST"]),
        Route(
            "/api/v1/inbox/{request_id}/proposal/submission",
            request_proposal_submission,
            methods=["POST"],
        ),
        Route("/api/v1/setup/warehouse-binding", warehouse_binding, methods=["POST"]),
        Route("/api/v1/setup/process-packages", process_package, methods=["POST"]),
        Route("/api/v1/setup/sources", register_source, methods=["POST"]),
        Route("/api/v1/dashboards/publications", publish_dashboard, methods=["POST"]),
        Route("/api/v1/reviews/{review_id}/decisions", review_decision, methods=["POST"]),
        Route("/api/v1/inbox/{request_id}/decisions", request_decision, methods=["POST"]),
        Route(
            "/api/v1/inbox/{request_id}/product-intent/approval",
            product_intent_approval,
            methods=["POST"],
        ),
        Route("/api/v1/inbox/{request_id}/admission", request_admission, methods=["POST"]),
        Route("/api/v1/requests", create_request, methods=["POST"]),
        Route("/api/v1/requests/{request_id}/conversation", append_message, methods=["POST"]),
        Route(
            "/api/v1/requests/{request_id}/clarified-outcome/acceptance",
            accept_outcome,
            methods=["POST"],
        ),
        Route("/api/v1/requests/{request_id}/withdrawal", withdraw_request, methods=["POST"]),
        Route("/api/v1/requests/{request_id}/access/revocation", revoke_access, methods=["POST"]),
        Route("/api/v1/operations/{operation_id}/retry", retry_operation, methods=["POST"]),
        Route(
            "/api/v1/incidents/{incident_id}/recovery",
            recover_incident,
            methods=["POST"],
        ),
    ]
    if dependencies.backend.fixture_mode:
        routes.append(Route("/api/v1/demo/reset", reset, methods=["POST"]))
    return routes
