"""The one request the demonstration console starts with.

A console that opens on an empty inbox demonstrates nothing, so the demonstration seeds the
stakeholder question it is built around and stops there. Clarifying it, preparing a proposal
and approving it are the demonstration itself, and are left to the person running it.

The question is submitted through the request service's own transaction; none of it is
console state. Nothing in this package imports from `tests/`, and no test module is executed
at runtime.
"""

from __future__ import annotations

from heinzel_request_management import RequestManagementService, StakeholderQuestion

from .collaborators import DEMO_REQUESTER_ID
from .publication import DEMO_QUESTION, DEMO_TENANT_ID

__all__ = ["seed_demo_request"]

# Why the requester is asking. The demonstration's question is an operational one, so the
# purpose the architect reads beside it is an operational purpose.
_DEMO_PURPOSE = "weekly operations review"


def seed_demo_request(requests: RequestManagementService) -> None:
    """Leave the demonstration's own stakeholder question waiting in the inbox.

    This returns without writing anything when that question is already there, so
    restarting the demonstration over an existing state directory does not pile up
    duplicates of it.
    """
    if _demonstration_question_is_present(requests):
        return
    requests.submit_question(
        tenant_id=DEMO_TENANT_ID,
        requester_id=DEMO_REQUESTER_ID,
        purpose=_DEMO_PURPOSE,
        question=DEMO_QUESTION,
        title=DEMO_QUESTION,
    )


def _demonstration_question_is_present(requests: RequestManagementService) -> bool:
    """Whether the inbox already holds the question this seed submits.

    The test is the question itself rather than whether the inbox holds anything, because a
    person can submit their own question before the demonstration is ever seeded — with
    seeding off, or over a state directory carried from an earlier session. A guard on an
    empty inbox would then never seed, silently, and the demonstration would open on a
    question it cannot ground.

    `InboxRequest` carries no field the demonstration owns purely as a marker: the title,
    the purpose and the question are all text a person reads, and `delegated_agent` asserts
    that an agent acted for the requester, which is not true here and must not be claimed.
    So the seeded request is recognised by its own requester and its exact question text.

    The match is on the question, not on the state it reached, so a seeded request driven
    to a terminal state — rejected, no valid plan, cancelled or failed — still matches, and
    a later restart leaves that dead request in place rather than seeding a live one. The
    way out is to discard the state directory, which `docker compose down -v` does.
    """
    return any(
        request.requester_id == DEMO_REQUESTER_ID
        and isinstance(request.payload, StakeholderQuestion)
        and request.payload.question == DEMO_QUESTION
        for request in requests.list_inbox(DEMO_TENANT_ID)
    )
