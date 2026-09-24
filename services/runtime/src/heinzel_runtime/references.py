from __future__ import annotations

import re
import secrets
from collections.abc import Callable

from .acquisition import ReferenceFactory

_KIND = re.compile(r"^[a-z][a-z0-9_]*$")
_TOKEN_BYTES = 24


def opaque_reference_factory(
    entropy: Callable[[int], str] = secrets.token_urlsafe,
) -> ReferenceFactory:
    """Allocate the opaque receipt references the acquisition records refer to.

    `AcquisitionEvidenceReceipt` refuses a reference shaped like a digest, because a
    reference derived from content can be recomputed by a reader and then read as a
    claim about that content. These are allocated instead: the kind is readable so a
    reference can be traced back to what asked for it, and the rest is entropy.

    Uniqueness is the load-bearing property rather than unpredictability.
    `evidence_id` is part of the receipt store's primary key, so a collision is not
    a duplicate but a contradiction -- the second append is refused as a conflicting
    replay, failing a run that did nothing wrong.
    """

    def allocate(kind: str) -> str:
        if not _KIND.fullmatch(kind):
            raise ValueError(f"reference kind must be a lowercase identifier, got {kind!r}")
        return f"{kind.replace('_', '-')}-ref:{entropy(_TOKEN_BYTES)}"

    return allocate
