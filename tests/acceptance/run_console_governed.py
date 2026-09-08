"""Stand the data architect console up in `governed_local` mode.

This composes the console against the owning services themselves -- warehouse-control,
request-management, fulfillment, and the semantic registry -- with their state in real
SQLite files on disk, and serves it on loopback behind the compiled browser bundle. It
is the acceptance run of `docs/console/acceptance-run.md` made interactive: the same
wiring, driven by a person instead of a test.

**This is not a live claim.** The warehouse provider is the local-acceptance harness,
not a real engine: it returns local-acceptance grade validation evidence that
`LocalAcceptanceWarehouseReadinessPolicy` admits, so a binding reaches `ready` without
any container existing. Every other transaction is the owning service's own. Read
`docs/console/known-gaps.md` before reading any screen as coverage.

**This harness has no authentication.** The actor is chosen by a request header so one
browser can walk both the architect and the requester surface, which is exactly the
thing a deployment must never do. It refuses to bind anywhere but loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pillarmesh_catalog_control import CatalogControlService, SQLiteCatalogRepository
from pillarmesh_console.app import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.governed_adapters import (
    CatalogControlBindingReader,
    DerivedTenantRunReader,
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceBindingDirectory,
    InMemoryWorkspacePrincipalDirectory,
    PolicyPermittedDataProductReader,
    WarehouseControlBindingReader,
    WarehouseControlLifecycleCommands,
    WarehouseRepositoryOperationReader,
)
from pillarmesh_console.governed_backend import GovernedConsoleBackend
from pillarmesh_console.operation_handles import InMemoryOperationHandleRepository
from pillarmesh_contract_model import digest
from pillarmesh_contract_service import (
    SQLiteAcquisitionContractLifecycleRepository,
)
from pillarmesh_evidence import SQLiteStore
from pillarmesh_request_management import (
    FulfillmentPolicyCompiler,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
)
from pillarmesh_semantic_registry import SemanticFulfillmentSnapshotAdapter
from pillarmesh_semantic_registry.repository import SQLiteSemanticRepository
from pillarmesh_semantic_registry.review import SemanticReviewService
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseControlService,
    WarehouseLifecycleOrchestrator,
    WarehouseProvider,
    WarehouseProvisionResult,
    WarehouseRestoreVerification,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pillarmesh_warehouse_control.repository import SQLiteWarehouseRepository
from starlette.applications import Starlette
from starlette.requests import Request

from tests.acceptance.run_plan3b import (
    NOW,
    ScenarioAnswerProvider,
    ScenarioAuthorityResolver,
    ScenarioFreshness,
    published_repository,
)

TENANT = "tenant-a"
ARCHITECT = "architect-a"
REQUESTER = "requester-a"
REQUESTER_PRINCIPAL = f"principal:{REQUESTER}"
ARCHITECT_PRINCIPAL = "role:data_engineering_architect"
ACTOR_HEADER = "x-pillarmesh-actor"

_DEFAULT_PORT = 8000
# Not `localhost`: the allowed origin is built from whichever spelling is bound, and a
# browser opened at the other spelling sends an origin that does not match, so every
# read works while every command is refused `same_origin_required`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def default_state_directory() -> Path:
    """Where the owning services keep their databases when none is named.

    Deliberately outside the repository: the repository-structure gate admits a
    fixed set of root entries, so a state directory dropped beside `services/`
    would fail it for everyone. This state is throwaway by nature - delete the
    directory to start the scenario again.

    Named per user because `tempfile.gettempdir()` is `/tmp` on Linux and in CI. A
    shared name there lands a second user on a private directory the first owns,
    where the secret store's owner check fails and the orchestrator reports it as
    `invalid_provider_response`, a permanent failure that names no cause.
    """
    return Path(tempfile.gettempdir()) / f"pillarmesh-governed-local-{os.getuid()}"


def _clock() -> datetime:
    """The scenario's clock.

    The semantic publication, its authority observation, and the freshness evaluator
    are all pinned to this instant, so a wall clock would make the seeded grounding
    stale on arrival and every proposal would compile to `No Valid Plan`.
    """
    return NOW


def _worker_thread_connection(database_path: str) -> sqlite3.Connection:
    """A connection a threadpool worker may use.

    Command routes run the backend in a threadpool and SQLite connections carry
    thread affinity, so composing the application is where the thread-tolerant
    connection belongs. Both owning repositories accept an injected connection.
    """
    return sqlite3.connect(database_path, check_same_thread=False)


class LocalAcceptanceWarehouseProvider:
    """The smallest provider that carries a binding to a proven ready state.

    Its evidence is local-acceptance grade on purpose, and only
    `LocalAcceptanceWarehouseReadinessPolicy` admits it, so nothing reachable from
    this harness can claim production validation.
    """

    engine_kind = EngineKind.POSTGRESQL

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._result(binding, operation)

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return self._result(binding, operation)

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> InitialWarehouseValidationResult:
        del resume
        restore = WarehouseRestoreVerification(
            verification_id="wrv-console-governed-local",
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            engine_kind=binding.engine_kind,
            source_backup_artifact_digest="3" * 64,
            representative_data_digest="4" * 64,
            schema_metadata_digest="5" * 64,
            principal_profile_digest="6" * 64,
            integrity_marker_digest="7" * 64,
            query_behavior_digest="8" * 64,
            verified_at=_clock(),
        )
        return InitialWarehouseValidationResult(
            evidence=WarehouseValidationEvidence(
                evidence_id="wev-console-governed-local",
                tenant_id=binding.tenant_id,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
                engine_kind=binding.engine_kind,
                engine_version="1.0",
                engine_build_digest="9" * 64,
                engine_image_digest="a" * 64,
                principal_profile_digest=restore.principal_profile_digest,
                namespace_grant_matrix_digest="b" * 64,
                tls_probe_digest="c" * 64,
                network_isolation_probe_digest="d" * 64,
                encryption_at_rest_evidence_digest="e" * 64,
                encryption_at_rest_disposition=(
                    EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE
                ),
                positive_probe_digest="f" * 64,
                denial_probe_digest="0" * 64,
                ledger_probe_digest="1" * 64,
                monitoring_probe_digest="2" * 64,
                capacity_alert_probe_digest="3" * 64,
                backup_artifact_digest=restore.source_backup_artifact_digest,
                restore_verification_digest=digest(restore),
                restore_cleanup_digest="4" * 64,
                observed_at=_clock(),
            ),
            restore_verification=restore,
        )

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        raise NotImplementedError("this harness never suspends a binding")

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None:
        raise NotImplementedError("this harness never resumes a binding")

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence:
        raise NotImplementedError("this harness never retires a binding")

    @staticmethod
    def _result(
        binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult:
        return WarehouseProvisionResult(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=operation.binding_revision,
            operation_id=operation.operation_id,
            engine_kind=binding.engine_kind,
            private_resource_handle="private://local-acceptance/credential-canary",
            provider_build_digest="1" * 64,
            resource_inventory_digest="2" * 64,
        )


class _DeploymentRoleResolver:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == TENANT and (actor_id, authority_ref) in {
            (REQUESTER, REQUESTER_PRINCIPAL),
            (ARCHITECT, ARCHITECT_PRINCIPAL),
        }


def _context(actor: str) -> TrustedActorContext:
    if actor == REQUESTER:
        return TrustedActorContext(
            tenant_id=TENANT,
            actor_id=REQUESTER,
            roles=("requester",),
            active_role="requester",
            session_id="session-requester",
        )
    return TrustedActorContext(
        tenant_id=TENANT,
        actor_id=ARCHITECT,
        roles=("data_architect",),
        active_role="data_architect",
        session_id="session-architect",
    )


@dataclass(frozen=True, slots=True)
class SeededDecision:
    """What `seed` committed, named so a caller can navigate straight to it."""

    request_id: str
    proposal_digest: str
    """The digest of the proposal subject an approving authority must sign."""
    data_product_ref: str
    """The data product the seeded policy permits, so a caller can read it back."""


class _PersistedWorkspaceBindingDirectory(InMemoryWorkspaceBindingDirectory):
    """The workspace's binding directory, kept across restarts.

    Which binding a workspace uses is deployment configuration, not something an
    owning service publishes - neither warehouse-control nor catalog-control
    enumerates bindings, because enumerating them would itself be a disclosure. Held
    only in memory it was lost on restart: the warehouse binding became unreachable
    though warehouse-control still held it, and the catalog binding was worse,
    because the harness minted a fresh draft each time and left the previous one
    orphaned.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._recorded: dict[str, dict[str, str]] = {"warehouse": {}, "catalog": {}}
        if path.exists():
            self._recorded = json.loads(path.read_text(encoding="utf-8"))
            for tenant_id, binding_id in self._recorded["warehouse"].items():
                super().bind_warehouse(tenant_id=tenant_id, binding_id=binding_id)
            for tenant_id, binding_id in self._recorded["catalog"].items():
                super().bind_catalog(tenant_id=tenant_id, binding_id=binding_id)

    def bind_warehouse(self, *, tenant_id: str, binding_id: str) -> None:
        super().bind_warehouse(tenant_id=tenant_id, binding_id=binding_id)
        self._record("warehouse", tenant_id, binding_id)

    def bind_catalog(self, *, tenant_id: str, binding_id: str) -> None:
        super().bind_catalog(tenant_id=tenant_id, binding_id=binding_id)
        self._record("catalog", tenant_id, binding_id)

    def _record(self, kind: str, tenant_id: str, binding_id: str) -> None:
        self._recorded[kind][tenant_id] = binding_id
        self._path.write_text(
            json.dumps(self._recorded, indent=2, sort_keys=True), encoding="utf-8"
        )


