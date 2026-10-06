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

import shutil
from contextlib import ExitStack, suppress
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
from .answer_runtime import DemoGovernedAnswer, demo_governed_answer
from .bootstrap import ensure_demo_generation
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
from .cursor_cipher import DemoCursorCipher
from .publication import DEMO_TENANT_ID, build_demo_publication
from .seed import seed_demo_request
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

    def __init__(self, state_dir: Path, *, warehouse_dsn: str | None = None) -> None:
        """Assemble the console, and its governed answer when it is given a warehouse.

        `warehouse_dsn` is a superuser connection to an otherwise empty database. Given one, the
        demonstration provisions it, acquires and lands its seeded source, materializes and
        publishes a product, and composes the governed answer over it -- so a question admitted
        in the console is answered from that product. Given none, the console is what it was:
        every answer capability reports itself as not delivered, which is the honest answer when
        there is no warehouse to answer from.

        The DSN is a parameter rather than an argument of the command that starts the console,
        because it carries a password and a command's arguments are readable from the process
        table.
        """
        # The cipher is named here rather than inside `DemoStores`, because which cipher
        # seals a deployment's cursors is the deployment's answer and not the store's. This
        # demonstration's answer is a key beside its own state; a deployment names the one
        # its key custody answers for.
        self._stores = DemoStores(state_dir, cursor_cipher_factory=DemoCursorCipher)
        self._closing = ExitStack()
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
            governed_answer = (
                None
                if warehouse_dsn is None
                else self._compose_governed_answer(
                    state_dir,
                    warehouse_dsn=warehouse_dsn,
                    principals=principals,
                )
            )
            self.governed_answer = governed_answer
            runtime = None if governed_answer is None else governed_answer.runtime
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
                # A question's plan is admitted through the governed answer, because that
                # admission is the one that leaves a plan behind for the execution to run.
                answer_admission_commands=(
                    None
                    if governed_answer is None
                    else governed_answer.admission_commands(self._requests)
                ),
                # The receipts the governed acquisition composed at startup. The evidence
                # store answers this read itself, so nothing stands between the console and
                # what the acquisition recorded.
                #
                # Offered only once there is a warehouse, because only then has anything been
                # acquired. The store opens either way, so wiring it unconditionally would
                # answer a console that never acquired with an empty list -- a capability that
                # looks delivered and holds nothing, which is what `not_delivered` exists to
                # say instead.
                acquisition_receipts=(
                    None if governed_answer is None else self._stores.acquisition_evidence
                ),
                fulfillment_execution_commands=runtime,
                incidents=None if runtime is None else runtime.incidents,
                answer_results=None if runtime is None else runtime.results,
                verified_answers=None if runtime is None else runtime.answers,
                answer_downloads=None if runtime is None else runtime.downloads,
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
                self._closing.close()
            with suppress(Exception):
                self._stores.close()
            raise

    def _compose_governed_answer(
        self,
        state_dir: Path,
        *,
        warehouse_dsn: str,
        principals: InMemoryWorkspacePrincipalDirectory,
    ) -> DemoGovernedAnswer:
        """Bring the warehouse to a published generation and compose the answer over it.

        `dbt` is looked up rather than assumed: the materialization runs it as a subprocess, and
        a console that started without it would provision a warehouse and then fail partway
        through materializing, leaving the two to be discarded together.
        """
        dbt_executable = shutil.which("dbt")
        if dbt_executable is None:
            raise RuntimeError(
                "the demonstration's warehouse needs the locked dbt executable on PATH to "
                "materialize its product"
            )
        generation = ensure_demo_generation(
            bootstrap_dsn=warehouse_dsn,
            stores=self._stores,
            publication=self.publication,
            dbt_executable=Path(dbt_executable),
            workspace=state_dir / "materialization",
            clock=demo_clock,
        )
        return self._closing.enter_context(
            demo_governed_answer(
                state_dir / "answers",
                stores=self._stores,
                requests=self._requests,
                principals=principals,
                publication=self.publication,
                generation=generation,
                principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
                clock=demo_clock,
            )
        )

    def seed_demonstration_request(self) -> None:
        """Leave the demonstration's own question waiting, if it is not already there.

        The request service itself stays private. Exposing it would let any holder of a
        console drive requests directly, past the session, origin, CSRF and actor checks
        the HTTP surface enforces. This is the one write the demonstration needs before a
        browser connects, so it is the only one the console offers.
        """
        seed_demo_request(self._requests)

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
        """Stop the governed answer's own services, then close every store this console opened.

        In that order: the entitlement authority serves over loopback for as long as the console
        does, and stopping it after the stores would leave it answering from handles that had
        already been released.
        """
        try:
            self._closing.close()
        finally:
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
