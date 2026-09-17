from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

import psycopg
from psycopg import pq

from .warehouse_protocol import connect_denial_probe

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_PROBED_SSL_MODES = frozenset({"disable", "prefer", "verify-full"})
_AUTHORIZATION_REJECTIONS = (
    psycopg.errors.InvalidAuthorizationSpecification,
    psycopg.errors.InvalidPassword,
)


class _ProbeConnection(Protocol):
    def close(self) -> None: ...


type StartupDenialProbe = Callable[..., _ProbeConnection]


def default_startup_denial_probe(
    *,
    connect: object | None,
    probe: StartupDenialProbe | None,
) -> StartupDenialProbe | None:
    # A substituted driver reaches no server, so it is probed only when a probe is supplied.
    if probe is not None:
        return probe
    return connect_denial_probe if connect is None else None


def connect_attributing_startup_denial[ConnectionT](
    connect: Callable[[str], ConnectionT],
    conninfo: str,
    *,
    probe: StartupDenialProbe | None,
) -> ConnectionT:
    try:
        return connect(conninfo)
    except psycopg.OperationalError as error:
        if probe is None:
            raise
        raise recover_startup_denial(error, conninfo, probe=probe) from None


def recover_startup_denial(
    error: psycopg.OperationalError,
    conninfo: str,
    *,
    probe: StartupDenialProbe = connect_denial_probe,
) -> psycopg.Error:
    """Return the structured authorization rejection behind a SQLSTATE-less startup failure.

    libpq surfaces a startup rejection such as FATAL 28P01 only as localized message text, so a
    rejected credential is indistinguishable from an unavailable server. The probe repeats the
    startup handshake with exactly the parameters libpq resolved and retains the server's SQLSTATE.
    Only a structured 28-class rejection replaces the original error; every other outcome returns
    it unchanged so transport and availability failures keep their retryable classification.
    """
    if error.sqlstate is not None:
        return error
    parameters = _probe_parameters(conninfo)
    if parameters is None:
        return error
    try:
        connection = probe(**parameters)
    except _AUTHORIZATION_REJECTIONS as rejection:
        return rejection
    except (psycopg.Error, OSError, ValueError):
        # The probe is diagnostic only; its own failure is not evidence about the credential.
        return error
    with suppress(Exception):
        connection.close()
    return error


def _probe_parameters(conninfo: str) -> dict[str, object] | None:
    resolved = _resolve_like_libpq(conninfo)
    if resolved is None:
        return None
    # Decline every connection whose negotiation the probe cannot reproduce exactly. Probing with
    # weaker transport security than libpq used would expose the credential on a channel the
    # operator never configured.
    if any(keyword in resolved for keyword in ("service", "hostaddr", "require_auth")):
        return None
    if resolved.get("gssencmode", "disable") != "disable":
        return None
    if resolved.get("channel_binding", "prefer") == "require":
        return None
    if resolved.get("sslnegotiation", "postgres") != "postgres":
        return None
    host = resolved.get("host", "")
    if not host or "," in host or host.startswith(("/", "@")):
        return None
    user = resolved.get("user", "")
    password = resolved.get("password", "")
    if not user or not password:
        return None
    try:
        port = int(resolved.get("port", "5432"))
        timeout_seconds = int(resolved.get("connect_timeout", "0"))
    except ValueError:
        return None
    if not 1 <= port <= 65_535:
        return None
    sslmode = resolved.get("sslmode", "prefer")
    if sslmode not in _PROBED_SSL_MODES:
        return None
    root_certificate = resolved.get("sslrootcert")
    if sslmode == "verify-full" and root_certificate in (None, "system"):
        return None
    return {
        "dbname": resolved.get("dbname") or user,
        "user": user,
        "password": password,
        "host": host,
        "port": port,
        "sslmode": sslmode,
        "connect_timeout": (
            float(timeout_seconds) if timeout_seconds > 0 else _DEFAULT_CONNECT_TIMEOUT_SECONDS
        ),
        "sslrootcert": root_certificate if sslmode == "verify-full" else None,
        "sslcert": resolved.get("sslcert") if sslmode == "verify-full" else None,
        "sslkey": resolved.get("sslkey") if sslmode == "verify-full" else None,
    }


def _resolve_like_libpq(conninfo: str) -> dict[str, str] | None:
    try:
        explicit = pq.Conninfo.parse(conninfo.encode("utf-8"))
        defaults = pq.Conninfo.get_defaults()
    except (psycopg.Error, UnicodeError):
        return None
    try:
        resolved = {
            option.keyword.decode("utf-8"): option.val.decode("utf-8")
            for option in defaults
            if option.val is not None
        }
        resolved.update(
            (option.keyword.decode("utf-8"), option.val.decode("utf-8"))
            for option in explicit
            if option.val is not None
        )
    except UnicodeError:
        return None
    return resolved
