from __future__ import annotations

import ipaddress
import re
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
# Letters and digits for a name, `-`, `.` and `_` for its labels, `:` for an IPv6 literal.
# `_` is a legal byte in a DNS label and is what a Docker Compose service is usually named,
# so a console published beside one can be configured with that host.
_HOSTNAME_CHARACTERS = frozenset(ascii_lowercase + digits + "-._:")
# One label of a host a browser reads as an IPv4 address: a decimal or `0x` hexadecimal
# number. Lowercase because `urlsplit` has already lowercased the host.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]+")


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


def _is_address_literal(hostname: str) -> bool:
    """Whether `hostname` is an address spelled as a name rather than a name.

    A browser reads a host whose every label is a number - decimal or `0x` hexadecimal - as
    an IPv4 address, and no resolvable name is spelled that way, because no top-level domain
    is a number.
    """
    return all(_NUMERIC_LABEL.fullmatch(label) for label in hostname.split("."))


def _canonical_address(hostname: str) -> str | None:
    """The one spelling a browser gives the address `hostname`, or `None` when it is none.

    `http://0177.0.0.1` and `http://127.1` are both sent as `http://127.0.0.1`, and
    `http://[2001:db8:0:0:0:0:0:1]` as `http://[2001:db8::1]`, so accepting a second
    spelling as the configured origin would serve a console whose every command is refused.
    """
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # `ipaddress` spells an IPv4-mapped address with a dotted quad, `::ffff:127.0.0.1`,
        # where a browser spells every IPv6 address in hextets, `::ffff:7f00:1`. Deriving
        # the browser's spelling would take a second IPv6 serializer, so this one family is
        # left exactly as written: both its spellings are accepted, as both were before.
        return hostname
    return str(address)


def canonical_browser_origin(value: str, *, schemes: frozenset[str]) -> str | None:
    """The origin a browser would send for `value`, or `None` when no browser sends one.

    One implementation serves every same-origin decision in this package: the managed link
    origin below, and the origin the `heinzel-console` command accepts commands from. Both
    are compared to an `Origin` header literally, so both must agree on the spelling a
    browser produces - notably that the default port of the scheme is left out (RFC 6454
    section 6.1), so `http://host:80` and `https://host:443` are named without it, and that
    an address is spelled the one way a browser spells it, so `http://[2001:db8:0:0:0:0:0:1]`
    is named `http://[2001:db8::1]`.

    Refused, because no browser sends such an origin at all: a credential, a backslash,
    whitespace, a scheme this function does not know or `schemes` does not allow, a missing
    host, an empty or otherwise unusable port, an address literal that is not an address,
    and a character outside a hostname's ASCII alphabet - which covers a percent escape, a
    NUL, and a name a browser would send as punycode.

    A path, a query or a fragment is *discarded*, not refused: the caller is given the
    origin of `value`, which is what the managed link check needs, because it hands this
    function the whole link URL, path included. A caller that must reject them checks them
    itself, as `create_app` does.
    """
    if "\\" in value or not value.isascii() or not value.isprintable():
        return None
    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        # Raises for a port that is negative, out of range or not a number - none of which
        # the authority's own text reveals when it is compared as written. An empty port is
        # not one of them: `urlsplit("http://host:").port` is `None`, so the authority's own
        # text is what catches it below.
        port = parsed.port
    except ValueError:
        return None
    # `None` for any scheme but `http` and `https`, whose default ports are the only ones
    # this function knows; a caller may name others in `schemes`, and gets no origin for
    # them rather than a `KeyError` from the port below.
    default_port = _DEFAULT_PORTS.get(parsed.scheme)
    if (
        parsed.scheme not in schemes
        or default_port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    # `http://host:` is a port no browser sends, and `urlsplit` reports it as no port at
    # all. The credential an authority may carry is refused above, so this is host and port
    # alone and may be read here.
    if parsed.netloc.endswith(":"):
        return None
    # Already lowercased by `urlsplit`, which owns that part of the spelling.
    hostname = parsed.hostname
    # A percent escape, a NUL or anything else outside this alphabet is not a host a browser
    # resolves, and a caller may repeat what is returned here in a log.
    if not hostname or set(hostname) - _HOSTNAME_CHARACTERS:
        return None
    if parsed.netloc.startswith("[") or _is_address_literal(hostname):
        canonical_address = _canonical_address(hostname)
        # A literal that is no address at all is no host a browser reaches either.
        if canonical_address is None:
            return None
        hostname = canonical_address
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port_suffix = "" if port in (None, default_port) else f":{port}"
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
