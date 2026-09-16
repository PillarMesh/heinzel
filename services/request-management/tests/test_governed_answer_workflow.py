from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_compiler import GovernedQueryPlan
from pillarmesh_compiler.query_repository import SQLiteQueryPlanRepository
from pillarmesh_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier
from pillarmesh_contract_model import ArtifactReference, canonical_bytes, digest
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_request_management import (
    AnswerDeliveryAuthorization,
    ExecuteGovernedAnswerWorkflowCommand,
    GovernedAnswerDeliveryAuthorizationUnavailable,
    GovernedAnswerExecutionAuthorizationDenied,
    GovernedAnswerExecutionAuthorizationUnavailable,
    GovernedAnswerExecutionDeferred,
    GovernedAnswerExecutionFailed,
    GovernedAnswerNotVisible,
    GovernedAnswerService,
    GovernedAnswerStaleRevision,
    GovernedAnswerWorkflow,
    InboxRequest,
    PolicyAdmissionExecutionAuthorizer,
    PolicyAdmissionReceipt,
    RepositoryAnswerExecutionReader,
    RepositoryAnswerPlanReader,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteGovernedAnswerRepository,
    SQLitePolicyAdmissionEvidenceReader,
    SQLiteRequestRepository,
    StakeholderQuestion,
    WorkflowIncidentProjectionDeferred,
    WorkflowIncidentProjector,
)
from pillarmesh_request_management.answer_admission import PolicyScanReservation
from pillarmesh_runtime import (
    AnswerExecutionAuthorization,
    AnswerExecutionIncidentProjector,
    AnswerExecutionReceipt,
    AnswerProductGenerationReference,
    AnswerQueryColumn,
    AnswerQueryCursor,
    QueryGenerationState,
    QueryResultNotFound,
    ReadOnlyAnswerQuery,
    SQLiteAnswerResultStore,
)
from pillarmesh_runtime.answer_models import AnswerQueryValue
from pillarmesh_runtime.query_execution import GovernedQueryExecutor
from pillarmesh_state import SQLiteIncidentRepository

NOW = datetime(2026, 9, 12, 8, tzinfo=UTC)
TENANT = "tenant-a"
REQUEST = "request-1"
VALIDATION_DIGEST = "b" * 64
POLICY_DIGEST = "6" * 64
ENTITLEMENT_DIGEST = "7" * 64


def _signed_plan(key: Ed25519PrivateKey) -> GovernedQueryPlan:
    generation = {
        "product_ref": {
            "artifact_id": "product-revenue",
            "version": 4,
            "digest": "a" * 64,
        },
        "generation": 7,
    }
    statement = (
        'SELECT "source"."region", SUM("source"."revenue") '
        'FROM "consumption"."sales" AS "source" GROUP BY "source"."region" '
        'HAVING COUNT(DISTINCT "source"."customer_id") >= %s LIMIT 10'
    )
    parameters = ({"name": "p0", "value_type": "integer", "value": 5},)
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "query-plan-1",
        "tenant_id": TENANT,
        "validation_digest": VALIDATION_DIGEST,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (
            {"artifact_id": "consumption-1", "version": 2, "digest": "c" * 64},
        ),
        "product_generation_refs": (generation,),
        "minimum_group_size": 5,
        "statement": statement,
        "parameters": parameters,
        "statement_digest": digest(statement),
        "parameter_digest": digest(parameters),
        "estimated_scan": {"rows": 10, "bytes": 500, "estimator_version": "pg-1"},
        "ceilings": {
            "row_limit": 10,
            "scan": {"rows": 100, "bytes": 10_000},
            "period_scan": {"rows": 1_000, "bytes": 100_000},
        },
        "routing": "policy_admitted",
    }
    plan_digest = digest(body)
    return GovernedQueryPlan.model_validate(
        {
            **body,
            "plan_digest": plan_digest,
            "signature": QueryPlanSigner("compiler-1", key).sign(plan_digest),
        }
    )


class _ExecutionAuthorizer:
    def __init__(self, *failures: Exception) -> None:
        self._failures = list(failures)

    def recheck(
        self, *, tenant_id: str, request_id: str, validation_digest: str, plan_digest: str
    ) -> AnswerExecutionAuthorization:
        if self._failures:
            raise self._failures.pop(0)
        return AnswerExecutionAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
            plan_digest=plan_digest,
            policy_revision=3,
            entitlement_digest=ENTITLEMENT_DIGEST,
            row_ceiling=10,
            byte_ceiling=10_000,
            statement_timeout_seconds=15,
            result_retention_seconds=3600,
            freshness_observation_ref="freshness-1",
            quality_observation_ref="quality-1",
        )


