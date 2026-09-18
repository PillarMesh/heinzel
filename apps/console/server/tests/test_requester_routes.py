from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from heinzel_console import create_app
from heinzel_console.auth import TrustedActorContext
from heinzel_console.contracts import ActorRole, CreateRequestCommand
from heinzel_console.fixture_backend import FixtureConsoleBackend
from heinzel_console.fixture_data import BLOCKED_REQUEST_DIGEST, build_fixture_seed
from heinzel_console.request_intake import request_intake_content
from heinzel_contract_model import digest
from starlette.testclient import TestClient

_BLOCKED_REQUEST_ID = "request-blocked-acceptance"


def _seed_conversation_digest(request_id: str) -> str:
    """The digest the projection actually returns for a seeded thread.

    Binding to the read is the point: a constant would let the command and the
    projection drift apart, which is what the backend used to paper over.
    """
    return build_fixture_seed().request_details[request_id].conversation.conversation_digest


_REQUESTER_REQUEST_FIELDS = {
    "request_id",
    "kind",
    "state",
    "title",
    "requested_outcome",
    "revision",
    "updated_at",
    "result_page_available",
    "own_decisions",
    "clarified_outcome",
    "question",
    "denial_explanation",
    "no_valid_plan_explanation",
}

# Reviewer-only vocabulary from RequestDetailView and its proposal models. A requester projection
# that grows any of these has crossed the request fulfillment read boundary this surface depends on.
_FORBIDDEN_REQUESTER_FIELDS = {
    "candidate",
    "effective_scope",
    "exclusions",
    "required_authorities",
    "authority_summary",
    "decisions",
    "evidence",
    "evidence_refs",
    "proposal",
    "proposal_digest",
    "available_actions",
    "lifecycle",
    "risk",
    "blocked_reason",
    "intended_checks",
    "denied_checks",
}


def _context(
    *,
    actor_id: str = "actor-requester",
    tenant_id: str = "tenant-primary",
    active_role: ActorRole = "requester",
) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=(active_role,),
        active_role=active_role,
        session_id=f"session-{actor_id}",
    )


@contextmanager
def _client(
    *,
    backend: FixtureConsoleBackend | None = None,
    context: TrustedActorContext | None = None,
) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            backend=backend or FixtureConsoleBackend(),
            context_provider=lambda _: context or _context(),
            allowed_origin="http://testserver",
        )
    ) as client:
        yield client


def _command_headers(client: TestClient, key: str) -> dict[str, str]:
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    return {
        "Origin": "http://testserver",
        "X-CSRF-Token": token,
        "Idempotency-Key": key,
    }


def _stakeholder_question_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "expected_revision": 1,
        "request_digest": "c" * 64,
        "active_role": "requester",
        "title": "Weekly net revenue movement",
        "request": {
            "kind": "stakeholder_question",
            "purpose": "Prepare the weekly revenue review.",
            "question": "Why did net revenue move last week?",
        },
    }

    payload["request_digest"] = digest(
        request_intake_content(CreateRequestCommand.model_validate(payload))
    )
    return payload


def test_the_requester_list_contains_only_requests_owned_by_the_calling_requester() -> None:
    with _client() as owning_client:
        owned = owning_client.get("/api/v1/requests/mine")
    with _client(context=_context(actor_id="actor-other-requester")) as other_client:
        other = other_client.get("/api/v1/requests/mine")

    assert owned.status_code == 200
    assert {item["request_id"] for item in owned.json()["data"]} == {
        "request-answer",
        "request-access",
        _BLOCKED_REQUEST_ID,
        "request-stale",
        "request-no-valid-plan",
    }

    assert other.status_code == 200
    assert other.json()["data"] == []


def test_the_requester_projection_omits_candidate_scope_reviewer_and_evidence_vocabulary() -> None:
    with _client() as client:
        response = client.get("/api/v1/requests/mine")

    for item in response.json()["data"]:
        assert set(item) == _REQUESTER_REQUEST_FIELDS
        assert set(item) & _FORBIDDEN_REQUESTER_FIELDS == set()
        outcome = item["clarified_outcome"]
        if outcome is not None:
            assert set(outcome) == {
                "request_id",
                "revision",
                "statement_digest",
                "restated_request",
                "purpose",
                "in_scope_summary",
                "out_of_scope_summary",
                "accepted",
            }


