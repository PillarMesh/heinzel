from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never

import pytest
from heinzel_access_control import (
    AccessGrant,
    AccessGrantDenied,
    AccessGrantIntegrityError,
    AccessGrantState,
)
from heinzel_bi_control import (
    DashboardPublication,
    DashboardPublicationAttempt,
    DashboardPublicationIntent,
    DashboardPublicationRecord,
)
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_connection_broker import (
    SourceBindingPersistenceError,
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from heinzel_console import fixture_backend, fixture_data
from heinzel_console.auth import TrustedActorContext
from heinzel_console.contracts import (
    AccessRevocationCommand,
    ActorRole,
    DashboardPublicationCommand,
    ResetCommand,
    SetupStageView,
    SetupView,
    WorkspaceView,
)
from heinzel_console.errors import ConsoleConflict, ConsoleNotFound, ConsoleUnavailable
from heinzel_console.governed_adapters import (
    AccessGrantCommands,
    AccessGrantReader,
    AccessGrantRevocationCommands,
    CatalogBindingReader,
    CatalogSearchHealthReader,
    DashboardPublicationCommands,
    DashboardPublicationReader,
    DataProductReferenceReader,
    EnrolledSourceConnection,
    EnrolledSourceConnectionReader,
    FulfillmentViewReader,
    GovernedWorkspaceIdentity,
    InMemoryWorkspacePrincipalDirectory,
    PolicyPermittedDataProductReader,
    ProductPublicationDefinitionReader,
    RequestImpactReader,
    RequestInboxReader,
    SelectableAnswerTermReader,
    SourceBindingReader,
    SourceRegistrationCommands,
    TenantAcquisitionReceiptReader,
    TenantRunLifecycleReader,
    TenantRunReader,
    WarehouseBindingReader,
    WarehouseOperationIdentity,
    WarehouseOperationReader,
)
from heinzel_console.governed_backend import (
    CAPABILITY_NOT_DELIVERED,
    GovernedConsoleBackend,
)
from heinzel_console.operation_handles import (
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    OperationHandleRepository,
    mint_console_handle,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_evidence import (
    AcquisitionEvidenceOutcome,
    AcquisitionEvidenceReceipt,
    AcquisitionPublicReasonCode,
    RunRecord,
    RunState,
)
from heinzel_provider_sdk import (
    CatalogColumn,
    CatalogLineageSource,
    CatalogProductDefinition,
    catalog_product_external_key,
)
from heinzel_request_management import (
    ApprovalRequirement,
    ArchitectRequestView,
    BoundSemanticReference,
    ClarifiedOutcomeStatement,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    InboxRequest,
    RequesterAccessDeliveryView,
    RequesterRequestView,
    RequestState,
    StakeholderAnswerDraft,
)
from heinzel_request_management.requester_view import OwnDecisionView
from heinzel_state import (
    RunIntent,
    RunLifecycleSnapshot,
    RunService,
    SQLiteRunRepository,
    TriggerWindow,
)
from heinzel_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehouseOperationKind,
    WarehouseOperationPhase,
    WarehouseOperationStatus,
    WarehousePersistenceError,
)

_TENANT = "tenant-alpha"
_ACTOR = "actor-architect"
_PROVIDER_HANDLE_CANARY = "canary-provider-resource-handle-must-not-leak"
_PROPOSAL_TEXT_CANARY = "canary-unapproved-proposal-text-must-not-reach-a-requester"
_FIXED_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

_IDENTITY = GovernedWorkspaceIdentity(
    tenant_ref="tenant-alpha",
    tenant_display_name="Alpha",
    workspace_ref="workspace-alpha",
    workspace_display_name="Alpha workspace",
)


def _architect_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_ACTOR,
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-architect",
    )


def _requester_context() -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id="actor-requester",
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )


def _binding(state: WarehouseBindingState = WarehouseBindingState.READY) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="whb-0123456789abcdef01234567",
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
        capability_profile_digest="a" * 64,
        lifecycle_state=state,
        revision=2,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _CatalogReader:
    def current_binding(self, tenant_id: str) -> CatalogBinding | None:
        if tenant_id != _TENANT:
            return None
        return CatalogBinding(
            binding_id="cat-0123456789abcdef01234567",
            tenant_id=_TENANT,
            capability_profile_digest="a" * 64,
            lifecycle_state=CatalogBindingState.READY,
            revision=2,
            created_at=_FIXED_TIME,
            updated_at=_FIXED_TIME,
            provisioned_at=_FIXED_TIME,
        )


class _SearchHealth:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    def search_ready(self, tenant_id: str) -> bool:
        return self._ready and tenant_id == _TENANT


def _private_operation(
    *,
    status: WarehouseOperationStatus = WarehouseOperationStatus.RUNNING,
    classification: WarehouseFailureClassification | None = None,
) -> PrivateWarehouseOperation:
    return PrivateWarehouseOperation(
        tenant_id=_TENANT,
        binding_id="whb-0123456789abcdef01234567",
        binding_revision=2,
        operation_id="whop-private-operation-identity",
        operation_kind=WarehouseOperationKind.PROVISION,
        engine_kind=EngineKind.POSTGRESQL,
        status=status,
        phase=WarehouseOperationPhase.PROVIDER_CREATED,
        provider_resource_handle=_PROVIDER_HANDLE_CANARY,
        failure_classification=classification,
        started_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _StaticWarehouseBindingReader:
    def __init__(self, binding: WarehouseBinding | None) -> None:
        self._binding = binding

    def current_binding(self, tenant_id: str) -> WarehouseBinding | None:
        return self._binding if tenant_id == _TENANT else None


class _FailingWarehouseBindingReader:
    def current_binding(self, tenant_id: str) -> Never:
        raise WarehousePersistenceError("load warehouse binding")


class _StaticWarehouseOperationReader:
    def __init__(self, operation: PrivateWarehouseOperation | None) -> None:
        self._operation = operation

    def load_operation(
        self, *, tenant_id: str, private_identity: str
    ) -> PrivateWarehouseOperation | None:
        return self._operation


def _inbox_request(request_id: str, requester_id: str) -> InboxRequest:
    return InboxRequest.model_validate(
        {
            "request_id": request_id,
            "tenant_id": _TENANT,
            "requester_id": requester_id,
            "payload": {
                "request_type": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            },
            "state": RequestState.PROPOSED,
            "revision": 3,
            "submitted_at": _FIXED_TIME,
            "updated_at": _FIXED_TIME,
        }
    )


def _data_access_request(request_id: str, requester_id: str) -> InboxRequest:
    return InboxRequest.model_validate(
        {
            "request_id": request_id,
            "tenant_id": _TENANT,
            "requester_id": requester_id,
            "payload": {
                "request_type": "data_access",
                "purpose": "Prepare a quarterly finance dashboard",
                "data_product_id": "data-product-finance",
                "requested_fields": ["net_revenue"],
                "access_mode": "dashboard",
                "expires_at": "2026-10-01T00:00:00Z",
            },
            "state": RequestState.PROPOSED,
            "revision": 3,
            "submitted_at": _FIXED_TIME,
            "updated_at": _FIXED_TIME,
        }
    )


def _access_grant(state: AccessGrantState, *, tenant_id: str = _TENANT) -> AccessGrant:
    values: dict[str, object] = {
        "grant_id": "grant-private-ref",
        "tenant_id": tenant_id,
        "request_id": "req-00000000000000000002",
        "revision": 2,
        "state": state,
        "principal_ref": "principal:requester",
        "purpose": "Analyze regional revenue",
        "purpose_digest": digest("Analyze regional revenue"),
        "data_product_version_ref": ArtifactReference(
            artifact_id="product-revenue", version=1, digest="a" * 64
        ),
        "fields": ("net-revenue", "region"),
        "classification_refs": (),
        "access_mode": "query",
        "permissions": ("query", "view"),
        "effective_at": _FIXED_TIME,
        "expires_at": _FIXED_TIME + timedelta(days=1),
        "policy_revision": 1,
        "admission_receipt_ref": ArtifactReference(
            artifact_id="admission-access", version=1, digest="b" * 64
        ),
        "entitlement_snapshot_digest": "c" * 64,
        "created_at": _FIXED_TIME,
        "updated_at": _FIXED_TIME,
    }
    if state == "failed":
        values["failed_action"] = "revoke"
    return AccessGrant.model_validate(values)


class _StaticAccessGrantReader:
    def __init__(self, grant: AccessGrant | None) -> None:
        self._grant = grant
        self.calls: list[tuple[str, str]] = []

    def load_current_for_request(self, tenant_id: str, request_id: str) -> AccessGrant | None:
        self.calls.append((tenant_id, request_id))
        if self._grant is None or self._grant.tenant_id != tenant_id:
            return None
        return self._grant if self._grant.request_id == request_id else None


class _FailingAccessGrantReader:
    def load_current_for_request(self, tenant_id: str, request_id: str) -> Never:
        raise AccessGrantIntegrityError("stored grant is invalid")


class _MisdirectedAccessGrantReader:
    def __init__(self, grant: AccessGrant) -> None:
        self._grant = grant

    def load_current_for_request(self, tenant_id: str, request_id: str) -> AccessGrant:
        return self._grant


class _AccessRevocations:
    def __init__(self, result: AccessGrant, *, stale: bool = False) -> None:
        self.result = result
        self.stale = stale
        self.calls: list[dict[str, object]] = []

    def revoke_for_request(self, **values: object) -> AccessGrant:
        self.calls.append(values)
        if self.stale:
            from heinzel_access_control import AccessGrantStaleRevision

            raise AccessGrantStaleRevision("access grant revision is stale")
        return self.result


def _clarified_outcome(request_id: str) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="out-00000000000000000001-0123456789abcdef01234567",
        tenant_id=_TENANT,
        request_id=request_id,
        request_revision=2,
        restated_request="Report net revenue for the last closed quarter.",
        purpose_digest="b" * 64,
        in_scope_summary="Closed quarter revenue.",
        out_of_scope_summary="Forecasts.",
        created_at=_FIXED_TIME,
    )


def _proposal_with_canary(request_id: str) -> FulfillmentProposal:
    subject = StakeholderAnswerDraft(
        answer_text=_PROPOSAL_TEXT_CANARY,
        governed_dataset_refs=(),
        metric_refs=(),
        as_of=_FIXED_TIME,
        freshness_disposition="unknown",
        material_quality_limitations=(),
        lineage_refs=(),
        disclosure_classifications=(),
    )
    return FulfillmentProposal(
        proposal_id="prp-00000000000000000001-0123456789abcdef01234567",
        tenant_id=_TENANT,
        request_id=request_id,
        request_revision=3,
        revision=1,
        clarified_outcome_digest=digest(_clarified_outcome(request_id)),
        grounding_snapshot_digest="c" * 64,
        policy_snapshot_digest="d" * 64,
        subject=subject,
        required_approvals=(
            ApprovalRequirement(
                authority_ref="principal:requester",
                reason_code="clarified_outcome_acceptance",
                subject_digest=digest(_clarified_outcome(request_id)),
            ),
        ),
        created_at=_FIXED_TIME,
    )


