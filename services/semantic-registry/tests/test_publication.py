from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pytest
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    ContractConstraint,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FieldMapping,
    FreshnessRequirement,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    digest,
)
from heinzel_provider_openmetadata import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
)
from heinzel_request_management import (
    RequestManagementService,
    RequestState,
    SQLiteRequestRepository,
)

if TYPE_CHECKING:
    from heinzel_semantic_registry.publication import (
        CatalogPublicationIntent,
        CatalogPublicationProvider,
    )

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _binding(
    *, tenant_id: str = "tenant-a", state: CatalogBindingState = CatalogBindingState.READY
) -> CatalogBinding:
    return CatalogBinding(
        binding_id="catalog-a",
        tenant_id=tenant_id,
        capability_profile_digest="a" * 64,
        lifecycle_state=state,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
        provisioned_at=NOW,
    )


def _semantic_version(*, tenant_id: str = "tenant-a") -> ApprovedSemanticVersion:
    return ApprovedSemanticVersion(
        semantic_version_id="semantic-a",
        tenant_id=tenant_id,
        version=1,
        process_package_ref=ArtifactReference(artifact_id="package-a", version=1, digest="b" * 64),
        candidate_set_digest="c" * 64,
        review_bundle_digest="d" * 64,
        entities=(
            SemanticObject(
                object_id="customer",
                name="Customer",
                definition="A customer.",
                source_refs=("package-a",),
            ),
        ),
        events=(),
        states=(),
        relationships=(),
        identity_rules=(),
        constraints=(),
        metrics=(),
        classifications=(),
        authority_bindings=(),
        approval_ids=("approval-owner",),
        created_at=NOW,
    )


def _contract(semantic_version: ApprovedSemanticVersion) -> ManagedIntegrationContract:
    return ManagedIntegrationContract(
        contract_id="contract-a",
        tenant_id=semantic_version.tenant_id,
        version=1,
        formation_status="ready_to_activate",
        semantic_version_ref=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        source_observation_refs=(),
        mappings=(
            FieldMapping(
                source_ref="customer_id", semantic_ref="customer", transformation="identity"
            ),
        ),
        integrity_constraints=(
            ContractConstraint(
                constraint_id="customer-key", kind="identity", expression="customer_id"
            ),
        ),
        destination_product=DestinationProductRequirement(
            product_name="warehouse",
            warehouse_binding_id="warehouse-a",
            supported_engines=("postgresql",),
        ),
        freshness=FreshnessRequirement(maximum_age_seconds=3600),
        quality=QualityPolicy(required_constraint_ids=("customer-key",)),
        trigger_policy=TriggerRequirement(run_now_allowed=True),
        access_policy=AccessPolicy(classification_refs=(), required_approver_refs=("role:owner",)),
        evidence_policy=EvidencePolicy(),
        failure_policy=FailurePolicy(),
        approval_ids=("approval-owner",),
    )


def test_publication_module_exposes_the_governed_publication_service() -> None:
    from heinzel_semantic_registry.publication import SemanticPublicationService

    assert SemanticPublicationService.__name__ == "SemanticPublicationService"


