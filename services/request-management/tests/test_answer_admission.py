from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from heinzel_contract_model import ArtifactModel, ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    AnswerIntentValidation,
    AnswerPolicyAdmissionService,
    AnswerQuestionIntent,
    AnswerScopePolicy,
    InboxRequest,
    PolicyAdmissionReceipt,
    PolicyAdmissionResult,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteRequestRepository,
    StakeholderQuestion,
)
from heinzel_request_management.answer_admission import (
    PolicyAdmissionBudgetExceeded,
    PolicyScanReservation,
)
from heinzel_request_management.answer_models import AnswerProductGenerationReference
from heinzel_request_management.answer_usage import (
    AnswerExecutionUsageEvidence,
    AnswerPlanUsageEvidence,
    AnswerPolicyUsageUnavailable,
    DurableStatementCeilingBreachReader,
)
from heinzel_request_management.repository import StaleRevisionError

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
DIGEST = "a" * 64
PERIOD_START = datetime(2026, 9, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 10, 1, tzinfo=UTC)


class _NoExecutionUsage:
    def list_for_tenant(self, *, tenant_id: str) -> tuple[AnswerExecutionUsageEvidence, ...]:
        return ()


class _NoPlanUsage:
    def read(self, *, tenant_id: str, plan_digest: str) -> AnswerPlanUsageEvidence | None:
        raise AssertionError("invalid admission authority must stop before plan usage")


def _reference(identifier: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=1, digest=DIGEST)


def _intent(kind: Literal["definition", "metric_value"] = "metric_value") -> AnswerQuestionIntent:
    return AnswerQuestionIntent(
        intent_id="intent-1",
        tenant_id="tenant-1",
        request_id="request-1",
        request_revision=2,
        question_digest=digest(
            StakeholderQuestion(purpose="operations", question="What is revenue?")
        ),
        intent_kind=kind,
        metric_refs=("revenue",),
        dimension_refs=(),
        filters=(),
        time_window=None,
        ordering=(),
        row_limit=10,
        interpreter="model",
        interpreter_ref="scripted-v1",
        created_at=NOW,
    )


def _policy(**changes: object) -> AnswerScopePolicy:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "tenant_id": "tenant-1",
        "revision": 1,
        "prior_policy_digest": None,
        "principal_scope": ("principal-1",),
        "purposes": ("operations",),
        "semantic_version_ref": _reference("semantic"),
        "data_product_version_refs": (_reference("product"),),
        "metric_version_refs": (_reference("metric"),),
        "dimension_refs": (),
        "filter_domains": (),
        "max_time_window": 3600,
        "max_staleness": 300,
        "quality_disposition": "block",
        "disclosure_classifications": (),
        "disclosure_entity": "customer",
        "minimum_group_size": 1,
        "restatement_confirmation": "model_interpreted",
        "row_ceiling": 100,
        "byte_ceiling": 10_000,
        "scan_ceiling": 1_000,
        "period_scan_budget": 10_000,
        "statement_timeout": 30,
        "result_retention": 3600,
        "agent_access": "allowed",
        "model_disclosure": "metadata",
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "created_at": NOW - timedelta(days=1),
        "approval_ids": ("approval-1",),
    }
    values.update(changes)
    return AnswerScopePolicy.model_validate(values)


def _validation(
    intent: AnswerQuestionIntent,
    policy: AnswerScopePolicy,
    **changes: object,
) -> AnswerIntentValidation:
    values: dict[str, object] = {
        "validation_id": "validation-1",
        "tenant_id": intent.tenant_id,
        "request_id": intent.request_id,
        "request_revision": intent.request_revision,
        "intent_digest": digest(intent),
        "semantic_version_digest": policy.semantic_version_ref.digest,
        "policy_id": policy.policy_id,
        "policy_revision": policy.revision,
        "policy_digest": policy.canonical_digest(),
        "entitlement_snapshot_digest": "c" * 64,
        "bound_metric_versions": (_reference("metric"),),
        "bound_dimensions": (),
        "bound_filters": (),
        "restatement": "Metric metric; limit 10.",
        "product_generation_refs": (
            AnswerProductGenerationReference(product_ref=_reference("product"), generation=1),
        ),
        "outcome": "admitted",
        "reason_codes": (),
        "created_at": NOW,
    }
    values.update(changes)
    return AnswerIntentValidation.model_validate(values)


class PlanScan(ArtifactModel):
    rows: int
    bytes: int


class PlanCeilings(ArtifactModel):
    row_limit: int
    scan: PlanScan
    period_scan: PlanScan


class Plan(ArtifactModel):
    tenant_id: str
    validation_digest: str
    plan_digest: str
    statement_digest: str
    estimated_scan: PlanScan | None
    ceilings: PlanCeilings


