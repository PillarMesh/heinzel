from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogControlService,
    CatalogValidationEvidence,
    SQLiteCatalogRepository,
)
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ContractFormationInput,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FreshnessRequirement,
    InformationKind,
    ManagedIntegrationContract,
    QualityPolicy,
    TriggerRequirement,
    canonical_bytes,
    digest,
)
from heinzel_contract_service import (
    BusinessProcessManifest,
    FormationAuthorityObservation,
    FormationDecisionBinding,
    FormationReferenceLoader,
    FormationReviewBundle,
    FormationReviewItem,
    IntegrationContractFormationService,
    ProcessPackageReceipt,
    ProcessPackageService,
    SourceObservation,
    SQLiteProcessPackageRepository,
    SQLiteSourceObservationRepository,
)
from heinzel_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from heinzel_request_management import (
    DecisionKind,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SQLiteRequestRepository,
)
from heinzel_semantic_registry import (
    ApprovalCompilationInput,
    ApprovedSemanticCompiler,
    AuthorityObservation,
    AuthorityResolution,
    AuthorityResolutionStatus,
    AuthorityResolver,
    AuthoritySourceKind,
    CatalogPublicationIntent,
    CatalogPublicationReceipt,
    DeterministicManifestExtractor,
    OntologyReviewBundle,
    ReviewItemDecision,
    SemanticCandidateSet,
    SemanticPublicationService,
    SemanticReviewService,
    SQLiteCatalogPublicationRepository,
    SQLiteSemanticRepository,
    SQLiteSemanticVersionRepository,
)

_NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
_OWNER_ACTOR = "business-owner"
_OWNER_ROLE = "role:business_owner"


@dataclass(frozen=True, slots=True)
class ProcessPackage:
    package_id: str
    package_version: int
    narrative: bytes
    manifest: bytes


@dataclass(frozen=True, slots=True)
class OwnerDecision:
    review_bundle_digest: str
    candidate_set_digest: str
    approved: bool


@dataclass(frozen=True, slots=True)
class ExactCleanupTarget:
    resource_kind: str
    resource_id: str


class AcceptanceIdentifiers(ArtifactModel):
    process_package_id: str
    candidate_set_id: str
    authority_observation_id: str
    review_bundle_id: str
    semantic_version_id: str
    contract_id: str
    catalog_binding_id: str
    publication_receipt_id: str
    request_id: str


class SemanticFormationResult(ArtifactModel):
    authority_resolution: AuthorityResolution
    request: InboxRequest
    execution_occurred: bool
    identifiers: AcceptanceIdentifiers
    catalog_binding: CatalogBinding | None = None
    candidate_set: SemanticCandidateSet | None = None
    authority_observation: AuthorityObservation | None = None
    authority_observations: tuple[AuthorityObservation, ...] = ()
    semantic_version: ApprovedSemanticVersion | None = None
    contract: ManagedIntegrationContract | None = None
    publication_receipt: CatalogPublicationReceipt | None = None


class _SemanticFormationReplayRecord(ArtifactModel):
    input_digest: str
    result: SemanticFormationResult


class _SemanticFormationReplayRepository:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load(self, *, tenant_id: str, correlation_id: str) -> _SemanticFormationReplayRecord | None:
        path = self._path(tenant_id=tenant_id, correlation_id=correlation_id)
        try:
            return _SemanticFormationReplayRecord.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None

    def store(
        self, *, tenant_id: str, correlation_id: str, record: _SemanticFormationReplayRecord
    ) -> _SemanticFormationReplayRecord:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(tenant_id=tenant_id, correlation_id=correlation_id)
        payload = canonical_bytes(record)
        if path.exists():
            stored = _SemanticFormationReplayRecord.model_validate_json(path.read_bytes())
            if stored != record:
                raise ValueError("correlation replay record differs from persisted result")
            return stored
        temporary = path.with_name(path.name + ".temporary")
        temporary.write_bytes(payload)
        temporary.replace(path)
        return record

    def _path(self, *, tenant_id: str, correlation_id: str) -> Path:
        identity = digest(
            {
                "domain": "heinzel-semantic-formation-replay-v1",
                "tenant_id": tenant_id,
                "correlation_id": correlation_id,
            }
        )
        return self._directory / f"{identity}.json"