class _StaticRequestReader:
    def __init__(self, requests: tuple[InboxRequest, ...]) -> None:
        self._requests = requests

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]:
        return self._requests if tenant_id == _TENANT else ()

    def get(self, tenant_id: str, request_id: str) -> InboxRequest:
        for request in self._requests:
            if request.tenant_id == tenant_id and request.request_id == request_id:
                return request
        raise KeyError("request was not found")


class _RequesterOnlyFulfillmentReader:
    """Stands in for `FulfillmentReadService` and fails if the console reaches for the
    architect projection while composing a requester view."""

    def __init__(
        self,
        request: InboxRequest,
        *,
        delivered_access: RequesterAccessDeliveryView | None = None,
        no_valid_plan_explanation: str | None = None,
    ) -> None:
        self._request = request
        self._delivered_access = delivered_access
        self._no_valid_plan_explanation = no_valid_plan_explanation

    def requester_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> RequesterRequestView:
        return RequesterRequestView(
            request_id=request_id,
            state=self._request.state,
            revision=self._request.revision,
            clarified_outcomes=(_clarified_outcome(request_id),),
            own_decisions=(
                OwnDecisionView(
                    lifecycle="fulfillment",
                    decision="approve",
                    authority_ref="principal:requester",
                    created_at=_FIXED_TIME,
                ),
            ),
            fulfillment_status="in_review",
            denial_explanation=None,
            no_valid_plan_explanation=self._no_valid_plan_explanation,
            delivered_access=self._delivered_access,
        )

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView:
        raise AssertionError("a requester projection must never read the architect projection")

    def reviewer_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> Never:
        raise AssertionError("a requester projection must never read the reviewer projection")


def _backend(
    *,
    operation_handles: OperationHandleRepository | None = None,
    warehouse_bindings: WarehouseBindingReader | None = None,
    catalog_bindings: CatalogBindingReader | None = None,
    catalog_search_health: CatalogSearchHealthReader | None = None,
    warehouse_operations: WarehouseOperationReader | None = None,
    requests: RequestInboxReader | None = None,
    fulfillment: FulfillmentViewReader | None = None,
    runs: TenantRunReader | None = None,
    run_lifecycle: TenantRunLifecycleReader | None = None,
    acquisition_receipts: TenantAcquisitionReceiptReader | None = None,
    source_bindings: SourceBindingReader | None = None,
    enrolled_source_connections: EnrolledSourceConnectionReader | None = None,
    source_registration_commands: SourceRegistrationCommands | None = None,
    selectable_answer_terms: SelectableAnswerTermReader | None = None,
    data_products: DataProductReferenceReader | None = None,
    product_publications: ProductPublicationDefinitionReader | None = None,
    impact_reader: RequestImpactReader | None = None,
    access_grants: AccessGrantReader | None = None,
    access_grant_commands: AccessGrantCommands | None = None,
    access_revocation_commands: AccessGrantRevocationCommands | None = None,
    dashboards: DashboardPublicationReader | None = None,
    dashboard_publication_commands: DashboardPublicationCommands | None = None,
    principals: InMemoryWorkspacePrincipalDirectory | None = None,
) -> GovernedConsoleBackend:
    """Inject doubles under the backend's own parameter types.

    This took `**overrides: object` and silenced the constructor with
    `type: ignore[arg-type]`, which suppressed exactly the protocol conformance
    check that would have caught a reader whose signature the owning service does
    not accept -- the defect this directory is checked for in the first place.
    """
    return GovernedConsoleBackend(
        identity=_IDENTITY,
        operation_handles=operation_handles or InMemoryOperationHandleRepository(),
        warehouse_bindings=warehouse_bindings,
        catalog_bindings=catalog_bindings,
        catalog_search_health=catalog_search_health,
        warehouse_operations=warehouse_operations,
        requests=requests,
        fulfillment=fulfillment,
        runs=runs,
        run_lifecycle=run_lifecycle,
        acquisition_receipts=acquisition_receipts,
        source_bindings=source_bindings,
        enrolled_source_connections=enrolled_source_connections,
        source_registration_commands=source_registration_commands,
        selectable_answer_terms=selectable_answer_terms,
        data_products=data_products,
        product_publications=product_publications,
        impact_reader=impact_reader,
        access_grants=access_grants,
        access_grant_commands=access_grant_commands,
        access_revocation_commands=access_revocation_commands,
        dashboards=dashboards,
        dashboard_publication_commands=dashboard_publication_commands,
        principals=principals,
    )


class _DashboardReader:
    def __init__(self, publications: tuple[DashboardPublication, ...]) -> None:
        self._publications = publications
        self.requested_tenants: list[str] = []

    def list_publications(self, tenant_id: str) -> tuple[DashboardPublication, ...]:
        self.requested_tenants.append(tenant_id)
        return self._publications if tenant_id == _TENANT else ()


def _dashboard_publication(
    *,
    dashboard_id: str = "internal:revenue-dashboard",
    source_request_id: str = "req-00000000000000000002",
    title: str = "Current revenue overview",
    lifecycle_state: str = "active",
    data_product_version_ref: ArtifactReference | None = None,
) -> DashboardPublication:
    return DashboardPublication.model_validate(
        {
            "dashboard_id": dashboard_id,
            "version": 3,
            "title": title,
            "source_request_id": source_request_id,
            "data_product_version_ref": data_product_version_ref
            or _reference("product-revenue", version=1),
            "lifecycle_state": lifecycle_state,
            "as_of": _FIXED_TIME,
            "freshness_disposition": "current",
            "published_at": _FIXED_TIME,
        }
    )


class _DashboardAccessCommands:
    def __init__(self, authorized_grant: AccessGrant, *, denied: bool = False) -> None:
        self._authorized_grant = authorized_grant
        self._denied = denied
        self.calls: list[dict[str, object]] = []

    def apply(self, *, tenant_id: str, request_id: str, grant_id: str) -> AccessGrant:
        raise AssertionError("a dashboard read must not apply an access grant")

    def authorize(self, **values: object) -> AccessGrant:
        self.calls.append(values)
        if self._denied or values["grant_id"] != self._authorized_grant.grant_id:
            raise AccessGrantDenied("access grant is unavailable")
        return self._authorized_grant


def test_dashboard_listing_projects_only_public_owned_fields_behind_an_opaque_reference() -> None:
    reader = _DashboardReader((_dashboard_publication(),))
    backend = _backend(dashboards=reader)

    listing = backend.get_dashboards(_architect_context())

    assert reader.requested_tenants == [_TENANT]
    assert len(listing.dashboards) == 1
    dashboard = listing.dashboards[0]
    assert dashboard.display_name == "Current revenue overview"
    assert dashboard.state == "ready"
    assert dashboard.as_of == _FIXED_TIME
    assert dashboard.freshness == "current"
    assert dashboard.access_state == "workspace_role"
    assert dashboard.dashboard_ref.startswith("dashboard-")
    assert "revenue" not in dashboard.dashboard_ref
    assert dashboard.preview_ref is None
    assert dashboard.link_ref is None
    assert "Revision 3" in dashboard.summary
    assert "Superset" not in dashboard.model_dump_json()
    assert "internal:revenue-dashboard" not in dashboard.model_dump_json()

    assert backend.get_dashboard(_architect_context(), dashboard.dashboard_ref) == dashboard


def test_dashboard_reads_fail_closed_without_provider_receipt_authority() -> None:
    backend = _backend()

    with pytest.raises(ConsoleUnavailable) as listing_failure:
        backend.get_dashboards(_architect_context())
    with pytest.raises(ConsoleUnavailable) as detail_failure:
        backend.get_dashboard(_architect_context(), "dashboard-deadbeef")

    assert listing_failure.value.code == CAPABILITY_NOT_DELIVERED
    assert detail_failure.value.code == CAPABILITY_NOT_DELIVERED


def test_requester_cannot_list_dashboards_without_grant_bound_dashboard_authority() -> None:
    backend = _backend(dashboards=_DashboardReader((_dashboard_publication(),)))

    with pytest.raises(ConsoleNotFound):
        backend.get_dashboards(_requester_context())
    with pytest.raises(ConsoleNotFound):
        backend.get_dashboard(_requester_context(), "dashboard-untrusted")


def test_requester_lists_only_dashboards_authorized_by_current_access_control() -> None:
    authorized = _dashboard_publication(source_request_id="req-answer-revenue")
    hidden = _dashboard_publication(
        dashboard_id="internal:executive-dashboard",
        source_request_id="req-answer-executive",
        title="Executive margin",
        data_product_version_ref=_reference("product-executive", version=1),
    )
    grant = _access_grant("active").model_copy(
        update={
            "access_mode": "dashboard",
            "permissions": ("dashboard", "view"),
            "data_product_version_ref": authorized.data_product_version_ref,
        }
    )
    grants = _StaticAccessGrantReader(grant)
    commands = _DashboardAccessCommands(grant)
    principals = InMemoryWorkspacePrincipalDirectory()
    principals.bind_principal(
        tenant_id=_TENANT,
        actor_id="actor-requester",
        role="requester",
        principal_ref=grant.principal_ref,
    )
    backend = _backend(
        dashboards=_DashboardReader((hidden, authorized)),
        requests=_StaticRequestReader((_data_access_request(grant.request_id, "actor-requester"),)),
        access_grants=grants,
        access_grant_commands=commands,
        principals=principals,
    )

    listing = backend.get_dashboards(_requester_context())

    assert [dashboard.display_name for dashboard in listing.dashboards] == [
        "Current revenue overview"
    ]
    assert listing.dashboards[0].access_state == "active"
    assert grants.calls == [
        (_TENANT, grant.request_id),
    ]
    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "grant_id": grant.grant_id,
            "principal_ref": grant.principal_ref,
            "purpose": grant.purpose,
            "permission": "dashboard",
            "product_version_ref": authorized.data_product_version_ref,
        }
    ]


def test_requester_dashboard_listing_hides_a_currently_denied_grant() -> None:
    publication = _dashboard_publication(source_request_id="req-answer-revenue")
    grant = _access_grant("active").model_copy(
        update={
            "access_mode": "dashboard",
            "permissions": ("dashboard", "view"),
            "data_product_version_ref": publication.data_product_version_ref,
        }
    )
    principals = InMemoryWorkspacePrincipalDirectory()
    principals.bind_principal(
        tenant_id=_TENANT,
        actor_id="actor-requester",
        role="requester",
        principal_ref=grant.principal_ref,
    )
    backend = _backend(
        dashboards=_DashboardReader((publication,)),
        requests=_StaticRequestReader((_data_access_request(grant.request_id, "actor-requester"),)),
        access_grants=_StaticAccessGrantReader(grant),
        access_grant_commands=_DashboardAccessCommands(grant, denied=True),
        principals=principals,
    )

    assert backend.get_dashboards(_requester_context()).dashboards == ()


def test_dashboard_detail_rejects_the_owning_services_private_identifier() -> None:
    backend = _backend(dashboards=_DashboardReader((_dashboard_publication(),)))

    with pytest.raises(ConsoleNotFound):
        backend.get_dashboard(_architect_context(), "internal:revenue-dashboard")


