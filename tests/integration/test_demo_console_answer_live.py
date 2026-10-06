"""The console answers the seeded question, driven as a browser drives it.

Everything else that proves this chain calls the services directly. This drives the HTTP surface
the demonstration ships, under the same origin, CSRF and idempotency rules every command is held
to, from the requester's question to the requester reading their own answer.

The journey is the one `test_demo_console.py` already walks to `execution_ready`, continued past
it: with a warehouse configured, admitting the approved proposal admits the governed query plan,
which is what leaves a plan for the execution to run.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from heinzel_console.demo import DEMO_ACTOR_HEADER, DemoConsole
from heinzel_console.demo.collaborators import DEMO_ARCHITECT_ID, DEMO_REQUESTER_ID
from heinzel_console.demo.materialization import DEMO_GROUP_COLUMN, DEMO_MEASURE_COLUMN
from heinzel_console.demo.publication import DEMO_QUESTION, DEMO_TENANT_ID
from heinzel_console.demo.warehouse import DEMO_SOURCE_DAYS
from starlette.testclient import TestClient

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster

pytestmark = pytest.mark.live

ORIGIN = "http://127.0.0.1:8000"


class _Browser:
    """The console, and a client that speaks to it the way a browser does."""

    def __init__(self, console: DemoConsole) -> None:
        self.client = TestClient(console.build_app(origin=ORIGIN))

    def get(self, path: str, *, actor: str) -> httpx2.Response:
        return self.client.get(path, headers={DEMO_ACTOR_HEADER: actor})

    def post(self, path: str, body: dict[str, Any], *, key: str, actor: str) -> httpx2.Response:
        headers = {DEMO_ACTOR_HEADER: actor}
        session = self.client.get("/api/v1/session", headers=headers)
        return self.client.post(
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

    def data(self, response: httpx2.Response) -> dict[str, Any]:
        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()["data"]
        return payload


@pytest.fixture(name="warehouse")
def _warehouse(tmp_path: Path) -> Iterator[str]:
    if shutil.which("dbt") is None:
        pytest.skip("the locked dbt executable is unavailable")
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        yield bootstrap_dsn


def test_the_console_answers_the_seeded_question_through_its_own_api(
    tmp_path: Path, warehouse: str
) -> None:
    """Intake, clarification, proposal, acceptance, approval, admission, answer.

    Admission is where this differs from the console with no warehouse: the governed query plan
    is compiled and admitted there, so the request reaches `delivered` rather than stopping at
    `execution_ready`.
    """
    with DemoConsole(tmp_path / "state", warehouse_dsn=warehouse) as console:
        browser = _Browser(console)
        console.seed_demonstration_request()
        (item,) = browser.data(browser.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID))["items"]
        request_id = item["request_id"]
        opened = browser.data(browser.get(f"/api/v1/inbox/{request_id}", actor=DEMO_ARCHITECT_ID))
        assert opened["question"] == DEMO_QUESTION

        clarified = browser.data(
            browser.post(
                f"/api/v1/inbox/{request_id}/clarification",
                {
                    "expected_revision": opened["revision"],
                    "active_role": "data_architect",
                    "restated_request": "Provide the governed definition of the named term.",
                    "in_scope_summary": "Approved semantic scope only.",
                    "out_of_scope_summary": "No raw rows and no wider access.",
                },
                key="answer-clarify",
                actor=DEMO_ARCHITECT_ID,
            )
        )
        proposed = browser.data(
            browser.post(
                f"/api/v1/inbox/{request_id}/proposal",
                {"expected_revision": clarified["revision"], "active_role": "data_architect"},
                key="answer-prepare",
                actor=DEMO_ARCHITECT_ID,
            )
        )
        submitted = browser.data(
            browser.post(
                f"/api/v1/inbox/{request_id}/proposal/submission",
                {"expected_revision": proposed["revision"], "active_role": "data_architect"},
                key="answer-submit",
                actor=DEMO_ARCHITECT_ID,
            )
        )
        assert submitted["state"] == "awaiting_approval"

        statement = browser.data(
            browser.get(f"/api/v1/requests/{request_id}/clarified-outcome", actor=DEMO_REQUESTER_ID)
        )
        browser.data(
            browser.post(
                f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
                {
                    "expected_revision": statement["revision"],
                    "clarified_outcome_digest": statement["statement_digest"],
                    "active_role": "requester",
                    "decision": "approve",
                },
                key="answer-accept",
                actor=DEMO_REQUESTER_ID,
            )
        )
        detail = browser.data(browser.get(f"/api/v1/inbox/{request_id}", actor=DEMO_ARCHITECT_ID))
        reviewed = browser.data(
            browser.post(
                f"/api/v1/inbox/{request_id}/decisions",
                {
                    "expected_revision": detail["revision"],
                    "reviewed_digest": detail["proposal_digest"],
                    "active_role": "data_architect",
                    "decision": "approve",
                },
                key="answer-approve",
                actor=DEMO_ARCHITECT_ID,
            )
        )
        admitted = browser.data(
            browser.post(
                f"/api/v1/inbox/{request_id}/admission",
                {
                    "expected_revision": reviewed["revision"],
                    "reviewed_digest": reviewed["proposal_digest"],
                    "active_role": "data_architect",
                },
                key="answer-admit",
                actor=DEMO_ARCHITECT_ID,
            )
        )
        # Past `execution_ready`: the governed admission left a plan, and the execution ran it.
        assert admitted["state"] == "delivered"

        # The requester reads their own answer, and it is the product's numbers.
        result = browser.data(
            browser.get(f"/api/v1/requests/{request_id}/result", actor=DEMO_REQUESTER_ID)
        )
        assert [column["name"] for column in result["columns"]] == [
            DEMO_GROUP_COLUMN,
            DEMO_MEASURE_COLUMN,
        ]
        assert [[str(value) for value in row] for row in result["rows"]] == [
            list(pair)
            for pair in zip(
                DEMO_SOURCE_DAYS,
                ("30.000000000", "125.500000000", "99.000000000"),
                strict=True,
            )
        ]

        # The architect is not the requester, and a result belongs to whoever asked for it.
        refused = browser.get(f"/api/v1/requests/{request_id}/result", actor=DEMO_ARCHITECT_ID)
        assert refused.status_code == 404, refused.text

        answer = console.governed_answer
        assert answer is not None

    # Closing the console closed the answer runtime too, not only the stores the console opened
    # itself. The runtime holds four SQLite connections of its own, and a console that composed
    # one per start and closed none would hold every one of them until the process ended.
    with pytest.raises(sqlite3.ProgrammingError):
        answer.runtime.results.load_execution(DEMO_TENANT_ID, request_id)


def test_a_second_console_over_the_same_state_resumes_the_product_it_published(
    tmp_path: Path, warehouse: str
) -> None:
    """A restart finds its own generation rather than materializing a second one.

    This is what `docker compose restart` and a stopped-then-started container do, and the
    resume path is not the one the journey above walks: provisioning is skipped, the role
    passwords are rotated, the grants and the seal are reapplied, and the signed model and the
    query binding are read back out of the state directory rather than carried from the
    materialization. Each of those is a place a generation can be lost between starts.
    """
    state = tmp_path / "state"
    with DemoConsole(state, warehouse_dsn=warehouse) as first:
        first.seed_demonstration_request()
        opening = _Browser(first)
        (seeded,) = opening.data(opening.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID))["items"]
        published = first.governed_answer
        assert published is not None
        generation = published.preparation.generation

    with DemoConsole(state, warehouse_dsn=warehouse) as second:
        second.seed_demonstration_request()
        resumed = second.governed_answer
        assert resumed is not None
        # The same generation, not a second one: a restart that materialized again would
        # publish authority for a relation the warehouse already holds.
        assert resumed.preparation.generation == generation
        browser = _Browser(second)
        (item,) = browser.data(browser.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID))["items"]
        # The one seeded question, not a second one, and still where the first start left it.
        assert item["request_id"] == seeded["request_id"]
        assert item["state"] == seeded["state"]


def test_a_console_with_no_warehouse_still_stops_at_execution_ready(tmp_path: Path) -> None:
    """The answer capabilities report themselves as not delivered rather than looking empty.

    Asserted beside the answering journey, because the two must stay different: a console given
    no warehouse must not quietly behave as though it had one.
    """
    with DemoConsole(tmp_path / "state") as console:
        browser = _Browser(console)
        console.seed_demonstration_request()
        (item,) = browser.data(browser.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID))["items"]

        refused = browser.get(
            f"/api/v1/requests/{item['request_id']}/result", actor=DEMO_REQUESTER_ID
        )

        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["code"] == "capability_not_delivered"


def test_the_console_serves_the_receipts_its_acquisition_recorded(
    tmp_path: Path, warehouse: str
) -> None:
    """The acquisition surface answers from the evidence the startup acquisition composed.

    Before the acquisition ran through the runtime's own application there was nothing for
    this route to read, and it refused. What it must not do now is the other failure: a
    console that acquired nothing still has an evidence store, and answering that one with
    an empty list would read as a delivered capability holding nothing.
    """
    with DemoConsole(tmp_path / "state", warehouse_dsn=warehouse) as console:
        browser = _Browser(console)
        response = browser.get("/api/v1/acquisition-receipts", actor=DEMO_ARCHITECT_ID)
        assert response.status_code == 200, response.text
        receipts = browser.data(response)["receipts"]
        assert receipts, "the startup acquisition recorded no receipt"

    # The same console without a warehouse acquires nothing, and says so rather than
    # answering with the empty store it still opens.
    with DemoConsole(tmp_path / "dry-state") as dry:
        refused = _Browser(dry).get("/api/v1/acquisition-receipts", actor=DEMO_ARCHITECT_ID)
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["code"] == "capability_not_delivered"


def test_a_console_given_a_connection_reports_no_managed_warehouse(
    tmp_path: Path, warehouse: str
) -> None:
    """The DSN path provisions a database no governing service owns, and says exactly that.

    This is the half of the opt-in that cannot be proved offline: a console given a connection
    answers every question from the product it materialized, and still reports the managed
    warehouse as not delivered, because the warehouse it used is one it provisioned beside
    warehouse-control rather than through it. Reporting a binding here would claim a managed
    warehouse where a temporary local database is, and `WarehouseBinding.deployment_mode` admits
    only `heinzel_cloud`.

    The opt-in path is what reports a binding, and it needs a Docker daemon; the offline suite
    pins its refusals and the setup surface it answers from a binding already carried to
    `ready`. See `apps/console/server/tests/test_demo_managed_warehouse.py`.
    """
    with DemoConsole(tmp_path / "state", warehouse_dsn=warehouse) as console:
        browser = _Browser(console)
        workspace = browser.data(browser.get("/api/v1/workspace", actor=DEMO_ARCHITECT_ID))
        refused = browser.get("/api/v1/setup", actor=DEMO_ARCHITECT_ID)

    managed = next(
        item for item in workspace["capabilities"] if item["capability_id"] == "warehouse-binding"
    )
    assert managed["state"] == "not_delivered"
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "capability_not_delivered"
