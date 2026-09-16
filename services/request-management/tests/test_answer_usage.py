from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import pytest
from pillarmesh_request_management.answer_usage import (
    AnswerExecutionUsageEvidence,
    AnswerPlanUsageEvidence,
    AnswerPolicyUsageUnavailable,
    DurableStatementCeilingBreachReader,
    PolicyAdmissionUsageEvidence,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Admission:
    tenant_id: str
    request_id: str
    policy_id: str
    policy_revision: int
    plan_digest: str | None


@dataclass(frozen=True)
class Execution:
    tenant_id: str
    request_id: str
    plan_digest: str
    outcome: Literal["succeeded", "ceiling_exceeded"]


@dataclass(frozen=True)
class Plan:
    tenant_id: str
    plan_digest: str
    statement_digest: str


class Admissions:
    def __init__(self, records: tuple[Admission, ...]) -> None:
        self.records: tuple[PolicyAdmissionUsageEvidence, ...] = records

    def list_for_policy(
        self, *, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[PolicyAdmissionUsageEvidence, ...]:
        return tuple(
            record
            for record in self.records
            if record.tenant_id == tenant_id
            and record.policy_id == policy_id
            and record.policy_revision == policy_revision
        )


class UnscopedAdmissions(Admissions):
    def list_for_policy(
        self, *, tenant_id: str, policy_id: str, policy_revision: int
    ) -> tuple[PolicyAdmissionUsageEvidence, ...]:
        return self.records


class Executions:
    def __init__(self, records: tuple[Execution, ...]) -> None:
        self.records: tuple[AnswerExecutionUsageEvidence, ...] = records

    def list_for_tenant(self, *, tenant_id: str) -> tuple[AnswerExecutionUsageEvidence, ...]:
        return tuple(record for record in self.records if record.tenant_id == tenant_id)


class UnavailableExecutions:
    def list_for_tenant(self, *, tenant_id: str) -> tuple[AnswerExecutionUsageEvidence, ...]:
        raise RuntimeError(f"execution store unavailable for {tenant_id}")


class Plans:
    def __init__(self, records: tuple[Plan, ...]) -> None:
        self.records: dict[str, AnswerPlanUsageEvidence] = {
            record.plan_digest: record for record in records
        }

    def read(self, *, tenant_id: str, plan_digest: str) -> AnswerPlanUsageEvidence | None:
        record = self.records.get(plan_digest)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record


def test_statement_breach_uses_exact_current_policy_execution_receipts() -> None:
    admissions = Admissions(
        (
            Admission("tenant-1", "old-request", "policy-1", 1, "1" * 64),
            Admission("tenant-1", "current-request", "policy-1", 2, "2" * 64),
            Admission("tenant-1", "successful-request", "policy-1", 2, "3" * 64),
            Admission("tenant-2", "foreign-request", "policy-1", 2, "4" * 64),
        )
    )
    executions = Executions(
        (
            Execution("tenant-1", "old-request", "1" * 64, "ceiling_exceeded"),
            Execution("tenant-1", "current-request", "2" * 64, "ceiling_exceeded"),
            Execution("tenant-1", "successful-request", "3" * 64, "succeeded"),
            Execution("tenant-2", "foreign-request", "4" * 64, "ceiling_exceeded"),
        )
    )
    plans = Plans(
        (
            Plan("tenant-1", "1" * 64, "a" * 64),
            Plan("tenant-1", "2" * 64, "b" * 64),
            Plan("tenant-1", "3" * 64, "c" * 64),
            Plan("tenant-2", "4" * 64, "d" * 64),
        )
    )

    breaches = DurableStatementCeilingBreachReader(
        admissions=admissions,
        executions=executions,
        plans=plans,
    ).list_breached_statement_digests(tenant_id="tenant-1", policy_id="policy-1", policy_revision=2)

    assert breaches == ("b" * 64,)


def test_statement_breach_requires_exact_request_and_plan_binding() -> None:
    reader = DurableStatementCeilingBreachReader(
        admissions=Admissions((Admission("tenant-1", "request-1", "policy-1", 1, "1" * 64),)),
        executions=Executions((Execution("tenant-1", "request-2", "1" * 64, "ceiling_exceeded"),)),
        plans=Plans((Plan("tenant-1", "1" * 64, "a" * 64),)),
    )

    assert (
        reader.list_breached_statement_digests(
            tenant_id="tenant-1", policy_id="policy-1", policy_revision=1
        )
        == ()
    )


def test_statement_breach_rechecks_policy_scope_from_reader() -> None:
    reader = DurableStatementCeilingBreachReader(
        admissions=UnscopedAdmissions(
            (Admission("tenant-1", "request-1", "other-policy", 2, "1" * 64),)
        ),
        executions=Executions((Execution("tenant-1", "request-1", "1" * 64, "ceiling_exceeded"),)),
        plans=Plans((Plan("tenant-1", "1" * 64, "a" * 64),)),
    )

    assert (
        reader.list_breached_statement_digests(
            tenant_id="tenant-1", policy_id="policy-1", policy_revision=2
        )
        == ()
    )


def test_statement_breach_fails_closed_when_plan_authority_is_unavailable() -> None:
    reader = DurableStatementCeilingBreachReader(
        admissions=Admissions((Admission("tenant-1", "request-1", "policy-1", 1, "1" * 64),)),
        executions=Executions((Execution("tenant-1", "request-1", "1" * 64, "ceiling_exceeded"),)),
        plans=Plans(()),
    )

    with pytest.raises(AnswerPolicyUsageUnavailable, match="plan authority"):
        reader.list_breached_statement_digests(
            tenant_id="tenant-1", policy_id="policy-1", policy_revision=1
        )


def test_statement_breach_classifies_unavailable_receipt_reader() -> None:
    reader = DurableStatementCeilingBreachReader(
        admissions=Admissions(()),
        executions=UnavailableExecutions(),
        plans=Plans(()),
    )

    with pytest.raises(AnswerPolicyUsageUnavailable, match="usage evidence"):
        reader.list_breached_statement_digests(
            tenant_id="tenant-1", policy_id="policy-1", policy_revision=1
        )
