from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from heinzel_contract_model import digest
from heinzel_request_management import (
    DecisionKind,
    FulfillmentIntegrityError,
    FulfillmentNotVisible,
    FulfillmentReadService,
    RequestState,
    ResolutionFailure,
)

_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_fulfillment_approval.py")))
prepared_service = _SUPPORT["prepared_service"]
service = _SUPPORT["service"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]
StaticSnapshotResolver = _SUPPORT["StaticSnapshotResolver"]


def _refused_request(safe_explanation: str | None):
    fulfillment, requests, repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    fulfillment._snapshot_resolver = StaticSnapshotResolver(
        ResolutionFailure(
            reason_codes=("local_scenario_not_supported",),
            constraint_refs=(),
            smallest_changes=("private-remediation-canary",),
            requester_safe_explanation=safe_explanation,
        )
    )
    refusal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )
    return requests, repository, reader, investigating, refusal


def test_requester_view_never_contains_candidate_or_private_policy_content() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
    requester_requirement = next(
        item
        for item in proposal.required_approvals
        if item.authority_ref == "principal:requester-a"
    )
    fulfillment.record_approval(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
        authority_ref=requester_requirement.authority_ref,
        subject_digest=requester_requirement.subject_digest,
        decision="approve",
        expected_revision=awaiting.revision,
    )
    architect_requirement = next(
        item
        for item in proposal.required_approvals
        if item.authority_ref == "role:data_engineering_architect"
    )
    fulfillment.record_approval(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        authority_ref=architect_requirement.authority_ref,
        subject_digest=architect_requirement.subject_digest,
        decision="approve",
        expected_revision=awaiting.revision,
    )
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
    )

    serialized = view.model_dump_json()
    assert "Provide governed net revenue." in serialized
    assert "Net revenue is gross revenue" not in serialized
    assert "monthly close" not in serialized
    assert proposal.policy_snapshot_digest not in serialized
    assert proposal.grounding_snapshot_digest not in serialized
    assert len(view.own_decisions) == 1
    assert view.own_decisions[0].lifecycle == "fulfillment"
    assert view.own_decisions[0].authority_ref == "principal:requester-a"


def test_verified_delivery_publishes_the_exact_approved_answer_to_the_requester() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
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
            expected_revision=awaiting.revision,
        )
    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    receipt = fulfillment.deliver_answer(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="heinzel-runtime",
        expected_revision=admission.resulting_request_revision,
        executed_answer=proposal.subject,
        verification_refs=(proposal.subject.governed_dataset_refs[0],),
    )
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )
    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
    )

    assert receipt.answer == proposal.subject
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.DELIVERED
    assert view.delivered_answer == proposal.subject
    assert view.fulfillment_status == "closed"


def test_a_retried_delivery_returns_the_receipt_it_already_recorded() -> None:
    """A retry carries the revision the caller read before delivering; delivery moved it on."""
    fulfillment, _, prepared = prepared_service()
    _, proposal, awaiting = prepared
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
            expected_revision=awaiting.revision,
        )
    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    delivery = {
        "tenant_id": "tenant-a",
        "request_id": proposal.request_id,
        "actor_id": "heinzel-runtime",
        "expected_revision": admission.resulting_request_revision,
        "executed_answer": proposal.subject,
        "verification_refs": (proposal.subject.governed_dataset_refs[0],),
    }

    first = fulfillment.deliver_answer(**delivery)
    retried = fulfillment.deliver_answer(**delivery)

    assert retried == first


def test_delivery_rejects_an_answer_that_differs_from_the_approved_proposal() -> None:
    fulfillment, requests, prepared = prepared_service()
    _, proposal, awaiting = prepared
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
            expected_revision=awaiting.revision,
        )
    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    changed = proposal.subject.model_copy(update={"answer_text": "A different answer."})

    with pytest.raises(FulfillmentIntegrityError, match="differs from the approved proposal"):
        fulfillment.deliver_answer(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="heinzel-runtime",
            expected_revision=admission.resulting_request_revision,
            executed_answer=changed,
            verification_refs=(proposal.subject.governed_dataset_refs[0],),
        )

    assert requests.get("tenant-a", proposal.request_id).state is RequestState.EXECUTING


def test_requester_view_projects_only_the_safe_current_no_valid_plan_explanation() -> None:
    safe_explanation = (
        "This local environment has no authoritative source configured for that question."
    )
    _, _, reader, investigating, _ = _refused_request(safe_explanation)

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="requester-a",
    )

    assert view.no_valid_plan_explanation == safe_explanation
    assert "private-remediation-canary" not in view.model_dump_json()
    assert not hasattr(view, "no_valid_plans")


def test_requester_view_does_not_derive_safe_copy_from_internal_remediation() -> None:
    _, _, reader, investigating, _ = _refused_request(None)

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="requester-a",
    )

    assert view.no_valid_plan_explanation is None
    assert "private-remediation-canary" not in view.model_dump_json()


def test_requester_view_suppresses_no_valid_plan_from_another_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_explanation = "This explanation belongs to another revision."
    _, repository, reader, investigating, refusal = _refused_request(safe_explanation)
    stale_refusal = refusal.model_copy(
        update={"resulting_request_revision": refusal.resulting_request_revision + 1}
    )
    monkeypatch.setattr(repository, "list_no_valid_plans", lambda *_: (stale_refusal,))

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="requester-a",
    )

    assert view.no_valid_plan_explanation is None


