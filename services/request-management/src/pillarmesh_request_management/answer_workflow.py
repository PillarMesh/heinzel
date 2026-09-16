from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from .answer_adapters import adapt_compiler_query_plan, adapt_runtime_execution_receipt
from .answer_errors import (
    GovernedAnswerExecutionAuthorizationDenied,
    GovernedAnswerExecutionAuthorizationUnavailable,
    GovernedAnswerExecutionDeferred,
    GovernedAnswerExecutionFailed,
    GovernedAnswerNotVisible,
    GovernedAnswerStaleRevision,
    GovernedAnswerVerificationError,
)
from .answer_models import (
    AnswerExecutionEvidence,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    DeliverGovernedAnswerCommand,
    ExecuteGovernedAnswerWorkflowCommand,
    GovernedAnswer,
)
from .models import InboxRequest, RequestState, TransitionEvent

if TYPE_CHECKING:
    from pillarmesh_compiler import GovernedQueryPlan
    from pillarmesh_runtime import AnswerExecutionAuthorization, AnswerExecutionReceipt

    from .answer_admission import PolicyAdmissionReceipt


class WorkflowRequestService(Protocol):
    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...

    def transition(
        self,
        tenant_id: str,
        request_id: str,
        to_state: RequestState,
        *,
        actor_id: str,
        expected_revision: int,
    ) -> InboxRequest: ...

    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]: ...


class WorkflowAdmissionRepository(Protocol):
    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[PolicyAdmissionReceipt, ...]: ...


class WorkflowPlanRepository(Protocol):
    def read(self, tenant_id: str, plan_digest: str) -> GovernedQueryPlan | None: ...


class WorkflowExecutionRepository(Protocol):
    def load_execution(
        self, tenant_id: str, request_id: str
    ) -> tuple[str, AnswerExecutionReceipt] | None: ...


class WorkflowExecutor(Protocol):
    def execute(self, *, request_id: str, plan: BaseModel) -> AnswerExecutionReceipt: ...


class WorkflowIncidentProjectionDeferred(RuntimeError):
    pass


class WorkflowIncidentProjector(Protocol):
    def project(self, receipt: AnswerExecutionReceipt) -> object | None: ...


class WorkflowDeliveryService(Protocol):
    def deliver(self, command: DeliverGovernedAnswerCommand) -> GovernedAnswer: ...


class CurrentExecutionAuthorizationRechecker(Protocol):
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
        plan_digest: str,
    ) -> AnswerExecutionAuthorization: ...


class PolicyAdmissionExecutionAuthorizer:
    def __init__(
        self,
        admissions: WorkflowAdmissionRepository,
        current_authority: CurrentExecutionAuthorizationRechecker,
    ) -> None:
        self._admissions = admissions
        self._current_authority = current_authority

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
        plan_digest: str,
    ) -> AnswerExecutionAuthorization:
        try:
            matching = tuple(
                admission
                for admission in self._admissions.list_admissions(tenant_id, request_id)
                if admission.validation_digest == validation_digest
                and admission.plan_digest == plan_digest
            )
        except KeyError:
            matching = ()
        if len(matching) != 1:
            raise GovernedAnswerNotVisible("answer execution authority is not visible")
        admission = matching[0]
        authorization = self._current_authority.recheck(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
            plan_digest=plan_digest,
        )
        if (
            authorization.tenant_id != tenant_id
            or authorization.request_id != request_id
            or authorization.validation_digest != admission.validation_digest
            or authorization.plan_digest != admission.plan_digest
            or authorization.policy_revision != admission.policy_revision
            or authorization.entitlement_digest != admission.entitlement_snapshot_digest
        ):
            raise GovernedAnswerVerificationError(
                "runtime authorization does not match the policy admission"
            )
        return authorization


