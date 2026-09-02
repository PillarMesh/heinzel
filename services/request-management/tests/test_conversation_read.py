"""The conversation thread must be readable, not only appendable.

Addendum section 13.5 has PillarMesh ask business-meaning questions directly to
the requester while the data engineer observes, intervenes, or takes over. None
of that is possible while `conversation_entries` is a write-only table, and the
console's decision workspace cannot render a thread it cannot read.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from pillarmesh_request_management import RequestManagementService, RequestState
from pillarmesh_request_management.repository import SQLiteRequestRepository

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _service() -> tuple[RequestManagementService, SQLiteRequestRepository]:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"), _owns_connection=True)
    return RequestManagementService(repository, clock=lambda: NOW), repository


def _submitted_question(service: RequestManagementService, tenant_id: str = "tenant-a") -> str:
    request = service.submit_question(
        tenant_id=tenant_id,
        requester_id="requester-a",
        question="What is net revenue?",
        purpose="Board reporting",
    )
    return request.request_id


def test_conversation_entries_are_readable_in_append_order() -> None:
    service, _ = _service()
    request_id = _submitted_question(service)
    first = service.append_conversation(
        tenant_id="tenant-a",
        request_id=request_id,
        expected_revision=1,
        actor_id="architect-a",
        body="Which refunds count?",
    )
    second = service.append_conversation(
        tenant_id="tenant-a",
        request_id=request_id,
        expected_revision=2,
        actor_id="requester-a",
        body="Approved refunds only.",
    )

    entries = service.list_conversation("tenant-a", request_id)

    assert tuple(entry.entry_id for entry in entries) == (first.entry_id, second.entry_id)
    assert tuple(entry.actor_id for entry in entries) == ("architect-a", "requester-a")
    assert tuple(entry.body for entry in entries) == (
        "Which refunds count?",
        "Approved refunds only.",
    )


def test_an_empty_thread_reads_as_empty_rather_than_missing() -> None:
    service, _ = _service()
    request_id = _submitted_question(service)

    assert service.list_conversation("tenant-a", request_id) == ()


def test_another_tenant_cannot_read_the_thread() -> None:
    service, _ = _service()
    request_id = _submitted_question(service)
    service.append_conversation(
        tenant_id="tenant-a",
        request_id=request_id,
        expected_revision=1,
        actor_id="architect-a",
        body="Which refunds count?",
    )

    with pytest.raises(KeyError):
        service.list_conversation("tenant-b", request_id)


def test_a_missing_request_and_another_tenants_request_are_indistinguishable() -> None:
    """Reading must not answer "does this identifier exist" for a leaked id.

    The message echoes the identifier the caller supplied, which tells the caller
    nothing it did not already know; what must not differ is the wording, which
    is what would otherwise separate "absent" from "someone else's".
    """
    service, _ = _service()
    request_id = _submitted_question(service)

    with pytest.raises(KeyError) as foreign:
        service.list_conversation("tenant-b", request_id)
    with pytest.raises(KeyError) as absent:
        service.list_conversation("tenant-b", "req-does-not-exist")

    assert str(foreign.value).replace(request_id, "<id>") == str(absent.value).replace(
        "req-does-not-exist", "<id>"
    )


def test_threads_do_not_bleed_between_requests() -> None:
    service, _ = _service()
    first_request = _submitted_question(service)
    second_request = _submitted_question(service)
    service.append_conversation(
        tenant_id="tenant-a",
        request_id=first_request,
        expected_revision=1,
        actor_id="architect-a",
        body="First thread only.",
    )

    assert len(service.list_conversation("tenant-a", first_request)) == 1
    assert service.list_conversation("tenant-a", second_request) == ()


def test_the_thread_survives_a_lifecycle_transition() -> None:
    service, _ = _service()
    request_id = _submitted_question(service)
    service.append_conversation(
        tenant_id="tenant-a",
        request_id=request_id,
        expected_revision=1,
        actor_id="architect-a",
        body="Which refunds count?",
    )
    service.transition(
        "tenant-a",
        request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=2,
    )

    assert len(service.list_conversation("tenant-a", request_id)) == 1
