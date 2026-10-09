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

import os
import shutil
from collections.abc import Mapping
from contextlib import ExitStack, closing, suppress
from pathlib import Path, PurePosixPath
from types import TracebackType

from heinzel_bi_control import (
    DashboardCompositionService,
    DashboardContractVerifier,
    SQLiteDashboardConnectionRepository,
)
from heinzel_provider_postgresql import POSTGRESQL_WAREHOUSE_CONTAINER_PORT
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
    ContractPublishableDashboardReader,
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceActorDirectory,
    InMemoryWorkspacePrincipalDirectory,
    RepositoryCurrentGovernedAnswerReader,
    RepositoryDashboardPublicationReader,
    RepositoryDashboardRevisionReader,
    WorkflowDashboardPublicationCommands,
)
from ..governed_backend import GovernedConsoleBackend
from ..operation_handles import InMemoryOperationHandleRepository
from .access import DemoAccessControl, build_demo_access_control
from .answer_runtime import DemoGovernedAnswer, demo_governed_answer
from .bi_provider import (
    SUPERSET_TLS_DIRECTORY_VARIABLE,
    SUPERSET_WAREHOUSE_TLS_DIRECTORY_VARIABLE,
    DemoWarehouseRoute,
    demo_dashboard_publication,
    record_demo_dashboard_connection,
    share_warehouse_client_material,
)
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
from .dashboard_contract import (
    demo_access_policy_reference,
    demo_dashboard_contract_keys,
    resolve_dashboard_contract_key,
    seed_demo_dashboard_contract,
)
from .managed_warehouse import (
    DemoManagedWarehouse,
    ManagedWarehouseOption,
    provision_demo_managed_warehouse,
)
from .provenance import DemoProvenanceSubject, DemoRequestProvenanceReader
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


def _demo_warehouse_route(
    environment: Mapping[str, str], *, managed: DemoManagedWarehouse | None
) -> DemoWarehouseRoute | None:
    """How Superset reaches the warehouse, when it reaches it differently than the console does.

    `None` on the demonstration's own warehouse, where both reach the same sibling container by the
    same name, and on a managed warehouse this start did not provision, which it cannot reach at
    all.

    On a managed warehouse it needs two things of the deployment, and this returns `None` without
    either: a Superset to publish to, and somewhere inside its container that Superset keeps its own
    copy of the warehouse's client material. `share_warehouse_client_material` publishes that
    material where Superset can read it; the copy is Superset's because libpq refuses a private key
    any group or world can read and only the user presenting it can own such a file.

    `None` means publication reports itself as not delivered, rather than publishing a dashboard
    whose every query would be refused. A published dashboard that cannot read its own product is
    worse than an absent one: the refusal arrives at whoever opens it rather than at whoever
    deployed it.
    """
    if managed is None or managed.private_directory is None:
        return None
    shared = environment.get(SUPERSET_TLS_DIRECTORY_VARIABLE, "").strip()
    inside = environment.get(SUPERSET_WAREHOUSE_TLS_DIRECTORY_VARIABLE, "").strip()
    if not shared or not inside:
        return None
    share_warehouse_client_material(managed.private_directory, destination=Path(shared))
    return DemoWarehouseRoute(
        host=managed.internal_hostname,
        # The port the warehouse listens on inside its own container, not the one its Compose
        # project publishes to the host: a client on the shared network reaches the container.
        port=POSTGRESQL_WAREHOUSE_CONTAINER_PORT,
        tls_directory=PurePosixPath(inside),
    )