class GovernedAnswerWorkflow:
    def __init__(
        self,
        *,
        requests: WorkflowRequestService,
        admissions: WorkflowAdmissionRepository,
        plans: WorkflowPlanRepository,
        executions: WorkflowExecutionRepository,
        executor: WorkflowExecutor,
        incident_projector: WorkflowIncidentProjector,
        delivery: WorkflowDeliveryService,
    ) -> None:
        self._requests = requests
        self._admissions = admissions
        self._plans = plans
        self._executions = executions
        self._executor = executor
        self._incident_projector = incident_projector
        self._delivery = delivery

    def execute(self, command: ExecuteGovernedAnswerWorkflowCommand) -> GovernedAnswer:
        request = self._request(command)
        admission = self._admission(command)
        executing_revision = admission.request_revision + 1
        verifying_revision = executing_revision + 1
        if command.expected_revision != executing_revision:
            raise GovernedAnswerStaleRevision("governed answer workflow revision is stale")
        if admission.plan_digest is None:
            raise GovernedAnswerVerificationError("answer admission does not bind a query plan")
        plan = self._plans.read(command.tenant_id, admission.plan_digest)
        if plan is None:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible")
        plan_evidence = self._plan_evidence(
            command, admission.validation_digest, admission.plan_digest, plan
        )
        execution = self._executions.load_execution(command.tenant_id, command.request_id)

        if request.state is RequestState.DELIVERED and request.revision == verifying_revision + 1:
            persisted_receipt = self._successful_receipt(
                command, admission.plan_digest, plan_evidence.product_generation_refs, execution
            )
            return self._deliver(command, verifying_revision, persisted_receipt.receipt_id)
        if request.state is RequestState.FAILED:
            if execution is None:
                history = self._requests.list_transition_history(
                    command.tenant_id, command.request_id
                )
                terminal = history[-1] if history else None
                if terminal is not None and (
                    terminal.from_state is RequestState.EXECUTING
                    and terminal.to_state is RequestState.FAILED
                    and terminal.actor_id == command.actor_id
                    and terminal.request_revision == executing_revision + 1
                ):
                    raise GovernedAnswerExecutionFailed("authorization")
                raise GovernedAnswerStaleRevision("governed answer workflow revision is stale")
            if execution[1].outcome == "succeeded":
                raise GovernedAnswerExecutionFailed("verification")
            raise GovernedAnswerExecutionFailed("execution")
        if request.state is RequestState.VERIFYING and request.revision == verifying_revision:
            persisted_receipt = self._successful_receipt(
                command, admission.plan_digest, plan_evidence.product_generation_refs, execution
            )
            return self._verify_and_deliver(
                command, verifying_revision, persisted_receipt.receipt_id
            )
        if request.state is not RequestState.EXECUTING or request.revision != executing_revision:
            raise GovernedAnswerStaleRevision("governed answer workflow revision is stale")

        try:
            receipt = self._executor.execute(request_id=command.request_id, plan=plan)
        except GovernedAnswerExecutionAuthorizationUnavailable as error:
            raise GovernedAnswerExecutionDeferred("authorization") from error
        except GovernedAnswerExecutionAuthorizationDenied as error:
            self._transition(command, RequestState.FAILED, executing_revision)
            raise GovernedAnswerExecutionFailed("authorization") from error
        receipt_evidence = adapt_runtime_execution_receipt(
            receipt,
            tenant_id=command.tenant_id,
            request_id=command.request_id,
            plan_digest=admission.plan_digest,
            product_generation_refs=plan_evidence.product_generation_refs,
        )
        if receipt_evidence.outcome != "succeeded":
            try:
                self._incident_projector.project(receipt)
            except WorkflowIncidentProjectionDeferred as error:
                raise GovernedAnswerExecutionDeferred("incident projection") from error
            self._transition(command, RequestState.FAILED, executing_revision)
            raise GovernedAnswerExecutionFailed("execution")
        self._transition(command, RequestState.VERIFYING, executing_revision)
        return self._verify_and_deliver(command, verifying_revision, receipt_evidence.receipt_id)

    def _request(self, command: ExecuteGovernedAnswerWorkflowCommand) -> InboxRequest:
        try:
            request = self._requests.get(command.tenant_id, command.request_id)
        except KeyError:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible") from None
        if request.requester_id != command.requester_id:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible")
        return request

    def _admission(self, command: ExecuteGovernedAnswerWorkflowCommand) -> PolicyAdmissionReceipt:
        try:
            admission = next(
                (
                    candidate
                    for candidate in self._admissions.list_admissions(
                        command.tenant_id, command.request_id
                    )
                    if candidate.admission_id == command.admission_ref
                ),
                None,
            )
        except KeyError:
            admission = None
        if admission is None:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible")
        return admission

    @staticmethod
    def _plan_evidence(
        command: ExecuteGovernedAnswerWorkflowCommand,
        validation_digest: str,
        plan_digest: str,
        plan: GovernedQueryPlan,
    ) -> AnswerPlanEvidence:
        generations = tuple(
            AnswerProductGenerationReference.model_validate(
                reference.model_dump(mode="python"), strict=True
            )
            for reference in plan.product_generation_refs
        )
        evidence = adapt_compiler_query_plan(
            plan,
            tenant_id=command.tenant_id,
            plan_digest=plan_digest,
            product_generation_refs=generations,
        )
        if evidence.validation_digest != validation_digest:
            raise GovernedAnswerVerificationError(
                "answer plan validation digest does not match admission"
            )
        return evidence

    def _successful_receipt(
        self,
        command: ExecuteGovernedAnswerWorkflowCommand,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
        execution: tuple[str, AnswerExecutionReceipt] | None,
    ) -> AnswerExecutionEvidence:
        if execution is None:
            raise GovernedAnswerStaleRevision("governed answer execution receipt is missing")
        evidence = adapt_runtime_execution_receipt(
            execution[1],
            tenant_id=command.tenant_id,
            request_id=command.request_id,
            plan_digest=plan_digest,
            product_generation_refs=product_generation_refs,
        )
        if evidence.outcome != "succeeded":
            raise GovernedAnswerExecutionFailed("execution")
        return evidence

    def _verify_and_deliver(
        self,
        command: ExecuteGovernedAnswerWorkflowCommand,
        verifying_revision: int,
        receipt_ref: str,
    ) -> GovernedAnswer:
        try:
            return self._deliver(command, verifying_revision, receipt_ref)
        except (GovernedAnswerNotVisible, GovernedAnswerVerificationError) as error:
            self._transition(command, RequestState.FAILED, verifying_revision)
            raise GovernedAnswerExecutionFailed("verification") from error

    def _deliver(
        self,
        command: ExecuteGovernedAnswerWorkflowCommand,
        verifying_revision: int,
        receipt_ref: str,
    ) -> GovernedAnswer:
        return self._delivery.deliver(
            DeliverGovernedAnswerCommand(
                tenant_id=command.tenant_id,
                request_id=command.request_id,
                request_revision=verifying_revision,
                requester_id=command.requester_id,
                actor_id=command.actor_id,
                admission_ref=command.admission_ref,
                execution_receipt_ref=receipt_ref,
                model_narrative=command.model_narrative,
                refreshes_answer_ref=command.refreshes_answer_ref,
            )
        )

    def _transition(
        self,
        command: ExecuteGovernedAnswerWorkflowCommand,
        state: RequestState,
        expected_revision: int,
    ) -> None:
        try:
            self._requests.transition(
                command.tenant_id,
                command.request_id,
                state,
                actor_id=command.actor_id,
                expected_revision=expected_revision,
            )
        except ValueError:
            raise GovernedAnswerStaleRevision(
                "governed answer workflow revision is stale"
            ) from None
