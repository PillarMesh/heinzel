from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    DecisionKind,
    FulfillmentIntegrityError,
    FulfillmentNotVisible,
    FulfillmentReadService,
)

_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_fulfillment_approval.py")))
prepared_service = _SUPPORT["prepared_service"]
service = _SUPPORT["service"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]


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
