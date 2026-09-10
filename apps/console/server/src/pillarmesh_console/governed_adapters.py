"""Narrow read adapters over the owning services' public interfaces.

Each protocol here is the console's own minimal view of one owning component. The
console never opens another component's database; it resolves bindings, operations,
review bundles, requests, and fulfillment projections through the interfaces those
components publish, and it classifies their typed failures rather than reinterpreting
them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pillarmesh_catalog_control import (
    CatalogBinding,
    CatalogControlService,
    CatalogPersistenceError,
)
from pillarmesh_contract_model import ArtifactReference
from pillarmesh_contract_service import AcquisitionContractLifecycleRepository
from pillarmesh_evidence import AcquisitionEvidenceReceipt, RunRecord
from pillarmesh_request_management import (
    ArchitectRequestView,
    ClarifiedOutcomeStatement,
    ConversationAuthorRole,
    ConversationEntry,
    FulfillmentApprovalBinding,
    FulfillmentAuthorityError,
    FulfillmentGroundingError,
    FulfillmentIntegrityError,
    FulfillmentNotVisible,
    FulfillmentOutcomeResult,
    FulfillmentOwnershipError,
    FulfillmentPolicyError,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    FulfillmentStaleRevision,
    InboxRequest,
    RequesterRequestView,
    TransitionEvent,
)
from pillarmesh_request_management.intake import RequestDigestMismatch
from pillarmesh_semantic_registry import OntologyReviewBundle, SemanticPersistenceError
from pillarmesh_semantic_registry.review import ReviewItemDecision
from pillarmesh_warehouse_control import (
    EngineKind,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
    WarehouseFailureClassification,
    WarehouseLifecycleOrchestrator,
    WarehousePersistenceError,
    WarehouseProviderError,
)
from pillarmesh_warehouse_control.repository import WarehouseRepository
from pydantic import ValidationError

from .contracts import ActorRole
from .errors import (
    ConsoleConflict,
    ConsoleError,
    ConsoleInvalidRequest,
    ConsoleNotFound,
    ConsoleUnavailable,
)

_WAREHOUSE_IDENTITY_SEPARATOR = "/"

type DownstreamClassification = Literal["unavailable", "not_visible", "conflict", "integrity"]


@dataclass(frozen=True, slots=True)
class GovernedWorkspaceIdentity:
    """Display identity for the governed workspace.

    Tenant and actor authority still come from the trusted request context; these are
    presentation values only.
    """

    tenant_ref: str
    tenant_display_name: str
    workspace_ref: str
    workspace_display_name: str


@dataclass(frozen=True, slots=True)
class WarehouseOperationIdentity:
    """The private identity of one warehouse operation.

    A handle record stores a single opaque string, so the two identifiers a warehouse
    operation needs are encoded together here and decoded only inside the adapter that
    owns the warehouse repository.
    """

    binding_id: str
    operation_id: str

    def encode(self) -> str:
        if (
            _WAREHOUSE_IDENTITY_SEPARATOR in self.binding_id
            or _WAREHOUSE_IDENTITY_SEPARATOR in self.operation_id
        ):
            raise ValueError("warehouse operation identifiers must not contain the separator")
        return f"{self.binding_id}{_WAREHOUSE_IDENTITY_SEPARATOR}{self.operation_id}"

    @classmethod
    def decode(cls, private_identity: str) -> WarehouseOperationIdentity | None:
        binding_id, separator, operation_id = private_identity.partition(
            _WAREHOUSE_IDENTITY_SEPARATOR
        )
        if not separator or not binding_id or not operation_id:
            return None
        return cls(binding_id=binding_id, operation_id=operation_id)


class WorkspaceBindingDirectory(Protocol):
    """Maps a tenant to the managed-service bindings its workspace established.

    The owning services address a binding by identity and deliberately refuse to
    enumerate; the console keeps its own record of which binding a workspace uses.
    """

    def warehouse_binding_id(self, tenant_id: str) -> str | None: ...

    def catalog_binding_id(self, tenant_id: str) -> str | None: ...


class InMemoryWorkspaceBindingDirectory:
    def __init__(self) -> None:
        self._warehouse: dict[str, str] = {}
        self._catalog: dict[str, str] = {}

    def bind_warehouse(self, *, tenant_id: str, binding_id: str) -> None:
        self._warehouse[tenant_id] = binding_id

    def bind_catalog(self, *, tenant_id: str, binding_id: str) -> None:
        self._catalog[tenant_id] = binding_id

    def warehouse_binding_id(self, tenant_id: str) -> str | None:
        return self._warehouse.get(tenant_id)

    def catalog_binding_id(self, tenant_id: str) -> str | None:
        return self._catalog.get(tenant_id)


class WorkspaceActorDirectory(Protocol):
    """Resolves deployment-owned presentation names without changing authority."""

    def display_name(self, *, tenant_id: str, actor_id: str) -> str | None: ...


class InMemoryWorkspaceActorDirectory:
    def __init__(self) -> None:
        self._display_names: dict[tuple[str, str], str] = {}

    def bind_actor(self, *, tenant_id: str, actor_id: str, display_name: str) -> None:
        if not display_name.strip():
            raise ValueError("actor display name must not be blank")
        self._display_names[(tenant_id, actor_id)] = display_name

    def display_name(self, *, tenant_id: str, actor_id: str) -> str | None:
        return self._display_names.get((tenant_id, actor_id))


class WarehouseBindingReader(Protocol):
    def current_binding(self, tenant_id: str) -> WarehouseBinding | None: ...


class WarehouseOperationReader(Protocol):
    def load_operation(
        self, *, tenant_id: str, private_identity: str
    ) -> PrivateWarehouseOperation | None: ...


class CatalogBindingReader(Protocol):
    def current_binding(self, tenant_id: str) -> CatalogBinding | None: ...


class CatalogSearchHealthReader(Protocol):
    """Reports whether the catalog's search projection can serve this tenant."""

    def search_ready(self, tenant_id: str) -> bool: ...


