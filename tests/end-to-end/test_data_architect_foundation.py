from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_service import (
    BusinessProcessManifest,
    ProcessPackageReceipt,
    ProcessPackageService,
)
from pillarmesh_contract_service.process_service import SQLiteProcessPackageRepository
from pillarmesh_request_management import RequestManagementService
from pillarmesh_request_management.repository import SQLiteRequestRepository
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def warehouse_service() -> WarehouseControlService:
    return WarehouseControlService(SQLiteWarehouseRepository(":memory:"), clock=lambda: NOW)


@pytest.fixture
def process_service() -> ProcessPackageService:
    return ProcessPackageService(SQLiteProcessPackageRepository(":memory:"), clock=lambda: NOW)


MARKDOWN = "text/markdown; charset=utf-8"


@pytest.fixture
def request_service() -> RequestManagementService:
    return RequestManagementService(SQLiteRequestRepository(":memory:"), clock=lambda: NOW)


def create_ready_clickhouse_binding(
    service: WarehouseControlService, tenant_id: str
) -> WarehouseBinding:
    binding = service.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    for state in (
        WarehouseBindingState.PROVISIONING,
        WarehouseBindingState.VALIDATING,
        WarehouseBindingState.READY,
    ):
        binding = service.transition(
            tenant_id, binding.binding_id, state, expected_revision=binding.revision
        )
    return binding


def upload_revenue_process(service: ProcessPackageService, tenant_id: str) -> ProcessPackageReceipt:
    manifest = BusinessProcessManifest(
        process_name="revenue-to-cash",
        owner="finance-data-owner",
        participants=("customer", "finance"),
        outcomes=("recognized-revenue",),
        entities=("Customer", "Invoice", "Payment", "Refund"),
        events=("invoice-issued", "payment-settled", "refund-issued"),
        states=("invoice-open", "invoice-paid", "invoice-refunded"),
        rules=("refund does not exceed settled payment",),
        source_references=("postgresql.billing", "stripe"),
        unresolved_questions=("refund exception owner",),
    )
    return service.upload(tenant_id, b"# Revenue to cash\n", MARKDOWN, manifest, "architect-a")


def test_architect_establishes_control_plane_and_receives_work(
    warehouse_service: WarehouseControlService,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(warehouse_service, tenant_id="tenant-a")
    package = upload_revenue_process(process_service, tenant_id="tenant-a")
    question = request_service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )
    access = request_service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )

    assert binding.lifecycle_state is WarehouseBindingState.READY
    assert package.tenant_id == question.tenant_id == access.tenant_id == "tenant-a"
    assert [item.payload.request_type for item in request_service.list_inbox("tenant-a")] == [
        "stakeholder_question",
        "data_access",
    ]


def test_a_second_tenant_sees_none_of_the_first_tenants_control_plane(
    warehouse_service: WarehouseControlService,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(warehouse_service, tenant_id="tenant-a")
    package = upload_revenue_process(process_service, tenant_id="tenant-a")
    question = request_service.submit_question(
        tenant_id="tenant-a",
        requester_id="finance-user",
        purpose="monthly close",
        question="What were net refunds yesterday?",
    )
    access = request_service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="analyst-a",
        purpose="refund investigation",
        data_product_id="finance-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )

    # Holding a valid identifier is not authority. Every boundary refuses on tenant.
    assert request_service.list_inbox("tenant-b") == ()
    with pytest.raises(KeyError, match="belongs to another tenant"):
        warehouse_service.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        process_service.get_original("tenant-b", package.package_id, package.version)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        request_service.get("tenant-b", access.request_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        request_service.get("tenant-b", question.request_id)
