"""Governed-local console read projections over disposable owning-service repositories.

Every read here goes through a real service or repository interface. Nothing in this
module may fall back to `FixtureConsoleBackend`; the assertions below prove that a
missing downstream implementation and a failing repository stay distinguishable from
demo content.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_catalog_control import CatalogControlService, SQLiteCatalogRepository
from heinzel_console.auth import TrustedActorContext
from heinzel_console.errors import ConsoleUnavailable
from heinzel_console.governed_adapters import (
    CatalogControlBindingReader,
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceBindingDirectory,
    WarehouseControlBindingReader,
    WarehouseOperationIdentity,
    WarehouseRepositoryOperationReader,
)
from heinzel_console.governed_backend import CAPABILITY_NOT_DELIVERED, GovernedConsoleBackend
from heinzel_console.operation_handles import (
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    mint_console_handle,
)
from heinzel_request_management import (
    FulfillmentReadService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
)
from heinzel_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseControlService,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository

_TENANT = "tenant-governed"
_ARCHITECT = "actor-architect"
_REQUESTER = "actor-requester"
_PROVIDER_HANDLE_CANARY = "canary-provider-resource-handle-must-not-leak"
_FIXTURE_MARKERS = ("Revenue to cash", "tenant-demo", "Northwind Demo", "Fixture")


def _clock() -> datetime:
    return datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class _GovernedStack:
    def __init__(self, directory: Path) -> None:
        self.warehouse_repository = SQLiteWarehouseRepository(str(directory / "warehouse.sqlite3"))
        self.catalog_repository = SQLiteCatalogRepository(str(directory / "catalog.sqlite3"))
        self.request_repository = SQLiteRequestRepository.open(str(directory / "requests.sqlite3"))
        self.fulfillment_repository = SQLiteFulfillmentRepository(self.request_repository)
        self.warehouse = WarehouseControlService(self.warehouse_repository, clock=_clock)
        self.catalog = CatalogControlService(self.catalog_repository, clock=_clock)
        self.requests = RequestManagementService(self.request_repository, clock=_clock)
        self.fulfillment = FulfillmentReadService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            authority_role_resolver=None,
        )
        self.directory = InMemoryWorkspaceBindingDirectory()
        self.handles = InMemoryOperationHandleRepository()

    def close(self) -> None:
        self.warehouse_repository.close()
        self.catalog_repository.close()
        self.request_repository.close()

    def backend(self) -> GovernedConsoleBackend:
        return GovernedConsoleBackend(
            identity=GovernedWorkspaceIdentity(
                tenant_ref="tenant-governed",
                tenant_display_name="Governed tenant",
                workspace_ref="workspace-governed",
                workspace_display_name="Governed workspace",
            ),
            operation_handles=self.handles,
            warehouse_bindings=WarehouseControlBindingReader(
                service=self.warehouse, directory=self.directory
            ),
            warehouse_operations=WarehouseRepositoryOperationReader(self.warehouse_repository),
            catalog_bindings=CatalogControlBindingReader(
                service=self.catalog, directory=self.directory
            ),
            requests=self.requests,
            fulfillment=self.fulfillment,
        )


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[_GovernedStack]:
    governed = _GovernedStack(tmp_path)
    try:
        yield governed
    finally:
        governed.close()


def _architect_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_ARCHITECT,
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-architect",
    )


def _requester_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )


def test_setup_projection_reads_a_new_governed_warehouse_binding(stack: _GovernedStack) -> None:
    binding = stack.warehouse.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    stack.directory.bind_warehouse(tenant_id=_TENANT, binding_id=binding.binding_id)

    setup = stack.backend().get_setup(_architect_context())

    assert setup.warehouse_binding is not None
    assert setup.warehouse_binding.binding_ref == binding.binding_id
    assert setup.warehouse_binding.engine == "clickhouse"
    assert setup.warehouse_binding.state == "blocked"


def test_setup_projection_carries_no_fixture_content(stack: _GovernedStack) -> None:
    binding = stack.warehouse.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    stack.directory.bind_warehouse(tenant_id=_TENANT, binding_id=binding.binding_id)

    serialized = stack.backend().get_setup(_architect_context()).model_dump_json()

    assert all(marker not in serialized for marker in _FIXTURE_MARKERS)


def test_inbox_projection_reads_a_new_governed_request(stack: _GovernedStack) -> None:
    request = stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Quarterly board reporting",
        question="What was net revenue last quarter?",
    )

    inbox = stack.backend().get_inbox(_architect_context())

    assert tuple(item.request_id for item in inbox.items) == (request.request_id,)
    assert inbox.items[0].state == "submitted"
    assert inbox.items[0].purpose == "Quarterly board reporting"


def test_requester_projection_reads_only_the_requesters_own_requests(
    stack: _GovernedStack,
) -> None:
    mine = stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Quarterly board reporting",
        question="What was net revenue last quarter?",
    )
    stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id="actor-other-requester",
        purpose="Unrelated purpose",
        question="Unrelated question?",
    )

    requests = stack.backend().get_requester_requests(_requester_context())

    assert tuple(view.request_id for view in requests) == (mine.request_id,)


def test_another_tenants_request_never_appears_in_the_inbox(stack: _GovernedStack) -> None:
    stack.requests.submit_question(
        tenant_id="tenant-other",
        requester_id=_REQUESTER,
        purpose="Other tenant purpose",
        question="Other tenant question?",
    )

    inbox = stack.backend().get_inbox(_architect_context())

    assert inbox.items == ()


def test_operation_projection_hides_the_private_warehouse_operation(
    stack: _GovernedStack,
) -> None:
    binding = stack.warehouse.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    stack.directory.bind_warehouse(tenant_id=_TENANT, binding_id=binding.binding_id)
    claimed = PrivateWarehouseOperation(
        tenant_id=_TENANT,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id="whop-private-operation-identity",
        operation_kind=WarehouseOperationKind.PROVISION,
        engine_kind=EngineKind.POSTGRESQL,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=_clock(),
        updated_at=_clock(),
    )
    stack.warehouse_repository.claim_operation(claimed)
    created = claimed.model_copy(
        update={
            "status": WarehouseOperationStatus.RUNNING,
            "phase": WarehouseOperationPhase.PROVIDER_CREATED,
            "provider_resource_handle": _PROVIDER_HANDLE_CANARY,
        }
    )
    stack.warehouse_repository.save_operation(claimed, created)
    console_handle = mint_console_handle()
    stack.handles.store(
        OperationHandleRecord(
            tenant_id=_TENANT,
            console_handle=console_handle,
            capability_kind="warehouse_lifecycle",
            private_identity=WarehouseOperationIdentity(
                binding_id=binding.binding_id,
                operation_id=claimed.operation_id,
            ).encode(),
        )
    )

    view = stack.backend().get_operation(_architect_context(), console_handle)
    serialized = view.model_dump_json()

    assert view.operation_id == console_handle
    assert view.state == "running"
    assert view.phase == "provider_created"
    assert _PROVIDER_HANDLE_CANARY not in serialized
    assert claimed.operation_id not in serialized
    assert binding.binding_id not in serialized


@pytest.mark.parametrize(
    ("capability", "read"),
    [
        ("request-conversation", lambda backend, context: backend.get_conversation(context, "r-1")),
        (
            "request-conversation",
            lambda backend, context: backend.get_request_detail(context, "r-1"),
        ),
        ("data-product-runs", lambda backend, context: backend.get_runs(context)),
        (
            "data-product-runs",
            lambda backend, context: backend.get_data_product(context, "product-revenue"),
        ),
        (
            "analyst-dashboard",
            lambda backend, context: backend.get_dashboard(context, "dashboard-revenue"),
        ),
        (
            "catalog-asset-preview",
            lambda backend, context: backend.get_catalog_asset(context, "asset-revenue"),
        ),
    ],
)
def test_undelivered_capabilities_fail_closed_rather_than_serving_fixtures(
    stack: _GovernedStack,
    capability: str,
    read: object,
) -> None:
    backend = stack.backend()

    with pytest.raises(ConsoleUnavailable) as failure:
        read(backend, _architect_context())  # type: ignore[operator]
    states = {
        item.capability_id: item.state
        for item in backend.get_workspace(_architect_context()).capabilities
    }

    assert failure.value.code == CAPABILITY_NOT_DELIVERED
    assert states[capability] == "not_delivered"


def test_workspace_manifest_distinguishes_wired_from_undelivered_capabilities(
    stack: _GovernedStack,
) -> None:
    binding = stack.warehouse.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    stack.directory.bind_warehouse(tenant_id=_TENANT, binding_id=binding.binding_id)

    workspace = stack.backend().get_workspace(_architect_context())
    states = {item.capability_id: item.state for item in workspace.capabilities}

    assert states["warehouse-binding"] == "blocked"
    assert states["request-fulfillment"] == "ready"
    assert states["request-conversation"] == "not_delivered"
    assert states["source-acquisition"] == "not_delivered"


def test_a_closed_repository_reports_unavailable_instead_of_demo_state(
    stack: _GovernedStack,
) -> None:
    binding = stack.warehouse.create_draft(
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
    )
    stack.directory.bind_warehouse(tenant_id=_TENANT, binding_id=binding.binding_id)
    backend = stack.backend()
    stack.warehouse_repository.close()

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_setup(_architect_context())

    assert failure.value.code == "downstream_unavailable"
    assert failure.value.recovery_action == "retry"


def test_no_governed_projection_repeats_committed_fixture_content(stack: _GovernedStack) -> None:
    stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Quarterly board reporting",
        question="What was net revenue last quarter?",
    )
    backend = stack.backend()

    serialized = json.dumps(
        [
            backend.get_workspace(_architect_context()).model_dump(mode="json"),
            backend.get_inbox(_architect_context()).model_dump(mode="json"),
            backend.get_session(_architect_context(), "t" * 40).model_dump(mode="json"),
        ]
    )

    assert all(marker not in serialized for marker in _FIXTURE_MARKERS)
