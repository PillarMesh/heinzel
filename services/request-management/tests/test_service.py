import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_request_management import (
    InboxRequest,
    RequestManagementService,
    RequestState,
)
from heinzel_request_management.repository import SQLiteRequestRepository
from pydantic import ValidationError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def test_request_repository_does_not_close_a_caller_owned_connection() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteRequestRepository(connection)

    repository.close()

    assert connection.execute("SELECT 1").fetchone() == (1,)
    connection.close()


def test_opened_request_repository_closes_its_owned_connection(tmp_path: Path) -> None:
    repository = SQLiteRequestRepository.open(str(tmp_path / "requests.sqlite"))

    repository.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        repository.list_inbox("tenant-a")


def test_question_and_access_request_share_ordered_inbox() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    question = service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )
    access = service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )

    assert [item.request_id for item in service.list_inbox("tenant-a")] == [
        question.request_id,
        access.request_id,
    ]


def access_request(service: RequestManagementService) -> InboxRequest:
    return service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("refund_amount",),
        access_mode="query",
        expires_at=NOW + timedelta(days=1),
    )


def question(service: RequestManagementService) -> InboxRequest:
    return service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )


def insert_durable_request_payload(
    repository: SQLiteRequestRepository, payload: dict[str, object]
) -> None:
    serialized_request = {
        "request_id": "req-invalid",
        "tenant_id": "tenant-a",
        "requester_id": "finance-user",
        "payload": payload,
        "state": "submitted",
        "revision": 1,
        "submitted_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    repository._connection.execute(
        "INSERT INTO request_revisions "
        "(request_id, revision, tenant_id, submitted_at, payload) VALUES (?, ?, ?, ?, ?)",
        (
            "req-invalid",
            1,
            "tenant-a",
            NOW.isoformat(),
            json.dumps(serialized_request).encode("utf-8"),
        ),
    )
    repository._connection.commit()


def test_request_cannot_skip_approval_state() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    with pytest.raises(ValueError, match="submitted -> executing"):
        service.transition(
            "tenant-a",
            request.request_id,
            RequestState.EXECUTING,
            actor_id="architect-a",
            expected_revision=request.revision,
        )


def test_one_tenant_cannot_see_or_move_another_tenants_request() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    assert service.list_inbox("tenant-b") == ()
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.get("tenant-b", request.request_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.transition(
            "tenant-b",
            request.request_id,
            RequestState.CLARIFYING,
            actor_id="attacker",
            expected_revision=request.revision,
        )


def test_access_expiry_must_be_aware_and_after_submission() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)

    with pytest.raises(ValueError, match="expires_at"):
        service.submit_access_request(
            tenant_id="tenant-a",
            requester_id="analyst-a",
            purpose="refund investigation",
            data_product_id="finance-revenue",
            requested_fields=("refund_amount",),
            access_mode="query",
            expires_at=NOW - timedelta(days=1),
        )


def test_stale_caller_loses_after_another_transition_advances_revision() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    advanced = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=request.revision,
    )

    assert advanced.revision == 2
    with pytest.raises(ValueError, match=r"^request revision is stale$"):
        service.transition(
            "tenant-a",
            request.request_id,
            RequestState.SUBMITTED,
            actor_id="architect-b",
            expected_revision=request.revision,
        )


def test_transition_retains_prior_append_only_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "requests.db"
    repository = SQLiteRequestRepository.open(str(database_path))
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = access_request(service)

    service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=request.revision,
    )

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT revision, payload FROM request_revisions "
            "WHERE request_id = ? ORDER BY revision",
            (request.request_id,),
        ).fetchall()

    assert [(revision, json.loads(payload)["state"]) for revision, payload in rows] == [
        (1, "submitted"),
        (2, "clarifying"),
    ]


def test_transition_history_attributes_each_immutable_revision() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)

    clarifying = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=request.revision,
    )
    service.transition(
        "tenant-a",
        request.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-b",
        expected_revision=clarifying.revision,
    )

    history = service.list_transition_history("tenant-a", request.request_id)

    assert [
        (event.request_revision, event.actor_id, event.from_state, event.to_state)
        for event in history
    ] == [
        (2, "architect-a", RequestState.SUBMITTED, RequestState.CLARIFYING),
        (3, "architect-b", RequestState.CLARIFYING, RequestState.INVESTIGATING),
    ]
    assert history[0].created_at == NOW
    assert history[0].event_id != history[1].event_id


def test_transition_history_refuses_another_tenant() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)
    service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=request.revision,
    )

    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.list_transition_history("tenant-b", request.request_id)


