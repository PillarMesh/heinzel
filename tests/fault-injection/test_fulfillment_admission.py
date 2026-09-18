from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from heinzel_request_management import FulfillmentIntegrityError, RequestState

_SUPPORT = runpy.run_path(
    str(
        Path(__file__).parents[2] / "services/request-management/tests/test_fulfillment_approval.py"
    )
)
prepared_service = _SUPPORT["prepared_service"]


def test_evidence_failure_rolls_back_admission_and_execution_transition() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
    for authority_ref, actor_id in (
        ("principal:requester-a", "requester-a"),
        ("role:data_engineering_architect", "architect-a"),
    ):
        requirement = next(
            item for item in proposal.required_approvals if item.authority_ref == authority_ref
        )
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actor_id,
            authority_ref=authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    repository._connection.execute(
        "CREATE TRIGGER fail_admission_evidence "
        "BEFORE INSERT ON fulfillment_evidence_receipts "
        "BEGIN SELECT RAISE(ABORT, 'forced admission evidence failure'); END"
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

    current = requests.get("tenant-a", proposal.request_id)
    assert current.state is RequestState.AWAITING_APPROVAL
    assert current.revision == awaiting.revision
    assert repository.list_admissions("tenant-a", proposal.request_id) == ()
    assert repository.list_evidence("tenant-a", proposal.request_id) == ()