class SemanticReviewReader(Protocol):
    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle: ...


class RequestInboxReader(Protocol):
    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]: ...

    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...


class TenantRunReader(Protocol):
    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]: ...


class TenantAcquisitionReceiptReader(Protocol):
    """The evidence store's own acquisition read, named as the store names it.

    Unlike a run, a receipt records the tenant it belongs to, so no derivation
    stands between the console and the store and `SQLiteStore` satisfies this
    directly. Naming the method anything else would require an adapter whose only
    purpose was to rename a call.
    """

    def list_acquisition_receipts(
        self, tenant_id: str
    ) -> tuple[AcquisitionEvidenceReceipt, ...]: ...


class DerivedTenantRunReader:
    """Resolve a tenant's runs without any run recording a tenant.

    A run is stored in an append-only, digested evidence chain and carries no
    tenant. Adding one would change the digest of every existing record and demand
    a migration with an evidence-continuity story. It is not needed: a run carries
    a contract digest, and a contract digest is tenant-qualified by the acquisition
    lifecycle. The tenant is therefore derived at read time --
    tenant -> activated contracts -> runs witnessed under them -- and the evidence
    chain stays exactly as it was witnessed.

    A tenant with no activated contracts yields no digests and therefore no runs,
    which is the answer that keeps one tenant from reading another's evidence.
    """

    def __init__(
        self,
        *,
        lifecycles: AcquisitionContractLifecycleRepository,
        evidence: EvidenceRunReader,
    ) -> None:
        self._lifecycles = lifecycles
        self._evidence = evidence

    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]:
        digests = tuple(
            state.contract_digest for state in self._lifecycles.list_activated(tenant_id)
        )
        return self._evidence.list_runs_for_contracts(digests)


class EvidenceRunReader(Protocol):
    def list_runs_for_contracts(
        self, contract_digests: Collection[str]
    ) -> tuple[RunRecord, ...]: ...


class DataProductReferenceReader(Protocol):
    def permitted_references(self, tenant_id: str) -> tuple[ArtifactReference, ...]: ...


