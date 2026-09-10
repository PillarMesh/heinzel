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
import re
import sqlite3
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from pillarmesh_catalog_control import CatalogControlService, SQLiteCatalogRepository
from pillarmesh_console.app import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.governed_adapters import (
    CatalogControlBindingReader,
    CatalogSearchHealthReader,
    DerivedTenantRunReader,
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceActorDirectory,
    InMemoryWorkspaceBindingDirectory,
    InMemoryWorkspacePrincipalDirectory,
    PolicyPermittedDataProductReader,
    WarehouseControlBindingReader,
    WarehouseControlLifecycleCommands,
    WarehouseRepositoryOperationReader,
)
from pillarmesh_console.governed_backend import GovernedConsoleBackend
from pillarmesh_console.operation_handles import InMemoryOperationHandleRepository
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    ManagedIntegrationContract,
    SemanticObject,
    digest,
)
from pillarmesh_contract_service import (
    SQLiteAcquisitionContractLifecycleRepository,
)
from pillarmesh_evidence import SQLiteStore
from pillarmesh_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from pillarmesh_request_management import (
    AnswerCandidateProvider,
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    InboxRequest,
    RequestManagementService,
    ResolutionFailure,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
    StakeholderQuestion,
)
from pillarmesh_semantic_registry import (
    FulfillmentAuthorityObservation,
    SemanticFulfillmentSnapshotAdapter,
    SQLiteCatalogPublicationRepository,
)
from pillarmesh_semantic_registry.publication import (
    CatalogPublicationIntent,
    CatalogPublicationReceipt,
    CatalogPublicationRepository,
)
from pillarmesh_semantic_registry.repository import SQLiteSemanticRepository
from pillarmesh_semantic_registry.review import SemanticReviewService
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    InitialWarehouseValidationResult,
    LocalAcceptanceWarehouseReadinessPolicy,
    PrivateWarehouseOperation,
    WarehouseBinding,
    WarehouseBindingState,
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
    """Wall time for transactions performed through the interactive product."""
    return datetime.now(UTC)


def _operational_clock() -> datetime:
    """Wall time for credentials checked by processes outside the scenario."""
    return datetime.now(UTC)


def _worker_thread_connection(database_path: str) -> sqlite3.Connection:
    """A connection a threadpool worker may use.

    Command routes run the backend in a threadpool and SQLite connections carry
    thread affinity, so composing the application is where the thread-tolerant
    connection belongs. Both owning repositories accept an injected connection.
    """
    return sqlite3.connect(database_path, check_same_thread=False)


