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
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from pillarmesh_access_control import (
    AccessGrantApplicationService,
    CurrentEntitlementSnapshot,
    EntitlementPermission,
    RequestManagementAccessDeliveryReader,
    RequestManagementAdmittedAccessProposalReader,
    SQLiteAccessGrantRepository,
)
from pillarmesh_catalog_control import CatalogControlService, SQLiteCatalogRepository
from pillarmesh_console.app import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.contracts import ActorRole
from pillarmesh_console.governed_adapters import (
    CatalogBindingReader,
    CatalogControlBindingReader,
    CatalogSearchHealthReader,
    DashboardPublicationReader,
    DerivedTenantRunReader,
    GovernedProductIntentAuthority,
    GovernedWorkspaceIdentity,
    InMemoryWorkspaceActorDirectory,
    InMemoryWorkspaceBindingDirectory,
    InMemoryWorkspacePrincipalDirectory,
    PolicyPermittedDataProductReader,
    ProductPublicationDefinitionReader,
    WarehouseBindingReader,
    WarehouseControlBindingReader,
    WarehouseControlLifecycleCommands,
    WarehouseRepositoryOperationReader,
)
from pillarmesh_console.governed_backend import GovernedConsoleBackend
from pillarmesh_console.impact_projection import (
    ImpactViewProjector,
    RequestImpactProjectionReader,
)
from pillarmesh_console.operation_handles import InMemoryOperationHandleRepository
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    ImpactSubject,
    ManagedIntegrationContract,
    SemanticObject,
    digest,
)
from pillarmesh_contract_service import (
    AcquisitionActivationApproval,
    ProcessPackageService,
    SourceObservation,
    SQLiteAcquisitionContractLifecycleRepository,
    SQLiteProcessPackageRepository,
    SQLiteSourceObservationRepository,
    ValidatedSourceBinding,
)
from pillarmesh_evidence import SQLiteAcquisitionEvidenceWriter, SQLiteStore
from pillarmesh_knowledge_graph import (
    ContextEdge,
    ContextGraphProjector,
    ContextGraphRepository,
    ContextNode,
    ImpactAnalyzer,
    SourceRecordObservation,
)
from pillarmesh_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from pillarmesh_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    AccessEffectResult,
)
from pillarmesh_request_management import (
    AccessGrantAdmissionBinding,
    AccessGrantEffectTarget,
    AccessScopePreview,
    AnswerCandidateProvider,
    DataAccessRequest,
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentImpactBindingReader,
    FulfillmentPolicyCompiler,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    GraphImpactAdmissionResolver,
    InboxRequest,
    ProductIntentApprovalService,
    ProductIntentCandidateService,
    RequestManagementService,
    ResolutionFailure,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
    StakeholderQuestion,
)
from pillarmesh_runtime import (
    AnswerResultAccessEffectProvider,
    AnswerResultAccessTarget,
    compose_acquisition_application,
    opaque_reference_factory,
)
from pillarmesh_semantic_registry import (
    FulfillmentAuthorityObservation,
    SemanticFulfillmentSnapshotAdapter,
    SQLiteCatalogPublicationRepository,
    SQLiteSemanticVersionRepository,
)
from pillarmesh_semantic_registry.publication import (
    CatalogPublicationIntent,
    CatalogPublicationReceipt,
    CatalogPublicationRepository,
)
from pillarmesh_semantic_registry.repository import SQLiteSemanticRepository
from pillarmesh_semantic_registry.review import SemanticReviewService
from pillarmesh_state import (
    RunLifecycleSnapshot,
    RunService,
    SQLiteRunRepository,
)
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

from tests.acceptance.console_answer_runtime import (
    GovernedAnswerRuntime,
    GovernedAnswerRuntimeConfiguration,
)
from tests.acceptance.run_plan3b import (
    ScenarioFreshness,
    published_repository,
)
from tests.acceptance.run_plan4a import OfflinePlan4AHarness

