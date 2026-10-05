"""Waiting for the warehouse to accept connections, and refusing to wait for an answer.

The console's first act is to connect to its warehouse, and nothing it does afterwards would
retry. Compose holds the first start back until the warehouse reports healthy; `docker compose
restart` does not honour `depends_on` at all, so the console comes back while PostgreSQL is still
starting and every step below it connects without retrying.

The distinction this file is about: a connection nothing answered is worth waiting through, and a
refusal the server produced is not. Waiting for a wrong password to become right is a minute of
silence followed by the same error.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from heinzel_console.demo import bootstrap
from heinzel_console.demo.collaborators import demo_clock
from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import (
    WarehouseUnreachable,
    wait_for_demo_warehouse,
)

DSN = "postgresql://postgres@warehouse:5432/heinzel"


class _Clock:
    """A monotonic reading that only `sleep` advances, so no test waits in real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _refused() -> psycopg.OperationalError:
    """What `psycopg` raises when nothing answered: no `sqlstate`, because nothing produced one."""
    return psycopg.OperationalError("connection to server at 'warehouse' failed")


def _server_said(sqlstate: str) -> psycopg.OperationalError:
    error = psycopg.OperationalError("the server answered")
    error.sqlstate = sqlstate
    return error


def test_a_warehouse_that_answers_at_once_is_not_waited_for() -> None:
    clock = _Clock()
    opened = _Connection()

    wait_for_demo_warehouse(
        DSN, connect=lambda dsn: opened, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert clock.slept == []
    # Closed again: this opens a connection to learn one thing and holds nothing afterwards.
    assert opened.closed


def test_a_refused_connection_is_waited_through() -> None:
    """The restart case: PostgreSQL is coming back and nothing is listening yet."""
    clock = _Clock()
    attempts: list[str] = []

    def connect(dsn: str) -> _Connection:
        attempts.append(dsn)
        if len(attempts) < 4:
            raise _refused()
        return _Connection()

    wait_for_demo_warehouse(DSN, connect=connect, sleep=clock.sleep, monotonic=clock.monotonic)

    assert len(attempts) == 4
    assert clock.slept == [1.0, 1.0, 1.0]


def test_a_server_still_starting_up_is_waited_through() -> None:
    """`cannot_connect_now` is the one refusal the server makes that is worth waiting for."""
    clock = _Clock()
    attempts: list[str] = []

    def connect(dsn: str) -> _Connection:
        attempts.append(dsn)
        if len(attempts) < 2:
            raise _server_said("57P03")
        return _Connection()

    wait_for_demo_warehouse(DSN, connect=connect, sleep=clock.sleep, monotonic=clock.monotonic)

    assert len(attempts) == 2


@pytest.mark.parametrize(
    ("sqlstate", "what_it_means"),
    [
        ("28P01", "the password is wrong"),
        ("3D000", "the database does not exist"),
        ("28000", "the role may not connect"),
        ("53300", "the server is out of connection slots"),
    ],
)
def test_a_refusal_the_server_produced_is_raised_at_once(sqlstate: str, what_it_means: str) -> None:
    """Waiting changes none of these, and waiting hides the error for the length of the budget."""
    clock = _Clock()

    def connect(dsn: str) -> _Connection:
        raise _server_said(sqlstate)

    with pytest.raises(psycopg.OperationalError) as refused:
        wait_for_demo_warehouse(DSN, connect=connect, sleep=clock.sleep, monotonic=clock.monotonic)

    assert refused.value.sqlstate == sqlstate, what_it_means
    assert clock.slept == []


def test_a_warehouse_that_never_answers_is_refused_with_the_budget_it_was_given() -> None:
    """Named rather than retried forever: an address nothing listens on is a configuration
    error, and a console that waited on it would never report anything."""
    clock = _Clock()

    def connect(dsn: str) -> _Connection:
        raise _refused()

    with pytest.raises(WarehouseUnreachable, match="0:00:05"):
        wait_for_demo_warehouse(
            DSN,
            connect=connect,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            budget=timedelta(seconds=5),
        )

    # The last attempt is made at the deadline rather than after it, so a budget of five seconds
    # is five one-second waits and six attempts.
    assert clock.slept == [1.0] * 5


def test_the_cause_is_kept_on_the_refusal() -> None:
    """The transport error says which host and port were tried, which the message above does not."""
    clock = _Clock()
    cause = _refused()

    def connect(dsn: str) -> _Connection:
        raise cause

    with pytest.raises(WarehouseUnreachable) as unreachable:
        wait_for_demo_warehouse(
            DSN,
            connect=connect,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            budget=timedelta(seconds=1),
        )

    assert unreachable.value.__cause__ is cause


class _Waited(RuntimeError):
    """Raised from the patched wait, so reaching it is what the test observes."""


def _raise_waited(bootstrap_dsn: str) -> None:
    raise _Waited(bootstrap_dsn)


def test_the_bootstrap_waits_before_it_connects_to_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is the first thing `ensure_demo_generation` does, which is the whole point of it.

    Asserted on the order rather than the call, because a wait placed after the first connection
    would pass a test that only checked it ran. Nothing else here is patched: the DSN names a port
    nothing listens on, so any step that connected before the wait would raise
    `psycopg.OperationalError` instead.
    """
    monkeypatch.setattr(bootstrap, "wait_for_demo_warehouse", _raise_waited)
    stores = DemoStores(tmp_path / "state")
    try:
        publication = build_demo_publication(stores, clock=demo_clock)
        with pytest.raises(_Waited):
            bootstrap.ensure_demo_generation(
                # Port 1 on loopback, which nothing serves.
                bootstrap_dsn="postgresql://postgres@127.0.0.1:1/heinzel",
                stores=stores,
                publication=publication,
                dbt_executable=tmp_path / "dbt",
                workspace=tmp_path / "materialization",
                clock=demo_clock,
            )
    finally:
        stores.close()
