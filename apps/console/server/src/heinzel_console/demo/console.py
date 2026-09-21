"""The demonstration console: the governed backend over the demonstration's own stores.

This is a demonstration, not a deployment. It has no authentication: the browser names the
actor it wants in a request header, and anything unrecognised is the architect. That is a
deliberate property of a local demonstration and is not a security boundary.

Only the collaborators the demonstrated journey needs are supplied. Every other collaborator
stays `None`, so the console reports those capabilities as not delivered — which is the
honest answer, because this demonstration does not deliver them.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from types import TracebackType

from heinzel_request_management import (
    FulfillmentReadService,
    FulfillmentService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
)
from starlette.applications import Starlette
from starlette.requests import Request

from ..app import create_app
from ..auth import TrustedActorContext
from ..governed_adapters import (
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceActorDirectory,
    InMemoryWorkspacePrincipalDirectory,
)
from ..governed_backend import GovernedConsoleBackend
from ..operation_handles import InMemoryOperationHandleRepository
from .collaborators import (
    DEMO_ARCHITECT_ID,
    DEMO_ARCHITECT_PRINCIPAL_REF,
    DEMO_REQUESTER_ID,
    DEMO_REQUESTER_PRINCIPAL_REF,
    DemoAnswerCandidateProvider,
    DemoRoleResolver,
    build_demo_policy_compiler,
    build_demo_snapshot_resolver,
    demo_clock,
)
from .publication import DEMO_TENANT_ID, build_demo_publication
from .stores import DemoStores

__all__ = ["DEMO_ACTOR_HEADER", "DemoConsole"]

# The header a local browser names its actor with. There is no authentication here, so this
# header is a convenience for switching between the two demonstration roles, never a claim
# the console trusts about a real identity.
DEMO_ACTOR_HEADER = "x-heinzel-actor"

_ARCHITECT_CONTEXT = TrustedActorContext(
    tenant_id=DEMO_TENANT_ID,
    actor_id=DEMO_ARCHITECT_ID,
    roles=("data_architect",),
    active_role="data_architect",
    session_id="session-demo-architect",
)
_REQUESTER_CONTEXT = TrustedActorContext(
    tenant_id=DEMO_TENANT_ID,
    actor_id=DEMO_REQUESTER_ID,
    roles=("requester",),
    active_role="requester",
    session_id="session-demo-requester",
)


class DemoConsole:
    """The governed console backend, assembled over one state directory.

    The services are held as attributes rather than locals because the backend borrows them
    through protocols and keeps no reference of its own to the repository underneath.
    """

    def __init__(self, state_dir: Path) -> None:
        self._stores = DemoStores(state_dir)
        try:
            publication = build_demo_publication(self._stores, clock=demo_clock)
            role_resolver = DemoRoleResolver()
            fulfillment_repository = SQLiteFulfillmentRepository(self._stores.requests)
            self._requests = RequestManagementService(self._stores.requests, clock=demo_clock)
            self._fulfillment = FulfillmentService(
                request_service=self._requests,
                repository=fulfillment_repository,
                snapshot_resolver=build_demo_snapshot_resolver(
                    publications=self._stores.publications,
                    publication=publication,
                    clock=demo_clock,
                ),
                answer_candidate_provider=DemoAnswerCandidateProvider(publication=publication),
                policy_compiler=build_demo_policy_compiler(),
                authority_role_resolver=role_resolver,
                clock=demo_clock,
            )
            self._fulfillment_reads = FulfillmentReadService(
                request_service=self._requests,
                repository=fulfillment_repository,
                authority_role_resolver=role_resolver,
            )
            actors = InMemoryWorkspaceActorDirectory()
            actors.bind_actor(
                tenant_id=DEMO_TENANT_ID,
                actor_id=DEMO_ARCHITECT_ID,
                display_name="Data engineering architect",
            )
            actors.bind_actor(
                tenant_id=DEMO_TENANT_ID,
                actor_id=DEMO_REQUESTER_ID,
                display_name="Requester",
            )
            principals = InMemoryWorkspacePrincipalDirectory()
            principals.bind_principal(
                tenant_id=DEMO_TENANT_ID,
                actor_id=DEMO_ARCHITECT_ID,
                role="data_architect",
                principal_ref=DEMO_ARCHITECT_PRINCIPAL_REF,
            )
            principals.bind_principal(
                tenant_id=DEMO_TENANT_ID,
                actor_id=DEMO_REQUESTER_ID,
                role="requester",
                principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
            )
            self.publication = publication
            self.backend = GovernedConsoleBackend(
                identity=GovernedWorkspaceIdentity(
                    tenant_ref=DEMO_TENANT_ID,
                    tenant_display_name="Demonstration tenant",
                    workspace_ref="workspace-demo",
                    workspace_display_name="Demonstration workspace",
                ),
                operation_handles=InMemoryOperationHandleRepository(),
                requests=self._requests,
                request_commands=self._requests,
                fulfillment=self._fulfillment_reads,
                fulfillment_commands=self._fulfillment,
                fulfillment_preparation_commands=self._fulfillment,
                # The demonstration delivers no grant application, expiry or revocation, so
                # its own workspace card reports data access as not delivered. Intake must
                # fail closed to match it: accepted, such a request clears intake and
                # clarification and is then refused at preparation with advice to reload
                # that cannot help, leaving a request in the inbox no action can move.
                data_access_intake_available=False,
                actors=actors,
                principals=principals,
                clock=demo_clock,
            )
        except BaseException:
            # Best-effort clean-up: a failure to close must not replace the failure to build.
            with suppress(Exception):
                self._stores.close()
            raise

    def build_app(self, *, origin: str, dist: Path | None = None) -> Starlette:
        """The console application, serving this backend at `origin`."""
        return create_app(
            backend=self.backend,
            context_provider=_actor_context,
            allowed_origin=origin,
            dist_directory=dist,
        )

    def inbox_request_ids(self) -> tuple[str, ...]:
        """Every request identifier the architect's inbox shows, in inbox order.

        The inbox holds a request from intake onwards, whatever its state, so this is the
        whole inbox rather than only what is waiting on a decision.
        """
        return tuple(item.request_id for item in self.backend.get_inbox(_ARCHITECT_CONTEXT).items)

    def close(self) -> None:
        """Close every store this console opened."""
        self._stores.close()

    def __enter__(self) -> DemoConsole:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _actor_context(request: Request) -> TrustedActorContext:
    """The actor the request names, defaulting to the architect.

    An absent or unrecognised header is the architect rather than an error, so a browser
    opened at the console with no header in hand lands on the reviewing role instead of a
    failure page.
    """
    if request.headers.get(DEMO_ACTOR_HEADER) == DEMO_REQUESTER_ID:
        return _REQUESTER_CONTEXT
    return _ARCHITECT_CONTEXT