class PolicyPermittedDataProductReader:
    """The data product references a tenant's own policy snapshots permit.

    This is the whole of what the estate asserts about a data product. The refs are
    read back from the fulfillment proposals the tenant already owns, so the console
    reports what a governing decision permitted rather than maintaining a product
    catalogue of its own.

    Proposals are owned per request, not per tenant: `list_proposals` requires a
    request identifier and there is no tenant-wide listing. The tenant's requests are
    therefore walked first. An earlier version called `list_proposals(tenant_id)`,
    which no owning repository accepts, so every data-product read raised.
    """

    def __init__(
        self,
        *,
        repository: FulfillmentReadRepository,
        requests: RequestInboxReader,
    ) -> None:
        self._repository = repository
        self._requests = requests

    def permitted_references(self, tenant_id: str) -> tuple[ArtifactReference, ...]:
        references: list[ArtifactReference] = []
        for request in self._requests.list_inbox(tenant_id):
            for proposal in self._repository.list_proposals(tenant_id, request.request_id):
                try:
                    snapshot = self._repository.load_policy_snapshot(
                        tenant_id, proposal.policy_snapshot_digest
                    )
                except KeyError:
                    # The snapshot is not readable by this tenant, so the proposal
                    # permits nothing this reader may report. One unreadable snapshot
                    # is not grounds for failing the whole listing.
                    continue
                references.extend(snapshot.permitted_data_product_refs)
        return tuple(references)


class FulfillmentReadRepository(Protocol):
    """Mirrors the owning repository exactly, including that a missing snapshot raises.

    Both signatures were previously wrong here -- `list_proposals` lost its request
    identifier and `load_policy_snapshot` was declared optional when it raises -- and
    because the harness that passes the real repository is not type checked, nothing
    caught it before the route returned 500.
    """

    def list_proposals(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentProposal, ...]: ...

    def load_policy_snapshot(
        self, tenant_id: str, snapshot_digest: str
    ) -> FulfillmentPolicySnapshot: ...


class FulfillmentViewReader(Protocol):
    def requester_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> RequesterRequestView: ...

    def architect_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ArchitectRequestView: ...


class WarehouseControlBindingReader:
    def __init__(
        self, *, service: WarehouseControlService, directory: WorkspaceBindingDirectory
    ) -> None:
        self._service = service
        self._directory = directory

    def current_binding(self, tenant_id: str) -> WarehouseBinding | None:
        binding_id = self._directory.warehouse_binding_id(tenant_id)
        if binding_id is None:
            return None
        try:
            return self._service.get(tenant_id, binding_id)
        except KeyError:
            return None


class CatalogControlBindingReader:
    def __init__(
        self, *, service: CatalogControlService, directory: WorkspaceBindingDirectory
    ) -> None:
        self._service = service
        self._directory = directory

    def current_binding(self, tenant_id: str) -> CatalogBinding | None:
        binding_id = self._directory.catalog_binding_id(tenant_id)
        if binding_id is None:
            return None
        try:
            return self._service.get(tenant_id, binding_id)
        except KeyError:
            return None


class WarehouseRepositoryOperationReader:
    """Reads one private warehouse operation through the repository's load interface.

    The private record never leaves this boundary intact; the backend projects only a
    lifecycle phase, a typed state, and a classified failure from it.
    """

    def __init__(self, repository: WarehouseRepository) -> None:
        self._repository = repository

    def load_operation(
        self, *, tenant_id: str, private_identity: str
    ) -> PrivateWarehouseOperation | None:
        identity = WarehouseOperationIdentity.decode(private_identity)
        if identity is None:
            return None
        try:
            return self._repository.load_operation(
                tenant_id, identity.binding_id, identity.operation_id
            )
        except KeyError:
            return None


class WorkspacePrincipalDirectory(Protocol):
    """Maps a trusted actor and role to the authority reference the owning service names.

    The owning services validate authority themselves; they never publish the reverse
    lookup, because enumerating a tenant's principals would itself be a disclosure. The
    console therefore keeps its own deployment-configured record and passes the named
    reference back for the owning service to check.
    """

    def principal_ref(self, *, tenant_id: str, actor_id: str, role: ActorRole) -> str | None: ...


