from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import canonical_bytes
from heinzel_state import (
    IncidentConflictError,
    IncidentIntegrityError,
    IncidentNotFoundError,
    IncidentPersistenceError,
    IncidentRecord,
    SQLiteIncidentRepository,
    StaleIncidentRevisionError,
)

NOW = datetime(2026, 9, 12, 18, tzinfo=UTC)


def incident(
    *,
    incident_id: str = "incident-b",
    tenant_id: str = "tenant-a",
    revision: int = 1,
    updated_at: datetime = NOW,
    user_impact: str = "The current source interval has not been loaded.",
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=incident_id,
        tenant_id=tenant_id,
        revision=revision,
        kind="source_unavailable",
        classification="transient",
        last_successful_stage="contract_activation",
        failed_stage="extract",
        user_impact=user_impact,
        next_automatic_action="retry_transient_attempt",
        allowed_operator_actions=("retry_transient_attempt",),
        source_service="runtime",
        source_record_ref="attempt:run-a:1",
        run_id="run-a",
        run_attempt_number=1,
        evidence_refs=("evidence:attempt:run-a:1", "evidence:source:a"),
        opened_at=NOW,
        updated_at=updated_at,
    )


def test_lost_response_replay_returns_exact_stored_revision(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite3"
    repository = SQLiteIncidentRepository(path)
    original = incident()

    first = repository.append(original, expected_current_revision=0)
    repository.close()
    repository = SQLiteIncidentRepository(path)
    replay = repository.append(original, expected_current_revision=0)

    assert replay == first
    assert replay.source_record_ref == "attempt:run-a:1"
    assert replay.evidence_refs == (
        "evidence:attempt:run-a:1",
        "evidence:source:a",
    )
    assert repository.list_current("tenant-a") == (original,)


def test_append_requires_revision_one_then_contiguous_current_revision(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")

    with pytest.raises(StaleIncidentRevisionError):
        repository.append(incident(revision=2), expected_current_revision=0)

    repository.append(incident(), expected_current_revision=0)
    updated = incident(revision=2, updated_at=NOW + timedelta(seconds=1), user_impact="Delayed.")

    assert repository.append(updated, expected_current_revision=1) == updated
    with pytest.raises(StaleIncidentRevisionError):
        repository.append(
            incident(revision=3, updated_at=NOW + timedelta(seconds=2)),
            expected_current_revision=1,
        )


def test_conflicting_replay_cannot_replace_an_append_only_revision(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    original = incident()
    repository.append(original, expected_current_revision=0)

    with pytest.raises(IncidentConflictError, match="replay conflicts"):
        repository.append(
            incident(user_impact="Different payload."),
            expected_current_revision=0,
        )

    assert repository.load_current("tenant-a", original.incident_id) == original


def test_tenant_index_is_non_enumerating_and_allows_scoped_ids(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    tenant_a = incident(tenant_id="tenant-a")
    tenant_b = incident(tenant_id="tenant-b")
    repository.append(tenant_a, expected_current_revision=0)
    repository.append(tenant_b, expected_current_revision=0)

    assert repository.load_current("tenant-a", tenant_a.incident_id) == tenant_a
    assert repository.load_current("tenant-b", tenant_b.incident_id) == tenant_b
    with pytest.raises(IncidentNotFoundError, match="incident is unavailable"):
        repository.load_current("tenant-c", tenant_a.incident_id)


def test_list_returns_only_current_incident_revisions_in_canonical_order(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    second = incident(incident_id="incident-b")
    first = incident(incident_id="incident-a")
    repository.append(second, expected_current_revision=0)
    repository.append(first, expected_current_revision=0)
    current_second = incident(
        incident_id="incident-b",
        revision=2,
        updated_at=NOW + timedelta(seconds=1),
        user_impact="Still delayed.",
    )
    repository.append(current_second, expected_current_revision=1)

    assert repository.list_current("tenant-a") == (first, current_second)


def test_revisions_remain_durable_after_repository_reopens(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite3"
    first_repository = SQLiteIncidentRepository(path)
    original = incident()
    updated = incident(
        revision=2,
        updated_at=NOW + timedelta(seconds=1),
        user_impact="Retry is scheduled.",
    )
    first_repository.append(original, expected_current_revision=0)
    first_repository.append(updated, expected_current_revision=1)
    first_repository.close()

    reopened = SQLiteIncidentRepository(path)

    assert reopened.load_revision("tenant-a", original.incident_id, 1) == original
    assert reopened.load_current("tenant-a", original.incident_id) == updated


def test_malformed_stored_payload_fails_at_the_storage_boundary(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite3"
    repository = SQLiteIncidentRepository(path)
    original = incident()
    repository.append(original, expected_current_revision=0)
    repository.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incident_revisions SET payload = ? WHERE tenant_id = ? AND incident_id = ?",
            (b"not-json", "tenant-a", original.incident_id),
        )

    reopened = SQLiteIncidentRepository(path)

    with pytest.raises(IncidentIntegrityError, match="stored incident is invalid"):
        reopened.load_current("tenant-a", original.incident_id)


def test_payload_cannot_claim_a_tenant_different_from_its_index(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite3"
    repository = SQLiteIncidentRepository(path)
    original = incident()
    repository.append(original, expected_current_revision=0)
    repository.close()
    foreign_payload = canonical_bytes(incident(tenant_id="tenant-b"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incident_revisions SET payload = ? WHERE tenant_id = ? AND incident_id = ?",
            (foreign_payload, "tenant-a", original.incident_id),
        )

    reopened = SQLiteIncidentRepository(path)

    with pytest.raises(IncidentIntegrityError, match="stored incident is invalid"):
        reopened.load_current("tenant-a", original.incident_id)


def test_failed_current_projection_rolls_back_the_appended_revision(tmp_path: Path) -> None:
    path = tmp_path / "incidents.sqlite3"
    repository = SQLiteIncidentRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_incident_current BEFORE INSERT ON incident_current "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(IncidentPersistenceError, match="append incident revision"):
        repository.append(incident(), expected_current_revision=0)
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM incident_revisions").fetchone()

    assert count == (0,)
