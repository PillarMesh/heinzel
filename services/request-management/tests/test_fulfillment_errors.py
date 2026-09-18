from __future__ import annotations

import runpy
from datetime import timedelta
from pathlib import Path

import pytest
from heinzel_request_management import (
    FulfillmentAuthorityError,
    FulfillmentGroundingError,
    FulfillmentIntegrityError,
    FulfillmentOwnershipError,
    FulfillmentPolicyError,
    FulfillmentProposal,
    FulfillmentService,
    FulfillmentStaleRevision,
    InboxRequest,
    RequestManagementService,
    SQLiteFulfillmentRepository,
)

_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_answer_fulfillment.py")))
NOW = _SUPPORT["NOW"]
StaticSnapshotResolver = _SUPPORT["StaticSnapshotResolver"]
service = _SUPPORT["service"]
snapshots = _SUPPORT["snapshots"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]


class FailingSnapshotResolver:
    def resolve(self, *, tenant_id: str, request: object) -> object:
        raise RuntimeError(f"private resolver failure for {tenant_id}")


class NoRoles:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return False


def _submitted_proposal() -> tuple[
    FulfillmentService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
    FulfillmentProposal,
    InboxRequest,
]:
    fulfillment, requests, repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    return fulfillment, requests, repository, proposal, awaiting


def test_cross_tenant_public_operation_raises_sanitized_ownership_error() -> None:
    fulfillment, requests, repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)

    with pytest.raises(FulfillmentOwnershipError) as captured:
        fulfillment.propose_answer(
            tenant_id="tenant-b",
            request_id=investigating.request_id,
            actor_id="architect-b",
            expected_revision=investigating.revision,
        )

    assert str(captured.value) == "fulfillment request is not owned by this tenant"
    assert requests.get("tenant-a", investigating.request_id) == investigating
    assert repository.list_proposals("tenant-a", investigating.request_id) == ()


def test_stale_public_operation_raises_typed_revision_error() -> None:
    fulfillment, _, _, _, awaiting = _submitted_proposal()

    with pytest.raises(FulfillmentStaleRevision, match="revision is stale") as captured:
        fulfillment.cancel(
            tenant_id="tenant-a",
            request_id=awaiting.request_id,
            actor_id="requester-a",
            expected_revision=awaiting.revision - 1,
        )

    assert captured.value.__cause__ is None


def test_snapshot_resolver_failure_raises_sanitized_grounding_error() -> None:
    fulfillment, requests, _ = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    fulfillment._snapshot_resolver = FailingSnapshotResolver()

    with pytest.raises(FulfillmentGroundingError) as captured:
        fulfillment.propose_answer(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
            actor_id="architect-a",
            expected_revision=investigating.revision,
        )

    assert str(captured.value) == "fulfillment grounding could not be resolved"
    assert captured.value.__cause__ is None


def test_expired_reresolved_policy_raises_typed_policy_error() -> None:
    fulfillment, _, _, proposal, awaiting = _submitted_proposal()
    grounding, expired_policy = snapshots()
    expired_policy = expired_policy.model_copy(
        update={
            "observed_at": NOW + timedelta(hours=1),
            "valid_until": NOW + timedelta(hours=2),
        }
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver((grounding, expired_policy))
    fulfillment._clock = lambda: NOW + timedelta(hours=2)

    with pytest.raises(FulfillmentPolicyError, match="not current"):
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )


def test_policy_expiring_at_admission_time_must_be_reresolved() -> None:
    fulfillment, _, _, proposal, awaiting = _submitted_proposal()
    fulfillment._clock = lambda: NOW + timedelta(hours=1)

    with pytest.raises(FulfillmentPolicyError, match="not current"):
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )


def test_unheld_approval_role_raises_typed_authority_error() -> None:
    fulfillment, _, _, proposal, awaiting = _submitted_proposal()
    fulfillment._authority_role_resolver = NoRoles()
    requirement = proposal.required_approvals[0]

    with pytest.raises(FulfillmentAuthorityError, match="required authority"):
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="unrelated-a",
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )


def test_invalid_public_clock_raises_sanitized_integrity_error() -> None:
    fulfillment, requests, _ = service()
    submitted = requests.submit_question(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="monthly close",
        question="What is net revenue?",
    )
    fulfillment._clock = lambda: NOW.replace(tzinfo=None)

    with pytest.raises(FulfillmentIntegrityError) as captured:
        fulfillment.clarify_outcome(
            tenant_id="tenant-a",
            request_id=submitted.request_id,
            actor_id="architect-a",
            restated_request="Provide governed net revenue.",
            in_scope_summary="Approved product and metric.",
            out_of_scope_summary="No raw rows.",
            expected_revision=submitted.revision,
        )

    assert str(captured.value) == "fulfillment operation failed integrity validation"
    assert captured.value.__cause__ is None
