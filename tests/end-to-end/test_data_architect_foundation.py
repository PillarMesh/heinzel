from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_contract_service import (
    BusinessProcessManifest,
    ProcessPackageReceipt,
    ProcessPackageService,
)
from pillarmesh_contract_service.process_service import SQLiteProcessPackageRepository
from pillarmesh_request_management import RequestManagementService
from pillarmesh_request_management.repository import SQLiteRequestRepository
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehouseRestoreVerification,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def warehouse_repository() -> SQLiteWarehouseRepository:
    return SQLiteWarehouseRepository(":memory:")


@pytest.fixture
def warehouse_service(warehouse_repository: SQLiteWarehouseRepository) -> WarehouseControlService:
    return WarehouseControlService(warehouse_repository, clock=lambda: NOW)


@pytest.fixture
def process_service() -> ProcessPackageService:
    return ProcessPackageService(SQLiteProcessPackageRepository(":memory:"), clock=lambda: NOW)


MARKDOWN = "text/markdown; charset=utf-8"


@pytest.fixture
def request_service() -> RequestManagementService:
    return RequestManagementService(SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW)


def create_ready_clickhouse_binding(
    service: WarehouseControlService,
    repository: SQLiteWarehouseRepository,
    tenant_id: str,
) -> WarehouseBinding:
    binding = service.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    binding = service.transition(
        tenant_id,
        binding.binding_id,
        WarehouseBindingState.PROVISIONING,
        expected_revision=binding.revision,
    )
    operation = PrivateWarehouseOperation(
        tenant_id=tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        operation_id="wop-foundation-provision",
        operation_kind=WarehouseOperationKind.PROVISION,
        engine_kind=binding.engine_kind,
        status=WarehouseOperationStatus.CLAIMED,
        phase=WarehouseOperationPhase.CLAIMED,
        started_at=NOW,
        updated_at=NOW,
    )
    assert repository.claim_operation(operation)
    running = operation.model_copy(update={"status": WarehouseOperationStatus.RUNNING})
    repository.save_operation(operation, running)
    provider_created = running.model_copy(
        update={
            "phase": WarehouseOperationPhase.PROVIDER_CREATED,
            "provider_resource_handle": "clickhouse-foundation",
        }
    )
    repository.save_operation(running, provider_created)
    binding = service.transition(
        tenant_id,
        binding.binding_id,
        WarehouseBindingState.VALIDATING,
        expected_revision=binding.revision,
    )
    validating = provider_created.model_copy(update={"phase": WarehouseOperationPhase.VALIDATING})
    repository.save_operation(provider_created, validating)
    restore = WarehouseRestoreVerification(
        verification_id="wrv-foundation",
        tenant_id=tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        engine_kind=binding.engine_kind,
        source_backup_artifact_digest="a" * 64,
        representative_data_digest="b" * 64,
        schema_metadata_digest="c" * 64,
        principal_profile_digest="d" * 64,
        integrity_marker_digest="e" * 64,
        query_behavior_digest="f" * 64,
        verified_at=NOW,
    )
    evidence = WarehouseValidationEvidence(
        evidence_id="wev-foundation",
        tenant_id=tenant_id,
        binding_id=binding.binding_id,
        binding_revision=binding.revision,
        validation_profile=WarehouseValidationProfile.PRODUCTION,
        engine_kind=binding.engine_kind,
        engine_version="25.8",
        engine_build_digest="0" * 64,
        engine_image_digest="1" * 64,
        principal_profile_digest=restore.principal_profile_digest,
        namespace_grant_matrix_digest="3" * 64,
        tls_probe_digest="4" * 64,
        network_isolation_probe_digest="5" * 64,
        encryption_at_rest_evidence_digest="6" * 64,
        encryption_at_rest_disposition=EncryptionAtRestDisposition.PROVEN,
        positive_probe_digest="7" * 64,
        denial_probe_digest="8" * 64,
        ledger_probe_digest="9" * 64,
        monitoring_probe_digest="a" * 64,
        capacity_alert_probe_digest="b" * 64,
        backup_artifact_digest=restore.source_backup_artifact_digest,
        restore_verification_digest=digest(restore),
        restore_cleanup_digest="c" * 64,
        observed_at=NOW,
    )
    return service.record_validation(
        tenant_id,
        binding.binding_id,
        evidence,
        restore,
        operation=validating,
        expected_revision=binding.revision,
    )


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
    warehouse_repository: SQLiteWarehouseRepository,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(
        warehouse_service, warehouse_repository, tenant_id="tenant-a"
    )
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
    warehouse_repository: SQLiteWarehouseRepository,
    process_service: ProcessPackageService,
    request_service: RequestManagementService,
) -> None:
    binding = create_ready_clickhouse_binding(
        warehouse_service, warehouse_repository, tenant_id="tenant-a"
    )
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
    with pytest.raises(KeyError, match="warehouse binding was not found"):
        warehouse_service.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        process_service.get_original("tenant-b", package.package_id, package.version)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        request_service.get("tenant-b", access.request_id)
    with pytest.raises(KeyError, match="belongs to another tenant"):
        request_service.get("tenant-b", question.request_id)
