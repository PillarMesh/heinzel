"""Narrow read adapters over the owning services' public interfaces.

Each protocol here is the console's own minimal view of one owning component. The
console never opens another component's database; it resolves bindings, operations,
review bundles, requests, and fulfillment projections through the interfaces those
components publish, and it classifies their typed failures rather than reinterpreting
them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, get_args

from heinzel_access_control import (
    AccessGrant,
    AccessGrantDenied,
    AccessGrantIntegrityError,
    AccessGrantStaleRevision,
    EntitlementPermission,
)
from heinzel_bi_control import (
    DashboardAccessAuthorityError,
    DashboardAccessAuthorization,
    DashboardAnswerAuthorityReader,
    DashboardAuthorityUnavailable,
    DashboardContractAuthorityError,
    DashboardContractVerifier,
    DashboardDesiredState,
    DashboardPublication,
    DashboardPublicationRecord,
    DashboardPublicationWorkflow,
    DeclareDashboardPublicationCommand,
    InvalidDashboardContract,
    SQLiteDashboardContractRepository,
    SQLiteDashboardRepository,
)
from heinzel_catalog_control import (
    CatalogBinding,
    CatalogControlService,
    CatalogPersistenceError,
)
from heinzel_connection_broker import (
    SourceAccountMode,
    SourceBindingBoundaryError,
    SourceBindingConflictError,
    SourceBindingIntegrityError,
    SourceBindingNotFoundError,
    SourceBindingPersistenceError,
    SourceBindingService,
    SourceConnectionBinding,
    SourceConnectionBindingState,
    StaleSourceBindingRevisionError,
)
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    TriggerRequirement,
    canonical_bytes,
)
from heinzel_contract_model import (
    digest as canonical_digest,
)
from heinzel_contract_service import (
    AcquisitionContractLifecycleRepository,
    BusinessProcessManifest,
    ProcessPackageReceipt,
    ProcessPackageSnapshot,
    SourceObservation,
)
from heinzel_evidence import AcquisitionEvidenceReceipt, RunRecord
from heinzel_provider_sdk import CatalogProductDefinition
from heinzel_provider_sdk.errors import AcquisitionProviderKind
from heinzel_request_management import (
    ApprovedProductIntent,
    ArchitectRequestView,
    BoundSemanticReference,
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
    GovernedAnswer,
    InboxRequest,
    ProductIntent,
    ProductIntentAuthorityRefs,
    ProductIntentCandidate,
    ProductIntentConstraints,
    ProductIntentNoValidPlan,
    QuestionTermSelection,
    RequesterRequestView,
    ReviewerRequestView,
    TransitionEvent,
)
from heinzel_request_management.intake import RequestDigestMismatch
from heinzel_runtime import (
    AcquisitionAuthorizationError,
    AcquisitionContractError,
    AcquisitionIntegrityError,
    AcquisitionOwnershipError,
    AcquisitionPreparationResult,
    AcquisitionStaleRevision,
    AcquisitionThrottledError,
    AcquisitionTransientError,
    AnswerExecutionReceipt,
    AnswerResultSnapshot,
)
from heinzel_semantic_registry import OntologyReviewBundle, SemanticPersistenceError
from heinzel_semantic_registry.review import ReviewItemDecision
from heinzel_state import (
    IncidentConflictError,
    IncidentIntegrityError,
    IncidentNotFoundError,
    IncidentPersistenceError,
    IncidentRecord,
    RecoveryActionEvidence,
    RecoveryActionNotAllowedError,
    RecoveryCommand,
    RunLifecycleSnapshot,
    StaleIncidentRevisionError,
)
from heinzel_warehouse_control import (
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
from heinzel_warehouse_control.repository import WarehouseRepository
from pydantic import Field, ValidationError, field_validator

from .contracts import AcquisitionModeView, ActorRole, ImpactView, ProvenanceView
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


class TenantIncidentReader(Protocol):
    def list_current(self, tenant_id: str) -> tuple[IncidentRecord, ...]: ...

    def list_recovery_evidence(
        self, tenant_id: str, incident_id: str
    ) -> tuple[RecoveryActionEvidence, ...]: ...


class IncidentRecoveryCommands(Protocol):
    def execute(self, command: RecoveryCommand) -> RecoveryActionEvidence: ...


class CatalogBindingReader(Protocol):
    def current_binding(self, tenant_id: str) -> CatalogBinding | None: ...


class CatalogSearchHealthReader(Protocol):
    """Reports whether the catalog's search projection can serve this tenant."""

    def search_ready(self, tenant_id: str) -> bool: ...


