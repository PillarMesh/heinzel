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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Never

from .demo import build_demo_app

__all__ = [
    "main",
    "parse_arguments",
    "require_loopback",
    "resolve_origin",
    "warn_when_unauthenticated",
]

_LOGGER = logging.getLogger(__name__)

# The hosts an unauthenticated console may bind. Shared shape with the governed acceptance
# harness, which refuses the same way for the same reason.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

_LOOPBACK_HOST = "127.0.0.1"
# Only reachable behind `--container`. IPv4 is deliberate: the quickstart publishes an IPv4
# port (`127.0.0.1:8000:8000`) onto Docker's IPv4 bridge network.
_EVERY_INTERFACE = "0.0.0.0"
_DEFAULT_PORT = 8000

_ORIGIN_VARIABLE = "HEINZEL_CONSOLE_ALLOWED_ORIGIN"
_DIST_VARIABLE = "HEINZEL_CONSOLE_DIST"

# Invalid configuration, matching argparse's own code for arguments it rejects.
_INVALID_CONFIGURATION = 2
# The console was configured correctly but its surroundings refused it.
_UNAVAILABLE = 1


class PrivateArgumentParser(argparse.ArgumentParser):
    """A parser whose failure never echoes the arguments it was given."""

    def error(self, message: str) -> Never:
        self.exit(2, "heinzel-console: error: command arguments are invalid\n")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse `argv`, resolving `--container` into the host to bind.

    Abbreviation is off: argparse would otherwise accept `--c` for `--container`, so a typo
    could bind every interface instead of failing.
    """
    root = PrivateArgumentParser(prog="heinzel-console", allow_abbrev=False)
    commands = root.add_subparsers(required=True, metavar="command")
    serve = commands.add_parser("serve", help="Serve the demonstration console", allow_abbrev=False)
    serve.add_argument("--state-dir", required=True, type=Path, dest="state_dir")
    serve.add_argument("--port", default=_DEFAULT_PORT, type=int)
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


def resolve_origin(*, origin: str | None, container: bool, port: int) -> str:
    """The origin the browser will send, which is never the address the server binds.

    Commands are refused unless their `Origin` header matches this exactly, so deriving it
    from the bind address would serve a console where every read works and every command
    fails. Under `--container` the bind address is `0.0.0.0`, which no browser ever sends,
    and the port the reader opens is the published one rather than this one — so there is
    nothing to derive it from and the command refuses to start.
    """
    if origin is not None:
        return origin
    configured = os.environ.get(_ORIGIN_VARIABLE)
    if configured:
        return configured
    if container:
        raise ValueError(
            "a console bound to every interface cannot derive the origin the browser will "
            f"send: give --origin or set {_ORIGIN_VARIABLE}, for example "
            "http://127.0.0.1:8000"
        )
    return f"http://{_LOOPBACK_HOST}:{port}"


def require_bundle(dist: Path | None) -> Path | None:
    """Refuse a bundle path that holds no bundle, rather than serving a blank page."""
    if dist is not None and not dist.is_dir():
        raise ValueError(
            f"the compiled console bundle named by --dist or {_DIST_VARIABLE} is not a directory"
        )
    return dist


def warn_when_unauthenticated(*, host: str) -> None:
    """Say plainly, every time, that what is being served has no authentication."""
    _LOGGER.warning(
        "the demonstration console has no authentication: anyone who reaches %s acts as the "
        "architect, so it must not be exposed to a network",
        host,
    )


@contextmanager
def _termination_raises_system_exit() -> Iterator[None]:
    """Make SIGTERM leave `uvicorn.run` as an exception, so the state directory is released.

    uvicorn restores the handlers it replaced and then re-raises the signal it caught. With
    SIGTERM's default disposition that kills the process inside `uvicorn.run`, and the
    clean-up below it never runs — which is precisely how a container is stopped.
    """

    def _terminate(signal_number: int, frame: FrameType | None) -> Never:
        raise SystemExit(128 + signal_number)

    try:
        previous = signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        # Not the main thread, so the disposition is not this command's to change.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    host: str = arguments.host
    if not arguments.container:
        # Unreachable while `parse_arguments` is the only writer of `host`; it guards the
        # edit that adds a `--host` option.
        require_loopback(host)
    warn_when_unauthenticated(host=host)

    import uvicorn

    try:
        origin = resolve_origin(
            origin=arguments.origin, container=arguments.container, port=arguments.port
        )
        dist = require_bundle(arguments.dist)
        app, close = build_demo_app(
            arguments.state_dir, seed=not arguments.no_seed, origin=origin, dist=dist
        )
    except ValueError as invalid:
        _LOGGER.error("the console cannot be configured: %s", invalid)
        return _INVALID_CONFIGURATION
    except OSError as refused:
        # The path is left out deliberately: this command never echoes what it was given.
        _LOGGER.error(
            "the console's state directory could not be opened: %s", refused.strerror or refused
        )
        return _UNAVAILABLE
    try:
        with _termination_raises_system_exit():
            uvicorn.run(app, host=host, port=arguments.port)
    finally:
        close()
    return 0
