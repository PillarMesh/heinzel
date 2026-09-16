from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Never

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pillarmesh_access_control import CurrentEntitlementSnapshot
from pillarmesh_context_exposure import (
    CurrentDelegationAssertion,
    DescribeMetricQuery,
    GetImpactQuery,
    RequestManagementAgentAdapter,
    SearchCatalogQuery,
)
from pillarmesh_context_exposure.app import (
    AgentInterfaceConfigurationError,
    AgentInterfacePorts,
    build_application,
    build_production_application,
)
from pillarmesh_context_exposure.mcp_server import build_server
from pillarmesh_context_exposure.settings import AppSettings
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AnswerScopePolicy,
    RequestManagementService,
    SQLiteRequestRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
PURPOSE = "monthly revenue analysis"
PRINCIPAL = "principal:requester-a"
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=1, digest="a" * 64)
SEMANTIC = ArtifactReference(artifact_id="semantic:revenue", version=1, digest="b" * 64)


def test_agent_startup_refuses_when_current_authority_ports_are_unconfigured() -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:", check_same_thread=False))
    request_service = RequestManagementService(repository, clock=lambda: NOW)
    settings = AppSettings.model_validate(
        {
            "tenant_id": "tenant-a",
            "delegation_id": "delegation-1",
            "principal_ref": "principal:requester-a",
            "agent_client_ref": "agent-client:assistant-a",
            "purpose": "monthly revenue analysis",
        }
    )

    with pytest.raises(AgentInterfaceConfigurationError, match="context-exposure deployment"):
        build_production_application(settings)

    assert request_service.list_inbox("tenant-a") == ()


class _Delegations:
    def resolve_current(self, **_identity: str) -> CurrentDelegationAssertion:
        return CurrentDelegationAssertion(
            delegation_id="delegation-1",
            tenant_id="tenant-a",
            principal_ref=PRINCIPAL,
            agent_client_ref="agent-client:assistant-a",
            decision="active",
            authority_ref="authority:delegations",
            authority_revision=3,
            effective_at=NOW - timedelta(hours=1),
            valid_until=NOW + timedelta(hours=1),
        )


class _Entitlements:
    def resolve_current(self, **_identity: str) -> CurrentEntitlementSnapshot:
        semantic = {
            "schema_version": "1",
            "tenant_id": "tenant-a",
            "principal_ref": PRINCIPAL,
            "purpose_digest": digest(PURPOSE),
            "connected_authority_ref": "authority:entitlements",
            "source_revision": 2,
            "source_payload_digest": "c" * 64,
            "product_version_refs": (PRODUCT,),
            "semantic_refs": (SEMANTIC,),
            "filter_domains": (),
            "permissions": ("query", "view"),
            "effective_at": NOW - timedelta(hours=1),
            "valid_until": NOW + timedelta(hours=1),
        }
        return CurrentEntitlementSnapshot.model_validate(
            {
                **semantic,
                "snapshot_id": "entitlement-1",
                "snapshot_digest": digest(semantic),
                "observation_id": "observation-1",
                "resolved_at": NOW,
            }
        )


class _Policies:
    def resolve_current(self, **_identity: str) -> AnswerScopePolicy:
        return AnswerScopePolicy(
            policy_id="policy-1",
            tenant_id="tenant-a",
            revision=1,
            principal_scope=(PRINCIPAL,),
            purposes=(PURPOSE,),
            semantic_version_ref=SEMANTIC,
            data_product_version_refs=(PRODUCT,),
            metric_version_refs=(),
            dimension_refs=(),
            filter_domains=(),
            max_time_window=31,
            max_staleness=3600,
            quality_disposition="block",
            disclosure_classifications=(),
            disclosure_entity="customer",
            minimum_group_size=2,
            row_ceiling=100,
            byte_ceiling=100_000,
            scan_ceiling=1_000_000,
            period_scan_budget=10_000_000,
            statement_timeout=30,
            result_retention=3600,
            agent_access="allowed",
            model_disclosure="results",
            valid_from=NOW - timedelta(hours=1),
            valid_until=NOW + timedelta(hours=1),
            approval_ids=("approval-1",),
            created_at=NOW - timedelta(hours=1),
        )


class _UnusedReads:
    def search_catalog(self, _query: SearchCatalogQuery) -> Never:
        raise AssertionError("catalog read was not expected")

    def describe_metric(self, _query: DescribeMetricQuery) -> Never:
        raise AssertionError("metric read was not expected")

    def get_impact(self, _query: GetImpactQuery) -> Never:
        raise AssertionError("impact read was not expected")


def test_mcp_question_creates_one_real_request_with_dual_provenance_and_no_approval() -> None:
    repository = SQLiteRequestRepository(sqlite3.connect(":memory:", check_same_thread=False))
    requests = RequestManagementService(repository, clock=lambda: NOW)
    request_adapter = RequestManagementAgentAdapter(requests)
    unused_reads = _UnusedReads()
    application = build_application(
        AppSettings.model_validate(
            {
                "tenant_id": "tenant-a",
                "delegation_id": "delegation-1",
                "principal_ref": PRINCIPAL,
                "agent_client_ref": "agent-client:assistant-a",
                "purpose": PURPOSE,
            }
        ),
        AgentInterfacePorts(
            delegations=_Delegations(),
            entitlements=_Entitlements(),
            policies=_Policies(),
            requests=request_adapter,
            catalog=unused_reads,
            impacts=unused_reads,
        ),
        clock=lambda: NOW,
    )
    server = build_server(application)

    asyncio.run(
        server.call_tool(
            "ask_question",
            {"title": "Revenue by region", "question": "What is revenue by region?"},
        )
    )

    stored = requests.list_inbox("tenant-a")
    assert len(stored) == 1
    assert stored[0].requester_id == PRINCIPAL
    assert stored[0].delegated_agent is not None
    assert stored[0].delegated_agent.agent_client_ref == "agent-client:assistant-a"
    assert repository.list_decisions("tenant-a", stored[0].request_id) == ()
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool("approve_request", {"request_id": stored[0].request_id}))