def _plan(validation: AnswerIntentValidation, **changes: object) -> Plan:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "validation_digest": digest(validation),
        "plan_digest": "d" * 64,
        "statement_digest": "e" * 64,
        "estimated_scan": {"rows": 50, "bytes": 500},
        "ceilings": {
            "row_limit": 100,
            "scan": {"rows": 1_000, "bytes": 1_000},
            "period_scan": {"rows": 10_000, "bytes": 10_000},
        },
    }
    values.update(changes)
    return Plan.model_validate(values)


class AdmissionRepository:
    def __init__(self, request: InboxRequest, *, period_scan_consumed: int = 100) -> None:
        self.request = request
        self.receipts: list[PolicyAdmissionReceipt] = []
        self.period_scan_consumed = period_scan_consumed
        self.reservations: list[PolicyScanReservation] = []
        self.plans: dict[str, Plan] = {}

    def authorize_plan(self, plan: Plan | None) -> None:
        if plan is not None:
            self.plans[plan.plan_digest] = plan

    def read(self, tenant_id: str, plan_digest: str) -> Plan | None:
        plan = self.plans.get(plan_digest)
        if plan is None or plan.tenant_id != tenant_id:
            return None
        return plan

    def load_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        assert tenant_id == self.request.tenant_id
        assert request_id == self.request.request_id
        return self.request

    def load_for_request_revision(
        self, *, tenant_id: str, request_id: str, request_revision: int
    ) -> PolicyAdmissionReceipt | None:
        return next(
            (
                receipt
                for receipt in self.receipts
                if receipt.tenant_id == tenant_id
                and receipt.request_id == request_id
                and receipt.request_revision == request_revision
            ),
            None,
        )

    def record_and_transition(
        self,
        receipt: PolicyAdmissionReceipt,
        *,
        actor_id: str,
        reservation: PolicyScanReservation | None,
    ) -> tuple[PolicyAdmissionReceipt, InboxRequest] | PolicyAdmissionBudgetExceeded:
        assert actor_id == "system:answer-policy"
        existing = self.load_for_request_revision(
            tenant_id=receipt.tenant_id,
            request_id=receipt.request_id,
            request_revision=receipt.request_revision,
        )
        if existing is not None:
            if existing.plan_digest is None:
                assert reservation is None
                assert self.reservations == []
            else:
                assert reservation is not None
                assert self.reservations == [reservation]
            return existing, self.request
        if reservation is not None and (
            self.period_scan_consumed + reservation.reserved_scan > reservation.period_budget
        ):
            return PolicyAdmissionBudgetExceeded(
                period_scan_consumed=self.period_scan_consumed,
                requested_scan=reservation.reserved_scan,
                period_budget=reservation.period_budget,
            )
        stored = receipt.model_copy(
            update={
                "period_scan_consumed": (None if reservation is None else self.period_scan_consumed)
            }
        )
        self.receipts.append(stored)
        if reservation is not None:
            self.reservations.append(reservation)
            self.period_scan_consumed += reservation.reserved_scan
        self.request = self.request.model_copy(
            update={
                "state": RequestState.EXECUTING,
                "revision": self.request.revision + 1,
                "updated_at": stored.created_at,
            }
        )
        return stored, self.request

    def read_period_scan_consumed(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> int:
        assert tenant_id == self.request.tenant_id
        assert policy_id == "policy-1"
        assert period_start == PERIOD_START
        assert period_end == PERIOD_END
        return self.period_scan_consumed


class Breaches:
    def __init__(
        self,
        statement_digests: tuple[str, ...] = (),
        *,
        unavailable: bool = False,
    ) -> None:
        self.statement_digests = statement_digests
        self.unavailable = unavailable

    def list_breached_statement_digests(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        policy_revision: int,
    ) -> tuple[str, ...]:
        assert tenant_id == "tenant-1"
        assert policy_id == "policy-1"
        assert policy_revision >= 1
        if self.unavailable:
            raise AnswerPolicyUsageUnavailable("execution authority unavailable")
        return self.statement_digests


class Restatements:
    def confirmed_revision(
        self,
        *,
        acceptance_id: str,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        validation: AnswerIntentValidation,
    ) -> int | None:
        assert tenant_id == validation.tenant_id
        assert request_id == validation.request_id
        assert requester_id == "requester-1"
        return validation.request_revision if acceptance_id == "restatement-1" else None


def _request(*, cancelled: bool = False) -> InboxRequest:
    return InboxRequest(
        request_id="request-1",
        tenant_id="tenant-1",
        requester_id="requester-1",
        payload=StakeholderQuestion(purpose="operations", question="What is revenue?"),
        state=RequestState.CANCELLED if cancelled else RequestState.AWAITING_APPROVAL,
        revision=2,
        submitted_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )


class _DefaultPlan:
    pass


_DEFAULT_PLAN = _DefaultPlan()


def _admit(
    *,
    intent: AnswerQuestionIntent | None = None,
    policy: AnswerScopePolicy | None = None,
    validation: AnswerIntentValidation | None = None,
    plan: Plan | _DefaultPlan | None = _DEFAULT_PLAN,
    request: InboxRequest | None = None,
    restatement_acceptance_ref: str | None = "restatement-1",
    current_entitlement_snapshot_digest: str = "c" * 64,
    prior_statement_breach_digests: tuple[str, ...] = (),
    period_scan_consumed: int = 100,
    latest_policy_revision: int = 1,
    usage_unavailable: bool = False,
) -> tuple[PolicyAdmissionResult, AdmissionRepository]:
    resolved_intent = intent or _intent()
    resolved_policy = policy or _policy()
    resolved_validation = validation or _validation(resolved_intent, resolved_policy)
    resolved_plan = _plan(resolved_validation) if isinstance(plan, _DefaultPlan) else plan
    repository = AdmissionRepository(
        request or _request(), period_scan_consumed=period_scan_consumed
    )
    repository.authorize_plan(resolved_plan)
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(
            prior_statement_breach_digests,
            unavailable=usage_unavailable,
        ),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
        restatements=Restatements(),
    )
    result = service.admit(
        intent=resolved_intent,
        validation=resolved_validation,
        policy=resolved_policy,
        plan=resolved_plan,
        restatement_acceptance_ref=restatement_acceptance_ref,
        current_entitlement_snapshot_digest=current_entitlement_snapshot_digest,
        latest_policy_revision=latest_policy_revision,
        actor_id="system:answer-policy",
    )
    return result, repository