def test_publish_rejects_a_binding_that_is_not_ready() -> None:
    semantic_version = _semantic_version()
    service = _service(_PublicationProvider())

    with pytest.raises(ValueError, match="ready"):
        service.publish(
            binding=_binding(state=CatalogBindingState.VALIDATING),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publication_intent_binds_exact_tenant_versions_and_stable_identities() -> None:
    from heinzel_semantic_registry.publication import (
        CatalogPublicationIntent,
        publication_intent,
    )

    semantic_version = _semantic_version()
    intent = publication_intent(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )

    assert isinstance(intent, CatalogPublicationIntent)
    assert intent.tenant_id == "tenant-a"
    assert intent.catalog_binding_revision == 3
    assert intent.semantic_version_digest == digest(semantic_version)
    assert intent.contract_digest == digest(_contract(semantic_version))
    assert len(intent.semantic_identities) == 1
    assert intent.operation_id == digest(intent.model_dump(exclude={"operation_id"}))


def test_publication_intent_carries_approved_objects_and_exact_contract_reference() -> None:
    from heinzel_semantic_registry.publication import publication_intent

    semantic_version = _semantic_version()
    contract = _contract(semantic_version)

    intent = publication_intent(
        binding=_binding(), semantic_version=semantic_version, contract=contract
    )

    assert intent.semantic_objects == semantic_version.entities
    assert intent.contract_reference == contract.semantic_version_ref.model_copy(
        update={
            "artifact_id": contract.contract_id,
            "version": contract.version,
            "digest": digest(contract),
        }
    )


def test_publish_rejects_replay_when_persisted_semantic_bytes_differ_from_caller_input() -> None:
    from heinzel_semantic_registry.publication import SQLiteCatalogPublicationRepository

    provider = _PublicationProvider()
    repository = SQLiteCatalogPublicationRepository(":memory:")
    service = _service(provider, repository=repository)
    semantic_version = _semantic_version()
    arguments = {
        "binding": _binding(),
        "semantic_version": semantic_version,
        "contract": _contract(semantic_version),
    }

    service.publish(**arguments)
    repository._connection.execute(
        "UPDATE catalog_publication_intents SET semantic_payload = ?",
        (b'{"not":"an approved semantic version"}',),
    )
    repository._connection.commit()

    with pytest.raises(ValueError, match="persisted approved semantic version"):
        service.publish(**arguments)


def test_publication_recovery_loads_the_exact_persisted_intent_with_tenant_isolation() -> None:
    from heinzel_semantic_registry.publication import (
        SQLiteCatalogPublicationRepository,
        publication_intent,
    )

    repository = SQLiteCatalogPublicationRepository(":memory:")
    semantic_version = _semantic_version()
    contract = _contract(semantic_version)
    intent = publication_intent(
        binding=_binding(),
        semantic_version=semantic_version,
        contract=contract,
    )
    repository.store_intent(
        intent=intent,
        semantic_version=semantic_version,
        contract=contract,
    )

    assert (
        repository.load_intent(
            tenant_id=intent.tenant_id,
            operation_id=intent.operation_id,
        )
        == intent
    )
    with pytest.raises(KeyError):
        repository.load_intent(
            tenant_id="tenant-b",
            operation_id=intent.operation_id,
        )


def test_publication_effect_count_is_tenant_scoped_and_replay_stable() -> None:
    from heinzel_semantic_registry.publication import SQLiteCatalogPublicationRepository

    repository = SQLiteCatalogPublicationRepository(":memory:")
    service = _service(_PublicationProvider(), repository=repository)
    semantic_version = _semantic_version()
    arguments = {
        "binding": _binding(),
        "semantic_version": semantic_version,
        "contract": _contract(semantic_version),
    }

    assert repository.effect_count(tenant_id="tenant-a") == 0

    receipt = service.publish(**arguments)

    assert repository.effect_count(tenant_id=receipt.tenant_id) == 2
    assert repository.effect_count(tenant_id="tenant-b") == 0

    service.publish(**arguments)

    assert repository.effect_count(tenant_id=receipt.tenant_id) == 2


def test_receipt_never_exposes_an_opaque_provider_identifier() -> None:
    from heinzel_semantic_registry.publication import CatalogPublicationReceipt

    receipt = CatalogPublicationReceipt(
        publication_id="publication-a",
        tenant_id="tenant-a",
        intent_digest="a" * 64,
        provider_version="1.13.3",
        published_refs=(ArtifactReference(artifact_id="semantic-a", version=1, digest="b" * 64),),
        round_trip_observation_digest="c" * 64,
        published_at=NOW,
    )

    assert receipt.round_trip_verified is True
    assert set(receipt.model_dump()) == {
        "schema_version",
        "publication_id",
        "tenant_id",
        "intent_digest",
        "provider_version",
        "published_refs",
        "round_trip_observation_digest",
        "round_trip_verified",
        "published_at",
    }


class _PublicationProvider:
    provider_version = "1.13.3"

    def __init__(self) -> None:
        self._observations: dict[str, CatalogObjectSnapshot] = {}
        self._lost_response = False
        self.diverge_after_first_read = False
        self._read_count = 0

    def lose_first_response_after_commit(self) -> None:
        self._lost_response = True

    def publish(
        self, *, tenant_id: str, intent: CatalogPublicationIntent
    ) -> tuple[CatalogObjectRef, ...]:
        references = [
            self._record(
                tenant_id,
                "namespace",
                "namespace",
                {"name": tenant_id, "namespace": tenant_id},
            )
        ]
        for identity, semantic_object in zip(
            intent.semantic_identities, intent.semantic_objects, strict=True
        ):
            references.append(
                self._record(
                    tenant_id,
                    "glossary_term",
                    identity,
                    {
                        "name": semantic_object.name,
                        "definition": semantic_object.definition,
                        "owner_ref": "runtime",
                        "provenance_ref": intent.contract_reference.digest,
                    },
                )
            )
            references.append(
                self._record(
                    tenant_id,
                    "classification",
                    digest({"operation_id": intent.operation_id, "subject": identity}),
                    {
                        "subject_ref": identity,
                        "classification_ref": intent.contract_digest,
                        "provenance_ref": intent.semantic_version_digest,
                    },
                )
            )
        if len(intent.semantic_identities) > 1:
            references.append(
                self._record(
                    tenant_id,
                    "lineage",
                    digest({"operation_id": intent.operation_id, "kind": "lineage"}),
                    {
                        "from_ref": intent.semantic_identities[0],
                        "to_ref": intent.semantic_identities[1],
                        "producer_ref": intent.contract_digest,
                        "evidence_ref": intent.semantic_version_digest,
                    },
                )
            )
        if self._lost_response:
            self._lost_response = False
            raise CatalogProviderError("response was lost", classification="transient")
        return tuple(references)

    def observe(self, *, tenant_id: str, reference: CatalogObjectRef) -> CatalogObjectSnapshot:
        observation = self._observations[reference.stable_identity]
        self._read_count += 1
        if self.diverge_after_first_read and self._read_count > len(self._observations):
            payload = dict(observation.normalized_payload) | {"classification": "provider-value"}
            return observation.model_copy(
                update={"normalized_payload": payload, "normalized_digest": digest(payload)}
            )
        return observation

    def edit_classification(self) -> None:
        identity, observation = next(
            item for item in self._observations.items() if item[1].object_kind == "glossary_term"
        )
        payload = dict(observation.normalized_payload) | {"classification": "legacy"}
        self._observations[identity] = observation.model_copy(
            update={"normalized_payload": payload, "normalized_digest": digest(payload)}
        )

    def edit_description(self) -> None:
        identity, observation = next(
            item for item in self._observations.items() if item[1].object_kind == "glossary_term"
        )
        payload = dict(observation.normalized_payload) | {"description": "cosmetic update"}
        self._observations[identity] = observation.model_copy(
            update={"normalized_payload": payload, "normalized_digest": digest(payload)}
        )

    def _record(
        self,
        tenant_id: str,
        object_kind: Literal["namespace", "glossary_term", "classification", "lineage"],
        identity: str,
        payload: dict[str, str],
    ) -> CatalogObjectRef:
        stable_identity = f"{object_kind}:{tenant_id}:{identity}"
        reference = CatalogObjectRef(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            normalized_digest=digest({"identity": stable_identity}),
        )
        self._observations[stable_identity] = CatalogObjectSnapshot(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            logical_identity=identity,
            object_kind=object_kind,
            normalized_payload=payload,
            normalized_digest=digest(payload),
        )
        return reference


class _FullPublicationProvider:
    provider_version = "1.13.3"

    def __init__(
        self,
        *,
        consistently_wrong_readback: bool = False,
        missing_glossary_provenance: bool = False,
        swapped_glossary_identities: bool = False,
        swapped_observation_references: bool = False,
        omitted_object_kind: Literal["namespace", "glossary_term", "classification", "lineage"]
        | None = None,
    ) -> None:
        self._observations: dict[str, CatalogObjectSnapshot] = {}
        self._consistently_wrong_readback = consistently_wrong_readback
        self._missing_glossary_provenance = missing_glossary_provenance
        self._swapped_glossary_identities = swapped_glossary_identities
        self._swapped_observation_references = swapped_observation_references
        self._omitted_object_kind = omitted_object_kind

    def publish(
        self, *, tenant_id: str, intent: CatalogPublicationIntent
    ) -> tuple[CatalogObjectRef, ...]:
        references: list[CatalogObjectRef] = []
        self._append_record(
            references, tenant_id, "namespace", "namespace", self._namespace_payload(tenant_id)
        )
        glossary_identities = intent.semantic_identities
        if self._swapped_glossary_identities:
            glossary_identities = tuple(reversed(glossary_identities))
        for logical_identity, semantic_object in zip(
            glossary_identities, intent.semantic_objects, strict=True
        ):
            self._append_record(
                references,
                tenant_id,
                "glossary_term",
                logical_identity,
                self._term_payload(intent, semantic_object),
            )
        for identity in intent.semantic_identities:
            classification_identity = digest(
                {"operation_id": intent.operation_id, "subject": identity}
            )
            self._append_record(
                references,
                tenant_id,
                "classification",
                classification_identity,
                self._classification_payload(intent, identity),
            )
        if len(intent.semantic_identities) > 1:
            self._append_record(
                references,
                tenant_id,
                "lineage",
                digest({"operation_id": intent.operation_id, "kind": "lineage"}),
                {
                    "from_ref": intent.semantic_identities[0],
                    "to_ref": intent.semantic_identities[1],
                    "producer_ref": intent.contract_digest,
                    "evidence_ref": intent.semantic_version_digest,
                },
            )
        return tuple(references)

    def observe(self, *, tenant_id: str, reference: CatalogObjectRef) -> CatalogObjectSnapshot:
        if self._swapped_observation_references:
            glossary_identities = tuple(
                identity
                for identity, observation in self._observations.items()
                if observation.object_kind == "glossary_term"
            )
            if reference.stable_identity in glossary_identities:
                other_identity = next(
                    identity
                    for identity in glossary_identities
                    if identity != reference.stable_identity
                )
                return self._observations[other_identity]
        return self._observations[reference.stable_identity]

    def edit_ownership(self) -> None:
        identity, observation = next(
            item for item in self._observations.items() if item[1].object_kind == "glossary_term"
        )
        payload = dict(observation.normalized_payload) | {"owner_ref": "legacy-owner"}
        self._observations[identity] = observation.model_copy(
            update={"normalized_payload": payload, "normalized_digest": digest(payload)}
        )

    def _append_record(
        self,
        references: list[CatalogObjectRef],
        tenant_id: str,
        object_kind: Literal["namespace", "glossary_term", "classification", "lineage"],
        identity: str,
        payload: dict[str, str],
    ) -> None:
        if object_kind != self._omitted_object_kind:
            references.append(self._record(tenant_id, object_kind, identity, payload))

    def _record(
        self,
        tenant_id: str,
        object_kind: Literal["namespace", "glossary_term", "classification", "lineage"],
        identity: str,
        payload: dict[str, str],
    ) -> CatalogObjectRef:
        if self._consistently_wrong_readback:
            payload = {field: "wrong" for field in payload}
        stable_identity = f"{object_kind}:{tenant_id}:{identity}"
        reference = CatalogObjectRef(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            normalized_digest=digest({"identity": stable_identity}),
        )
        self._observations[stable_identity] = CatalogObjectSnapshot(
            tenant_key=tenant_id,
            stable_identity=stable_identity,
            logical_identity=identity,
            object_kind=object_kind,
            normalized_payload=payload,
            normalized_digest=digest(payload),
        )
        return reference

    def _namespace_payload(self, tenant_id: str) -> dict[str, str]:
        return {"name": tenant_id, "namespace": tenant_id}

    def _term_payload(
        self, intent: CatalogPublicationIntent, semantic_object: SemanticObject
    ) -> dict[str, str]:
        payload = {
            "name": semantic_object.name,
            "definition": semantic_object.definition,
            "owner_ref": "runtime",
            "provenance_ref": intent.contract_reference.digest,
        }
        if self._missing_glossary_provenance:
            del payload["provenance_ref"]
        return payload

    def _classification_payload(
        self, intent: CatalogPublicationIntent, identity: str
    ) -> dict[str, str]:
        return {
            "subject_ref": identity,
            "classification_ref": intent.contract_digest,
            "provenance_ref": intent.semantic_version_digest,
        }


def _service(
    provider: CatalogPublicationProvider,
    *,
    repository: object | None = None,
    semantic_repository: object | None = None,
    semantic_version_repository: object | None = None,
):
    from heinzel_semantic_registry import SQLiteSemanticRepository
    from heinzel_semantic_registry.publication import (
        SemanticPublicationService,
        SQLiteCatalogPublicationRepository,
    )

    request_service = RequestManagementService(
        SQLiteRequestRepository.open(":memory:"), clock=lambda: NOW
    )
    return SemanticPublicationService(
        repository=repository or SQLiteCatalogPublicationRepository(":memory:"),
        provider=provider,
        semantic_repository=semantic_repository or SQLiteSemanticRepository(":memory:"),
        semantic_version_repository=semantic_version_repository
        or _SemanticVersionLoader(_semantic_version()),
        request_service=request_service,
        clock=lambda: NOW,
    )


class _SemanticVersionLoader:
    def __init__(self, semantic_version: ApprovedSemanticVersion) -> None:
        self._semantic_version = semantic_version
        self.loaded: list[tuple[str, str, int]] = []

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion:
        self.loaded.append((tenant_id, semantic_version_id, version))
        return self._semantic_version


def test_lost_publication_response_replay_creates_one_receipt_and_no_duplicate_identity() -> None:
    provider = _PublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version()
    arguments = {
        "binding": _binding(),
        "semantic_version": semantic_version,
        "contract": _contract(semantic_version),
    }

    provider.lose_first_response_after_commit()
    with pytest.raises(CatalogProviderError, match="lost"):
        service.publish(**arguments)

    receipt = service.publish(**arguments)

    assert receipt.published_refs[0].artifact_id == semantic_version.semantic_version_id
    assert {observation.object_kind for observation in provider._observations.values()} == {
        "namespace",
        "glossary_term",
        "classification",
    }
    assert service.publish(**arguments) == receipt


def test_repository_lists_only_the_tenants_publications_newest_first() -> None:
    from heinzel_semantic_registry.publication import SQLiteCatalogPublicationRepository

    repository = SQLiteCatalogPublicationRepository(":memory:")
    provider = _PublicationProvider()
    semantic_version = _semantic_version()
    service = _service(provider, repository=repository)
    receipt = service.publish(
        binding=_binding(),
        semantic_version=semantic_version,
        contract=_contract(semantic_version),
    )

    assert repository.list_publications(tenant_id="tenant-a") == (receipt,)
    assert repository.list_publications(tenant_id="tenant-b") == ()


def test_publish_rejects_a_provider_readback_that_changes_between_independent_full_reads() -> None:
    provider = _PublicationProvider()
    provider.diverge_after_first_read = True
    service = _service(provider)
    semantic_version = _semantic_version()

    with pytest.raises(ValueError, match="independent readback"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publish_rejects_two_consistently_wrong_readbacks_against_intended_payloads() -> None:
    provider = _FullPublicationProvider(consistently_wrong_readback=True)
    semantic_version = _semantic_version().model_copy(
        update={
            "entities": (
                *_semantic_version().entities,
                SemanticObject(
                    object_id="order",
                    name="Order",
                    definition="An order.",
                    source_refs=("package-a",),
                ),
            )
        }
    )
    service = _service(
        provider, semantic_version_repository=_SemanticVersionLoader(semantic_version)
    )

    with pytest.raises(ValueError, match="intended"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publish_rejects_correct_payloads_bound_to_swapped_logical_identities() -> None:
    provider = _FullPublicationProvider(swapped_glossary_identities=True)
    semantic_version = _semantic_version().model_copy(
        update={
            "entities": (
                *_semantic_version().entities,
                SemanticObject(
                    object_id="order",
                    name="Order",
                    definition="An order.",
                    source_refs=("package-a",),
                ),
            )
        }
    )
    service = _service(
        provider, semantic_version_repository=_SemanticVersionLoader(semantic_version)
    )

    with pytest.raises(ValueError, match="intended"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publish_rejects_snapshots_swapped_between_provider_references() -> None:
    provider = _FullPublicationProvider(swapped_observation_references=True)
    semantic_version = _semantic_version().model_copy(
        update={
            "entities": (
                *_semantic_version().entities,
                SemanticObject(
                    object_id="order",
                    name="Order",
                    definition="An order.",
                    source_refs=("package-a",),
                ),
            )
        }
    )
    service = _service(
        provider, semantic_version_repository=_SemanticVersionLoader(semantic_version)
    )

    with pytest.raises(ValueError, match="reference"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publish_rejects_a_glossary_term_with_missing_provenance_reference() -> None:
    provider = _FullPublicationProvider(missing_glossary_provenance=True)
    service = _service(provider)
    semantic_version = _semantic_version()

    with pytest.raises(ValueError, match="intended"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_publish_rejects_a_readback_that_omits_an_expected_classification() -> None:
    provider = _FullPublicationProvider(omitted_object_kind="classification")
    service = _service(provider)
    semantic_version = _semantic_version()

    with pytest.raises(ValueError, match="intended"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_drift_classification_uses_only_the_changed_object_kind_and_field() -> None:
    provider = _FullPublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version()
    receipt = service.publish(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )

    provider.edit_ownership()
    proposal = service.observe_drift(tenant_id="tenant-a", publication_id=receipt.publication_id)

    assert proposal is not None
    assert proposal.information_kind == "ownership"


def test_publish_requires_an_injected_repository_to_prove_the_approved_version_is_persisted() -> (
    None
):
    semantic_version = _semantic_version()
    provider = _PublicationProvider()
    repository = _SemanticVersionLoader(semantic_version)
    service = _service(provider, semantic_version_repository=repository)

    receipt = service.publish(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )

    assert receipt.round_trip_verified is True
    assert repository.loaded == [("tenant-a", "semantic-a", 1)]


def test_publish_rejects_an_approved_version_that_the_injected_repository_did_not_persist() -> None:
    semantic_version = _semantic_version()
    persisted_without_approval = semantic_version.model_copy(update={"approval_ids": ()})
    service = _service(
        _PublicationProvider(),
        semantic_version_repository=_SemanticVersionLoader(persisted_without_approval),
    )

    with pytest.raises(ValueError, match="persisted approved semantic version"):
        service.publish(
            binding=_binding(),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )


def test_catalog_semantic_drift_creates_investigating_change_request_without_mutating_receipt() -> (
    None
):
    provider = _PublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version()
    receipt = service.publish(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )
    original_receipt = receipt.model_copy(deep=True)

    provider.edit_classification()
    proposal = service.observe_drift(tenant_id="tenant-a", publication_id=receipt.publication_id)

    assert proposal is not None
    assert proposal.auto_applied is False
    assert proposal.request.payload.request_type == "schema_semantic_change"
    assert proposal.request.state is RequestState.INVESTIGATING
    assert proposal.information_kind == "meaning"
    assert proposal.request.payload.before_observation_digest != proposal.before_observation_digest
    assert proposal.request.payload.after_observation_digest != proposal.after_observation_digest
    assert proposal.request.payload.affected_semantic_ref == digest(semantic_version)
    assert proposal.request.payload.affected_contract_ref == "contract-a"
    assert proposal.request.payload.required_authority_refs == ("contract-a",)
    assert receipt == original_receipt


def test_description_only_catalog_drift_is_explicitly_allowed_without_a_change_request() -> None:
    provider = _PublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version()
    receipt = service.publish(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )

    provider.edit_description()

    assert (
        service.observe_drift(tenant_id="tenant-a", publication_id=receipt.publication_id) is None
    )


def test_drift_persists_tenant_scoped_immutable_before_and_after_authority_observations() -> None:
    from heinzel_semantic_registry import SQLiteSemanticRepository

    provider = _PublicationProvider()
    semantic_repository = SQLiteSemanticRepository(":memory:")
    service = _service(provider, semantic_repository=semantic_repository)
    semantic_version = _semantic_version()
    receipt = service.publish(
        binding=_binding(), semantic_version=semantic_version, contract=_contract(semantic_version)
    )

    provider.edit_classification()
    proposal = service.observe_drift(tenant_id="tenant-a", publication_id=receipt.publication_id)

    assert proposal is not None
    request = proposal.request.payload
    assert request.before_observation_digest is not None
    assert request.after_observation_digest is not None
    stored = semantic_repository._connection.execute(
        "SELECT payload FROM authority_observations WHERE tenant_id = ?", ("tenant-a",)
    ).fetchall()
    observations = tuple(
        semantic_repository.load_observation("tenant-a", json.loads(row[0])["observation_id"])
        for row in stored
    )
    before = next(
        item for item in observations if digest(item) == request.before_observation_digest
    )
    after = next(item for item in observations if digest(item) == request.after_observation_digest)
    assert before.observed_digest == proposal.before_observation_digest
    assert after.observed_digest == proposal.after_observation_digest
    assert before.tenant_id == after.tenant_id == "tenant-a"
    assert before.observation_id != after.observation_id


def test_publication_rejects_cross_tenant_artifacts_before_provider_effect() -> None:
    provider = _PublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version(tenant_id="tenant-b")

    with pytest.raises(ValueError, match="one tenant"):
        service.publish(
            binding=_binding(tenant_id="tenant-a"),
            semantic_version=semantic_version,
            contract=_contract(semantic_version),
        )

    assert provider._observations == {}


def test_ambiguous_conflict_replays_by_reading_back_the_previously_persisted_intent() -> None:
    provider = _PublicationProvider()
    service = _service(provider)
    semantic_version = _semantic_version()
    arguments = {
        "binding": _binding(),
        "semantic_version": semantic_version,
        "contract": _contract(semantic_version),
    }

    first = service.publish(**arguments)
    second = service.publish(**arguments)

    assert second == first
