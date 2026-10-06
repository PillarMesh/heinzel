"""The `heinzel-console` command: serve the demonstration console from a state directory.

This serves a demonstration, not a deployment. The console has no authentication — the browser
names its actor in a header — so the command binds a loopback host unless `--container` says
the published port of a container image is the network boundary instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Never

from .demo import ManagedWarehouseOption, build_demo_app
from .routes import canonical_browser_origin

type _SignalHandler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None

__all__ = [
    "announce_accepted_origin",
    "main",
    "parse_arguments",
    "require_bundle",
    "require_loopback",
    "resolve_managed_warehouse",
    "resolve_origin",
    "warn_when_unauthenticated",
]

_LOGGER = logging.getLogger(__name__)

# The hosts an unauthenticated console may bind.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

_LOOPBACK_HOST = "127.0.0.1"
# Only reachable behind `--container`. IPv4 is deliberate: a container image is meant to
# publish an IPv4 port (`127.0.0.1:8000:8000`) onto Docker's IPv4 bridge network.
_EVERY_INTERFACE = "0.0.0.0"
_DEFAULT_PORT = 8000

# The range a TCP port number can occupy. Zero is excluded along with out-of-range values:
# it would leave the kernel to choose the port, which no configured origin could then name.
_LOWEST_PORT = 1
_HIGHEST_PORT = 65535

_BROWSER_SCHEMES = frozenset({"http", "https"})

# Every signal that means "stop": `docker stop` sends SIGTERM, a Compose `stop_signal:` may
# name another, and a closing terminal sends SIGHUP. Each must leave the stores released.
_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

_ORIGIN_VARIABLE = "HEINZEL_CONSOLE_ALLOWED_ORIGIN"
_DIST_VARIABLE = "HEINZEL_CONSOLE_DIST"
# The warehouse the demonstration provisions, acquires from and answers over. Read from the
# environment and never from a command argument: it carries a password, and a process's arguments
# are readable by anything that can list processes. Absent, the console reports every answer
# capability as not delivered, which is the honest answer with no warehouse to answer from.
_WAREHOUSE_VARIABLE = "HEINZEL_DEMO_WAREHOUSE_DSN"
# The other way to have a warehouse: warehouse-control provisions one, by driving the Compose
# project the demonstration ships, and the console reports the binding it made. Opt-in rather
# than default because it needs a reachable Docker daemon -- and, from inside a container, that
# daemon's socket, which is host-level access. The variable above needs neither, so it stays the
# default. The two are exclusive; `DemoConsole` refuses both.
_WAREHOUSE_CONTROL_VARIABLE = "HEINZEL_DEMO_WAREHOUSE_CONTROL"
# What counts as selecting it. An unrecognised value is refused rather than read as off: a
# console that treated `HEINZEL_DEMO_WAREHOUSE_CONTROL=flase` as unset would start on the other
# path and report a capability the operator asked for as not delivered.
_AFFIRMATIVE = frozenset({"1", "true", "yes", "on"})
_NEGATIVE = frozenset({"0", "false", "no", "off"})

# Invalid configuration, matching argparse's own code for arguments it rejects.
_INVALID_CONFIGURATION = 2
# The console was configured correctly but its surroundings refused it.
_UNAVAILABLE = 1


class PrivateArgumentParser(argparse.ArgumentParser):
    """A parser whose failure never echoes the arguments it was given."""

    def error(self, message: str) -> Never:
        self.exit(2, "heinzel-console: error: command arguments are invalid\n")


def _port_number(text: str) -> int:
    """A port a socket can actually carry, so an impossible one never reaches `bind()`.

    Out of range, the value reaches `bind()` inside uvicorn and ends the command in an
    `OverflowError` traceback rather than the message argparse gives every other rejection.
    """
    port = int(text)
    if not _LOWEST_PORT <= port <= _HIGHEST_PORT:
        raise ValueError("a port must be between 1 and 65535")
    return port


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse `argv`, resolving `--container` into the host to bind.

    Abbreviation is off: argparse would otherwise accept `--c` for `--container`, so a typo
    could bind every interface instead of failing.
    """
    root = PrivateArgumentParser(prog="heinzel-console", allow_abbrev=False)
    commands = root.add_subparsers(required=True, metavar="command")
    serve = commands.add_parser("serve", help="Serve the demonstration console", allow_abbrev=False)
    serve.add_argument("--state-dir", required=True, type=Path, dest="state_dir")
    serve.add_argument("--port", default=_DEFAULT_PORT, type=_port_number)
    serve.add_argument(
        "--dist", type=Path, help="Compiled console bundle to serve alongside the API"
    )
    serve.add_argument(
        "--origin",
        help=(
            "Origin the browser uses, for example http://127.0.0.1:8000. A command whose "
            f"`Origin` header differs is refused. Follows {_ORIGIN_VARIABLE} when omitted."
        ),
    )
    serve.add_argument(
        "--no-seed",
        action="store_true",
        dest="no_seed",
        help="Start with an empty inbox instead of the demonstration's own question",
    )
    serve.add_argument(
        "--container",
        action="store_true",
        help=(
            "Bind every interface. Meant for the container image, where the published port is "
            "the network boundary. This console has no authentication, so nothing else should "
            "use it."
        ),
    )
    arguments = root.parse_args(argv)
    arguments.host = _EVERY_INTERFACE if arguments.container else _LOOPBACK_HOST
    return arguments