def _service_admit(
    service: AnswerPolicyAdmissionService,
    *,
    intent: AnswerQuestionIntent,
    validation: AnswerIntentValidation,
    policy: AnswerScopePolicy,
    plan: Plan | None,
) -> PolicyAdmissionResult:
    return service.admit(
        intent=intent,
        validation=validation,
        policy=policy,
        plan=plan,
        restatement_acceptance_ref=None,
        current_entitlement_snapshot_digest="c" * 64,
        latest_policy_revision=1,
        actor_id="system:answer-policy",
    )


def test_definition_admission_marks_every_plan_term_not_applicable() -> None:
    intent = _intent("definition")
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=None,
        restatement_acceptance_ref=None,
        period_scan_consumed=0,
    )

    assert result.evaluation.outcome == "admitted"
    assert result.evaluation.terms.plan_binding == "not_applicable_for_definition"
    assert result.evaluation.terms.plan_estimate == "not_applicable_for_definition"
    assert result.evaluation.terms.plan_ceilings == "not_applicable_for_definition"
    assert result.evaluation.terms.prior_statement_breach == "not_applicable_for_definition"
    assert result.evaluation.terms.period_scan_budget == "not_applicable_for_definition"
    assert result.receipt is not None
    assert result.receipt.plan_digest is None
    assert result.receipt.period_scan_consumed is None
    assert repository.request.state is RequestState.EXECUTING


@pytest.mark.parametrize(
    ("plan", "reason"),
    (
        (None, "plan_missing"),
        ("missing-estimate", "estimate_unavailable"),
        ("over-estimate", "scan_ceiling_exceeded"),
    ),
)
def test_metric_plan_routing_refuses_missing_or_over_ceiling_estimates(
    plan: object,
    reason: str,
) -> None:
    intent = _intent()
    policy = _policy()
    validation = _validation(intent, policy)
    resolved_plan: Plan | None
    if plan == "missing-estimate":
        resolved_plan = _plan(validation, estimated_scan=None)
    elif plan == "over-estimate":
        resolved_plan = _plan(validation, estimated_scan={"rows": 100, "bytes": 1_001})
    else:
        resolved_plan = None

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=resolved_plan,
    )

    assert result.receipt is None
    assert result.evaluation.outcome == "review_required"
    assert reason in result.evaluation.reason_codes
    assert repository.request.state is RequestState.AWAITING_APPROVAL
    assert repository.receipts == []


def test_admission_checks_exact_bindings_before_recording() -> None:
    intent = _intent()
    policy = _policy()
    validation = _validation(intent, policy)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=_plan(validation, tenant_id="tenant-2"),
        current_entitlement_snapshot_digest="f" * 64,
        latest_policy_revision=2,
    )

    assert result.receipt is None
    assert result.evaluation.outcome == "no_valid_plan"
    assert result.evaluation.reason_codes == (
        "plan_tenant_mismatch",
        "policy_revision_not_latest",
        "entitlement_not_current",
    )
    assert repository.receipts == []


