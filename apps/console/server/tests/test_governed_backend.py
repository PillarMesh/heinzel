from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_console import fixture_backend, fixture_data
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.contracts import ActorRole, ResetCommand, WorkspaceView
from pillarmesh_console.errors import ConsoleNotFound, ConsoleUnavailable
from pillarmesh_console.governed_adapters import (
    CatalogBindingReader,
    CatalogSearchHealthReader,
    DataProductReferenceReader,
    FulfillmentViewReader,
    GovernedWorkspaceIdentity,
    PolicyPermittedDataProductReader,
    RequestInboxReader,
    TenantAcquisitionReceiptReader,
    TenantRunReader,
    WarehouseBindingReader,
    WarehouseOperationIdentity,
    WarehouseOperationReader,
)
from pillarmesh_console.governed_backend import (
    CAPABILITY_NOT_DELIVERED,
    GovernedConsoleBackend,
)
from pillarmesh_console.operation_handles import (
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    OperationHandleRepository,
    mint_console_handle,
)
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_evidence import (
    AcquisitionEvidenceOutcome,
    AcquisitionEvidenceReceipt,
    AcquisitionPublicReasonCode,
    RunRecord,
    RunState,
)
from pillarmesh_request_management import (
    ApprovalRequirement,
    ArchitectRequestView,
    ClarifiedOutcomeStatement,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    InboxRequest,
    RequesterRequestView,
    RequestState,
    StakeholderAnswerDraft,
)
from pillarmesh_request_management.requester_view import OwnDecisionView
from pillarmesh_warehouse_control import (
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
        self, request: InboxRequest, *, no_valid_plan_explanation: str | None = None
    ) -> None:
        self._request = request
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
        )

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView:
        raise AssertionError("a requester projection must never read the architect projection")


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
    acquisition_receipts: TenantAcquisitionReceiptReader | None = None,
    data_products: DataProductReferenceReader | None = None,
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
        acquisition_receipts=acquisition_receipts,
        data_products=data_products,
    )


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
    source = Path(__file__).resolve().parents[1] / "src" / "pillarmesh_console"

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


class _StubDataProductReader:
    def __init__(self, references: dict[str, tuple[ArtifactReference, ...]]) -> None:
        self._references = references
        self.asked: list[str] = []

    def permitted_references(self, tenant_id: str) -> tuple[ArtifactReference, ...]:
        self.asked.append(tenant_id)
        return self._references.get(tenant_id, ())


def _reference(artifact_id: str, version: int = 1) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest="c" * 64)


def test_a_data_product_projects_only_the_reference_an_owning_service_asserts() -> None:
    """A data product is an `ArtifactReference` and nothing more.

    No service stores a name, a state or a summary for one, so the projection
    carries the identifier, the version and the digest, and the console invents
    none of the rest.
    """
    reader = _StubDataProductReader({_TENANT: (_reference("product-revenue", version=3),)})
    backend = _backend(data_products=reader)

    view = backend.get_data_product(_architect_context(), "product-revenue")

    assert view.data_product_id == "product-revenue"
    assert view.version == 3
    assert view.artifact_digest == "c" * 64


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
    backend = _backend(data_products=reader)

    with pytest.raises(ConsoleNotFound):
        backend.get_data_product(_architect_context(), "product-somebody-elses")

    assert reader.asked == [_TENANT]


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

    from pillarmesh_console.contracts import (
        AcquisitionModeView,
        AcquisitionOutcomeView,
        AcquisitionReasonCodeView,
    )
    from pillarmesh_evidence import AcquisitionEvidenceReceipt

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
        Path(__file__).resolve().parents[1] / "src" / "pillarmesh_console" / "governed_backend.py"
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
    from pillarmesh_console.governed_backend import _REQUEST_STATES
    from pillarmesh_request_management import RequestState

    assert _REQUEST_STATES[RequestState.DELIVERED] == "delivered"
    assert _REQUEST_STATES[RequestState.MONITORING] == "delivered"
    assert _REQUEST_STATES[RequestState.NO_VALID_PLAN] == "no_valid_plan"
    assert _REQUEST_STATES[RequestState.CANCELLED] == "cancelled"
    assert _REQUEST_STATES[RequestState.FAILED] == "failed"
    assert _REQUEST_STATES[RequestState.RETIRED] == "closed"
    assert set(_REQUEST_STATES) == set(RequestState)