def test_catalog_readiness_degrades_when_search_is_unhealthy() -> None:
    backend = _backend(
        catalog_bindings=_CatalogReader(),
        catalog_search_health=_SearchHealth(False),
    )

    catalog = next(
        capability
        for capability in backend.get_workspace(_architect_context()).capabilities
        if capability.capability_id == "catalog-binding"
    )

    assert catalog.state == "degraded"
    assert "search" in catalog.detail.lower()


def _forbid_fixture_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def never_called(*_: object, **__: object) -> Never:
        raise AssertionError("the governed backend must never read fixture data")

    monkeypatch.setattr(fixture_backend.FixtureConsoleBackend, "__init__", never_called)
    monkeypatch.setattr(fixture_data, "build_fixture_seed", never_called)


def test_governed_backend_reports_governed_local_provenance() -> None:
    assert _backend().fixture_mode is False


def test_governed_module_does_not_import_the_fixture_backend() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "heinzel_console"

    for module in ("governed_backend.py", "governed_adapters.py", "operation_handles.py"):
        text = (source / module).read_text(encoding="utf-8")

        assert "fixture_backend" not in text
        assert "fixture_data" not in text


@pytest.mark.parametrize(
    ("read", "context"),
    [
        pytest.param(
            lambda backend, context: backend.get_setup(context),
            _architect_context(),
            id="setup",
        ),
        pytest.param(
            lambda backend, context: backend.get_inbox(context),
            _architect_context(),
            id="inbox",
        ),
        pytest.param(
            lambda backend, context: backend.get_runs(context),
            _architect_context(),
            id="runs",
        ),
        pytest.param(
            lambda backend, context: backend.get_review(context, "review-000000000000000000000001"),
            _architect_context(),
            id="review",
        ),
        pytest.param(
            lambda backend, context: backend.get_conversation(context, "req-1"),
            _architect_context(),
            id="conversation",
        ),
        pytest.param(
            lambda backend, context: backend.get_request_detail(context, "req-1"),
            _architect_context(),
            id="request-detail",
        ),
        pytest.param(
            lambda backend, context: backend.get_data_product(context, "product-revenue"),
            _architect_context(),
            id="data-product",
        ),
        pytest.param(
            lambda backend, context: backend.get_dashboard(context, "dashboard-revenue"),
            _architect_context(),
            id="dashboard",
        ),
        pytest.param(
            lambda backend, context: backend.get_catalog_asset(context, "asset-revenue"),
            _architect_context(),
            id="catalog-asset",
        ),
        # Only a requester may read their own requests, so the not-delivered branch
        # this test reaches is behind that gate.
        pytest.param(
            lambda backend, context: backend.get_requester_requests(context),
            _requester_context(),
            id="requester-requests",
        ),
    ],
)
def test_missing_downstream_implementation_is_not_delivered_without_fixture_fallback(
    monkeypatch: pytest.MonkeyPatch,
    read: object,
    context: TrustedActorContext,
) -> None:
    _forbid_fixture_data(monkeypatch)
    backend = _backend()

    with pytest.raises(ConsoleUnavailable) as failure:
        read(backend, context)  # type: ignore[operator]

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_missing_impact_reader_fails_closed_without_graph_or_fixture_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fixture_data(monkeypatch)

    with pytest.raises(ConsoleUnavailable) as failure:
        _backend().get_request_impact(_architect_context(), "request-answer")

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_workspace_manifest_states_the_unwired_capabilities_as_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fixture_data(monkeypatch)

    workspace = _backend().get_workspace(_architect_context())
    states = {capability.capability_id: capability.state for capability in workspace.capabilities}

    assert states["warehouse-binding"] == "not_delivered"
    assert states["request-fulfillment"] == "not_delivered"
    assert states["request-conversation"] == "not_delivered"
    assert states["analyst-dashboard"] == "not_delivered"
    assert all(
        capability.dependency is not None
        for capability in workspace.capabilities
        if capability.state == "not_delivered"
    )