@pytest.mark.parametrize(
    ("validation_changes", "policy_changes", "reason"),
    (
        ({"intent_digest": "f" * 64}, {}, "intent_binding_mismatch"),
        ({"policy_digest": "f" * 64}, {}, "validation_policy_mismatch"),
        ({}, {"valid_until": NOW}, "policy_not_current"),
    ),
)
def test_admission_requires_exact_validation_and_current_policy(
    validation_changes: dict[str, object],
    policy_changes: dict[str, object],
    reason: str,
) -> None:
    intent = _intent()
    policy = _policy(**policy_changes)
    validation = _validation(intent, policy, **validation_changes)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=_plan(validation),
    )

    assert result.receipt is None
    assert result.evaluation.outcome == "no_valid_plan"
    assert reason in result.evaluation.reason_codes
    assert repository.receipts == []


def test_admission_binds_the_intent_question_to_the_durable_request_payload() -> None:
    intent = _intent().model_copy(update={"question_digest": "f" * 64})
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=_plan(validation),
        restatement_acceptance_ref=None,
    )

    assert result.receipt is None
    assert result.evaluation.outcome == "no_valid_plan"
    assert "intent_binding_mismatch" in result.evaluation.reason_codes
    assert repository.receipts == []


def test_admission_rejects_plan_declaring_ceiling_above_policy() -> None:
    intent = _intent()
    policy = _policy()
    validation = _validation(intent, policy)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=_plan(
            validation,
            ceilings={
                "row_limit": policy.row_ceiling + 1,
                "scan": {"rows": 1_000, "bytes": policy.scan_ceiling},
                "period_scan": {"rows": 10_000, "bytes": policy.period_scan_budget},
            },
        ),
    )

    assert result.evaluation.outcome == "review_required"
    assert result.evaluation.reason_codes == ("plan_ceiling_exceeded",)
    assert repository.receipts == []


def test_admission_requires_restatement_and_honors_breach_budget_and_cancellation() -> None:
    intent = _intent()
    policy = _policy()
    validation = _validation(intent, policy)
    plan = _plan(validation)

    result, repository = _admit(
        intent=intent,
        policy=policy,
        validation=validation,
        plan=plan,
        request=_request(cancelled=True),
        restatement_acceptance_ref=None,
        prior_statement_breach_digests=(plan.statement_digest,),
        period_scan_consumed=9_600,
    )

    assert result.receipt is None
    assert result.evaluation.reason_codes == (
        "request_state_not_admissible",
        "restatement_not_accepted",
        "prior_statement_ceiling_breach",
        "period_scan_budget_exceeded",
        "request_cancelled",
    )
    assert repository.receipts == []


def test_metric_admission_reserves_full_executable_scan_ceiling() -> None:
    result, repository = _admit(
        policy=_policy(restatement_confirmation="never"),
        restatement_acceptance_ref=None,
        period_scan_consumed=9_000,
    )

    assert result.evaluation.outcome == "admitted"
    assert result.receipt is not None
    assert result.receipt.period_scan_consumed == 9_000
    assert repository.reservations == [
        PolicyScanReservation(
            reserved_scan=1_000,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=10_000,
        )
    ]
    assert repository.period_scan_consumed == 10_000


def test_metric_admission_fails_closed_when_usage_authority_is_unavailable() -> None:
    result, repository = _admit(
        policy=_policy(restatement_confirmation="never"),
        restatement_acceptance_ref=None,
        usage_unavailable=True,
    )

    assert result.receipt is None
    assert result.evaluation.outcome == "dependency_required"
    assert result.evaluation.reason_codes == ("usage_authority_unavailable",)
    assert repository.receipts == []


def test_exact_service_replay_returns_receipt_without_second_scan_reservation() -> None:
    intent = _intent()
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)
    plan = _plan(validation)
    repository = AdmissionRepository(_request())
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
    )

    repository.authorize_plan(plan)

    def admit(
        *,
        replay_intent: AnswerQuestionIntent = intent,
        replay_validation: AnswerIntentValidation = validation,
        replay_plan: Plan = plan,
    ) -> PolicyAdmissionResult:
        return service.admit(
            intent=replay_intent,
            validation=replay_validation,
            policy=policy,
            plan=replay_plan,
            restatement_acceptance_ref=None,
            current_entitlement_snapshot_digest="c" * 64,
            latest_policy_revision=1,
            actor_id="system:answer-policy",
        )

    first = admit()
    replay = admit()

    assert replay.receipt == first.receipt
    assert replay.request == first.request
    assert replay.evaluation.outcome == "admitted"
    assert len(repository.reservations) == 1


