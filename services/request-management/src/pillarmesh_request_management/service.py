from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pillarmesh_contract_model import digest

from .models import (
    ConversationEntry,
    DataAccessRequest,
    DecisionBinding,
    DecisionKind,
    InboxRequest,
    RequestState,
    StakeholderQuestion,
    TransitionEvent,
)
from .repository import RequestRepository, StaleRevisionError

_TRANSITIONS: dict[RequestState, frozenset[RequestState]] = {
    RequestState.SUBMITTED: frozenset({RequestState.CLARIFYING, RequestState.INVESTIGATING}),
    RequestState.CLARIFYING: frozenset({RequestState.INVESTIGATING, RequestState.SUBMITTED}),
    RequestState.INVESTIGATING: frozenset({RequestState.PROPOSED, RequestState.NO_VALID_PLAN}),
    RequestState.PROPOSED: frozenset({RequestState.AWAITING_APPROVAL, RequestState.INVESTIGATING}),
    RequestState.AWAITING_APPROVAL: frozenset(
        {RequestState.EXECUTING, RequestState.REJECTED, RequestState.INVESTIGATING}
    ),
    RequestState.EXECUTING: frozenset({RequestState.VERIFYING, RequestState.FAILED}),
    RequestState.VERIFYING: frozenset({RequestState.DELIVERED, RequestState.FAILED}),
    RequestState.DELIVERED: frozenset({RequestState.MONITORING, RequestState.RETIRED}),
    RequestState.MONITORING: frozenset({RequestState.RETIRED}),
    RequestState.REJECTED: frozenset(),
    RequestState.NO_VALID_PLAN: frozenset(),
    RequestState.CANCELLED: frozenset(),
    RequestState.FAILED: frozenset(),
    RequestState.RETIRED: frozenset(),
}


class RequestManagementService:
    def __init__(self, repository: RequestRepository, *, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def submit_question(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        purpose: str,
        question: str,
    ) -> InboxRequest:
        now = self._now()
        request = InboxRequest(
            request_id=self._request_id(tenant_id),
            tenant_id=tenant_id,
            requester_id=requester_id,
            payload=StakeholderQuestion(purpose=purpose, question=question),
            state=RequestState.SUBMITTED,
            revision=1,
            submitted_at=now,
            updated_at=now,
        )
        self._repository.save(request)
        return request

    def submit_access_request(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        purpose: str,
        data_product_id: str,
        requested_fields: tuple[str, ...],
        access_mode: Literal["query", "dashboard", "export"],
        expires_at: datetime,
    ) -> InboxRequest:
        now = self._now()
        if expires_at.tzinfo is None or expires_at.utcoffset() is None or expires_at <= now:
            raise ValueError("expires_at must be timezone-aware and after submission")
        request = InboxRequest(
            request_id=self._request_id(tenant_id),
            tenant_id=tenant_id,
            requester_id=requester_id,
            payload=DataAccessRequest(
                purpose=purpose,
                data_product_id=data_product_id,
                requested_fields=requested_fields,
                access_mode=access_mode,
                expires_at=expires_at,
            ),
            state=RequestState.SUBMITTED,
            revision=1,
            submitted_at=now,
            updated_at=now,
        )
        self._repository.save(request)
        return request

    def transition(
        self,
        tenant_id: str,
        request_id: str,
        state: RequestState,
        *,
        actor_id: str,
        expected_revision: int,
    ) -> InboxRequest:
        request = self.get(tenant_id, request_id)
        self._assert_current_revision(request, expected_revision)
        allowed_states = _TRANSITIONS[request.state]
        if state not in allowed_states and (
            state is not RequestState.CANCELLED or request.state in _terminal_states()
        ):
            raise ValueError(f"transition {request.state.value} -> {state.value} is not allowed")
        transitioned_at = self._now()
        try:
            return self._repository.transition(
                tenant_id,
                request_id,
                expected_revision,
                actor_id,
                state,
                transitioned_at,
            )
        except StaleRevisionError as error:
            raise ValueError("request revision is stale") from error

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        body: str,
        *,
        expected_revision: int,
    ) -> ConversationEntry:
        request = self.get(tenant_id, request_id)
        self._assert_current_revision(request, expected_revision)
        try:
            return self._repository.append_conversation(
                tenant_id,
                request_id,
                expected_revision,
                actor_id,
                body,
                self._now(),
            )
        except StaleRevisionError as error:
            raise ValueError("request revision is stale") from error

    def record_decision(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        actor_id: str,
        kind: DecisionKind,
        subject_digest: str,
    ) -> DecisionBinding:
        request = self.get(tenant_id, request_id)
        self._assert_current_revision(request, request_revision)
        try:
            return self._repository.record_decision(
                tenant_id,
                request_id,
                request_revision,
                actor_id,
                kind,
                subject_digest,
                self._now(),
            )
        except StaleRevisionError as error:
            raise ValueError("request revision is stale") from error

    def get(self, tenant_id: str, request_id: str) -> InboxRequest:
        request = self._repository.load(tenant_id, request_id)
        if request is None:
            raise KeyError(f"request {request_id} belongs to another tenant")
        return request

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]:
        return self._repository.list_inbox(tenant_id)

    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]:
        return self._repository.list_transition_history(tenant_id, request_id)

    def _request_id(self, tenant_id: str) -> str:
        sequence = self._repository.next_sequence(tenant_id)
        identity_digest = digest(
            {
                "domain": "pillarmesh-request-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
        return f"req-{sequence:020d}-{identity_digest}"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return timezone-aware UTC")
        return now.astimezone(UTC)

    @staticmethod
    def _assert_current_revision(request: InboxRequest, expected_revision: int) -> None:
        if request.revision != expected_revision:
            raise ValueError("request revision is stale")


def _terminal_states() -> frozenset[RequestState]:
    return frozenset(
        {
            RequestState.REJECTED,
            RequestState.NO_VALID_PLAN,
            RequestState.CANCELLED,
            RequestState.FAILED,
            RequestState.RETIRED,
        }
    )