class DashboardPublicationReader(Protocol):
    """Reads only dashboards whose desired state has a matching provider receipt."""

    def list_publications(self, tenant_id: str) -> tuple[DashboardPublication, ...]: ...


class GovernedAnswerRecordReader(Protocol):
    def list_for_request(self, tenant_id: str, request_id: str) -> tuple[GovernedAnswer, ...]: ...


class CurrentGovernedAnswerReader(Protocol):
    """Names the answer a request is currently delivered on, if it has one."""

    def current_answer_id(self, tenant_id: str, request_id: str) -> str | None: ...


class RepositoryCurrentGovernedAnswerReader:
    """Name the newest answer of a request, which is the only one that can be published.

    The dashboard answer authority refuses any answer that is not the last one recorded for its
    request, so naming an earlier one would always be refused later. Resolving it here means the
    refusal for a request with no answer at all is a clear one, rather than a publication that is
    declared and then fails against an authority that answered nothing.
    """

    def __init__(self, answers: GovernedAnswerRecordReader) -> None:
        self._answers = answers

    def current_answer_id(self, tenant_id: str, request_id: str) -> str | None:
        answers = self._answers.list_for_request(tenant_id, request_id)
        return answers[-1].answer_id if answers else None


@dataclass(frozen=True, slots=True)
class PublishableDashboard:
    """A certified dashboard one delivered answer could be published to."""

    dashboard_id: str
    dashboard_version: int
    owner: str
    next_revision: int


@dataclass(frozen=True, slots=True)
class PublishableDashboardOffering:
    answer_title: str | None
    dashboards: tuple[PublishableDashboard, ...]