class _Generations:
    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState:
        return QueryGenerationState(addressable=True)


class _Cursor:
    columns: tuple[AnswerQueryColumn, ...] = (
        AnswerQueryColumn(name="region", value_type="string"),
        AnswerQueryColumn(name="revenue", value_type="decimal"),
    )
    suppressed_group_count = 0

    def __init__(self) -> None:
        self._rows = iter((("West", Decimal("12.50")),))

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None:
        return next(self._rows, None)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Provider:
    engine_kind: Literal["postgresql"] = "postgresql"

    def __init__(self, failure: ProviderError | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return _Cursor()


class _IncidentProjector:
    def __init__(self) -> None:
        self.receipts: list[AnswerExecutionReceipt] = []

    def project(self, receipt: AnswerExecutionReceipt) -> None:
        self.receipts.append(receipt)


class _LostIncidentProjectionResponse:
    def __init__(self, delegate: AnswerExecutionIncidentProjector) -> None:
        self._delegate = delegate
        self.receipts: list[AnswerExecutionReceipt] = []

    def project(self, receipt: AnswerExecutionReceipt) -> object | None:
        self.receipts.append(receipt)
        projected = self._delegate.project(receipt)
        if len(self.receipts) == 1:
            raise WorkflowIncidentProjectionDeferred("incident projection response was lost")
        return projected


class _FailingIncidentProjector:
    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    def project(self, receipt: AnswerExecutionReceipt) -> None:
        del receipt
        raise self._failure


class _DeliveryAuthorizer:
    def __init__(
        self, plan_digest: str, *, current: bool = True, failures: tuple[Exception, ...] = ()
    ) -> None:
        self._plan_digest = plan_digest
        self._current = current
        self._failures = list(failures)

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> AnswerDeliveryAuthorization:
        if self._failures:
            raise self._failures.pop(0)
        return AnswerDeliveryAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            requester_id=requester_id,
            request_revision=4,
            plan_digest=self._plan_digest,
            policy_id="policy-1",
            policy_revision=3,
            policy_snapshot_digest=POLICY_DIGEST,
            entitlement_snapshot_digest=ENTITLEMENT_DIGEST,
            policy_current=self._current,
            entitlement_current=self._current,
            valid_until=NOW + timedelta(minutes=30),
            row_ceiling=10,
            byte_ceiling=10_000,
            minimum_group_size=5,
            freshness_observation_ref="freshness-1",
            quality_observation_ref="quality-1",
            freshness_disposition="current",
            metric_version_refs=(
                ArtifactReference(artifact_id="metric-revenue", version=1, digest="8" * 64),
            ),
            material_quality_limitations=(),
            lineage_refs=(),
            as_of=NOW,
            restatement="Revenue by region.",
            approved_narrative_terms=("revenue", "region"),
        )


def _workflow(
    *,
    provider: _Provider,
    current_authority: bool = True,
    execution_authority_failures: tuple[Exception, ...] = (),
    delivery_authority_failures: tuple[Exception, ...] = (),
    incident_projector: WorkflowIncidentProjector | None = None,
) -> tuple[
    GovernedAnswerWorkflow,
    RequestManagementService,
    SQLiteGovernedAnswerRepository,
    SQLiteAnswerResultStore,
    GovernedQueryPlan,
]:
    key = Ed25519PrivateKey.generate()
    plan = _signed_plan(key)
    plan_repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    plan_repository.save(plan)
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    requests.save(
        InboxRequest(
            request_id=REQUEST,
            tenant_id=TENANT,
            requester_id="requester-a",
            payload=StakeholderQuestion(purpose="monthly close", question="What is revenue?"),
            state=RequestState.SUBMITTED,
            revision=1,
            submitted_at=NOW - timedelta(minutes=10),
            updated_at=NOW - timedelta(minutes=10),
        )
    )
    investigating = request_service.transition(
        TENANT,
        REQUEST,
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=1,
    )
    admissions = SQLiteAnswerAdmissionRepository(requests)
    admissions.record_and_transition(
        PolicyAdmissionReceipt(
            admission_id="admission-1",
            tenant_id=TENANT,
            request_id=REQUEST,
            request_revision=investigating.revision,
            validation_digest=VALIDATION_DIGEST,
            restatement_acceptance_ref="restatement-1",
            plan_digest=plan.plan_digest,
            policy_id="policy-1",
            policy_revision=3,
            policy_digest=POLICY_DIGEST,
            entitlement_snapshot_digest=ENTITLEMENT_DIGEST,
            period_scan_consumed=None,
            created_at=NOW - timedelta(minutes=5),
        ),
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation.for_calendar_month(
            reserved_scan=10_000,
            period_budget=100_000,
            at=NOW - timedelta(minutes=5),
        ),
    )
    results = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    executor = GovernedQueryExecutor(
        store=results,
        signature_verifier=QueryPlanVerifier({"compiler-1": key.public_key()}),
        authorizer=PolicyAdmissionExecutionAuthorizer(
            admissions, _ExecutionAuthorizer(*execution_authority_failures)
        ),
        generation_reader=_Generations(),
        provider_resolver=lambda engine_kind: provider,
        clock=lambda: NOW,
        sleeper=lambda delay: None,
    )
    answers = SQLiteGovernedAnswerRepository(requests)
    delivery = GovernedAnswerService(
        repository=answers,
        admission_reader=SQLitePolicyAdmissionEvidenceReader(admissions, requests),
        execution_reader=RepositoryAnswerExecutionReader(
            results, missing_result_error=QueryResultNotFound
        ),
        plan_reader=RepositoryAnswerPlanReader(plan_repository),
        authorization_rechecker=_DeliveryAuthorizer(
            plan.plan_digest,
            current=current_authority,
            failures=delivery_authority_failures,
        ),
        clock=lambda: NOW,
    )
    workflow = GovernedAnswerWorkflow(
        requests=request_service,
        admissions=admissions,
        plans=plan_repository,
        executions=results,
        executor=executor,
        incident_projector=incident_projector or _IncidentProjector(),
        delivery=delivery,
    )
    return workflow, request_service, answers, results, plan


