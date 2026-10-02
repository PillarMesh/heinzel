"""The governed answer runtime, composed for a console that has one.

This is the composition the governed answer path needs: the owning answer services around one
durable request repository, with a warehouse provider resolved per engine. It lived under
`tests/acceptance/` until the quickstart image needed it, and the image ships `apps`, `packages`,
`providers` and `services` but not `tests` -- so a console in a container could not reach it.

Nothing here is test-only. The acceptance harness still composes it, and now so can a shipped
console.
"""

from __future__ import annotations

from .authority import CurrentAnswerAuthority, ProductAnswerAuthority
from .product_authority import (
    ApprovedProductAnswerMetadataReader,
    DurableProductAnswerAuthorityReader,
    SourceFreshnessReader,
)
from .runtime import GovernedAnswerRuntime, GovernedAnswerRuntimeConfiguration

__all__ = [
    "ApprovedProductAnswerMetadataReader",
    "CurrentAnswerAuthority",
    "DurableProductAnswerAuthorityReader",
    "GovernedAnswerRuntime",
    "GovernedAnswerRuntimeConfiguration",
    "ProductAnswerAuthority",
    "SourceFreshnessReader",
]
