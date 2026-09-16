from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    DelegatedRequestProvenance,
    RequestManagementService,
    SQLiteRequestRepository,
)

NOW = datetime(2026, 9, 12, 22, tzinfo=UTC)
PURPOSE = "Review regional revenue"


def _provenance(**changes: object) -> DelegatedRequestProvenance:
    values: dict[str, object] = {
        "delegation_id": "delegation-1",
        "principal_ref": "principal:requester-a",
        "agent_client_ref": "agent-client:assistant-a",
        "purpose_digest": digest(PURPOSE),
        "delegation_authority_ref": "authority:delegations",
        "delegation_authority_revision": 3,
        "entitlement_snapshot_digest": "a" * 64,
        "policy_id": "policy-1",
        "policy_revision": 4,
        "policy_digest": "b" * 64,
        "invoked_at": NOW,
    }
    values.update(changes)
    return DelegatedRequestProvenance.model_validate(values)


def test_delegated_question_persists_human_and_agent_provenance_atomically() -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    service = RequestManagementService(repository, clock=lambda: NOW)
    provenance = _provenance()

    request = service.submit_question(
        tenant_id="tenant-a",
        requester_id="principal:requester-a",
        title="Revenue by region",
        purpose=PURPOSE,
        question="What is revenue by region?",
        delegated_agent=provenance,
    )

    assert request.delegated_agent == provenance
    assert repository.load("tenant-a", request.request_id) == request


@pytest.mark.parametrize(
    "provenance",
    (
        _provenance(principal_ref="principal:other"),
        _provenance(purpose_digest=digest("another purpose")),
    ),
)
def test_delegated_question_rejects_mismatched_authority_without_allocating_request(
    provenance: DelegatedRequestProvenance,
) -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    service = RequestManagementService(repository, clock=lambda: NOW)

    with pytest.raises(ValueError, match="provenance does not match"):
        service.submit_question(
            tenant_id="tenant-a",
            requester_id="principal:requester-a",
            title="Revenue by region",
            purpose=PURPOSE,
            question="What is revenue by region?",
            delegated_agent=provenance,
        )

    assert repository.list_inbox("tenant-a") == ()
    assert repository.peek_next_sequence("tenant-a") == 1