def require_loopback(host: str) -> str:
    """Refuse to serve an unauthenticated console off the machine."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            "the demonstration console has no authentication and may bind only to "
            f"a loopback host, not {host!r}"
        )
    return host


def _unspellable_reason(origin: str) -> str:
    """Why no browser sends `origin` in any spelling, in words an operator can act on.

    Only ever reached for a value `canonical_browser_origin` has already refused, so this
    explains that decision and never makes one: the most a branch here that falls behind it
    can do is name the wrong defect of a value that is refused either way.
    """
    scheme, separator, authority = origin.partition("://")
    if not separator:
        return "it does not begin with http:// or https://"
    if scheme.lower() not in _BROWSER_SCHEMES:
        return "its scheme is neither http nor https"
    if not authority:
        return "it names no host"
    if not origin.isascii():
        return (
            "its host is not ASCII, and a browser sends such a name in its punycode form, "
            "which begins xn--"
        )
    return (
        "its host carries a character no hostname has, is an address literal that is no "
        "address, or carries a port no browser sends"
    )


def _require_browser_origin(origin: str, *, source: str) -> str:
    """Refuse an origin no browser would send, rather than one the gate can never match.

    The gate compares the `Origin` header to this value literally, so a trailing slash, a
    path, an uppercase host, surrounding whitespace, or the scheme's default port spelled
    out each serve a console whose every page renders and whose every command is refused
    `same_origin_required`. Normalizing quietly would accept an origin nobody asked for, so
    the spelling a browser actually sends is required, and anything else is named and
    refused - by the one function that decides this for the whole package, so the command
    and the routes cannot disagree about what a browser sends.
    """
    if not origin.strip():
        raise ValueError(f"{source} was given no value: omit it or set {_ORIGIN_VARIABLE}")
    # The userinfo of a URL is a credential and must never be repeated here or in a log.
    # Checked before the value is parsed: `urlsplit` raises for some hosts, and the
    # `ValueError` it raises carries the whole authority, credential included, into `main`.
    if "@" in origin:
        raise ValueError(
            f"{source} must be a bare origin such as http://127.0.0.1:8000, carrying no credentials"
        )
    # Derived from the value given rather than the value itself: an origin is a network
    # address the operator chose, and without the spelling to use the refusal cannot be acted
    # on. A value carrying a credential is refused above, before this is built.
    expected = canonical_browser_origin(origin, schemes=_BROWSER_SCHEMES)
    if expected is None:
        # No browser spelling of this value exists, so there is none to name. The five
        # defects below are all correctable, and listing them for a value that has none of
        # them tells the operator to correct what is already correct.
        raise ValueError(
            f"{source} must be an origin a browser can send, for example "
            f"http://127.0.0.1:8000, but {_unspellable_reason(origin)}"
        )
    if expected != origin:
        raise ValueError(
            f"{source} must be exactly the origin the browser sends, with no trailing slash, "
            f"path, surrounding space, uppercase, or default port, for example {expected}"
        )
    return origin


def resolve_origin(*, origin: str | None, container: bool, port: int) -> str:
    """The origin the browser will send, which is never the address the server binds.

    Commands are refused unless their `Origin` header matches this exactly, so deriving it
    from the bind address would serve a console where every read works and every command
    fails. Under `--container` the bind address is `0.0.0.0`, which no browser ever sends,
    and the port the reader opens is the published one rather than this one — so there is
    nothing to derive it from and the command refuses to start.
    """
    if origin is not None:
        # An empty value is a mistake, not an omission: `--origin "$VARIABLE"` with the
        # variable unset would otherwise be returned verbatim, and `create_app` falls back
        # from it to a default origin, bypassing the refusal below.
        return _require_browser_origin(origin, source="--origin")
    configured = os.environ.get(_ORIGIN_VARIABLE)
    if configured:
        return _require_browser_origin(configured, source=_ORIGIN_VARIABLE)
    if container:
        raise ValueError(
            "a console bound to every interface cannot derive the origin the browser will "
            f"send: give --origin or set {_ORIGIN_VARIABLE}, for example "
            "http://127.0.0.1:8000"
        )
    # Through the same function the given values go through: `--port 80` is served on the
    # loopback host, and the browser then sends `http://127.0.0.1`, with no port at all.
    derived = canonical_browser_origin(f"http://{_LOOPBACK_HOST}:{port}", schemes=_BROWSER_SCHEMES)
    if derived is None:
        # Unreachable: the host is a literal and the port is validated by `_port_number`.
        raise ValueError(f"the origin of a console on port {port} could not be derived")
    return derived


def require_bundle(*, dist: Path | None, configured: str | None) -> Path | None:
    """Resolve the compiled bundle from `--dist` or the environment, refusing a path that
    holds no bundle rather than serving a blank page.

    Both settings are resolved here so that one code path validates them and the refusal can
    name the two places the path could have come from. `create_app` reads the variable too,
    but reports only that a dist directory does not exist, naming neither setting.
    """
    selected = dist
    if selected is None and configured and configured.strip():
        selected = Path(configured)
    if selected is not None and not selected.is_dir():
        raise ValueError(
            f"the compiled console bundle named by --dist or {_DIST_VARIABLE} is not a directory"
        )
    return selected


def resolve_managed_warehouse(configured: str | None) -> ManagedWarehouseOption | None:
    """Whether to provision through warehouse-control, from the one variable that says so.

    `None` is the demonstration as it has always been. A value is required to be one of the
    spellings below rather than judged truthy, because the two paths deploy differently -- one
    needs a Docker socket and the other does not -- and silently choosing the other one over a
    typo would report the capability the operator asked for as not delivered.
    """
    if configured is None or not configured.strip():
        return None
    selection = configured.strip().lower()
    if selection in _AFFIRMATIVE:
        return ManagedWarehouseOption()
    if selection in _NEGATIVE:
        return None
    raise ValueError(
        f"{_WAREHOUSE_CONTROL_VARIABLE} must be one of "
        f"{', '.join(sorted(_AFFIRMATIVE | _NEGATIVE))}, or unset"
    )


def warn_when_unauthenticated(*, host: str) -> None:
    """Say plainly, every time, that what is being served has no authentication."""
    _LOGGER.warning(
        "the demonstration console has no authentication: anyone who reaches %s acts as the "
        "architect, so it must not be exposed to a network",
        host,
    )


def announce_accepted_origin(*, origin: str) -> None:
    """Name the one origin whose commands are accepted.

    A browser opened anywhere else is refused `same_origin_required`, which names neither the
    expected nor the received origin, and the startup log otherwise holds only the bind
    address. A published port that differs from the configured origin then reads as a product
    whose every button is broken, with nothing pointing at the setting that fixes it.
    """
    _LOGGER.warning(
        "commands are accepted only from %s: a browser opened on any other origin renders "
        "every page and has every command refused, so set %s to the origin it will use",
        origin,
        _ORIGIN_VARIABLE,
    )


def _ignore_termination() -> None:
    """Hold every termination signal off the clean-up, whatever started it."""
    for number in _TERMINATION_SIGNALS:
        with suppress(ValueError):
            # Not the main thread, so the disposition is not this command's to change.
            signal.signal(number, signal.SIG_IGN)


@contextmanager
def _termination_raises_system_exit() -> Iterator[None]:
    """Make a termination signal leave the build and the server as an exception, so the
    stores are released.

    uvicorn restores the handlers it replaced and then re-raises the signal it caught. With
    SIGTERM's default disposition that kills the process inside `uvicorn.run`, and the
    clean-up below it never runs — which is precisely how a container is stopped. A stop that
    lands while the demonstration is still being seeded ends the same way, so this covers the
    whole lifetime rather than the server alone.

    SIGHUP and SIGQUIT are covered too: `docker stop` sends SIGTERM, but a Compose
    `stop_signal:` names whichever signal it likes, and a terminal that closes on a bare
    `heinzel-console serve` sends SIGHUP.
    """

    def _terminate(signal_number: int, frame: FrameType | None) -> Never:
        # A second signal must not abort the clean-up the first one started.
        _ignore_termination()
        raise SystemExit(128 + signal_number)

    previous: list[tuple[int, _SignalHandler]] = []
    try:
        for number in _TERMINATION_SIGNALS:
            previous.append((number, signal.signal(number, _terminate)))
    except ValueError:
        # Not the main thread, so the disposition is not this command's to change.
        _restore(previous)
        yield
        return
    try:
        yield
    finally:
        _restore(previous)


def _restore(handlers: list[tuple[int, _SignalHandler]]) -> None:
    for number, handler in reversed(handlers):
        signal.signal(number, handler)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    host: str = arguments.host
    warn_when_unauthenticated(host=host)

    import uvicorn

    # Installed before anything is opened and restored after everything is released, so a
    # stop that lands anywhere between those two points still releases the state directory.
    # SIGKILL cannot be handled at all, and SIGINT during the clean-up still interrupts it.
    with _termination_raises_system_exit():
        try:
            if not arguments.container:
                # Unreachable while `parse_arguments` is the only writer of `host`; it guards
                # the edit that adds a `--host` option.
                require_loopback(host)
            origin = resolve_origin(
                origin=arguments.origin, container=arguments.container, port=arguments.port
            )
            dist = require_bundle(dist=arguments.dist, configured=os.environ.get(_DIST_VARIABLE))
            app, close = build_demo_app(
                arguments.state_dir,
                seed=not arguments.no_seed,
                origin=origin,
                dist=dist,
                # Blank reads as absent: an unset variable and one set to the empty string are
                # the same statement, and passing "" on would reach psycopg as a DSN naming no
                # host at all.
                warehouse_dsn=os.environ.get(_WAREHOUSE_VARIABLE) or None,
                managed_warehouse=resolve_managed_warehouse(
                    os.environ.get(_WAREHOUSE_CONTROL_VARIABLE)
                ),
            )
        except ValueError as invalid:
            _LOGGER.error("the console cannot be configured: %s", invalid)
            return _INVALID_CONFIGURATION
        except RuntimeError as unavailable:
            # The warehouse was configured but could not be brought to a published product: a
            # warehouse that never accepted a connection, a missing `dbt`, a database that is not
            # empty, or a state directory and a warehouse that disagree. Each names what to do
            # about it. On the warehouse-control path it also covers a Docker daemon this process
            # cannot reach, which `ManagedWarehouseRefused` reports as the socket to mount rather
            # than as the classification the orchestrator recorded.
            _LOGGER.error("the demonstration's warehouse is not ready: %s", unavailable)
            return _UNAVAILABLE
        except OSError as refused:
            _LOGGER.error(
                "the console's state directory could not be opened: %s%s",
                # `strerror` is absent on an `OSError` raised by hand, and formatting the
                # exception itself would print whatever its message happens to carry.
                refused.strerror or type(refused).__name__,
                # The operator typed this path, and two candidate directories are otherwise
                # guesswork. It is not an argument echoed back by the parser.
                f" ({refused.filename})" if refused.filename else "",
            )
            return _UNAVAILABLE
        try:
            announce_accepted_origin(origin=origin)
            uvicorn.run(app, host=host, port=arguments.port)
        except OSError as unavailable:
            # Not the port already being in use: uvicorn catches `EADDRINUSE` itself, reports
            # it as `[Errno 48]` and exits the process with 3, which is neither of the codes
            # this module returns and never reaches here. Documenting 3 as this command's own
            # would claim a code it does not choose and uvicorn could change. This covers the
            # bind failures uvicorn re-raises instead, where a traceback out of `bind()` reads
            # as a crash rather than as a port to change.
            _LOGGER.error(
                "the console could not serve on port %d: %s",
                arguments.port,
                unavailable.strerror or type(unavailable).__name__,
            )
            return _UNAVAILABLE
        finally:
            # Held off for the whole clean-up, not only once a signal has started one:
            # uvicorn also returns on SIGINT, on its own exit and on an exception, and a
            # SIGTERM landing then would abort `close()` half-way. The context manager's
            # own `finally` restores the previous handlers after this.
            _ignore_termination()
            close()
    return 0
