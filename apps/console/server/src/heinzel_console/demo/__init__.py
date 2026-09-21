"""A local demonstration console: the governed services over SQLite, with demo-grade collaborators.

This is a demonstration, not a deployment. It has no authentication, it provisions no warehouse,
and the data it holds is illustrative.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette

from .console import DEMO_ACTOR_HEADER, DemoConsole
from .seed import seed_demo_request

__all__ = ["DEMO_ACTOR_HEADER", "DemoConsole", "build_demo_app", "seed_demo_request"]


def build_demo_app(
    state_dir: Path,
    *,
    seed: bool = True,
    origin: str = "http://127.0.0.1:8000",
    dist: Path | None = None,
) -> tuple[Starlette, Callable[[], None]]:
    """Build the demonstration console and a callable that closes its stores."""
    console = DemoConsole(state_dir)
    try:
        if seed:
            seed_demo_request(console)
        return console.build_app(origin=origin, dist=dist), console.close
    except BaseException:
        console.close()
        raise