def _command() -> ExecuteGovernedAnswerWorkflowCommand:
    return ExecuteGovernedAnswerWorkflowCommand(
        tenant_id=TENANT,
        request_id=REQUEST,
        expected_revision=3,
        requester_id="requester-a",
        actor_id="system:answer-runtime",
        admission_ref="admission-1",
        model_narrative="For West, revenue was 12.50.",
    )


def test_real_signed_plan_executes_once_and_delivers_through_exact_state_lineage() -> None:
    provider = _Provider()
    workflow, requests, answers, _, _ = _workflow(provider=provider)

    first = workflow.execute(_command())
    replay = workflow.execute(_command())

    assert canonical_bytes(replay) == canonical_bytes(first)
    assert provider.calls == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.DELIVERED
    assert tuple(
        (event.from_state, event.to_state)
        for event in requests.list_transition_history(TENANT, REQUEST)[-3:]
    ) == (
        (RequestState.INVESTIGATING, RequestState.EXECUTING),
        (RequestState.EXECUTING, RequestState.VERIFYING),
        (RequestState.VERIFYING, RequestState.DELIVERED),
    )
    assert answers.list_for_request(TENANT, REQUEST) == (first,)


def test_terminal_runtime_failure_records_failed_once_without_an_answer() -> None:
    provider = _Provider(ProviderError("rejected", "statement_rejected"))
    workflow, requests, answers, results, _ = _workflow(provider=provider)

    for _ in range(2):
        with pytest.raises(GovernedAnswerExecutionFailed, match="execution failed"):
            workflow.execute(_command())

    assert provider.calls == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.FAILED
    assert len(requests.list_transition_history(TENANT, REQUEST)) == 3
    assert answers.list_for_request(TENANT, REQUEST) == ()
    assert results.load_execution(TENANT, REQUEST) is not None


def test_transient_retry_exhaustion_projects_exactly_one_incident(tmp_path: Path) -> None:
    provider = _Provider(ProviderError("unavailable", "transient_unavailable"))
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = AnswerExecutionIncidentProjector(incidents)
    workflow, requests, _, results, _ = _workflow(
        provider=provider,
        incident_projector=projector,
    )

    with pytest.raises(GovernedAnswerExecutionFailed, match="execution failed"):
        workflow.execute(_command())

    receipt = results.load_execution(TENANT, REQUEST)
    assert receipt is not None
    assert receipt[1].attempt == 3
    assert provider.calls == 3
    assert requests.get(TENANT, REQUEST).state is RequestState.FAILED
    projected = incidents.list_current(TENANT)
    assert len(projected) == 1
    assert projected[0].source_record_ref == receipt[1].receipt_id


def test_successful_execution_is_not_sent_to_incident_projection() -> None:
    provider = _Provider()
    projector = _IncidentProjector()
    workflow, _, _, _, _ = _workflow(provider=provider, incident_projector=projector)

    workflow.execute(_command())

    assert projector.receipts == []