class GovernedConsoleDeployment:
    """The owning services, their databases, and the console that projects them."""

    def __init__(self, directory: Path, *, engine: str = "local-acceptance") -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.warehouse_path = str(directory / "warehouse.sqlite3")
        self.request_path = str(directory / "requests.sqlite3")
        self._warehouse_connection = _worker_thread_connection(self.warehouse_path)
        self.warehouse_repository = SQLiteWarehouseRepository(connection=self._warehouse_connection)
        # `CatalogControlBindingReader` has been built since Plan 2 and was never
        # composed, so the console reported the capability as unwired rather than
        # undelivered. The catalog binding is created here rather than by a console
        # command, because the console contract carries no catalog command.
        self.catalog_path = str(directory / "catalog.sqlite3")
        self.catalog_repository = SQLiteCatalogRepository(
            connection=_worker_thread_connection(self.catalog_path)
        )
        self.catalog = CatalogControlService(self.catalog_repository, clock=_clock)
        self.request_repository = SQLiteRequestRepository(
            _worker_thread_connection(self.request_path), _owns_connection=True
        )
        self.fulfillment_repository = SQLiteFulfillmentRepository(self.request_repository)
        self.control = WarehouseControlService(
            self.warehouse_repository,
            clock=_clock,
            readiness_policy=LocalAcceptanceWarehouseReadinessPolicy(),
        )
        self.orchestrator = WarehouseLifecycleOrchestrator(
            control=self.control,
            repository=self.warehouse_repository,
            provider=self._provider(engine),
            clock=_clock,
        )
        self.requests = RequestManagementService(self.request_repository, clock=_clock)
        # Both semantic-review seams existed on the governed backend and neither was
        # wired, so the console reported the capability as unwired for a service that
        # has been implemented since Plan 2. The reader is the repository, because
        # `load_review_bundle` is a repository read; the command is the service, which
        # is what checks the deciding actor's authority.
        self.semantic_path = str(directory / "semantic.sqlite3")
        self.semantic_repository = SQLiteSemanticRepository(
            connection=_worker_thread_connection(self.semantic_path)
        )
        self.semantic_reviews = SemanticReviewService(
            semantic_repository=self.semantic_repository,
            request_service=self.requests,
            clock=_clock,
        )
        publication_repository, receipt, integration_contract = published_repository(
            check_same_thread=False
        )
        self.publication_repository = publication_repository
        self.fulfillment = FulfillmentService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            snapshot_resolver=SemanticFulfillmentSnapshotAdapter(
                publication_repository=publication_repository,
                authority_resolver=ScenarioAuthorityResolver(
                    receipt.publication_id, integration_contract
                ),
                clock=_clock,
            ),
            answer_candidate_provider=ScenarioAnswerProvider(),
            authority_role_resolver=_DeploymentRoleResolver(),
            policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=ScenarioFreshness()),
            clock=_clock,
        )
        self.reads = FulfillmentReadService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            authority_role_resolver=_DeploymentRoleResolver(),
        )
        # The two reads a tenant-scoped run listing derives its tenant through. A run
        # carries no tenant; it is reachable only because its contract digest is
        # activated for one. Composing them here is what makes the capability
        # delivered rather than merely built.
        self.lifecycle_path = str(directory / "acquisition-lifecycle.sqlite3")
        self.lifecycles = SQLiteAcquisitionContractLifecycleRepository(self.lifecycle_path)
        self.evidence_path = directory / "evidence.sqlite3"
        self.evidence = SQLiteStore.open(self.evidence_path, check_same_thread=False)
        self.bindings = _PersistedWorkspaceBindingDirectory(directory / "bindings.json")
        if self.bindings.catalog_binding_id(TENANT) is None:
            # Only when the workspace has none: catalog-control publishes no way to
            # ask whether a tenant already has a binding, so minting one per
            # construction stacked orphans the directory then abandoned.
            self.bindings.bind_catalog(
                tenant_id=TENANT,
                binding_id=self.catalog.create_draft(tenant_id=TENANT).binding_id,
            )
        principals = InMemoryWorkspacePrincipalDirectory()
        principals.bind_principal(
            tenant_id=TENANT,
            actor_id=REQUESTER,
            role="requester",
            principal_ref=REQUESTER_PRINCIPAL,
        )
        principals.bind_principal(
            tenant_id=TENANT,
            actor_id=ARCHITECT,
            role="data_architect",
            principal_ref=ARCHITECT_PRINCIPAL,
        )
        self.backend = GovernedConsoleBackend(
            identity=GovernedWorkspaceIdentity(
                tenant_ref="tenant-governed",
                tenant_display_name="Governed local tenant",
                workspace_ref="workspace-governed",
                workspace_display_name="Revenue to cash",
            ),
            operation_handles=InMemoryOperationHandleRepository(),
            warehouse_bindings=WarehouseControlBindingReader(
                service=self.control, directory=self.bindings
            ),
            catalog_bindings=CatalogControlBindingReader(
                service=self.catalog, directory=self.bindings
            ),
            warehouse_operations=WarehouseRepositoryOperationReader(self.warehouse_repository),
            requests=self.requests,
            fulfillment=self.reads,
            principals=principals,
            warehouse_commands=WarehouseControlLifecycleCommands(
                service=self.control,
                orchestrator=self.orchestrator,
                repository=self.warehouse_repository,
                directory=self.bindings,
            ),
            request_commands=self.requests,
            fulfillment_commands=self.fulfillment,
            fulfillment_preparation_commands=self.fulfillment,
            semantic_reviews=self.semantic_repository,
            semantic_review_commands=self.semantic_reviews,
            runs=DerivedTenantRunReader(lifecycles=self.lifecycles, evidence=self.evidence),
            # The store satisfies the receipt reader directly: a receipt records its
            # own tenant, so unlike a run there is nothing to derive and no adapter
            # whose only purpose would be to rename the call.
            acquisition_receipts=self.evidence,
            data_products=PolicyPermittedDataProductReader(
                repository=self.fulfillment_repository, requests=self.requests
            ),
        )

    def _provider(self, engine: str) -> WarehouseProvider:
        """The local-acceptance harness, or the real PostgreSQL provider.

        The real one is deferred: its ledger recorder is scoped to a binding that
        does not exist until someone confirms one in the browser.
        """
        if engine == "local-acceptance":
            return LocalAcceptanceWarehouseProvider()
        if engine != "postgresql":
            raise ValueError(f"unknown warehouse engine {engine!r}")
        from tests.acceptance.console_postgresql_engine import (
            DeferredPostgreSQLProvider,
            build_postgresql_provider,
            run_operation_secrets,
        )

        # One set of credentials for the run: a retry mints a new operation, and new
        # passwords with it would not match the container already created.
        operation_secrets = run_operation_secrets(clock=_clock)

        def factory(*, binding_id: str, operation_id: str) -> WarehouseProvider:
            return build_postgresql_provider(
                repository=self.warehouse_repository,
                binding_id=binding_id,
                operation_id=operation_id,
                operation_secrets=operation_secrets,
                directory=self.directory / "postgresql",
                clock=_clock,
            )

        return DeferredPostgreSQLProvider(factory)

    @staticmethod
    def require_loopback(host: str) -> str:
        """Refuse to serve an unauthenticated harness off the machine."""
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(
                "the governed-local console has no authentication and may bind only to "
                f"a loopback host, not {host!r}"
            )
        return host

    def build_app(
        self, *, origin: str | None = None, dist: Path | None = None, actor: str | None = None
    ) -> Starlette:
        if actor is not None and actor not in (ARCHITECT, REQUESTER):
            raise ValueError("local actor must be the architect or requester")
        return create_app(
            backend=self.backend,
            context_provider=self._actor_for if actor is None else lambda request: _context(actor),
            allowed_origin=origin or f"http://127.0.0.1:{_DEFAULT_PORT}",
            dist_directory=dist,
        )

    def seed(self) -> SeededDecision:
        """Commit one decision the architect can act on, through the owning services.

        Without this the console is correct and empty, which demonstrates nothing.
        Every call here is the owning service's own transaction; none of it is
        console state.
        """
        existing = self._already_seeded()
        if existing is not None:
            return existing
        request = self.requests.submit_question(
            tenant_id=TENANT,
            requester_id=REQUESTER,
            purpose="semantic definition",
            question="What does net revenue mean?",
        )
        self.requests.append_conversation(
            TENANT,
            request.request_id,
            REQUESTER,
            "Please use the approved governed definition.",
            expected_revision=self.requests.get(TENANT, request.request_id).revision,
            author_role="requester",
        )
        self.fulfillment.clarify_outcome(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            restated_request="Provide the governed definition of net revenue.",
            in_scope_summary="Approved semantic scope only.",
            out_of_scope_summary="No raw rows and no wider access.",
            expected_revision=self.requests.get(TENANT, request.request_id).revision,
        )
        proposal = self.fulfillment.propose_answer(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            expected_revision=self.requests.get(TENANT, request.request_id).revision,
        )
        if not isinstance(proposal, FulfillmentProposal):
            raise RuntimeError(f"the seeded request compiled to {type(proposal).__name__}")
        self.fulfillment.submit_proposal(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            expected_revision=proposal.request_revision,
        )
        return SeededDecision(
            request_id=request.request_id,
            proposal_digest=digest(proposal.subject),
            data_product_ref=self._permitted_product_ref(proposal),
        )

    def _already_seeded(self) -> SeededDecision | None:
        """The decision a previous run of this state directory left behind.

        The databases persist between runs by design, and `main` re-enters `seed`
        on every start, so minting another identical question each time left a queue
        of copies no reviewer could tell apart.
        """
        for request in self.requests.list_inbox(TENANT):
            proposals = self.fulfillment_repository.list_proposals(TENANT, request.request_id)
            if proposals:
                return SeededDecision(
                    request_id=request.request_id,
                    proposal_digest=digest(proposals[-1].subject),
                    data_product_ref=self._permitted_product_ref(proposals[-1]),
                )
        return None

    def _permitted_product_ref(self, proposal: FulfillmentProposal) -> str:
        """The data product this proposal's governing policy permits.

        Named from the committed policy rather than hardcoded, so a test reads back
        the reference the seed actually created.
        """
        policy = self.fulfillment_repository.load_policy_snapshot(
            TENANT, proposal.policy_snapshot_digest
        )
        return policy.permitted_data_product_refs[0].artifact_id

    def close(self) -> None:
        """Close every store, then raise the first failure.

        A chain of nested `finally` blocks grew one level per store and abandoned
        the remaining handles whenever an early close raised. Closing all of them
        first and re-raising afterwards keeps the state directory reusable between
        runs while still surfacing the failure.
        """
        failure: BaseException | None = None
        for closing in (
            self.warehouse_repository,
            self.request_repository,
            self.catalog_repository,
            self.semantic_repository,
            self.publication_repository,
            self.lifecycles,
            self.evidence,
        ):
            try:
                closing.close()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def _actor_for(self, request: Request) -> TrustedActorContext:
        requested = request.headers.get(ACTOR_HEADER, ARCHITECT)
        return _context(requested if requested in (ARCHITECT, REQUESTER) else ARCHITECT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=default_state_directory(), type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=_DEFAULT_PORT, type=int)
    parser.add_argument("--dist", default=Path("apps/console/dist"), type=Path)
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument(
        "--actor",
        choices=(ARCHITECT, REQUESTER),
        help="Fixed local browser identity; ignores actor headers",
    )
    parser.add_argument(
        "--engine",
        default="local-acceptance",
        choices=("local-acceptance", "postgresql"),
        help=(
            "local-acceptance reaches ready with no engine in existence; "
            "postgresql provisions a real container through the real provider"
        ),
    )
    arguments = parser.parse_args(argv)

    host = GovernedConsoleDeployment.require_loopback(arguments.host)
    deployment = GovernedConsoleDeployment(arguments.directory, engine=arguments.engine)
    if not arguments.no_seed:
        seeded = deployment.seed()
        print(f"seeded decision {seeded.request_id} awaiting the architect's approval")

    import uvicorn

    origin = f"http://{host}:{arguments.port}"
    dist = arguments.dist if arguments.dist.is_dir() else None
    if dist is None:
        print(f"no compiled bundle at {arguments.dist}; serving the API only")
    print(f"governed_local console on {origin} -- warehouse provider is {arguments.engine}")
    try:
        uvicorn.run(
            deployment.build_app(origin=origin, dist=dist, actor=arguments.actor),
            host=host,
            port=arguments.port,
        )
    finally:
        deployment.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