TENANT = "tenant-a"
ARCHITECT = "architect-a"
REQUESTER = "requester-a"
DATA_OWNER = "data-owner-a"
POLICY_APPROVER = "policy-approver-a"
IMPACT_OWNER = "role:finance_data_owner"
REQUESTER_PRINCIPAL = f"principal:{REQUESTER}"
ARCHITECT_PRINCIPAL = "role:data_engineering_architect"
POLICY_AUTHORITY = "role:policy_authority"
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


class _PerCallSemanticVersionReader:
    """Loads approved semantic versions on whichever worker thread asks.

    The owning repository opens its SQLite connection in its constructor, and SQLite connections
    carry thread affinity, so a shared instance cannot serve threadpool routes.
    """

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion:
        repository = SQLiteSemanticVersionRepository(self._database_path)
        try:
            return repository.load(tenant_id, semantic_version_id, version)
        finally:
            repository.close()


class _PerCallSourceObservationReader:
    """Loads source observations on whichever worker thread asks, for the same reason."""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path

    def load(self, tenant_id: str, observation_id: str, version: int) -> SourceObservation:
        repository = SQLiteSourceObservationRepository(self._database_path)
        try:
            return repository.load(tenant_id, observation_id, version)
        finally:
            repository.close()


class _PerCallRunLifecycleReader:
    """Describes state-owned runs on whichever worker thread asks, for the same reason."""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path

    def describe_runs(self, tenant_id: str) -> tuple[RunLifecycleSnapshot, ...]:
        repository = SQLiteRunRepository(self._database_path)
        try:
            return RunService(repository, clock=_clock).describe_runs(tenant_id)
        finally:
            repository.close()


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
            (DATA_OWNER, IMPACT_OWNER),
            (POLICY_APPROVER, POLICY_AUTHORITY),
        }


class _DeploymentAccessRevocationAuthority:
    def __init__(self, requests: RequestManagementService) -> None:
        self._requests = requests

    def may_revoke(self, *, tenant_id: str, request_id: str, actor_id: str) -> bool:
        try:
            request = self._requests.get(tenant_id, request_id)
        except KeyError:
            return False
        return actor_id in (request.requester_id, ARCHITECT)


class _PublishedMetricImpactSubjectResolver:
    def resolve(
        self,
        *,
        tenant_id: str,
        proposal: FulfillmentProposal,
    ) -> ImpactSubject | None:
        subject = proposal.subject
        if not isinstance(subject, StakeholderAnswerDraft) or len(subject.metric_refs) != 1:
            return None
        return ImpactSubject(
            subject_kind="metric_version_change",
            subject_ref=subject.metric_refs[0].artifact_id,
            change_subject_digest=digest(subject),
        )


class _GovernedLocalImpactVisibility:
    def can_view(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        active_role: ActorRole,
        node: ContextNode,
    ) -> bool:
        return tenant_id == TENANT and actor_id == ARCHITECT and active_role == "data_architect"


class _PublishedImpactLabels:
    def __init__(self, *, subject_labels: dict[str, str], product_ref: str) -> None:
        self._subject_labels = dict(subject_labels)
        self._product_ref = product_ref

    def subject_label(self, tenant_id: str, subject_ref: str) -> str | None:
        return self._subject_labels.get(subject_ref) if tenant_id == TENANT else None

    def impact_label(self, tenant_id: str, node_id: str) -> str | None:
        if tenant_id != TENANT or node_id != self._product_ref:
            return None
        return "Revenue data product"

    def owner_label(self, tenant_id: str, owner_ref: str) -> str | None:
        if tenant_id == TENANT and owner_ref == IMPACT_OWNER:
            return "Finance data owner"
        return None

    def authority_label(self, tenant_id: str, authority_ref: str) -> str | None:
        return self.owner_label(tenant_id, authority_ref)


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
        intent, _, _ = self._publications.load_publication(
            tenant_id=tenant_id,
            publication_id=self._publication_id,
        )
        semantic_version, integration_contract = self._publications.load_inputs(
            tenant_id=tenant_id,
            operation_id=intent.operation_id,
        )
        product = integration_contract.destination_product
        if isinstance(request.payload, DataAccessRequest):
            if request.payload.data_product_id != product.product_name:
                return ResolutionFailure(
                    reason_codes=("published_data_product_not_found",),
                    constraint_refs=(),
                    smallest_changes=("Choose the current governed data product.",),
                    requester_safe_explanation=(
                        "The selected data product is not in the current governed catalog."
                    ),
                )
        else:
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
                        "Ask about one term in the workspace's current approved semantic "
                        "publication.",
                    ),
                    requester_safe_explanation=(
                        "The current governed catalog does not contain one unambiguous term for "
                        "this question."
                    ),
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
            permitted_access_modes=("dashboard", "query"),
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


