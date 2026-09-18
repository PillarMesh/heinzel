import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Thread

import pytest
from heinzel_request_management import (
    ConversationEntry,
    DecisionBinding,
    DecisionKind,
    InboxRequest,
    RequestManagementService,
    RequestState,
)
from heinzel_request_management.repository import SQLiteRequestRepository, StaleRevisionError
from pydantic import ValidationError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def service() -> RequestManagementService:
    return RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)


def question(service: RequestManagementService, tenant_id: str = "tenant-a") -> InboxRequest:
    return service.submit_question(
        tenant_id=tenant_id,
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )


def artifact_next_sequence(repository: SQLiteRequestRepository, artifact_kind: str) -> int | None:
    row = repository._connection.execute(
        "SELECT next_sequence FROM artifact_sequences WHERE tenant_id = ? AND artifact_kind = ?",
        ("tenant-a", artifact_kind),
    ).fetchone()
    return None if row is None else int(row[0])


def artifact_row_count(repository: SQLiteRequestRepository, artifact_kind: str) -> int:
    if artifact_kind == "conversation":
        row = repository._connection.execute(
            "SELECT COUNT(*) FROM conversation_entries WHERE tenant_id = ?", ("tenant-a",)
        ).fetchone()
    else:
        assert artifact_kind == "decision"
        row = repository._connection.execute(
            "SELECT COUNT(*) FROM decision_bindings WHERE tenant_id = ?", ("tenant-a",)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_append_conversation_binds_entry_to_advanced_revision(
    service: RequestManagementService,
) -> None:
    request = question(service)

    entry = service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "Investigating governed metrics",
        expected_revision=request.revision,
    )

    assert entry.request_id == request.request_id
    assert entry.request_revision == 2
    assert entry.actor_id == "architect-a"
    assert entry.body == "Investigating governed metrics"
    assert entry.created_at == NOW
    assert service.get("tenant-a", request.request_id).revision == 2
    assert service.get("tenant-a", request.request_id).state is RequestState.SUBMITTED


def test_conversation_entry_id_is_deterministic_under_a_frozen_clock() -> None:
    first = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    second = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)

    first_entry = first.append_conversation(
        "tenant-a",
        question(first).request_id,
        "architect-a",
        "Investigating governed metrics",
        expected_revision=1,
    )
    second_entry = second.append_conversation(
        "tenant-a",
        question(second).request_id,
        "architect-a",
        "Investigating governed metrics",
        expected_revision=1,
    )

    assert first_entry.entry_id == second_entry.entry_id


def test_decision_rejects_stale_request_revision(service: RequestManagementService) -> None:
    request = question(service)
    service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "Investigating governed metrics",
        expected_revision=request.revision,
    )

    with pytest.raises(ValueError, match=r"^request revision is stale$"):
        service.record_decision(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest="0" * 64,
        )


def test_record_decision_binds_exact_revision_without_transitioning_request(
    service: RequestManagementService,
) -> None:
    request = question(service)

    decision = service.record_decision(
        tenant_id="tenant-a",
        request_id=request.request_id,
        request_revision=request.revision,
        actor_id="architect-a",
        kind=DecisionKind.APPROVE,
        subject_digest="0" * 64,
    )

    assert decision.request_id == request.request_id
    assert decision.request_revision == request.revision
    assert decision.kind is DecisionKind.APPROVE
    assert decision.created_at == NOW
    current = service.get("tenant-a", request.request_id)
    assert current.revision == request.revision
    assert current.state is RequestState.SUBMITTED


def test_decision_id_is_deterministic_under_a_frozen_clock() -> None:
    first = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    second = RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)
    first_request = question(first)
    second_request = question(second)

    first_decision = first.record_decision(
        tenant_id="tenant-a",
        request_id=first_request.request_id,
        request_revision=first_request.revision,
        actor_id="architect-a",
        kind=DecisionKind.APPROVE,
        subject_digest="0" * 64,
    )
    second_decision = second.record_decision(
        tenant_id="tenant-a",
        request_id=second_request.request_id,
        request_revision=second_request.revision,
        actor_id="architect-a",
        kind=DecisionKind.APPROVE,
        subject_digest="0" * 64,
    )

    assert first_decision.decision_id == second_decision.decision_id


def test_conversation_and_decision_refuse_another_tenant(
    service: RequestManagementService,
) -> None:
    request = question(service)

    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.append_conversation(
            "tenant-b", request.request_id, "attacker", "note", expected_revision=request.revision
        )
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.record_decision(
            tenant_id="tenant-b",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="attacker",
            kind=DecisionKind.APPROVE,
            subject_digest="0" * 64,
        )
    assert service.get("tenant-a", request.request_id).revision == request.revision


def test_stale_conversation_cannot_append_an_orphaned_entry(
    service: RequestManagementService,
) -> None:
    request = question(service)
    service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "first note",
        expected_revision=request.revision,
    )

    with pytest.raises(ValueError, match=r"^request revision is stale$"):
        service.append_conversation(
            "tenant-a",
            request.request_id,
            "architect-b",
            "stale note",
            expected_revision=request.revision,
        )

    assert service.get("tenant-a", request.request_id).revision == 2