def test_an_undelivered_warehouse_binding_is_not_reported_as_setup_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace state must never name a surface the server refuses to serve.

    `setup` sends the console to `/setup`, and `get_setup` raises `capability_not_delivered`
    without a warehouse binding reader. Reporting `setup` for a deployment that delivers no
    warehouse binding therefore routes the reader to a page that answers 503. The capability
    list still carries the absence, so nothing is hidden by summarising the workspace as
    active.
    """
    _forbid_fixture_data(monkeypatch)

    workspace = _backend().get_workspace(_architect_context())
    states = {capability.capability_id: capability.state for capability in workspace.capabilities}

    assert states["warehouse-binding"] == "not_delivered"
    assert workspace.state == "active"


def test_repository_failure_degrades_the_capability_rather_than_serving_fixture_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fixture_data(monkeypatch)
    backend = _backend(warehouse_bindings=_FailingWarehouseBindingReader())

    workspace = backend.get_workspace(_architect_context())
    states = {capability.capability_id: capability.state for capability in workspace.capabilities}

    assert states["warehouse-binding"] == "degraded"
    assert workspace.state == "unavailable"
    assert workspace.recovery_message is not None


def test_repository_failure_raises_a_typed_unavailable_error_for_a_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fixture_data(monkeypatch)
    backend = _backend(warehouse_bindings=_FailingWarehouseBindingReader())

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_setup(_architect_context())

    assert failure.value.code == "downstream_unavailable"
    assert failure.value.recovery_action == "retry"
    assert "sqlite" not in failure.value.safe_message.lower()


def test_setup_projection_reports_the_governed_warehouse_binding() -> None:
    backend = _backend(warehouse_bindings=_StaticWarehouseBindingReader(_binding()))

    setup = backend.get_setup(_architect_context())

    assert setup.warehouse_binding is not None
    assert setup.warehouse_binding.engine == "postgresql"
    assert setup.warehouse_binding.state == "ready"


def test_operation_projection_never_exposes_provider_or_private_identities() -> None:
    handles = InMemoryOperationHandleRepository()
    record = OperationHandleRecord(
        tenant_id=_TENANT,
        console_handle=mint_console_handle(),
        capability_kind="warehouse_lifecycle",
        private_identity=WarehouseOperationIdentity(
            binding_id="whb-0123456789abcdef01234567",
            operation_id="whop-private-operation-identity",
        ).encode(),
    )
    handles.store(record)
    backend = _backend(
        operation_handles=handles,
        warehouse_operations=_StaticWarehouseOperationReader(_private_operation()),
    )

    view = backend.get_operation(_architect_context(), record.console_handle)
    payload = view.model_dump_json()

    assert view.operation_id == record.console_handle
    assert _PROVIDER_HANDLE_CANARY not in payload
    assert "whop-private-operation-identity" not in payload
    assert "whb-0123456789abcdef01234567" not in payload
    assert view.phase == "provider_created"
    assert view.state == "running"


def test_operation_failure_classification_is_preserved_rather_than_flattened() -> None:
    handles = InMemoryOperationHandleRepository()
    record = OperationHandleRecord(
        tenant_id=_TENANT,
        console_handle=mint_console_handle(),
        capability_kind="warehouse_lifecycle",
        private_identity=WarehouseOperationIdentity(
            binding_id="whb-0123456789abcdef01234567",
            operation_id="whop-private-operation-identity",
        ).encode(),
    )
    handles.store(record)
    backend = _backend(
        operation_handles=handles,
        warehouse_operations=_StaticWarehouseOperationReader(
            _private_operation(
                status=WarehouseOperationStatus.FAILED,
                classification=WarehouseFailureClassification.TRANSIENT_UNAVAILABLE,
            )
        ),
    )

    view = backend.get_operation(_architect_context(), record.console_handle)

    assert view.state == "failed"
    assert view.failure is not None
    assert view.failure.classification == "transient"


def test_wrong_tenant_operation_lookup_is_indistinguishable_from_an_unknown_handle() -> None:
    handles = InMemoryOperationHandleRepository()
    record = OperationHandleRecord(
        tenant_id="tenant-beta",
        console_handle=mint_console_handle(),
        capability_kind="warehouse_lifecycle",
        private_identity="whb-other/whop-other",
    )
    handles.store(record)
    backend = _backend(
        operation_handles=handles,
        warehouse_operations=_StaticWarehouseOperationReader(_private_operation()),
    )

    with pytest.raises(ConsoleNotFound) as owned_by_another_tenant:
        backend.get_operation(_architect_context(), record.console_handle)
    with pytest.raises(ConsoleNotFound) as never_minted:
        backend.get_operation(_architect_context(), mint_console_handle())

    assert owned_by_another_tenant.value.code == never_minted.value.code
    assert owned_by_another_tenant.value.safe_message == never_minted.value.safe_message


def test_operation_handle_for_an_unwired_capability_kind_is_not_delivered() -> None:
    handles = InMemoryOperationHandleRepository()
    record = OperationHandleRecord(
        tenant_id=_TENANT,
        console_handle=mint_console_handle(),
        capability_kind="acquisition_batch",
        private_identity="batch-private-identity",
    )
    handles.store(record)
    backend = _backend(operation_handles=handles)

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_operation(_architect_context(), record.console_handle)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_requester_projection_excludes_unapproved_proposal_text() -> None:
    request = _inbox_request("req-00000000000000000001", "actor-requester")
    proposal = _proposal_with_canary(request.request_id)
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
    )

    requests = backend.get_requester_requests(_requester_context())
    outcome = backend.get_clarified_outcome(_requester_context(), request.request_id)
    serialized = json.dumps(
        [view.model_dump(mode="json") for view in requests] + [outcome.model_dump(mode="json")]
    )

    assert isinstance(proposal.subject, StakeholderAnswerDraft)
    assert _PROPOSAL_TEXT_CANARY in proposal.subject.answer_text
    assert _PROPOSAL_TEXT_CANARY not in serialized
    assert outcome.statement_digest == digest(_clarified_outcome(request.request_id))
    assert requests[0].question == "What was net revenue last quarter?"


def test_requester_projection_includes_only_the_safe_no_valid_plan_explanation() -> None:
    request = _inbox_request("req-00000000000000000001", "actor-requester").model_copy(
        update={"state": RequestState.NO_VALID_PLAN}
    )
    safe_explanation = (
        "This local environment has no authoritative source configured for that question."
    )
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(
            request, no_valid_plan_explanation=safe_explanation
        ),
    )

    projected = backend.get_requester_requests(_requester_context())[0]

    assert projected.no_valid_plan_explanation == safe_explanation
    assert projected.question == "What was net revenue last quarter?"


def test_requester_projection_does_not_invent_a_question_for_data_access() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
    )

    projected = backend.get_requester_requests(_requester_context())[0]

    assert projected.question is None


@pytest.mark.parametrize(
    ("state", "title", "summary"),
    (
        ("pending", "Access is being set up", "approved access is being applied"),
        ("active", "Access is active", "available until"),
        ("expired", "Access has expired", "can no longer be used"),
        ("revocation_pending", "Access removal is in progress", "already unavailable"),
        ("revoked", "Access has been removed", "can no longer be used"),
        ("failed", "Access removal needs attention", "remains unavailable"),
    ),
)
def test_requester_projection_explains_authoritative_access_lifecycle(
    state: AccessGrantState, title: str, summary: str
) -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    reader = _StaticAccessGrantReader(_access_grant(state))
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=reader,
    )

    projected = backend.get_requester_requests(_requester_context())[0]

    assert projected.access_lifecycle is not None
    assert projected.access_lifecycle.state == state
    assert projected.access_lifecycle.title == title
    assert summary in projected.access_lifecycle.summary
    assert projected.access_lifecycle.revision == 2
    assert projected.access_lifecycle.can_revoke is (state == "active")
    assert "grant-private-ref" not in projected.model_dump_json()
    assert reader.calls == [(_TENANT, request.request_id)]


def test_requester_projection_hides_historical_delivery_after_access_is_revoked() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    delivery = RequesterAccessDeliveryView(
        access_mode="query",
        fields=("net-revenue",),
        effective_at=_FIXED_TIME,
        expires_at=_FIXED_TIME + timedelta(days=1),
        permissions=("query", "view"),
    )
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request, delivered_access=delivery),
        access_grants=_StaticAccessGrantReader(_access_grant("revoked")),
    )

    projected = backend.get_requester_requests(_requester_context())[0]

    assert projected.access_lifecycle is not None
    assert projected.access_lifecycle.state == "revoked"
    assert projected.delivered_access is None


def test_requester_projection_has_no_access_lifecycle_without_an_authoritative_grant() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_StaticAccessGrantReader(None),
    )

    assert backend.get_requester_requests(_requester_context())[0].access_lifecycle is None


def test_failed_access_setup_is_distinguished_from_failed_removal() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    grant = _access_grant("failed").model_copy(update={"failed_action": "apply"})
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_StaticAccessGrantReader(grant),
    )

    lifecycle = backend.get_requester_requests(_requester_context())[0].access_lifecycle

    assert lifecycle is not None
    assert lifecycle.title == "Access setup needs attention"


def test_corrupt_access_grant_projection_fails_closed() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_FailingAccessGrantReader(),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_requester_requests(_requester_context())

    assert failure.value.code == "downstream_integrity"


def test_misdirected_access_grant_projection_fails_closed() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_MisdirectedAccessGrantReader(
            _access_grant("active", tenant_id="tenant-other")
        ),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_requester_requests(_requester_context())

    assert failure.value.code == "downstream_integrity"


def test_requester_revokes_owned_active_access_without_public_grant_identity() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    requests = _StaticRequestReader((request,))
    active = _access_grant("active")
    revoked = active.model_copy(update={"state": "revoked", "revision": 3})
    commands = _AccessRevocations(revoked)
    backend = _backend(
        requests=requests,
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_StaticAccessGrantReader(active),
        access_revocation_commands=commands,
    )

    result = backend.revoke_access(
        _requester_context(),
        request.request_id,
        AccessRevocationCommand(
            expected_revision=2,
            active_role="requester",
            reason="The analysis is complete.",
        ),
    )

    assert result.state == "revoked"
    assert not result.can_revoke
    assert "grant-private-ref" not in result.model_dump_json()
    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "request_id": request.request_id,
            "actor_id": "actor-requester",
            "expected_revision": 2,
            "reason": "The analysis is complete.",
        }
    ]


def test_architect_revokes_active_access_through_the_owning_service() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    active = _access_grant("active")
    pending = active.model_copy(update={"state": "revocation_pending", "revision": 3})
    commands = _AccessRevocations(pending)
    backend = _backend(
        requests=_StaticRequestReader((request,)),
        access_revocation_commands=commands,
    )

    result = backend.revoke_access(
        _architect_context(),
        request.request_id,
        AccessRevocationCommand(
            expected_revision=2,
            active_role="data_architect",
            reason="The access window should close now.",
        ),
    )

    assert result.state == "revocation_pending"
    assert not result.can_revoke
    assert commands.calls[0]["actor_id"] == _ACTOR


def test_manual_access_revocation_rejects_stale_and_cross_requester_commands() -> None:
    request = _data_access_request("req-00000000000000000002", "actor-requester")
    requests = _StaticRequestReader((request,))
    active = _access_grant("active")
    stale = _AccessRevocations(active, stale=True)
    backend = _backend(
        requests=requests,
        fulfillment=_RequesterOnlyFulfillmentReader(request),
        access_grants=_StaticAccessGrantReader(active),
        access_revocation_commands=stale,
    )
    command = AccessRevocationCommand(
        expected_revision=1,
        active_role="requester",
        reason="The analysis is complete.",
    )

    with pytest.raises(ConsoleConflict) as conflict:
        backend.revoke_access(_requester_context(), request.request_id, command)
    with pytest.raises(ConsoleNotFound):
        backend.revoke_access(
            TrustedActorContext(
                tenant_id=_TENANT,
                actor_id="actor-other-requester",
                roles=("requester",),
                active_role="requester",
                session_id="session-other",
            ),
            request.request_id,
            command,
        )

    assert conflict.value.code == "stale_revision"
    assert len(stale.calls) == 1


def test_requester_projection_excludes_another_requesters_request() -> None:
    mine = _inbox_request("req-00000000000000000001", "actor-requester")
    theirs = _inbox_request("req-00000000000000000002", "actor-someone-else")
    backend = _backend(
        requests=_StaticRequestReader((mine, theirs)),
        fulfillment=_RequesterOnlyFulfillmentReader(mine),
    )

    requests = backend.get_requester_requests(_requester_context())

    assert tuple(view.request_id for view in requests) == (mine.request_id,)


def test_governed_commands_are_not_delivered_until_they_are_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_fixture_data(monkeypatch)
    backend = _backend()

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.reset(
            _architect_context(),
            fixture_reset_command(),
        )

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def fixture_reset_command() -> ResetCommand:
    return ResetCommand(
        expected_revision=1,
        setup_digest="e" * 64,
        reset_token="r" * 40,
        active_role="data_architect",
    )


def test_the_warehouse_options_present_an_engine_name_rather_than_its_enum_value() -> None:
    """`label` is what the architect reads; the enum value is not a name.

    The governed adapter set `label` to `engine.value`, so the choice rendered as
    `postgresql` and `clickhouse`, and it put a whole sentence into
    `supported_region`, a field the browser prints beside a capacity token as a
    caption. Presenting an engine's name is presentation, not authority.
    """
    backend = _backend(warehouse_bindings=_StaticWarehouseBindingReader(None))

    setup = backend.get_setup(_architect_context())

    labels = {option.engine: option.label for option in setup.warehouse_options}
    assert labels == {"postgresql": "PostgreSQL", "clickhouse": "ClickHouse"}
    for option in setup.warehouse_options:
        assert len(option.supported_region.split()) <= 4
        assert not option.supported_region.endswith(".")


class _StubRunReader:
    """A tenant run reader that records the tenant it was asked about."""

    def __init__(self, runs: dict[str, tuple[RunRecord, ...]]) -> None:
        self._runs = runs
        self.asked: list[str] = []

    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]:
        self.asked.append(tenant_id)
        return self._runs.get(tenant_id, ())


def _run(run_id: str, contract_digest: str, state: RunState) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        activation_key=f"activation-{run_id}",
        contract_digest=contract_digest,
        summary_digest="b" * 64,
        signed_graph_json="{}",
        state=state,
        checkpoint="closed",
        batch_id=None,
        created_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, 13, tzinfo=UTC),
    )


def test_runs_are_read_for_the_calling_tenant_only() -> None:
    """The console never widens a read past the tenant its context names."""
    reader = _StubRunReader({_TENANT: (_run("run-1", "a" * 64, "succeeded"),)})
    backend = _backend(runs=reader)

    view = backend.get_runs(_architect_context())

    assert reader.asked == [_TENANT]
    assert [run.run_id for run in view.runs] == ["run-1"]


def test_a_run_projects_the_owner_s_own_state_without_a_lossy_mapping() -> None:
    """`non_conforming` is a witnessed outcome, not an unknown one.

    Folding it into the shared `OperationState` vocabulary would have to call it
    `outcome_unknown`, which reports a known non-conformance as ignorance. The run
    projection therefore carries the evidence store's own vocabulary verbatim.
    """
    reader = _StubRunReader({_TENANT: (_run("run-1", "a" * 64, "non_conforming"),)})
    backend = _backend(runs=reader)

    view = backend.get_runs(_architect_context())

    assert view.runs[0].state == "non_conforming"


def test_a_run_projects_the_contract_digest_rather_than_an_invented_product_name() -> None:
    reader = _StubRunReader({_TENANT: (_run("run-1", "a" * 64, "succeeded"),)})
    backend = _backend(runs=reader)

    view = backend.get_runs(_architect_context())

    assert view.runs[0].contract_digest == "a" * 64


def test_a_tenant_with_no_runs_reads_an_empty_listing_rather_than_a_failure() -> None:
    """Delivered-and-empty is a different answer from not-delivered."""
    backend = _backend(runs=_StubRunReader({}))

    assert backend.get_runs(_architect_context()).runs == ()


def _state_runs(tmp_path: Path, clock: list[datetime]) -> tuple[RunService, str]:
    service = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=lambda: clock[0])
    run = service.materialize(
        RunIntent(
            tenant_id=_TENANT,
            contract_id="contract-revenue",
            contract_revision=2,
            plan_digest="c" * 64,
            trigger_policy_version="daily-v1",
            trigger_window=TriggerWindow(
                starts_at=datetime(2026, 9, 16, tzinfo=UTC),
                ends_at=datetime(2026, 9, 17, tzinfo=UTC),
            ),
            reason="scheduled",
        )
    )
    return service, run.run_id


def test_leased_runs_project_attempts_epochs_and_the_last_durable_boundary(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    service, run_id = _state_runs(tmp_path, clock)
    first = service.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-a", lease_seconds=60)
    clock[0] += timedelta(seconds=61)
    second = service.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60)
    service.extend_lease(tenant_id=_TENANT, claim=second, lease_seconds=120)
    service.complete(
        tenant_id=_TENANT,
        run_id=run_id,
        attempt_number=second.attempt_number,
        epoch=second.epoch,
        worker_id=second.worker_id,
        outcome="failed",
        failure_classification="transient",
        durable_boundary_ref="acquisition_prepared:prepared-1",
    )
    backend = _backend(runs=_StubRunReader({}), run_lifecycle=service)

    (leased,) = backend.get_runs(_architect_context()).leased_runs

    assert leased.run_id == run_id
    assert (leased.contract_id, leased.contract_revision, leased.trigger_reason) == (
        "contract-revenue",
        2,
        "scheduled",
    )
    assert leased.window_starts_at == datetime(2026, 9, 16, tzinfo=UTC)
    assert leased.status == "retryable"
    assert leased.last_durable_boundary_ref == "acquisition_prepared:prepared-1"
    assert [(item.attempt_number, item.epoch, item.outcome) for item in leased.attempts] == [
        (1, 1, None),
        (2, 2, "failed"),
    ]
    assert leased.attempts[0].lease_expires_at == first.lease_expires_at
    assert leased.attempts[0].lease_extensions == 0
    assert leased.attempts[1].lease_extensions == 1
    assert leased.attempts[1].failure_classification == "transient"
    assert leased.attempts[1].durable_boundary_ref == "acquisition_prepared:prepared-1"


def test_leased_runs_are_read_for_the_calling_tenant_only(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    service, _run_id = _state_runs(tmp_path, clock)

    class _Recording:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def describe_runs(self, tenant_id: str) -> tuple[RunLifecycleSnapshot, ...]:
            self.asked.append(tenant_id)
            return service.describe_runs(tenant_id)

    recording = _Recording()
    backend = _backend(runs=_StubRunReader({}), run_lifecycle=recording)

    assert len(backend.get_runs(_architect_context()).leased_runs) == 1
    assert recording.asked == [_TENANT]


def test_leased_runs_are_empty_when_no_run_lifecycle_reader_is_composed() -> None:
    """The witnessed-run listing stays delivered; leased runs are simply absent."""
    backend = _backend(runs=_StubRunReader({_TENANT: (_run("run-1", "a" * 64, "succeeded"),)}))

    view = backend.get_runs(_architect_context())

    assert [run.run_id for run in view.runs] == ["run-1"]
    assert view.leased_runs == ()


def test_an_unavailable_run_lifecycle_read_is_reported_without_losing_witnessed_runs() -> None:
    class _Unavailable:
        def describe_runs(self, tenant_id: str) -> tuple[RunLifecycleSnapshot, ...]:
            raise OSError("state store unavailable")

    backend = _backend(
        runs=_StubRunReader({_TENANT: (_run("run-1", "a" * 64, "succeeded"),)}),
        run_lifecycle=_Unavailable(),
    )

    view = backend.get_runs(_architect_context())

    assert [run.run_id for run in view.runs] == ["run-1"]
    assert view.leased_runs == ()
    assert view.leased_runs_available is False


def test_a_composed_run_lifecycle_read_is_reported_available(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 17, 12, tzinfo=UTC)]
    service, _run_id = _state_runs(tmp_path, clock)

    view = _backend(runs=_StubRunReader({}), run_lifecycle=service).get_runs(_architect_context())

    assert view.leased_runs_available is True
    assert len(view.leased_runs) == 1


class _StubDataProductReader:
    def __init__(self, references: dict[str, tuple[ArtifactReference, ...]]) -> None:
        self._references = references
        self.asked: list[str] = []

    def permitted_references(self, tenant_id: str) -> tuple[ArtifactReference, ...]:
        self.asked.append(tenant_id)
        return self._references.get(tenant_id, ())


def _reference(artifact_id: str, version: int = 1) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest="c" * 64)


class _StubProductPublicationReader:
    def __init__(self, definitions: dict[tuple[str, str, int], CatalogProductDefinition]) -> None:
        self._definitions = definitions
        self.asked: list[tuple[str, ArtifactReference]] = []

    def definition_for_reference(
        self, *, tenant_id: str, product_ref: ArtifactReference
    ) -> CatalogProductDefinition | None:
        self.asked.append((tenant_id, product_ref))
        return self._definitions.get((tenant_id, product_ref.artifact_id, product_ref.version))


def _published_definition(
    *, tenant_id: str = _TENANT, product_id: str = "product-revenue", product_revision: int = 3
) -> CatalogProductDefinition:
    return CatalogProductDefinition(
        tenant_id=tenant_id,
        idempotency_key="a" * 64,
        stable_external_key=catalog_product_external_key(
            tenant_id=tenant_id,
            product_id=product_id,
            product_revision=product_revision,
            generation=7,
        ),
        catalog_binding_id="catalog-alpha",
        catalog_revision=4,
        product_id=product_id,
        product_revision=product_revision,
        generation=7,
        name="Current revenue by region",
        description="Approved revenue grouped by region for finance reporting.",
        owner_refs=("owner:finance",),
        namespace="analytics",
        relation_name="revenue_by_region",
        columns=(
            CatalogColumn(name="region", type_name="TEXT", nullable=False),
            CatalogColumn(name="revenue", type_name="NUMERIC", nullable=False),
        ),
        lineage_sources=(
            CatalogLineageSource(
                source_ref="source:orders",
                freshness_observation_ref=ArtifactReference(
                    artifact_id="freshness:orders", version=8, digest="b" * 64
                ),
                freshness_observation_digest="b" * 64,
                watermark_at=_FIXED_TIME - timedelta(hours=1),
                observed_at=_FIXED_TIME,
            ),
        ),
        contract_digest="d" * 64,
        semantic_version_digest="e" * 64,
        materialization_receipt_ref=ArtifactReference(
            artifact_id="receipt:revenue", version=7, digest="f" * 64
        ),
        materialization_receipt_digest="f" * 64,
        publication_authority_digest="1" * 64,
    )


def test_a_data_product_projects_the_owning_publication_definition() -> None:
    reader = _StubDataProductReader({_TENANT: (_reference("product-revenue", version=3),)})
    publication_reader = _StubProductPublicationReader(
        {(_TENANT, "product-revenue", 3): _published_definition()}
    )
    backend = _backend(data_products=reader, product_publications=publication_reader)

    view = backend.get_data_product(_architect_context(), "product-revenue")

    assert view.data_product_id == "product-revenue"
    assert view.version == 3
    assert view.publication_status == "published"
    assert view.name == "Current revenue by region"
    assert view.description == "Approved revenue grouped by region for finance reporting."
    assert view.product_revision == 3
    assert view.generation == 7
    assert view.catalog_revision == 4
    assert view.namespace == "analytics"
    assert view.relation_name == "revenue_by_region"
    assert view.column_count == 2
    assert view.source_count == 1
    assert view.freshness_observed_at == _FIXED_TIME


def test_a_permitted_but_unpublished_product_invents_no_display_identity() -> None:
    reference = _reference("product-revenue", version=3)
    backend = _backend(
        data_products=_StubDataProductReader({_TENANT: (reference,)}),
        product_publications=_StubProductPublicationReader({}),
    )

    view = backend.get_data_product(_architect_context(), "product-revenue")

    assert view.publication_status == "pending"
    assert view.name is None
    assert view.description is None
    assert view.product_revision is None
    assert view.column_count is None


def test_an_empty_permitted_product_listing_does_not_read_publication_authority() -> None:
    publication_reader = _StubProductPublicationReader({})
    backend = _backend(
        data_products=_StubDataProductReader({_TENANT: ()}),
        product_publications=publication_reader,
    )

    assert backend.get_data_products(_architect_context()).products == ()
    assert publication_reader.asked == []


def test_a_publication_definition_from_another_tenant_is_denied() -> None:
    reference = _reference("product-revenue", version=3)
    publication_reader = _StubProductPublicationReader(
        {(_TENANT, "product-revenue", 3): _published_definition(tenant_id="tenant-other")}
    )
    backend = _backend(
        data_products=_StubDataProductReader({_TENANT: (reference,)}),
        product_publications=publication_reader,
    )

    with pytest.raises(ConsoleNotFound):
        backend.get_data_product(_architect_context(), "product-revenue")


def test_data_product_listing_deduplicates_versions_and_links_the_newest() -> None:
    reader = _StubDataProductReader(
        {
            _TENANT: (
                _reference("product-revenue", version=1),
                _reference("product-customer", version=2),
                _reference("product-revenue", version=3),
            )
        }
    )
    backend = _backend(data_products=reader)

    listing = backend.get_data_products(_architect_context())

    assert [(product.data_product_id, product.version) for product in listing.products] == [
        ("product-customer", 2),
        ("product-revenue", 3),
    ]


def test_a_data_product_outside_the_tenant_s_permitted_references_is_not_found() -> None:
    """Reading is scoped by what the tenant's own policy snapshots permit."""
    reader = _StubDataProductReader({_TENANT: (_reference("product-revenue"),)})
    publication_reader = _StubProductPublicationReader(
        {(_TENANT, "product-somebody-elses", 1): _published_definition()}
    )
    backend = _backend(data_products=reader, product_publications=publication_reader)

    with pytest.raises(ConsoleNotFound):
        backend.get_data_product(_architect_context(), "product-somebody-elses")

    assert reader.asked == [_TENANT]
    assert publication_reader.asked == []


