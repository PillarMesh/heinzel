from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from string import ascii_lowercase, digits
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

# The default port of each scheme a browser names an origin with, which it then leaves out.
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HTTPS_ONLY = frozenset({"https"})
# Letters and digits for a name, `-` and `.` for its labels, `:` for an IPv6 literal.
_HOSTNAME_CHARACTERS = frozenset(ascii_lowercase + digits + "-.:")


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


def canonical_browser_origin(value: str, *, schemes: frozenset[str]) -> str | None:
    """The origin a browser would send for `value`, or `None` when no browser sends one.

    One implementation serves every same-origin decision in this package: the managed link
    origin below, and the origin the `heinzel-console` command accepts commands from. Both
    are compared to an `Origin` header literally, so both must agree on the spelling a
    browser produces - notably that the default port of the scheme is left out (RFC 6454
    section 6.1), so `http://host:80` and `https://host:443` are named without it.

    A credential, a path, a query, a fragment, a backslash, whitespace, or a character
    outside a hostname's ASCII alphabet has no browser spelling at all, and is refused
    rather than adjusted into one.
    """
    if "\\" in value or not value.isascii() or not value.isprintable():
        return None
    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        # Raises for a port that is empty, negative, out of range or not a number - none of
        # which the authority's own text reveals when it is compared as written.
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in schemes
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    hostname = parsed.hostname.lower()
    # A percent escape, a NUL or anything else outside this alphabet is not a host a browser
    # resolves, and a caller may repeat what is returned here in a log.
    if not hostname or set(hostname) - _HOSTNAME_CHARACTERS:
        return None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port_suffix = "" if port in (None, _DEFAULT_PORTS[parsed.scheme]) else f":{port}"
    return f"{parsed.scheme}://{hostname}{port_suffix}"


def normalized_https_origin(value: str) -> str | None:
    return canonical_browser_origin(value, schemes=_HTTPS_ONLY)


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
    "canonical_browser_origin",
    "correlation_id",
    "envelope_response",
    "normalized_https_origin",
    "path_parameter",
    "trusted_context",
]
