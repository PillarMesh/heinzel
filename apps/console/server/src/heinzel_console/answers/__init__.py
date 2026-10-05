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
from .policy_authority import (
    LOCAL_CONNECTED_AUTHORITY_REF,
    LOCAL_SIGNING_KEY_REF,
    LocalDevelopmentPolicyAuthority,
    LocalSignedPolicyAuthority,
    create_local_policy_server,
    local_signed_policy_authority,
)
from .product_authority import (
    ApprovedProductAnswerMetadataReader,
    DurableProductAnswerAuthorityReader,
    MaterializationReceiptReader,
    SourceFreshnessReader,
)
from .query_binding import (
    DurableProductQueryBindingReader,
    GovernedQueryBindingProjection,
    ProductQueryBindingRepositoryReader,
    ProductQueryBindingUnavailable,
)
from .runtime import GovernedAnswerRuntime, GovernedAnswerRuntimeConfiguration

__all__ = [
    "LOCAL_CONNECTED_AUTHORITY_REF",
    "LOCAL_SIGNING_KEY_REF",
    "ApprovedProductAnswerMetadataReader",
    "CurrentAnswerAuthority",
    "DurableProductAnswerAuthorityReader",
    "DurableProductQueryBindingReader",
    "GovernedAnswerRuntime",
    "GovernedAnswerRuntimeConfiguration",
    "GovernedQueryBindingProjection",
    "LocalDevelopmentPolicyAuthority",
    "LocalSignedPolicyAuthority",
    "MaterializationReceiptReader",
    "ProductAnswerAuthority",
    "ProductQueryBindingRepositoryReader",
    "ProductQueryBindingUnavailable",
    "SourceFreshnessReader",
    "create_local_policy_server",
    "local_signed_policy_authority",
]