def test_the_reviewer_request_detail_is_denied_to_a_requester() -> None:
    with _client() as client:
        detail = client.get(f"/api/v1/inbox/{_BLOCKED_REQUEST_ID}")
        inbox = client.get("/api/v1/inbox")

    assert detail.status_code == 404
    assert inbox.status_code == 404


def test_the_requester_list_is_denied_to_every_non_requester_role() -> None:
    with _client(context=_context(actor_id="actor-architect", active_role="data_architect")) as (
        client
    ):
        response = client.get("/api/v1/requests/mine")

    assert response.status_code == 404


def test_another_requesters_request_is_indistinguishable_from_a_request_that_does_not_exist() -> (
    None
):
    with _client(context=_context(actor_id="actor-other-requester")) as client:
        existing = client.get(f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/conversation")
        unknown = client.get("/api/v1/requests/request-absent/conversation")
        existing_outcome = client.get(f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome")
        unknown_outcome = client.get("/api/v1/requests/request-absent/clarified-outcome")

    assert existing.status_code == unknown.status_code == 404
    assert existing.json()["error"] == unknown.json()["error"]
    assert existing_outcome.status_code == unknown_outcome.status_code == 404
    assert existing_outcome.json()["error"] == unknown_outcome.json()["error"]


def test_a_created_request_rejects_a_browser_supplied_requester_identity() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/requests",
            json=_stakeholder_question_payload() | {"actor_id": "actor-someone-else"},
            headers=_command_headers(client, "idempotency-create-identity"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_a_created_request_returns_its_authoritative_revision_and_submitted_state() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/requests",
            json=_stakeholder_question_payload(),
            headers=_command_headers(client, "idempotency-create-question"),
        )

    assert response.status_code == 200
    created = response.json()["data"]
    assert set(created) == _REQUESTER_REQUEST_FIELDS
    assert created["revision"] == 1
    assert created["state"] == "submitted"
    assert created["kind"] == "stakeholder_question"
    assert created["question"] == "Why did net revenue move last week?"
    assert created["clarified_outcome"] is None
    assert created["no_valid_plan_explanation"] is None


def test_a_created_request_rejects_an_active_role_other_than_requester() -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/requests",
            json=_stakeholder_question_payload() | {"active_role": "data_architect"},
            headers=_command_headers(client, "idempotency-create-role"),
        )

    assert response.status_code == 422
    assert response.json()["error"]["field"] == "active_role"


def test_a_conversation_message_records_the_requesters_own_body_and_advances_the_revision() -> None:
    with _client() as client:
        response = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/conversation",
            json={
                "expected_revision": 2,
                "conversation_digest": _seed_conversation_digest(_BLOCKED_REQUEST_ID),
                "active_role": "requester",
                "body": "Weekly means the ISO week ending Sunday.",
            },
            headers=_command_headers(client, "idempotency-conversation-reply"),
        )

    assert response.status_code == 200
    conversation = response.json()["data"]
    assert conversation["revision"] == 3
    assert conversation["messages"][-1]["author_role"] == "requester"
    assert conversation["messages"][-1]["body"] == "Weekly means the ISO week ending Sunday."


def test_clarified_outcome_acceptance_requires_the_exact_digest_and_expected_revision() -> None:
    with _client() as client:
        stale_digest = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome/acceptance",
            json={
                "expected_revision": 2,
                "clarified_outcome_digest": "f" * 64,
                "active_role": "requester",
                "decision": "approve",
            },
            headers=_command_headers(client, "idempotency-acceptance-digest"),
        )
        stale_revision = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome/acceptance",
            json={
                "expected_revision": 1,
                "clarified_outcome_digest": BLOCKED_REQUEST_DIGEST,
                "active_role": "requester",
                "decision": "approve",
            },
            headers=_command_headers(client, "idempotency-acceptance-revision"),
        )

    assert stale_digest.status_code == 409
    assert stale_digest.json()["error"]["code"] == "stale_digest"
    assert stale_revision.status_code == 409
    assert stale_revision.json()["error"]["code"] == "stale_revision"


def test_accepting_the_outcome_binds_the_requester_authority_and_unblocks_it() -> None:
    with _client() as client:
        acceptance = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome/acceptance",
            json={
                "expected_revision": 2,
                "clarified_outcome_digest": BLOCKED_REQUEST_DIGEST,
                "active_role": "requester",
                "decision": "approve",
            },
            headers=_command_headers(client, "idempotency-acceptance-approve"),
        )
        followed = client.get("/api/v1/requests/mine")

    assert acceptance.status_code == 200
    assert acceptance.json()["data"]["accepted"] is True
    assert acceptance.json()["data"]["revision"] == 3

    accepted_request = next(
        item for item in followed.json()["data"] if item["request_id"] == _BLOCKED_REQUEST_ID
    )
    assert accepted_request["state"] == "proposed"
    assert accepted_request["clarified_outcome"]["accepted"] is True
    assert set(accepted_request) == _REQUESTER_REQUEST_FIELDS


