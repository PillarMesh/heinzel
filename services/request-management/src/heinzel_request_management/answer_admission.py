from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self

from heinzel_contract_model import ArtifactModel, canonical_bytes, digest
from pydantic import Field, field_validator, model_validator

from .answer_investigation import AnswerInvestigationReader
from .answer_models import AnswerIntentValidation, AnswerQuestionIntent
from .answer_policy import AnswerScopePolicy
from .answer_restatement import RestatementAcceptanceReader
from .answer_usage import AnswerPolicyUsageUnavailable, StatementCeilingBreachReader
from .models import InboxRequest, RequestState
from .repository import SQLiteRequestRepository, StaleRevisionError

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SQLITE_INTEGER_MAX = 2**63 - 1

type PolicyAdmissionTermStatus = Literal["satisfied", "failed", "not_applicable_for_definition"]
type PolicyAdmissionOutcome = Literal[
    "admitted", "review_required", "dependency_required", "no_valid_plan"
]
type PolicyAdmissionReason = Literal[
    "request_not_investigating",
    "validation_not_admitted",
    "intent_binding_mismatch",
    "validation_policy_mismatch",
    "restatement_not_accepted",
    "plan_missing",
    "plan_not_allowed_for_definition",
    "plan_validation_mismatch",
    "estimate_unavailable",
    "plan_ceiling_exceeded",
    "scan_ceiling_exceeded",
    "prior_statement_ceiling_breach",
    "period_scan_budget_exceeded",
    "policy_revision_not_latest",
    "policy_not_current",
    "entitlement_not_current",
    "request_tenant_mismatch",
    "validation_tenant_mismatch",
    "policy_tenant_mismatch",
    "plan_tenant_mismatch",
    "request_cancelled",
    "usage_authority_unavailable",
]


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class PolicyAdmissionReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    admission_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)
    validation_digest: str = Field(pattern=_DIGEST_PATTERN)
    restatement_acceptance_ref: str | None = Field(default=None, min_length=1)
    plan_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    period_scan_consumed: int | None = Field(default=None, ge=0, le=_SQLITE_INTEGER_MAX)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")


class PolicyAdmissionTerms(ArtifactModel):
    request_state: PolicyAdmissionTermStatus
    validation_outcome: PolicyAdmissionTermStatus
    restatement: PolicyAdmissionTermStatus
    plan_binding: PolicyAdmissionTermStatus
    plan_estimate: PolicyAdmissionTermStatus
    plan_ceilings: PolicyAdmissionTermStatus
    prior_statement_breach: PolicyAdmissionTermStatus
    period_scan_budget: PolicyAdmissionTermStatus
    policy_revision: PolicyAdmissionTermStatus
    policy_validity: PolicyAdmissionTermStatus
    entitlement: PolicyAdmissionTermStatus
    tenant: PolicyAdmissionTermStatus
    cancellation: PolicyAdmissionTermStatus


class PolicyAdmissionEvaluation(ArtifactModel):
    outcome: PolicyAdmissionOutcome
    reason_codes: tuple[PolicyAdmissionReason, ...]
    terms: PolicyAdmissionTerms


class PolicyAdmissionResult(ArtifactModel):
    evaluation: PolicyAdmissionEvaluation
    receipt: PolicyAdmissionReceipt | None
    request: InboxRequest


class AnswerScanForAdmission(Protocol):
    @property
    def rows(self) -> int: ...

    @property
    def bytes(self) -> int: ...


class AnswerCeilingsForAdmission(Protocol):
    @property
    def row_limit(self) -> int: ...

    @property
    def scan(self) -> AnswerScanForAdmission: ...

    @property
    def period_scan(self) -> AnswerScanForAdmission: ...


