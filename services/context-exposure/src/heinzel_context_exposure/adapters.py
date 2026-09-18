from __future__ import annotations

from typing import Protocol

from heinzel_request_management import (
    ConversationAuthorRole,
    ConversationEntry,
    DelegatedRequestProvenance,
    InboxRequest,
)
from pydantic import BaseModel

from .tools import (
    AgentAnswer,
    AgentAnswerExplanation,
    AgentRequestQuery,
    AgentRequestView,
    AskQuestionCommand,
    ListMyRequestsQuery,
    ReplyToClarificationCommand,
)

type _ScopedRequest = (
    AskQuestionCommand | ReplyToClarificationCommand | AgentRequestQuery | ListMyRequestsQuery
)


class RequestInboxReader(Protocol):
    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]: ...

    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...

    def submit_question(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        purpose: str,
        question: str,
        title: str | None = None,
        delegated_agent: DelegatedRequestProvenance | None = None,
    ) -> InboxRequest: ...

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        body: str,
        *,
        expected_revision: int,
        author_role: ConversationAuthorRole | None = None,
    ) -> ConversationEntry: ...


class AgentAnswerReader(Protocol):
    def get_answer(self, query: AgentRequestQuery) -> AgentAnswer | None: ...


class RequestManagementAgentAdapter:
    """Delegate agent request writes to request-management and project its durable state."""

    def __init__(
        self,
        requests: RequestInboxReader,
        *,
        answers: AgentAnswerReader | None = None,
    ) -> None:
        self._requests = requests
        self._answers = answers

    def ask_question(self, command: AskQuestionCommand) -> AgentRequestView:
        command = AskQuestionCommand.model_validate(_payload(command), strict=True)
        _require_provenance_scope(command.provenance.principal_ref, command)
        request = self._requests.submit_question(
            tenant_id=command.tenant_id,
            requester_id=command.provenance.principal_ref,
            title=command.title,
            purpose=command.purpose,
            question=command.question,
            delegated_agent=_delegated_provenance(command),
        )
        return _request_view(request)

    def reply_to_clarification(self, command: ReplyToClarificationCommand) -> AgentRequestView:
        command = ReplyToClarificationCommand.model_validate(_payload(command), strict=True)
        _require_provenance_scope(command.provenance.principal_ref, command)
        request = self._requests.get(command.tenant_id, command.request_id)
        if request.requester_id != command.provenance.principal_ref:
            raise KeyError("request is not visible")
        self._requests.append_conversation(
            command.tenant_id,
            command.request_id,
            command.provenance.principal_ref,
            command.clarification,
            expected_revision=command.expected_revision,
            author_role="requester",
        )
        return _request_view(self._requests.get(command.tenant_id, command.request_id))

    def get_answer(self, query: AgentRequestQuery) -> AgentAnswer | None:
        query = AgentRequestQuery.model_validate(_payload(query), strict=True)
        _require_provenance_scope(query.provenance.principal_ref, query)
        self._require_owned_request(query)
        answers = self._answers
        if answers is None:
            raise RuntimeError("governed answer reader is not configured")
        return answers.get_answer(query)

    def explain_answer(self, query: AgentRequestQuery) -> AgentAnswerExplanation | None:
        answer = self.get_answer(query)
        return None if answer is None else AgentAnswerExplanation.from_answer(answer)

    def list_my_requests(self, query: ListMyRequestsQuery) -> tuple[AgentRequestView, ...]:
        query = ListMyRequestsQuery.model_validate(_payload(query), strict=True)
        _require_provenance_scope(query.principal_ref, query)
        visible = tuple(
            request
            for request in self._requests.list_inbox(query.tenant_id)
            if request.requester_id == query.principal_ref and request.title is not None
        )
        return tuple(_request_view(request) for request in visible[: query.limit])

    def _require_owned_request(self, query: AgentRequestQuery) -> InboxRequest:
        request = self._requests.get(query.tenant_id, query.request_id)
        if request.requester_id != query.provenance.principal_ref:
            raise KeyError("request is not visible")
        return request


def _delegated_provenance(command: AskQuestionCommand) -> DelegatedRequestProvenance:
    provenance = command.provenance
    return DelegatedRequestProvenance(
        delegation_id=provenance.delegation_id,
        principal_ref=provenance.principal_ref,
        agent_client_ref=provenance.agent_client_ref,
        purpose_digest=provenance.purpose_digest,
        delegation_authority_ref=provenance.delegation_authority_ref,
        delegation_authority_revision=provenance.delegation_authority_revision,
        entitlement_snapshot_digest=provenance.entitlement_snapshot_digest,
        policy_id=provenance.policy_id,
        policy_revision=provenance.policy_revision,
        policy_digest=provenance.policy_digest,
        invoked_at=provenance.invoked_at,
    )


def _request_view(request: InboxRequest) -> AgentRequestView:
    if request.title is None:
        raise ValueError("agent-created request must have a title")
    return AgentRequestView(
        request_id=request.request_id,
        title=request.title,
        state=request.state.value,
        revision=request.revision,
        updated_at=request.updated_at,
    )


def _require_provenance_scope(
    principal_ref: str,
    value: _ScopedRequest,
) -> None:
    if principal_ref != value.provenance.principal_ref:
        raise ValueError("request query does not match its invocation provenance scope")


def _payload(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value
