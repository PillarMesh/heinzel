"""A governed-local console journey driven entirely through HTTP.

Nothing here asserts on the backend object. Every command is a real request against a
`governed_local` Starlette application, and every claim about what happened is checked
by reopening the owning services' own SQLite databases from disk afterwards, so a
console projection can never stand in as evidence for a transaction that did not
commit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pillarmesh_console.app import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.governed_adapters import (
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceBindingDirectory,
    InMemoryWorkspacePrincipalDirectory,
    WarehouseControlBindingReader,
    WarehouseControlLifecycleCommands,
    WarehouseRepositoryOperationReader,
)
from pillarmesh_console.governed_backend import CAPABILITY_NOT_DELIVERED, GovernedConsoleBackend
from pillarmesh_console.operation_handles import InMemoryOperationHandleRepository
from pillarmesh_contract_model import digest
from pillarmesh_request_management import (
    DataAccessRequest,
    FulfillmentPolicyCompiler,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    RequestIntakeContent,
    RequestManagementService,
    RequestState,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
    StakeholderQuestion,
)
from pillarmesh_semantic_registry import SemanticFulfillmentSnapshotAdapter
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseLifecycleOrchestrator,
    WarehouseProvisionResult,
    WarehouseRestoreVerification,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from starlette.testclient import TestClient

from tests.acceptance.run_plan3b import (
    NOW,
    ScenarioAnswerProvider,
    ScenarioAuthorityResolver,
    ScenarioFreshness,
    published_repository,
)

_TENANT = "tenant-a"
_ARCHITECT = "architect-a"
_REQUESTER = "requester-a"
_REQUESTER_PRINCIPAL = f"principal:{_REQUESTER}"
_ARCHITECT_PRINCIPAL = "role:data_engineering_architect"
_ORIGIN = "http://127.0.0.1:8000"
_PROVIDER_HANDLE_CANARY = "private://local-acceptance/credential-canary"
_UNSUPPORTED_READS = (
    "/api/v1/runs",
    "/api/v1/data-products/product-revenue",
    "/api/v1/catalog/asset-revenue",
    "/api/v1/dashboards/dashboard-revenue",
    "/api/v1/previews/preview-revenue",
    "/api/v1/links/link-revenue",
)


def _clock() -> datetime:
    return NOW


def _worker_thread_connection(database_path: str) -> sqlite3.Connection:
    """A connection a threadpool worker may use.

    Command routes run the backend in a threadpool, so a connection carrying
    SQLite's default thread affinity fails on the first request. Composing the
    application is where that choice belongs, and both owning repositories accept
    an injected connection, so this journey makes the same choice a deployment
    would. Requests here are sequential, so one thread uses it at a time.
    """
    return sqlite3.connect(database_path, check_same_thread=False)


class _LocalAcceptanceProvider:
    """The smallest provider that can carry a binding to a proven ready state.

    Its evidence is local-acceptance grade on purpose: the readiness policy below is
    the one that admits it, so this journey never claims production validation.
    """

    engine_kind = EngineKind.POSTGRESQL

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._result(binding, operation)

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._result(binding, operation)

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult:
        if resume:
            raise AssertionError("this journey never resumes a suspended binding")
        restore = WarehouseRestoreVerification(
            verification_id="wrv-console-journey",
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            engine_kind=binding.engine_kind,
            source_backup_artifact_digest="3" * 64,
            representative_data_digest="4" * 64,
            schema_metadata_digest="5" * 64,
            principal_profile_digest="6" * 64,
            integrity_marker_digest="7" * 64,
            query_behavior_digest="8" * 64,
            verified_at=_clock(),
        )
        return InitialWarehouseValidationResult(
            evidence=WarehouseValidationEvidence(
                evidence_id="wev-console-journey",
                tenant_id=binding.tenant_id,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
                engine_kind=binding.engine_kind,
                engine_version="1.0",
                engine_build_digest="9" * 64,
                engine_image_digest="a" * 64,
                principal_profile_digest=restore.principal_profile_digest,
                namespace_grant_matrix_digest="b" * 64,
                tls_probe_digest="c" * 64,
                network_isolation_probe_digest="d" * 64,
                encryption_at_rest_evidence_digest="e" * 64,
                encryption_at_rest_disposition=(
                    EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE
                ),
                positive_probe_digest="f" * 64,
                denial_probe_digest="0" * 64,
                ledger_probe_digest="1" * 64,
                monitoring_probe_digest="2" * 64,
                capacity_alert_probe_digest="3" * 64,
                backup_artifact_digest=restore.source_backup_artifact_digest,
                restore_verification_digest=digest(restore),
                restore_cleanup_digest="4" * 64,
                observed_at=_clock(),
            ),
            restore_verification=restore,
        )

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        raise AssertionError("this journey never suspends a binding")

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        raise AssertionError("this journey never resumes a binding")

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        raise AssertionError("this journey never retires a binding")

    @staticmethod
    def _result(
        binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return WarehouseProvisionResult(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=binding.engine_kind,
            private_resource_handle=_PROVIDER_HANDLE_CANARY,
            provider_build_digest="1" * 64,
            resource_inventory_digest="2" * 64,
        )


class _JourneyRoleResolver:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == _TENANT and (actor_id, authority_ref) in {
            (_REQUESTER, _REQUESTER_PRINCIPAL),
            (_ARCHITECT, _ARCHITECT_PRINCIPAL),
        }


def _context(actor: str) -> TrustedActorContext:
    if actor == _ARCHITECT:
        return TrustedActorContext(
            tenant_id=_TENANT,
            actor_id=_ARCHITECT,
            roles=("data_architect",),
            active_role="data_architect",
            session_id="session-architect",
        )
    return TrustedActorContext(
        tenant_id=_TENANT,
        actor_id=_REQUESTER,
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )


class _Journey:
    """The governed application plus direct access to the owning services.

    Earlier regression setup can use the owning services directly. The complete
    new-request journey exercises every user command through HTTP.
    """

    def __init__(self, directory: Path) -> None:
        self.warehouse_path = str(directory / "warehouse.sqlite3")
        self.request_path = str(directory / "requests.sqlite3")
        self.warehouse_connection = _worker_thread_connection(self.warehouse_path)
        self.warehouse_repository = SQLiteWarehouseRepository(connection=self.warehouse_connection)
        self.request_repository = SQLiteRequestRepository(
            _worker_thread_connection(self.request_path), _owns_connection=True
        )
        self.fulfillment_repository = SQLiteFulfillmentRepository(self.request_repository)
        self.control = WarehouseControlService(
            self.warehouse_repository,
            clock=_clock,
            readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
        )
        self.orchestrator = WarehouseLifecycleOrchestrator(
            control=self.control,
            repository=self.warehouse_repository,
            provider=_LocalAcceptanceProvider(),
            clock=_clock,
        )
        self.requests = RequestManagementService(self.request_repository, clock=_clock)
        publication_repository, receipt, integration_contract = published_repository(
            check_same_thread=False
        )
        self.publication_repository = publication_repository
        self.fulfillment = FulfillmentService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            snapshot_resolver=SemanticFulfillmentSnapshotAdapter(
                publication_repository=publication_repository,
                authority_resolver=ScenarioAuthorityResolver(
                    receipt.publication_id, integration_contract
                ),
                clock=_clock,
            ),
            answer_candidate_provider=ScenarioAnswerProvider(),
            authority_role_resolver=_JourneyRoleResolver(),
            policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=ScenarioFreshness()),
            clock=_clock,
        )
        self.reads = FulfillmentReadService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            authority_role_resolver=_JourneyRoleResolver(),
        )
        self.bindings = InMemoryWorkspaceBindingDirectory()
        principals = InMemoryWorkspacePrincipalDirectory()
        principals.bind_principal(
            tenant_id=_TENANT,
            actor_id=_REQUESTER,
            role="requester",
            principal_ref=_REQUESTER_PRINCIPAL,
        )
        principals.bind_principal(
            tenant_id=_TENANT,
            actor_id=_ARCHITECT,
            role="data_architect",
            principal_ref=_ARCHITECT_PRINCIPAL,
        )
        self.backend = GovernedConsoleBackend(
            identity=GovernedWorkspaceIdentity(
                tenant_ref="tenant-governed",
                tenant_display_name="Governed tenant",
                workspace_ref="workspace-governed",
                workspace_display_name="Governed workspace",
            ),
            operation_handles=InMemoryOperationHandleRepository(),
            warehouse_bindings=WarehouseControlBindingReader(
                service=self.control, directory=self.bindings
            ),
            warehouse_operations=WarehouseRepositoryOperationReader(self.warehouse_repository),
            requests=self.requests,
            fulfillment=self.reads,
            principals=principals,
            warehouse_commands=WarehouseControlLifecycleCommands(
                service=self.control,
                orchestrator=self.orchestrator,
                repository=self.warehouse_repository,
                directory=self.bindings,
            ),
            request_commands=self.requests,
            fulfillment_commands=self.fulfillment,
            fulfillment_preparation_commands=self.fulfillment,
            semantic_review_commands=None,
        )
        self.actor = _ARCHITECT
        self.client = TestClient(
            create_app(
                backend=self.backend,
                context_provider=lambda _: _context(self.actor),
                allowed_origin=_ORIGIN,
            )
        )
        self._csrf: dict[str, str] = {}

    def close(self) -> None:
        self.client.close()
        self.warehouse_repository.close()
        self.request_repository.close()
        self.publication_repository.close()

    def as_actor(self, actor: str) -> None:
        self.actor = actor

    def get(self, path: str) -> Any:
        return self.client.get(path)

    def post(self, path: str, payload: dict[str, Any], *, key: str) -> Any:
        if self.actor not in self._csrf:
            session = self.client.get("/api/v1/session")
            assert session.status_code == 200
            self._csrf[self.actor] = session.json()["data"]["csrf_token"]
        return self.client.post(
            path,
            json=payload,
            headers={
                "origin": _ORIGIN,
                "x-csrf-token": self._csrf[self.actor],
                "idempotency-key": key,
            },
        )


@pytest.fixture
def journey(tmp_path: Path) -> Iterator[_Journey]:
    running = _Journey(tmp_path)
    try:
        yield running
    finally:
        running.close()


def _reopened_warehouse(journey: _Journey) -> Iterator[SQLiteWarehouseRepository]:
    repository = SQLiteWarehouseRepository(journey.warehouse_path)
    try:
        yield repository
    finally:
        repository.close()


def test_a_confirmed_warehouse_binding_reaches_a_ready_state_recorded_by_its_owning_service(
    journey: _Journey,
) -> None:
    journey.as_actor(_ARCHITECT)
    setup = journey.get("/api/v1/setup").json()["data"]

    response = journey.post(
        "/api/v1/setup/warehouse-binding",
        {
            "expected_revision": setup["revision"],
            "reviewed_digest": setup["setup_digest"],
            "active_role": "data_architect",
            "engine": "postgresql",
            "region": "us-west-2",
            "capacity": "mvp-fixed",
        },
        key="warehouse-confirmation-1",
    )
    operation = response.json()["data"]
    projected = journey.get(f"/api/v1/operations/{operation['operation_id']}").json()["data"]

    assert response.status_code == 200
    assert operation["state"] == "succeeded"
    assert projected["state"] == "succeeded"
    assert _PROVIDER_HANDLE_CANARY not in response.text
    binding_id = journey.bindings.warehouse_binding_id(_TENANT)
    assert binding_id is not None
    assert binding_id not in response.text

    reopened = SQLiteWarehouseRepository(journey.warehouse_path)
    try:
        stored = reopened.load(_TENANT, binding_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.lifecycle_state is WarehouseBindingState.READY
    assert stored.provisioned_at is not None


def test_a_requester_journey_commits_every_step_to_the_owning_request_service(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)

    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "What does net revenue mean?",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key="request-intake-1",
    )
    request_id = created.json()["data"]["request_id"]
    conversation = journey.get(f"/api/v1/requests/{request_id}/conversation").json()["data"]
    replied = journey.post(
        f"/api/v1/requests/{request_id}/conversation",
        {
            "expected_revision": conversation["revision"],
            "conversation_digest": conversation["conversation_digest"],
            "active_role": "requester",
            "body": "Please use the approved governed definition.",
        },
        key="conversation-1",
    )

    assert created.status_code == 200
    assert created.headers["X-PillarMesh-Data-Provenance"] == "governed_local"
    assert replied.status_code == 200
    assert len(replied.json()["data"]["messages"]) == 1

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        stored = reopened.load(_TENANT, request_id)
        entries = reopened.list_conversation(_TENANT, request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.requester_id == _REQUESTER
    assert tuple(entry.body for entry in entries) == (
        "Please use the approved governed definition.",
    )


def test_a_stale_conversation_reply_is_refused_and_writes_nothing(journey: _Journey) -> None:
    journey.as_actor(_REQUESTER)
    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "What does net revenue mean?",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key="request-intake-stale",
    )
    request_id = created.json()["data"]["request_id"]

    refused = journey.post(
        f"/api/v1/requests/{request_id}/conversation",
        {
            "expected_revision": 1,
            "conversation_digest": "9" * 64,
            "active_role": "requester",
            "body": "A reply written against a thread that no longer exists.",
        },
        key="conversation-stale",
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["recovery_action"] == "reload"

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        entries = reopened.list_conversation(_TENANT, request_id)
    finally:
        reopened.close()

    assert entries == ()


def _question_intake_digest() -> str:
    return digest(
        RequestIntakeContent(
            title="What does net revenue mean?",
            payload=StakeholderQuestion(
                purpose="semantic definition", question="What does net revenue mean?"
            ),
        )
    )


def _submitted_question(journey: _Journey, *, key: str) -> str:
    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "What does net revenue mean?",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key=key,
    )
    assert created.status_code == 200
    request_id: str = created.json()["data"]["request_id"]
    return request_id


def _compile_proposal(journey: _Journey, request_id: str) -> FulfillmentProposal:
    request = journey.requests.get(_TENANT, request_id)
    journey.fulfillment.clarify_outcome(
        tenant_id=_TENANT,
        request_id=request_id,
        actor_id=_ARCHITECT,
        restated_request="Provide the governed definition of net revenue.",
        in_scope_summary="Approved semantic scope only.",
        out_of_scope_summary="No raw rows and no wider access.",
        expected_revision=request.revision,
    )
    investigating = journey.requests.get(_TENANT, request_id)
    proposal = journey.fulfillment.propose_answer(
        tenant_id=_TENANT,
        request_id=request_id,
        actor_id=_ARCHITECT,
        expected_revision=investigating.revision,
    )
    assert isinstance(proposal, FulfillmentProposal)
    journey.fulfillment.submit_proposal(
        tenant_id=_TENANT,
        request_id=request_id,
        actor_id=_ARCHITECT,
        expected_revision=proposal.request_revision,
    )
    return proposal


def test_a_clarified_outcome_and_an_architect_decision_reach_the_fulfillment_service(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "What does net revenue mean?",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key="request-intake-decision",
    )
    request_id = created.json()["data"]["request_id"]
    proposal = _compile_proposal(journey, request_id)
    assert {item.authority_ref for item in proposal.required_approvals} == {
        _REQUESTER_PRINCIPAL,
        _ARCHITECT_PRINCIPAL,
    }

    outcome = journey.get(f"/api/v1/requests/{request_id}/clarified-outcome").json()["data"]
    accepted = journey.post(
        f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": outcome["revision"],
            "clarified_outcome_digest": outcome["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        key="clarified-outcome-1",
    )

    journey.as_actor(_ARCHITECT)
    detail = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    decided = journey.post(
        f"/api/v1/inbox/{request_id}/decisions",
        {
            "expected_revision": detail["revision"],
            "reviewed_digest": detail["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        key="architect-decision-1",
    )

    assert accepted.status_code == 200
    assert accepted.json()["data"]["accepted"] is True
    assert detail["available_actions"] == ["approve", "reject", "request_changes"]
    assert decided.status_code == 200

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        fulfillment = SQLiteFulfillmentRepository(reopened)
        stored = reopened.load(_TENANT, request_id)
        approvals = fulfillment.list_approvals(_TENANT, request_id)
        outcomes = fulfillment.list_clarified_outcomes(_TENANT, request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.state is RequestState.AWAITING_APPROVAL
    assert {approval.authority_ref for approval in approvals} == {
        _REQUESTER_PRINCIPAL,
        _ARCHITECT_PRINCIPAL,
    }
    assert {approval.actor_id for approval in approvals} == {_REQUESTER, _ARCHITECT}
    assert {approval.decision for approval in approvals} == {"approve"}
    assert {approval.proposal_digest for approval in approvals} == {digest(proposal)}
    requester_approval = next(
        approval for approval in approvals if approval.authority_ref == _REQUESTER_PRINCIPAL
    )
    architect_approval = next(
        approval for approval in approvals if approval.authority_ref == _ARCHITECT_PRINCIPAL
    )
    assert requester_approval.subject_digest == digest(outcomes[-1])
    assert architect_approval.subject_digest == digest(proposal.subject)


def test_an_admitted_proposal_reaches_execution_through_the_owning_transaction(
    journey: _Journey,
) -> None:
    """Admission is what carries an approved proposal past `awaiting_approval`.

    Every required approval standing against the exact proposal used to be the
    terminal state a console-driven journey could reach, because the contract carried
    no admission command. The console now offers one, and the fulfillment service
    still decides whether it may be applied.
    """
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="request-intake-admission")
    proposal = _compile_proposal(journey, request_id)
    outcome = journey.get(f"/api/v1/requests/{request_id}/clarified-outcome").json()["data"]
    journey.post(
        f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": outcome["revision"],
            "clarified_outcome_digest": outcome["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        key="clarified-outcome-admission",
    )

    journey.as_actor(_ARCHITECT)
    before = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    blocked = journey.post(
        f"/api/v1/inbox/{request_id}/admission",
        {
            "expected_revision": before["revision"],
            "reviewed_digest": before["proposal_digest"],
            "active_role": "data_architect",
        },
        key="admission-before-approval",
    )
    journey.post(
        f"/api/v1/inbox/{request_id}/decisions",
        {
            "expected_revision": before["revision"],
            "reviewed_digest": before["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        key="architect-decision-admission",
    )
    ready = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    admitted = journey.post(
        f"/api/v1/inbox/{request_id}/admission",
        {
            "expected_revision": ready["revision"],
            "reviewed_digest": ready["proposal_digest"],
            "active_role": "data_architect",
        },
        key="admission-after-approval",
    )

    # The architect's own approval is outstanding until it is recorded, so the console
    # says so rather than offering a command the service would refuse.
    assert before["admission"]["available"] is False
    assert "1 of 2 required approvals" in before["admission"]["blocking_reason"]
    assert blocked.status_code == 409
    assert ready["admission"] == {"available": True, "blocking_reason": None}
    assert admitted.status_code == 200
    assert all(
        item["satisfied"] for item in admitted.json()["data"]["proposal"]["required_approvals"]
    )

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        fulfillment = SQLiteFulfillmentRepository(reopened)
        stored = reopened.load(_TENANT, request_id)
        admissions = fulfillment.list_admissions(_TENANT, request_id)
        receipts = fulfillment.list_evidence(_TENANT, request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.state is RequestState.EXECUTING
    assert len(admissions) == 1
    assert admissions[-1].proposal_digest == digest(proposal)
    assert admissions[-1].execution_status == "ready_for_execution"
    assert {receipt.outcome for receipt in receipts} == {"execution_ready"}


def test_a_revision_bump_after_the_approvals_withdraws_the_offered_admission(
    journey: _Journey,
) -> None:
    """`admit` binds each approval to the request revision it was recorded at.

    Any later command moves the revision on, and a clarification message is one an
    architect sends routinely. Matching approvals on the authority alone left the
    action advertised after that, and the service then refused with an authority
    failure, which the console reports as not-visible: a `404` on a request the
    architect is looking at.
    """
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="request-intake-revision")
    _compile_proposal(journey, request_id)
    outcome = journey.get(f"/api/v1/requests/{request_id}/clarified-outcome").json()["data"]
    journey.post(
        f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": outcome["revision"],
            "clarified_outcome_digest": outcome["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        key="clarified-outcome-revision",
    )

    journey.as_actor(_ARCHITECT)
    detail = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    journey.post(
        f"/api/v1/inbox/{request_id}/decisions",
        {
            "expected_revision": detail["revision"],
            "reviewed_digest": detail["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        key="architect-decision-revision",
    )
    approved = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    conversation = approved["conversation"]
    journey.post(
        f"/api/v1/requests/{request_id}/conversation",
        {
            "expected_revision": conversation["revision"],
            "conversation_digest": conversation["conversation_digest"],
            "active_role": "data_architect",
            "body": "One more note before admission.",
        },
        key="conversation-revision",
    )
    bumped = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    refused = journey.post(
        f"/api/v1/inbox/{request_id}/admission",
        {
            "expected_revision": bumped["revision"],
            "reviewed_digest": bumped["proposal_digest"],
            "active_role": "data_architect",
        },
        key="admission-after-bump",
    )

    assert approved["admission"] == {"available": True, "blocking_reason": None}
    assert bumped["revision"] > approved["revision"]
    assert bumped["admission"]["available"] is False
    assert all(not item["satisfied"] for item in bumped["proposal"]["required_approvals"])
    assert bumped["evidence"]["authorization_summary"].startswith("0 of ")
    # Refused as a conflict the architect can act on, never as a missing resource.
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "admission_unavailable"


def test_an_unapproved_proposal_never_reaches_the_requesters_own_projection(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "What does net revenue mean?",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key="request-intake-privacy",
    )
    request_id = created.json()["data"]["request_id"]
    proposal = _compile_proposal(journey, request_id)
    candidate = proposal.subject.answer_text  # type: ignore[union-attr]

    mine = journey.get("/api/v1/requests/mine")
    outcome = journey.get(f"/api/v1/requests/{request_id}/clarified-outcome")
    detail = journey.get(f"/api/v1/inbox/{request_id}")

    assert candidate not in mine.text
    assert candidate not in outcome.text
    # The inbox is an architect surface; a requester must not reach it at all.
    assert detail.status_code == 404


def test_every_unsupported_capability_reports_not_delivered_rather_than_a_substitute(
    journey: _Journey,
) -> None:
    journey.as_actor(_ARCHITECT)

    package = journey.post(
        "/api/v1/setup/process-packages",
        {
            "expected_revision": 1,
            "package_digest": "0" * 64,
            "active_role": "data_architect",
            "file_name": "revenue-to-cash.pdf",
            "media_type": "application/pdf",
        },
        key="process-package-1",
    )
    retry = journey.post(
        "/api/v1/operations/op_" + "0" * 32 + "/retry",
        {
            "expected_revision": 1,
            "operation_digest": "0" * 64,
            "retry_token": "r" * 40,
            "active_role": "data_architect",
        },
        key="retry-operation-1",
    )
    reset = journey.post(
        "/api/v1/demo/reset",
        {
            "expected_revision": 1,
            "setup_digest": "0" * 64,
            "reset_token": "t" * 40,
            "active_role": "data_architect",
        },
        key="reset-attempt-1",
    )
    reads = {path: journey.get(path) for path in _UNSUPPORTED_READS}
    capabilities = {
        item["capability_id"]: item["state"]
        for item in journey.get("/api/v1/workspace").json()["data"]["capabilities"]
    }

    assert package.status_code == 503
    assert package.json()["error"]["code"] == CAPABILITY_NOT_DELIVERED
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == CAPABILITY_NOT_DELIVERED
    # The demo reset route exists only for the fixture backend.
    assert reset.status_code == 404
    for path, response in reads.items():
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == CAPABILITY_NOT_DELIVERED, path
    assert capabilities["process-package"] == "not_delivered"
    assert capabilities["operation-retry"] == "not_delivered"
    assert capabilities["analyst-dashboard"] == "not_delivered"
    assert capabilities["source-acquisition"] == "not_delivered"
    assert capabilities["data-product-runs"] == "not_delivered"
    assert capabilities["request-conversation"] == "ready"


def test_a_repeated_idempotency_key_does_not_make_the_console_a_replay_authority(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    payload = {
        "expected_revision": 1,
        "request_digest": _question_intake_digest(),
        "active_role": "requester",
        "title": "What does net revenue mean?",
        "request": {
            "kind": "stakeholder_question",
            "purpose": "semantic definition",
            "question": "What does net revenue mean?",
        },
    }

    first = journey.post("/api/v1/requests", payload, key="repeated-key")
    second = journey.post("/api/v1/requests", payload, key="repeated-key")

    assert first.status_code == 200
    assert second.status_code == 200
    # The owning service minted two identities, which is exactly what a console that
    # is not a replay authority must let happen.
    assert first.json()["data"]["request_id"] != second.json()["data"]["request_id"]

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        inbox = reopened.list_inbox(_TENANT)
    finally:
        reopened.close()

    assert len(inbox) == 2


def test_another_tenant_can_never_reach_this_workspaces_request(journey: _Journey) -> None:
    foreign = journey.requests.submit_question(
        tenant_id="tenant-b",
        requester_id=_REQUESTER,
        purpose="semantic definition",
        question="What does net revenue mean?",
    )
    journey.as_actor(_REQUESTER)

    conversation = journey.get(f"/api/v1/requests/{foreign.request_id}/conversation")

    assert conversation.status_code == 404


def test_a_transient_owning_failure_reports_retry_rather_than_a_terminal_verdict(
    journey: _Journey,
) -> None:
    journey.as_actor(_ARCHITECT)
    setup = journey.get("/api/v1/setup").json()["data"]
    # The journey owns this connection, so the journey closes it: a borrowed
    # connection is not the repository's to close.
    journey.warehouse_connection.close()

    response = journey.post(
        "/api/v1/setup/warehouse-binding",
        {
            "expected_revision": setup["revision"],
            "reviewed_digest": setup["setup_digest"],
            "active_role": "data_architect",
            "engine": "postgresql",
            "region": "us-west-2",
            "capacity": "mvp-fixed",
        },
        key="warehouse-confirmation-unavailable",
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "downstream_unavailable"
    assert response.json()["error"]["recovery_action"] == "retry"


def test_the_expiry_of_a_data_access_request_reaches_the_owning_service_unchanged(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    expires_at = NOW + timedelta(days=3)

    created = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": digest(
                RequestIntakeContent(
                    title="Finance export",
                    payload=DataAccessRequest(
                        purpose="finance access",
                        data_product_id="product-revenue",
                        requested_fields=("order_total", "customer_id"),
                        access_mode="export",
                        expires_at=expires_at,
                    ),
                )
            ),
            "active_role": "requester",
            "title": "Finance export",
            "request": {
                "kind": "data_access",
                "purpose": "finance access",
                "data_product_ref": "product-revenue",
                "requested_fields": ["order_total", "customer_id"],
                "access_mode": "export",
                "expires_at": expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            },
        },
        key="access-intake-1",
    )
    request_id = created.json()["data"]["request_id"]

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        stored = reopened.load(_TENANT, request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.payload.request_type == "data_access"
    assert stored.payload.expires_at == expires_at
    assert stored.payload.requested_fields == ("order_total", "customer_id")


def test_a_tampered_intake_is_refused_before_any_durable_request_is_created(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)

    response = journey.post(
        "/api/v1/requests",
        {
            "expected_revision": 1,
            "request_digest": _question_intake_digest(),
            "active_role": "requester",
            "title": "Changed after hashing",
            "request": {
                "kind": "stakeholder_question",
                "purpose": "semantic definition",
                "question": "What does net revenue mean?",
            },
        },
        key="request-intake-tampered",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_digest_mismatch"
    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        assert reopened.list_inbox(_TENANT) == ()
        assert reopened.peek_next_sequence(_TENANT) == 1
    finally:
        reopened.close()


def test_conversation_roles_follow_trusted_http_authors_and_survive_database_reopen(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="role-request-intake")
    for actor, role in ((_REQUESTER, "requester"), (_ARCHITECT, "data_architect")):
        journey.as_actor(actor)
        conversation = journey.get(f"/api/v1/requests/{request_id}/conversation").json()["data"]
        response = journey.post(
            f"/api/v1/requests/{request_id}/conversation",
            {
                "expected_revision": conversation["revision"],
                "conversation_digest": conversation["conversation_digest"],
                "active_role": role,
                "body": "A contribution with recorded provenance.",
            },
            key=f"role-reply-{role}",
        )
        assert response.status_code == 200
        assert response.json()["data"]["messages"][-1]["author_role"] == role

    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        entries = reopened.list_conversation(_TENANT, request_id)
        assert tuple(entry.author_role for entry in entries) == ("requester", "data_architect")
        assert tuple(entry.actor_id for entry in entries) == (_REQUESTER, _ARCHITECT)
        assert tuple(entry.request_revision for entry in entries) == (2, 3)
    finally:
        reopened.close()


def test_architect_reviews_the_owning_answer_and_artifacts_over_http(journey: _Journey) -> None:
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="reviewable-answer")
    proposal = _compile_proposal(journey, request_id)
    assert isinstance(proposal.subject, StakeholderAnswerDraft)
    journey.as_actor(_ARCHITECT)

    response = journey.get(f"/api/v1/inbox/{request_id}")

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["proposal"]["candidate"] == proposal.subject.answer_text
    assert [item["artifact_reference"] for item in detail["proposal"]["datasets"]] == [
        reference.model_dump(mode="json") for reference in proposal.subject.governed_dataset_refs
    ]
    assert detail["proposal"]["required_approvals"]
    assert all(not item["satisfied"] for item in detail["proposal"]["required_approvals"])


def test_a_new_request_reaches_admission_using_only_console_http_commands(
    journey: _Journey,
) -> None:
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="complete-ui-intake")
    journey.as_actor(_ARCHITECT)
    clarified = journey.post(
        f"/api/v1/inbox/{request_id}/clarification",
        {
            "expected_revision": 1,
            "active_role": "data_architect",
            "restated_request": "Provide the governed definition of net revenue.",
            "in_scope_summary": "Approved semantic scope only.",
            "out_of_scope_summary": "No raw rows or wider access.",
        },
        key="complete-ui-clarification",
    )
    assert clarified.status_code == 200
    prepared = journey.post(
        f"/api/v1/inbox/{request_id}/proposal",
        {
            "expected_revision": clarified.json()["data"]["revision"],
            "active_role": "data_architect",
        },
        key="complete-ui-prepare",
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["data"]["state"] == "proposed"
    assert prepared.json()["data"]["available_actions"] == []
    submitted = journey.post(
        f"/api/v1/inbox/{request_id}/proposal/submission",
        {"expected_revision": prepared.json()["data"]["revision"], "active_role": "data_architect"},
        key="complete-ui-submit",
    )
    assert submitted.status_code == 200
    assert submitted.json()["data"]["state"] == "awaiting_approval"
    journey.as_actor(_REQUESTER)
    outcome = journey.get(f"/api/v1/requests/{request_id}/clarified-outcome").json()["data"]
    accepted = journey.post(
        f"/api/v1/requests/{request_id}/clarified-outcome/acceptance",
        {
            "expected_revision": outcome["revision"],
            "clarified_outcome_digest": outcome["statement_digest"],
            "active_role": "requester",
            "decision": "approve",
        },
        key="complete-ui-accept",
    )
    assert accepted.status_code == 200
    journey.as_actor(_ARCHITECT)
    detail = journey.get(f"/api/v1/inbox/{request_id}").json()["data"]
    approved = journey.post(
        f"/api/v1/inbox/{request_id}/decisions",
        {
            "expected_revision": detail["revision"],
            "reviewed_digest": detail["proposal_digest"],
            "active_role": "data_architect",
            "decision": "approve",
        },
        key="complete-ui-approve",
    )
    assert approved.status_code == 200
    admitted = journey.post(
        f"/api/v1/inbox/{request_id}/admission",
        {
            "expected_revision": approved.json()["data"]["revision"],
            "reviewed_digest": approved.json()["data"]["proposal_digest"],
            "active_role": "data_architect",
        },
        key="complete-ui-admit",
    )
    assert admitted.status_code == 200
    reopened = SQLiteRequestRepository.open(journey.request_path)
    try:
        stored = reopened.load(_TENANT, request_id)
        assert stored is not None and stored.state is RequestState.EXECUTING
        fulfillment = SQLiteFulfillmentRepository(reopened)
        assert len(fulfillment.list_admissions(_TENANT, request_id)) == 1
        assert len(fulfillment.list_evidence(_TENANT, request_id)) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("actor", "patch", "status"),
    [
        (_REQUESTER, {}, 404),
        (_ARCHITECT, {"active_role": "requester"}, 404),
        (_ARCHITECT, {"expected_revision": 99}, 409),
        (_ARCHITECT, {"restated_request": "x" * 4001}, 422),
        (_ARCHITECT, {"in_scope_summary": ""}, 422),
        (_ARCHITECT, {"unexpected": True}, 422),
    ],
)
def test_preparation_refusals_leave_the_request_and_clarification_unchanged(
    journey: _Journey, actor: str, patch: dict[str, Any], status: int
) -> None:
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="refused-preparation-intake")
    journey.as_actor(actor)

    response = journey.post(
        f"/api/v1/inbox/{request_id}/clarification",
        {
            "expected_revision": 1,
            "active_role": "data_architect",
            "restated_request": "Define net revenue.",
            "in_scope_summary": "Governed definition.",
            "out_of_scope_summary": "Raw rows.",
            **patch,
        },
        key="refused-preparation-command",
    )

    assert response.status_code == status
    request = journey.requests.get(_TENANT, request_id)
    assert request.state is RequestState.SUBMITTED and request.revision == 1
    assert journey.fulfillment_repository.list_clarified_outcomes(_TENANT, request_id) == ()


@pytest.mark.parametrize("path", ["proposal", "proposal/submission"])
def test_preparing_or_submitting_without_clarification_is_refused(
    journey: _Journey, path: str
) -> None:
    journey.as_actor(_REQUESTER)
    request_id = _submitted_question(journey, key="unclarified-intake")
    journey.as_actor(_ARCHITECT)

    response = journey.post(
        f"/api/v1/inbox/{request_id}/{path}",
        {"expected_revision": 1, "active_role": "data_architect"},
        key="unclarified-command",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preparation_unavailable"
    assert journey.requests.get(_TENANT, request_id).revision == 1
    assert journey.fulfillment_repository.list_proposals(_TENANT, request_id) == ()


def test_missing_governed_data_exposes_the_preparation_dependency(journey: _Journey) -> None:
    request = journey.requests.submit_question(
        tenant_id=_TENANT,
        requester_id=_REQUESTER,
        purpose="missing governed data",
        question="What is net revenue?",
    )
    journey.as_actor(_ARCHITECT)
    clarified = journey.post(
        f"/api/v1/inbox/{request.request_id}/clarification",
        {
            "expected_revision": 1,
            "active_role": "data_architect",
            "restated_request": "Net revenue",
            "in_scope_summary": "Current governed data",
            "out_of_scope_summary": "Unapproved data",
        },
        key="dependency-clarify",
    )
    response = journey.post(
        f"/api/v1/inbox/{request.request_id}/proposal",
        {
            "expected_revision": clarified.json()["data"]["revision"],
            "active_role": "data_architect",
        },
        key="dependency-prepare",
    )

    assert response.status_code == 200
    assert response.json()["data"]["proposal"] is None
    assert response.json()["data"]["preparation_actions"] == []
    assert "data_product_change" in " ".join(response.json()["data"]["preparation_notes"])
    assert len(journey.fulfillment_repository.list_dependencies(_TENANT, request.request_id)) == 1