class _PublishedAccessProvider:
    def __init__(self, *, publications: CatalogPublicationRepository, publication_id: str) -> None:
        self._publications = publications
        self._publication_id = publication_id

    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: object,
    ) -> AccessScopePreview:
        del policy
        if not isinstance(request.payload, DataAccessRequest):
            raise ValueError("access preview requires a data access request")
        intent, _, _ = self._publications.load_publication(
            tenant_id=request.tenant_id,
            publication_id=self._publication_id,
        )
        available_fields = frozenset(item.object_id for item in intent.semantic_objects)
        effective_fields = tuple(
            field for field in request.payload.requested_fields if field in available_fields
        )
        if not effective_fields:
            raise ValueError("none of the requested fields are published semantic terms")
        return AccessScopePreview(
            requester_principal_ref=f"principal:{request.requester_id}",
            data_product_ref=grounding.governed_dataset_refs[0],
            access_mode=request.payload.access_mode,
            requested_fields=request.payload.requested_fields,
            effective_object_refs=grounding.governed_dataset_refs,
            effective_fields=effective_fields,
            excluded_scopes=tuple(
                field for field in request.payload.requested_fields if field not in available_fields
            ),
            classifications=grounding.classification_refs,
            expires_at=request.payload.expires_at,
        )


class _PublishedProductOwnerResolver:
    def resolve(self, *, tenant_id: str, data_product_ref: ArtifactReference) -> str:
        if tenant_id != TENANT or not data_product_ref.artifact_id:
            raise ValueError("data product owner authority is unavailable")
        return IMPACT_OWNER


class _GovernedLocalEntitlementAuthority:
    def __init__(
        self,
        *,
        product_ref: ArtifactReference,
        semantic_refs: tuple[ArtifactReference, ...],
    ) -> None:
        self._product_ref = product_ref
        self._semantic_refs = semantic_refs

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot:
        values: dict[str, object] = {
            "schema_version": "1",
            "tenant_id": tenant_id,
            "principal_ref": principal_ref,
            "purpose_digest": purpose_digest,
            "connected_authority_ref": "governed-local-policy-authority",
            "source_revision": 1,
            "source_payload_digest": digest(
                {
                    "product_ref": self._product_ref,
                    "semantic_refs": self._semantic_refs,
                }
            ),
            "product_version_refs": (self._product_ref,),
            "semantic_refs": self._semantic_refs,
            "filter_domains": (),
            "permissions": ("dashboard", "query", "view"),
            "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
            "valid_until": datetime(2030, 1, 1, tzinfo=UTC),
        }
        snapshot_digest = digest(values)
        return CurrentEntitlementSnapshot.model_validate(
            {
                **values,
                "snapshot_id": f"entitlement-{snapshot_digest[:24]}",
                "snapshot_digest": snapshot_digest,
                "observation_id": f"observation-{snapshot_digest[:24]}",
                "resolved_at": _clock(),
            },
            strict=True,
        )