def test_the_newest_version_of_a_permitted_reference_is_the_one_projected() -> None:
    """Policy snapshots accumulate, so the same product appears at several versions."""
    reader = _StubDataProductReader(
        {
            _TENANT: (
                _reference("product-revenue", version=1),
                _reference("product-revenue", version=4),
                _reference("product-revenue", version=2),
            )
        }
    )
    backend = _backend(data_products=reader)

    assert backend.get_data_product(_architect_context(), "product-revenue").version == 4


def test_the_run_capability_is_ready_only_when_both_owning_reads_are_wired() -> None:
    """The register must follow the wiring, not a hardcoded verdict.

    Reporting `not_delivered` for a capability that was merely uncomposed has
    already happened twice in this console, so the state is derived from whether
    the readers are present rather than asserted in a constant.
    """
    unwired = _backend().get_workspace(_architect_context())
    wired = _backend(
        runs=_StubRunReader({}), data_products=_StubDataProductReader({})
    ).get_workspace(_architect_context())

    def state(view: WorkspaceView) -> str:
        return next(
            capability.state
            for capability in view.capabilities
            if capability.capability_id == "data-product-runs"
        )

    assert state(unwired) == "not_delivered"
    assert state(wired) == "ready"


class _RealShapedFulfillmentRepository:
    """Mirrors the OWNING repository, in signature and in what it returns.

    The previous stub took `list_proposals(tenant_id)`. The real repository takes
    `(tenant_id, request_id)` and raises `KeyError` for a policy snapshot it cannot
    read, so the tests agreed with a reader that could never work against it. The
    types below are the repository's own, so a double this permissive cannot come
    back: `tuple[object, ...]` would satisfy any reader at all.
    """

    def __init__(
        self,
        *,
        proposals: dict[str, tuple[FulfillmentProposal, ...]],
        snapshots: dict[str, FulfillmentPolicySnapshot],
    ) -> None:
        self._proposals = proposals
        self._snapshots = snapshots
        self.requested: list[tuple[str, str]] = []

    def list_proposals(self, tenant_id: str, request_id: str) -> tuple[FulfillmentProposal, ...]:
        self.requested.append((tenant_id, request_id))
        return self._proposals.get(request_id, ())

    def load_policy_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentPolicySnapshot:
        if snapshot_digest not in self._snapshots:
            raise KeyError("policy snapshot is unavailable to the tenant")
        return self._snapshots[snapshot_digest]


class _StubRequestLister:
    def __init__(self, request_ids: tuple[str, ...]) -> None:
        self._requests = tuple(
            _inbox_request(identifier, "actor-requester") for identifier in request_ids
        )

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]:
        return self._requests if tenant_id == _TENANT else ()

    def get(self, tenant_id: str, request_id: str) -> InboxRequest:
        for request in self._requests:
            if request.tenant_id == tenant_id and request.request_id == request_id:
                return request
        raise KeyError(request_id)


def _policy(*references: ArtifactReference) -> FulfillmentPolicySnapshot:
    """The real snapshot, not a namespace carrying one attribute.

    A double that only had `permitted_data_product_refs` agreed with any reader
    that asked for it, including one calling a signature the owning repository
    does not accept.
    """
    return FulfillmentPolicySnapshot(
        snapshot_id="pol-00000000000000000001-0123456789abcdef01234567",
        tenant_id=_TENANT,
        requester_id="actor-requester",
        requester_principal_ref="principal:requester",
        purpose_digest="b" * 64,
        approved_policy_refs=(_reference("policy-governed-read"),),
        entitlement_observation_refs=(_reference("entitlement-observation"),),
        classification_rule_refs=(),
        permitted_data_product_refs=references,
        permitted_access_modes=("query",),
        maximum_expiry=None,
        policy_authority_classifications=(),
        observed_at=_FIXED_TIME,
        valid_until=_FIXED_TIME + timedelta(hours=1),
    )


