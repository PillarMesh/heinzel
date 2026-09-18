from __future__ import annotations

from typing import Protocol


class PolicyAdmissionUsageEvidence(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def request_id(self) -> str: ...

    @property
    def policy_id(self) -> str: ...

    @property
    def policy_revision(self) -> int: ...

    @property
    def plan_digest(self) -> str | None: ...


class AnswerExecutionUsageEvidence(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def request_id(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    @property
    def outcome(self) -> str: ...


class AnswerPlanUsageEvidence(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    @property
    def statement_digest(self) -> str: ...


class PolicyAdmissionUsageReader(Protocol):
    def list_for_policy(
        self, *, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[PolicyAdmissionUsageEvidence, ...]: ...


class AnswerExecutionUsageReader(Protocol):
    def list_for_tenant(self, *, tenant_id: str) -> tuple[AnswerExecutionUsageEvidence, ...]: ...


class AnswerPlanUsageReader(Protocol):
    def read(self, *, tenant_id: str, plan_digest: str) -> AnswerPlanUsageEvidence | None: ...


class StatementCeilingBreachReader(Protocol):
    def list_breached_statement_digests(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        policy_revision: int,
    ) -> tuple[str, ...]: ...


class AnswerPolicyUsageUnavailable(RuntimeError):
    pass


class DurableStatementCeilingBreachReader:
    def __init__(
        self,
        *,
        admissions: PolicyAdmissionUsageReader,
        executions: AnswerExecutionUsageReader,
        plans: AnswerPlanUsageReader,
    ) -> None:
        self._admissions = admissions
        self._executions = executions
        self._plans = plans

    def list_breached_statement_digests(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        policy_revision: int,
    ) -> tuple[str, ...]:
        try:
            return self._resolve(
                tenant_id=tenant_id,
                policy_id=policy_id,
                policy_revision=policy_revision,
            )
        except AnswerPolicyUsageUnavailable:
            raise
        except Exception as error:
            raise AnswerPolicyUsageUnavailable("answer usage evidence is unavailable") from error

    def _resolve(
        self,
        *,
        tenant_id: str,
        policy_id: str,
        policy_revision: int,
    ) -> tuple[str, ...]:
        admitted_plans = {
            (admission.request_id, admission.plan_digest)
            for admission in self._admissions.list_for_policy(
                tenant_id=tenant_id,
                policy_id=policy_id,
                policy_revision=policy_revision,
            )
            if admission.tenant_id == tenant_id
            and admission.policy_id == policy_id
            and admission.policy_revision == policy_revision
            and admission.plan_digest is not None
        }
        breached_plan_digests = {
            execution.plan_digest
            for execution in self._executions.list_for_tenant(tenant_id=tenant_id)
            if execution.tenant_id == tenant_id
            and execution.outcome == "ceiling_exceeded"
            and (execution.request_id, execution.plan_digest) in admitted_plans
        }
        statement_digests: set[str] = set()
        for plan_digest in breached_plan_digests:
            plan = self._plans.read(tenant_id=tenant_id, plan_digest=plan_digest)
            if plan is None or plan.tenant_id != tenant_id or plan.plan_digest != plan_digest:
                raise AnswerPolicyUsageUnavailable(
                    "exact plan authority is unavailable for a ceiling breach"
                )
            statement_digests.add(plan.statement_digest)
        return tuple(sorted(statement_digests))