def test_transition_event_failure_rolls_back_request_revision_and_sequence() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = access_request(service)
    repository._connection.execute(
        "CREATE TRIGGER fail_transition_event BEFORE INSERT ON transition_events "
        "BEGIN SELECT RAISE(ABORT, 'forced transition event failure'); END"
    )
    repository._connection.commit()

    with pytest.raises(ValueError, match=r"^request revision is stale$"):
        service.transition(
            "tenant-a",
            request.request_id,
            RequestState.CLARIFYING,
            actor_id="architect-a",
            expected_revision=request.revision,
        )

    assert service.get("tenant-a", request.request_id).revision == request.revision
    assert service.list_transition_history("tenant-a", request.request_id) == ()
    assert (
        repository._connection.execute(
            "SELECT next_sequence FROM artifact_sequences "
            "WHERE tenant_id = ? AND artifact_kind = ?",
            ("tenant-a", "transition"),
        ).fetchone()
        is None
    )


def test_durable_deserialization_rejects_unknown_payload_field() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    insert_durable_request_payload(
        repository,
        {
            "request_type": "stakeholder_question",
            "purpose": "monthly close",
            "question": "What were net refunds yesterday?",
            "unexpected": "must fail",
        },
    )

    with pytest.raises(ValidationError, match="unexpected"):
        repository.load("tenant-a", "req-invalid")


def test_durable_deserialization_rejects_unknown_request_discriminator() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    insert_durable_request_payload(
        repository,
        {
            "request_type": "future_request",
            "purpose": "monthly close",
            "question": "What were net refunds yesterday?",
        },
    )

    with pytest.raises(ValidationError, match="request_type"):
        repository.load("tenant-a", "req-invalid")


def test_same_tenant_submissions_have_distinct_deterministic_ids_under_frozen_clock() -> None:
    first_service = RequestManagementService(
        SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW
    )
    second_service = RequestManagementService(
        SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW
    )

    first_ids = (question(first_service).request_id, question(first_service).request_id)
    second_ids = (question(second_service).request_id, question(second_service).request_id)

    assert first_ids[0] != first_ids[1]
    assert first_ids == second_ids


def test_stored_request_payload_is_the_canonical_form_the_platform_digests() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )

    payload = repository._connection.execute("SELECT payload FROM request_revisions").fetchone()[0]

    assert bytes(payload) == canonical_bytes(request)
    assert hashlib.sha256(payload).hexdigest() == digest(request)


def test_request_transition_table_is_exact_and_terminal_states_have_no_successors() -> None:
    """Every request state has exactly these successors; cancellation is handled separately."""
    from heinzel_request_management.service import _TRANSITIONS

    assert set(_TRANSITIONS) == set(RequestState)
    assert {
        source.value: frozenset(target.value for target in targets)
        for source, targets in _TRANSITIONS.items()
    } == {
        "submitted": frozenset({"clarifying", "investigating"}),
        "clarifying": frozenset({"investigating", "submitted"}),
        "investigating": frozenset({"proposed", "executing", "no_valid_plan"}),
        "proposed": frozenset({"awaiting_approval", "investigating"}),
        "awaiting_approval": frozenset({"executing", "rejected", "investigating"}),
        "executing": frozenset({"verifying", "failed"}),
        "verifying": frozenset({"delivered", "failed"}),
        "delivered": frozenset({"monitoring", "retired"}),
        "monitoring": frozenset({"retired"}),
        "rejected": frozenset(),
        "no_valid_plan": frozenset(),
        "cancelled": frozenset(),
        "failed": frozenset(),
        "retired": frozenset(),
    }


def test_terminal_request_states_are_exact_and_are_the_states_without_successors() -> None:
    """Cancellation is admitted from every state except these, so the set must stay exact."""
    from heinzel_request_management.service import _TRANSITIONS, _terminal_states

    terminal = frozenset(state.value for state in _terminal_states())

    assert terminal == frozenset({"rejected", "no_valid_plan", "cancelled", "failed", "retired"})
    assert terminal == frozenset(
        state.value for state, targets in _TRANSITIONS.items() if not targets
    )


def test_cancelled_request_cannot_be_cancelled_again() -> None:
    service = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    request = access_request(service)
    cancelled = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CANCELLED,
        actor_id="architect-a",
        expected_revision=request.revision,
    )

    with pytest.raises(ValueError, match="cancelled -> cancelled is not allowed"):
        service.transition(
            "tenant-a",
            cancelled.request_id,
            RequestState.CANCELLED,
            actor_id="architect-a",
            expected_revision=cancelled.revision,
        )
