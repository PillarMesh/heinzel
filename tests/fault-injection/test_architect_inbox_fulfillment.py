from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from heinzel_request_management import (
    FulfillmentAdmissionReceipt,
    FulfillmentIntegrityError,
    FulfillmentProposal,
    FulfillmentService,
    FulfillmentStaleRevision,
    RequestState,
)

_SUPPORT = runpy.run_path(
    str(
        Path(__file__).parents[2] / "services/request-management/tests/test_fulfillment_approval.py"
    )
)
prepared_service = _SUPPORT["prepared_service"]


def _approve_current_proposal(
    fulfillment: FulfillmentService, proposal: FulfillmentProposal, revision: int
) -> None:
    actors = {
        "principal:requester-a": "requester-a",
        "role:data_engineering_architect": "architect-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=revision,
        )


@pytest.mark.parametrize(
    "table",
    (
        "request_revisions",
        "transition_events",
        "fulfillment_admissions",
        "fulfillment_evidence_receipts",
    ),
)
def test_each_admission_write_failure_rolls_back_and_retry_converges(table: str) -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
    _approve_current_proposal(fulfillment, proposal, awaiting.revision)
    trigger = f"fail_request_fulfillment_{table}"
    repository._connection.execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'forced Plan 3B write failure'); END"
    )
    repository._connection.commit()

    with pytest.raises(FulfillmentIntegrityError) as captured:
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )

    assert str(captured.value) == "fulfillment operation failed integrity validation"
    assert captured.value.__cause__ is None

    assert requests.get("tenant-a", proposal.request_id) == awaiting
    assert repository.list_admissions("tenant-a", proposal.request_id) == ()
    assert repository.list_evidence("tenant-a", proposal.request_id) == ()

    repository._connection.execute(f"DROP TRIGGER {trigger}")
    repository._connection.commit()
    replayed = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    assert isinstance(replayed, FulfillmentAdmissionReceipt)
    assert replayed.execution_status == "ready_for_execution"
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.EXECUTING
    assert repository.list_admissions("tenant-a", proposal.request_id) == (replayed,)
    assert len(repository.list_evidence("tenant-a", proposal.request_id)) == 1


def test_cancellation_wins_a_stale_admission_race_without_partial_effects() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
    _approve_current_proposal(fulfillment, proposal, awaiting.revision)

    cancellation = fulfillment.cancel(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
        expected_revision=awaiting.revision,
    )

    with pytest.raises(FulfillmentStaleRevision, match="stale"):
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )

    assert cancellation.outcome == "cancelled"
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.CANCELLED
    assert repository.list_admissions("tenant-a", proposal.request_id) == ()
    assert repository.list_evidence("tenant-a", proposal.request_id) == (cancellation,)