def test_requester_view_suppresses_no_valid_plan_outside_its_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_explanation = "This explanation belongs to a terminal refusal."
    requests, _, reader, investigating, _ = _refused_request(safe_explanation)
    terminal = requests.get("tenant-a", investigating.request_id)
    non_terminal = terminal.model_copy(update={"state": RequestState.INVESTIGATING})
    monkeypatch.setattr(requests, "get", lambda *_: non_terminal)

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="requester-a",
    )

    assert view.no_valid_plan_explanation is None


def test_requester_view_excludes_other_actors_semantic_decisions() -> None:
    fulfillment, requests, repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    subject_digest = digest("plan-2-review-subject")
    for actor_id in ("requester-a", "architect-a"):
        requests.record_decision(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
            request_revision=investigating.revision,
            actor_id=actor_id,
            kind=DecisionKind.APPROVE,
            subject_digest=subject_digest,
        )
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
    )

    assert tuple((item.lifecycle, item.decision) for item in view.own_decisions) == (
        ("semantic_review", "approve"),
    )


def test_reviewer_sees_only_the_exact_subject_for_held_authority() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    view = reader.reviewer_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        authority_ref="role:data_engineering_architect",
    )

    assert view.subject == proposal.subject
    assert view.requirement.authority_ref == "role:data_engineering_architect"
    assert not hasattr(view, "policy_snapshot_digest")


def test_reviewer_does_not_reuse_a_decision_from_a_superseded_proposal() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, awaiting = prepared
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
        decision="approve",
        expected_revision=awaiting.revision,
    )
    requester_requirement = next(
        item
        for item in proposal.required_approvals
        if item.authority_ref == "principal:requester-a"
    )
    fulfillment.record_approval(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
        authority_ref=requester_requirement.authority_ref,
        subject_digest=requester_requirement.subject_digest,
        decision="approve",
        expected_revision=awaiting.revision,
    )
    investigating = requests.transition(
        "tenant-a",
        proposal.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    replacement = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=replacement.request_revision,
    )
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    view = reader.reviewer_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        authority_ref="role:data_engineering_architect",
    )

    assert view.proposal_id == replacement.proposal_id
    assert view.own_decisions == ()
    requester_view = reader.requester_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
    )
    assert (
        tuple(item for item in requester_view.own_decisions if item.lifecycle == "fulfillment")
        == ()
    )


def test_requester_requirement_never_projects_the_candidate_subject() -> None:
    """The requester holds a requirement, but it binds the clarified outcome.

    The acceptance requirement names the requesting principal so the requester can
    accept the restated question. Projecting the candidate for any held authority
    would hand them the unapproved answer text through the reviewer view.
    """
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    view = reader.reviewer_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
        authority_ref="principal:requester-a",
    )

    assert view.subject is None
    assert view.requirement.authority_ref == "principal:requester-a"
    assert view.requirement.subject_digest == digest(view.clarified_outcomes[-1])
    assert "answer_text" not in view.model_dump_json()


def test_reviewer_and_architect_views_fail_closed_without_role_authority() -> None:
    _, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=None,
    )

    with pytest.raises(FulfillmentNotVisible, match="not visible"):
        reader.reviewer_view(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            authority_ref="role:data_engineering_architect",
        )
    with pytest.raises(FulfillmentNotVisible, match="not visible"):
        reader.architect_view(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
        )


def test_architect_view_requires_held_role_and_returns_internal_projection() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    with pytest.raises(FulfillmentNotVisible, match="not visible"):
        reader.architect_view(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="unrelated-a",
        )

    view = reader.architect_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
    )

    assert view.request.request_id == proposal.request_id
    assert view.proposals == (proposal,)
    assert view.clarified_outcomes
    assert view.approvals == ()
    assert view.admissions == ()
    assert view.dependencies == ()
    assert view.no_valid_plans == ()
    assert view.denials == ()
    assert view.evidence == ()


def test_unrelated_actor_receives_generic_not_visible_error() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    with pytest.raises(FulfillmentNotVisible, match="not visible"):
        reader.requester_view(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="unrelated-a",
        )


def test_cross_tenant_view_receives_generic_not_visible_error() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    with pytest.raises(FulfillmentNotVisible, match="not visible"):
        reader.requester_view(
            tenant_id="tenant-b",
            request_id=proposal.request_id,
            actor_id="requester-a",
        )


def test_malformed_private_proposal_fails_closed_without_payload_in_error() -> None:
    fulfillment, requests, prepared = prepared_service()
    repository, proposal, _ = prepared
    private_marker = "private-candidate-answer"
    repository._connection.execute(
        "UPDATE fulfillment_proposals SET payload = ? WHERE proposal_id = ?",
        (f'{{"answer_text":"{private_marker}"}}', proposal.proposal_id),
    )
    repository._connection.commit()
    reader = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=fulfillment._authority_role_resolver,
    )

    with pytest.raises(FulfillmentIntegrityError) as captured:
        reader.architect_view(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
        )

    assert str(captured.value) == "stored fulfillment artifact is invalid"
    assert private_marker not in repr(captured.value)
    assert captured.value.__cause__ is None