class StrictValidatingCatalog:
    """Provider double whose effects are verified by the real publication service."""

    provider_version = "offline-openmetadata-1.13.3"

    def __init__(self, *, refund_is_legacy_attribute: bool = False) -> None:
        self.refund_is_legacy_attribute = refund_is_legacy_attribute
        self.provider_effect_count = 0
        self.publication_effect_count = 0
        self.cleanup_effect_count = 0
        self._observations: dict[str, CatalogObjectSnapshot] = {}

    def publish(
        self, *, tenant_id: str, intent: CatalogPublicationIntent
    ) -> tuple[CatalogObjectRef, ...]:
        self.provider_effect_count += 1
        self.publication_effect_count += 1
        references = [
            self._record(
                tenant_id=tenant_id,
                object_kind="namespace",
                logical_identity="namespace",
                payload={"name": tenant_id, "namespace": tenant_id},
            )
        ]
        for identity, semantic_object in zip(
            intent.semantic_identities, intent.semantic_objects, strict=True
        ):
            references.append(
                self._record(
                    tenant_id=tenant_id,
                    object_kind="glossary_term",
                    logical_identity=identity,
                    payload={
                        "name": semantic_object.name,
                        "definition": semantic_object.definition,
                        "owner_ref": "runtime",
                        "provenance_ref": intent.contract_reference.digest,
                    },
                )
            )
            references.append(
                self._record(
                    tenant_id=tenant_id,
                    object_kind="classification",
                    logical_identity=digest(
                        {"operation_id": intent.operation_id, "subject": identity}
                    ),
                    payload={
                        "subject_ref": identity,
                        "classification_ref": intent.contract_digest,
                        "provenance_ref": intent.semantic_version_digest,
                    },
                )
            )
        if len(intent.semantic_identities) > 1:
            references.append(
                self._record(
                    tenant_id=tenant_id,
                    object_kind="lineage",
                    logical_identity=digest(
                        {"operation_id": intent.operation_id, "kind": "lineage"}
                    ),
                    payload={
                        "from_ref": intent.semantic_identities[0],
                        "to_ref": intent.semantic_identities[1],
                        "producer_ref": intent.contract_digest,
                        "evidence_ref": intent.semantic_version_digest,
                    },
                )
            )
        return tuple(references)

    def observe(self, *, tenant_id: str, reference: CatalogObjectRef) -> CatalogObjectSnapshot:
        observation = self._observations.get(reference.stable_identity)
        if observation is None or observation.tenant_key != tenant_id:
            raise KeyError(reference.stable_identity)
        return observation

    def cleanup(self, targets: tuple[ExactCleanupTarget, ...]) -> None:
        self.cleanup_effect_count += len(targets)

    def _record(
        self,
        *,
        tenant_id: str,
        object_kind: Literal["namespace", "glossary_term", "classification", "lineage"],
        logical_identity: str,
        payload: dict[str, str],
    ) -> CatalogObjectRef:
        stable_identity = f"{object_kind}:{tenant_id}:{logical_identity}"
        reference = CatalogObjectRef(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            normalized_digest=digest({"identity": stable_identity}),
        )
        self._observations[stable_identity] = CatalogObjectSnapshot(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            logical_identity=logical_identity,
            object_kind=object_kind,
            normalized_payload=payload,
            normalized_digest=digest(payload),
        )
        return reference


class _FormationLoader(FormationReferenceLoader):
    def __init__(
        self,
        *,
        process_receipts: dict[tuple[str, str], ProcessPackageReceipt],
        semantic_repository: SQLiteSemanticRepository,
        semantic_version_repository: SQLiteSemanticVersionRepository,
        request_service: RequestManagementService,
        observation_ids: dict[tuple[str, str], str],
        review_bundle_ids: dict[tuple[str, str], str],
        decision_requests: dict[tuple[str, str], tuple[str, str]],
        source_observation_repository: SQLiteSourceObservationRepository,
    ) -> None:
        self._process_receipts = process_receipts
        self._semantic_repository = semantic_repository
        self._semantic_version_repository = semantic_version_repository
        self._request_service = request_service
        self._observation_ids = observation_ids
        self._review_bundle_ids = review_bundle_ids
        self._decision_requests = decision_requests
        self._source_observation_repository = source_observation_repository

    def load_semantic_version(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ApprovedSemanticVersion | None:
        try:
            semantic_version = self._semantic_version_repository.load(
                tenant_id, reference.artifact_id, reference.version
            )
        except KeyError:
            return None
        expected = ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        )
        return semantic_version if reference == expected else None

    def load_process_receipt(
        self, tenant_id: str, reference: ArtifactReference
    ) -> ProcessPackageReceipt | None:
        receipt = self._process_receipts.get((tenant_id, reference.artifact_id))
        if receipt is None:
            return None
        if receipt.version != reference.version or receipt.original_digest != reference.digest:
            return None
        return receipt

    def has_source_observation(self, tenant_id: str, reference: ArtifactReference) -> bool:
        return self._source_observation_repository.has_current(tenant_id, reference, now=_NOW)

    def load_authority_observation(
        self, tenant_id: str, observation_digest: str
    ) -> FormationAuthorityObservation | None:
        observation_id = self._observation_ids.get((tenant_id, observation_digest))
        if observation_id is None:
            return None
        return self._semantic_repository.load_observation(tenant_id, observation_id)

    def load_review_bundle(
        self, tenant_id: str, review_bundle_digest: str
    ) -> FormationReviewBundle | None:
        bundle_id = self._review_bundle_ids.get((tenant_id, review_bundle_digest))
        if bundle_id is None:
            return None
        bundle = self._semantic_repository.load_review_bundle(tenant_id, bundle_id)
        return FormationReviewBundle(
            tenant_id=bundle.tenant_id,
            items=tuple(
                FormationReviewItem(
                    semantic_revision_digest=item.semantic_revision_digest,
                    required_authority_ref=item.required_authority_ref,
                    status=item.status,
                )
                for item in bundle.items
            ),
        )

    def load_decision_binding(
        self, tenant_id: str, approval_id: str
    ) -> FormationDecisionBinding | None:
        request_and_authority = self._decision_requests.get((tenant_id, approval_id))
        if request_and_authority is None:
            return None
        request_id, authority_ref = request_and_authority
        decision = next(
            (
                item
                for item in self._request_service.list_decisions(tenant_id, request_id)
                if item.decision_id == approval_id
            ),
            None,
        )
        if decision is None:
            return None
        return FormationDecisionBinding(
            approval_id=decision.decision_id,
            subject_digest=decision.subject_digest,
            authority_ref=authority_ref,
        )


