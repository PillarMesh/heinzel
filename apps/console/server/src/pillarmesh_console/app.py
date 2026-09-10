from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Match, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Scope

from .auth import InFlightCommandKeys, SessionCsrfTokens, TrustedActorContext
from .backend import ConsoleBackend
from .contracts import ApiError, ApiMeta, ConsoleErrorEnvelope, DataProvenance
from .errors import (
    ConsoleConflict,
    ConsoleError,
    ConsoleInvalidRequest,
    ConsoleNotFound,
    ConsoleUnauthenticated,
    ConsoleUnavailable,
)
from .fixture_backend import FixtureConsoleBackend
from .routes import ContextProvider, RouteDependencies, build_routes, correlation_id

_LOGGER = logging.getLogger(__name__)
_CONTENT_SECURITY_POLICY = "default-src 'self'; img-src 'self'; frame-src 'self'"
_DEFAULT_ALLOWED_ORIGIN = "http://127.0.0.1:8000"
type RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


class _RequestSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, data_provenance: DataProvenance) -> None:
        super().__init__(app)
        self._data_provenance = data_provenance

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.correlation_id = f"correlation-{secrets.token_hex(16)}"
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Correlation-ID"] = correlation_id(request)
        response.headers["X-PillarMesh-Data-Provenance"] = self._data_provenance
        return response


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _default_context(_: Request) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id="tenant-primary",
        actor_id="actor-architect",
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-default-architect",
    )


def _error_status(error: ConsoleError) -> int:
    if isinstance(error, ConsoleUnauthenticated):
        return 401
    if isinstance(error, ConsoleNotFound):
        return 404
    if isinstance(error, ConsoleConflict):
        return 409
    if isinstance(error, ConsoleUnavailable):
        return 503
    if isinstance(error, ConsoleInvalidRequest):
        return 422
    return 500


class _BrowserAssets(StaticFiles):
    """Serve the compiled bundle, falling back to the shell for browser routes.

    React Router owns paths like `/inbox`, which exist in the browser and not on
    disk. Plain `StaticFiles` answers those with 404, so a deep link or a reload
    would break. Only a missing path falls back; a missing *asset* must still 404
    rather than return HTML, or a broken bundle reference would look like a page.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as missing:
            # StaticFiles signals a miss by raising, not by returning a 404.
            if missing.status_code != 404 or path.startswith("assets/"):
                raise
        return await super().get_response("index.html", scope)


def create_app(
    *,
    backend: ConsoleBackend | None = None,
    context_provider: ContextProvider | None = None,
    allowed_origin: str | None = None,
    dist_directory: Path | str | None = None,
) -> Starlette:
    """Build the console application.

    `dist_directory` serves the compiled browser bundle from this same origin, so
    a production-shaped local build proves the application and its API share one
    origin. It is mounted last, after every API route, and is absent unless a
    build directory is supplied or `PILLARMESH_CONSOLE_DIST` names one.

    `allowed_origin` follows `PILLARMESH_CONSOLE_ALLOWED_ORIGIN` when it is not
    given. The launch scripts advertise a configurable port, and an origin fixed
    at the default one leaves every read working while every command is refused
    `same_origin_required` - a misconfiguration that reads as a broken product.
    """
    selected_origin = (
        allowed_origin
        or os.environ.get("PILLARMESH_CONSOLE_ALLOWED_ORIGIN")
        or _DEFAULT_ALLOWED_ORIGIN
    )
    selected_backend = backend or FixtureConsoleBackend()
    dependencies = RouteDependencies(
        backend=selected_backend,
        context_provider=context_provider or _default_context,
        csrf_tokens=SessionCsrfTokens(),
        in_flight_commands=InFlightCommandKeys(),
        allowed_origin=selected_origin,
    )
    routes: list[BaseRoute] = [Route("/healthz", endpoint=_health, methods=["GET"])]
    api_routes = build_routes(dependencies)
    routes.extend(api_routes)

    async def unknown_api_path(request: Request) -> Response:
        # A known API path asked with the wrong method is still a method error, and names the
        # methods the path does allow, exactly as the router would have without this route.
        allowed = {
            method
            for route in api_routes
            if isinstance(route, Route) and route.matches(request.scope)[0] is Match.PARTIAL
            for method in route.methods or ()
        }
        if allowed:
            raise HTTPException(status_code=405, headers={"Allow": ", ".join(sorted(allowed))})
        # Anything else under /api is absent. Without this the browser shell would answer it
        # with 200 HTML, and an API client or monitor would read a missing endpoint as success.
        raise ConsoleNotFound()

    unknown_api_methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    routes.append(Route("/api", endpoint=unknown_api_path, methods=unknown_api_methods))
    routes.append(Route("/api/{path:path}", endpoint=unknown_api_path, methods=unknown_api_methods))
    configured_dist = dist_directory or os.environ.get("PILLARMESH_CONSOLE_DIST")
    if configured_dist is not None:
        resolved_dist = Path(configured_dist).resolve()
        if not resolved_dist.is_dir():
            raise ValueError("console dist directory does not exist")
        # Mounted last so no static path can shadow an API route.
        routes.append(Mount("/", app=_BrowserAssets(directory=resolved_dist, html=True)))
    app = Starlette(routes=routes)
    app.state.allowed_origin = selected_origin

    async def console_error_handler(request: Request, exception: Exception) -> JSONResponse:
        error = exception
        if not isinstance(error, ConsoleError):
            _LOGGER.error(
                "console_request_failed code=internal_error correlation_id=%s",
                correlation_id(request),
            )
            error = ConsoleError(
                code="internal_error",
                safe_message="The request could not be completed.",
                recovery_action="contact_support",
            )
        envelope = ConsoleErrorEnvelope(
            meta=ApiMeta(
                data_provenance=dependencies.provenance,
                correlation_id=correlation_id(request),
            ),
            error=ApiError(
                code=error.code,
                safe_message=error.safe_message,
                recovery_action=error.recovery_action,
                field=error.field,
            ),
        )
        return JSONResponse(envelope.model_dump(mode="json"), status_code=_error_status(error))

    app.add_exception_handler(ConsoleError, console_error_handler)
    app.add_exception_handler(Exception, console_error_handler)
    app.add_middleware(_RequestSecurityMiddleware, data_provenance=dependencies.provenance)
    return app