def _proposal(policy_snapshot_digest: str) -> FulfillmentProposal:
    return FulfillmentProposal(
        proposal_id="prp-00000000000000000002-0123456789abcdef01234567",
        tenant_id=_TENANT,
        request_id="req-00000000000000000001-0123456789abcdef01234567",
        request_revision=3,
        revision=1,
        clarified_outcome_digest="a" * 64,
        grounding_snapshot_digest="c" * 64,
        policy_snapshot_digest=policy_snapshot_digest,
        subject=StakeholderAnswerDraft(
            answer_text="Net revenue rose.",
            governed_dataset_refs=(),
            metric_refs=(),
            as_of=_FIXED_TIME,
            freshness_disposition="unknown",
            material_quality_limitations=(),
            lineage_refs=(),
            disclosure_classifications=(),
        ),
        required_approvals=(
            ApprovalRequirement(
                authority_ref="principal:requester",
                reason_code="clarified_outcome_acceptance",
                subject_digest="a" * 64,
            ),
        ),
        created_at=_FIXED_TIME,
    )


def test_permitted_references_walk_every_request_of_the_tenant() -> None:
    """There is no tenant-wide proposal listing; proposals are per request.

    `list_proposals` requires a request id, so the references have to be gathered by
    walking the tenant's own requests. Calling it with a tenant alone raised
    TypeError and made every data-product read a 500.
    """
    repository = _RealShapedFulfillmentRepository(
        proposals={"req-1": (_proposal("d" * 64),), "req-2": (_proposal("e" * 64),)},
        snapshots={
            "d" * 64: _policy(_reference("product-revenue", version=2)),
            "e" * 64: _policy(_reference("product-orders", version=1)),
        },
    )
    reader = PolicyPermittedDataProductReader(
        repository=repository, requests=_StubRequestLister(("req-1", "req-2"))
    )

    references = reader.permitted_references(_TENANT)

    assert repository.requested == [(_TENANT, "req-1"), (_TENANT, "req-2")]
    assert {reference.artifact_id for reference in references} == {
        "product-revenue",
        "product-orders",
    }


def test_a_snapshot_the_tenant_cannot_read_skips_that_proposal() -> None:
    """`load_policy_snapshot` raises rather than returning None.

    The reader tested `snapshot is not None`, a branch that can never be taken, so a
    single unreadable snapshot turned the whole read into a 500 instead of skipping
    the one proposal it belongs to.
    """
    repository = _RealShapedFulfillmentRepository(
        proposals={"req-1": (_proposal("d" * 64), _proposal("f" * 64))},
        snapshots={"d" * 64: _policy(_reference("product-revenue"))},
    )
    reader = PolicyPermittedDataProductReader(
        repository=repository, requests=_StubRequestLister(("req-1",))
    )

    references = reader.permitted_references(_TENANT)

    assert [reference.artifact_id for reference in references] == ["product-revenue"]


class _StubAcquisitionReceiptReader:
    """Mirrors the OWNING store's method name and signature exactly.

    `SQLiteStore.list_acquisition_receipts` is what the console composes in
    governed-local mode, so a double that renamed or reshaped the call would let a
    signature mismatch reach a live run instead of this test.
    """

    def __init__(self, receipts: dict[str, tuple[AcquisitionEvidenceReceipt, ...]]) -> None:
        self._receipts = receipts
        self.asked: list[str] = []

    def list_acquisition_receipts(self, tenant_id: str) -> tuple[AcquisitionEvidenceReceipt, ...]:
        self.asked.append(tenant_id)
        return self._receipts.get(tenant_id, ())


def _receipt(
    evidence_id: str = "evidence-ref:prepared-1",
    *,
    outcome: AcquisitionEvidenceOutcome = "prepared",
    reason_codes: tuple[AcquisitionPublicReasonCode, ...] = (),
    logical_object_refs: tuple[str, ...] = ("orders",),
) -> AcquisitionEvidenceReceipt:
    prepared = outcome in ("prepared", "acknowledged")
    return AcquisitionEvidenceReceipt(
        evidence_id=evidence_id,
        tenant_id=_TENANT,
        run_intent_ref="1" * 64,
        contract_ref="contract:orders:v1",
        source_binding_ref="source-binding:orders",
        acquisition_mode="snapshot",
        logical_object_refs=logical_object_refs,
        prepared_receipt_ref="receipt-ref:prepared-1" if prepared else None,
        checkpoint_receipt_ref="receipt-ref:checkpoint-1" if outcome == "acknowledged" else None,
        prior_checkpoint_revision=0,
        resulting_checkpoint_revision=1 if outcome == "acknowledged" else None,
        reason_codes=reason_codes,
        outcome=outcome,
        created_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )


def test_acquisition_receipts_are_read_for_the_calling_tenant_only() -> None:
    reader = _StubAcquisitionReceiptReader({_TENANT: (_receipt(),)})
    backend = _backend(acquisition_receipts=reader)

    view = backend.get_acquisition_receipts(_architect_context())

    assert reader.asked == [_TENANT]
    assert [receipt.evidence_id for receipt in view.receipts] == ["evidence-ref:prepared-1"]


def test_a_governed_refusal_is_listed_beside_a_successful_acquisition() -> None:
    """A refusal an operator cannot see is a refusal they cannot act on.

    The receipt model carries `outcome` and `reason_codes` precisely so a refusal
    is publishable, so filtering refused receipts out of the listing would discard
    the half of the record that explains why nothing was acquired.
    """
    reader = _StubAcquisitionReceiptReader(
        {
            _TENANT: (
                _receipt("evidence-ref:prepared-1"),
                _receipt(
                    "evidence-ref:refused-1",
                    outcome="no_valid_plan",
                    reason_codes=("contract_not_activated",),
                ),
            )
        }
    )
    backend = _backend(acquisition_receipts=reader)

    view = backend.get_acquisition_receipts(_architect_context())

    assert [receipt.outcome for receipt in view.receipts] == ["prepared", "no_valid_plan"]
    assert view.receipts[1].reason_codes == ("contract_not_activated",)
    assert view.receipts[0].reason_codes == ()


def test_an_acquisition_receipt_projects_references_and_invents_no_names() -> None:
    """The rule `DataProductView` follows applies here unchanged.

    Every identifier on a receipt is a reference an owning service allocated. The
    console shows those references and does not invent a display name for a
    contract, a source binding or a logical object it has no authority to name.
    """
    reader = _StubAcquisitionReceiptReader({_TENANT: (_receipt(),)})
    backend = _backend(acquisition_receipts=reader)

    receipt = backend.get_acquisition_receipts(_architect_context()).receipts[0]

    assert receipt.contract_ref == "contract:orders:v1"
    assert receipt.source_binding_ref == "source-binding:orders"
    assert receipt.logical_object_refs == ("orders",)
    assert receipt.acquisition_mode == "snapshot"


def test_an_acquisition_receipt_projection_excludes_every_private_field() -> None:
    """The receipt is public, but the projection must not widen it by accident.

    A checkpoint revision and a prepared-receipt reference are internal recovery
    state; they say where the runtime is in its own protocol, which is not
    something an operator reads and not something the console should republish.
    """
    reader = _StubAcquisitionReceiptReader({_TENANT: (_receipt(outcome="acknowledged"),)})
    backend = _backend(acquisition_receipts=reader)

    serialized = backend.get_acquisition_receipts(_architect_context()).model_dump_json()

    for forbidden in (
        "prepared_receipt_ref",
        "checkpoint_receipt_ref",
        "prior_checkpoint_revision",
        "resulting_checkpoint_revision",
        "run_intent_ref",
        "tenant_id",
    ):
        assert forbidden not in serialized


def test_a_tenant_with_no_acquisition_receipts_reads_an_empty_listing() -> None:
    """Delivered-and-empty is a different answer from not-delivered."""
    backend = _backend(acquisition_receipts=_StubAcquisitionReceiptReader({}))

    assert backend.get_acquisition_receipts(_architect_context()).receipts == ()


def test_reading_acquisition_receipts_without_a_store_is_not_delivered() -> None:
    with pytest.raises(ConsoleUnavailable) as raised:
        _backend().get_acquisition_receipts(_architect_context())

    assert raised.value.code == CAPABILITY_NOT_DELIVERED


def test_the_acquisition_evidence_capability_follows_the_wiring() -> None:
    unwired = _backend().get_workspace(_architect_context())
    wired = _backend(acquisition_receipts=_StubAcquisitionReceiptReader({})).get_workspace(
        _architect_context()
    )

    def state(view: WorkspaceView, capability_id: str) -> str:
        return next(
            capability.state
            for capability in view.capabilities
            if capability.capability_id == capability_id
        )

    assert state(unwired, "acquisition-evidence") == "not_delivered"
    assert state(wired, "acquisition-evidence") == "ready"


def test_source_acquisition_stays_undelivered_while_no_runtime_is_composed() -> None:
    """Retaining receipts is not the same capability as performing an acquisition.

    Nothing in the console can start an acquisition, so reporting
    `source-acquisition` as ready once the read exists would assert a capability
    that cannot happen -- the exact defect the register was rebuilt to prevent.
    """
    view = _backend(acquisition_receipts=_StubAcquisitionReceiptReader({})).get_workspace(
        _architect_context()
    )

    capability = next(
        item for item in view.capabilities if item.capability_id == "source-acquisition"
    )

    assert capability.state == "not_delivered"
    assert "in-memory test double" not in capability.detail


def test_the_console_acquisition_vocabulary_mirrors_the_owning_receipt_exactly() -> None:
    """A mirrored vocabulary that drifts silently rejects what the owner accepts.

    The console restates these closed vocabularies rather than importing the
    provider SDK it does not depend on, so the mirror is pinned to the owning
    model's own annotations here instead of to a copied literal.
    """
    from typing import get_args

    from heinzel_console.contracts import (
        AcquisitionModeView,
        AcquisitionOutcomeView,
        AcquisitionReasonCodeView,
    )
    from heinzel_evidence import AcquisitionEvidenceReceipt

    fields = AcquisitionEvidenceReceipt.model_fields

    assert set(get_args(AcquisitionOutcomeView)) == set(get_args(fields["outcome"].annotation))
    assert set(get_args(AcquisitionModeView)) == set(
        get_args(fields["acquisition_mode"].annotation)
    )
    assert set(get_args(AcquisitionReasonCodeView)) == set(
        get_args(get_args(fields["reason_codes"].annotation)[0])
    )