class PolicyScanReservation(ArtifactModel):
    """Irrevocably reserve the plan's maximum authorized scan for one UTC calendar month.

    Execution receipts expose result size but no authoritative source scan, so admission never
    reduces or releases this conservative reservation after an attempt.
    """

    reserved_scan: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)
    period_start: datetime
    period_end: datetime
    period_budget: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)

    @field_validator("period_start", "period_end")
    @classmethod
    def period_timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "period timestamp"))

    @model_validator(mode="after")
    def period_is_one_utc_calendar_month(self) -> Self:
        expected_start = self.period_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if expected_start.month == 12:
            expected_end = expected_start.replace(year=expected_start.year + 1, month=1)
        else:
            expected_end = expected_start.replace(month=expected_start.month + 1)
        if self.period_start != expected_start or self.period_end != expected_end:
            raise ValueError("scan reservation period must be one UTC calendar month")
        return self

    @classmethod
    def for_calendar_month(
        cls,
        *,
        reserved_scan: int,
        period_budget: int,
        at: datetime,
    ) -> Self:
        current = _utc(at, "reservation timestamp")
        period_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_start.month == 12:
            period_end = period_start.replace(year=period_start.year + 1, month=1)
        else:
            period_end = period_start.replace(month=period_start.month + 1)
        return cls(
            reserved_scan=reserved_scan,
            period_start=period_start,
            period_end=period_end,
            period_budget=period_budget,
        )


class PolicyAdmissionBudgetExceeded(ArtifactModel):
    period_scan_consumed: int = Field(ge=0, le=_SQLITE_INTEGER_MAX)
    requested_scan: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)
    period_budget: int = Field(gt=0, le=_SQLITE_INTEGER_MAX)


