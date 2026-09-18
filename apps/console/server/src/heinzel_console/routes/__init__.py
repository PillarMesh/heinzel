from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, TypeAdapter
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from ..auth import InFlightCommandKeys, SessionCsrfTokens, TrustedActorContext
from ..backend import ConsoleBackend
from ..contracts import ApiMeta, DataProvenance
from ..errors import ConsoleInvalidRequest, ConsoleUnauthenticated

type ContextProvider = Callable[[Request], TrustedActorContext | None]


@dataclass(frozen=True, slots=True)
class RouteDependencies:
    backend: ConsoleBackend
    context_provider: ContextProvider
    csrf_tokens: SessionCsrfTokens
    in_flight_commands: InFlightCommandKeys
    allowed_origin: str
    managed_link_origin: str | None = None

    @property
    def provenance(self) -> DataProvenance:
        return "demo_fixture" if self.backend.fixture_mode else "governed_local"


def normalized_https_origin(value: str) -> str | None:
    if "\\" in value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = parsed.hostname
    if not hostname.isascii():
        return None
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port_suffix = "" if port in (None, 443) else f":{port}"
    return f"https://{hostname}{port_suffix}"


def correlation_id(request: Request) -> str:
    existing = getattr(request.state, "correlation_id", None)
    if isinstance(existing, str):
        return existing
    generated = f"correlation-{secrets.token_hex(16)}"
    request.state.correlation_id = generated
    return generated


def trusted_context(request: Request, dependencies: RouteDependencies) -> TrustedActorContext:
    context = dependencies.context_provider(request)
    if context is None:
        raise ConsoleUnauthenticated(
            code="unauthenticated",
            safe_message="A trusted session is required.",
            recovery_action="reauthenticate",
        )
    return context


def path_parameter(request: Request, name: str) -> str:
    value = request.path_params.get(name)
    if not isinstance(value, str) or not value:
        raise ConsoleInvalidRequest(
            code="invalid_request",
            safe_message="A required path value is invalid.",
            recovery_action="correct_input",
            field=name,
        )
    return value


def _wire_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, tuple):
        return tuple(_wire_value(item) for item in value)
    return value


def envelope_response[EnvelopeModel: BaseModel](
    request: Request,
    dependencies: RouteDependencies,
    data: object,
    envelope_adapter: TypeAdapter[EnvelopeModel],
    *,
    status_code: int = 200,
) -> JSONResponse:
    meta = ApiMeta(
        data_provenance=dependencies.provenance,
        correlation_id=correlation_id(request),
    )
    envelope = envelope_adapter.validate_python(
        {"meta": meta.model_dump(mode="python"), "data": _wire_value(data)}
    )
    response = JSONResponse(
        envelope_adapter.dump_python(envelope, mode="json"), status_code=status_code
    )
    response.headers["X-Correlation-ID"] = meta.correlation_id
    response.headers["X-Heinzel-Data-Provenance"] = meta.data_provenance
    return response


def build_routes(dependencies: RouteDependencies) -> list[BaseRoute]:
    from .commands import command_routes
    from .read import read_routes

    routes: list[BaseRoute] = list(read_routes(dependencies))
    routes.extend(command_routes(dependencies))
    return routes


__all__ = [
    "ContextProvider",
    "RouteDependencies",
    "build_routes",
    "correlation_id",
    "envelope_response",
    "normalized_https_origin",
    "path_parameter",
    "trusted_context",
]