@pytest.mark.parametrize(
    "changed_intent",
    (
        _intent().model_copy(update={"question_digest": "f" * 64}),
        _intent().model_copy(update={"metric_refs": ("another_metric",)}),
    ),
)
def test_service_replay_rejects_an_intent_not_bound_by_the_durable_validation(
    changed_intent: AnswerQuestionIntent,
) -> None:
    intent = _intent()
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)
    plan = _plan(validation)
    repository = AdmissionRepository(_request())
    repository.authorize_plan(plan)
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
    )
    first = _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)

    with pytest.raises(StaleRevisionError, match="replay conflicts"):
        _service_admit(
            service,
            intent=changed_intent,
            validation=validation,
            policy=policy,
            plan=plan,
        )

    assert len(repository.reservations) == 1
    assert repository.receipts == [first.receipt]


@pytest.mark.parametrize(
    "plan_changes",
    (
        {"validation_digest": "f" * 64},
        {"statement_digest": "f" * 64},
    ),
)
def test_service_replay_requires_the_exact_durable_plan(plan_changes: dict[str, object]) -> None:
    intent = _intent()
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)
    plan = _plan(validation)
    repository = AdmissionRepository(_request())
    repository.authorize_plan(plan)
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
    )
    _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)

    with pytest.raises(StaleRevisionError, match="replay conflicts"):
        _service_admit(
            service,
            intent=intent,
            validation=validation,
            policy=policy,
            plan=plan.model_copy(update=plan_changes),
        )

    assert len(repository.reservations) == 1


def test_service_replay_requires_the_request_payload_bound_by_the_intent() -> None:
    intent = _intent()
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)
    plan = _plan(validation)
    repository = AdmissionRepository(_request())
    repository.authorize_plan(plan)
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
    )
    _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)
    repository.request = repository.request.model_copy(
        update={
            "payload": StakeholderQuestion(purpose="operations", question="What is another metric?")
        }
    )

    with pytest.raises(StaleRevisionError, match="replay conflicts"):
        _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)


def test_service_replay_requires_the_durable_plan_to_remain_available() -> None:
    intent = _intent()
    policy = _policy(restatement_confirmation="never")
    validation = _validation(intent, policy)
    plan = _plan(validation)
    repository = AdmissionRepository(_request())
    repository.authorize_plan(plan)
    service = AnswerPolicyAdmissionService(
        repository,
        plans=repository,
        breaches=Breaches(),
        clock=lambda: NOW,
        admission_identifier=lambda: "admission-1",
    )
    _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)
    repository.plans.clear()

    with pytest.raises(StaleRevisionError, match="plan authority is unavailable"):
        _service_admit(service, intent=intent, validation=validation, policy=policy, plan=plan)


def test_sqlite_repository_records_receipt_and_transition_atomically() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-1",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        restatement_acceptance_ref="restatement-1",
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        period_scan_consumed=None,
        created_at=NOW,
    )

    recorded = repository.record_and_transition(
        receipt,
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=1_000,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=10_000,
        ),
    )

    assert not isinstance(recorded, PolicyAdmissionBudgetExceeded)
    stored, transitioned = recorded
    assert stored == receipt.model_copy(update={"period_scan_consumed": 0})
    assert transitioned.state is RequestState.EXECUTING
    assert repository.list_admissions("tenant-1", investigating.request_id) == (stored,)
    assert repository.list_for_policy(
        tenant_id="tenant-1", policy_id="policy-1", policy_revision=1
    ) == (stored,)
    assert (
        repository.list_for_policy(tenant_id="tenant-1", policy_id="policy-1", policy_revision=2)
        == ()
    )


