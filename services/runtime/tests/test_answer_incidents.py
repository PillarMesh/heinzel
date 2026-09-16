from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from pillarmesh_runtime import (
    AnswerExecutionIncidentProjectionUnavailable,
    AnswerExecutionIncidentProjector,
    AnswerExecutionReceipt,
    AnswerProductGenerationReference,
    AnswerQueryReference,
)
from pillarmesh_state import IncidentPersistenceError, IncidentRecord, SQLiteIncidentRepository

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


def _receipt(
    *,
    tenant_id: str = "tenant-a",
    outcome: Literal["succeeded", "provider_failed"] = "provider_failed",
    classification: Literal["transient_unavailable", "statement_rejected"] | None = (
        "transient_unavailable"
    ),
) -> AnswerExecutionReceipt:
    result_fields = (
        {
            "result_schema_digest": "c" * 64,
            "result_digest": "d" * 64,
            "result_ref": "result-1",
        }
        if outcome == "succeeded"
        else {}
    )
    return AnswerExecutionReceipt.model_validate(
        {
            "receipt_id": f"receipt-{tenant_id}",
            "tenant_id": tenant_id,
            "request_id": "request-1",
            "plan_digest": "a" * 64,
            "product_generation_refs": (
                AnswerProductGenerationReference(
                    product_ref=AnswerQueryReference(
                        artifact_id="product-1", version=1, digest="b" * 64
                    ),
                    generation=1,
                ),
            ),
            "attempt": 3,
            "started_at": NOW,
            "completed_at": NOW,
            "outcome": outcome,
            "provider_error_classification": classification,
            "row_count": 0,
            "byte_count": 0,
            "suppressed_group_count": 0,
            "freshness_observation_ref": "freshness-1",
            "quality_observation_ref": "quality-1",
            **result_fields,
        }
    )


def test_final_transient_query_failure_projects_one_replay_safe_incident(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = AnswerExecutionIncidentProjector(repository)
    receipt = _receipt()

    first = projector.project(receipt)
    replay = projector.project(receipt)

    assert replay == first
    assert first is not None
    assert repository.list_current("tenant-a") == (first,)
    assert first.kind == "query_failure"
    assert first.classification == "transient"
    assert first.failed_stage == "governed_query"
    assert first.source_record_ref == receipt.receipt_id
    assert first.evidence_refs == (receipt.receipt_id,)
    assert first.run_id is None
    assert first.run_attempt_number is None
    assert first.next_automatic_action is None
    assert first.allowed_operator_actions == ()


def test_success_projects_no_incident(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = AnswerExecutionIncidentProjector(repository)

    projected = projector.project(_receipt(outcome="succeeded", classification=None))

    assert projected is None
    assert repository.list_current("tenant-a") == ()


def test_permanent_query_failure_never_offers_retry(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = AnswerExecutionIncidentProjector(repository)

    incident = projector.project(_receipt(classification="statement_rejected"))

    assert incident is not None
    assert incident.classification == "permanent"
    assert incident.next_automatic_action is None
    assert incident.allowed_operator_actions == ()


def test_incident_identity_is_tenant_scoped(tmp_path: Path) -> None:
    repository = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = AnswerExecutionIncidentProjector(repository)

    tenant_a = projector.project(_receipt(tenant_id="tenant-a"))
    tenant_b = projector.project(_receipt(tenant_id="tenant-b"))

    assert tenant_a is not None and tenant_b is not None
    assert tenant_a.incident_id != tenant_b.incident_id
    assert repository.list_current("tenant-a") == (tenant_a,)
    assert repository.list_current("tenant-b") == (tenant_b,)


def test_expected_incident_persistence_outage_has_a_typed_boundary() -> None:
    class UnavailableRepository:
        def append(
            self, incident: IncidentRecord, *, expected_current_revision: int
        ) -> IncidentRecord:
            del incident, expected_current_revision
            raise IncidentPersistenceError(operation="append incident revision")

    projector = AnswerExecutionIncidentProjector(UnavailableRepository())

    with pytest.raises(AnswerExecutionIncidentProjectionUnavailable, match="unavailable"):
        projector.project(_receipt())


@pytest.mark.parametrize("failure", (ValueError("invalid incident"), AssertionError("bug")))
def test_programming_or_integrity_failure_is_not_flattened(failure: Exception) -> None:
    class FailingRepository:
        def append(
            self, incident: IncidentRecord, *, expected_current_revision: int
        ) -> IncidentRecord:
            del incident, expected_current_revision
            raise failure

    projector = AnswerExecutionIncidentProjector(FailingRepository())

    with pytest.raises(type(failure), match=str(failure)):
        projector.project(_receipt())