def strict_validating_catalog() -> StrictValidatingCatalog:
    return StrictValidatingCatalog()


def legacy_refund_attribute_catalog() -> StrictValidatingCatalog:
    return StrictValidatingCatalog(refund_is_legacy_attribute=True)


def revenue_to_cash_package() -> ProcessPackage:
    return ProcessPackage(
        package_id="package-revenue-to-cash",
        package_version=1,
        narrative=b"Revenue to Cash: invoice, payment, and settlement.\n",
        manifest=_manifest_bytes(is_refund=False),
    )


def revised_revenue_to_cash_package() -> ProcessPackage:
    return ProcessPackage(
        package_id="package-revenue-to-cash-revised",
        package_version=2,
        narrative=(
            b"Revenue to Cash revision: invoice, payment, settlement, and reconciliation.\n"
        ),
        manifest=_manifest_bytes(is_refund=False),
    )


def refund_entity_package() -> ProcessPackage:
    return ProcessPackage(
        package_id="package-refund-entity",
        package_version=1,
        narrative=b"Refund entity with legacy refund attribute authority.\n",
        manifest=_manifest_bytes(is_refund=True),
    )


class OfflineSemanticFormationHarness:
    """Test-only composition root for the complete governed semantic formation journey."""

    def __init__(self, *, database_path: Path, catalog: StrictValidatingCatalog) -> None:
        self.database_path = database_path
        self.catalog = catalog
        self._process_repository = SQLiteProcessPackageRepository(str(database_path))
        self._process_service = ProcessPackageService(self._process_repository, clock=lambda: _NOW)
        self._source_observation_repository = SQLiteSourceObservationRepository(
            str(_sibling_database(database_path, "source-observations"))
        )
        self._semantic_repository = SQLiteSemanticRepository(
            str(_sibling_database(database_path, "semantic"))
        )
        self._semantic_version_repository = SQLiteSemanticVersionRepository(
            str(_sibling_database(database_path, "semantic-versions"))
        )
        self._request_repository = SQLiteRequestRepository.open(
            str(_sibling_database(database_path, "requests"))
        )
        self._request_service = RequestManagementService(
            self._request_repository, clock=lambda: _NOW
        )
        self._catalog_repository = SQLiteCatalogRepository(
            str(_sibling_database(database_path, "catalog"))
        )
        self._catalog_control = CatalogControlService(self._catalog_repository, clock=lambda: _NOW)
        self._publication_repository = SQLiteCatalogPublicationRepository(
            str(_sibling_database(database_path, "publications"))
        )
        self._extractor = DeterministicManifestExtractor(
            self._semantic_repository,
            extractor_id="heinzel-bounded-markdown",
            extractor_version="1.0.0",
            clock=lambda: _NOW,
        )
        self._review_service = SemanticReviewService(
            semantic_repository=self._semantic_repository,
            request_service=self._request_service,
            clock=lambda: _NOW,
        )
        self._publication_service = SemanticPublicationService(
            repository=self._publication_repository,
            provider=catalog,
            semantic_repository=self._semantic_repository,
            semantic_version_repository=self._semantic_version_repository,
            request_service=self._request_service,
            clock=lambda: _NOW,
        )
        self._replay_repository = _SemanticFormationReplayRepository(
            database_path.with_name(database_path.stem + "-replays")
        )
        self._process_receipts: dict[tuple[str, str], ProcessPackageReceipt] = {}
        self._observation_ids: dict[tuple[str, str], str] = {}
        self._review_bundle_ids: dict[tuple[str, str], str] = {}
        self._decision_requests: dict[tuple[str, str], tuple[str, str]] = {}
        self._source_observation_refs: dict[str, tuple[ArtifactReference, ...]] = {}
        self._semantic_version_counts: dict[str, int] = {}
        self._review_submission_counts: dict[str, int] = {}
        self._contracts: dict[tuple[str, str], ManagedIntegrationContract] = {}
        self._cleanup_targets: set[tuple[str, str, str]] = set()

    def close(self) -> None:
        self._publication_repository.close()
        self._catalog_repository.close()
        self._request_repository.close()
        self._semantic_version_repository.close()
        self._semantic_repository.close()
        self._source_observation_repository.close()
        self._process_repository.close()

    def run(
        self,
        *,
        tenant_id: str,
        process_package: ProcessPackage,
        owner_decisions: tuple[OwnerDecision, ...] | None = None,
        correlation_id: str | None = None,
    ) -> SemanticFormationResult:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        try:
            process_package.narrative.decode("utf-8")
            manifest = BusinessProcessManifest.model_validate_json(process_package.manifest)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("process package bytes are invalid") from None
        package_material = {
            "package_id": process_package.package_id,
            "package_version": process_package.package_version,
            "narrative_digest": sha256(process_package.narrative).hexdigest(),
            "manifest_source_digest": sha256(process_package.manifest).hexdigest(),
        }
        replay_identity = correlation_id or digest(
            {"tenant_id": tenant_id, "package": package_material}
        )
        input_digest = digest(
            {"tenant_id": tenant_id, "package": package_material, "manifest": manifest}
        )
        replay_record = self._replay_repository.load(
            tenant_id=tenant_id, correlation_id=replay_identity
        )
        if replay_record is not None:
            if replay_record.input_digest != input_digest:
                raise ValueError("correlation replay input differs from the original request")
            receipt = self._rehydrate_replay(
                tenant_id=tenant_id,
                process_package=process_package,
                manifest=manifest,
                result=replay_record.result,
            )
            candidate_set = self._extractor.extract(
                tenant_id=tenant_id,
                receipt=receipt,
                manifest=manifest,
                original=process_package.narrative,
            )
            self._verify_replay(
                tenant_id=tenant_id,
                result=replay_record.result,
                candidate_set=candidate_set,
            )
            return replay_record.result

        receipt = self._upload_or_verify(tenant_id=tenant_id, process_package=process_package)
        self._source_observation_refs[tenant_id] = (
            self._record_source_observation(
                tenant_id=tenant_id,
                receipt=receipt,
                manifest=manifest,
            ),
        )
        candidate_set = self._extractor.extract(
            tenant_id=tenant_id,
            receipt=receipt,
            manifest=manifest,
            original=process_package.narrative,
        )
        observations, resolutions = self._resolve_authority(
            tenant_id=tenant_id, candidate_set=candidate_set
        )
        bundle = self._review_service.create_bundle(
            tenant_id=tenant_id,
            candidate_set_id=candidate_set.set_id,
            candidate_set_revision=candidate_set.revision,
            resolutions=resolutions,
        )
        self._review_bundle_ids[(tenant_id, digest(bundle))] = bundle.bundle_id
        self._validate_owner_decisions(
            owner_decisions=owner_decisions, bundle=bundle, candidate_set=candidate_set
        )

        if any(
            resolution.status is not AuthorityResolutionStatus.RESOLVED
            for resolution in resolutions
        ):
            result = self._finish_no_valid_plan(
                tenant_id=tenant_id,
                receipt=receipt,
                candidate_set=candidate_set,
                observations=observations,
                resolutions=resolutions,
                bundle=bundle,
            )
        else:
            result = self._finish_success(
                tenant_id=tenant_id,
                receipt=receipt,
                candidate_set=candidate_set,
                observations=observations,
                resolutions=resolutions,
                bundle=bundle,
            )
        return self._replay_repository.store(
            tenant_id=tenant_id,
            correlation_id=replay_identity,
            record=_SemanticFormationReplayRecord(input_digest=input_digest, result=result),
        ).result

    def cleanup(self, *, tenant_id: str, targets: tuple[ExactCleanupTarget, ...]) -> None:
        if not targets or any(
            target.resource_id in {"*", "unrecorded", ""}
            or (tenant_id, target.resource_kind, target.resource_id) not in self._cleanup_targets
            for target in targets
        ):
            raise ValueError("exact recorded cleanup target required")
        self.catalog.cleanup(targets)

    def semantic_version_count(self, tenant_id: str) -> int:
        return self._semantic_version_counts.get(tenant_id, 0)

    def review_submission_count(self, tenant_id: str) -> int:
        return self._review_submission_counts.get(tenant_id, 0)

    def form_contract_with_approval_ids(
        self,
        *,
        tenant_id: str,
        semantic_version: ApprovedSemanticVersion,
        approval_ids: tuple[str, ...],
    ) -> ManagedIntegrationContract:
        return self._form_contract(
            tenant_id=tenant_id,
            semantic_version=semantic_version,
            approval_ids=approval_ids,
        )

    def get_process_package(self, tenant_id: str, artifact_id: str) -> ProcessPackageReceipt:
        receipt = self._process_receipts.get((tenant_id, artifact_id))
        if receipt is None:
            self._process_service.get_original(tenant_id, artifact_id, 1)
            raise LookupError((tenant_id, artifact_id))
        self._process_service.get_original(tenant_id, receipt.package_id, receipt.version)
        return receipt

    def get_candidate_set(self, tenant_id: str, artifact_id: str) -> SemanticCandidateSet:
        return self._semantic_repository.load_revision(tenant_id, artifact_id, 1)

    def get_authority_observation(self, tenant_id: str, artifact_id: str) -> AuthorityObservation:
        return self._semantic_repository.load_observation(tenant_id, artifact_id)

    def get_review_bundle(self, tenant_id: str, artifact_id: str) -> OntologyReviewBundle:
        return self._semantic_repository.load_review_bundle(tenant_id, artifact_id)

    def get_semantic_version(self, tenant_id: str, artifact_id: str) -> ApprovedSemanticVersion:
        return self._semantic_version_repository.load(tenant_id, artifact_id, 1)

    def get_contract(self, tenant_id: str, artifact_id: str) -> ManagedIntegrationContract:
        contract = self._contracts.get((tenant_id, artifact_id))
        if contract is None:
            raise LookupError((tenant_id, artifact_id))
        return contract

    def get_catalog_binding(self, tenant_id: str, artifact_id: str) -> CatalogBinding:
        return self._catalog_control.get(tenant_id, artifact_id)

    def get_publication_receipt(
        self, tenant_id: str, artifact_id: str
    ) -> CatalogPublicationReceipt:
        _, receipt, _ = self._publication_repository.load_publication(
            tenant_id=tenant_id, publication_id=artifact_id
        )
        return receipt

    def get_request(self, tenant_id: str, artifact_id: str) -> InboxRequest:
        return self._request_service.get(tenant_id, artifact_id)

    def _rehydrate_replay(
        self,
        *,
        tenant_id: str,
        process_package: ProcessPackage,
        manifest: BusinessProcessManifest,
        result: SemanticFormationResult,
    ) -> ProcessPackageReceipt:
        candidate_set = result.candidate_set
        if candidate_set is None:
            raise ValueError("persisted replay is missing its candidate set")
        receipt = ProcessPackageReceipt(
            package_id=result.identifiers.process_package_id,
            tenant_id=tenant_id,
            version=candidate_set.package_version,
            media_type="text/markdown; charset=utf-8",
            original_digest=candidate_set.original_digest,
            manifest_digest=candidate_set.manifest_digest,
            manifest_source_digest=sha256(process_package.manifest).hexdigest(),
            uploader_id="data-architect",
            received_at=_NOW,
        )
        original = self._process_service.get_original(
            tenant_id, receipt.package_id, receipt.version
        )
        if original != process_package.narrative:
            raise ValueError("persisted replay package bytes differ from the request")
        if (
            self._process_service.get_manifest(tenant_id, receipt.package_id, receipt.version)
            != process_package.manifest
        ):
            raise ValueError("persisted replay manifest bytes differ from the request")
        if digest(manifest) != receipt.manifest_digest:
            raise ValueError("persisted replay manifest differs from the request")
        self._process_receipts[(tenant_id, receipt.package_id)] = receipt
        self._process_receipts[(tenant_id, process_package.package_id)] = receipt
        for observation in result.authority_observations:
            self._observation_ids[(tenant_id, digest(observation))] = observation.observation_id
        bundle = self._semantic_repository.load_review_bundle(
            tenant_id, result.identifiers.review_bundle_id
        )
        self._review_bundle_ids[(tenant_id, digest(bundle))] = bundle.bundle_id
        self._review_submission_counts[tenant_id] = 1
        if result.semantic_version is not None:
            persisted_semantic = self._semantic_version_repository.load(
                tenant_id,
                result.semantic_version.semantic_version_id,
                result.semantic_version.version,
            )
            if persisted_semantic != result.semantic_version:
                raise ValueError("persisted replay semantic version differs")
            self._semantic_version_counts[tenant_id] = 1
        if result.contract is not None:
            self._contracts[(tenant_id, result.contract.contract_id)] = result.contract
            if not all(
                self._source_observation_repository.has_current(tenant_id, reference, now=_NOW)
                for reference in result.contract.source_observation_refs
            ):
                raise ValueError("persisted replay source observation differs")
            self._source_observation_refs[tenant_id] = result.contract.source_observation_refs
        request = self._request_service.get(tenant_id, result.identifiers.request_id)
        decisions = self._request_service.list_decisions(tenant_id, request.request_id)
        for decision in decisions:
            item = next(
                (
                    review_item
                    for review_item in bundle.items
                    if review_item.semantic_revision_digest == decision.subject_digest
                ),
                None,
            )
            if item is not None:
                self._decision_requests[(tenant_id, decision.decision_id)] = (
                    request.request_id,
                    item.required_authority_ref,
                )
        if result.catalog_binding is not None:
            for resource_kind, resource_id in (
                ("catalog_binding", result.catalog_binding.binding_id),
                ("tenant_namespace", f"namespace-{tenant_id}"),
            ):
                self._cleanup_targets.add((tenant_id, resource_kind, resource_id))
        return receipt

    def _upload_or_verify(
        self,
        *,
        tenant_id: str,
        process_package: ProcessPackage,
    ) -> ProcessPackageReceipt:
        original = process_package.narrative
        package_key = (tenant_id, process_package.package_id)
        receipt = self._process_receipts.get(package_key)
        if receipt is None:
            receipt = self._process_service.upload_manifest_bytes(
                tenant_id,
                original,
                "text/markdown; charset=utf-8",
                process_package.manifest,
                "data-architect",
            )
            self._process_receipts[(tenant_id, receipt.package_id)] = receipt
            self._process_receipts[package_key] = receipt
        elif (
            self._process_service.get_original(tenant_id, receipt.package_id, receipt.version)
            != original
            or self._process_service.get_manifest(tenant_id, receipt.package_id, receipt.version)
            != process_package.manifest
        ):
            raise ValueError("replayed process package bytes differ from persisted input")
        return receipt

    def _resolve_authority(
        self, *, tenant_id: str, candidate_set: SemanticCandidateSet
    ) -> tuple[tuple[AuthorityObservation, ...], tuple[AuthorityResolution, ...]]:
        resolver = AuthorityResolver(
            tenant_id=tenant_id,
            clock=lambda: _NOW,
            candidate_ownership_verifier=self._semantic_repository.owns_candidate,
        )
        observations: list[AuthorityObservation] = []
        resolutions: list[AuthorityResolution] = []
        for candidate in candidate_set.candidates:
            process_observation = self._record_observation(
                tenant_id=tenant_id,
                information_kind=InformationKind.BUSINESS_MEANING,
                source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
                subject_ref=candidate.name,
                assertion=candidate.proposed_definition or candidate.name,
                authority_ref="owner:finance-data-owner",
                observed_digest=digest(
                    {"candidate_id": candidate.candidate_id, "source": "process-package"}
                ),
            )
            candidate_observations = [process_observation]
            if self.catalog.refund_is_legacy_attribute and candidate.name == "Refund":
                candidate_observations.append(
                    self._record_observation(
                        tenant_id=tenant_id,
                        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
                        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
                        subject_ref=candidate.name,
                        assertion="Refund is a legacy attribute",
                        authority_ref="catalog:legacy-refund",
                        observed_digest=digest(
                            {"candidate_id": candidate.candidate_id, "source": "legacy-catalog"}
                        ),
                    )
                )
            observations.extend(candidate_observations)
            resolutions.append(
                resolver.resolve(candidate=candidate, observations=tuple(candidate_observations))
            )
        return tuple(observations), tuple(resolutions)

    def _record_source_observation(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        manifest: BusinessProcessManifest,
    ) -> ArtifactReference:
        source_ref = manifest.source_references[0]
        observation = self._source_observation_repository.store(
            SourceObservation(
                observation_id="source-"
                + digest(
                    {
                        "tenant_id": tenant_id,
                        "source_ref": source_ref,
                        "manifest_digest": receipt.manifest_digest,
                    }
                )[:24],
                tenant_id=tenant_id,
                version=1,
                source_ref=source_ref,
                schema_digest=digest(
                    {
                        "entities": manifest.entities,
                        "events": manifest.events,
                        "states": manifest.states,
                    }
                ),
                observed_at=_NOW,
                valid_until=_NOW + timedelta(days=1),
            )
        )
        return ArtifactReference(
            artifact_id=observation.observation_id,
            version=observation.version,
            digest=digest(observation),
        )

    def _record_observation(
        self,
        *,
        tenant_id: str,
        information_kind: InformationKind,
        source_kind: AuthoritySourceKind,
        subject_ref: str,
        assertion: str,
        authority_ref: str,
        observed_digest: str,
    ) -> AuthorityObservation:
        observation = self._semantic_repository.record_observation(
            tenant_id=tenant_id,
            information_kind=information_kind,
            source_kind=source_kind,
            subject_ref=subject_ref,
            assertion=assertion,
            authority_ref=authority_ref,
            observed_digest=observed_digest,
            observed_at=_NOW,
            valid_until=_NOW + timedelta(days=1),
        )
        self._observation_ids[(tenant_id, digest(observation))] = observation.observation_id
        return observation

    @staticmethod
    def _validate_owner_decisions(
        *,
        owner_decisions: tuple[OwnerDecision, ...] | None,
        bundle: OntologyReviewBundle,
        candidate_set: SemanticCandidateSet,
    ) -> None:
        if owner_decisions is None:
            return
        for decision in owner_decisions:
            if decision.review_bundle_digest != digest(
                bundle
            ) or decision.candidate_set_digest != digest(candidate_set):
                raise ValueError("stale approval does not bind current review artifacts")
            if not decision.approved:
                raise ValueError("owner approval is required")

    def _finish_success(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        candidate_set: SemanticCandidateSet,
        observations: tuple[AuthorityObservation, ...],
        resolutions: tuple[AuthorityResolution, ...],
        bundle: OntologyReviewBundle,
    ) -> SemanticFormationResult:
        self._semantic_repository.grant_authority_role(
            tenant_id=tenant_id, actor_id=_OWNER_ACTOR, authority_ref=_OWNER_ROLE
        )
        for item in bundle.items:
            bundle = self._review_service.decide_item(
                tenant_id=tenant_id,
                bundle_id=bundle.bundle_id,
                item_id=item.item_id,
                decision=ReviewItemDecision.ACCEPT,
                actor_id=_OWNER_ACTOR,
                expected_revision=bundle.revision,
            )
        request = self._review_service.submit_bundle(
            tenant_id=tenant_id,
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision,
            requester_id="data-architect",
        )
        self._review_submission_counts[tenant_id] = 1
        approval_ids: list[str] = []
        for item in bundle.items:
            decision = self._request_service.record_decision(
                tenant_id=tenant_id,
                request_id=request.request_id,
                request_revision=request.revision,
                actor_id=_OWNER_ACTOR,
                kind=DecisionKind.APPROVE,
                subject_digest=item.semantic_revision_digest,
            )
            approval_ids.append(decision.decision_id)
            self._decision_requests[(tenant_id, decision.decision_id)] = (
                request.request_id,
                item.required_authority_ref,
            )
        bundle = self._review_service.finalize_bundle(
            tenant_id=tenant_id,
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision + 1,
            request_id=request.request_id,
            request_revision=request.revision,
            decision=DecisionKind.APPROVE,
        )
        self._review_bundle_ids[(tenant_id, digest(bundle))] = bundle.bundle_id
        compiled = ApprovedSemanticCompiler(self._semantic_version_repository).compile(
            ApprovalCompilationInput(
                tenant_id=tenant_id,
                candidate_set=candidate_set,
                review_bundle=bundle,
                authority_observations=observations,
                approval_ids=tuple(approval_ids),
                approved_at=_NOW,
            ),
            now=_NOW,
        )
        if not isinstance(compiled, ApprovedSemanticVersion):
            raise RuntimeError("approved semantic compilation returned No Valid Plan")
        self._semantic_version_counts[tenant_id] = 1
        contract = self._form_contract(
            tenant_id=tenant_id,
            semantic_version=compiled,
            approval_ids=tuple(approval_ids),
        )
        binding = self._ready_catalog_binding(tenant_id)
        publication_receipt = self._publication_service.publish(
            binding=binding, semantic_version=compiled, contract=contract
        )
        request = self._request_service.get(tenant_id, request.request_id)
        for state in (RequestState.VERIFYING, RequestState.DELIVERED):
            request = self._request_service.transition(
                tenant_id,
                request.request_id,
                state,
                actor_id="data-architect",
                expected_revision=request.revision,
            )
        identifiers = _identifiers(
            receipt=receipt,
            candidate_set=candidate_set,
            observation=observations[0],
            bundle=bundle,
            semantic_version=compiled,
            contract=contract,
            binding=binding,
            publication_receipt=publication_receipt,
            request=request,
        )
        for resource_kind, resource_id in (
            ("catalog_binding", binding.binding_id),
            ("tenant_namespace", f"namespace-{tenant_id}"),
        ):
            self._cleanup_targets.add((tenant_id, resource_kind, resource_id))
        return SemanticFormationResult(
            authority_resolution=resolutions[0],
            request=request,
            execution_occurred=False,
            identifiers=identifiers,
            catalog_binding=binding,
            candidate_set=candidate_set,
            authority_observation=observations[0],
            authority_observations=observations,
            semantic_version=compiled,
            contract=contract,
            publication_receipt=publication_receipt,
        )

    def _finish_no_valid_plan(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        candidate_set: SemanticCandidateSet,
        observations: tuple[AuthorityObservation, ...],
        resolutions: tuple[AuthorityResolution, ...],
        bundle: OntologyReviewBundle,
    ) -> SemanticFormationResult:
        request = self._review_service.submit_bundle(
            tenant_id=tenant_id,
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision,
            requester_id="data-architect",
        )
        self._review_submission_counts[tenant_id] = 1
        bundle = self._review_service.finalize_bundle(
            tenant_id=tenant_id,
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision + 1,
            request_id=request.request_id,
            request_revision=request.revision,
            decision=ReviewItemDecision.UNRESOLVED,
        )
        self._review_bundle_ids[(tenant_id, digest(bundle))] = bundle.bundle_id
        request = self._request_service.get(tenant_id, request.request_id)
        request = self._request_service.transition(
            tenant_id,
            request.request_id,
            RequestState.NO_VALID_PLAN,
            actor_id="data-architect",
            expected_revision=request.revision,
        )
        identifiers = AcceptanceIdentifiers(
            process_package_id=receipt.package_id,
            candidate_set_id=candidate_set.set_id,
            authority_observation_id=observations[0].observation_id,
            review_bundle_id=bundle.bundle_id,
            semantic_version_id="",
            contract_id="",
            catalog_binding_id="",
            publication_receipt_id="",
            request_id=request.request_id,
        )
        unresolved = next(
            resolution
            for resolution in resolutions
            if resolution.status is not AuthorityResolutionStatus.RESOLVED
        )
        return SemanticFormationResult(
            authority_resolution=unresolved,
            request=request,
            execution_occurred=False,
            identifiers=identifiers,
            candidate_set=candidate_set,
            authority_observation=observations[0],
            authority_observations=observations,
        )

    def _form_contract(
        self,
        *,
        tenant_id: str,
        semantic_version: ApprovedSemanticVersion,
        approval_ids: tuple[str, ...],
    ) -> ManagedIntegrationContract:
        loader = _FormationLoader(
            process_receipts=self._process_receipts,
            semantic_repository=self._semantic_repository,
            semantic_version_repository=self._semantic_version_repository,
            request_service=self._request_service,
            observation_ids=self._observation_ids,
            review_bundle_ids=self._review_bundle_ids,
            decision_requests=self._decision_requests,
            source_observation_repository=self._source_observation_repository,
        )
        formation = IntegrationContractFormationService(loader, clock=lambda: _NOW).form(
            ContractFormationInput(
                tenant_id=tenant_id,
                semantic_version_ref=ArtifactReference(
                    artifact_id=semantic_version.semantic_version_id,
                    version=semantic_version.version,
                    digest=digest(semantic_version),
                ),
                source_observation_refs=self._source_observation_refs.get(tenant_id, ()),
                destination_product=DestinationProductRequirement(
                    product_name="finance-revenue",
                    warehouse_binding_id="warehouse-managed",
                    supported_engines=("postgresql", "clickhouse"),
                ),
                freshness=FreshnessRequirement(maximum_age_seconds=3600),
                quality=QualityPolicy(required_constraint_ids=("invoice-key",)),
                trigger_policy=TriggerRequirement(run_now_allowed=True),
                access_policy=AccessPolicy(
                    classification_refs=(), required_approver_refs=(_OWNER_ROLE,)
                ),
                evidence_policy=EvidencePolicy(),
                failure_policy=FailurePolicy(),
                approval_ids=approval_ids,
                identity_rules=(("invoice", "invoice_id"),),
                required_approval_ids=approval_ids,
            )
        )
        if formation.status is not ContractFormationStatus.READY_TO_ACTIVATE:
            raise RuntimeError(f"contract formation failed: {formation.no_valid_plan}")
        if formation.contract is None:
            raise RuntimeError("ready contract formation omitted the contract")
        self._contracts[(tenant_id, formation.contract.contract_id)] = formation.contract
        return formation.contract

    def _ready_catalog_binding(self, tenant_id: str) -> CatalogBinding:
        binding = self._catalog_control.create_draft(tenant_id=tenant_id)
        for state in (CatalogBindingState.PROVISIONING, CatalogBindingState.VALIDATING):
            binding = self._catalog_control.transition(
                tenant_id, binding.binding_id, state, expected_revision=binding.revision
            )
        return self._catalog_control.record_validation(
            tenant_id=tenant_id,
            binding_id=binding.binding_id,
            expected_revision=binding.revision,
            evidence=CatalogValidationEvidence(
                evidence_id=f"validation-{binding.binding_id}",
                tenant_id=tenant_id,
                binding_id=binding.binding_id,
                binding_revision=binding.revision,
                provider_version=self.catalog.provider_version,
                provider_build_digest=digest(
                    {"provider_version": self.catalog.provider_version, "source": "offline-fake"}
                ),
                provider_image_set_digest=digest(
                    {"provider_version": self.catalog.provider_version, "images": "offline-fake"}
                ),
                positive_probe_digest=digest({"probe": "positive", "tenant": tenant_id}),
                denial_probe_digest=digest({"probe": "denial", "tenant": tenant_id}),
                stable_identity_probe_digest=digest(
                    {"probe": "stable-identity", "tenant": tenant_id}
                ),
                backup_probe_digest=digest({"probe": "backup", "tenant": tenant_id}),
                observed_at=_NOW,
            ),
        )

    def _verify_replay(
        self,
        *,
        tenant_id: str,
        result: SemanticFormationResult,
        candidate_set: SemanticCandidateSet,
    ) -> None:
        if result.candidate_set != candidate_set:
            raise ValueError("candidate extraction replay did not converge")
        self.get_process_package(tenant_id, result.identifiers.process_package_id)
        self.get_candidate_set(tenant_id, result.identifiers.candidate_set_id)
        observations = tuple(
            self.get_authority_observation(tenant_id, observation.observation_id)
            for observation in result.authority_observations
        )
        if not observations:
            raise ValueError("persisted replay is missing authority observations")
        resolution = AuthorityResolver(
            tenant_id=tenant_id,
            clock=lambda: _NOW,
            candidate_ownership_verifier=self._semantic_repository.owns_candidate,
        ).resolve(candidate=candidate_set.candidates[0], observations=observations)
        if resolution != result.authority_resolution:
            raise ValueError("authority replay did not converge")
        bundle = self.get_review_bundle(tenant_id, result.identifiers.review_bundle_id)
        replay_request = self._review_service.submit_bundle(
            tenant_id=tenant_id,
            bundle_id=bundle.bundle_id,
            expected_revision=bundle.revision,
            requester_id="data-architect",
        )
        if replay_request != self.get_request(tenant_id, result.identifiers.request_id):
            raise ValueError("review submission replay did not converge")
        if result.publication_receipt is None:
            return
        if (
            result.catalog_binding is None
            or result.semantic_version is None
            or result.contract is None
        ):
            raise RuntimeError("successful replay is missing governed publication inputs")
        semantic_version = ApprovedSemanticCompiler(self._semantic_version_repository).compile(
            ApprovalCompilationInput(
                tenant_id=tenant_id,
                candidate_set=candidate_set,
                review_bundle=bundle,
                authority_observations=observations,
                approval_ids=result.semantic_version.approval_ids,
                approved_at=_NOW,
            ),
            now=_NOW,
        )
        if not isinstance(semantic_version, ApprovedSemanticVersion):
            raise ValueError("semantic compilation replay returned No Valid Plan")
        if semantic_version != result.semantic_version:
            raise ValueError("semantic compilation replay did not converge")
        contract = self._form_contract(
            tenant_id=tenant_id,
            semantic_version=result.semantic_version,
            approval_ids=result.semantic_version.approval_ids,
        )
        if contract != result.contract:
            raise ValueError("contract formation replay did not converge")
        replay_receipt = self._publication_service.publish(
            binding=self.get_catalog_binding(tenant_id, result.catalog_binding.binding_id),
            semantic_version=self.get_semantic_version(
                tenant_id, semantic_version.semantic_version_id
            ),
            contract=contract,
        )
        if replay_receipt != result.publication_receipt:
            raise ValueError("publication replay did not converge")


