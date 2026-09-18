from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_contract_model import canonical_bytes
from heinzel_request_management import (
    ConversationAuthorRole,
    ConversationEntry,
    RequestManagementService,
    SQLiteRequestRepository,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "role",
    [
        "requester",
        "data_architect",
        "data_owner",
        "policy_approver",
        "budget_approver",
        "heinzel",
    ],
)
def test_author_role_survives_database_reopen_with_the_message_and_revision(
    tmp_path: Path,
    role: ConversationAuthorRole,
) -> None:
    path = str(tmp_path / "requests.sqlite")
    repository = SQLiteRequestRepository.open(path)
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a", requester_id="actor-a", purpose="Reporting", question="Which total?"
    )

    entry = service.append_conversation(
        "tenant-a",
        request.request_id,
        "owner-a",
        "Use the governed total.",
        expected_revision=1,
        author_role=role,
    )
    repository.close()
    reopened = SQLiteRequestRepository.open(path)
    try:
        assert reopened.list_conversation("tenant-a", request.request_id) == (entry,)
        assert reopened.list_conversation("tenant-a", request.request_id)[0].author_role == role
        stored = reopened.load("tenant-a", request.request_id)
        assert stored is not None
        assert stored.revision == 2
        with pytest.raises(KeyError):
            reopened.list_conversation("tenant-b", request.request_id)
    finally:
        reopened.close()


def test_legacy_entries_keep_their_bytes_and_have_no_inferred_role(tmp_path: Path) -> None:
    path = str(tmp_path / "requests.sqlite")
    repository = SQLiteRequestRepository.open(path)
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a", requester_id="actor-a", purpose="Reporting", question="Which total?"
    )
    entry = service.append_conversation(
        "tenant-a", request.request_id, "actor-a", "Please clarify.", expected_revision=1
    )
    with sqlite3.connect(path) as connection:
        before = bytes(connection.execute("SELECT payload FROM conversation_entries").fetchone()[0])

    assert b'"author_role"' not in before
    assert entry.author_role is None
    assert canonical_bytes(entry) == before
    assert service.list_conversation("tenant-a", request.request_id)[0].author_role is None
    with sqlite3.connect(path) as connection:
        after = bytes(connection.execute("SELECT payload FROM conversation_entries").fetchone()[0])
    assert after == before
    repository.close()


@pytest.mark.parametrize("role", ["administrator", "unknown", "", "DATA_OWNER"])
def test_unrecognized_conversation_roles_are_rejected(role: str) -> None:
    with pytest.raises(ValidationError):
        ConversationEntry.model_validate(
            {
                "entry_id": "entry-a",
                "request_id": "request-a",
                "request_revision": 2,
                "actor_id": "actor-a",
                "body": "Contribution",
                "created_at": NOW,
                "author_role": role,
            }
        )


def test_stale_and_foreign_writes_cannot_record_or_change_author_roles() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a", requester_id="actor-a", purpose="Reporting", question="Which total?"
    )
    entry = service.append_conversation(
        "tenant-a",
        request.request_id,
        "actor-a",
        "First contribution.",
        expected_revision=1,
        author_role="requester",
    )

    with pytest.raises(ValueError, match="stale"):
        service.append_conversation(
            "tenant-a",
            request.request_id,
            "actor-a",
            "Changed role.",
            expected_revision=1,
            author_role="data_owner",
        )
    with pytest.raises(KeyError):
        service.append_conversation(
            "tenant-b",
            request.request_id,
            "actor-a",
            "Foreign contribution.",
            expected_revision=2,
            author_role="data_owner",
        )

    assert service.list_conversation("tenant-a", request.request_id) == (entry,)
    assert service.get("tenant-a", request.request_id).revision == 2
    repository.close()


def test_message_role_and_request_revision_rollback_together_on_insert_failure(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "requests.sqlite")
    repository = SQLiteRequestRepository.open(path)
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = service.submit_question(
        tenant_id="tenant-a", requester_id="actor-a", purpose="Reporting", question="Which total?"
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_message BEFORE INSERT ON conversation_entries "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(ValueError):
        service.append_conversation(
            "tenant-a",
            request.request_id,
            "actor-a",
            "Owner contribution.",
            expected_revision=1,
            author_role="data_owner",
        )

    assert service.list_conversation("tenant-a", request.request_id) == ()
    assert service.get("tenant-a", request.request_id).revision == 1
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_message")
    entry = service.append_conversation(
        "tenant-a",
        request.request_id,
        "actor-a",
        "Owner contribution.",
        expected_revision=1,
        author_role="data_owner",
    )
    assert entry.author_role == "data_owner"
    assert entry.request_revision == 2
    repository.close()