class DemoConsole:
    """The governed console backend, assembled over one state directory.

    The services are held as attributes rather than locals because the backend borrows them
    through protocols and keeps no reference of its own to the repository underneath.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        warehouse_dsn: str | None = None,
        managed_warehouse: ManagedWarehouseOption | None = None,
    ) -> None:
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

        `managed_warehouse` is the other way to have a warehouse, and the only one that produces
        a binding warehouse-control owns: given it, the console provisions a warehouse through
        warehouse-control, reports the binding so the setup surface answers and its `foundation`
        stage completes, and answers out of that warehouse. Given neither, the console is what it
        was.

        The managed path answers only in the start that provisioned it. Provisioning rotates the
        administering login to the `administration` operation secret, this demonstration mints
        that secret per start and stores it nowhere, so a later start adopts a `ready` binding it
        holds no credential for. It reports the binding and reports every answer capability as
        not delivered. Discard the state directory, and the containers and volumes named in the
        refusal messages, to demonstrate the answer again.

        The two are exclusive. Each is a complete statement about where the demonstration's
        warehouse comes from, and a console given both would provision one warehouse through
        warehouse-control and answer out of another -- so the setup surface and the answer would
        describe different databases, with nothing saying which the demonstration is about.
        """
        if warehouse_dsn is not None and managed_warehouse is not None:
            raise ValueError(
                "the demonstration takes a warehouse connection or provisions a warehouse "
                "through warehouse-control, not both: they are two answers to where its "
                "warehouse comes from, and nothing here can reconcile them"
            )
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
            # Provisioned before the answer, because on this path it is the warehouse the
            # answer is composed over, and before the backend because the binding the reader
            # projects has to be `ready` by the time the setup surface can be asked for it: a
            # reader over a binding still being provisioned would report the foundation stage
            # incomplete for a warehouse that was on its way, and no console read would ever
            # revisit it.
            managed = (
                None
                if managed_warehouse is None
                else self._closing.enter_context(
                    provision_demo_managed_warehouse(
                        state_dir, option=managed_warehouse, clock=demo_clock
                    )
                )
            )
            self.managed_warehouse = managed
            # Whichever path was taken, this is the warehouse the demonstration answers from.
            # `None` on the managed path means the warehouse exists and this process cannot
            # administer it: provisioning rotated the administering login to a secret an earlier
            # start minted and kept nowhere. The answer capabilities then report themselves as
            # not delivered, which is the same honest answer they give with no warehouse at all,
            # rather than a connection failure during startup.
            answering_dsn = warehouse_dsn if managed is None else managed.administration_dsn
            governed_answer = (
                None
                if answering_dsn is None
                else self._compose_governed_answer(
                    state_dir,
                    warehouse_dsn=answering_dsn,
                    principals=principals,
                    dashboard_route=_demo_warehouse_route(os.environ, managed=managed),
                )
            )
            self.governed_answer = governed_answer
            runtime = None if governed_answer is None else governed_answer.runtime
            # Access-control over the demonstration's own stores. Composed only alongside a
            # governed answer, and for the same reason the publishable dashboards are: the grant is
            # narrowed against the entitlement the answer resolves through, and one tenant,
            # principal and purpose has one authority. A second resolver here would let the answer
            # and the grant be narrowed against different assertions of the same entitlement, with
            # nothing downstream saying which one the requester actually holds. Without an answer
            # there is nothing to grant access to either, so none at all is the honest posture.
            access: DemoAccessControl | None = (
                None
                if governed_answer is None
                else build_demo_access_control(
                    grants=self._stores.access_grants,
                    result_access_connection=self._stores.result_access_connection,
                    requests=self._requests,
                    fulfillment_repository=fulfillment_repository,
                    entitlements=governed_answer.runtime.entitlements,
                    product_ref=governed_answer.preparation.product_ref,
                    bindings=governed_answer.preparation.bindings,
                    clock=demo_clock,
                )
            )
            self.access = access
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
                access_candidate_provider=None if access is None else access.scope_previews,
                data_product_owner_resolver=None if access is None else access.product_owners,
                access_grant_admission_resolver=(
                    None if access is None else access.grant_admission
                ),
                access_grant_activation_reader=None if access is None else access.activation,
                clock=demo_clock,
            )
            self._fulfillment_reads = FulfillmentReadService(
                request_service=self._requests,
                repository=fulfillment_repository,
                authority_role_resolver=role_resolver,
            )
            # The certified dashboard the delivered answer could be published to. Seeded only
            # alongside a composed answer: with no answer there is no product generation for a
            # contract to name, and a contract naming nothing would be offered to nothing.
            publishable_dashboards = (
                None
                if governed_answer is None
                else self._compose_publishable_dashboards(state_dir, answer=governed_answer)
            )
            publication_commands = (
                None
                if governed_answer is None
                else self._compose_dashboard_publication(
                    state_dir, answer=governed_answer, managed=managed
                )
            )
            # How the answer was produced, joined from the receipts each service left behind.
            # Offered only alongside a governed answer, because the query and the execution are
            # read through the runtime the answer owns -- and because with no answer there is no
            # chain to read: the earlier steps would show, and the reader would then be a
            # capability that reports an acquisition and stops.
            request_provenance = (
                None
                if governed_answer is None
                else DemoRequestProvenanceReader(
                    acquisition_lifecycle=self._stores.acquisition_lifecycle,
                    generations=self._stores.generations,
                    landed_generations=self._stores.landed_generations,
                    materializations=self._stores.materialization_receipts,
                    plans=governed_answer.runtime.plans,
                    results=governed_answer.runtime.results,
                    signed_models=self._stores.signed_models,
                    subject=DemoProvenanceSubject(
                        acquisition_contract_ref=publication.contract.contract_id,
                        acquisition_contract_revision=publication.contract.version,
                        product_id=governed_answer.preparation.product_ref.artifact_id,
                        product_revision=governed_answer.preparation.product_ref.version,
                        product_generation=governed_answer.preparation.generation,
                        warehouse=None if managed is None else managed.binding,
                    ),
                )
            )
            self.backend = GovernedConsoleBackend(
                identity=GovernedWorkspaceIdentity(
                    tenant_ref=DEMO_TENANT_ID,
                    tenant_display_name="Demonstration tenant",
                    workspace_ref="workspace-demo",
                    workspace_display_name="Demonstration workspace",
                ),
                operation_handles=InMemoryOperationHandleRepository(),
                # The managed warehouse, read back out of warehouse-control itself. Offered only
                # on that path: on the DSN path the demonstration provisions a database
                # warehouse-control never saw, and a reader there would have to report either
                # nothing or a binding nobody made -- so the capability stays `not_delivered`,
                # which is the honest answer about a warehouse no governing service owns.
                warehouse_bindings=None if managed is None else managed.bindings,
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
                # The approved terms a question may be composed from, read from the composed
                # answer's own bindings so what the builder offers is exactly what the
                # interpreter resolves and the validation admits.
                #
                # Offered only once there is a composed answer, for the same reason the receipts
                # are: without one, nothing can answer a question composed from these terms, and
                # a builder in front of nothing is a form that produces an unanswerable request.
                selectable_answer_terms=governed_answer,
                fulfillment_execution_commands=runtime,
                incidents=None if runtime is None else runtime.incidents,
                answer_results=None if runtime is None else runtime.results,
                verified_answers=None if runtime is None else runtime.answers,
                answer_downloads=None if runtime is None else runtime.downloads,
                # The dashboards the delivered answer could be published to, read from the
                # contracts the demonstration certified. Publication itself is wired only when the
                # deployment named a Superset to publish to, so the offering names what matches and
                # the view says publication is unavailable rather than offering a control that
                # refuses every press.
                publishable_dashboards=publishable_dashboards,
                request_provenance=request_provenance,
                # What was published, read out of bi-control's own repository rather than through a
                # provider. A publication is recorded there only against a provider receipt, so this
                # answers the same question -- and it keeps answering it for a deployment that has
                # since been given no Superset, where a reader built over the control service would
                # report nothing published and something was.
                dashboards=RepositoryDashboardPublicationReader(self._stores.dashboards),
                dashboard_publication_commands=publication_commands,
                # Access-control applies, verifies and revokes the grant an admitted data access
                # request produces, so intake may accept one. All five move together: the
                # workspace card reports data access ready only when every one of them is wired,
                # and an intake that accepted a request the rest could not finish would leave it
                # in the inbox with no action able to move it.
                fulfillment_access_execution_commands=(
                    None if access is None else self._fulfillment
                ),
                access_grant_commands=None if access is None else access.grant_commands,
                access_grants=None if access is None else access.grants,
                access_revocation_commands=(None if access is None else access.revocation_commands),
                data_access_intake_available=access is not None,
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
        dashboard_route: DemoWarehouseRoute | None,
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
            state_dir=state_dir,
            workspace=state_dir / "materialization",
            clock=demo_clock,
            dashboard_route=dashboard_route,
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

    def _compose_publishable_dashboards(
        self, state_dir: Path, *, answer: DemoGovernedAnswer
    ) -> ContractPublishableDashboardReader:
        """Seed the demonstration's certified dashboard contract and read the offering over it.

        The contract is composed from the answer's own product generation and the approved terms
        the publication carries, so the offering names it for exactly the answer this console
        delivers. Composing it from anything else would seed a contract whose product the answer
        never read, which the offering would then correctly never name.
        """
        signing_key = resolve_dashboard_contract_key(state_dir)
        seed_demo_dashboard_contract(
            self._stores.dashboard_contracts,
            tenant_id=DEMO_TENANT_ID,
            signing_key=signing_key,
            semantic_version=self.publication.semantic_version,
            product_ref=answer.preparation.product_ref,
            access_policy_ref=demo_access_policy_reference(answer.preparation.policy),
        )
        return ContractPublishableDashboardReader(
            contracts=self._stores.dashboard_contracts,
            contract_verifier=DashboardContractVerifier(demo_dashboard_contract_keys(signing_key)),
            answers=RepositoryCurrentGovernedAnswerReader(answer.runtime.answer_records),
            answer_authority=answer.runtime.dashboard_answers,
            dashboard_control=RepositoryDashboardRevisionReader(self._stores.dashboards),
            clock=demo_clock,
        )

    def _compose_dashboard_publication(
        self, state_dir: Path, *, answer: DemoGovernedAnswer, managed: DemoManagedWarehouse | None
    ) -> WorkflowDashboardPublicationCommands | None:
        """The publication commands, when the deployment named a Superset to publish to.

        The dataset connection is recorded here, before any publication can be attempted, and only
        on the managed path. It cites the warehouse binding warehouse-control holds, which is the
        one thing the other path cannot produce: ADR-0003 keeps a warehouse that service never
        provisioned out of scope, so a database it never saw has no binding to cite. Recording one
        anyway would make the connection authority a rubber stamp -- bi-control carries that
        citation and verifies none of it -- so the other path records nothing and its publication
        fails as an unavailable authority, which is the truth about it.
        """
        signing_key = resolve_dashboard_contract_key(state_dir)
        # Opened here rather than inside `compose_publisher`, so the console closes it: a store
        # the composition owns privately would outlive every caller able to close it.
        connections = self._closing.enter_context(
            closing(
                SQLiteDashboardConnectionRepository(
                    str(state_dir / "dashboard-connections.sqlite3")
                )
            )
        )
        if managed is not None:
            record_demo_dashboard_connection(
                connections,
                query_bindings=self._stores.query_bindings,
                product_ref=answer.preparation.product_ref,
                generation=answer.preparation.generation,
                warehouse_binding=managed.binding,
            )
        return self._closing.enter_context(
            demo_dashboard_publication(
                os.environ,
                state_dir=state_dir,
                dashboards=self._stores.dashboards,
                database_uri=answer.dashboard_database_uri,
                current_answers=RepositoryCurrentGovernedAnswerReader(
                    answer.runtime.answer_records
                ),
                answer_authority=answer.runtime.dashboard_answers,
                compose_publisher=lambda control: DashboardCompositionService(
                    contracts=self._stores.dashboard_contracts,
                    contract_verifier=DashboardContractVerifier(
                        demo_dashboard_contract_keys(signing_key)
                    ),
                    answers=answer.runtime.dashboard_answers,
                    query_bindings=self._stores.query_bindings,
                    materializations=self._stores.materialization_receipts,
                    product_publications=self._stores.product_publications,
                    connections=connections,
                    dashboard_control=control,
                    clock=demo_clock,
                ),
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
