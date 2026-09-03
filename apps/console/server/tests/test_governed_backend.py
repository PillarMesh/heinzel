from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from pillarmesh_console import fixture_backend, fixture_data
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.errors import ConsoleNotFound, ConsoleUnavailable
from pillarmesh_console.governed_adapters import (
    GovernedWorkspaceIdentity,
    WarehouseOperationIdentity,
)
from pillarmesh_console.governed_backend import (
    CAPABILITY_NOT_DELIVERED,
    GovernedConsoleBackend,
)
from pillarmesh_console.operation_handles import (
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    mint_console_handle,
)
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    ArchitectRequestView,
    ClarifiedOutcomeStatement,
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
            {
                "authority_ref": "principal:requester",
                "reason_code": "clarified_outcome_acceptance",
                "subject_digest": digest(_clarified_outcome(request_id)),
            },
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

    def __init__(self, request: InboxRequest) -> None:
        self._request = request

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
        )

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView:
        raise AssertionError("a requester projection must never read the architect projection")


def _backend(**overrides: object) -> GovernedConsoleBackend:
    defaults: dict[str, object] = {
        "identity": _IDENTITY,
        "operation_handles": InMemoryOperationHandleRepository(),
    }
    return GovernedConsoleBackend(**(defaults | overrides))  # type: ignore[arg-type]


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
    "read",
    [
        pytest.param(lambda backend, context: backend.get_setup(context), id="setup"),
        pytest.param(lambda backend, context: backend.get_inbox(context), id="inbox"),
        pytest.param(lambda backend, context: backend.get_runs(context), id="runs"),
        pytest.param(
            lambda backend, context: backend.get_review(context, "review-000000000000000000000001"),
            id="review",
        ),
        pytest.param(
            lambda backend, context: backend.get_conversation(context, "req-1"), id="conversation"
        ),
        pytest.param(
            lambda backend, context: backend.get_request_detail(context, "req-1"),
            id="request-detail",
        ),
        pytest.param(
            lambda backend, context: backend.get_data_product(context, "product-revenue"),
            id="data-product",
        ),
        pytest.param(
            lambda backend, context: backend.get_dashboard(context, "dashboard-revenue"),
            id="dashboard",
        ),
        pytest.param(
            lambda backend, context: backend.get_catalog_asset(context, "asset-revenue"),
            id="catalog-asset",
        ),
        pytest.param(
            lambda backend, context: backend.get_requester_requests(context),
            id="requester-requests",
        ),
    ],
)
def test_missing_downstream_implementation_is_not_delivered_without_fixture_fallback(
    monkeypatch: pytest.MonkeyPatch,
    read: object,
) -> None:
    _forbid_fixture_data(monkeypatch)
    backend = _backend()

    with pytest.raises(ConsoleUnavailable) as failure:
        read(backend, _architect_context())  # type: ignore[operator]

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

    assert _PROPOSAL_TEXT_CANARY in proposal.subject.answer_text
    assert _PROPOSAL_TEXT_CANARY not in serialized
    assert outcome.statement_digest == digest(_clarified_outcome(request.request_id))


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


def fixture_reset_command() -> object:
    from pillarmesh_console.contracts import ResetCommand

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