class DashboardRevisionReader(Protocol):
    """The two reads a publishable offering needs to say which revision it would publish."""

    def get_current_desired(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardDesiredState | None: ...

    def get_publication(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardPublication | None: ...


class RepositoryDashboardPublicationReader:
    """List published dashboards straight from bi-control's repository.

    `DashboardControlService` satisfies the same protocol and is what a deployment with a provider
    passes. A publication exists in that repository only because a provider receipted it -- the
    query joins the receipt rather than reading the desired state alone -- so reading it here
    answers the same question without holding a provider that must never be called.

    Which matters for what the console can show. A dashboard published by an earlier start stays
    published, and a deployment that has since been given no Superset can still say so: a read
    surface that went absent whenever a provider was would report nothing published where something
    was, and the record is the publication rather than the connection to the instance.
    """

    def __init__(self, repository: SQLiteDashboardRepository) -> None:
        self._repository = repository

    def list_publications(self, tenant_id: str) -> tuple[DashboardPublication, ...]:
        return self._repository.list_current_publications(tenant_id)


class RepositoryDashboardRevisionReader:
    """Read revisions straight from bi-control's repository, with no BI provider in reach.

    `DashboardControlService` satisfies the same protocol and is what a deployment with a provider
    passes. A deployment without one still has to answer which revision would be published, and
    constructing the control service to do it would mean holding a provider that must never be
    called.
    """

    def __init__(self, repository: SQLiteDashboardRepository) -> None:
        self._repository = repository

    def get_current_desired(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardDesiredState | None:
        return self._repository.load_current_desired(tenant_id, dashboard_id, version)

    def get_publication(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardPublication | None:
        return self._repository.load_current_publication(tenant_id, dashboard_id, version)


class PublishableDashboardReader(Protocol):
    def offering(self, *, tenant_id: str, request_id: str) -> PublishableDashboardOffering: ...


class RequestProvenanceReader(Protocol):
    """Read back the receipts the services recorded while producing one answer.

    One method, returning the view whole, because the join is across stores each service owns
    on its own terms -- the acquisition contract's agreed object shape, the landing receipt, the
    compiled transform and its materialization receipt, the compiled query and its execution.
    Whoever can reach all of those can assemble it; the console only reads what comes back.
    """

    def provenance(self, *, tenant_id: str, request_id: str) -> ProvenanceView | None: ...


class DashboardPublicationCommands(Protocol):
    """Declare a publication and drive it to one settled outcome.

    One call rather than two, for the same reason `SourceRegistrationCommands.register_source` is
    one: the declaration and the attempt belong to bi-control's workflow, and the console orders
    them without owning the lifecycle between them.
    """

    def publish(
        self,
        *,
        tenant_id: str,
        request_id: str,
        dashboard_id: str,
        dashboard_version: int,
        expected_revision: int,
    ) -> DashboardPublicationRecord: ...


class SemanticReviewReader(Protocol):
    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle: ...


class RequestInboxReader(Protocol):
    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]: ...

    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...


class AccessGrantReader(Protocol):
    """Reads access-control's current grant for one tenant-qualified request."""

    def load_current_for_request(self, tenant_id: str, request_id: str) -> AccessGrant | None: ...


class AccessGrantRevocationCommands(Protocol):
    def revoke_for_request(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
        reason: str,
    ) -> AccessGrant: ...


class RequestImpactReader(Protocol):
    def get_request_impact(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        active_role: ActorRole,
        request_id: str,
    ) -> ImpactView | None: ...


class ProductIntentReviewReader(Protocol):
    def current_candidate(
        self, tenant_id: str, request_id: str
    ) -> ProductIntentCandidate | None: ...


class ProductIntentApprovalCommands(Protocol):
    def approve(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        approved_by: str,
        intent: ProductIntent,
        authority_refs: ProductIntentAuthorityRefs | None,
    ) -> ApprovedProductIntent | ProductIntentNoValidPlan: ...

    def list_for_request(
        self, tenant_id: str, request_id: str
    ) -> tuple[ApprovedProductIntent, ...]: ...


class ApprovedSemanticVersionReader(Protocol):
    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion: ...


class SourceObservationReader(Protocol):
    def load(self, tenant_id: str, observation_id: str, version: int) -> SourceObservation: ...


class SourceBindingReader(Protocol):
    """Which sources the connection broker holds a binding for, for one tenant.

    `SourceBindingService` and `SQLiteSourceBindingRepository` both satisfy this directly, so
    nothing stands between the console and the register the broker keeps.

    Absent, the console reports the sources stage as blocked rather than as having none: a
    console with no broker read behind it cannot tell a tenant that registered nothing from one
    whose register it cannot see.
    """

    def list_for_tenant(self, tenant_id: str) -> tuple[SourceConnectionBinding, ...]: ...


@dataclass(frozen=True, slots=True)
class EnrolledSourceConnection:
    """One connection an operator enrolled, and the declaration the deployment reads it under.

    Deliberately not a connection: a handle, the provider that reads it, the account mode the
    credential is for, and the logical objects declared for it. No endpoint, no credential, and
    no reference to either -- a reference is what the broker persists and what a probe presents,
    and the console has no business holding one.

    The declaration travels with the handle because the probe requires the settings it validates
    against to declare exactly the binding's approved objects
    (`PostgreSQLSourceCapabilityProbe._resolved_settings`). Registering therefore cannot invent
    one, and nothing the browser sends can widen it.
    """

    connection_handle: str
    provider_kind: AcquisitionProviderKind
    account_mode: SourceAccountMode
    declared_object_refs: tuple[str, ...]


class EnrolledSourceConnectionReader(Protocol):
    """Which already enrolled connections this tenant could register a source for.

    Enrolment is an operator action that happens before any binding exists, in whatever secret
    custody the deployment injected into the broker, so this read is the deployment's answer and
    not a service's. It is tenant-qualified like every other console read even though a
    custodian need not be: which tenant may register which handle is the deployment's answer
    too, and a console that asked for every handle a custodian holds would offer one workspace
    the handles of another.

    Absent, the console reports that it cannot register a source rather than offering nothing to
    register: those are different answers, and only one of them is honest about the wiring.
    """

    def list_enrolled_source_connections(
        self, tenant_id: str
    ) -> tuple[EnrolledSourceConnection, ...]: ...


class SourceRegistrationCommands(Protocol):
    """Register one enrolled connection as a validated source binding.

    One call rather than the broker's three, for the same reason
    `WarehouseLifecycleCommands.confirm_binding` is one: the three transactions belong to the
    broker, and the console orders them without owning the lifecycle between them.
    """

    def register_source(
        self,
        *,
        tenant_id: str,
        connection_handle: str,
        provider_kind: AcquisitionProviderKind,
        account_mode: SourceAccountMode,
        approved_object_refs: tuple[str, ...],
    ) -> SourceConnectionBinding: ...


# The Integration Contract trigger vocabulary is the platform's only source of cadence. A product
# cannot be fresher than its source is refreshed, so the shortest supported cadence is the minimum
# source interval. Adding a cadence to TriggerRequirement without mapping it here fails at import.
_CADENCE_SECONDS: dict[str, int] = {"daily": 86_400}
_SUPPORTED_CADENCES = frozenset(get_args(TriggerRequirement.model_fields["cadence"].annotation))
if not _CADENCE_SECONDS.keys() >= _SUPPORTED_CADENCES:
    raise RuntimeError("every trigger cadence must map to a minimum source interval")
_MINIMUM_SOURCE_INTERVAL_SECONDS = min(_CADENCE_SECONDS[name] for name in _SUPPORTED_CADENCES)


class GovernedProductIntentAuthority:
    """Derives product intent constraints from the governed records an intent names.

    Request management evaluates an intent only against constraints it gets from here. Every
    constraint comes from a record loaded by exact identity and checked against the tenant and the
    reference digest: approved metrics and dimensions from the approved semantic version, approved
    sources from source observations that are current at evaluation time, and the minimum source
    interval from the platform's supported trigger cadences. Nothing the proposer asserted about
    constraints is used.
    """

    def __init__(
        self,
        *,
        semantic_versions: ApprovedSemanticVersionReader,
        source_observations: SourceObservationReader,
    ) -> None:
        self._semantic_versions = semantic_versions
        self._source_observations = source_observations

    def resolve_constraints(
        self,
        *,
        tenant_id: str,
        authority_refs: ProductIntentAuthorityRefs,
        evaluated_at: datetime,
    ) -> ProductIntentConstraints | None:
        semantic_ref = authority_refs.semantic_version
        try:
            semantic_version = self._semantic_versions.load(
                tenant_id, semantic_ref.artifact_id, semantic_ref.version
            )
        except KeyError:
            return None
        if (
            semantic_version.tenant_id != tenant_id
            or semantic_version.semantic_version_id != semantic_ref.artifact_id
            or semantic_version.version != semantic_ref.version
            or canonical_digest(semantic_version) != semantic_ref.digest
        ):
            return None
        source_refs: list[str] = []
        for observation_ref in authority_refs.source_observations:
            try:
                observation = self._source_observations.load(
                    tenant_id, observation_ref.artifact_id, observation_ref.version
                )
            except KeyError:
                return None
            if (
                observation.tenant_id != tenant_id
                or observation.observation_id != observation_ref.artifact_id
                or observation.version != observation_ref.version
                or canonical_digest(observation) != observation_ref.digest
                or not observation.observed_at <= evaluated_at < observation.valid_until
            ):
                return None
            source_refs.append(observation.source_ref)
        return ProductIntentConstraints(
            approved_source_refs=tuple(dict.fromkeys(source_refs)),
            approved_metric_refs=tuple(metric.object_id for metric in semantic_version.metrics),
            approved_dimension_refs=tuple(entity.object_id for entity in semantic_version.entities),
            minimum_source_interval_seconds=_MINIMUM_SOURCE_INTERVAL_SECONDS,
        )


class ApprovedProductIntentResolver(Protocol):
    def resolve(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ApprovedProductIntent | None: ...


class GovernedApprovedProductIntentSources:
    """Lets contract-service ask request management which sources an approved intent covers.

    An activation cites the product intent it serves by reference. Only an approval recorded for
    the tenant at exactly that reference answers; anything else answers None and activation is
    refused.
    """

    def __init__(self, approvals: ApprovedProductIntentResolver) -> None:
        self._approvals = approvals

    def approved_source_refs(
        self, *, tenant_id: str, product_intent_ref: ArtifactReference
    ) -> tuple[str, ...] | None:
        approval = self._approvals.resolve(tenant_id, product_intent_ref)
        return None if approval is None else approval.intent.source_refs


class ProcessPackageCommands(Protocol):
    def upload(
        self,
        tenant_id: str,
        original: bytes,
        media_type: str,
        manifest: BusinessProcessManifest,
        uploader_id: str,
    ) -> ProcessPackageReceipt: ...

    def latest(self, tenant_id: str) -> ProcessPackageSnapshot | None: ...


class TenantRunReader(Protocol):
    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]: ...


class TenantRunLifecycleReader(Protocol):
    """State's own run lifecycle read; `RunService` satisfies it directly."""

    def describe_runs(self, tenant_id: str) -> tuple[RunLifecycleSnapshot, ...]: ...


class SelectableAnswerTermReader(Protocol):
    """Which approved terms a tenant's publication carries, as the answer path binds them.

    The same `BoundSemanticReference` tuple an `AnswerValidationContext` is given, so what a
    requester is offered to compose a question from cannot drift from what the semantic layer
    will resolve it against. A console that listed terms of its own would offer a question the
    validation then refuses as an unknown reference.

    Absent, the console reports the capability as not delivered rather than offering a list: a
    builder with nothing published behind it would be a form that cannot produce an answerable
    question.
    """

    def list_selectable_answer_terms(
        self, tenant_id: str
    ) -> tuple[BoundSemanticReference, ...]: ...


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


class AcquisitionRunNowCommands(Protocol):
    """The exact command surface published by the acquisition application."""

    def run_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        trigger_window: str,
        acquisition_mode: AcquisitionModeView,
    ) -> AcquisitionPreparationResult: ...


class VerifiedAnswerReader(Protocol):
    def read_for_request(
        self, *, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer: ...

    def read_for_download(
        self, *, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer: ...


class AnswerResultReader(Protocol):
    def load_execution(
        self, tenant_id: str, request_id: str
    ) -> tuple[str, AnswerExecutionReceipt] | None: ...

    def read_result(self, tenant_id: str, result_ref: str) -> AnswerResultSnapshot: ...


class AnswerDownloadReceipt(ArtifactModel):
    download_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("download receipt timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class AnswerDownloadReceiptWriter(Protocol):
    def record(self, receipt: AnswerDownloadReceipt) -> None: ...


class SQLiteAnswerDownloadReceiptRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS answer_download_receipts ("
            "download_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        self._connection.commit()

    def record(self, receipt: AnswerDownloadReceipt) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO answer_download_receipts "
                "(download_id, tenant_id, request_id, created_at, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.download_id,
                    receipt.tenant_id,
                    receipt.request_id,
                    receipt.created_at.isoformat(),
                    canonical_bytes(receipt),
                ),
            )

    def list_for_request(
        self, tenant_id: str, request_id: str
    ) -> tuple[AnswerDownloadReceipt, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM answer_download_receipts "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY created_at, download_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(AnswerDownloadReceipt.model_validate_json(row[0]) for row in rows)


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


class ProductPublicationDefinitionReader(Protocol):
    """Reads the owning publication definition for an already permitted product."""

    def definition_for_reference(
        self, *, tenant_id: str, product_ref: ArtifactReference
    ) -> CatalogProductDefinition | None: ...


class PolicyPermittedDataProductReader:
    """The data product references a tenant's own policy snapshots permit.

    This is the whole of what the estate asserts about a data product. The refs are
    read back from the fulfillment proposals the tenant already owns, so the console
    reports what a governing decision permitted rather than maintaining a product
    catalogue of its own.

    Proposals are owned per request, not per tenant: `list_proposals` requires a
    request identifier and there is no tenant-wide listing. The tenant's requests are
    therefore walked first. Passing a tenant identifier to `list_proposals` is not
    accepted by any owning repository and would make every data-product read raise.
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

    def reviewer_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> ReviewerRequestView: ...


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
        selection: QuestionTermSelection | None = None,
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

    def propose_access(
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


class AnswerAdmissionCommands(Protocol):
    """Admit a stakeholder question's governed query plan, transitioning it to execution.

    Separate from `FulfillmentDecisionCommands.admit` because they are different admissions of
    different things: the fulfillment service admits an approved proposal, and the governed
    answer admits a compiled statement against the policy's ceilings. Both move a request to
    executing, and only one of them leaves behind a plan for `execute_answer` to run -- so for a
    question, this is the one that must be made.
    """

    def admit_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> object: ...


class FulfillmentAccessExecutionCommands(Protocol):
    def execute_access(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> object: ...


class AccessGrantCommands(Protocol):
    def apply(self, *, tenant_id: str, request_id: str, grant_id: str) -> object: ...

    def authorize(
        self,
        *,
        tenant_id: str,
        grant_id: str,
        principal_ref: str,
        purpose: str,
        permission: EntitlementPermission,
        product_version_ref: ArtifactReference,
    ) -> AccessGrant: ...


class DashboardAccessControlAuthority:
    """Translate access-control's current decision into BI-control's narrow contract."""

    def __init__(
        self,
        *,
        commands: AccessGrantCommands,
        clock: Callable[[], datetime],
    ) -> None:
        self._commands = commands
        self._clock = clock

    def authorize(
        self,
        *,
        tenant_id: str,
        authorization_id: str,
        principal_ref: str,
        purpose: str,
        dashboard_id: str,
        dashboard_version: int,
        data_product_version_ref: ArtifactReference,
    ) -> DashboardAccessAuthorization | None:
        try:
            grant = self._commands.authorize(
                tenant_id=tenant_id,
                grant_id=authorization_id,
                principal_ref=principal_ref,
                purpose=purpose,
                permission="dashboard",
                product_version_ref=data_product_version_ref,
            )
        except AccessGrantDenied:
            return None
        except Exception as error:
            raise DashboardAccessAuthorityError("access-control unavailable") from error
        if (
            grant.grant_id != authorization_id
            or grant.tenant_id != tenant_id
            or grant.principal_ref != principal_ref
            or grant.purpose != purpose
            or grant.purpose_digest != canonical_digest(purpose)
            or grant.data_product_version_ref != data_product_version_ref
            or grant.access_mode != "dashboard"
            or grant.state != "active"
            or "dashboard" not in grant.permissions
        ):
            return None
        verified_at = self._clock()
        if verified_at.tzinfo is None or verified_at.utcoffset() != timedelta(0):
            raise DashboardAccessAuthorityError("access-control unavailable")
        try:
            return DashboardAccessAuthorization(
                authorization_id=grant.grant_id,
                revision=grant.revision,
                state=grant.state,
                tenant_id=grant.tenant_id,
                access_request_id=grant.request_id,
                dashboard_id=dashboard_id,
                dashboard_version=dashboard_version,
                data_product_version_ref=grant.data_product_version_ref,
                principal_ref=grant.principal_ref,
                purpose_digest=grant.purpose_digest,
                effective_at=grant.effective_at,
                expires_at=grant.expires_at,
                verified_at=verified_at.astimezone(UTC),
            )
        except ValueError as error:
            raise DashboardAccessAuthorityError("access-control unavailable") from error


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


class BrokerSourceRegistrationCommands:
    """Drives one source registration through the broker's three owning transactions.

    `create_draft`, `transition` and `validate` all belong to the connection broker; the console
    only orders them, and each step is held to the revision the previous one returned so a
    concurrent change to the same binding is refused by the broker rather than overwritten here.

    Nothing is forced to `ready`: `validate` reaches it only on evidence the provider's probe
    returned, and a probe that refuses raises `SourceBindingBoundaryError` out of this method
    with the binding left where the broker left it. So a registration that fails leaves a
    binding in `validating` with no capability authority, which is the state the broker records
    and not one this adapter decides.

    The registration is deliberately not idempotent over an existing binding: the broker derives
    the binding identifier from the tenant, provider and handle, so a second registration of the
    same handle is refused by `create_draft` as a conflict instead of silently re-probing a
    binding that already exists.
    """

    def __init__(self, service: SourceBindingService) -> None:
        self._service = service

    def register_source(
        self,
        *,
        tenant_id: str,
        connection_handle: str,
        provider_kind: AcquisitionProviderKind,
        account_mode: SourceAccountMode,
        approved_object_refs: tuple[str, ...],
    ) -> SourceConnectionBinding:
        draft = self._service.create_draft(
            tenant_id=tenant_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            approved_object_refs=approved_object_refs,
        )
        validating = self._service.transition(
            tenant_id,
            draft.binding_id,
            SourceConnectionBindingState.VALIDATING,
            expected_revision=draft.revision,
        )
        return self._service.validate(
            tenant_id, draft.binding_id, expected_revision=validating.revision
        )


def _publication_intent_id(
    *,
    tenant_id: str,
    dashboard_id: str,
    dashboard_version: int,
    request_id: str,
    answer_id: str,
    expected_revision: int,
) -> str:
    return (
        "dashboard-publication-"
        + canonical_digest(
            {
                "domain": "heinzel-console-dashboard-publication-intent-v1",
                "tenant_id": tenant_id,
                "dashboard_id": dashboard_id,
                "dashboard_version": dashboard_version,
                "request_id": request_id,
                "answer_id": answer_id,
                "expected_revision": expected_revision,
            }
        )[:32]
    )


class ContractPublishableDashboardReader:
    """Offer the certified dashboards one request's delivered answer could be published to.

    Every condition applied here is a refusal the composition already makes: a certified contract
    naming exactly one data product, that product being the one the answer read, the contract's
    metrics being the answer's metrics, a visual intent, and an answer current enough for the
    contract's freshness requirement. Composition stays authoritative and re-checks all of it; this
    reader exists so an architect is offered what will work rather than discovering it by refusal,
    and so the publication command can be checked against an offering the server composed.

    A stored contract that fails signature verification is never skipped. Dropping it would turn an
    integrity failure into a shorter list, so the whole offering fails instead.
    """

    def __init__(
        self,
        *,
        contracts: SQLiteDashboardContractRepository,
        contract_verifier: DashboardContractVerifier,
        answers: CurrentGovernedAnswerReader,
        answer_authority: DashboardAnswerAuthorityReader,
        dashboard_control: DashboardRevisionReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._contracts = contracts
        self._contract_verifier = contract_verifier
        self._answers = answers
        self._answer_authority = answer_authority
        self._dashboard_control = dashboard_control
        self._clock = clock

    def offering(self, *, tenant_id: str, request_id: str) -> PublishableDashboardOffering:
        answer_id = self._answers.current_answer_id(tenant_id, request_id)
        if answer_id is None:
            return PublishableDashboardOffering(answer_title=None, dashboards=())
        answer = self._answer_authority.read_exact(
            tenant_id=tenant_id, request_id=request_id, answer_id=answer_id
        )
        if answer is None or len(answer.product_generation_refs) != 1:
            return PublishableDashboardOffering(answer_title=None, dashboards=())
        product_ref = answer.product_generation_refs[0].product_ref
        age_seconds = (self._clock() - answer.as_of).total_seconds()
        offered: list[PublishableDashboard] = []
        for signed in self._contracts.list_for_tenant(tenant_id):
            try:
                contract = self._contract_verifier.verify(signed)
            except InvalidDashboardContract as error:
                raise DashboardContractAuthorityError(
                    "a stored dashboard contract failed verification"
                ) from error
            if (
                contract.lifecycle_state != "certified"
                or len(contract.data_product_versions) != 1
                or contract.data_product_versions[0] != product_ref
                or contract.metric_versions != answer.metric_version_refs
                or not contract.visual_intents
                or answer.freshness_disposition != "current"
                or not 0 <= age_seconds <= contract.freshness_requirement.maximum_age_seconds
            ):
                continue
            offered.append(
                PublishableDashboard(
                    dashboard_id=contract.dashboard_id,
                    dashboard_version=contract.version,
                    owner=contract.owner,
                    next_revision=self._next_revision(
                        tenant_id, contract.dashboard_id, contract.version
                    ),
                )
            )
        return PublishableDashboardOffering(answer_title=answer.title, dashboards=tuple(offered))

    def _next_revision(self, tenant_id: str, dashboard_id: str, version: int) -> int:
        current = self._dashboard_control.get_current_desired(tenant_id, dashboard_id, version)
        if current is None:
            return 1
        #
        # A desired revision whose apply never produced a receipt is republished as itself rather
        # than superseded, so a publication that failed at the provider stays recoverable instead of
        # leaving a revision nothing ever applied behind it.
        published = self._dashboard_control.get_publication(tenant_id, dashboard_id, version)
        return current.revision if published is None else current.revision + 1


class WorkflowDashboardPublicationCommands:
    """Order bi-control's declaration and its attempt for one console publication.

    The intent identifier is derived from the publication the command describes, not from the
    request's idempotency key. Two architects asking for the same publication therefore converge on
    one intent and one window, instead of opening a second window over the same answer and giving
    the publication more retries than its result retention allows.
    """

    def __init__(
        self,
        workflow: DashboardPublicationWorkflow,
        answers: CurrentGovernedAnswerReader,
    ) -> None:
        self._workflow = workflow
        self._answers = answers

    def publish(
        self,
        *,
        tenant_id: str,
        request_id: str,
        dashboard_id: str,
        dashboard_version: int,
        expected_revision: int,
    ) -> DashboardPublicationRecord:
        answer_id = self._answers.current_answer_id(tenant_id, request_id)
        if answer_id is None:
            raise DashboardAuthorityUnavailable("dashboard answer authority is unavailable")
        intent_id = _publication_intent_id(
            tenant_id=tenant_id,
            dashboard_id=dashboard_id,
            dashboard_version=dashboard_version,
            request_id=request_id,
            answer_id=answer_id,
            expected_revision=expected_revision,
        )
        self._workflow.declare(
            DeclareDashboardPublicationCommand(
                intent_id=intent_id,
                tenant_id=tenant_id,
                dashboard_id=dashboard_id,
                dashboard_version=dashboard_version,
                request_id=request_id,
                answer_id=answer_id,
                expected_revision=expected_revision,
            )
        )
        return self._workflow.advance(tenant_id=tenant_id, intent_id=intent_id)


def classify_downstream_failure(error: Exception) -> DownstreamClassification:
    """Classify an owning component's failure without flattening it.

    A transient persistence or transport failure must never be recorded as a terminal
    verdict about the request, and an ownership failure must never announce that the
    object exists.
    """
    if isinstance(error, FulfillmentStaleRevision):
        return "conflict"
    if isinstance(error, AcquisitionStaleRevision):
        return "conflict"
    if isinstance(
        error,
        (
            AccessGrantStaleRevision,
            StaleIncidentRevisionError,
            IncidentConflictError,
            RecoveryActionNotAllowedError,
            StaleSourceBindingRevisionError,
            SourceBindingConflictError,
        ),
    ):
        # A source binding that already exists, or whose revision moved under a registration,
        # is a conflict the architect resolves by reloading -- never a transient failure, and
        # never a verdict about the source itself.
        return "conflict"
    if isinstance(
        error,
        (
            FulfillmentNotVisible,
            AccessGrantDenied,
            FulfillmentOwnershipError,
            FulfillmentAuthorityError,
            AcquisitionOwnershipError,
            AcquisitionAuthorizationError,
            PermissionError,
            IncidentNotFoundError,
            SourceBindingNotFoundError,
        ),
    ):
        # An authority failure answers exactly as an unknown object does, so a probe
        # cannot learn that the object exists.
        return "not_visible"
    if isinstance(error, (SourceBindingIntegrityError, SourceBindingBoundaryError)):
        # Before the transient arm below, because `SourceBindingIntegrityError` subclasses
        # `SourceBindingPersistenceError`: corruption reported as a momentary failure would be
        # retried forever. `SourceBindingBoundaryError` is the broker refusing what crossed its
        # private boundary -- a resolver that could not resolve, or a probe that refused the
        # source -- which retrying cannot change either, and which the console must not describe
        # any further than this: the broker drops the cause deliberately, because a resolver's
        # own message may quote the connection detail it was resolving.
        return "integrity"
    if isinstance(
        error,
        (
            WarehousePersistenceError,
            CatalogPersistenceError,
            SemanticPersistenceError,
            sqlite3.Error,
            OSError,
            IncidentPersistenceError,
            AcquisitionTransientError,
            AcquisitionThrottledError,
            SourceBindingPersistenceError,
        ),
    ):
        return "unavailable"
    if isinstance(
        error,
        (
            ValidationError,
            AccessGrantIntegrityError,
            IncidentIntegrityError,
            AcquisitionContractError,
            AcquisitionIntegrityError,
        ),
    ):
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
