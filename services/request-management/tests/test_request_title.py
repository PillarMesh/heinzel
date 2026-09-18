from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_request_management import RequestManagementService, RequestState
from heinzel_request_management.repository import SQLiteRequestRepository
from pydantic import ValidationError

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.parametrize("access_request", (False, True))
def test_submitted_title_survives_a_transition_and_database_reopen(
    tmp_path: Path, access_request: bool
) -> None:
    database = str(tmp_path / "requests.sqlite")
    repository = SQLiteRequestRepository.open(database)
    service = RequestManagementService(repository, clock=lambda: NOW)
    title = "Quarterly revenue review"

    if access_request:
        request = service.submit_access_request(
            tenant_id="tenant-a",
            requester_id="requester-a",
            title=title,
            purpose="Quarterly reporting",
            data_product_id="product-revenue",
            requested_fields=("revenue",),
            access_mode="query",
            expires_at=NOW + timedelta(days=1),
        )
    else:
        request = service.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            title=title,
            purpose="Quarterly reporting",
            question="What was revenue?",
        )
    service.transition(
        "tenant-a",
        request.request_id,
        RequestState.CLARIFYING,
        actor_id="architect-a",
        expected_revision=1,
    )
    repository.close()

    reopened = SQLiteRequestRepository.open(database)
    try:
        loaded = reopened.load("tenant-a", request.request_id)
        assert loaded is not None
        assert loaded.title == title
        assert loaded.revision == 2
        assert reopened.list_inbox("tenant-a")[0].title == title
        with pytest.raises(KeyError, match="another tenant"):
            reopened.load("tenant-b", request.request_id)
    finally:
        reopened.close()


@pytest.mark.parametrize("title", ("", "x" * 16_001), ids=("empty", "too-long"))
def test_invalid_title_is_rejected_without_storing_a_request(title: str) -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteRequestRepository(connection)
    service = RequestManagementService(repository, clock=lambda: NOW)

    with pytest.raises(ValidationError, match="title"):
        service.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            title=title,
            purpose="Quarterly reporting",
            question="What was revenue?",
        )

    assert repository.list_inbox("tenant-a") == ()
    connection.close()


def test_existing_request_without_a_title_remains_readable() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteRequestRepository(connection)
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="Quarterly reporting",
        question="What was revenue?",
    )
    payload = request.model_dump_json(exclude={"title"})
    connection.execute("UPDATE request_revisions SET payload = ?", (payload,))
    connection.commit()

    loaded = repository.load("tenant-a", request.request_id)

    assert loaded is not None
    assert loaded.title is None
    assert loaded.payload == request.payload
    assert loaded.model_dump_json() == payload
    assert connection.execute("SELECT payload FROM request_revisions").fetchone()[0] == payload
    connection.close()