def test_repository_rolls_back_conversation_when_revision_race_loses(tmp_path) -> None:
    database_path = tmp_path / "requests.db"
    repository = SQLiteRequestRepository.open(str(database_path))
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)
    first_entry = service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "first note",
        expected_revision=request.revision,
    )
    with pytest.raises(StaleRevisionError, match="request revision was not advanced"):
        repository.append_conversation(
            "tenant-a",
            request.request_id,
            request.revision,
            "architect-b",
            "stale note",
            NOW,
        )

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT entry_id FROM conversation_entries WHERE request_id = ? ORDER BY entry_id",
            (request.request_id,),
        ).fetchall()
    assert rows == [(first_entry.entry_id,)]


def test_two_connection_conversation_race_rolls_back_loser_artifact_sequence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "requests.db"
    initializer = RequestManagementService(
        SQLiteRequestRepository.open(str(database_path)), clock=lambda: NOW
    )
    request = question(initializer)
    barrier = Barrier(2)
    outcomes: Queue[ConversationEntry | Exception] = Queue()

    def synchronized_clock() -> datetime:
        barrier.wait(timeout=5)
        return NOW

    def append(actor_id: str) -> None:
        thread_service = RequestManagementService(
            SQLiteRequestRepository.open(str(database_path)), clock=synchronized_clock
        )
        try:
            outcomes.put(
                thread_service.append_conversation(
                    "tenant-a",
                    request.request_id,
                    actor_id,
                    "racing note",
                    expected_revision=request.revision,
                )
            )
        except Exception as error:
            outcomes.put(error)

    first = Thread(target=append, args=("architect-a",))
    second = Thread(target=append, args=("architect-b",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    results = [outcomes.get_nowait() for _ in range(2)]
    assert sum(isinstance(result, ConversationEntry) for result in results) == 1
    stale_errors = [
        result
        for result in results
        if isinstance(result, ValueError) and str(result) == "request revision is stale"
    ]
    assert len(stale_errors) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT next_sequence FROM artifact_sequences "
            "WHERE tenant_id = ? AND artifact_kind = ?",
            ("tenant-a", "conversation"),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_entries WHERE request_id = ?",
            (request.request_id,),
        ).fetchone() == (1,)


def test_decision_lost_race_leaves_no_artifact_or_sequence(tmp_path: Path) -> None:
    database_path = tmp_path / "requests.db"
    initializer = RequestManagementService(
        SQLiteRequestRepository.open(str(database_path)), clock=lambda: NOW
    )
    request = question(initializer)
    reads_complete = Barrier(2)
    conversation_done = Event()
    conversation_outcome: Queue[ConversationEntry | Exception] = Queue()
    decision_outcome: Queue[DecisionBinding | Exception] = Queue()

    def conversation_clock() -> datetime:
        reads_complete.wait(timeout=5)
        return NOW

    def decision_clock() -> datetime:
        reads_complete.wait(timeout=5)
        if not conversation_done.wait(timeout=5):
            raise RuntimeError("conversation race winner did not finish")
        return NOW

    def append_conversation() -> None:
        thread_service = RequestManagementService(
            SQLiteRequestRepository.open(str(database_path)), clock=conversation_clock
        )
        try:
            conversation_outcome.put(
                thread_service.append_conversation(
                    "tenant-a",
                    request.request_id,
                    "architect-a",
                    "winning note",
                    expected_revision=request.revision,
                )
            )
        except Exception as error:
            conversation_outcome.put(error)
        finally:
            conversation_done.set()

    def record_decision() -> None:
        thread_service = RequestManagementService(
            SQLiteRequestRepository.open(str(database_path)), clock=decision_clock
        )
        try:
            decision_outcome.put(
                thread_service.record_decision(
                    tenant_id="tenant-a",
                    request_id=request.request_id,
                    request_revision=request.revision,
                    actor_id="architect-b",
                    kind=DecisionKind.APPROVE,
                    subject_digest="0" * 64,
                )
            )
        except Exception as error:
            decision_outcome.put(error)

    conversation_thread = Thread(target=append_conversation)
    decision_thread = Thread(target=record_decision)
    conversation_thread.start()
    decision_thread.start()
    conversation_thread.join(timeout=10)
    decision_thread.join(timeout=10)

    assert not conversation_thread.is_alive()
    assert not decision_thread.is_alive()
    assert isinstance(conversation_outcome.get_nowait(), ConversationEntry)
    decision_result = decision_outcome.get_nowait()
    assert isinstance(decision_result, ValueError)
    assert str(decision_result) == "request revision is stale"
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT next_sequence FROM artifact_sequences "
                "WHERE tenant_id = ? AND artifact_kind = ?",
                ("tenant-a", "decision"),
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM decision_bindings WHERE request_id = ?",
            (request.request_id,),
        ).fetchone() == (0,)


