from __future__ import annotations

import runpy
from datetime import timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import digest
from heinzel_request_management import (
    DecisionKind,
    FulfillmentAdmissionReceipt,
    FulfillmentProposal,
    FulfillmentService,
    InboxRequest,
    RequestManagementService,
    RequestNoValidPlan,
    RequestState,
    ResolutionFailure,
    SQLiteFulfillmentRepository,
)
from pydantic import ValidationError

_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_answer_fulfillment.py")))
NOW = _SUPPORT["NOW"]
service = _SUPPORT["service"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]
snapshots = _SUPPORT["snapshots"]
StaticSnapshotResolver = _SUPPORT["StaticSnapshotResolver"]


class RoleResolver:
    def __init__(self) -> None:
        self.revoked: set[tuple[str, str]] = set()

    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        identity = (actor_id, authority_ref)
        return (
            tenant_id == "tenant-a"
            and identity not in self.revoked
            and identity
            in {
                ("requester-a", "principal:requester-a"),
                ("architect-a", "role:data_engineering_architect"),
            }
        )


def prepared_service() -> tuple[
    FulfillmentService,
    RequestManagementService,
    tuple[SQLiteFulfillmentRepository, FulfillmentProposal, InboxRequest],
]:
    """Seed one submitted proposal awaiting its approvals.

    The third element was declared `object`, so unpacking it failed and every name
    taken from it lost its type across the file. `propose_answer` also returns the
    full outcome union, so the proposal is narrowed once here rather than at each
    of the thirty-odd places that read a proposal-only field.
    """
    # `service` is loaded through `runpy`, so its own annotations do not survive.
    fulfillment: FulfillmentService
    requests: RequestManagementService
    repository: SQLiteFulfillmentRepository
    fulfillment, requests, repository = service()
    fulfillment._authority_role_resolver = RoleResolver()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    if not isinstance(proposal, FulfillmentProposal):
        raise AssertionError("the seeded answer request did not produce a proposal")
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    return fulfillment, requests, (repository, proposal, awaiting)


def test_exact_role_scoped_approvals_admit_only_current_proposal() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
    approvals = []
    for authority_ref, actor_id in (
        ("principal:requester-a", "requester-a"),
        ("role:data_engineering_architect", "architect-a"),
    ):
        requirement = next(
            item for item in proposal.required_approvals if item.authority_ref == authority_ref
        )
        approvals.append(
            fulfillment.record_approval(
                tenant_id="tenant-a",
                request_id=proposal.request_id,
                actor_id=actor_id,
                authority_ref=authority_ref,
                subject_digest=requirement.subject_digest,
                decision="approve",
                expected_revision=awaiting.revision,
            )
        )

    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(admission, FulfillmentAdmissionReceipt)

    assert admission.proposal_digest == digest(proposal)
    assert admission.approval_ids == tuple(item.approval_id for item in approvals)
    assert admission.execution_status == "ready_for_execution"
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.EXECUTING
    assert repository.list_admissions("tenant-a", proposal.request_id) == (admission,)


def test_actor_without_current_role_cannot_record_approval() -> None:
    fulfillment, _, prepared = prepared_service()
    _, proposal, awaiting = prepared
    requirement = next(
        item
        for item in proposal.required_approvals
        if item.authority_ref == "role:data_engineering_architect"
    )

    try:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="unrelated-a",
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    except ValueError as error:
        assert str(error) == "actor does not hold the required authority"
    else:
        raise AssertionError("approval unexpectedly accepted an unrelated actor")


def test_wrong_subject_digest_cannot_satisfy_requirement() -> None:
    fulfillment, _, prepared = prepared_service()
    _, proposal, awaiting = prepared

    try:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            authority_ref="role:data_engineering_architect",
            subject_digest="f" * 64,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    except ValueError as error:
        assert str(error) == "approval does not match an exact proposal requirement"
    else:
        raise AssertionError("approval unexpectedly accepted a different subject")


def test_role_revocation_after_approval_prevents_admission() -> None:
    fulfillment, _, prepared = prepared_service()
    _, proposal, awaiting = prepared
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
    resolver = fulfillment._authority_role_resolver
    assert isinstance(resolver, RoleResolver)
    resolver.revoked.add(("architect-a", "role:data_engineering_architect"))

    try:
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )
    except ValueError as error:
        assert "one current approval" in str(error)
    else:
        raise AssertionError("revoked approval unexpectedly admitted")


def test_rejection_binding_moves_request_to_rejected() -> None:
    fulfillment, requests, prepared = prepared_service()
    _, proposal, awaiting = prepared
    requirement = next(
        item
        for item in proposal.required_approvals
        if item.authority_ref == "role:data_engineering_architect"
    )

    fulfillment.record_approval(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        authority_ref=requirement.authority_ref,
        subject_digest=requirement.subject_digest,
        decision="reject",
        expected_revision=awaiting.revision,
    )

    assert requests.get("tenant-a", proposal.request_id).state is RequestState.REJECTED


def test_cancellation_is_terminal_and_prevents_admission() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared

    evidence = fulfillment.cancel(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
        expected_revision=awaiting.revision,
    )

    assert evidence.outcome == "cancelled"
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.CANCELLED
    assert repository.list_admissions("tenant-a", proposal.request_id) == ()
    try:
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=evidence.request_revision,
        )
    except ValueError as error:
        assert "awaiting approval" in str(error)
    else:
        raise AssertionError("cancelled request unexpectedly admitted")


