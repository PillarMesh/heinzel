"""Governed-local command delegation.

Every test here proves one of four properties: the exact domain identity the owning
service needs reaches it unchanged, the console never invents an owning transaction,
a transient failure never becomes a terminal verdict, and no private operation
identity leaves the handle repository.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never

import pytest
from pillarmesh_console.auth import InFlightCommandKeys, TrustedActorContext
from pillarmesh_console.contracts import (
    ClarifiedOutcomeAcceptanceCommand,
    ConversationMessageCommand,
    CreateRequestCommand,
    DecisionCommand,
    ProcessPackageCommand,
    ResetCommand,
    RetryOperationCommand,
    WarehouseBindingCommand,
)
from pillarmesh_console.errors import (
    ConsoleConflict,
    ConsoleInvalidRequest,
    ConsoleNotFound,
    ConsoleUnavailable,
)
from pillarmesh_console.governed_adapters import (
    GovernedWorkspaceIdentity,
    InMemoryWorkspacePrincipalDirectory,
    WarehouseConfirmation,
    WarehouseOperationIdentity,
)
from pillarmesh_console.governed_backend import (
    CAPABILITY_NOT_DELIVERED,
    GovernedConsoleBackend,
)
from pillarmesh_console.operation_handles import (
    CONSOLE_HANDLE_PATTERN,
    InMemoryOperationHandleRepository,
)
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    ArchitectRequestView,
    ClarifiedOutcomeStatement,
    FulfillmentApprovalBinding,
    FulfillmentProposal,
    InboxRequest,
    RequesterRequestView,
    RequestManagementService,
    RequestState,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)
from pillarmesh_request_management.fulfillment_models import ApprovalRequirement
from pillarmesh_request_management.requester_view import OwnDecisionView
from pillarmesh_semantic_registry import OntologyReviewBundle, OntologyReviewItem
from pillarmesh_semantic_registry.review import ReviewItemDecision
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseFailureClassification,
    WarehousePersistenceError,
)

_TENANT = "tenant-alpha"
_ARCHITECT = "actor-architect"
_REQUESTER = "actor-requester"
_REQUESTER_PRINCIPAL = "principal:requester:actor-requester"
_ARCHITECT_PRINCIPAL = "role:data_engineering_architect"
_FIXED_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_PRIVATE_OPERATION_ID = "wop-private-operation-identity"

_IDENTITY = GovernedWorkspaceIdentity(
    tenant_ref="tenant-alpha",
    tenant_display_name="Alpha",
    workspace_ref="workspace-alpha",
    workspace_display_name="Alpha workspace",
)


def _clock() -> datetime:
    return _FIXED_TIME


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


def _principals() -> InMemoryWorkspacePrincipalDirectory:
    directory = InMemoryWorkspacePrincipalDirectory()
    directory.bind_principal(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        role="requester",
        principal_ref=_REQUESTER_PRINCIPAL,
    )
    directory.bind_principal(
        tenant_id=_TENANT,
        actor_id=_ARCHITECT,
        role="data_architect",
        principal_ref=_ARCHITECT_PRINCIPAL,
    )
    return directory


def _binding(
    state: WarehouseBindingState = WarehouseBindingState.READY, revision: int = 3
) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="whb-0123456789abcdef01234567",
        tenant_id=_TENANT,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west-2",
        capacity_profile="mvp-fixed",
        capability_profile_digest="a" * 64,
        lifecycle_state=state,
        revision=revision,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _RecordingWarehouseCommands:
    def __init__(self, confirmation: WarehouseConfirmation) -> None:
        self._confirmation = confirmation
        self.calls: list[dict[str, str]] = []

    def confirm_binding(
        self, *, tenant_id: str, engine: str, region: str, capacity: str
    ) -> WarehouseConfirmation:
        self.calls.append(
            {"tenant_id": tenant_id, "engine": engine, "region": region, "capacity": capacity}
        )
        return self._confirmation


class _FailingWarehouseCommands:
    def confirm_binding(self, *, tenant_id: str, engine: str, region: str, capacity: str) -> Never:
        raise WarehousePersistenceError("save warehouse binding")


class _StaticWarehouseBindingReader:
    def __init__(self, binding: WarehouseBinding | None) -> None:
        self._binding = binding

    def current_binding(self, tenant_id: str) -> WarehouseBinding | None:
        return self._binding if tenant_id == _TENANT else None


class _RecordingFulfillmentCommands:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
        subject_digest: str,
        decision: Literal["approve", "reject", "request_changes"],
        expected_revision: int,
    ) -> FulfillmentApprovalBinding:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "actor_id": actor_id,
                "authority_ref": authority_ref,
                "subject_digest": subject_digest,
                "decision": decision,
                "expected_revision": expected_revision,
            }
        )
        return FulfillmentApprovalBinding(
            approval_id="apr-0000000000000000000001-" + "b" * 24,
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=expected_revision,
            proposal_id="prp-0000000000000000000001-" + "c" * 24,
            proposal_revision=1,
            proposal_digest="d" * 64,
            subject_digest=subject_digest,
            actor_id=actor_id,
            authority_ref=authority_ref,
            decision=decision,
            created_at=_FIXED_TIME,
        )


class _UnavailableFulfillmentCommands:
    def record_approval(self, **_: object) -> Never:
        raise OSError("fulfillment repository is unreachable")


def _statement(request: InboxRequest) -> ClarifiedOutcomeStatement:
    return ClarifiedOutcomeStatement(
        statement_id="cls-0000000000000000000001-" + "a" * 24,
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        restated_request="Report governed net revenue for the last closed quarter.",
        purpose_digest=digest(request.payload.purpose),
        in_scope_summary="Governed revenue datasets only.",
        out_of_scope_summary="No customer-level detail.",
        created_at=_FIXED_TIME,
    )


def _answer_draft() -> StakeholderAnswerDraft:
    return StakeholderAnswerDraft(
        answer_text="Net revenue was 12.4 million for the quarter.",
        governed_dataset_refs=(),
        metric_refs=(),
        as_of=_FIXED_TIME,
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(),
        disclosure_classifications=(),
    )


def _proposal(request: InboxRequest, statement: ClarifiedOutcomeStatement) -> FulfillmentProposal:
    subject = _answer_draft()
    return FulfillmentProposal(
        proposal_id="prp-0000000000000000000001-" + "c" * 24,
        tenant_id=request.tenant_id,
        request_id=request.request_id,
        request_revision=request.revision,
        revision=1,
        clarified_outcome_digest=digest(statement),
        grounding_snapshot_digest="e" * 64,
        policy_snapshot_digest="f" * 64,
        subject=subject,
        required_approvals=(
            ApprovalRequirement(
                authority_ref=_ARCHITECT_PRINCIPAL,
                reason_code="data_engineering_architect",
                subject_digest=digest(subject),
            ),
            ApprovalRequirement(
                authority_ref=_REQUESTER_PRINCIPAL,
                reason_code="clarified_outcome_acceptance",
                subject_digest=digest(statement),
            ),
        ),
        created_at=_FIXED_TIME,
    )


class _StaticFulfillmentViews:
    def __init__(self, *, request: InboxRequest, statement: ClarifiedOutcomeStatement) -> None:
        self._request = request
        self._statement = statement
        self.own_decisions: tuple[OwnDecisionView, ...] = ()

    def requester_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> RequesterRequestView:
        if request_id != self._request.request_id or actor_id != self._request.requester_id:
            raise KeyError(request_id)
        return RequesterRequestView(
            request_id=self._request.request_id,
            state=self._request.state,
            revision=self._request.revision,
            clarified_outcomes=(self._statement,),
            own_decisions=self.own_decisions,
            fulfillment_status="in_review",
            denial_explanation=None,
        )

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView:
        if request_id != self._request.request_id:
            raise KeyError(request_id)
        return ArchitectRequestView(
            request=self._request,
            clarified_outcomes=(self._statement,),
            proposals=(_proposal(self._request, self._statement),),
            approvals=(),
            admissions=(),
            dependencies=(),
            no_valid_plans=(),
            denials=(),
            evidence=(),
        )


class _RecordingSemanticReviewCommands:
    def __init__(self, bundle: OntologyReviewBundle) -> None:
        self._bundle = bundle
        self.calls: list[dict[str, object]] = []

    def decide_item(
        self,
        *,
        tenant_id: str,
        bundle_id: str,
        item_id: str,
        decision: ReviewItemDecision,
        actor_id: str,
        expected_revision: int,
        revised_content: str | None = None,
        merge_candidate_ids: tuple[str, ...] = (),
    ) -> OntologyReviewBundle:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "bundle_id": bundle_id,
                "item_id": item_id,
                "decision": decision,
                "actor_id": actor_id,
                "expected_revision": expected_revision,
                "revised_content": revised_content,
                "merge_candidate_ids": merge_candidate_ids,
            }
        )
        return self._bundle.model_copy(
            update={"revision": expected_revision + 1, "updated_at": _FIXED_TIME}
        )


class _StaticSemanticReviewReader:
    def __init__(self, bundle: OntologyReviewBundle) -> None:
        self._bundle = bundle

    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle:
        if bundle_id != self._bundle.bundle_id:
            raise KeyError(bundle_id)
        return self._bundle


def _review_bundle(pending_items: int = 1) -> OntologyReviewBundle:
    return OntologyReviewBundle(
        bundle_id="orb-0000000000000000000001-" + "a" * 24,
        tenant_id=_TENANT,
        revision=4,
        candidate_set_digest="1" * 64,
        authority_observation_digests=("2" * 64,),
        items=tuple(
            OntologyReviewItem(
                item_id=f"ori-{index}",
                candidate_ids=(f"cand-{index}",),
                authority_resolution_digest="3" * 64,
                required_authority_ref="role:data_owner",
                semantic_revision_digest="4" * 64,
                status="pending",
            )
            for index in range(pending_items)
        ),
        required_authority_refs=("role:data_owner",),
        status="open",
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


class _Stack:
    def __init__(self, directory: Path) -> None:
        self.repository = SQLiteRequestRepository.open(str(directory / "requests.sqlite3"))
        self.requests = RequestManagementService(self.repository, clock=_clock)
        self.handles = InMemoryOperationHandleRepository()
        self.principals = _principals()

    def close(self) -> None:
        self.repository.close()

    def backend(self, **overrides: object) -> GovernedConsoleBackend:
        arguments: dict[str, object] = {
            "identity": _IDENTITY,
            "operation_handles": self.handles,
            "principals": self.principals,
            "requests": self.requests,
            "request_commands": self.requests,
        }
        arguments.update(overrides)
        return GovernedConsoleBackend(**arguments)  # type: ignore[arg-type]


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[_Stack]:
    governed = _Stack(tmp_path)
    try:
        yield governed
    finally:
        governed.close()


def _question(stack: _Stack) -> InboxRequest:
    return stack.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="Quarterly board reporting",
        question="What was net revenue last quarter?",
    )


def _conversation_digest(request_id: str, revision: int, message_ids: tuple[str, ...]) -> str:
    payload = "\x00".join((request_id, str(revision), *message_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_request_intake_delegates_the_trusted_tenant_and_actor_not_the_browser_payload(
    stack: _Stack,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Quarterly revenue",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            },
        }
    )

    created = stack.backend().create_request(_requester_context(), command)
    stored = stack.requests.get(_TENANT, created.request_id)

    assert stored.requester_id == _REQUESTER
    assert stored.tenant_id == _TENANT
    assert stored.state is RequestState.SUBMITTED
    assert stored.title == command.title
    assert created.title == command.title
    assert stack.backend().get_inbox(_architect_context()).items[0].title == command.title
    assert created.state == "submitted"


def test_data_access_intake_delegates_every_declared_scope_field(stack: _Stack) -> None:
    expires_at = _FIXED_TIME + timedelta(days=7)
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Revenue export",
            "request": {
                "kind": "data_access",
                "purpose": "Quarterly board reporting",
                "data_product_ref": "product-revenue",
                "requested_fields": ["order_total", "closed_at"],
                "access_mode": "export",
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        }
    )

    created = stack.backend().create_request(_requester_context(), command)
    stored = stack.requests.get(_TENANT, created.request_id)

    assert stored.title == command.title
    assert created.title == command.title
    assert stack.backend().get_inbox(_architect_context()).items[0].title == command.title
    assert stored.payload.request_type == "data_access"
    assert stored.payload.requested_fields == ("order_total", "closed_at")
    assert stored.payload.access_mode == "export"
    assert stored.payload.expires_at == expires_at


def test_request_intake_refuses_a_command_role_the_trusted_context_does_not_hold(
    stack: _Stack,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Quarterly revenue",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "Quarterly board reporting",
                "question": "What was net revenue last quarter?",
            },
        }
    )

    with pytest.raises(ConsoleNotFound):
        stack.backend().create_request(_architect_context(), command)


def test_conversation_projection_publishes_the_digest_a_reply_must_be_written_against(
    stack: _Stack,
) -> None:
    request = _question(stack)

    conversation = stack.backend().get_conversation(_requester_context(), request.request_id)

    assert conversation.request_id == request.request_id
    assert conversation.revision == request.revision
    assert conversation.messages == ()
    assert conversation.conversation_digest == _conversation_digest(request.request_id, 1, ())


def test_conversation_reply_delegates_the_exact_expected_revision_to_request_management(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    conversation = backend.get_conversation(_requester_context(), request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision,
        conversation_digest=conversation.conversation_digest,
        active_role="requester",
        body="Please use the governed revenue definition.",
    )

    updated = backend.append_conversation_message(_requester_context(), request.request_id, command)
    entries = stack.requests.list_conversation(_TENANT, request.request_id)

    assert len(entries) == 1
    assert entries[0].body == "Please use the governed revenue definition."
    assert updated.revision == request.revision + 1
    assert updated.conversation_digest == _conversation_digest(
        request.request_id, request.revision + 1, (entries[0].entry_id,)
    )


def test_a_stale_conversation_digest_is_a_conflict_and_never_reaches_the_owning_service(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    command = ConversationMessageCommand(
        expected_revision=request.revision,
        conversation_digest="9" * 64,
        active_role="requester",
        body="Reply written against a thread that no longer exists.",
    )

    with pytest.raises(ConsoleConflict) as failure:
        backend.append_conversation_message(_requester_context(), request.request_id, command)

    assert failure.value.recovery_action == "reload"
    assert stack.requests.list_conversation(_TENANT, request.request_id) == ()


def test_a_stale_conversation_revision_is_a_conflict_rather_than_a_denial(stack: _Stack) -> None:
    request = _question(stack)
    backend = stack.backend()
    conversation = backend.get_conversation(_requester_context(), request.request_id)
    command = ConversationMessageCommand(
        expected_revision=conversation.revision + 5,
        conversation_digest=conversation.conversation_digest,
        active_role="requester",
        body="Reply written against a future revision.",
    )

    with pytest.raises(ConsoleConflict):
        backend.append_conversation_message(_requester_context(), request.request_id, command)


def test_a_sequential_replay_under_one_idempotency_key_still_reaches_the_owning_service(
    stack: _Stack,
) -> None:
    request = _question(stack)
    backend = stack.backend()
    keys = InFlightCommandKeys()
    context = _requester_context()

    for _ in range(2):
        conversation = backend.get_conversation(context, request.request_id)
        command = ConversationMessageCommand(
            expected_revision=conversation.revision,
            conversation_digest=conversation.conversation_digest,
            active_role="requester",
            body="Repeated submission under one key.",
        )
        with keys.serialize(context, "idempotency-key-repeated"):
            backend.append_conversation_message(context, request.request_id, command)

    assert len(stack.requests.list_conversation(_TENANT, request.request_id)) == 2


def test_one_idempotency_key_serializes_concurrent_submissions_of_the_same_identity() -> None:
    keys = InFlightCommandKeys()
    context = _requester_context()
    overlaps: list[int] = []
    active = 0
    guard = threading.Lock()
    started = threading.Barrier(2)

    def submit() -> None:
        nonlocal active
        started.wait()
        with keys.serialize(context, "idempotency-key-concurrent"):
            with guard:
                active += 1
                overlaps.append(active)
            with guard:
                active -= 1

    workers = [threading.Thread(target=submit) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert overlaps == [1, 1]


def test_warehouse_confirmation_delegates_the_declared_engine_region_and_capacity(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=3,
            lifecycle_state=WarehouseBindingState.READY,
            operation_identity=None,
            failure_classification=None,
        )
    )
    backend = stack.backend(
        warehouse_commands=commands,
        warehouse_bindings=_StaticWarehouseBindingReader(_binding()),
    )
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "engine": "postgresql",
            "region": "us-west-2",
            "capacity": "mvp-fixed",
        }
    ]
    assert operation.state == "succeeded"
    assert CONSOLE_HANDLE_PATTERN.fullmatch(operation.operation_id) is not None


def test_a_confirmed_warehouse_operation_never_serializes_its_private_identity(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.VALIDATING,
            operation_identity=WarehouseOperationIdentity(
                binding_id="whb-0123456789abcdef01234567",
                operation_id=_PRIVATE_OPERATION_ID,
            ),
            failure_classification=None,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)
    serialized = operation.model_dump_json()
    record = stack.handles.load(tenant_id=_TENANT, console_handle=operation.operation_id)

    assert _PRIVATE_OPERATION_ID not in serialized
    assert "whb-0123456789abcdef01234567" not in serialized
    assert record is not None
    assert _PRIVATE_OPERATION_ID in record.private_identity


def test_a_transient_warehouse_failure_never_becomes_a_terminal_denial(stack: _Stack) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.PROVISIONING,
            operation_identity=WarehouseOperationIdentity(
                binding_id="whb-0123456789abcdef01234567",
                operation_id=_PRIVATE_OPERATION_ID,
            ),
            failure_classification=WarehouseFailureClassification.TRANSIENT_TRANSPORT,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert operation.state != "failed"
    assert operation.failure is not None
    assert operation.failure.classification == "transient"


def test_an_ambiguous_warehouse_outcome_stays_distinct_from_success_and_failure(
    stack: _Stack,
) -> None:
    commands = _RecordingWarehouseCommands(
        WarehouseConfirmation(
            binding_id="whb-0123456789abcdef01234567",
            binding_revision=2,
            lifecycle_state=WarehouseBindingState.PROVISIONING,
            operation_identity=None,
            failure_classification=WarehouseFailureClassification.AMBIGUOUS_OUTCOME,
        )
    )
    backend = stack.backend(warehouse_commands=commands)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    operation = backend.confirm_warehouse_binding(_architect_context(), command)

    assert operation.state == "outcome_unknown"
    assert operation.failure is not None
    assert operation.failure.classification == "unknown"


def test_an_unavailable_warehouse_repository_reports_retry_rather_than_a_verdict(
    stack: _Stack,
) -> None:
    backend = stack.backend(warehouse_commands=_FailingWarehouseCommands())
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.confirm_warehouse_binding(_architect_context(), command)

    assert failure.value.code == "downstream_unavailable"
    assert failure.value.recovery_action == "retry"


def test_clarified_outcome_acceptance_delegates_the_requester_principal_and_exact_digest(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest=digest(statement),
        active_role="requester",
        decision="approve",
    )

    backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "request_id": request.request_id,
            "actor_id": _REQUESTER,
            "authority_ref": _REQUESTER_PRINCIPAL,
            "subject_digest": digest(statement),
            "decision": "approve",
            "expected_revision": request.revision,
        }
    ]


def test_a_stale_clarified_outcome_digest_never_reaches_the_fulfillment_service(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest="9" * 64,
        active_role="requester",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert commands.calls == []


def test_an_unavailable_fulfillment_repository_never_becomes_a_non_conformance_verdict(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    backend = stack.backend(
        fulfillment=_StaticFulfillmentViews(request=request, statement=statement),
        fulfillment_commands=_UnavailableFulfillmentCommands(),
    )
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=request.revision,
        clarified_outcome_digest=digest(statement),
        active_role="requester",
        decision="approve",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.accept_clarified_outcome(_requester_context(), request.request_id, command)

    assert failure.value.recovery_action == "retry"


def test_an_architect_decision_delegates_the_architect_authority_and_reviewed_subject(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    subject_digest = digest(_answer_draft())
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = DecisionCommand(
        expected_revision=request.revision,
        reviewed_digest=subject_digest,
        active_role="data_architect",
        decision="approve",
    )

    detail = backend.decide_request(_architect_context(), request.request_id, command)

    assert commands.calls[0]["authority_ref"] == _ARCHITECT_PRINCIPAL
    assert commands.calls[0]["subject_digest"] == subject_digest
    assert commands.calls[0]["expected_revision"] == request.revision
    assert detail.request_id == request.request_id


def test_an_architect_decision_against_an_unheld_subject_never_reaches_the_service(
    stack: _Stack,
) -> None:
    request = _question(stack).model_copy(update={"state": RequestState.AWAITING_APPROVAL})
    statement = _statement(request)
    fulfillment = _StaticFulfillmentViews(request=request, statement=statement)
    commands = _RecordingFulfillmentCommands()
    backend = stack.backend(fulfillment=fulfillment, fulfillment_commands=commands)
    command = DecisionCommand(
        expected_revision=request.revision,
        reviewed_digest="9" * 64,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.decide_request(_architect_context(), request.request_id, command)

    assert commands.calls == []


def test_a_semantic_review_decision_delegates_the_bundles_exact_revision(stack: _Stack) -> None:
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="approve",
    )

    review = backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == [
        {
            "tenant_id": _TENANT,
            "bundle_id": bundle.bundle_id,
            "item_id": "ori-0",
            "decision": ReviewItemDecision.ACCEPT,
            "actor_id": _ARCHITECT,
            "expected_revision": bundle.revision,
            "revised_content": None,
            "merge_candidate_ids": (),
        }
    ]
    assert review.revision == bundle.revision + 1


def test_a_semantic_review_decision_against_a_stale_digest_never_reaches_the_service(
    stack: _Stack,
) -> None:
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest="9" * 64,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_a_multi_item_review_cannot_be_decided_by_a_command_that_names_no_item(
    stack: _Stack,
) -> None:
    bundle = _review_bundle(pending_items=2)
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_a_review_change_request_without_replacement_wording_is_refused(
    stack: _Stack,
) -> None:
    """The owning revise decision requires wording; the command may carry it now.

    This once read "because no revision content can be carried", which stopped being
    true when `DecisionCommand` gained the field. What remains true is that a change
    request arriving without wording has nothing to revise with.
    """
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )
    command = DecisionCommand(
        expected_revision=bundle.revision,
        reviewed_digest=bundle.candidate_set_digest,
        active_role="data_architect",
        decision="request_changes",
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(_architect_context(), bundle.bundle_id, command)

    assert commands.calls == []


def test_the_process_package_command_stays_undelivered_with_its_dependency_named(
    stack: _Stack,
) -> None:
    command = ProcessPackageCommand(
        expected_revision=1,
        package_digest="0" * 64,
        active_role="data_architect",
        file_name="revenue-to-cash.pdf",
        media_type="application/pdf",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        stack.backend().submit_process_package(_architect_context(), command)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_operation_retry_and_demo_reset_stay_undelivered_in_governed_mode(stack: _Stack) -> None:
    backend = stack.backend()
    retry = RetryOperationCommand(
        expected_revision=1,
        operation_digest="0" * 64,
        retry_token="r" * 40,
        active_role="data_architect",
    )
    reset = ResetCommand(
        expected_revision=1,
        setup_digest="0" * 64,
        reset_token="t" * 40,
        active_role="data_architect",
    )

    with pytest.raises(ConsoleUnavailable) as retry_failure:
        backend.retry_operation(_architect_context(), "op_" + "0" * 32, retry)
    with pytest.raises(ConsoleUnavailable) as reset_failure:
        backend.reset(_architect_context(), reset)

    assert retry_failure.value.code == CAPABILITY_NOT_DELIVERED
    assert reset_failure.value.code == CAPABILITY_NOT_DELIVERED


def test_a_command_never_runs_without_the_owning_service_being_wired(stack: _Stack) -> None:
    backend = stack.backend(warehouse_commands=None)
    command = WarehouseBindingCommand(
        expected_revision=1,
        reviewed_digest="0" * 64,
        active_role="data_architect",
        engine="postgresql",
        region="us-west-2",
        capacity="mvp-fixed",
    )

    with pytest.raises(ConsoleUnavailable) as failure:
        backend.confirm_warehouse_binding(_architect_context(), command)

    assert failure.value.code == CAPABILITY_NOT_DELIVERED


def test_another_tenants_request_is_not_readable_as_a_conversation(stack: _Stack) -> None:
    request = stack.requests.submit_question(
        tenant_id="tenant-other",
        requester_id=_REQUESTER,
        purpose="Other tenant purpose",
        question="Other tenant question?",
    )

    with pytest.raises(ConsoleNotFound):
        stack.backend().get_conversation(_requester_context(), request.request_id)


def test_a_named_item_decides_that_item_of_a_multi_item_review(stack: _Stack) -> None:
    """`decide_item` has always decided one item; the command can now say which.

    A bundle with several undecided items was refused outright, so an architect could
    not decide any of them through the console until every other item happened to be
    resolved elsewhere.
    """
    bundle = _review_bundle(pending_items=3)
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    backend.decide_review(
        _architect_context(),
        bundle.bundle_id,
        DecisionCommand(
            expected_revision=bundle.revision,
            reviewed_digest=bundle.candidate_set_digest,
            active_role="data_architect",
            decision="approve",
            review_item_id="ori-1",
        ),
    )

    assert [call["item_id"] for call in commands.calls] == ["ori-1"]


def test_a_change_request_delegates_the_replacement_wording(stack: _Stack) -> None:
    """`request_changes` reaches the owning revise decision now, carrying its wording.

    The console previously refused it and could not substitute `unresolved`, which
    drives the bundle to `no_valid_plan` -- a far stronger verdict than asking for a
    change.
    """
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    backend.decide_review(
        _architect_context(),
        bundle.bundle_id,
        DecisionCommand(
            expected_revision=bundle.revision,
            reviewed_digest=bundle.candidate_set_digest,
            active_role="data_architect",
            decision="request_changes",
            revised_content="net revenue excludes refunds",
        ),
    )

    assert commands.calls[0]["decision"] is ReviewItemDecision.REVISE
    assert commands.calls[0]["revised_content"] == "net revenue excludes refunds"


@pytest.mark.parametrize("named", ("ori-9", "ori-0"))
def test_an_item_that_is_not_awaiting_a_decision_is_refused(stack: _Stack, named: str) -> None:
    """Naming an absent item, or one already decided, is refused rather than resolved.

    Falling back to "the only pending one" for an unrecognised name would apply the
    decision to an item the architect did not read.
    """
    bundle = _review_bundle(pending_items=2).model_copy(
        update={
            "items": (
                _review_bundle(pending_items=2).items[0].model_copy(update={"status": "accepted"}),
                _review_bundle(pending_items=2).items[1],
            )
        }
    )
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(
            _architect_context(),
            bundle.bundle_id,
            DecisionCommand(
                expected_revision=bundle.revision,
                reviewed_digest=bundle.candidate_set_digest,
                active_role="data_architect",
                decision="approve",
                review_item_id=named,
            ),
        )

    assert commands.calls == []


def test_replacement_wording_on_an_approval_is_refused(stack: _Stack) -> None:
    """Wording only means something for a revision, so accepting it elsewhere invents one."""
    bundle = _review_bundle()
    commands = _RecordingSemanticReviewCommands(bundle)
    backend = stack.backend(
        semantic_reviews=_StaticSemanticReviewReader(bundle),
        semantic_review_commands=commands,
    )

    with pytest.raises(ConsoleInvalidRequest):
        backend.decide_review(
            _architect_context(),
            bundle.bundle_id,
            DecisionCommand(
                expected_revision=bundle.revision,
                reviewed_digest=bundle.candidate_set_digest,
                active_role="data_architect",
                decision="approve",
                revised_content="wording that approves nothing",
            ),
        )

    assert commands.calls == []
