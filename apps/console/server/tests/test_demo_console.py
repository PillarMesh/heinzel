"""The demonstration console, driven over its own HTTP API.

These tests walk the product surface the demonstration ships, rather than calling the
backend directly: the journey is only worth shipping if it works through the API a browser
uses, with the same origin, CSRF and idempotency rules every command is held to.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# `TestClient` subclasses `httpx2.Client`, so the responses it returns are httpx2's.
import httpx2
import pytest
from heinzel_console.demo import DEMO_ACTOR_HEADER, DemoConsole
from heinzel_console.demo.collaborators import DEMO_ARCHITECT_ID, DEMO_REQUESTER_ID
from heinzel_console.demo.publication import DEMO_QUESTION
from heinzel_contract_model import digest
from heinzel_request_management import RequestIntakeContent
from heinzel_request_management.models import (
    DataAccessRequest,
    QuestionTermSelection,
    StakeholderQuestion,
)
from starlette.testclient import TestClient

ORIGIN = "http://127.0.0.1:8000"
PURPOSE = "weekly review"
UNGROUNDED_QUESTION = "What is the CEO's home address?"


class _Console:
    """One demonstration console and a client that speaks to it as a browser would."""

    def __init__(self, console: DemoConsole) -> None:
        self.console = console
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

    def submit_question(self, question: str, *, key: str) -> str:
        """Submit `question` as the requester and return the request identifier."""
        payload = StakeholderQuestion(purpose=PURPOSE, question=question)
        created = self.post(
            "/api/v1/requests",
            {
                "expected_revision": 1,
                "request_digest": digest(RequestIntakeContent(title=question, payload=payload)),
                "active_role": "requester",
                "title": question,
                "request": {
                    "kind": "stakeholder_question",
                    "purpose": PURPOSE,
                    "question": question,
                },
            },
            key=key,
            actor=DEMO_REQUESTER_ID,
        )
        assert created.status_code == 200, created.text
        request_id: str = created.json()["data"]["request_id"]
        return request_id

    def clarify(self, request_id: str, *, key: str) -> httpx2.Response:
        return self.post(
            f"/api/v1/inbox/{request_id}/clarification",
            {
                "expected_revision": 1,
                "active_role": "data_architect",
                "restated_request": "Provide the governed definition of the named term.",
                "in_scope_summary": "Approved semantic scope only.",
                "out_of_scope_summary": "No raw rows and no wider access.",
            },
            key=key,
            actor=DEMO_ARCHITECT_ID,
        )

    def prepare_proposal(
        self, request_id: str, *, expected_revision: int, key: str
    ) -> httpx2.Response:
        return self.post(
            f"/api/v1/inbox/{request_id}/proposal",
            {"expected_revision": expected_revision, "active_role": "data_architect"},
            key=key,
            actor=DEMO_ARCHITECT_ID,
        )


@pytest.fixture
def console(tmp_path: Path) -> Iterator[_Console]:
    """One console per test, closed through the context manager a caller would use."""
    with DemoConsole(tmp_path / "state") as demo_console:
        yield _Console(demo_console)


def test_the_seeded_question_travels_the_whole_journey_to_execution_ready(
    console: _Console,
) -> None:
    request_id = console.submit_question(DEMO_QUESTION, key="journey-intake")
    assert console.console.inbox_request_ids() == (request_id,)

    clarified = console.clarify(request_id, key="journey-clarify")
    assert clarified.status_code == 200, clarified.text

    prepared = console.prepare_proposal(
        request_id,
        expected_revision=clarified.json()["data"]["revision"],
        key="journey-prepare",
    )
    assert prepared.status_code == 200, prepared.text
    proposal = prepared.json()["data"]
    assert proposal["state"] == "proposed"
    # The answer is the published definition of the term the question named, not a number
    # the demonstration invented.
    assert proposal["proposal"]["candidate"] == (
        "Daily order value is confirmed customer order value per calendar day."
    )

    submitted = console.post(
        f"/api/v1/inbox/{request_id}/proposal/submission",
        {"expected_revision": proposal["revision"], "active_role": "data_architect"},
        key="journey-submit",
        actor=DEMO_ARCHITECT_ID,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["state"] == "awaiting_approval"

    outcome = console.get(
        f"/api/v1/requests/{request_id}/clarified-outcome", actor=DEMO_REQUESTER_ID
    )
    assert outcome.status_code == 200, outcome.text
    statement = outcome.json()["data"]
    accepted = console.post(
        f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": statement["revision"],
            "clarified_outcome_digest": statement["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        key="journey-accept",
        actor=DEMO_REQUESTER_ID,
    )
    assert accepted.status_code == 200, accepted.text

    detail = console.get(f"/api/v1/inbox/{request_id}", actor=DEMO_ARCHITECT_ID).json()["data"]
    approved = console.post(
        f"/api/v1/inbox/{request_id}/decisions",
        {
            "expected_revision": detail["revision"],
            "reviewed_digest": detail["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        key="journey-approve",
        actor=DEMO_ARCHITECT_ID,
    )
    assert approved.status_code == 200, approved.text
    reviewed = approved.json()["data"]
    # Both authorities the proposal requires are recorded before admission is possible: the
    # requester's acceptance of the clarified outcome and the architect's own review.
    assert [approval["satisfied"] for approval in reviewed["proposal"]["required_approvals"]] == [
        True,
        True,
    ]

    admitted = console.post(
        f"/api/v1/inbox/{request_id}/admission",
        {
            "expected_revision": reviewed["revision"],
            "reviewed_digest": reviewed["proposal_digest"],
            "active_role": "data_architect",
        },
        key="journey-admit",
        actor=DEMO_ARCHITECT_ID,
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["data"]["state"] == "execution_ready"


@pytest.mark.parametrize(
    "path", ["/api/v1/runs", "/api/v1/acquisition-receipts", "/api/v1/answer-terms"]
)
def test_the_capabilities_outside_the_demonstration_answer_not_delivered(
    console: _Console, path: str
) -> None:
    """Runs, acquisition evidence and the governed terms are absent, and say so rather than
    looking empty.

    A 503 naming the missing dependency is the honest answer; an empty list would read as a
    working capability with nothing in it. The governed terms are the clearest case: a console
    with no composed answer behind it can resolve a question composed from no term at all, so
    offering a list would be offering a form that produces an unanswerable request.
    """
    response = console.get(path, actor=DEMO_ARCHITECT_ID)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "capability_not_delivered"


def test_the_result_of_an_admitted_request_answers_not_delivered_to_its_requester(
    console: _Console,
) -> None:
    """The third absent capability says so to the one actor entitled to ask for it.

    The result page is requester-scoped, so this refusal is only reachable as the requester
    who owns the request; the architect is answered `404 not_found` because the resource is
    not theirs to read, which says nothing about whether answer delivery is delivered.
    """
    request_id = console.submit_question(DEMO_QUESTION, key="result-intake")

    refused = console.get(f"/api/v1/requests/{request_id}/result", actor=DEMO_REQUESTER_ID)
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "capability_not_delivered"

    unscoped = console.get(f"/api/v1/requests/{request_id}/result", actor=DEMO_ARCHITECT_ID)
    assert unscoped.status_code == 404, unscoped.text
    assert unscoped.json()["error"]["code"] == "not_found"


def test_the_console_is_healthy_and_the_architect_inbox_is_reachable(console: _Console) -> None:
    assert console.client.get("/healthz").status_code == 200

    # The actor directory is what turns an identifier into a name a person reads; without
    # it the session would name the actor by its raw identifier or not at all.
    for actor, display_name in (
        (DEMO_ARCHITECT_ID, "Data engineering architect"),
        (DEMO_REQUESTER_ID, "Requester"),
    ):
        session = console.get("/api/v1/session", actor=actor)
        assert session.status_code == 200, session.text
        assert session.json()["data"]["actor"]["display_name"] == display_name

    inbox = console.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID)
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["data"]["items"] == []

    request_id = console.submit_question(DEMO_QUESTION, key="inbox-intake")
    listed = console.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID).json()["data"]["items"]
    assert [item["request_id"] for item in listed] == [request_id]
    assert console.console.inbox_request_ids() == (request_id,)


def test_a_data_access_request_is_refused_at_intake_rather_than_accepted(
    console: _Console,
) -> None:
    """Intake fails closed, matching the capability card the same console renders.

    Grant application, expiry and revocation are not delivered here, so the workspace card
    reports data access as not delivered. Accepting the request anyway would take a
    requester through intake and clarification only to fail at preparation with advice that
    cannot help, and would leave nothing in the inbox that any action can move.
    """
    expires_at = datetime.now(UTC) + timedelta(days=7)
    payload = DataAccessRequest(
        purpose=PURPOSE,
        data_product_id="orders_daily",
        requested_fields=("order_id",),
        access_mode="query",
        expires_at=expires_at,
    )
    created = console.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": digest(
                RequestIntakeContent(title="Access to orders", payload=payload)
            ),
            "active_role": "requester",
            "title": "Access to orders",
            "request": {
                "kind": "data_access",
                "purpose": PURPOSE,
                "data_product_ref": "orders_daily",
                "requested_fields": ["order_id"],
                "access_mode": "query",
                "expires_at": expires_at.isoformat(),
            },
        },
        key="access-intake",
        actor=DEMO_REQUESTER_ID,
    )
    assert created.status_code == 503, created.text
    assert created.json()["error"]["code"] == "capability_not_delivered"
    # Nothing was persisted, so no architect is left holding a request they cannot progress.
    assert console.console.inbox_request_ids() == ()


def test_a_question_naming_no_published_term_is_refused_rather_than_answered(
    console: _Console,
) -> None:
    """The refusal reaches both surfaces, and no answer is produced.

    The architect's proposal preparation succeeds as a command and returns `no_valid_plan`
    with no proposal; the requester sees the same state with a requester-safe explanation.
    Preparing a proposal for such a request never yields an answer to submit.
    """
    request_id = console.submit_question(UNGROUNDED_QUESTION, key="refusal-intake")
    clarified = console.clarify(request_id, key="refusal-clarify")
    assert clarified.status_code == 200, clarified.text

    prepared = console.prepare_proposal(
        request_id,
        expected_revision=clarified.json()["data"]["revision"],
        key="refusal-prepare",
    )
    assert prepared.status_code == 200, prepared.text
    refused = prepared.json()["data"]
    assert refused["state"] == "no_valid_plan"
    assert refused["proposal"] is None
    assert refused["preparation_notes"] == [
        "No Valid Plan: published_semantic_term_not_found.",
        "Required change: Ask about one term in the workspace's current approved semantic "
        "publication.",
    ]

    mine = console.get("/api/v1/requests/mine", actor=DEMO_REQUESTER_ID)
    assert mine.status_code == 200, mine.text
    (requester_view,) = mine.json()["data"]
    assert requester_view["state"] == "no_valid_plan"
    assert requester_view["no_valid_plan_explanation"] == (
        "The current governed catalog does not contain one unambiguous term for this question."
    )

    # A refused request has nothing to submit for approval, so the journey stops here.
    submitted = console.post(
        f"/api/v1/inbox/{request_id}/proposal/submission",
        {"expected_revision": refused["revision"], "active_role": "data_architect"},
        key="refusal-submit",
        actor=DEMO_ARCHITECT_ID,
    )
    assert submitted.status_code == 409, submitted.text
    assert submitted.json()["error"]["code"] == "preparation_unavailable"


def test_the_seeded_question_is_not_refused_where_an_ungrounded_one_is(
    console: _Console,
) -> None:
    """`DEMO_QUESTION` grounds at the step that refuses the ungrounded question.

    Proposal preparation is where the grounding is resolved and where a question the
    publication cannot ground becomes `no_valid_plan`. The seeded question must reach a
    proposal there, or the demonstration refuses its own happy path.
    """
    request_id = console.submit_question(DEMO_QUESTION, key="grounding-intake")
    clarified = console.clarify(request_id, key="grounding-clarify")
    assert clarified.status_code == 200, clarified.text

    prepared = console.prepare_proposal(
        request_id,
        expected_revision=clarified.json()["data"]["revision"],
        key="grounding-prepare",
    )
    assert prepared.status_code == 200, prepared.text
    proposed = prepared.json()["data"]
    assert proposed["state"] != "no_valid_plan"
    assert proposed["preparation_notes"] == []
    assert proposed["proposal"] is not None


def test_a_question_submitted_with_its_governed_terms_is_accepted_and_reaches_the_inbox(
    console: _Console,
) -> None:
    """The browser digests the selection with the question, and the service rebuilds both.

    The digest sent here is computed from the artifact the service will rebuild, so a console
    that dropped the selection, reordered it or left out its schema version would be refused
    `request_digest_mismatch` instead of recording a request.
    """
    selection = QuestionTermSelection(metric_ref="daily-order-value", dimension_refs=("order_day",))
    question = "What is the daily order value by day?"
    payload = StakeholderQuestion(purpose=PURPOSE, question=question, selection=selection)

    created = console.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": digest(RequestIntakeContent(title=question, payload=payload)),
            "active_role": "requester",
            "title": question,
            "request": {
                "kind": "stakeholder_question",
                "purpose": PURPOSE,
                "question": question,
                "selection": {
                    "metric_ref": "daily-order-value",
                    "dimension_refs": ["order_day"],
                },
            },
        },
        key="selected-intake",
        actor=DEMO_REQUESTER_ID,
    )

    assert created.status_code == 200, created.text
    request_id = created.json()["data"]["request_id"]
    opened = console.get(f"/api/v1/inbox/{request_id}", actor=DEMO_ARCHITECT_ID)
    assert opened.status_code == 200, opened.text
    assert opened.json()["data"]["question"] == question


def test_a_selection_the_term_artifact_refuses_never_reaches_the_inbox(console: _Console) -> None:
    """One term named as both the measure and its breakdown is refused at intake.

    The console rebuilds the payload through `QuestionTermSelection`, so the artifact's own
    refusal is what answers: the browser is not trusted to have offered a valid pair.
    """
    question = "What is the daily order value by itself?"

    refused = console.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": question,
            "request": {
                "kind": "stakeholder_question",
                "purpose": PURPOSE,
                "question": question,
                "selection": {
                    "metric_ref": "daily-order-value",
                    "dimension_refs": ["daily-order-value"],
                },
            },
        },
        key="self-selected-intake",
        actor=DEMO_REQUESTER_ID,
    )

    assert refused.status_code >= 400, refused.text
    inbox = console.get("/api/v1/inbox", actor=DEMO_ARCHITECT_ID)
    assert question not in [item["title"] for item in inbox.json()["data"]["items"]]


def test_the_console_reads_its_published_dashboards_without_a_bi_provider(
    console: _Console,
) -> None:
    """What was published is read from bi-control's repository, not through a provider.

    A publication is recorded there only against a provider receipt, so the read answers the same
    question a control service would. Wiring it over the control service instead would make the
    surface go absent exactly when no Superset is configured -- reporting nothing published for a
    deployment that published something and has since been given no instance to publish to.

    This console has no warehouse and so has published nothing, which is the case that proves the
    surface answers rather than refusing: an empty list, not `capability_not_delivered`.
    """
    listing = console.get("/api/v1/dashboards", actor=DEMO_ARCHITECT_ID)

    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["dashboards"] == []


def test_the_analyst_dashboard_capability_is_delivered_rather_than_awaiting_a_provider(
    console: _Console,
) -> None:
    """The capability list is what a deployment reads to find what is missing.

    Reporting this one `not_delivered` while the read surface answers would name work nobody has to
    do, and the dependency it named was a BI provider the read never needed.
    """
    workspace = console.get("/api/v1/workspace", actor=DEMO_ARCHITECT_ID)

    assert workspace.status_code == 200, workspace.text
    capabilities = {
        item["capability_id"]: item for item in workspace.json()["data"]["capabilities"]
    }
    assert capabilities["analyst-dashboard"]["state"] == "ready"
    assert capabilities["analyst-dashboard"]["dependency"] is None