def test_sqlite_repository_reserves_full_scan_ceiling_across_policy_revisions() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    repository = SQLiteAnswerAdmissionRepository(requests)

    def investigating(question: str) -> InboxRequest:
        submitted = request_service.submit_question(
            tenant_id="tenant-1",
            requester_id="requester-1",
            purpose="operations",
            question=question,
        )
        return request_service.transition(
            "tenant-1",
            submitted.request_id,
            RequestState.INVESTIGATING,
            actor_id="architect-1",
            expected_revision=submitted.revision,
        )

    def receipt(
        request: InboxRequest, *, admission_id: str, revision: int
    ) -> PolicyAdmissionReceipt:
        return PolicyAdmissionReceipt(
            admission_id=admission_id,
            tenant_id="tenant-1",
            request_id=request.request_id,
            request_revision=request.revision,
            validation_digest="a" * 64,
            restatement_acceptance_ref="restatement-1",
            plan_digest=(str(revision) * 64),
            policy_id="policy-1",
            policy_revision=revision,
            policy_digest="c" * 64,
            entitlement_snapshot_digest="d" * 64,
            period_scan_consumed=None,
            created_at=NOW,
        )

    first = investigating("Question one?")
    first_result = repository.record_and_transition(
        receipt(first, admission_id="admission-1", revision=1),
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=6_000,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=10_000,
        ),
    )
    second = investigating("Question two?")
    second_result = repository.record_and_transition(
        receipt(second, admission_id="admission-2", revision=2),
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=4_000,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=10_000,
        ),
    )
    third = investigating("Question three?")
    denied = repository.record_and_transition(
        receipt(third, admission_id="admission-3", revision=2),
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=1,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=10_000,
        ),
    )

    assert not isinstance(first_result, PolicyAdmissionBudgetExceeded)
    assert first_result[0].period_scan_consumed == 0
    assert not isinstance(second_result, PolicyAdmissionBudgetExceeded)
    assert second_result[0].period_scan_consumed == 6_000
    assert isinstance(denied, PolicyAdmissionBudgetExceeded)
    assert denied.period_scan_consumed == 10_000
    unchanged = requests.load("tenant-1", third.request_id)
    assert unchanged is not None
    assert unchanged.state is RequestState.INVESTIGATING
    assert (
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        == 10_000
    )
    requests.connection.execute(
        "UPDATE answer_policy_scan_reservations SET reserved_scan = ?, period_budget = ?",
        (2**62, 2**63 - 1),
    )
    requests.connection.commit()
    with pytest.raises(AnswerPolicyUsageUnavailable, match="invalid"):
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )


def test_sqlite_repository_exact_replay_does_not_reserve_scan_twice() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    candidate = PolicyAdmissionReceipt(
        admission_id="admission-1",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        restatement_acceptance_ref="restatement-1",
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        period_scan_consumed=None,
        created_at=NOW,
    )
    reservation = PolicyScanReservation(
        reserved_scan=1_000,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        period_budget=10_000,
    )

    first = repository.record_and_transition(
        candidate, actor_id="system:answer-policy", reservation=reservation
    )
    replay = repository.record_and_transition(
        candidate, actor_id="system:answer-policy", reservation=reservation
    )

    assert replay == first
    assert (
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        == 1_000
    )

    with pytest.raises(StaleRevisionError, match="reservation conflicts"):
        repository.record_and_transition(
            candidate,
            actor_id="system:answer-policy",
            reservation=reservation.model_copy(update={"period_budget": 20_000}),
        )
    with pytest.raises(StaleRevisionError, match="replay conflicts"):
        repository.record_and_transition(
            candidate.model_copy(update={"period_scan_consumed": 999}),
            actor_id="system:answer-policy",
            reservation=reservation,
        )
    requests.connection.execute(
        "UPDATE answer_policy_scan_reservations SET period_start = ?, period_end = ? "
        "WHERE admission_id = ?",
        (
            datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
            datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
            candidate.admission_id,
        ),
    )
    requests.connection.commit()
    with pytest.raises(StaleRevisionError, match="reservation conflicts"):
        repository.record_and_transition(
            candidate,
            actor_id="system:answer-policy",
            reservation=PolicyScanReservation(
                reserved_scan=1_000,
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                period_end=datetime(2026, 9, 1, tzinfo=UTC),
                period_budget=10_000,
            ),
        )


def test_scan_reservation_rejects_non_calendar_period() -> None:
    with pytest.raises(ValueError, match="UTC calendar month"):
        PolicyScanReservation(
            reserved_scan=1_000,
            period_start=PERIOD_START + timedelta(days=1),
            period_end=PERIOD_END,
            period_budget=10_000,
        )


def test_sqlite_repository_rejects_reservation_period_not_containing_receipt_timestamp() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-wrong-period",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        created_at=NOW,
    )

    with pytest.raises(ValueError, match="receipt timestamp"):
        repository.record_and_transition(
            receipt,
            actor_id="system:answer-policy",
            reservation=PolicyScanReservation(
                reserved_scan=100,
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                period_end=datetime(2026, 9, 1, tzinfo=UTC),
                period_budget=1_000,
            ),
        )

    assert repository.list_admissions("tenant-1", investigating.request_id) == ()


