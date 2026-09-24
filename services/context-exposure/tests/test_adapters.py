from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from heinzel_context_exposure import (
    AgentCallLimits,
    AgentInvocationProvenance,
    AskQuestionCommand,
    ListMyRequestsQuery,
    ReplyToClarificationCommand,
    RequestManagementAgentAdapter,
)
from heinzel_contract_model import digest
from heinzel_request_management import RequestManagementService, SQLiteRequestRepository

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _provenance(*, principal_ref: str = "principal:requester-a") -> AgentInvocationProvenance:
    return AgentInvocationProvenance(
        delegation_id="delegation-1",
        principal_ref=principal_ref,
        agent_client_ref="agent-client:assistant-a",
        purpose_digest=digest("monthly revenue analysis"),
        delegation_authority_ref="authority:delegations",
        delegation_authority_revision=1,
        entitlement_snapshot_digest="a" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="b" * 64,
        invoked_at=NOW,
    )


def test_request_list_adapter_uses_real_request_service_and_filters_current_principal() -> None:
    service = RequestManagementService(
        SQLiteRequestRepository(sqlite3.connect(":memory:")), clock=lambda: NOW
    )
    own = service.submit_question(
        tenant_id="tenant-a",
        requester_id="principal:requester-a",
        purpose="monthly revenue analysis",
        title="Revenue by region",
        question="What is revenue by region?",
    )
    service.submit_question(
        tenant_id="tenant-a",
        requester_id="principal:other",
        purpose="monthly revenue analysis",
        title="Private margin",
        question="What is private margin?",
    )
    service.submit_question(
        tenant_id="tenant-a",
        requester_id="principal:requester-a",
        purpose="monthly revenue analysis",
        question="Legacy untitled question",
    )
    adapter = RequestManagementAgentAdapter(service)

    result = adapter.list_my_requests(
        ListMyRequestsQuery(
            tenant_id="tenant-a",
            principal_ref="principal:requester-a",
            limit=10,
            provenance=_provenance(),
        )
    )

    assert tuple(item.request_id for item in result) == (own.request_id,)
    assert result[0].title == "Revenue by region"


def test_request_list_adapter_rejects_a_query_whose_provenance_scope_differs() -> None:
    service = RequestManagementService(
        SQLiteRequestRepository(sqlite3.connect(":memory:")), clock=lambda: NOW
    )
    adapter = RequestManagementAgentAdapter(service)

    with pytest.raises(ValueError, match="provenance scope"):
        adapter.list_my_requests(
            ListMyRequestsQuery(
                tenant_id="tenant-a",
                principal_ref="principal:requester-a",
                limit=10,
                provenance=_provenance(principal_ref="principal:other"),
            )
        )


def test_question_adapter_persists_dual_provenance_in_request_management() -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    service = RequestManagementService(repository, clock=lambda: NOW)
    adapter = RequestManagementAgentAdapter(service)

    result = adapter.ask_question(
        AskQuestionCommand(
            tenant_id="tenant-a",
            title="Revenue by region",
            purpose="monthly revenue analysis",
            question="What is revenue by region?",
            provenance=_provenance(),
            limits=AgentCallLimits(
                row_ceiling=100,
                result_byte_ceiling=100_000,
                scan_byte_ceiling=1_000_000,
                period_scan_byte_ceiling=10_000_000,
                statement_timeout_seconds=30,
            ),
        )
    )

    stored = repository.load("tenant-a", result.request_id)
    assert stored is not None and stored.delegated_agent is not None
    assert stored.requester_id == "principal:requester-a"
    assert stored.delegated_agent.agent_client_ref == "agent-client:assistant-a"
    assert stored.delegated_agent.policy_digest == "b" * 64


def test_clarification_adapter_uses_current_revision_and_requester_role() -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:"))
    service = RequestManagementService(repository, clock=lambda: NOW)
    adapter = RequestManagementAgentAdapter(service)
    created = adapter.ask_question(
        AskQuestionCommand(
            tenant_id="tenant-a",
            title="Revenue by region",
            purpose="monthly revenue analysis",
            question="What is revenue by region?",
            provenance=_provenance(),
            limits=AgentCallLimits(
                row_ceiling=100,
                result_byte_ceiling=100_000,
                scan_byte_ceiling=1_000_000,
                period_scan_byte_ceiling=10_000_000,
                statement_timeout_seconds=30,
            ),
        )
    )

    updated = adapter.reply_to_clarification(
        ReplyToClarificationCommand(
            tenant_id="tenant-a",
            request_id=created.request_id,
            expected_revision=created.revision,
            clarification="Use calendar months.",
            provenance=_provenance(),
        )
    )

    entries = repository.list_conversation("tenant-a", created.request_id)
    assert updated.revision == 2
    assert entries[0].actor_id == "principal:requester-a"
    assert entries[0].author_role == "requester"
