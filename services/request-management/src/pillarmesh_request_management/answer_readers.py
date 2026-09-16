from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .answer_adapters import (
    adapt_compiler_query_plan,
    adapt_runtime_execution_receipt,
    adapt_runtime_result_snapshot,
)
from .answer_errors import GovernedAnswerVerificationError
from .answer_models import (
    AnswerAdmissionEvidence,
    AnswerExecutionEvidence,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    AnswerResultEvidence,
)
from .models import RequestState, TransitionEvent

if TYPE_CHECKING:
    from pillarmesh_compiler import GovernedQueryPlan
    from pillarmesh_runtime import AnswerExecutionReceipt, AnswerResultSnapshot

    from .answer_admission import PolicyAdmissionReceipt


class PolicyAdmissionRepository(Protocol):
    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[PolicyAdmissionReceipt, ...]: ...


class RequestTransitionHistoryReader(Protocol):
    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]: ...


class QueryPlanRepository(Protocol):
    def read(self, tenant_id: str, plan_digest: str) -> GovernedQueryPlan | None: ...


class AnswerResultRepository(Protocol):
    def load_execution(
        self, tenant_id: str, request_id: str
    ) -> tuple[str, AnswerExecutionReceipt] | None: ...

    def read_result(self, tenant_id: str, result_ref: str) -> AnswerResultSnapshot: ...


class SQLitePolicyAdmissionEvidenceReader:
    def __init__(
        self,
        admissions: PolicyAdmissionRepository,
        requests: RequestTransitionHistoryReader,
    ) -> None:
        self._admissions = admissions
        self._requests = requests

    def read_admission(
        self, tenant_id: str, request_id: str, admission_ref: str
    ) -> AnswerAdmissionEvidence | None:
        try:
            receipt = next(
                (
                    candidate
                    for candidate in self._admissions.list_admissions(tenant_id, request_id)
                    if candidate.admission_id == admission_ref
                ),
                None,
            )
            history = self._requests.list_transition_history(tenant_id, request_id)
        except KeyError:
            return None
        if receipt is None:
            return None
        if receipt.plan_digest is None:
            raise GovernedAnswerVerificationError("answer admission does not bind a query plan")
        verifying_revision = _verifying_revision(receipt.request_revision, history)
        return AnswerAdmissionEvidence(
            admission_ref=receipt.admission_id,
            admission_kind="policy",
            tenant_id=receipt.tenant_id,
            request_id=receipt.request_id,
            admission_request_revision=receipt.request_revision,
            verifying_request_revision=verifying_revision,
            validation_digest=receipt.validation_digest,
            plan_digest=receipt.plan_digest,
            policy_id=receipt.policy_id,
            policy_revision=receipt.policy_revision,
            policy_snapshot_digest=receipt.policy_digest,
            entitlement_snapshot_digest=receipt.entitlement_snapshot_digest,
        )


class RepositoryAnswerPlanReader:
    def __init__(self, repository: QueryPlanRepository) -> None:
        self._repository = repository

    def read_plan(self, tenant_id: str, plan_digest: str) -> AnswerPlanEvidence | None:
        plan = self._repository.read(tenant_id, plan_digest)
        if plan is None:
            return None
        generations = tuple(
            AnswerProductGenerationReference.model_validate(
                reference.model_dump(mode="python"), strict=True
            )
            for reference in plan.product_generation_refs
        )
        return adapt_compiler_query_plan(
            plan,
            tenant_id=tenant_id,
            plan_digest=plan_digest,
            product_generation_refs=generations,
        )


class RepositoryAnswerExecutionReader:
    def __init__(
        self,
        repository: AnswerResultRepository,
        *,
        missing_result_error: type[Exception],
    ) -> None:
        self._repository = repository
        self._missing_result_error = missing_result_error

    def read_receipt(
        self,
        tenant_id: str,
        request_id: str,
        receipt_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerExecutionEvidence | None:
        execution = self._repository.load_execution(tenant_id, request_id)
        if execution is None or execution[1].receipt_id != receipt_ref:
            return None
        return adapt_runtime_execution_receipt(
            execution[1],
            tenant_id=tenant_id,
            request_id=request_id,
            plan_digest=plan_digest,
            product_generation_refs=product_generation_refs,
        )

    def read_result(
        self,
        tenant_id: str,
        request_id: str,
        result_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerResultEvidence | None:
        try:
            snapshot = self._repository.read_result(tenant_id, result_ref)
        except self._missing_result_error:
            return None
        return adapt_runtime_result_snapshot(
            snapshot,
            tenant_id=tenant_id,
            request_id=request_id,
            plan_digest=plan_digest,
            product_generation_refs=product_generation_refs,
        )


def _verifying_revision(admission_revision: int, history: tuple[TransitionEvent, ...]) -> int:
    executing_revision = admission_revision + 1
    verifying_revision = executing_revision + 1
    executing = next(
        (event for event in history if event.request_revision == executing_revision), None
    )
    verifying = next(
        (event for event in history if event.request_revision == verifying_revision), None
    )
    if executing is None or (
        executing.from_state is not RequestState.INVESTIGATING
        or executing.to_state is not RequestState.EXECUTING
    ):
        raise GovernedAnswerVerificationError(
            "answer admission lacks its exact executing transition"
        )
    if verifying is None or (
        verifying.from_state is not RequestState.EXECUTING
        or verifying.to_state is not RequestState.VERIFYING
    ):
        raise GovernedAnswerVerificationError(
            "answer admission lacks its exact verifying transition"
        )
    return verifying_revision