class InMemoryWorkspacePrincipalDirectory:
    def __init__(self) -> None:
        self._principals: dict[tuple[str, str, ActorRole], str] = {}

    def bind_principal(
        self, *, tenant_id: str, actor_id: str, role: ActorRole, principal_ref: str
    ) -> None:
        self._principals[(tenant_id, actor_id, role)] = principal_ref

    def principal_ref(self, *, tenant_id: str, actor_id: str, role: ActorRole) -> str | None:
        return self._principals.get((tenant_id, actor_id, role))


@dataclass(frozen=True, slots=True)
class WarehouseConfirmation:
    """What the warehouse lifecycle left behind after one confirmation attempt.

    `operation_identity` is present only while the owning operation is still live. A
    completed lifecycle keeps no live operation, so the console falls back to the
    binding itself as the thing its handle refers to.
    """

    binding_id: str
    binding_revision: int
    lifecycle_state: WarehouseBindingState
    operation_identity: WarehouseOperationIdentity | None
    failure_classification: WarehouseFailureClassification | None


class WarehouseLifecycleCommands(Protocol):
    def confirm_binding(
        self, *, tenant_id: str, engine: str, region: str, capacity: str
    ) -> WarehouseConfirmation: ...


class RequestIntakeCommands(Protocol):
    def submit_question(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        purpose: str,
        question: str,
        title: str | None = None,
        request_digest: str | None = None,
    ) -> InboxRequest: ...

    def submit_access_request(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        purpose: str,
        data_product_id: str,
        requested_fields: tuple[str, ...],
        access_mode: Literal["query", "dashboard", "export"],
        expires_at: datetime,
        title: str | None = None,
        request_digest: str | None = None,
    ) -> InboxRequest: ...

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        body: str,
        *,
        expected_revision: int,
        author_role: ConversationAuthorRole | None = None,
    ) -> ConversationEntry: ...

    def list_conversation(
        self, tenant_id: str, request_id: str
    ) -> tuple[ConversationEntry, ...]: ...

    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]: ...

    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...


