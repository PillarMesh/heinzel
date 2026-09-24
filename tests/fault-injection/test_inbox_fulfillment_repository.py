import sqlite3
from datetime import UTC, datetime

import pytest
from heinzel_request_management import (
    FulfillmentEvidenceReceipt,
    RequestManagementService,
    RequestNoValidPlan,
    RequestState,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def test_evidence_failure_rolls_back_no_valid_plan_and_request_transition() -> None:
    connection = sqlite3.connect(":memory:")
    request_repository = SQLiteRequestRepository(connection)
    fulfillment_repository = SQLiteFulfillmentRepository(request_repository)
    service = RequestManagementService(request_repository, clock=lambda: NOW)
    submitted = service.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="monthly close",
        question="What is net revenue?",
    )
    investigating = service.transition(
        "tenant-a",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=submitted.revision,
    )
    history_before = service.list_transition_history("tenant-a", submitted.request_id)
    record = RequestNoValidPlan(
        record_id="pending",
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        source_request_revision=investigating.revision,
        resulting_request_revision=investigating.revision + 1,
        reason_codes=("authority_unverifiable",),
        constraint_refs=(),
        smallest_changes=("Publish an approved authority observation.",),
        grounding_snapshot_digest=None,
        policy_snapshot_digest=None,
        created_at=NOW,
    )
    evidence = FulfillmentEvidenceReceipt(
        evidence_id="pending",
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        request_revision=investigating.revision + 1,
        outcome="no_valid_plan",
        proposal_id=None,
        proposal_revision=None,
        dependency_id=None,
        authority_refs=(),
        approval_ids=(),
        reason_codes=record.reason_codes,
        resulting_state=RequestState.NO_VALID_PLAN,
        created_at=NOW,
    )
    connection.execute(
        "CREATE TRIGGER fail_fulfillment_evidence "
        "BEFORE INSERT ON fulfillment_evidence_receipts "
        "BEGIN SELECT RAISE(ABORT, 'forced evidence failure'); END"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced evidence failure"):
        fulfillment_repository.store_no_valid_plan(
            record=record,
            evidence=evidence,
            actor_id="system:fulfillment",
            expected_revision=investigating.revision,
        )

    current = service.get("tenant-a", submitted.request_id)
    assert current == investigating
    assert service.list_transition_history("tenant-a", submitted.request_id) == history_before
    assert connection.execute("SELECT COUNT(*) FROM request_no_valid_plans").fetchone() == (0,)
    assert connection.execute("SELECT COUNT(*) FROM fulfillment_evidence_receipts").fetchone() == (
        0,
    )