class _ElasticsearchSearchHealth:
    def __init__(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("catalog search health must use a loopback HTTP endpoint")
        self._endpoint = endpoint.rstrip("/")

    def search_ready(self, tenant_id: str) -> bool:
        del tenant_id
        request = urllib.request.Request(
            self._endpoint + "/_cluster/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read(4096))
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("status") in {"green", "yellow"}


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


def _words(text: str) -> tuple[str, ...]:
    # Unicode word characters, with underscore as a separator: "Umsätze" is one word, and
    # "customer_audit" reads the same as "Customer audit".
    return tuple(word for word in re.split(r"[\W_]+", text.casefold()) if word)


def _semantic_matches(
    *,
    publications: CatalogPublicationRepository,
    tenant_id: str,
    publication_id: str,
    request: InboxRequest,
) -> tuple[SemanticObject, ...]:
    """Every published term the question names as whole words.

    Matching is on word sequences, never substrings, so "invoiced" does not name "Invoice". A
    term whose words sit wholly inside a longer named term is discarded, so asking about
    "Customer audit 2026" names that term rather than also naming "Customer".
    """
    if not isinstance(request.payload, StakeholderQuestion):
        return ()
    intent, _, _ = publications.load_publication(tenant_id=tenant_id, publication_id=publication_id)
    question = _words(request.payload.question)
    spans: list[tuple[int, int, SemanticObject]] = []
    for semantic_object in intent.semantic_objects:
        for phrase in {_words(semantic_object.name), _words(semantic_object.object_id)}:
            if not phrase:
                continue
            spans.extend(
                (start, start + len(phrase), semantic_object)
                for start in range(len(question) - len(phrase) + 1)
                if question[start : start + len(phrase)] == phrase
            )
    named = {
        id(semantic_object): semantic_object
        for start, end, semantic_object in spans
        if not any(
            other_start <= start and end <= other_end and (other_end - other_start) > (end - start)
            for other_start, other_end, _ in spans
        )
    }
    return tuple(named.values())


def _semantic_match(
    *,
    publications: CatalogPublicationRepository,
    tenant_id: str,
    publication_id: str,
    request: InboxRequest,
) -> SemanticObject | None:
    matches = _semantic_matches(
        publications=publications,
        tenant_id=tenant_id,
        publication_id=publication_id,
        request=request,
    )
    return matches[0] if len(matches) == 1 else None


class _ActivePublicationCatalog:
    """The publication repository as the console catalog may read it.

    Questions are answered from one active publication. Listing every publication the tenant ever
    recorded would advertise superseded terms that preparation then refuses, so the catalog read
    sees only the active one. Every other operation passes through unchanged.
    """

    def __init__(
        self, repository: CatalogPublicationRepository, active_publication_id: str
    ) -> None:
        self._repository = repository
        self._active_publication_id = active_publication_id

    def list_publications(self, *, tenant_id: str) -> tuple[CatalogPublicationReceipt, ...]:
        return tuple(
            receipt
            for receipt in self._repository.list_publications(tenant_id=tenant_id)
            if receipt.publication_id == self._active_publication_id
        )

    def close(self) -> None:
        self._repository.close()

    def store_intent(
        self,
        *,
        intent: CatalogPublicationIntent,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> CatalogPublicationIntent:
        return self._repository.store_intent(
            intent=intent, semantic_version=semantic_version, contract=contract
        )

    def load_inputs(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[ApprovedSemanticVersion, ManagedIntegrationContract]:
        return self._repository.load_inputs(tenant_id=tenant_id, operation_id=operation_id)

    def load_intent(self, *, tenant_id: str, operation_id: str) -> CatalogPublicationIntent:
        return self._repository.load_intent(tenant_id=tenant_id, operation_id=operation_id)

    def load_receipt(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogPublicationReceipt | None:
        return self._repository.load_receipt(tenant_id=tenant_id, operation_id=operation_id)

    def store_receipt(
        self,
        *,
        intent: CatalogPublicationIntent,
        receipt: CatalogPublicationReceipt,
        references: tuple[CatalogObjectRef, ...],
        observations: tuple[CatalogObjectSnapshot, ...],
    ) -> CatalogPublicationReceipt:
        return self._repository.store_receipt(
            intent=intent, receipt=receipt, references=references, observations=observations
        )

    def load_publication(
        self, *, tenant_id: str, publication_id: str
    ) -> tuple[CatalogPublicationIntent, CatalogPublicationReceipt, tuple[CatalogObjectRef, ...]]:
        return self._repository.load_publication(tenant_id=tenant_id, publication_id=publication_id)

    def load_observations(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[CatalogObjectSnapshot, ...]:
        return self._repository.load_observations(tenant_id=tenant_id, operation_id=operation_id)

    def effect_count(self, *, tenant_id: str) -> int:
        return self._repository.effect_count(tenant_id=tenant_id)


class _PublishedAuthorityResolver:
    def __init__(
        self,
        *,
        publications: CatalogPublicationRepository,
        publication_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._publications = publications
        self._publication_id = publication_id
        self._clock = clock

    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> FulfillmentAuthorityObservation | ResolutionFailure:
        matches = _semantic_matches(
            publications=self._publications,
            tenant_id=tenant_id,
            publication_id=self._publication_id,
            request=request,
        )
        if len(matches) > 1:
            return ResolutionFailure(
                reason_codes=("published_semantic_term_ambiguous",),
                constraint_refs=(),
                smallest_changes=(
                    "Ask about exactly one term in the workspace's current approved semantic "
                    "publication.",
                ),
                requester_safe_explanation=(
                    "This question names more than one term in the current governed catalog. "
                    "Ask about one term at a time."
                ),
            )
        if not matches:
            return ResolutionFailure(
                reason_codes=("published_semantic_term_not_found",),
                constraint_refs=(),
                smallest_changes=(
                    "Ask about one term in the workspace's current approved semantic publication.",
                ),
                requester_safe_explanation=(
                    "The current governed catalog does not contain one unambiguous term for "
                    "this question."
                ),
            )
        intent, _, _ = self._publications.load_publication(
            tenant_id=tenant_id,
            publication_id=self._publication_id,
        )
        semantic_version, integration_contract = self._publications.load_inputs(
            tenant_id=tenant_id,
            operation_id=intent.operation_id,
        )
        now = self._clock()
        policy_ref = ArtifactReference(
            artifact_id=f"policy-{integration_contract.contract_id}",
            version=integration_contract.version,
            digest=digest(integration_contract.access_policy),
        )
        classifications_by_id = {
            classification.object_id: classification
            for classification in semantic_version.classifications
        }
        classification_refs = tuple(
            ArtifactReference(
                artifact_id=classification_id,
                version=semantic_version.version,
                digest=digest(classifications_by_id[classification_id]),
            )
            for classification_id in integration_contract.access_policy.classification_refs
            if classification_id in classifications_by_id
        )
        product = integration_contract.destination_product
        return FulfillmentAuthorityObservation(
            tenant_id=tenant_id,
            catalog_publication_id=self._publication_id,
            requester_id=request.requester_id,
            requester_principal_ref=f"principal:{request.requester_id}",
            purpose_digest=digest(request.payload.purpose),
            authorization_policy_ref=policy_ref,
            approved_policy_refs=(policy_ref,),
            entitlement_observation_refs=tuple(
                ArtifactReference(
                    artifact_id=approval_id,
                    version=integration_contract.version,
                    digest=digest(approval_id),
                )
                for approval_id in integration_contract.approval_ids
            ),
            classification_rule_refs=classification_refs,
            permitted_data_product_refs=(
                ArtifactReference(
                    artifact_id=product.product_name,
                    version=integration_contract.version,
                    digest=digest(product),
                ),
            ),
            permitted_access_modes=("query",),
            maximum_expiry=now + timedelta(days=2),
            policy_authority_classifications=(
                integration_contract.access_policy.classification_refs
            ),
            freshness_observation_ref=None,
            quality_observation_refs=(),
            data_observation_refs=(),
            observed_at=now,
            valid_until=now + timedelta(hours=1),
        )


class _PublishedAnswerProvider:
    def __init__(self, *, publications: CatalogPublicationRepository, publication_id: str) -> None:
        self._publications = publications
        self._publication_id = publication_id

    def propose(
        self, *, request: InboxRequest, grounding: FulfillmentGroundingSnapshot
    ) -> StakeholderAnswerDraft:
        semantic_object = _semantic_match(
            publications=self._publications,
            tenant_id=request.tenant_id,
            publication_id=self._publication_id,
            request=request,
        )
        if semantic_object is None:
            raise ValueError("the request does not resolve to one published semantic term")
        definition = semantic_object.definition
        sentence_definition = definition[:1].lower() + definition[1:]
        metric_refs = tuple(
            reference
            for reference in grounding.metric_refs
            if reference.artifact_id == semantic_object.object_id
        )
        return StakeholderAnswerDraft(
            answer_text=f"{semantic_object.name} is {sentence_definition}",
            governed_dataset_refs=grounding.governed_dataset_refs,
            metric_refs=metric_refs,
            as_of=grounding.as_of,
            freshness_disposition=(
                "current" if grounding.freshness_observation_ref is not None else "not_applicable"
            ),
            material_quality_limitations=(),
            lineage_refs=grounding.lineage_refs,
            disclosure_classifications=(),
        )


class _GovernedAnswerExecutor:
    """Check the admitted answer against the recorded catalog publication and warehouse binding.

    This reads persisted state only: the binding record must be `ready` and the publication
    receipt must record a verified round trip. It does not query the warehouse or re-read the
    catalog at delivery time, so nothing downstream may describe the delivery as verified.
    """

    def __init__(
        self,
        *,
        publications: CatalogPublicationRepository,
        warehouse: WarehouseControlService,
        bindings: _PersistedWorkspaceBindingDirectory,
    ) -> None:
        self._publications = publications
        self._warehouse = warehouse
        self._bindings = bindings

    def execute(
        self,
        *,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        admission: FulfillmentAdmissionReceipt,
        grounding: FulfillmentGroundingSnapshot,
    ) -> tuple[StakeholderAnswerDraft, tuple[ArtifactReference, ...]]:
        if not isinstance(proposal.subject, StakeholderAnswerDraft):
            raise ValueError("the admitted proposal is not a stakeholder answer")
        if (
            admission.proposal_digest != digest(proposal)
            or request.tenant_id != grounding.tenant_id
        ):
            raise ValueError("the admitted answer does not bind the current grounding")
        binding_id = self._bindings.warehouse_binding_id(request.tenant_id)
        if binding_id is None:
            raise ValueError("the workspace has no managed warehouse binding")
        binding = self._warehouse.get(request.tenant_id, binding_id)
        if binding.lifecycle_state is not WarehouseBindingState.READY:
            raise ValueError("the managed warehouse binding is not ready")
        intent, receipt, _ = self._publications.load_publication(
            tenant_id=request.tenant_id,
            publication_id=grounding.catalog_publication_id,
        )
        observations = self._publications.load_observations(
            tenant_id=request.tenant_id, operation_id=intent.operation_id
        )
        if (
            digest(intent) != grounding.catalog_publication_intent_digest
            or receipt.round_trip_observation_digest
            != grounding.catalog_round_trip_observation_digest
            or not receipt.round_trip_verified
            or not observations
        ):
            raise ValueError("the catalog publication is not verified")
        warehouse_reference = ArtifactReference(
            artifact_id=binding.binding_id,
            version=binding.revision,
            digest=digest(binding),
        )
        references = (
            warehouse_reference,
            grounding.semantic_version_ref,
            grounding.integration_contract_ref,
            *proposal.subject.governed_dataset_refs,
            *proposal.subject.metric_refs,
            *proposal.subject.lineage_refs,
        )
        return proposal.subject, tuple(dict.fromkeys(references))


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
        self._reload()

    def bind_warehouse(self, *, tenant_id: str, binding_id: str) -> None:
        self._reload()
        super().bind_warehouse(tenant_id=tenant_id, binding_id=binding_id)
        self._record("warehouse", tenant_id, binding_id)

    def bind_catalog(self, *, tenant_id: str, binding_id: str) -> None:
        self._reload()
        super().bind_catalog(tenant_id=tenant_id, binding_id=binding_id)
        self._record("catalog", tenant_id, binding_id)

    def warehouse_binding_id(self, tenant_id: str) -> str | None:
        self._reload()
        return super().warehouse_binding_id(tenant_id)

    def catalog_binding_id(self, tenant_id: str) -> str | None:
        self._reload()
        return super().catalog_binding_id(tenant_id)

    def _reload(self) -> None:
        if not self._path.exists():
            return
        self._recorded = json.loads(self._path.read_text(encoding="utf-8"))
        self._warehouse = dict(self._recorded["warehouse"])
        self._catalog = dict(self._recorded["catalog"])

    def _record(self, kind: str, tenant_id: str, binding_id: str) -> None:
        self._recorded[kind][tenant_id] = binding_id
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self._path.parent, delete=False
        ) as temporary:
            temporary.write(json.dumps(self._recorded, indent=2, sort_keys=True))
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, self._path)


class GovernedConsoleDeployment:
    """The owning services, their databases, and the console that projects them."""

    def __init__(
        self,
        directory: Path,
        *,
        engine: str = "local-acceptance",
        answer_candidate_provider: AnswerCandidateProvider | None = None,
        publication_repository: CatalogPublicationRepository | None = None,
        catalog_search_health: CatalogSearchHealthReader | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.bindings = _PersistedWorkspaceBindingDirectory(directory / "bindings.json")
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
        supplied_publication_repository = publication_repository is not None
        if publication_repository is None:
            publication_repository, _, _ = published_repository(check_same_thread=False)
        recorded_publications = publication_repository.list_publications(tenant_id=TENANT)
        publications = recorded_publications
        active_catalog_binding_id = self.bindings.catalog_binding_id(TENANT)
        if supplied_publication_repository:
            binding_by_publication = {
                publication.publication_id: publication_repository.load_publication(
                    tenant_id=TENANT, publication_id=publication.publication_id
                )[0].catalog_binding_id
                for publication in recorded_publications
            }
            publications = tuple(
                publication
                for publication in recorded_publications
                if binding_by_publication[publication.publication_id] == active_catalog_binding_id
            )
            if recorded_publications and not publications:
                # A fresh state directory mints its own catalog binding, which can never be the
                # binding a live store was published through. Say which binding to record.
                raise ValueError(
                    "the publication store holds publications only for catalog binding(s) "
                    f"{', '.join(sorted(set(binding_by_publication.values())))}, but this "
                    "workspace's catalog binding is "
                    f"{active_catalog_binding_id or 'not yet recorded'}; record the store's "
                    f'binding for tenant {TENANT} under "catalog" in '
                    f"{directory / 'bindings.json'} before starting"
                )
        if not publications:
            raise ValueError(f"the publication store holds no publication for tenant {TENANT}")
        receipt = publications[0]
        self.publication_repository = publication_repository
        self.active_publication_id = receipt.publication_id
        self.fulfillment = FulfillmentService(
            request_service=self.requests,
            repository=self.fulfillment_repository,
            snapshot_resolver=SemanticFulfillmentSnapshotAdapter(
                publication_repository=publication_repository,
                authority_resolver=_PublishedAuthorityResolver(
                    publications=publication_repository,
                    publication_id=receipt.publication_id,
                    clock=_clock,
                ),
                clock=_clock,
            ),
            answer_candidate_provider=(
                answer_candidate_provider
                if answer_candidate_provider is not None
                else _PublishedAnswerProvider(
                    publications=publication_repository,
                    publication_id=receipt.publication_id,
                )
            ),
            answer_execution_provider=_GovernedAnswerExecutor(
                publications=publication_repository,
                warehouse=self.control,
                bindings=self.bindings,
            ),
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
        actors = InMemoryWorkspaceActorDirectory()
        actors.bind_actor(tenant_id=TENANT, actor_id=REQUESTER, display_name="Requester")
        actors.bind_actor(tenant_id=TENANT, actor_id=ARCHITECT, display_name="Data architect")
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
            catalog_search_health=catalog_search_health,
            catalog_publications=_ActivePublicationCatalog(
                self.publication_repository, self.active_publication_id
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
            fulfillment_execution_commands=self.fulfillment,
            fulfillment_preparation_commands=self.fulfillment,
            semantic_reviews=self.semantic_repository,
            semantic_review_commands=self.semantic_reviews,
            runs=DerivedTenantRunReader(lifecycles=self.lifecycles, evidence=self.evidence),
            # The store satisfies the receipt reader directly: a receipt records its
            # own tenant, so unlike a run there is nothing to derive and no adapter
            # whose only purpose would be to rename the call.
            acquisition_receipts=self.evidence,
            actors=actors,
            data_products=PolicyPermittedDataProductReader(
                repository=self.fulfillment_repository, requests=self.requests
            ),
            data_access_intake_available=False,
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
        operation_secrets = run_operation_secrets(clock=_operational_clock)

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
    parser.add_argument(
        "--publication-database",
        type=Path,
        help="Existing semantic publication database to use instead of the acceptance fixture",
    )
    parser.add_argument(
        "--catalog-search-url",
        help="Loopback Elasticsearch endpoint used for managed catalog readiness",
    )
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
    publication_repository = (
        None
        if arguments.publication_database is None
        else SQLiteCatalogPublicationRepository(
            str(arguments.publication_database), check_same_thread=False
        )
    )
    search_url = arguments.catalog_search_url
    if search_url is None and arguments.publication_database is not None:
        search_url = "http://127.0.0.1:9200"
    deployment = GovernedConsoleDeployment(
        arguments.directory,
        engine=arguments.engine,
        publication_repository=publication_repository,
        catalog_search_health=(
            None if search_url is None else _ElasticsearchSearchHealth(search_url)
        ),
    )
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