def test_equivalent_policy_reresolution_allows_admission_against_fresh_snapshot() -> None:
    fulfillment, _, prepared = prepared_service()
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
    grounding, old_policy = snapshots()
    fresh_policy = old_policy.model_copy(
        update={
            "observed_at": NOW + timedelta(hours=2),
            "valid_until": NOW + timedelta(hours=3),
        }
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver((grounding, fresh_policy))
    fulfillment._clock = lambda: NOW + timedelta(hours=2)

    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(admission, FulfillmentAdmissionReceipt)

    assert admission.execution_status == "ready_for_execution"
    assert admission.policy_snapshot_digest != proposal.policy_snapshot_digest
    assert (
        repository.load_policy_snapshot("tenant-a", admission.policy_snapshot_digest).valid_until
        == fresh_policy.valid_until
    )


def test_changed_grounding_supersedes_proposal_with_exact_current_context() -> None:
    fulfillment, requests, prepared = prepared_service()
    _, proposal, awaiting = prepared
    grounding, old_policy = snapshots()
    changed_grounding = grounding.model_copy(
        update={
            "snapshot_id": "grounding-2",
            "catalog_publication_id": "publication-2",
            "created_at": NOW + timedelta(hours=2),
        }
    )
    fresh_policy = old_policy.model_copy(
        update={
            "observed_at": NOW + timedelta(hours=2),
            "valid_until": NOW + timedelta(hours=3),
        }
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver((changed_grounding, fresh_policy))
    fulfillment._clock = lambda: NOW + timedelta(hours=2)

    revised = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(revised, FulfillmentProposal)

    assert revised.revision == 2
    assert revised.prior_proposal_digest == digest(proposal)
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.PROPOSED


def test_changed_policy_supersedes_proposal_and_invalidates_old_approvals() -> None:
    fulfillment, requests, prepared = prepared_service()
    _, proposal, awaiting = prepared
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
    grounding, old_policy = snapshots()
    changed_policy = old_policy.model_copy(
        update={
            "permitted_data_product_refs": (),
            "observed_at": NOW + timedelta(hours=2),
            "valid_until": NOW + timedelta(hours=3),
        }
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver((grounding, changed_policy))
    fulfillment._clock = lambda: NOW + timedelta(hours=2)

    revised = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(revised, FulfillmentProposal)

    assert revised.revision == 2
    assert revised.prior_proposal_digest == digest(proposal)
    assert revised.subject.subject_kind == "disclosure_denial"
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.PROPOSED


def test_failed_policy_reresolution_records_no_valid_plan() -> None:
    fulfillment, requests, prepared = prepared_service()
    _, proposal, awaiting = prepared
    failure = ResolutionFailure(
        reason_codes=("policy_authority_unavailable",),
        constraint_refs=(),
        smallest_changes=("Restore approved policy authority.",),
        requester_safe_explanation="The current policy authority is unavailable.",
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver(failure)
    fulfillment._clock = lambda: NOW + timedelta(hours=2)

    result = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(result, RequestNoValidPlan)

    assert result.reason_codes == ("policy_authority_unavailable",)
    assert result.requester_safe_explanation == "The current policy authority is unavailable."
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.NO_VALID_PLAN


def test_snapshot_resolution_failure_preserves_requester_safe_explanation() -> None:
    fulfillment, requests, repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    safe_explanation = (
        "This local environment has no authoritative source configured for that question."
    )
    fulfillment._snapshot_resolver = StaticSnapshotResolver(
        ResolutionFailure(
            reason_codes=("local_scenario_not_supported",),
            constraint_refs=(),
            smallest_changes=("Configure an authoritative source.",),
            requester_safe_explanation=safe_explanation,
        )
    )

    result = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert result.requester_safe_explanation == safe_explanation
    assert repository.list_no_valid_plans("tenant-a", investigating.request_id) == (result,)
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.NO_VALID_PLAN


def test_resolution_failure_rejects_oversized_requester_safe_explanation() -> None:
    with pytest.raises(ValidationError, match="at most 1000 characters"):
        ResolutionFailure(
            reason_codes=("local_scenario_not_supported",),
            constraint_refs=(),
            smallest_changes=("Configure an authoritative source.",),
            requester_safe_explanation="x" * 1001,
        )


def test_plan_two_decision_is_refused_throughout_the_approval_window() -> None:
    """The proposal is written at the `proposed` revision, then the request advances.

    Keying the guard to one revision leaves it inert at `awaiting_approval`, which
    is exactly when fulfillment approvals are collected.
    """
    _fulfillment, requests, prepared = prepared_service()
    _repository, proposal, awaiting = prepared
    assert awaiting.state is RequestState.AWAITING_APPROVAL
    assert awaiting.revision > proposal.request_revision

    with pytest.raises(ValueError, match="fulfillment proposal"):
        requests.record_decision(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            request_revision=awaiting.revision,
            actor_id="architect-a",
            kind=DecisionKind.APPROVE,
            subject_digest="0" * 64,
        )