class _GovernedLocalGrantAdmissionResolver:
    def __init__(self, entitlements: _GovernedLocalEntitlementAuthority) -> None:
        self._entitlements = entitlements

    def bind(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        policy: object,
    ) -> AccessGrantAdmissionBinding:
        del policy
        if not isinstance(request.payload, DataAccessRequest) or not isinstance(
            proposal.subject, AccessScopePreview
        ):
            raise ValueError("grant admission requires an access proposal")
        entitlement = self._entitlements.resolve_current(
            tenant_id=tenant_id,
            principal_ref=proposal.subject.requester_principal_ref,
            purpose_digest=digest(request.payload.purpose),
        )
        permissions: tuple[EntitlementPermission, ...] = (
            ("dashboard", "view")
            if request.payload.access_mode == "dashboard"
            else ("query", "view")
        )
        targets = [
            AccessGrantEffectTarget(
                surface="result",
                provider_resource_ref=f"result:{proposal.subject.data_product_ref.artifact_id}",
            ),
            AccessGrantEffectTarget(
                surface="warehouse",
                provider_resource_ref=f"relation:{proposal.subject.data_product_ref.artifact_id}",
            ),
        ]
        if request.payload.access_mode == "dashboard":
            targets.append(
                AccessGrantEffectTarget(
                    surface="superset",
                    provider_resource_ref=(
                        f"dashboard:{proposal.subject.data_product_ref.artifact_id}"
                    ),
                )
            )
        return AccessGrantAdmissionBinding(
            grant_id="grant-" + digest((tenant_id, request.request_id, digest(proposal)))[:24],
            proposal_digest=digest(proposal),
            entitlement_snapshot_digest=entitlement.snapshot_digest,
            policy_revision=entitlement.source_revision,
            effective_at=_clock(),
            permissions=permissions,
            targets=tuple(targets),
        )


def _access_command_matches_grant(
    command: AccessEffectCommand,
    grants: SQLiteAccessGrantRepository,
) -> bool:
    grant = grants.load_current(command.tenant_id, command.grant_id)
    expected_state = "pending" if command.action == "apply" else "revocation_pending"
    if grant is None or grant.state != expected_state or grant.revision != command.grant_revision:
        return False
    target = next(
        (item for item in grant.effect_targets if item.surface == command.surface),
        None,
    )
    scope = {
        "domain": "pillarmesh-access-effect-scope-v1",
        "tenant_id": grant.tenant_id,
        "grant_id": grant.grant_id,
        "principal_ref": grant.principal_ref,
        "provider_resource_ref": command.provider_resource_ref,
        "fields": grant.fields,
        "permissions": command.permissions,
        "effective_at": grant.effective_at,
        "expires_at": grant.expires_at,
    }
    return bool(
        target is not None
        and target.provider_resource_ref == command.provider_resource_ref
        and command.principal_ref == grant.principal_ref
        and command.fields == grant.fields
        and command.effective_at == grant.effective_at
        and command.expires_at == grant.expires_at
        and command.scope_digest == digest(scope)
    )


class _GovernedLocalResultTargetAuthority:
    def __init__(self, grants: SQLiteAccessGrantRepository) -> None:
        self._grants = grants

    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget | None:
        if command.surface != "result" or not _access_command_matches_grant(command, self._grants):
            return None
        return AnswerResultAccessTarget(
            tenant_id=command.tenant_id,
            grant_id=command.grant_id,
            grant_revision=command.grant_revision,
            principal_ref=command.principal_ref,
            result_ref=command.provider_resource_ref,
            fields=command.fields,
            permissions=cast(tuple[Literal["download", "view"], ...], command.permissions),
            effective_at=command.effective_at,
            expires_at=command.expires_at,
            scope_digest=command.scope_digest,
        )


class _GovernedLocalWarehouseAccessProvider:
    surface: Literal["warehouse"] = "warehouse"

    def __init__(self, grants: SQLiteAccessGrantRepository) -> None:
        self._grants = grants

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        if command.surface != self.surface or not _access_command_matches_grant(
            command, self._grants
        ):
            raise AccessEffectProviderError(
                outcome="permanent_failure",
                provider_receipt_digest=digest(command),
            )
        return AccessEffectResult(
            surface=command.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.governed-local-warehouse-access.v1",
                    "command": command,
                }
            ),
        )


