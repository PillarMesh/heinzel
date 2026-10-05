"""The delivery re-reads the request's transition history rather than trusting the receipt.

`_verifying_revision` is the third place the set of states an admission may be recorded from
is stated: the evaluation's reason code, the transaction that records the receipt, and here.
Each has to agree, and a test of one says nothing about the others -- so this one covers this
one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_request_management import GovernedAnswerVerificationError, RequestState
from heinzel_request_management.answer_readers import _verifying_revision
from heinzel_request_management.models import TransitionEvent

NOW = datetime(2026, 9, 13, tzinfo=UTC)
_ADMISSION_REVISION = 4


def _event(revision: int, *, from_state: RequestState, to_state: RequestState) -> TransitionEvent:
    return TransitionEvent(
        event_id=f"transition-{revision}",
        request_id="request-1",
        request_revision=revision,
        actor_id="architect-a",
        from_state=from_state,
        to_state=to_state,
        created_at=NOW,
    )


def _history(*, executing_from: RequestState) -> tuple[TransitionEvent, ...]:
    return (
        _event(
            _ADMISSION_REVISION + 1,
            from_state=executing_from,
            to_state=RequestState.EXECUTING,
        ),
        _event(
            _ADMISSION_REVISION + 2,
            from_state=RequestState.EXECUTING,
            to_state=RequestState.VERIFYING,
        ),
    )


@pytest.mark.parametrize(
    "executing_from",
    (RequestState.AWAITING_APPROVAL, RequestState.INVESTIGATING),
)
def test_either_admissible_state_delivers(executing_from: RequestState) -> None:
    """A reviewed proposal and an investigation both reach execution through an admission."""
    assert (
        _verifying_revision(_ADMISSION_REVISION, _history(executing_from=executing_from))
        == _ADMISSION_REVISION + 2
    )


@pytest.mark.parametrize(
    "executing_from",
    (RequestState.PROPOSED, RequestState.CLARIFYING, RequestState.SUBMITTED),
)
def test_any_other_route_to_executing_is_refused(executing_from: RequestState) -> None:
    """A request that reached execution another way was not admitted, whatever a receipt says."""
    with pytest.raises(GovernedAnswerVerificationError, match="executing transition"):
        _verifying_revision(_ADMISSION_REVISION, _history(executing_from=executing_from))


def test_a_history_with_no_executing_transition_is_refused() -> None:
    with pytest.raises(GovernedAnswerVerificationError, match="executing transition"):
        _verifying_revision(_ADMISSION_REVISION, ())


def test_a_history_with_no_verifying_transition_is_refused() -> None:
    """Delivery follows verification, so a receipt without one describes an undelivered answer."""
    executing_only = _history(executing_from=RequestState.AWAITING_APPROVAL)[:1]

    with pytest.raises(GovernedAnswerVerificationError, match="verifying transition"):
        _verifying_revision(_ADMISSION_REVISION, executing_only)