class FulfillmentPreparationCommands(Protocol):
    def clarify_outcome(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        restated_request: str,
        in_scope_summary: str,
        out_of_scope_summary: str,
        expected_revision: int,
    ) -> ClarifiedOutcomeStatement: ...

    def propose_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> FulfillmentOutcomeResult: ...

    def submit_proposal(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> InboxRequest: ...


class FulfillmentDecisionCommands(Protocol):
    def record_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
        subject_digest: str,
        decision: Literal["approve", "reject", "request_changes"],
        expected_revision: int,
    ) -> FulfillmentApprovalBinding: ...

    def admit(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> object: ...

    def cancel(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> object: ...


class FulfillmentExecutionCommands(Protocol):
    def execute_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> object: ...


class SemanticReviewCommands(Protocol):
    """Mirrors `SemanticReviewService.decide_item` exactly, defaults included.

    A protocol narrower than the transaction it stands for hides whatever it omits:
    `revised_content` was missing here, so the console could not express a revision
    and the type checker agreed with it. `merge_candidate_ids` is declared for the
    same reason, though nothing sends it yet.
    """

    def decide_item(
        self,
        *,
        tenant_id: str,
        bundle_id: str,
        item_id: str,
        decision: ReviewItemDecision,
        actor_id: str,
        expected_revision: int,
        revised_content: str | None = None,
        merge_candidate_ids: tuple[str, ...] = (),
    ) -> OntologyReviewBundle: ...


class WarehouseControlLifecycleCommands:
    """Drives one managed-warehouse confirmation through its two owning transactions.

    `create_draft` and `provision` both belong to warehouse-control; the console only
    orders them and records which binding this workspace uses. The private operation
    identity is read back through the repository's live-operation interface so a still
    running or failed lifecycle can be projected without the console ever inventing an
    identity of its own.
    """

    def __init__(
        self,
        *,
        service: WarehouseControlService,
        orchestrator: WarehouseLifecycleOrchestrator,
        repository: WarehouseRepository,
        directory: InMemoryWorkspaceBindingDirectory,
    ) -> None:
        self._service = service
        self._orchestrator = orchestrator
        self._repository = repository
        self._directory = directory

    def confirm_binding(
        self, *, tenant_id: str, engine: str, region: str, capacity: str
    ) -> WarehouseConfirmation:
        if capacity != "mvp-fixed":
            raise ValueError("the managed warehouse capacity profile is fixed")
        binding = self._service.create_draft(
            tenant_id=tenant_id,
            engine_kind=EngineKind(engine),
            region=region,
            capacity_profile="mvp-fixed",
        )
        self._directory.bind_warehouse(tenant_id=tenant_id, binding_id=binding.binding_id)
        classification: WarehouseFailureClassification | None = None
        try:
            self._orchestrator.provision(
                tenant_id, binding.binding_id, expected_revision=binding.revision
            )
        except WarehouseProviderError as failure:
            # The provider's own classification is the only thing that may decide
            # whether this is retryable; flattening it here would disable every
            # retry policy built on top of it.
            classification = failure.classification
        current = self._service.get(tenant_id, binding.binding_id)
        live = self._repository.load_live_operation(tenant_id, binding.binding_id)
        return WarehouseConfirmation(
            binding_id=current.binding_id,
            binding_revision=current.revision,
            lifecycle_state=current.lifecycle_state,
            operation_identity=(
                None
                if live is None
                else WarehouseOperationIdentity(
                    binding_id=live.binding_id, operation_id=live.operation_id
                )
            ),
            failure_classification=classification,
        )


def classify_downstream_failure(error: Exception) -> DownstreamClassification:
    """Classify an owning component's failure without flattening it.

    A transient persistence or transport failure must never be recorded as a terminal
    verdict about the request, and an ownership failure must never announce that the
    object exists.
    """
    if isinstance(error, FulfillmentStaleRevision):
        return "conflict"
    if isinstance(
        error,
        (
            FulfillmentNotVisible,
            FulfillmentOwnershipError,
            FulfillmentAuthorityError,
            PermissionError,
        ),
    ):
        # An authority failure answers exactly as an unknown object does, so a probe
        # cannot learn that the object exists.
        return "not_visible"
    if isinstance(
        error,
        (
            WarehousePersistenceError,
            CatalogPersistenceError,
            SemanticPersistenceError,
            sqlite3.Error,
            OSError,
        ),
    ):
        return "unavailable"
    if isinstance(error, ValidationError):
        # A persisted artifact did not match its own model. Reloading cannot help,
        # and calling it a conflict tells the operator to retry forever. Pydantic's
        # ValidationError subclasses ValueError, so this arm must precede it.
        return "integrity"
    if isinstance(
        error,
        (
            FulfillmentIntegrityError,
            FulfillmentGroundingError,
            FulfillmentPolicyError,
        ),
    ):
        # The owning service rejected the submitted domain state. That is a conflict
        # about a revision the caller must reload, never a transient failure and never
        # a verdict the console may record about the counterparty.
        return "conflict"
    if isinstance(error, ValueError):
        # A bare ValueError is a refusal the console cannot attribute to a revision:
        # a composition mistake reaches here the same way a domain rejection would.
        return "integrity"
    raise error


def console_error_for(error: Exception) -> ConsoleError:
    if isinstance(error, RequestDigestMismatch):
        return ConsoleInvalidRequest(
            code="request_digest_mismatch",
            safe_message="Request digest mismatch. Review the content and submit it again.",
            recovery_action="correct_input",
            field="request_digest",
        )
    classification = classify_downstream_failure(error)
    if classification == "not_visible":
        return ConsoleNotFound()
    if classification == "integrity":
        return ConsoleUnavailable(
            code="downstream_integrity",
            safe_message="The governing service returned state the console cannot trust.",
            recovery_action="contact_support",
        )
    if classification == "conflict":
        return ConsoleConflict(
            code="stale_revision",
            safe_message="The resource changed. Reload it and review the new revision.",
            recovery_action="reload",
        )
    return ConsoleUnavailable(
        code="downstream_unavailable",
        safe_message="The governing service is temporarily unavailable.",
        recovery_action="retry",
    )