def _sibling_database(database_path: Path, suffix: str) -> Path:
    return database_path.with_name(f"{database_path.stem}-{suffix}.sqlite")


def _manifest_bytes(*, is_refund: bool) -> bytes:
    manifest = BusinessProcessManifest(
        process_name="Refund" if is_refund else "Revenue to Cash",
        owner="finance-data-owner",
        participants=("finance",),
        outcomes=("Refund recorded",) if is_refund else ("Revenue settled",),
        entities=("Refund",) if is_refund else ("Invoice",),
        events=(),
        states=(),
        rules=(),
        source_references=("finance-system",),
        unresolved_questions=(),
    )
    return canonical_bytes(manifest) + b"\n"


def _identifiers(
    *,
    receipt: ProcessPackageReceipt,
    candidate_set: SemanticCandidateSet,
    observation: AuthorityObservation,
    bundle: OntologyReviewBundle,
    semantic_version: ApprovedSemanticVersion,
    contract: ManagedIntegrationContract,
    binding: CatalogBinding,
    publication_receipt: CatalogPublicationReceipt,
    request: InboxRequest,
) -> AcceptanceIdentifiers:
    return AcceptanceIdentifiers(
        process_package_id=receipt.package_id,
        candidate_set_id=candidate_set.set_id,
        authority_observation_id=observation.observation_id,
        review_bundle_id=bundle.bundle_id,
        semantic_version_id=semantic_version.semantic_version_id,
        contract_id=contract.contract_id,
        catalog_binding_id=binding.binding_id,
        publication_receipt_id=publication_receipt.publication_id,
        request_id=request.request_id,
    )


__all__: Final = [
    "AcceptanceIdentifiers",
    "ExactCleanupTarget",
    "OfflineSemanticFormationHarness",
    "OwnerDecision",
    "SemanticFormationResult",
    "legacy_refund_attribute_catalog",
    "refund_entity_package",
    "revenue_to_cash_package",
    "revised_revenue_to_cash_package",
    "strict_validating_catalog",
]
