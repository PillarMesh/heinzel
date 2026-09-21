"""The `heinzel-console` command: serve the demonstration console from a state directory.

This serves a demonstration, not a deployment. The console has no authentication — the browser
names its actor in a header — so the command binds a loopback host unless `--container` says
the published port of a container image is the network boundary instead.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from .demo import build_demo_app

__all__ = ["main", "parse_arguments", "require_loopback", "warn_when_unauthenticated"]

_LOGGER = logging.getLogger(__name__)

# The hosts an unauthenticated console may bind. Shared shape with the governed acceptance
# harness, which refuses the same way for the same reason.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

_LOOPBACK_HOST = "127.0.0.1"
# Only reachable behind `--container`, where the published port is the network boundary.
_EVERY_INTERFACE = "0.0.0.0"
_DEFAULT_PORT = 8000


class PrivateArgumentParser(argparse.ArgumentParser):
    """A parser whose failure never echoes the arguments it was given."""

    def error(self, message: str) -> Never:
        self.exit(2, "heinzel-console: error: command arguments are invalid\n")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse `argv`, resolving `--container` into the host to bind."""
    root = PrivateArgumentParser(prog="heinzel-console")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the demonstration console")
    serve.add_argument("--state-dir", required=True, type=Path, dest="state_dir")
    serve.add_argument("--port", default=_DEFAULT_PORT, type=int)
    serve.add_argument(
        "--dist", type=Path, help="Compiled console bundle to serve alongside the API"
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


def warn_when_unauthenticated(*, host: str) -> None:
    """Say plainly, every time, that what is being served has no authentication."""
    _LOGGER.warning(
        "the demonstration console has no authentication: anyone who reaches %s acts as the "
        "architect, so it must not be exposed to a network",
        host,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    host: str = arguments.host
    if not arguments.container:
        require_loopback(host)
    warn_when_unauthenticated(host=host)

    import uvicorn

    app, close = build_demo_app(
        arguments.state_dir,
        seed=not arguments.no_seed,
        origin=f"http://{host}:{arguments.port}",
        dist=arguments.dist,
    )
    try:
        uvicorn.run(app, host=host, port=arguments.port)
    finally:
        close()
    return 0