def test_conversation_and_decision_models_reject_naive_timestamps_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="timestamp"):
        ConversationEntry(
            entry_id="entry-1",
            request_id="request-1",
            request_revision=1,
            actor_id="architect-a",
            body="note",
            created_at=datetime(2026, 8, 17, 12),
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DecisionBinding.model_validate(
            {
                "decision_id": "decision-1",
                "request_id": "request-1",
                "request_revision": 1,
                "actor_id": "architect-a",
                "kind": "approve",
                "subject_digest": "0" * 64,
                "created_at": NOW,
                "unexpected": "must fail",
            }
        )


def test_invalid_artifact_inputs_leave_sequences_unallocated() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)

    with pytest.raises(ValidationError, match="body"):
        service.append_conversation(
            "tenant-a",
            request.request_id,
            "architect-a",
            "",
            expected_revision=request.revision,
        )
    with pytest.raises(ValidationError, match="subject_digest"):
        service.record_decision(
            tenant_id="tenant-a",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest="not-a-digest",
        )

    assert artifact_next_sequence(repository, "conversation") is None
    assert artifact_next_sequence(repository, "decision") is None
    assert artifact_row_count(repository, "conversation") == 0
    assert artifact_row_count(repository, "decision") == 0


def test_repository_refuses_another_tenant_before_invalid_artifact_inputs() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)

    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.append_conversation(
            "tenant-b",
            request.request_id,
            request.revision,
            "attacker",
            "",
            NOW,
        )
    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.record_decision(
            "tenant-b",
            request.request_id,
            request.revision,
            "attacker",
            DecisionKind.APPROVE,
            "not-a-digest",
            NOW,
        )

    assert artifact_next_sequence(repository, "conversation") is None
    assert artifact_next_sequence(repository, "decision") is None
    assert artifact_row_count(repository, "conversation") == 0
    assert artifact_row_count(repository, "decision") == 0


def test_service_refuses_another_tenant_before_invalid_artifact_inputs() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)

    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.append_conversation(
            "tenant-b",
            request.request_id,
            "attacker",
            "",
            expected_revision=request.revision,
        )
    with pytest.raises(KeyError, match="belongs to another tenant"):
        service.record_decision(
            tenant_id="tenant-b",
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="attacker",
            kind=DecisionKind.APPROVE,
            subject_digest="not-a-digest",
        )


def test_stale_repository_conversation_attempt_leaves_sequence_unchanged() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)
    service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "first note",
        expected_revision=request.revision,
    )
    sequence_before = artifact_next_sequence(repository, "conversation")

    with pytest.raises(StaleRevisionError, match="request revision was not advanced"):
        repository.append_conversation(
            "tenant-a",
            request.request_id,
            request.revision,
            "architect-b",
            "stale note",
            NOW,
        )

    assert artifact_next_sequence(repository, "conversation") == sequence_before
    assert artifact_row_count(repository, "conversation") == 1


def test_stale_repository_decision_attempt_leaves_sequence_unchanged() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)
    service.append_conversation(
        "tenant-a",
        request.request_id,
        "architect-a",
        "first note",
        expected_revision=request.revision,
    )
    sequence_before = artifact_next_sequence(repository, "decision")

    with pytest.raises(StaleRevisionError, match="request revision was not advanced"):
        repository.record_decision(
            "tenant-a",
            request.request_id,
            request.revision,
            "architect-b",
            DecisionKind.APPROVE,
            "0" * 64,
            NOW,
        )

    assert artifact_next_sequence(repository, "decision") == sequence_before
    assert artifact_row_count(repository, "decision") == 0


def test_failed_conversation_persistence_rolls_back_its_sequence() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)
    entry_id = repository._artifact_id("conversation", "tenant-a", 1)
    repository._connection.execute(
        "INSERT INTO conversation_entries "
        "(entry_id, request_id, request_revision, tenant_id, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (entry_id, "another-request", 1, "tenant-a", NOW.isoformat(), b"{}"),
    )
    repository._connection.commit()

    with pytest.raises(StaleRevisionError, match="request revision was not advanced"):
        repository.append_conversation(
            "tenant-a",
            request.request_id,
            request.revision,
            "architect-a",
            "note",
            NOW,
        )

    assert artifact_next_sequence(repository, "conversation") is None
    assert artifact_row_count(repository, "conversation") == 1
    assert service.get("tenant-a", request.request_id).revision == request.revision


def test_failed_decision_persistence_rolls_back_its_sequence() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = question(service)
    decision_id = repository._artifact_id("decision", "tenant-a", 1)
    repository._connection.execute(
        "INSERT INTO decision_bindings "
        "(decision_id, request_id, request_revision, tenant_id, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (decision_id, "another-request", 1, "tenant-a", NOW.isoformat(), b"{}"),
    )
    repository._connection.commit()

    with pytest.raises(StaleRevisionError, match="request revision was not advanced"):
        repository.record_decision(
            "tenant-a",
            request.request_id,
            request.revision,
            "architect-a",
            DecisionKind.APPROVE,
            "0" * 64,
            NOW,
        )

    assert artifact_next_sequence(repository, "decision") is None
    assert artifact_row_count(repository, "decision") == 1
