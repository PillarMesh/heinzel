"""The lifecycle timeline is written in the vocabulary the rest of the page uses.

Each entry is headed with the console's own request state, which collapses several of
request-management's. The sentence under the heading used to spell request-management's names
instead, so the timeline leaked `awaiting_approval` into prose and showed two entries headed
`execution ready` whose only difference was a transition between two names the console had
deliberately stopped using.
"""

from __future__ import annotations

from heinzel_console.governed_backend import _transition_summary
from heinzel_request_management import RequestState


def test_a_transition_is_described_in_the_states_the_page_shows() -> None:
    summary = _transition_summary(RequestState.PROPOSED, RequestState.AWAITING_APPROVAL)

    assert summary == "The request moved from proposed to awaiting approval."
    assert "_" not in summary


def test_a_step_the_console_does_not_distinguish_is_not_reported_as_a_repeated_move() -> None:
    """`EXECUTING` and `VERIFYING` are both `execution_ready` to a reader.

    Describing the step between them as a move from one state to another produced two entries
    under one heading, which reads as the same event recorded twice.
    """
    summary = _transition_summary(RequestState.EXECUTING, RequestState.VERIFYING)

    assert summary == "The request advanced within execution ready."
