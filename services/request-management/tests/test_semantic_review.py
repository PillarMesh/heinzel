from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pillarmesh_request_management import (
    DecisionKind,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SchemaSemanticChangeRequest,
    SQLiteRequestRepository,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def semantic_request_payload() -> dict[str, object]:
    return {
        "request_id": "req-semantic-1",
        "tenant_id": "tenant-a",
        "requester_id": "architect-a",
        "payload": {
            "request_type": "schema_semantic_change",
            "purpose": "Resolve the meaning of Refund",
            "review_bundle_id": "review-bundle-1",
            "review_bundle_digest": "a" * 64,
            "required_authority_refs": ("role:business_owner",),
        },
        "state": "submitted",
        "revision": 1,
        "submitted_at": NOW,
        "updated_at": NOW,
    }


def test_semantic_review_payload_remains_a_strict_discriminated_member() -> None:
    request = InboxRequest.model_validate(semantic_request_payload())

    assert isinstance(request.payload, SchemaSemanticChangeRequest)
    with pytest.raises(ValidationError):
        InboxRequest.model_validate(
            semantic_request_payload() | {"payload": {"request_type": "anything"}}
        )


def test_unresolved_review_returns_to_investigating_before_no_valid_plan() -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    request = InboxRequest.model_validate(semantic_request_payload())
    repository.save(request)

    investigating = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.INVESTIGATING,
        actor_id="reviewer-a",
        expected_revision=request.revision,
    )
    proposed = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.PROPOSED,
        actor_id="reviewer-a",
        expected_revision=investigating.revision,
    )
    awaiting = service.transition(
        "tenant-a",
        request.request_id,
        RequestState.AWAITING_APPROVAL,
        actor_id="reviewer-a",
        expected_revision=proposed.revision,
    )

    unresolved = service.record_review_decision(
        tenant_id="tenant-a",
        request_id=awaiting.request_id,
        request_revision=awaiting.revision,
        actor_id="reviewer-a",
        decision="unresolved",
    )
    assert unresolved.state is RequestState.INVESTIGATING

    terminal = service.transition(
        "tenant-a",
        unresolved.request_id,
        RequestState.NO_VALID_PLAN,
        actor_id="reviewer-a",
        expected_revision=unresolved.revision,
    )
    assert terminal.state is RequestState.NO_VALID_PLAN


def test_request_level_decision_kind_remains_exactly_plan_one_vocabulary() -> None:
    assert {kind.value for kind in DecisionKind} == {
        "approve",
        "reject",
        "request_changes",
    }
