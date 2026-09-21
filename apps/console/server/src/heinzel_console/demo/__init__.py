"""A local demonstration console: the governed services over SQLite, with demo-grade collaborators.

This is a demonstration, not a deployment. It has no authentication, it provisions no warehouse,
and the data it holds is illustrative.
"""

from __future__ import annotations

from .console import DEMO_ACTOR_HEADER, DemoConsole

__all__ = ["DEMO_ACTOR_HEADER", "DemoConsole"]