@pytest.mark.parametrize("stored_scan", ("not-an-integer", str(2**63)))
def test_scan_usage_fails_closed_for_malformed_durable_integer(stored_scan: str) -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-corrupt",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        created_at=NOW,
    )
    recorded = repository.record_and_transition(
        receipt,
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=100,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=1_000,
        ),
    )
    assert not isinstance(recorded, PolicyAdmissionBudgetExceeded)
    requests.connection.execute(
        "UPDATE answer_policy_scan_reservations SET reserved_scan = ? WHERE admission_id = ?",
        (stored_scan, receipt.admission_id),
    )
    requests.connection.commit()

    with pytest.raises(AnswerPolicyUsageUnavailable, match="invalid"):
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )


@pytest.mark.parametrize(
    ("column", "corrupt_value"),
    (
        ("tenant_id", "another-tenant"),
        ("policy_id", "another-policy"),
        ("policy_revision", "2"),
        ("period_start", "not-a-timestamp"),
        ("period_end", "not-a-timestamp"),
        ("period_budget", "not-an-integer"),
    ),
)
def test_scan_usage_fails_closed_when_reservation_index_disagrees_with_receipt(
    column: str,
    corrupt_value: str,
) -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-corrupt-index",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        created_at=NOW,
    )
    recorded = repository.record_and_transition(
        receipt,
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation(
            reserved_scan=100,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            period_budget=1_000,
        ),
    )
    assert not isinstance(recorded, PolicyAdmissionBudgetExceeded)
    requests.connection.execute(
        f"UPDATE answer_policy_scan_reservations SET {column} = ? WHERE admission_id = ?",
        (corrupt_value, receipt.admission_id),
    )
    requests.connection.commit()

    with pytest.raises(AnswerPolicyUsageUnavailable, match="invalid"):
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )


def test_sqlite_lookup_rejects_integer_outside_storage_domain_without_driver_leak() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    repository = SQLiteAnswerAdmissionRepository(requests)

    with pytest.raises(StaleRevisionError, match="lookup authority"):
        repository.load_for_request_revision(
            tenant_id="tenant-1",
            request_id="request-1",
            request_revision=2**63,
        )


def test_sqlite_lookup_rejects_admission_payload_that_disagrees_with_its_index() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-corrupt-payload",
        tenant_id="tenant-1",
        request_id="request-1",
        request_revision=2,
        validation_digest="a" * 64,
        plan_digest=None,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        created_at=NOW,
    )
    requests.connection.execute(
        "INSERT INTO answer_policy_admissions "
        "(admission_id, tenant_id, request_id, request_revision, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            receipt.admission_id,
            receipt.tenant_id,
            receipt.request_id,
            receipt.request_revision,
            receipt.created_at.isoformat(),
            canonical_bytes(receipt.model_copy(update={"tenant_id": "another-tenant"})),
        ),
    )
    requests.connection.commit()

    with pytest.raises(StaleRevisionError, match="lookup authority"):
        repository.load_for_request_revision(
            tenant_id="tenant-1",
            request_id="request-1",
            request_revision=2,
        )


def test_breach_history_fails_closed_when_admission_payload_disagrees_with_request_index() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-corrupt-breach-index",
        tenant_id="tenant-1",
        request_id="payload-request",
        request_revision=2,
        validation_digest="a" * 64,
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        period_scan_consumed=0,
        created_at=NOW,
    )
    requests.connection.execute(
        "INSERT INTO answer_policy_admissions "
        "(admission_id, tenant_id, request_id, request_revision, created_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            receipt.admission_id,
            receipt.tenant_id,
            "indexed-request",
            receipt.request_revision,
            receipt.created_at.isoformat(),
            canonical_bytes(receipt),
        ),
    )
    requests.connection.commit()
    reader = DurableStatementCeilingBreachReader(
        admissions=repository,
        executions=_NoExecutionUsage(),
        plans=_NoPlanUsage(),
    )

    with pytest.raises(AnswerPolicyUsageUnavailable, match="admission authority is invalid"):
        reader.list_breached_statement_digests(
            tenant_id="tenant-1",
            policy_id="policy-1",
            policy_revision=1,
        )


@pytest.mark.parametrize(
    "changes",
    ({"reserved_scan": 2**63}, {"period_budget": 2**63}),
)
def test_scan_reservation_rejects_integer_outside_sqlite_storage_domain(
    changes: dict[str, int],
) -> None:
    values: dict[str, object] = {
        "reserved_scan": 1,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "period_budget": 1,
    }
    values.update(changes)

    with pytest.raises(ValueError, match="less than or equal"):
        PolicyScanReservation.model_validate(values)