def test_the_governed_reads_enforce_the_role_set_the_fixture_backend_declares() -> None:
    """Governed mode must never be more permissive than the demo it stands in for.

    Both reads served any authenticated actor while `FixtureConsoleBackend`
    restricted them to `("data_architect", "data_owner")`, so a requester could read
    acquisition and run evidence in the real product that the demo refused them.
    """
    requester = TrustedActorContext(
        tenant_id=_TENANT,
        actor_id="actor-requester",
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )
    backend = _backend(
        runs=_StubRunReader({_TENANT: (_run("run-1", "a" * 64, "succeeded"),)}),
        acquisition_receipts=_StubAcquisitionReceiptReader({_TENANT: (_receipt(),)}),
    )

    with pytest.raises(ConsoleNotFound):
        backend.get_acquisition_receipts(requester)
    with pytest.raises(ConsoleNotFound):
        backend.get_runs(requester)

    assert backend.get_acquisition_receipts(_architect_context()).receipts != ()
    assert backend.get_runs(_architect_context()).runs != ()


def _role_context(role: ActorRole) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=f"actor-{role}",
        roles=(role,),
        active_role=role,
        session_id=f"session-{role}",
    )


_ALL_ROLES: tuple[ActorRole, ...] = (
    "requester",
    "data_architect",
    "data_owner",
    "policy_approver",
    "budget_approver",
)

# The role set each read is gated on, mirroring what `FixtureConsoleBackend` declares
# for the same read. Governed mode standing in for the demo must not answer an actor
# the demo refuses.
_GATED_READS: tuple[tuple[str, object, tuple[ActorRole, ...]], ...] = (
    ("setup", lambda backend, context: backend.get_setup(context), ("data_architect",)),
    (
        "inbox",
        lambda backend, context: backend.get_inbox(context),
        ("data_architect", "data_owner", "policy_approver", "budget_approver"),
    ),
    (
        "review",
        lambda backend, context: backend.get_review(context, "review-000000000000000000000001"),
        ("data_architect", "data_owner", "policy_approver", "budget_approver"),
    ),
    (
        "requester-requests",
        lambda backend, context: backend.get_requester_requests(context),
        ("requester",),
    ),
    (
        "conversation",
        lambda backend, context: backend.get_conversation(context, "req-1"),
        ("requester", "data_architect", "data_owner", "policy_approver"),
    ),
    (
        "clarified-outcome",
        lambda backend, context: backend.get_clarified_outcome(context, "req-1"),
        ("requester", "data_architect"),
    ),
)


@pytest.mark.parametrize(
    ("read", "allowed_roles", "role"),
    [
        pytest.param(read, allowed, role, id=f"{name}-{role}")
        for name, read, allowed in _GATED_READS
        for role in _ALL_ROLES
    ],
)
def test_each_governed_read_refuses_exactly_the_roles_the_fixture_backend_refuses(
    read: object,
    allowed_roles: tuple[ActorRole, ...],
    role: ActorRole,
) -> None:
    """A role the demo refuses must not be answered by the real product.

    The backend is left unwired, so a permitted role reaches `not_delivered` while a
    refused one must still see `ConsoleNotFound`. That difference is what proves the
    authorization runs *before* the delivery check: an unauthorized actor must not be
    able to learn which capabilities this deployment has wired.
    """
    backend = _backend()
    context = _role_context(role)

    if role in allowed_roles:
        with pytest.raises(ConsoleUnavailable) as delivery:
            read(backend, context)  # type: ignore[operator]
        assert delivery.value.code == CAPABILITY_NOT_DELIVERED
    else:
        with pytest.raises(ConsoleNotFound):
            read(backend, context)  # type: ignore[operator]


def test_every_governed_read_is_gated_or_deliberately_ungated() -> None:
    """The enumeration, not the six checks, is what keeps this closed.

    Applying an authorization helper to six reads is proved by six tests; that the
    helper reaches *every* read is not, and a read added later inherits nothing. This
    fails when a new `get_*` appears without a role set, so leaving one open becomes a
    decision recorded here rather than an omission nobody notices.

    A read is excused only if it is declared `-> Never`, which is how this backend says
    it serves nothing at all. A read that merely *has* a `not_delivered` branch is not
    excused: it answers whenever its reader is wired, and that answer needs a role.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "src" / "heinzel_console" / "governed_backend.py"
    ).read_text(encoding="utf-8")
    backend = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "GovernedConsoleBackend"
    )

    def serves_nothing(node: ast.FunctionDef) -> bool:
        """True when the read refuses unconditionally, by annotation or by body."""
        if isinstance(node.returns, ast.Name) and node.returns.id == "Never":
            return True
        statements = [
            statement
            for statement in node.body
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        ]
        return (
            len(statements) == 1
            and isinstance(statements[0], ast.Raise)
            and isinstance(statements[0].exc, ast.Call)
            and isinstance(statements[0].exc.func, ast.Name)
            and statements[0].exc.func.id == "_not_delivered"
        )

    ungated: set[str] = set()
    for node in backend.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("get_"):
            continue
        if serves_nothing(node):
            continue
        called = {
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        }
        if "_authorize" not in called:
            ungated.add(node.name)

    assert ungated == {
        # The demo authorizes these against `context.roles` or every role, so gating
        # them here would refuse an actor the demo admits.
        "get_session",
        "get_workspace",
        "get_evidence",
        "get_operation",
    }


def test_distinct_terminal_outcomes_keep_distinct_console_states() -> None:
    """A delivered answer and a refusal must never share one lifecycle label."""
    from heinzel_console.governed_backend import _REQUEST_STATES
    from heinzel_request_management import RequestState

    assert _REQUEST_STATES[RequestState.DELIVERED] == "delivered"
    assert _REQUEST_STATES[RequestState.MONITORING] == "delivered"
    assert _REQUEST_STATES[RequestState.NO_VALID_PLAN] == "no_valid_plan"
    assert _REQUEST_STATES[RequestState.CANCELLED] == "cancelled"
    assert _REQUEST_STATES[RequestState.FAILED] == "failed"
    assert _REQUEST_STATES[RequestState.RETIRED] == "closed"
    assert set(_REQUEST_STATES) == set(RequestState)


class _StubSelectableAnswerTermReader:
    """Mirrors the composed answer's own read, and answers with the real artifact.

    `DemoGovernedAnswer.list_selectable_answer_terms` is what the console is given in
    governed-local mode, so the double keeps that name and returns `BoundSemanticReference`
    itself rather than a looser shape: a projection that accepted a bare string here would not
    notice the console asking for a field the binding does not carry.
    """

    def __init__(self, terms: dict[str, tuple[BoundSemanticReference, ...]]) -> None:
        self._terms = terms
        self.asked: list[str] = []

    def list_selectable_answer_terms(self, tenant_id: str) -> tuple[BoundSemanticReference, ...]:
        self.asked.append(tenant_id)
        return self._terms.get(tenant_id, ())


def _bound_term(canonical_ref: str, kind: Literal["dimension", "metric"]) -> BoundSemanticReference:
    return BoundSemanticReference(
        canonical_ref=canonical_ref,
        kind=kind,
        aliases=(),
        version_ref=ArtifactReference(artifact_id=canonical_ref, version=3, digest="a" * 64),
        product_version_ref=ArtifactReference(
            artifact_id="product-revenue", version=2, digest="b" * 64
        ),
    )


def test_selectable_answer_terms_are_the_publications_own_terms_for_the_calling_tenant() -> None:
    """What a requester may compose from is read, never listed by the console.

    The console projects the canonical reference, the kind and the approved version the binding
    carries. Inventing a term, a label or a version here would offer a question the answer
    validation then refuses as an unknown reference.
    """
    reader = _StubSelectableAnswerTermReader(
        {
            _TENANT: (
                _bound_term("daily-order-value", "metric"),
                _bound_term("order_day", "dimension"),
            )
        }
    )
    backend = _backend(selectable_answer_terms=reader)

    view = backend.get_selectable_answer_terms(_requester_context())

    assert reader.asked == [_TENANT]
    assert [(term.term_ref, term.kind) for term in view.terms] == [
        ("daily-order-value", "metric"),
        ("order_day", "dimension"),
    ]
    assert view.terms[0].approved_version.version == 3
    assert view.terms[0].approved_version.digest == "a" * 64


def test_a_tenant_whose_publication_carries_no_terms_reads_an_empty_offering() -> None:
    """Delivered-and-empty is a different answer from not-delivered."""
    backend = _backend(selectable_answer_terms=_StubSelectableAnswerTermReader({}))

    assert backend.get_selectable_answer_terms(_requester_context()).terms == ()


def test_reading_selectable_answer_terms_without_a_publication_is_not_delivered() -> None:
    """A builder with nothing published behind it would be a form nothing can answer."""
    with pytest.raises(ConsoleUnavailable) as raised:
        _backend().get_selectable_answer_terms(_requester_context())

    assert raised.value.code == CAPABILITY_NOT_DELIVERED


def test_the_question_term_builder_capability_follows_the_wiring() -> None:
    unwired = _backend().get_workspace(_architect_context())
    wired = _backend(selectable_answer_terms=_StubSelectableAnswerTermReader({})).get_workspace(
        _architect_context()
    )

    def state(view: WorkspaceView, capability_id: str) -> str:
        return next(
            capability.state
            for capability in view.capabilities
            if capability.capability_id == capability_id
        )

    assert state(unwired, "question-term-builder") == "not_delivered"
    assert state(wired, "question-term-builder") == "ready"


class _StaticSourceBindingReader:
    """The broker's tenant listing, for one tenant and no other."""

    def __init__(self, *bindings: SourceConnectionBinding) -> None:
        self._bindings = bindings
        self.asked: list[str] = []

    def list_for_tenant(self, tenant_id: str) -> tuple[SourceConnectionBinding, ...]:
        self.asked.append(tenant_id)
        return self._bindings if tenant_id == _TENANT else ()


class _StaticEnrolledSourceConnections:
    def __init__(self, *connections: EnrolledSourceConnection) -> None:
        self._connections = connections

    def list_enrolled_source_connections(
        self, tenant_id: str
    ) -> tuple[EnrolledSourceConnection, ...]:
        return self._connections if tenant_id == _TENANT else ()


class _RefusingSourceRegistrationCommands:
    def register_source(
        self,
        *,
        tenant_id: str,
        connection_handle: str,
        provider_kind: str,
        account_mode: str,
        approved_object_refs: tuple[str, ...],
    ) -> Never:
        raise AssertionError("a setup read must not register a source")


def _source_binding(
    *,
    state: SourceConnectionBindingState = SourceConnectionBindingState.READY,
    handle: str = "enrolled-orders",
) -> SourceConnectionBinding:
    validated = state is SourceConnectionBindingState.READY
    return SourceConnectionBinding(
        binding_id="src-0123456789abcdef01234567",
        tenant_id=_TENANT,
        provider_kind="postgresql",
        connection_handle=handle,
        account_mode="not_applicable",
        lifecycle_state=state,
        approved_object_refs=("customer_orders",),
        capability_profile_digest="d" * 64 if validated else None,
        source_observation_ref="source-observation:postgresql:" + "1" * 64 if validated else None,
        credential_revision=1,
        revision=3,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


def _enrolled(handle: str = "enrolled-billing") -> EnrolledSourceConnection:
    return EnrolledSourceConnection(
        connection_handle=handle,
        provider_kind="postgresql",
        account_mode="live",
        declared_object_refs=("invoices",),
    )


def _sources_stage(setup: object) -> SetupStageView:
    assert isinstance(setup, SetupView)
    return next(item for item in setup.stages if item.stage == "sources")


def test_the_sources_stage_waits_on_its_prerequisites_before_it_is_the_current_work() -> None:
    """A wired reader with nothing registered is not started until the stages before it are done.

    The same shape `managed_services` derives: a stage cannot be the work in front of an architect
    while the warehouse and the managed catalog it needs are still unresolved.
    """
    reader = _StaticSourceBindingReader()
    without_catalog = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()), source_bindings=reader
    ).get_setup(_architect_context())

    assert _sources_stage(without_catalog).state == "not_started"

    ready = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_CatalogReader(),
        source_bindings=reader,
    ).get_setup(_architect_context())

    assert _sources_stage(ready).state == "current"
    assert reader.asked == [_TENANT, _TENANT]