class AnswerPlanForAdmission(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def validation_digest(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    @property
    def statement_digest(self) -> str: ...

    @property
    def estimated_scan(self) -> AnswerScanForAdmission | None: ...

    @property
    def ceilings(self) -> AnswerCeilingsForAdmission: ...


class AnswerAdmissionPlanReader(Protocol):
    def read(self, tenant_id: str, plan_digest: str) -> AnswerPlanForAdmission | None: ...


class AnswerAdmissionRepository(Protocol):
    def load_request(self, tenant_id: str, request_id: str) -> InboxRequest: ...

    def load_for_request_revision(
        self, *, tenant_id: str, request_id: str, request_revision: int
    ) -> PolicyAdmissionReceipt | None: ...

    def read_period_scan_consumed(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> int: ...

    def record_and_transition(
        self,
        receipt: PolicyAdmissionReceipt,
        *,
        actor_id: str,
        reservation: PolicyScanReservation | None,
    ) -> tuple[PolicyAdmissionReceipt, InboxRequest] | PolicyAdmissionBudgetExceeded: ...


class AnswerPolicyAdmissionService:
    def __init__(
        self,
        repository: AnswerAdmissionRepository,
        *,
        plans: AnswerAdmissionPlanReader,
        breaches: StatementCeilingBreachReader,
        clock: Callable[[], datetime],
        admission_identifier: Callable[[], str],
        restatements: RestatementAcceptanceReader | None = None,
        investigations: AnswerInvestigationReader | None = None,
    ) -> None:
        self._repository = repository
        self._plans = plans
        self._breaches = breaches
        self._clock = clock
        self._admission_identifier = admission_identifier
        self._restatements = restatements
        self._investigations = investigations

    def admit(
        self,
        *,
        intent: AnswerQuestionIntent,
        validation: AnswerIntentValidation,
        policy: AnswerScopePolicy,
        plan: AnswerPlanForAdmission | None,
        restatement_acceptance_ref: str | None,
        current_entitlement_snapshot_digest: str,
        latest_policy_revision: int,
        actor_id: str,
    ) -> PolicyAdmissionResult:
        now = _utc(self._clock(), "clock")
        request = self._repository.load_request(intent.tenant_id, intent.request_id)
        restatement_confirmed_revision = (
            self._restatements.confirmed_revision(
                acceptance_id=restatement_acceptance_ref,
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                requester_id=request.requester_id,
                validation=validation,
            )
            if restatement_acceptance_ref is not None and self._restatements is not None
            else None
        )
        investigation_confirmed_revision = (
            self._investigations.confirmed_revision(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                requester_id=request.requester_id,
                validation=validation,
            )
            if restatement_acceptance_ref is None and self._investigations is not None
            else None
        )
        continuity_revision = (
            restatement_confirmed_revision
            or investigation_confirmed_revision
            or intent.request_revision
        )
        restatement_accepted = restatement_confirmed_revision == request.revision
        investigation_current = investigation_confirmed_revision == request.revision
        existing = self._repository.load_for_request_revision(
            tenant_id=intent.tenant_id,
            request_id=intent.request_id,
            request_revision=continuity_revision,
        )
        if existing is not None:
            definition = existing.plan_digest is None
            authoritative_plan = self._read_plan_for_replay(existing, plan)
            if (
                existing.tenant_id != intent.tenant_id
                or existing.request_id != intent.request_id
                or existing.request_revision != continuity_revision
                or request.tenant_id != intent.tenant_id
                or request.request_id != intent.request_id
                or digest(request.payload) != intent.question_digest
                or validation.tenant_id != intent.tenant_id
                or validation.request_id != intent.request_id
                or validation.request_revision != intent.request_revision
                or validation.intent_digest != digest(intent)
                or existing.validation_digest != digest(validation)
                or existing.restatement_acceptance_ref != restatement_acceptance_ref
                or definition != (intent.intent_kind == "definition")
                or (definition and plan is not None)
                or (not definition and authoritative_plan != plan)
                or (
                    not definition
                    and authoritative_plan is not None
                    and authoritative_plan.validation_digest != existing.validation_digest
                )
                or existing.policy_id != policy.policy_id
                or existing.policy_revision != policy.revision
                or existing.policy_digest != policy.canonical_digest()
                or existing.entitlement_snapshot_digest != validation.entitlement_snapshot_digest
                or current_entitlement_snapshot_digest != existing.entitlement_snapshot_digest
                or latest_policy_revision != existing.policy_revision
            ):
                raise StaleRevisionError("policy admission replay conflicts")
            replay_reservation = (
                None
                if definition or plan is None
                else PolicyScanReservation.for_calendar_month(
                    reserved_scan=plan.ceilings.scan.bytes,
                    period_budget=policy.period_scan_budget,
                    at=existing.created_at,
                )
            )
            replayed = self._repository.record_and_transition(
                existing.model_copy(update={"period_scan_consumed": None}),
                actor_id=actor_id,
                reservation=replay_reservation,
            )
            if isinstance(replayed, PolicyAdmissionBudgetExceeded):
                raise StaleRevisionError("policy admission replay budget authority conflicts")
            replayed_receipt, current_request = replayed
            return PolicyAdmissionResult(
                evaluation=PolicyAdmissionEvaluation(
                    outcome="admitted",
                    reason_codes=(),
                    terms=self._admitted_terms(definition),
                ),
                receipt=replayed_receipt,
                request=current_request,
            )
        reasons: list[PolicyAdmissionReason] = []

        request_investigating = request.state is RequestState.INVESTIGATING
        if not request_investigating:
            reasons.append("request_not_investigating")

        validation_admitted = validation.outcome == "admitted"
        if not validation_admitted:
            reasons.append("validation_not_admitted")

        intent_bound = (
            validation.request_id == intent.request_id
            and validation.request_revision == intent.request_revision
            and validation.intent_digest == digest(intent)
            and request.request_id == intent.request_id
            and (
                request.revision == intent.request_revision
                or restatement_accepted
                or investigation_current
            )
            and digest(request.payload) == intent.question_digest
        )
        if not intent_bound:
            reasons.append("intent_binding_mismatch")

        policy_bound = (
            validation.policy_id == policy.policy_id
            and validation.policy_revision == policy.revision
            and validation.policy_digest == policy.canonical_digest()
        )
        if not policy_bound:
            reasons.append("validation_policy_mismatch")

        request_tenant = request.tenant_id == intent.tenant_id
        if not request_tenant:
            reasons.append("request_tenant_mismatch")
        validation_tenant = validation.tenant_id == intent.tenant_id
        if not validation_tenant:
            reasons.append("validation_tenant_mismatch")
        plan_tenant = plan is None or plan.tenant_id == intent.tenant_id
        if not plan_tenant:
            reasons.append("plan_tenant_mismatch")
        policy_tenant = policy.tenant_id == intent.tenant_id
        if not policy_tenant:
            reasons.append("policy_tenant_mismatch")
        tenant_satisfied = request_tenant and validation_tenant and policy_tenant and plan_tenant

        restatement_satisfied = (
            not policy.requires_restatement_confirmation(interpreter=intent.interpreter)
            or restatement_accepted
        )
        if not restatement_satisfied:
            reasons.append("restatement_not_accepted")

        definition = intent.intent_kind == "definition"
        plan_bound = definition and plan is None
        estimate_satisfied = definition
        ceilings_satisfied = definition
        breach_satisfied = definition
        budget_satisfied = definition
        reservation: PolicyScanReservation | None = None
        if definition:
            if plan is not None:
                reasons.append("plan_not_allowed_for_definition")
        elif plan is None:
            reasons.append("plan_missing")
        else:
            authoritative_plan = (
                self._read_plan(intent.tenant_id, plan)
                if plan.tenant_id == intent.tenant_id
                else plan
            )
            plan_bound = plan.validation_digest == digest(validation) and authoritative_plan == plan
            if not plan_bound:
                reasons.append("plan_validation_mismatch")

            estimate_satisfied = plan.estimated_scan is not None
            if not estimate_satisfied:
                reasons.append("estimate_unavailable")

            ceilings_satisfied = (
                plan.ceilings.row_limit <= policy.row_ceiling
                and plan.ceilings.scan.bytes <= policy.scan_ceiling
                and plan.ceilings.period_scan.bytes <= policy.period_scan_budget
            )
            if not ceilings_satisfied:
                reasons.append("plan_ceiling_exceeded")
            if (
                plan.estimated_scan is not None
                and plan.estimated_scan.bytes > plan.ceilings.scan.bytes
            ):
                estimate_satisfied = False
                reasons.append("scan_ceiling_exceeded")

            reservation = PolicyScanReservation.for_calendar_month(
                reserved_scan=plan.ceilings.scan.bytes,
                period_budget=policy.period_scan_budget,
                at=now,
            )
            try:
                prior_statement_breach_digests = self._breaches.list_breached_statement_digests(
                    tenant_id=intent.tenant_id,
                    policy_id=policy.policy_id,
                    policy_revision=policy.revision,
                )
                period_scan_consumed = self._repository.read_period_scan_consumed(
                    tenant_id=intent.tenant_id,
                    policy_id=policy.policy_id,
                    period_start=reservation.period_start,
                    period_end=reservation.period_end,
                )
            except AnswerPolicyUsageUnavailable:
                breach_satisfied = False
                budget_satisfied = False
                reasons.append("usage_authority_unavailable")
            else:
                breach_satisfied = plan.statement_digest not in prior_statement_breach_digests
                if not breach_satisfied:
                    reasons.append("prior_statement_ceiling_breach")

                budget_satisfied = (
                    period_scan_consumed + reservation.reserved_scan <= policy.period_scan_budget
                )
                if not budget_satisfied:
                    reasons.append("period_scan_budget_exceeded")

        policy_latest = policy.revision == latest_policy_revision
        if not policy_latest:
            reasons.append("policy_revision_not_latest")
        policy_current = policy.valid_from <= now < policy.valid_until
        if not policy_current:
            reasons.append("policy_not_current")
        entitlement_current = (
            validation.entitlement_snapshot_digest == current_entitlement_snapshot_digest
        )
        if not entitlement_current:
            reasons.append("entitlement_not_current")

        not_cancelled = request.state is not RequestState.CANCELLED
        if not not_cancelled:
            reasons.append("request_cancelled")

        terms = PolicyAdmissionTerms(
            request_state="satisfied" if request_investigating else "failed",
            validation_outcome=(
                "satisfied" if validation_admitted and intent_bound and policy_bound else "failed"
            ),
            restatement="satisfied" if restatement_satisfied else "failed",
            plan_binding=self._plan_term(definition, plan_bound),
            plan_estimate=self._plan_term(definition, estimate_satisfied),
            plan_ceilings=self._plan_term(definition, ceilings_satisfied),
            prior_statement_breach=self._plan_term(definition, breach_satisfied),
            period_scan_budget=self._plan_term(definition, budget_satisfied),
            policy_revision="satisfied" if policy_latest else "failed",
            policy_validity="satisfied" if policy_current else "failed",
            entitlement="satisfied" if entitlement_current else "failed",
            tenant="satisfied" if tenant_satisfied else "failed",
            cancellation="satisfied" if not_cancelled else "failed",
        )
        if reasons:
            evaluation = PolicyAdmissionEvaluation(
                outcome=self._failure_outcome(tuple(reasons)),
                reason_codes=tuple(dict.fromkeys(reasons)),
                terms=terms,
            )
            return PolicyAdmissionResult(evaluation=evaluation, receipt=None, request=request)

        receipt = PolicyAdmissionReceipt(
            admission_id=self._admission_identifier(),
            tenant_id=intent.tenant_id,
            request_id=intent.request_id,
            request_revision=request.revision,
            validation_digest=digest(validation),
            restatement_acceptance_ref=restatement_acceptance_ref if restatement_accepted else None,
            plan_digest=None if plan is None else plan.plan_digest,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            policy_digest=policy.canonical_digest(),
            entitlement_snapshot_digest=validation.entitlement_snapshot_digest,
            period_scan_consumed=None,
            created_at=now,
        )
        record_result = self._repository.record_and_transition(
            receipt,
            actor_id=actor_id,
            reservation=reservation,
        )
        if isinstance(record_result, PolicyAdmissionBudgetExceeded):
            return PolicyAdmissionResult(
                evaluation=PolicyAdmissionEvaluation(
                    outcome="dependency_required",
                    reason_codes=("period_scan_budget_exceeded",),
                    terms=terms.model_copy(update={"period_scan_budget": "failed"}),
                ),
                receipt=None,
                request=request,
            )
        stored, transitioned = record_result
        return PolicyAdmissionResult(
            evaluation=PolicyAdmissionEvaluation(outcome="admitted", reason_codes=(), terms=terms),
            receipt=stored,
            request=transitioned,
        )

    def _read_plan(
        self, tenant_id: str, plan: AnswerPlanForAdmission
    ) -> AnswerPlanForAdmission | None:
        try:
            return self._plans.read(tenant_id, plan.plan_digest)
        except (sqlite3.Error, RuntimeError, ValueError):
            return None

    def _read_plan_for_replay(
        self,
        receipt: PolicyAdmissionReceipt,
        plan: AnswerPlanForAdmission | None,
    ) -> AnswerPlanForAdmission | None:
        if receipt.plan_digest is None:
            return None
        if plan is None or plan.plan_digest != receipt.plan_digest:
            raise StaleRevisionError("policy admission replay conflicts")
        try:
            authoritative = self._plans.read(receipt.tenant_id, receipt.plan_digest)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise StaleRevisionError(
                "policy admission replay plan authority is unavailable"
            ) from error
        if authoritative is None:
            raise StaleRevisionError("policy admission replay plan authority is unavailable")
        return authoritative

    @staticmethod
    def _plan_term(definition: bool, satisfied: bool) -> PolicyAdmissionTermStatus:
        if definition:
            return "not_applicable_for_definition"
        return "satisfied" if satisfied else "failed"

    @staticmethod
    def _admitted_terms(definition: bool) -> PolicyAdmissionTerms:
        plan_term: PolicyAdmissionTermStatus = (
            "not_applicable_for_definition" if definition else "satisfied"
        )
        return PolicyAdmissionTerms(
            request_state="satisfied",
            validation_outcome="satisfied",
            restatement="satisfied",
            plan_binding=plan_term,
            plan_estimate=plan_term,
            plan_ceilings=plan_term,
            prior_statement_breach=plan_term,
            period_scan_budget=plan_term,
            policy_revision="satisfied",
            policy_validity="satisfied",
            entitlement="satisfied",
            tenant="satisfied",
            cancellation="satisfied",
        )

    @staticmethod
    def _failure_outcome(reasons: tuple[PolicyAdmissionReason, ...]) -> PolicyAdmissionOutcome:
        integrity = {
            "intent_binding_mismatch",
            "validation_policy_mismatch",
            "plan_validation_mismatch",
            "policy_revision_not_latest",
            "policy_not_current",
            "entitlement_not_current",
            "request_tenant_mismatch",
            "validation_tenant_mismatch",
            "policy_tenant_mismatch",
            "plan_tenant_mismatch",
        }
        if any(reason in integrity for reason in reasons):
            return "no_valid_plan"
        if "period_scan_budget_exceeded" in reasons:
            return "dependency_required"
        if "usage_authority_unavailable" in reasons:
            return "dependency_required"
        return "review_required"


class SQLiteAnswerAdmissionRepository:
    def __init__(self, requests: SQLiteRequestRepository) -> None:
        self._requests = requests
        self._connection = requests.connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS answer_policy_admissions ("
            "admission_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, request_id, request_revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS answer_policy_scan_reservations ("
            "admission_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, policy_id TEXT NOT NULL, "
            "policy_revision INTEGER NOT NULL, period_start TEXT NOT NULL, "
            "period_end TEXT NOT NULL, reserved_scan INTEGER NOT NULL, "
            "period_budget INTEGER NOT NULL, "
            "FOREIGN KEY (admission_id) REFERENCES answer_policy_admissions(admission_id))"
        )
        self._connection.commit()

    def load_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        return self._requests.load_owned_request(tenant_id, request_id)

    def load_for_request_revision(
        self, *, tenant_id: str, request_id: str, request_revision: int
    ) -> PolicyAdmissionReceipt | None:
        try:
            _strict_sqlite_integer(request_revision, "request revision", minimum=1)
            row = self._connection.execute(
                "SELECT payload FROM answer_policy_admissions "
                "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
                (tenant_id, request_id, request_revision),
            ).fetchone()
            if row is None:
                return None
            receipt = PolicyAdmissionReceipt.model_validate_json(bytes(row[0]), strict=True)
            if (
                receipt.tenant_id != tenant_id
                or receipt.request_id != request_id
                or receipt.request_revision != request_revision
            ):
                raise ValueError("admission payload disagrees with its durable index")
        except (OverflowError, TypeError, ValueError, sqlite3.Error) as error:
            raise StaleRevisionError(
                "policy admission lookup authority is invalid or unavailable"
            ) from error
        return receipt

    def record_and_transition(
        self,
        receipt: PolicyAdmissionReceipt,
        *,
        actor_id: str,
        reservation: PolicyScanReservation | None,
    ) -> tuple[PolicyAdmissionReceipt, InboxRequest] | PolicyAdmissionBudgetExceeded:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            replay = self._connection.execute(
                "SELECT payload FROM answer_policy_admissions "
                "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
                (receipt.tenant_id, receipt.request_id, receipt.request_revision),
            ).fetchone()
            if replay is not None:
                recorded = PolicyAdmissionReceipt.model_validate_json(bytes(replay[0]), strict=True)
                if receipt.period_scan_consumed is not None:
                    raise StaleRevisionError("policy admission replay conflicts")
                expected = receipt.model_copy(
                    update={"period_scan_consumed": recorded.period_scan_consumed}
                )
                if recorded != expected:
                    raise StaleRevisionError("policy admission replay conflicts")
                self._assert_replay_reservation(recorded, reservation)
                request = self._requests.load_owned_request(receipt.tenant_id, receipt.request_id)
                self._connection.commit()
                return recorded, request
            request = self._requests.load_owned_request(receipt.tenant_id, receipt.request_id)
            if request.revision != receipt.request_revision:
                raise StaleRevisionError("policy admission request revision is stale")
            if request.state is not RequestState.INVESTIGATING:
                raise ValueError("policy admission requires an investigating request")
            if (receipt.plan_digest is None) != (reservation is None):
                raise ValueError("metric admission requires one scan reservation")
            if receipt.period_scan_consumed is not None:
                raise ValueError("period scan consumption is repository-owned")
            if reservation is not None:
                _require_reservation_period_matches_receipt(receipt, reservation)
            stored = receipt
            if reservation is not None:
                period_scan_consumed = self.read_period_scan_consumed(
                    tenant_id=receipt.tenant_id,
                    policy_id=receipt.policy_id,
                    period_start=reservation.period_start,
                    period_end=reservation.period_end,
                )
                if period_scan_consumed + reservation.reserved_scan > reservation.period_budget:
                    with suppress(sqlite3.Error):
                        self._connection.rollback()
                    return PolicyAdmissionBudgetExceeded(
                        period_scan_consumed=period_scan_consumed,
                        requested_scan=reservation.reserved_scan,
                        period_budget=reservation.period_budget,
                    )
                stored = receipt.model_copy(update={"period_scan_consumed": period_scan_consumed})
            self._connection.execute(
                "INSERT INTO answer_policy_admissions "
                "(admission_id, tenant_id, request_id, request_revision, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    receipt.admission_id,
                    receipt.tenant_id,
                    receipt.request_id,
                    receipt.request_revision,
                    receipt.created_at.isoformat(),
                    canonical_bytes(stored),
                ),
            )
            if reservation is not None:
                self._connection.execute(
                    "INSERT INTO answer_policy_scan_reservations "
                    "(admission_id, tenant_id, policy_id, policy_revision, period_start, "
                    "period_end, reserved_scan, period_budget) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored.admission_id,
                        stored.tenant_id,
                        stored.policy_id,
                        stored.policy_revision,
                        reservation.period_start.isoformat(),
                        reservation.period_end.isoformat(),
                        reservation.reserved_scan,
                        reservation.period_budget,
                    ),
                )
            transitioned = self._requests.transition_in_transaction(
                tenant_id=stored.tenant_id,
                request_id=stored.request_id,
                expected_revision=stored.request_revision,
                actor_id=actor_id,
                to_state=RequestState.EXECUTING,
                created_at=stored.created_at,
            )
        except sqlite3.IntegrityError as error:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise StaleRevisionError("policy admission conflicted") from error
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        self._connection.commit()
        return stored, transitioned

    def read_period_scan_consumed(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> int:
        start = _utc(period_start, "period_start")
        end = _utc(period_end, "period_end")
        try:
            PolicyScanReservation(
                reserved_scan=1,
                period_start=start,
                period_end=end,
                period_budget=1,
            )
            rows = self._connection.execute(
                "SELECT reservations.admission_id, reservations.tenant_id, "
                "reservations.policy_id, reservations.policy_revision, "
                "reservations.period_start, reservations.period_end, "
                "reservations.reserved_scan, reservations.period_budget, admissions.payload "
                "FROM answer_policy_scan_reservations AS reservations "
                "LEFT JOIN answer_policy_admissions AS admissions "
                "ON admissions.admission_id = reservations.admission_id "
                "WHERE admissions.tenant_id = ? OR reservations.tenant_id = ? "
                "ORDER BY reservations.admission_id",
                (tenant_id, tenant_id),
            ).fetchall()
            total = 0
            for row in rows:
                stored_policy_id, reservation = _validated_reservation(row, tenant_id=tenant_id)
                if (
                    stored_policy_id != policy_id
                    or reservation.period_start != start
                    or reservation.period_end != end
                ):
                    continue
                total += reservation.reserved_scan
                _strict_sqlite_integer(total, "period scan total", minimum=0)
            return total
        except sqlite3.Error as error:
            raise AnswerPolicyUsageUnavailable(
                "scan reservation authority is unavailable"
            ) from error
        except (OverflowError, TypeError, ValueError) as error:
            raise AnswerPolicyUsageUnavailable("scan reservation authority is invalid") from error

    def _assert_replay_reservation(
        self,
        receipt: PolicyAdmissionReceipt,
        reservation: PolicyScanReservation | None,
    ) -> None:
        row = self._connection.execute(
            "SELECT period_start, period_end, reserved_scan, period_budget "
            "FROM answer_policy_scan_reservations WHERE admission_id = ?",
            (receipt.admission_id,),
        ).fetchone()
        if row is None:
            if reservation is not None:
                raise StaleRevisionError("policy admission replay reservation is missing")
            return
        if reservation is None:
            raise StaleRevisionError("policy admission replay reservation conflicts")
        try:
            _require_reservation_period_matches_receipt(receipt, reservation)
            stored = PolicyScanReservation(
                period_start=_strict_stored_timestamp(row[0], "period start"),
                period_end=_strict_stored_timestamp(row[1], "period end"),
                reserved_scan=_strict_sqlite_integer(row[2], "reserved scan", minimum=1),
                period_budget=_strict_sqlite_integer(row[3], "period budget", minimum=1),
            )
        except (TypeError, ValueError) as error:
            raise StaleRevisionError("policy admission replay reservation conflicts") from error
        if stored != reservation:
            raise StaleRevisionError("policy admission replay reservation conflicts")

    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[PolicyAdmissionReceipt, ...]:
        self._requests.load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM answer_policy_admissions "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY created_at, admission_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(PolicyAdmissionReceipt.model_validate_json(row[0]) for row in rows)

    def list_for_policy(
        self, *, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[PolicyAdmissionReceipt, ...]:
        try:
            if not tenant_id or not policy_id:
                raise ValueError("policy admission scope is invalid")
            _strict_sqlite_integer(policy_revision, "policy revision", minimum=1)
            rows = self._connection.execute(
                "SELECT admission_id, tenant_id, request_id, request_revision, created_at, payload "
                "FROM answer_policy_admissions WHERE tenant_id = ? "
                "ORDER BY created_at, admission_id",
                (tenant_id,),
            ).fetchall()
            receipts = tuple(_validated_admission_row(row, tenant_id=tenant_id) for row in rows)
        except sqlite3.Error as error:
            raise AnswerPolicyUsageUnavailable(
                "policy admission authority is unavailable"
            ) from error
        except (TypeError, ValueError) as error:
            raise AnswerPolicyUsageUnavailable("policy admission authority is invalid") from error
        return tuple(
            receipt
            for receipt in receipts
            if receipt.policy_id == policy_id and receipt.policy_revision == policy_revision
        )


def _strict_sqlite_integer(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > _SQLITE_INTEGER_MAX:
        raise ValueError(f"{field_name} is outside the SQLite integer storage domain")
    return value


def _strict_stored_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} is not stored as text")
    parsed = datetime.fromisoformat(value)
    normalized = _utc(parsed, field_name)
    if value != normalized.isoformat():
        raise ValueError(f"{field_name} is not stored canonically")
    return normalized


def _validated_admission_row(
    row: tuple[object, ...],
    *,
    tenant_id: str,
) -> PolicyAdmissionReceipt:
    if len(row) != 6:
        raise ValueError("policy admission row has an invalid shape")
    admission_id, stored_tenant_id, request_id, request_revision, created_at, payload = row
    if not isinstance(payload, bytes):
        raise TypeError("policy admission payload is not stored as bytes")
    receipt = PolicyAdmissionReceipt.model_validate_json(payload, strict=True)
    stored_revision = _strict_sqlite_integer(request_revision, "request revision", minimum=1)
    stored_created_at = _strict_stored_timestamp(created_at, "admission creation timestamp")
    if (
        not isinstance(admission_id, str)
        or not isinstance(stored_tenant_id, str)
        or not isinstance(request_id, str)
        or admission_id != receipt.admission_id
        or stored_tenant_id != tenant_id
        or stored_tenant_id != receipt.tenant_id
        or request_id != receipt.request_id
        or stored_revision != receipt.request_revision
        or stored_created_at != receipt.created_at
    ):
        raise ValueError("policy admission authority does not match its durable index")
    return receipt


def _require_reservation_period_matches_receipt(
    receipt: PolicyAdmissionReceipt,
    reservation: PolicyScanReservation,
) -> None:
    expected = PolicyScanReservation.for_calendar_month(
        reserved_scan=reservation.reserved_scan,
        period_budget=reservation.period_budget,
        at=receipt.created_at,
    )
    if reservation != expected:
        raise ValueError("scan reservation period does not contain the receipt timestamp")


def _validated_reservation(
    row: tuple[object, ...],
    *,
    tenant_id: str,
) -> tuple[str, PolicyScanReservation]:
    if len(row) != 9:
        raise ValueError("scan reservation row has an invalid shape")
    (
        admission_id,
        stored_tenant_id,
        stored_policy_id,
        stored_policy_revision,
        stored_period_start,
        stored_period_end,
        stored_scan,
        stored_budget,
        payload,
    ) = row
    if not isinstance(admission_id, str) or not admission_id:
        raise ValueError("scan reservation admission key is invalid")
    if not isinstance(stored_tenant_id, str) or not stored_tenant_id:
        raise ValueError("scan reservation tenant key is invalid")
    if not isinstance(stored_policy_id, str) or not stored_policy_id:
        raise ValueError("scan reservation key is invalid")
    if not isinstance(payload, bytes):
        raise ValueError("scan reservation admission payload is missing")
    policy_revision = _strict_sqlite_integer(stored_policy_revision, "policy revision", minimum=1)
    reservation = PolicyScanReservation(
        period_start=_strict_stored_timestamp(stored_period_start, "period start"),
        period_end=_strict_stored_timestamp(stored_period_end, "period end"),
        reserved_scan=_strict_sqlite_integer(stored_scan, "reserved scan", minimum=1),
        period_budget=_strict_sqlite_integer(stored_budget, "period budget", minimum=1),
    )
    receipt = PolicyAdmissionReceipt.model_validate_json(payload, strict=True)
    if (
        admission_id != receipt.admission_id
        or stored_tenant_id != tenant_id
        or stored_tenant_id != receipt.tenant_id
        or stored_policy_id != receipt.policy_id
        or policy_revision != receipt.policy_revision
        or receipt.plan_digest is None
    ):
        raise ValueError("scan reservation authority does not match its durable keys")
    _require_reservation_period_matches_receipt(receipt, reservation)
    return stored_policy_id, reservation
