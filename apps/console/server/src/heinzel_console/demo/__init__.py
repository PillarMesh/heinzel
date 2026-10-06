"""A local demonstration console: the governed services over SQLite, with demo-grade collaborators.

This is a demonstration, not a deployment. It has no authentication and the data it holds is
illustrative. By default it provisions no warehouse of its own: it is given one, or it reports
every warehouse-shaped capability as not delivered. `ManagedWarehouseOption` selects the opt-in
path on which warehouse-control creates one instead, which costs a reachable Docker daemon --
see `managed_warehouse.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from starlette.applications import Starlette

from .console import DEMO_ACTOR_HEADER, DemoConsole
from .managed_warehouse import ManagedWarehouseOption, ManagedWarehouseRefused

__all__ = [
    "DEMO_ACTOR_HEADER",
    "DemoConsole",
    "ManagedWarehouseOption",
    "ManagedWarehouseRefused",
    "build_demo_app",
]


def build_demo_app(
    state_dir: Path,
    *,
    seed: bool = True,
    origin: str = "http://127.0.0.1:8000",
    dist: Path | None = None,
    warehouse_dsn: str | None = None,
    managed_warehouse: ManagedWarehouseOption | None = None,
) -> tuple[Starlette, Callable[[], None]]:
    """Build the demonstration console and a callable that closes what it opened."""
    console = DemoConsole(
        state_dir, warehouse_dsn=warehouse_dsn, managed_warehouse=managed_warehouse
    )
    try:
        if seed:
            console.seed_demonstration_request()
        return console.build_app(origin=origin, dist=dist), console.close
    except BaseException:
        # Best-effort clean-up: a failure to close must not replace the failure to build.
        with suppress(Exception):
            console.close()
        raise