def test_incident_projection_failure_defers_transition_and_reuses_execution_receipt(
    tmp_path: Path,
) -> None:
    provider = _Provider(ProviderError("rejected", "statement_rejected"))
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    projector = _LostIncidentProjectionResponse(AnswerExecutionIncidentProjector(incidents))
    workflow, requests, _, results, _ = _workflow(
        provider=provider,
        incident_projector=projector,
    )

    with pytest.raises(GovernedAnswerExecutionDeferred, match="incident projection unavailable"):
        workflow.execute(_command())
    persisted = results.load_execution(TENANT, REQUEST)
    assert persisted is not None
    assert requests.get(TENANT, REQUEST).state is RequestState.EXECUTING

    with pytest.raises(GovernedAnswerExecutionFailed, match="execution failed"):
        workflow.execute(_command())

    assert provider.calls == 1
    assert len(projector.receipts) == 2
    assert canonical_bytes(projector.receipts[0]) == canonical_bytes(projector.receipts[1])
    assert len(incidents.list_current(TENANT)) == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.FAILED


@pytest.mark.parametrize("failure", (ValueError("invalid incident"), AssertionError("bug")))
def test_incident_projection_programming_or_integrity_failure_is_not_flattened(
    failure: Exception,
) -> None:
    provider = _Provider(ProviderError("rejected", "statement_rejected"))
    workflow, requests, _, _, _ = _workflow(
        provider=provider,
        incident_projector=_FailingIncidentProjector(failure),
    )

    with pytest.raises(type(failure), match=str(failure)):
        workflow.execute(_command())

    assert requests.get(TENANT, REQUEST).state is RequestState.EXECUTING


def test_transient_authority_outage_remains_retryable_without_a_false_terminal_receipt() -> None:
    provider = _Provider()
    workflow, requests, _, results, _ = _workflow(
        provider=provider,
        execution_authority_failures=(GovernedAnswerExecutionAuthorizationUnavailable(),),
    )

    with pytest.raises(GovernedAnswerExecutionDeferred, match="authorization unavailable"):
        workflow.execute(_command())
    answer = workflow.execute(_command())

    assert answer.request_id == REQUEST
    assert provider.calls == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.DELIVERED
    assert results.load_execution(TENANT, REQUEST) is not None


def test_revoked_authority_records_failed_once_without_executing() -> None:
    provider = _Provider()
    workflow, requests, answers, results, _ = _workflow(
        provider=provider,
        execution_authority_failures=(GovernedAnswerExecutionAuthorizationDenied(),),
    )

    for _ in range(2):
        with pytest.raises(GovernedAnswerExecutionFailed, match="authorization failed"):
            workflow.execute(_command())

    assert provider.calls == 0
    assert requests.get(TENANT, REQUEST).state is RequestState.FAILED
    assert len(requests.list_transition_history(TENANT, REQUEST)) == 3
    assert answers.list_for_request(TENANT, REQUEST) == ()
    assert results.load_execution(TENANT, REQUEST) is None


def test_verification_failure_records_failed_without_disclosing_an_answer() -> None:
    provider = _Provider()
    workflow, requests, answers, _, _ = _workflow(provider=provider, current_authority=False)

    with pytest.raises(GovernedAnswerExecutionFailed, match="verification failed"):
        workflow.execute(_command())
    with pytest.raises(GovernedAnswerExecutionFailed, match="verification failed"):
        workflow.execute(_command())

    assert provider.calls == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.FAILED
    assert len(requests.list_transition_history(TENANT, REQUEST)) == 4
    assert answers.list_for_request(TENANT, REQUEST) == ()


def test_delivery_authority_outage_keeps_verification_retryable() -> None:
    provider = _Provider()
    workflow, requests, _, _, _ = _workflow(
        provider=provider,
        delivery_authority_failures=(GovernedAnswerDeliveryAuthorizationUnavailable(),),
    )

    with pytest.raises(GovernedAnswerDeliveryAuthorizationUnavailable):
        workflow.execute(_command())
    assert requests.get(TENANT, REQUEST).state is RequestState.VERIFYING

    answer = workflow.execute(_command())

    assert answer.request_id == REQUEST
    assert provider.calls == 1
    assert requests.get(TENANT, REQUEST).state is RequestState.DELIVERED


def test_stale_or_cross_tenant_request_is_denied_before_execution() -> None:
    provider = _Provider()
    workflow, requests, _, _, _ = _workflow(provider=provider)
    requests.transition(
        TENANT,
        REQUEST,
        RequestState.FAILED,
        actor_id="other-runtime",
        expected_revision=3,
    )

    with pytest.raises(GovernedAnswerStaleRevision):
        workflow.execute(_command())
    with pytest.raises(GovernedAnswerNotVisible):
        workflow.execute(_command().model_copy(update={"tenant_id": "tenant-b"}))

    assert provider.calls == 0