def test_the_sources_stage_is_complete_only_once_a_registered_source_is_ready() -> None:
    """A draft is work started, not a registered source.

    `record_validation` is the only writer of `ready`, so every other state means no probe
    evidence exists -- and a stage reported complete over one would claim a source the broker
    never admitted.
    """
    for state in (
        SourceConnectionBindingState.DRAFT,
        SourceConnectionBindingState.VALIDATING,
        SourceConnectionBindingState.FAILED,
    ):
        setup = _backend(
            warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
            catalog_bindings=_CatalogReader(),
            source_bindings=_StaticSourceBindingReader(_source_binding(state=state)),
        ).get_setup(_architect_context())

        assert _sources_stage(setup).state == "current"

    complete = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_CatalogReader(),
        source_bindings=_StaticSourceBindingReader(_source_binding()),
    ).get_setup(_architect_context())

    assert _sources_stage(complete).state == "complete"


def test_another_tenants_registered_source_is_not_readable_as_this_workspaces() -> None:
    reader = _StaticSourceBindingReader(_source_binding())
    backend = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_CatalogReader(),
        source_bindings=reader,
        enrolled_source_connections=_StaticEnrolledSourceConnections(_enrolled()),
    )
    other_tenant = TrustedActorContext(
        tenant_id="tenant-beta",
        actor_id=_ACTOR,
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-architect",
    )

    setup = backend.get_setup(other_tenant)

    assert setup.sources == ()
    assert setup.enrollable_sources == ()
    assert reader.asked == ["tenant-beta"]


def test_an_enrolled_connection_already_registered_is_not_offered_again() -> None:
    """The subtraction is by handle, so the one source behind a handle is registered once."""
    backend = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_CatalogReader(),
        source_bindings=_StaticSourceBindingReader(_source_binding(handle="enrolled-orders")),
        enrolled_source_connections=_StaticEnrolledSourceConnections(
            _enrolled("enrolled-orders"), _enrolled("enrolled-billing")
        ),
    )

    setup = backend.get_setup(_architect_context())

    assert [item.connection_handle for item in setup.enrollable_sources] == ["enrolled-billing"]
    offered = setup.enrollable_sources[0]
    assert offered.source_type == "postgresql"
    assert offered.account_mode == "live"
    assert offered.declared_object_refs == ("invoices",)


def test_the_source_registration_capability_follows_the_wiring() -> None:
    """Read alone is degraded, not ready: nothing can be registered through it."""

    def state(view: WorkspaceView) -> str:
        return next(
            capability.state
            for capability in view.capabilities
            if capability.capability_id == "source-registration"
        )

    unwired = _backend().get_workspace(_architect_context())
    read_only = _backend(source_bindings=_StaticSourceBindingReader()).get_workspace(
        _architect_context()
    )
    wired = _backend(
        source_bindings=_StaticSourceBindingReader(),
        enrolled_source_connections=_StaticEnrolledSourceConnections(),
        source_registration_commands=_RefusingSourceRegistrationCommands(),
    ).get_workspace(_architect_context())

    assert state(unwired) == "not_delivered"
    assert state(read_only) == "degraded"
    assert state(wired) == "ready"


def test_a_registered_source_projection_carries_no_connection_detail() -> None:
    """Asserted over the serialized view, because that is what reaches a browser.

    A binding carries a handle and, privately in the broker, the reference pair that opens its
    credential. The projection must carry the handle and neither reference, and nothing shaped
    like a connection string.
    """
    backend = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        catalog_bindings=_CatalogReader(),
        source_bindings=_StaticSourceBindingReader(_source_binding()),
        enrolled_source_connections=_StaticEnrolledSourceConnections(_enrolled()),
    )

    payload = backend.get_setup(_architect_context()).model_dump_json()

    assert "enrolled-orders" in payload
    assert "postgresql://" not in payload
    assert "endpoint-ref:" not in payload
    assert "credential-ref:" not in payload
    assert "password" not in payload


def test_a_broker_whose_register_is_unreadable_reports_retry_rather_than_an_empty_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable register is not a workspace with no sources."""

    class _FailingSourceBindingReader:
        def list_for_tenant(self, tenant_id: str) -> Never:
            raise SourceBindingPersistenceError(operation="list source bindings")

    _forbid_fixture_data(monkeypatch)
    backend = _backend(
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
        source_bindings=_FailingSourceBindingReader(),
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.get_setup(_architect_context())

    assert failure.value.code == "downstream_unavailable"
    assert failure.value.recovery_action == "retry"
    assert "sqlite" not in failure.value.safe_message.lower()


def _publication_intent(
    *, intent_id: str = "dashboard-publication-1", expected_revision: int = 1
) -> DashboardPublicationIntent:
    return DashboardPublicationIntent(
        intent_id=intent_id,
        tenant_id=_TENANT,
        dashboard_id="internal:revenue-dashboard",
        dashboard_version=3,
        request_id="req-00000000000000000002",
        answer_id="answer-1",
        expected_revision=expected_revision,
        command_digest="1" * 64,
        result_expires_at=datetime(2026, 10, 7, 13, tzinfo=UTC),
        declared_at=datetime(2026, 10, 7, 12, tzinfo=UTC),
    )


def _publication_record(
    *,
    state: str,
    outcome: str | None = None,
    failure_code: str | None = None,
    dashboard_revision: int | None = None,
) -> DashboardPublicationRecord:
    intent = _publication_intent()
    attempts: tuple[DashboardPublicationAttempt, ...] = ()
    if outcome is not None:
        # Built through validation so the closed vocabularies this test parametrizes over are
        # checked against the owning model rather than asserted by the test's own annotations.
        attempts = (
            DashboardPublicationAttempt.model_validate(
                {
                    "intent_id": intent.intent_id,
                    "intent_digest": intent.intent_digest,
                    "attempt": 1,
                    "outcome": outcome,
                    "failure_code": failure_code,
                    "dashboard_revision": dashboard_revision,
                    "desired_digest": None if dashboard_revision is None else "e" * 64,
                    "observed_at": datetime(2026, 10, 7, 12, 5, tzinfo=UTC),
                }
            ),
        )
    return DashboardPublicationRecord.model_validate(
        {"intent": intent, "state": state, "attempts": attempts}
    )


class _PublicationCommands:
    def __init__(self, record: DashboardPublicationRecord | Exception) -> None:
        self._record = record
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        *,
        tenant_id: str,
        request_id: str,
        dashboard_id: str,
        dashboard_version: int,
        expected_revision: int,
    ) -> DashboardPublicationRecord:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "dashboard_id": dashboard_id,
                "dashboard_version": dashboard_version,
                "expected_revision": expected_revision,
            }
        )
        if isinstance(self._record, Exception):
            raise self._record
        return self._record


def _publication_command(*, expected_revision: int = 1) -> DashboardPublicationCommand:
    return DashboardPublicationCommand(
        expected_revision=expected_revision,
        active_role="data_architect",
        dashboard_id="internal:revenue-dashboard",
        dashboard_version=3,
        request_id="req-00000000000000000002",
    )


def test_publishing_a_dashboard_without_a_bi_workflow_reports_the_capability_as_not_delivered() -> (
    None
):
    backend = _backend()

    with pytest.raises(ConsoleUnavailable) as refusal:
        backend.publish_dashboard(_architect_context(), _publication_command())

    assert refusal.value.code == "capability_not_delivered"


def test_the_dashboard_publication_capability_is_ready_only_with_a_wired_workflow() -> None:
    without = _backend().get_workspace(_architect_context()).capabilities
    with_workflow = (
        _backend(
            dashboard_publication_commands=_PublicationCommands(
                _publication_record(state="published", outcome="published", dashboard_revision=1)
            )
        )
        .get_workspace(_architect_context())
        .capabilities
    )

    assert (
        next(item.state for item in without if item.capability_id == "dashboard-publication")
        == "not_delivered"
    )
    assert (
        next(item.state for item in with_workflow if item.capability_id == "dashboard-publication")
        == "ready"
    )


def test_a_published_dashboard_reports_the_revision_the_provider_applied() -> None:
    commands = _PublicationCommands(
        _publication_record(state="published", outcome="published", dashboard_revision=2)
    )
    backend = _backend(dashboard_publication_commands=commands)

    operation = backend.publish_dashboard(
        _architect_context(), _publication_command(expected_revision=2)
    )

    assert operation.state == "succeeded"
    assert operation.phase == "dashboard_published"
    assert operation.revision == 2
    assert operation.failure is None
    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "request_id": "req-00000000000000000002",
            "dashboard_id": "internal:revenue-dashboard",
            "dashboard_version": 3,
            "expected_revision": 2,
        }
    ]


def test_a_closed_publication_window_is_permanent_and_names_its_own_reason() -> None:
    backend = _backend(
        dashboard_publication_commands=_PublicationCommands(
            _publication_record(state="expired", outcome="expired")
        )
    )

    operation = backend.publish_dashboard(_architect_context(), _publication_command())

    assert operation.state == "failed"
    assert operation.failure is not None
    assert operation.failure.code == "dashboard_publication_window_expired"
    assert operation.failure.classification == "permanent"
    assert "no longer readable" in operation.failure.safe_message
    assert "retry" not in operation.recovery_actions


@pytest.mark.parametrize(
    ("failure_code", "state", "classification"),
    [
        ("authority_unavailable", "pending", "transient"),
        ("provider_unavailable", "pending", "transient"),
        ("no_valid_plan", "failed", "permanent"),
        ("stale_revision", "failed", "permanent"),
        ("provider_ambiguous", "failed", "permanent"),
        ("provider_rejected", "failed", "permanent"),
        ("authority_invalid", "failed", "permanent"),
    ],
)
def test_each_publication_failure_is_reported_with_its_own_classification(
    failure_code: str, state: str, classification: str
) -> None:
    backend = _backend(
        dashboard_publication_commands=_PublicationCommands(
            _publication_record(state=state, outcome="failed", failure_code=failure_code)
        )
    )

    operation = backend.publish_dashboard(_architect_context(), _publication_command())

    assert operation.state == "failed"
    assert operation.failure is not None
    assert operation.failure.code == f"dashboard_publication_{failure_code}"
    assert operation.failure.classification == classification


def test_only_a_data_architect_can_publish_a_dashboard() -> None:
    backend = _backend(
        dashboard_publication_commands=_PublicationCommands(
            _publication_record(state="published", outcome="published", dashboard_revision=1)
        )
    )

    # An unauthorized role is answered exactly as an unknown resource is, by design.
    with pytest.raises(ConsoleNotFound):
        backend.publish_dashboard(_requester_context(), _publication_command())