class _GovernedLocalDashboardAccessProvider:
    surface: Literal["superset"] = "superset"

    def __init__(self, grants: SQLiteAccessGrantRepository) -> None:
        self._grants = grants

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        if command.surface != self.surface or not _access_command_matches_grant(
            command, self._grants
        ):
            raise AccessEffectProviderError(
                outcome="permanent_failure",
                provider_receipt_digest=digest(command),
            )
        return AccessEffectResult(
            surface=command.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.governed-local-dashboard-access.v1",
                    "command": command,
                }
            ),
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
    contexts = {
        ARCHITECT: ("data_architect", "session-architect"),
        DATA_OWNER: ("data_owner", "session-data-owner"),
        POLICY_APPROVER: ("policy_approver", "session-policy-approver"),
    }
    role, session_id = contexts.get(actor, contexts[ARCHITECT])
    return TrustedActorContext(
        tenant_id=TENANT,
        actor_id=actor if actor in contexts else ARCHITECT,
        roles=(cast(ActorRole, role),),
        active_role=cast(ActorRole, role),
        session_id=session_id,
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
        warehouse_binding_reader: WarehouseBindingReader | None = None,
        catalog_binding_reader: CatalogBindingReader | None = None,
        answer_runtime_configuration: GovernedAnswerRuntimeConfiguration | None = None,
        product_publications: ProductPublicationDefinitionReader | None = None,
        dashboards: DashboardPublicationReader | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.bindings = _PersistedWorkspaceBindingDirectory(directory / "bindings.json")
        self.warehouse_path = str(directory / "warehouse.sqlite3")
        self.request_path = str(directory / "requests.sqlite3")
        self._warehouse_connection = _worker_thread_connection(self.warehouse_path)
        self.warehouse_repository: SQLiteWarehouseRepository
        if engine == "postgresql":
            from tests.acceptance.console_postgresql_engine import (
                PostgreSQLAcceptanceWarehouseRepository,
            )

            self.warehouse_repository = PostgreSQLAcceptanceWarehouseRepository(
                connection=self._warehouse_connection,
                workspace_directory=directory,
            )
        else:
            self.warehouse_repository = SQLiteWarehouseRepository(
                connection=self._warehouse_connection
            )
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
        request_clock = (
            answer_runtime_configuration.clock
            if answer_runtime_configuration is not None
            else _clock
        )
        self.requests = RequestManagementService(self.request_repository, clock=request_clock)
        self.product_intent_candidates = ProductIntentCandidateService(
            self.request_repository,
            clock=_clock,
        )
        # Approval derives constraints from these two governed stores, never from the proposer.
        self.semantic_versions_path = str(directory / "semantic-versions.sqlite3")
        self.source_observations_path = str(directory / "source-observations.sqlite3")
        self.state_runs_path = str(directory / "state-runs.sqlite3")
        SQLiteRunRepository(self.state_runs_path).close()
        SQLiteSemanticVersionRepository(self.semantic_versions_path).close()
        SQLiteSourceObservationRepository(self.source_observations_path).close()
        self.product_intent_approvals = ProductIntentApprovalService(
            self.request_repository,
            clock=_clock,
            authority=GovernedProductIntentAuthority(
                semantic_versions=_PerCallSemanticVersionReader(self.semantic_versions_path),
                source_observations=_PerCallSourceObservationReader(self.source_observations_path),
            ),
        )
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
        publication_intent, _, _ = publication_repository.load_publication(
            tenant_id=TENANT,
            publication_id=receipt.publication_id,
        )
        semantic_version, integration_contract = publication_repository.load_inputs(
            tenant_id=TENANT,
            operation_id=publication_intent.operation_id,
        )
        self.context_graph_repository = ContextGraphRepository(directory / "context-graph.sqlite3")
        contract_reference = ArtifactReference(
            artifact_id=integration_contract.contract_id,
            version=integration_contract.version,
            digest=digest(integration_contract),
        )
        product_node_id = f"data-product:{integration_contract.destination_product.product_name}"
        metric_references = tuple(
            ArtifactReference(
                artifact_id=metric.object_id,
                version=semantic_version.version,
                digest=digest(metric),
            )
            for metric in semantic_version.metrics
        )
        product_observation = SourceRecordObservation(
            tenant_id=TENANT,
            source_record_ref=contract_reference,
            producer="contract-service",
            observed_at=semantic_version.created_at,
            validity="valid",
        )
        metric_nodes = tuple(
            ContextNode(
                tenant_id=TENANT,
                node_id=reference.artifact_id,
                node_kind="metric_version",
                owner_ref=IMPACT_OWNER,
                provenance=SourceRecordObservation(
                    tenant_id=TENANT,
                    source_record_ref=reference,
                    producer="semantic-registry",
                    observed_at=semantic_version.created_at,
                    validity="valid",
                ),
            )
            for reference in metric_references
        )
        impact_snapshot = ContextGraphProjector().rebuild(
            tenant_id=TENANT,
            nodes=(
                *metric_nodes,
                ContextNode(
                    tenant_id=TENANT,
                    node_id=product_node_id,
                    node_kind="data_product",
                    owner_ref=IMPACT_OWNER,
                    provenance=product_observation,
                ),
            ),
            edges=tuple(
                ContextEdge(
                    tenant_id=TENANT,
                    edge_id=f"metric-to-product:{reference.artifact_id}",
                    source_node_id=reference.artifact_id,
                    target_node_id=product_node_id,
                    relationship="materializes",
                    evidence_kind="validated",
                    confidence=Decimal("1"),
                    evidence_refs=(reference, contract_reference),
                    provenance=product_observation,
                )
                for reference in metric_references
            ),
        )
        self.context_graph_repository.replace(impact_snapshot)
        self.impact_resolver = GraphImpactAdmissionResolver(
            repository=self.context_graph_repository,
            analyzer=ImpactAnalyzer(),
            subject_resolver=_PublishedMetricImpactSubjectResolver(),
            clock=_clock,
        )
        impact_labels = _PublishedImpactLabels(
            subject_labels={metric.object_id: metric.name for metric in semantic_version.metrics},
            product_ref=product_node_id,
        )
        product_ref = ArtifactReference(
            artifact_id=integration_contract.destination_product.product_name,
            version=integration_contract.version,
            digest=digest(integration_contract.destination_product),
        )
        semantic_refs = tuple(
            ArtifactReference(
                artifact_id=semantic_object.object_id,
                version=semantic_version.version,
                digest=digest(semantic_object),
            )
            for semantic_object in publication_intent.semantic_objects
        )
        self.access_path = str(directory / "access.sqlite3")
        self.access_grants = SQLiteAccessGrantRepository(
            _worker_thread_connection(self.access_path)
        )
        self.access_entitlements = _GovernedLocalEntitlementAuthority(
            product_ref=product_ref,
            semantic_refs=semantic_refs,
        )
        admitted_access = RequestManagementAdmittedAccessProposalReader(
            requests=self.requests,
            fulfillment=self.fulfillment_repository,
        )
        self.result_access = AnswerResultAccessEffectProvider(
            _worker_thread_connection(str(directory / "result-access.sqlite3")),
            targets=_GovernedLocalResultTargetAuthority(self.access_grants),
            clock=_clock,
        )
        self.access_application = AccessGrantApplicationService(
            grants=self.access_grants,
            admitted_proposals=admitted_access,
            entitlements=self.access_entitlements,
            providers=(
                self.result_access,
                _GovernedLocalDashboardAccessProvider(self.access_grants),
                _GovernedLocalWarehouseAccessProvider(self.access_grants),
            ),
            clock=_clock,
            revocation_authority=_DeploymentAccessRevocationAuthority(self.requests),
        )
        access_delivery_reader = RequestManagementAccessDeliveryReader(
            grants=self.access_grants,
            fulfillment=self.fulfillment_repository,
        )
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
            access_candidate_provider=_PublishedAccessProvider(
                publications=publication_repository,
                publication_id=receipt.publication_id,
            ),
            data_product_owner_resolver=_PublishedProductOwnerResolver(),
            access_grant_admission_resolver=_GovernedLocalGrantAdmissionResolver(
                self.access_entitlements
            ),
            access_grant_activation_reader=access_delivery_reader,
            authority_role_resolver=_DeploymentRoleResolver(),
            impact_admission_resolver=self.impact_resolver,
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
        self.process_package_path = str(directory / "process-packages.sqlite3")
        self.process_package_repository = SQLiteProcessPackageRepository(self.process_package_path)
        self.process_packages = ProcessPackageService(
            self.process_package_repository,
            clock=_clock,
        )
        self.evidence_path = directory / "evidence.sqlite3"
        self.evidence = SQLiteStore.open(self.evidence_path, check_same_thread=False)
        # The governed-local UI drives the same composed acquisition boundary as a
        # deployment. Its PostgreSQL session is the deterministic Plan 4A source so
        # browser testing remains offline, while lifecycle, state, artifacts, and
        # evidence use their durable implementations.
        self.source_acquisition = OfflinePlan4AHarness(
            directory / "source-acquisition",
            check_same_thread=False,
        )
        source_authority = self.source_acquisition.register_tenant(TENANT)
        source_contract = source_authority.contract
        source_binding = source_authority.binding
        self.lifecycles.activate_contract(
            idempotency_key="governed-local-managed-source-v1",
            contract=source_contract,
            approval=AcquisitionActivationApproval(
                tenant_id=TENANT,
                process_package_ref=source_contract.process_package_ref,
                product_intent_ref=source_contract.product_intent_ref,
                destination_product_ref=source_contract.destination_product_ref,
                approved_by=source_contract.activated_by,
                approved_at=source_contract.activated_at,
            ),
            source_validation=ValidatedSourceBinding(
                tenant_id=TENANT,
                source_binding_ref=source_binding.binding_id,
                source_binding_revision=source_binding.revision,
                credential_revision=source_binding.credential_revision,
                capability_profile_digest=source_contract.capability_profile_digest,
                source_observation_ref=source_contract.source_observation_ref,
                source_observation_digest=source_contract.source_observation_digest,
                validated_at=source_contract.activated_at,
            ),
        )
        self.acquisition = compose_acquisition_application(
            contract_repository=self.lifecycles,
            binding_repository=self.source_acquisition,
            observation_resolver=self.source_acquisition.load_observation,
            provider_resolver=self.source_acquisition.resolve_provider,
            state_store=self.source_acquisition.state,
            artifact_store=self.source_acquisition.artifact_store,
            evidence_writer=SQLiteAcquisitionEvidenceWriter(self.evidence),
            reference_factory=opaque_reference_factory(),
            clock=self.source_acquisition.clock,
        )
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
        principals.bind_principal(
            tenant_id=TENANT,
            actor_id=DATA_OWNER,
            role="data_owner",
            principal_ref=IMPACT_OWNER,
        )
        principals.bind_principal(
            tenant_id=TENANT,
            actor_id=POLICY_APPROVER,
            role="policy_approver",
            principal_ref=POLICY_AUTHORITY,
        )
        self.answer_runtime = (
            GovernedAnswerRuntime(
                directory / "answer-runtime",
                requests=self.request_repository,
                request_service=self.requests,
                principals=principals,
                configuration=answer_runtime_configuration,
            )
            if answer_runtime_configuration is not None
            else None
        )
        actors = InMemoryWorkspaceActorDirectory()
        actors.bind_actor(tenant_id=TENANT, actor_id=REQUESTER, display_name="Requester")
        actors.bind_actor(tenant_id=TENANT, actor_id=ARCHITECT, display_name="Data architect")
        actors.bind_actor(tenant_id=TENANT, actor_id=DATA_OWNER, display_name="Finance data owner")
        actors.bind_actor(
            tenant_id=TENANT,
            actor_id=POLICY_APPROVER,
            display_name="Policy approver",
        )
        self.backend = GovernedConsoleBackend(
            identity=GovernedWorkspaceIdentity(
                tenant_ref="tenant-governed",
                tenant_display_name="Governed local tenant",
                workspace_ref="workspace-governed",
                workspace_display_name="Revenue to cash",
            ),
            operation_handles=InMemoryOperationHandleRepository(),
            warehouse_bindings=warehouse_binding_reader
            or WarehouseControlBindingReader(service=self.control, directory=self.bindings),
            catalog_bindings=catalog_binding_reader
            or CatalogControlBindingReader(service=self.catalog, directory=self.bindings),
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
            fulfillment_execution_commands=self.answer_runtime or self.fulfillment,
            fulfillment_access_execution_commands=self.fulfillment,
            access_grant_commands=self.access_application,
            access_grants=self.access_grants,
            access_revocation_commands=self.access_application,
            fulfillment_preparation_commands=self.fulfillment,
            product_intent_reviews=self.product_intent_candidates,
            product_intent_commands=self.product_intent_approvals,
            process_package_commands=self.process_packages,
            semantic_reviews=self.semantic_repository,
            semantic_review_commands=self.semantic_reviews,
            runs=DerivedTenantRunReader(lifecycles=self.lifecycles, evidence=self.evidence),
            run_lifecycle=_PerCallRunLifecycleReader(self.state_runs_path),
            incidents=(self.answer_runtime.incidents if self.answer_runtime is not None else None),
            impact_reader=RequestImpactProjectionReader(
                bindings=FulfillmentImpactBindingReader(self.fulfillment_repository),
                analyzer=self.impact_resolver,
                visibility=_GovernedLocalImpactVisibility(),
                projector=ImpactViewProjector(impact_labels),
            ),
            # The store satisfies the receipt reader directly: a receipt records its
            # own tenant, so unlike a run there is nothing to derive and no adapter
            # whose only purpose would be to rename the call.
            acquisition_receipts=self.evidence,
            acquisition_commands=self.acquisition,
            actors=actors,
            data_products=PolicyPermittedDataProductReader(
                repository=self.fulfillment_repository, requests=self.requests
            ),
            product_publications=product_publications,
            dashboards=dashboards,
            answer_results=(
                self.answer_runtime.results if self.answer_runtime is not None else None
            ),
            verified_answers=(
                self.answer_runtime.answers if self.answer_runtime is not None else None
            ),
            answer_downloads=(
                self.answer_runtime.downloads if self.answer_runtime is not None else None
            ),
            data_access_intake_available=True,
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
        if actor is not None and actor not in (ARCHITECT, REQUESTER, DATA_OWNER, POLICY_APPROVER):
            raise ValueError(
                "local actor must be the architect, requester, data owner, or policy approver"
            )
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
        closings = (
            *((self.answer_runtime,) if self.answer_runtime is not None else ()),
            self.warehouse_repository,
            self.request_repository,
            self.catalog_repository,
            self.semantic_repository,
            self.publication_repository,
            self.source_acquisition,
            self.lifecycles,
            self.process_package_repository,
            self.evidence,
        )
        for closing in closings:
            try:
                closing.close()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def _actor_for(self, request: Request) -> TrustedActorContext:
        requested = request.headers.get(ACTOR_HEADER, ARCHITECT)
        allowed = (ARCHITECT, REQUESTER, DATA_OWNER, POLICY_APPROVER)
        return _context(requested if requested in allowed else ARCHITECT)


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
        choices=(ARCHITECT, REQUESTER, DATA_OWNER, POLICY_APPROVER),
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