@pytest.mark.parametrize(
    "changes",
    (
        {"request_revision": 2**63},
        {"policy_revision": 2**63},
        {"period_scan_consumed": 2**63},
    ),
)
def test_admission_receipt_rejects_integer_outside_sqlite_storage_domain(
    changes: dict[str, int],
) -> None:
    values: dict[str, object] = {
        "admission_id": "admission-oversized",
        "tenant_id": "tenant-1",
        "request_id": "request-1",
        "request_revision": 1,
        "validation_digest": "a" * 64,
        "plan_digest": "b" * 64,
        "policy_id": "policy-1",
        "policy_revision": 1,
        "policy_digest": "c" * 64,
        "entitlement_snapshot_digest": "d" * 64,
        "period_scan_consumed": 0,
        "created_at": NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match="less than or equal"):
        PolicyAdmissionReceipt.model_validate(values)


def test_sqlite_repository_rolls_back_receipt_when_exact_revision_is_stale() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-1",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision - 1,
        validation_digest="a" * 64,
        restatement_acceptance_ref="restatement-1",
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        period_scan_consumed=500,
        created_at=NOW,
    )

    with pytest.raises(StaleRevisionError, match="stale"):
        repository.record_and_transition(
            receipt,
            actor_id="system:answer-policy",
            reservation=PolicyScanReservation(
                reserved_scan=1_000,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                period_budget=10_000,
            ),
        )

    assert repository.list_admissions("tenant-1", investigating.request_id) == ()
    assert requests.load("tenant-1", investigating.request_id) == investigating


def test_sqlite_repository_rolls_back_scan_reservation_when_transition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )
    repository = SQLiteAnswerAdmissionRepository(requests)
    receipt = PolicyAdmissionReceipt(
        admission_id="admission-1",
        tenant_id="tenant-1",
        request_id=investigating.request_id,
        request_revision=investigating.revision,
        validation_digest="a" * 64,
        restatement_acceptance_ref=None,
        plan_digest="b" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_digest="c" * 64,
        entitlement_snapshot_digest="d" * 64,
        period_scan_consumed=None,
        created_at=NOW,
    )

    def fail_transition(**_arguments: object) -> InboxRequest:
        raise RuntimeError("transition failed")

    monkeypatch.setattr(requests, "transition_in_transaction", fail_transition)

    with pytest.raises(RuntimeError, match="transition failed"):
        repository.record_and_transition(
            receipt,
            actor_id="system:answer-policy",
            reservation=PolicyScanReservation(
                reserved_scan=1_000,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                period_budget=10_000,
            ),
        )

    assert repository.list_admissions("tenant-1", investigating.request_id) == ()
    assert (
        repository.read_period_scan_consumed(
            tenant_id="tenant-1",
            policy_id="policy-1",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
        )
        == 0
    )


def test_general_transition_cannot_bypass_policy_admission_receipt() -> None:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    submitted = request_service.submit_question(
        tenant_id="tenant-1",
        requester_id="requester-1",
        purpose="operations",
        question="What is revenue?",
    )
    investigating = request_service.transition(
        "tenant-1",
        submitted.request_id,
        RequestState.INVESTIGATING,
        actor_id="architect-1",
        expected_revision=submitted.revision,
    )

    with pytest.raises(ValueError, match="investigating -> executing"):
        request_service.transition(
            "tenant-1",
            investigating.request_id,
            RequestState.EXECUTING,
            actor_id="system:answer-policy",
            expected_revision=investigating.revision,
        )

    assert (
        SQLiteAnswerAdmissionRepository(requests).list_admissions(
            "tenant-1", investigating.request_id
        )
        == ()
    )


@pytest.mark.parametrize(
    "state",
    (RequestState.AWAITING_APPROVAL, RequestState.INVESTIGATING),
)
def test_both_states_an_admission_may_be_recorded_from_are_admitted(state: RequestState) -> None:
    """Two states reach an admission, because two paths do.

    A request awaiting approval has a proposal under review, which is what a console drives to. A
    request still being investigated has a restated question. Which of the two it is changes
    nothing this service decides, and neither state is a claim that the approvals or the
    requester's confirmation are recorded -- that is the caller's to establish.
    """
    result, repository = _admit(request=_request().model_copy(update={"state": state}))

    assert result.receipt is not None
    assert "request_state_not_admissible" not in result.evaluation.reason_codes
    assert repository.receipts != []


@pytest.mark.parametrize(
    "state",
    (
        RequestState.SUBMITTED,
        RequestState.CLARIFYING,
        RequestState.PROPOSED,
        RequestState.EXECUTING,
        RequestState.DELIVERED,
    ),
)
def test_every_other_state_is_refused_the_admission(state: RequestState) -> None:
    """Including `proposed` and `executing`: a proposal not yet submitted for review is not at a
    point an answer may be admitted from, and an executing request was admitted already."""
    result, repository = _admit(request=_request().model_copy(update={"state": state}))

    assert result.receipt is None
    assert "request_state_not_admissible" in result.evaluation.reason_codes
    assert repository.receipts == []
