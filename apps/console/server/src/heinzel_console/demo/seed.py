"""The one request the demonstration console starts with.

A console that opens on an empty inbox demonstrates nothing, so the demonstration seeds the
stakeholder question it is built around and stops there. Clarifying it, preparing a proposal
and approving it are the demonstration itself, and are left to the person running it.

The question is submitted through the request service's own transaction; none of it is
console state. Nothing in this package imports from `tests/`, and no test module is executed
at runtime.
"""

from __future__ import annotations

from .collaborators import DEMO_REQUESTER_ID
from .console import DemoConsole
from .publication import DEMO_QUESTION, DEMO_TENANT_ID

__all__ = ["seed_demo_request"]

# Why the requester is asking. The demonstration's question is an operational one, so the
# purpose the architect reads beside it is an operational purpose.
_DEMO_PURPOSE = "weekly operations review"


def seed_demo_request(console: DemoConsole) -> None:
    """Leave one stakeholder question waiting in the architect's inbox.

    This returns without writing anything when the inbox already holds a request, so
    restarting the demonstration over an existing state directory does not pile up
    duplicates of the same question.
    """
    if console.inbox_request_ids():
        return
    console.requests.submit_question(
        tenant_id=DEMO_TENANT_ID,
        requester_id=DEMO_REQUESTER_ID,
        purpose=_DEMO_PURPOSE,
        question=DEMO_QUESTION,
        title=DEMO_QUESTION,
    )