def test_requesting_changes_leaves_the_outcome_unaccepted_and_the_request_clarifying() -> None:
    with _client() as client:
        response = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome/acceptance",
            json={
                "expected_revision": 2,
                "clarified_outcome_digest": BLOCKED_REQUEST_DIGEST,
                "active_role": "requester",
                "decision": "request_changes",
            },
            headers=_command_headers(client, "idempotency-acceptance-changes"),
        )
        followed = client.get("/api/v1/requests/mine")

    assert response.status_code == 200
    assert response.json()["data"]["accepted"] is False

    changed_request = next(
        item for item in followed.json()["data"] if item["request_id"] == _BLOCKED_REQUEST_ID
    )
    assert changed_request["state"] == "clarifying"


def test_a_requester_withdraws_their_own_open_request() -> None:
    with _client() as client:
        response = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 2, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal"),
        )
        followed = client.get("/api/v1/requests/mine")

    assert response.status_code == 200
    assert response.json()["data"]["state"] == "cancelled"
    assert response.json()["data"]["revision"] == 3
    withdrawn = next(
        item for item in followed.json()["data"] if item["request_id"] == _BLOCKED_REQUEST_ID
    )
    assert withdrawn["state"] == "cancelled"


def test_withdrawing_an_already_withdrawn_request_returns_it_unchanged() -> None:
    """A retried withdrawal must not report failure for a request that was withdrawn."""
    with _client() as client:
        first = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 2, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal-first"),
        )
        retried = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 3, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal-second"),
        )

    assert first.status_code == 200
    assert retried.status_code == 200
    assert retried.json()["data"]["state"] == "cancelled"
    assert retried.json()["data"]["revision"] == first.json()["data"]["revision"]


def test_a_withdrawal_against_a_stale_revision_is_refused() -> None:
    with _client() as client:
        response = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 1, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal-stale"),
        )
        followed = client.get("/api/v1/requests/mine")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_revision"
    unchanged = next(
        item for item in followed.json()["data"] if item["request_id"] == _BLOCKED_REQUEST_ID
    )
    assert unchanged["state"] == "clarifying"


def test_a_withdrawn_request_accepts_no_clarified_outcome_decision() -> None:
    """A withdrawn request must not come back to life through a stale acceptance."""
    with _client() as client:
        withdrawn = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 2, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal-then-accept"),
        )
        acceptance = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/clarified-outcome/acceptance",
            json={
                "expected_revision": 2,
                "clarified_outcome_digest": BLOCKED_REQUEST_DIGEST,
                "active_role": "requester",
                "decision": "approve",
            },
            headers=_command_headers(client, "idempotency-accept-after-withdrawal"),
        )
        followed = client.get("/api/v1/requests/mine")

    assert withdrawn.status_code == 200
    assert acceptance.status_code == 409
    still_withdrawn = next(
        item for item in followed.json()["data"] if item["request_id"] == _BLOCKED_REQUEST_ID
    )
    assert still_withdrawn["state"] == "cancelled"


def test_only_the_owning_requester_can_withdraw_a_request() -> None:
    with _client(context=_context(actor_id="actor-other-requester")) as client:
        response = client.post(
            f"/api/v1/requests/{_BLOCKED_REQUEST_ID}/withdrawal",
            json={"expected_revision": 2, "active_role": "requester"},
            headers=_command_headers(client, "idempotency-withdrawal-other"),
        )

    assert response.status_code == 404


def test_a_mismatched_intake_digest_returns_an_input_error_without_creating_a_request() -> None:
    with _client() as client:
        before = client.get("/api/v1/requests/mine").json()["data"]
        response = client.post(
            "/api/v1/requests",
            json=_stakeholder_question_payload() | {"title": "Changed after hashing"},
            headers=_command_headers(client, "idempotency-create-tampered"),
        )
        after = client.get("/api/v1/requests/mine").json()["data"]

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_digest_mismatch"
    assert response.json()["error"]["recovery_action"] == "correct_input"
    assert after == before
