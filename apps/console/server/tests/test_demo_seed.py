"""The demonstration's seed and the package entry point that serves it.

The seed exists so that a person who starts the demonstration meets a request waiting for
them rather than an empty inbox. These tests hold it to that, and to leaving exactly one
request however many times the demonstration is restarted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from heinzel_console import demo as demo_package
from heinzel_console.demo import DemoConsole, build_demo_app, seed_demo_request
from heinzel_console.demo.collaborators import DEMO_ARCHITECT_ID
from heinzel_console.demo.console import DEMO_ACTOR_HEADER
from heinzel_console.demo.publication import DEMO_QUESTION
from heinzel_console.demo.stores import DemoStores
from starlette.testclient import TestClient

ORIGIN = "http://127.0.0.1:8000"


def _inbox(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/v1/inbox", headers={DEMO_ACTOR_HEADER: DEMO_ARCHITECT_ID})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["data"]["items"]
    return items


def _post(client: TestClient, path: str, body: dict[str, Any], *, key: str) -> httpx.Response:
    headers = {DEMO_ACTOR_HEADER: DEMO_ARCHITECT_ID}
    session = client.get("/api/v1/session", headers=headers)
    return client.post(
        path,
        json=body,
        headers={
            **headers,
            "origin": ORIGIN,
            "x-csrf-token": session.json()["data"]["csrf_token"],
            "idempotency-key": key,
            "content-type": "application/json",
        },
    )


def test_the_seeded_console_opens_with_one_request_waiting(tmp_path: Path) -> None:
    app, close = build_demo_app(tmp_path / "state", origin=ORIGIN)
    try:
        with TestClient(app) as client:
            assert len(_inbox(client)) == 1
    finally:
        close()


def test_restarting_the_demonstration_does_not_seed_a_second_request(tmp_path: Path) -> None:
    """The seed is idempotent over the state directory, not over one process.

    A demonstration that is stopped and started again is the ordinary case, and it must
    leave the person the same single request rather than a growing inbox.
    """
    state_dir = tmp_path / "state"
    app, close = build_demo_app(state_dir, origin=ORIGIN)
    try:
        with TestClient(app) as client:
            (first,) = _inbox(client)
    finally:
        close()

    app, close = build_demo_app(state_dir, origin=ORIGIN)
    try:
        with TestClient(app) as client:
            restarted = _inbox(client)
    finally:
        close()

    assert [item["request_id"] for item in restarted] == [first["request_id"]]


def test_seeding_can_be_turned_off_and_leaves_the_inbox_empty(tmp_path: Path) -> None:
    app, close = build_demo_app(tmp_path / "state", seed=False, origin=ORIGIN)
    try:
        with TestClient(app) as client:
            assert _inbox(client) == []
    finally:
        close()


def test_a_seed_that_fails_closes_the_stores_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed build leaves no store open, and reports the failure that caused it.

    The console is built before the seed runs, so a seed that raises would otherwise
    abandon every SQLite connection it opened and leave the state directory locked.
    """
    opened: list[sqlite3.Connection] = []
    connect = DemoStores._connect

    def recording_connect(database_path: Path) -> sqlite3.Connection:
        connection = connect(database_path)
        opened.append(connection)
        return connection

    def refusing_seed(console: object) -> None:
        raise RuntimeError("the seed refused to run")

    monkeypatch.setattr(DemoStores, "_connect", staticmethod(recording_connect))
    monkeypatch.setattr(demo_package, "seed_demo_request", refusing_seed)

    with pytest.raises(RuntimeError, match="the seed refused to run"):
        build_demo_app(tmp_path / "state", origin=ORIGIN)

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_the_seeded_request_is_the_demonstrations_own_question_and_grounds(
    tmp_path: Path,
) -> None:
    """The seed asks `DEMO_QUESTION`, and that question reaches a proposal.

    Preparation is where a question the publication cannot ground becomes `no_valid_plan`,
    so rewording the seeded question into something the authority resolver refuses would
    fail here rather than in front of the person running the demonstration.
    """
    app, close = build_demo_app(tmp_path / "state", origin=ORIGIN)
    try:
        with TestClient(app) as client:
            (item,) = _inbox(client)
            request_id = item["request_id"]
            detail = client.get(
                f"/api/v1/inbox/{request_id}",
                headers={DEMO_ACTOR_HEADER: DEMO_ARCHITECT_ID},
            )
            assert detail.status_code == 200, detail.text
            assert detail.json()["data"]["question"] == DEMO_QUESTION

            clarified = _post(
                client,
                f"/api/v1/inbox/{request_id}/clarification",
                {
                    "expected_revision": 1,
                    "active_role": "data_architect",
                    "restated_request": "Provide the governed definition of the named term.",
                    "in_scope_summary": "Approved semantic scope only.",
                    "out_of_scope_summary": "No raw rows and no wider access.",
                },
                key="seed-clarify",
            )
            assert clarified.status_code == 200, clarified.text

            prepared = _post(
                client,
                f"/api/v1/inbox/{request_id}/proposal",
                {
                    "expected_revision": clarified.json()["data"]["revision"],
                    "active_role": "data_architect",
                },
                key="seed-prepare",
            )
            assert prepared.status_code == 200, prepared.text
            proposed = prepared.json()["data"]
            assert proposed["state"] != "no_valid_plan"
            assert proposed["preparation_notes"] == []
    finally:
        close()


def test_seeding_an_inbox_that_already_holds_a_request_adds_nothing(tmp_path: Path) -> None:
    """Called directly, the seed is still idempotent, whatever put the request there."""
    with DemoConsole(tmp_path / "state") as console:
        seed_demo_request(console)
        first = console.inbox_request_ids()
        seed_demo_request(console)
        assert console.inbox_request_ids() == first
        assert len(first) == 1
