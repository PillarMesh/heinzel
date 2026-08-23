from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_catalog_control import CatalogBinding, CatalogBindingState
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ContractFormationStatus,
    ManagedIntegrationContract,
    SemanticObject,
    canonical_bytes,
    digest,
)
from pillarmesh_contract_model import (
    InformationKind as AuthorityInformationKind,
)
from pillarmesh_provider_openmetadata import CatalogObjectRef, CatalogObjectSnapshot
from pillarmesh_request_management import InboxRequest, RequestManagementService
from pydantic import Field, TypeAdapter, field_validator

from .models import AuthoritySourceKind
from .repository import SemanticRepository, SemanticVersionRepository

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
type InformationKind = Literal[
    "meaning", "identity", "classification", "ownership", "contract", "lineage"
]


class CatalogPublicationIntent(ArtifactModel):
    schema_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    catalog_binding_id: str = Field(min_length=1)
    catalog_binding_revision: int = Field(ge=1)
    semantic_version_digest: str = Field(pattern=_DIGEST_PATTERN)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    semantic_identities: tuple[str, ...] = Field(min_length=1)
    semantic_objects: tuple[SemanticObject, ...] = Field(min_length=1)
    contract_reference: ArtifactReference


class CatalogPublicationReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    publication_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    intent_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_version: str = Field(min_length=1)
    published_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    round_trip_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    round_trip_verified: Literal[True] = True
    published_at: datetime

    @field_validator("published_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("published_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class CatalogDriftProposal(ArtifactModel):
    proposal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    publication_id: str = Field(min_length=1)
    information_kind: InformationKind
    before_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    after_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    affected_semantic_version_id: str = Field(min_length=1)
    affected_contract_id: str = Field(min_length=1)
    request: InboxRequest
    auto_applied: Literal[False] = False


class CatalogPublicationProvider(Protocol):
    @property
    def provider_version(self) -> str: ...

    def publish(
        self, *, tenant_id: str, intent: CatalogPublicationIntent
    ) -> tuple[CatalogObjectRef, ...]: ...

    def observe(self, *, tenant_id: str, reference: CatalogObjectRef) -> CatalogObjectSnapshot: ...


class CatalogPublicationRepository(Protocol):
    def store_intent(
        self,
        *,
        intent: CatalogPublicationIntent,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> CatalogPublicationIntent: ...

    def load_inputs(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[ApprovedSemanticVersion, ManagedIntegrationContract]: ...

    def load_intent(self, *, tenant_id: str, operation_id: str) -> CatalogPublicationIntent: ...

    def load_receipt(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogPublicationReceipt | None: ...

    def store_receipt(
        self,
        *,
        intent: CatalogPublicationIntent,
        receipt: CatalogPublicationReceipt,
        references: tuple[CatalogObjectRef, ...],
        observations: tuple[CatalogObjectSnapshot, ...],
    ) -> CatalogPublicationReceipt: ...

    def load_publication(
        self, *, tenant_id: str, publication_id: str
    ) -> tuple[
        CatalogPublicationIntent, CatalogPublicationReceipt, tuple[CatalogObjectRef, ...]
    ]: ...

    def load_observations(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[CatalogObjectSnapshot, ...]: ...

    def effect_count(self, *, tenant_id: str) -> int: ...


class SQLiteCatalogPublicationRepository:
    """Persist intent before effects and keep provider identifiers private."""

    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS catalog_publication_intents ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload BLOB NOT NULL, "
            "semantic_payload BLOB NOT NULL, contract_payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS catalog_publication_receipts ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, publication_id TEXT NOT NULL, "
            "payload BLOB NOT NULL, private_references BLOB NOT NULL, "
            "private_observations BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), UNIQUE (tenant_id, publication_id), "
            "FOREIGN KEY (tenant_id, operation_id) REFERENCES catalog_publication_intents "
            "(tenant_id, operation_id))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store_intent(
        self,
        *,
        intent: CatalogPublicationIntent,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> CatalogPublicationIntent:
        with _transaction(self._connection):
            row = self._connection.execute(
                "SELECT payload, semantic_payload, contract_payload "
                "FROM catalog_publication_intents "
                "WHERE tenant_id = ? AND operation_id = ?",
                (intent.tenant_id, intent.operation_id),
            ).fetchone()
            if row is not None:
                stored = CatalogPublicationIntent.model_validate_json(row[0])
                if stored != intent:
                    raise ValueError(
                        "publication operation identity collides with different intent"
                    )
                self._assert_exact_inputs(
                    semantic_payload=row[1],
                    contract_payload=row[2],
                    semantic_version=semantic_version,
                    contract=contract,
                )
                return stored
            self._connection.execute(
                "INSERT INTO catalog_publication_intents "
                "(tenant_id, operation_id, payload, semantic_payload, contract_payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    intent.tenant_id,
                    intent.operation_id,
                    canonical_bytes(intent),
                    canonical_bytes(semantic_version),
                    canonical_bytes(contract),
                ),
            )
        return intent

    def load_inputs(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[ApprovedSemanticVersion, ManagedIntegrationContract]:
        row = self._connection.execute(
            "SELECT semantic_payload, contract_payload FROM catalog_publication_intents "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, operation_id))
        return (
            ApprovedSemanticVersion.model_validate_json(row[0]),
            ManagedIntegrationContract.model_validate_json(row[1]),
        )

    def load_intent(self, *, tenant_id: str, operation_id: str) -> CatalogPublicationIntent:
        row = self._connection.execute(
            "SELECT payload FROM catalog_publication_intents "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, operation_id))
        return CatalogPublicationIntent.model_validate_json(row[0])

    def effect_count(self, *, tenant_id: str) -> int:
        intent_count = self._connection.execute(
            "SELECT COUNT(*) FROM catalog_publication_intents WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        receipt_count = self._connection.execute(
            "SELECT COUNT(*) FROM catalog_publication_receipts WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if intent_count is None or receipt_count is None:
            raise RuntimeError("publication effect count is unavailable")
        return int(intent_count[0]) + int(receipt_count[0])

    @staticmethod
    def _assert_exact_inputs(
        *,
        semantic_payload: bytes,
        contract_payload: bytes,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> None:
        if semantic_payload != canonical_bytes(semantic_version):
            raise ValueError("persisted approved semantic version differs from caller input")
        if contract_payload != canonical_bytes(contract):
            raise ValueError("persisted managed integration contract differs from caller input")

    def load_receipt(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogPublicationReceipt | None:
        row = self._connection.execute(
            "SELECT payload FROM catalog_publication_receipts "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        return None if row is None else CatalogPublicationReceipt.model_validate_json(row[0])

    def store_receipt(
        self,
        *,
        intent: CatalogPublicationIntent,
        receipt: CatalogPublicationReceipt,
        references: tuple[CatalogObjectRef, ...],
        observations: tuple[CatalogObjectSnapshot, ...],
    ) -> CatalogPublicationReceipt:
        with _transaction(self._connection):
            existing = self.load_receipt(
                tenant_id=intent.tenant_id, operation_id=intent.operation_id
            )
            if existing is not None:
                if existing != receipt:
                    raise ValueError("publication receipt conflicts with an existing operation")
                return existing
            self._connection.execute(
                "INSERT INTO catalog_publication_receipts "
                "(tenant_id, operation_id, publication_id, payload, private_references, "
                "private_observations) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    intent.tenant_id,
                    intent.operation_id,
                    receipt.publication_id,
                    canonical_bytes(receipt),
                    canonical_bytes(references),
                    canonical_bytes(observations),
                ),
            )
        return receipt

    def load_publication(
        self, *, tenant_id: str, publication_id: str
    ) -> tuple[CatalogPublicationIntent, CatalogPublicationReceipt, tuple[CatalogObjectRef, ...]]:
        row = self._connection.execute(
            "SELECT intent.payload, receipt.payload, receipt.private_references "
            "FROM catalog_publication_receipts AS receipt "
            "JOIN catalog_publication_intents AS intent ON "
            "intent.tenant_id = receipt.tenant_id AND intent.operation_id = receipt.operation_id "
            "WHERE receipt.tenant_id = ? AND receipt.publication_id = ?",
            (tenant_id, publication_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, publication_id))
        references = TypeAdapter(tuple[CatalogObjectRef, ...]).validate_json(row[2])
        return (
            CatalogPublicationIntent.model_validate_json(row[0]),
            CatalogPublicationReceipt.model_validate_json(row[1]),
            references,
        )

    def load_observations(
        self, *, tenant_id: str, operation_id: str
    ) -> tuple[CatalogObjectSnapshot, ...]:
        row = self._connection.execute(
            "SELECT private_observations FROM catalog_publication_receipts "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, operation_id))
        return TypeAdapter(tuple[CatalogObjectSnapshot, ...]).validate_json(row[0])


class SemanticPublicationService:
    def __init__(
        self,
        *,
        repository: CatalogPublicationRepository,
        provider: CatalogPublicationProvider,
        semantic_repository: SemanticRepository,
        semantic_version_repository: SemanticVersionRepository | None,
        request_service: RequestManagementService,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._semantic_repository = semantic_repository
        self._semantic_version_repository = semantic_version_repository
        self._request_service = request_service
        self._clock = clock

    def publish(
        self,
        *,
        binding: CatalogBinding,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> CatalogPublicationReceipt:
        _assert_publishable(binding=binding, semantic_version=semantic_version, contract=contract)
        semantic_version_repository = self._semantic_version_repository
        if semantic_version_repository is None:
            raise ValueError("semantic version repository is required before publication")
        persisted_approved_version = semantic_version_repository.load(
            semantic_version.tenant_id,
            semantic_version.semantic_version_id,
            semantic_version.version,
        )
        if persisted_approved_version != semantic_version or (
            persisted_approved_version.approval_ids != semantic_version.approval_ids
        ):
            raise ValueError("persisted approved semantic version differs from caller input")
        intent = publication_intent(
            binding=binding, semantic_version=semantic_version, contract=contract
        )
        self._repository.store_intent(
            intent=intent, semantic_version=semantic_version, contract=contract
        )
        persisted_semantic_version, persisted_contract = self._repository.load_inputs(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
        if persisted_semantic_version != semantic_version:
            raise ValueError("persisted approved semantic version differs from caller input")
        if persisted_contract != contract:
            raise ValueError("persisted managed integration contract differs from caller input")
        receipt = self._repository.load_receipt(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
        if receipt is not None:
            return receipt
        references = self._provider.publish(tenant_id=intent.tenant_id, intent=intent)
        observations = tuple(
            self._provider.observe(tenant_id=intent.tenant_id, reference=reference)
            for reference in references
        )
        independent_observations = tuple(
            self._provider.observe(tenant_id=intent.tenant_id, reference=reference)
            for reference in references
        )
        _assert_exact_expected_observation_set(
            intent=intent,
            tenant_id=intent.tenant_id,
            references=references,
            observations=observations,
            independent_observations=independent_observations,
        )
        receipt = CatalogPublicationReceipt(
            publication_id="publication-" + intent.operation_id[:24],
            tenant_id=intent.tenant_id,
            intent_digest=digest(intent),
            provider_version=self._provider.provider_version,
            published_refs=tuple(
                ArtifactReference(
                    artifact_id=semantic_version.semantic_version_id,
                    version=semantic_version.version,
                    digest=digest(semantic_version),
                )
                for _ in intent.semantic_identities
            ),
            round_trip_observation_digest=digest(observations),
            published_at=_utc_now(self._clock()),
        )
        return self._repository.store_receipt(
            intent=intent, receipt=receipt, references=references, observations=observations
        )

    def observe_drift(self, *, tenant_id: str, publication_id: str) -> CatalogDriftProposal | None:
        intent, receipt, references = self._repository.load_publication(
            tenant_id=tenant_id, publication_id=publication_id
        )
        after_observations = tuple(
            self._provider.observe(tenant_id=tenant_id, reference=reference)
            for reference in references
        )
        after_digest = digest(after_observations)
        if after_digest == receipt.round_trip_observation_digest:
            return None
        before_observations = self._repository.load_observations(
            tenant_id=tenant_id, operation_id=intent.operation_id
        )
        changed_fields, changed_object_kinds = _changed_observation_details(
            before=before_observations, after=after_observations
        )
        if changed_fields and changed_fields <= {"description"}:
            return None
        observed_at = _utc_now(self._clock())
        information_kind = _drift_information_kind(
            object_kinds=changed_object_kinds, changed_fields=changed_fields
        )
        before_observation = self._semantic_repository.record_observation(
            tenant_id=tenant_id,
            information_kind=_authority_information_kind(information_kind),
            source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
            subject_ref=publication_id,
            assertion="catalog state before observed drift",
            authority_ref=intent.contract_reference.artifact_id,
            observed_digest=receipt.round_trip_observation_digest,
            observed_at=observed_at,
            valid_until=observed_at + timedelta(days=1),
        )
        after_observation = self._semantic_repository.record_observation(
            tenant_id=tenant_id,
            information_kind=_authority_information_kind(information_kind),
            source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
            subject_ref=publication_id,
            assertion="catalog state after observed drift",
            authority_ref=intent.contract_reference.artifact_id,
            observed_digest=after_digest,
            observed_at=observed_at,
            valid_until=observed_at + timedelta(days=1),
        )
        request = self._request_service.submit_schema_semantic_change(
            tenant_id=tenant_id,
            requester_id="semantic-publication-service",
            purpose="Catalog semantic drift requires owner review.",
            review_bundle_id=intent.semantic_version_digest,
            review_bundle_digest=intent.semantic_version_digest,
            required_authority_refs=(intent.contract_reference.artifact_id,),
            before_observation_digest=digest(before_observation),
            after_observation_digest=digest(after_observation),
            affected_semantic_ref=intent.semantic_version_digest,
            affected_contract_ref=intent.contract_reference.artifact_id,
        )
        request = self._request_service.record_review_decision(
            tenant_id=tenant_id,
            request_id=request.request_id,
            request_revision=request.revision,
            actor_id="semantic-publication-service",
            decision="unresolved",
        )
        return CatalogDriftProposal(
            proposal_id="catalog-drift-"
            + digest({"publication_id": publication_id, "after": after_digest})[:24],
            tenant_id=tenant_id,
            publication_id=publication_id,
            information_kind=information_kind,
            before_observation_digest=receipt.round_trip_observation_digest,
            after_observation_digest=after_digest,
            affected_semantic_version_id=receipt.published_refs[0].artifact_id,
            affected_contract_id=intent.contract_digest,
            request=request,
        )


def publication_intent(
    *,
    binding: CatalogBinding,
    semantic_version: ApprovedSemanticVersion,
    contract: ManagedIntegrationContract,
) -> CatalogPublicationIntent:
    if binding.tenant_id != semantic_version.tenant_id or contract.tenant_id != binding.tenant_id:
        raise ValueError("publication artifacts must belong to one tenant")
    semantic_object_pairs = tuple(
        (
            f"pillarmesh:{semantic_version.semantic_version_id}:{object_kind}:{semantic_object.object_id}",
            semantic_object,
        )
        for object_kind, semantic_objects in (
            ("entity", semantic_version.entities),
            ("event", semantic_version.events),
            ("state", semantic_version.states),
            ("relationship", semantic_version.relationships),
            ("metric", semantic_version.metrics),
            ("classification", semantic_version.classifications),
        )
        for semantic_object in semantic_objects
    )
    semantic_identities = tuple(identity for identity, _ in semantic_object_pairs)
    semantic_objects = tuple(semantic_object for _, semantic_object in semantic_object_pairs)
    contract_reference = ArtifactReference(
        artifact_id=contract.contract_id,
        version=contract.version,
        digest=digest(contract),
    )
    operation_payload = {
        "schema_version": "1",
        "tenant_id": binding.tenant_id,
        "catalog_binding_id": binding.binding_id,
        "catalog_binding_revision": binding.revision,
        "semantic_version_digest": digest(semantic_version),
        "contract_digest": digest(contract),
        "semantic_identities": semantic_identities,
        "semantic_objects": semantic_objects,
        "contract_reference": contract_reference,
    }
    return CatalogPublicationIntent(
        operation_id=digest(operation_payload),
        tenant_id=binding.tenant_id,
        catalog_binding_id=binding.binding_id,
        catalog_binding_revision=binding.revision,
        semantic_version_digest=digest(semantic_version),
        contract_digest=digest(contract),
        semantic_identities=semantic_identities,
        semantic_objects=semantic_objects,
        contract_reference=contract_reference,
    )


def _assert_publishable(
    *,
    binding: CatalogBinding,
    semantic_version: ApprovedSemanticVersion,
    contract: ManagedIntegrationContract,
) -> None:
    if binding.tenant_id != semantic_version.tenant_id or contract.tenant_id != binding.tenant_id:
        raise ValueError("publication artifacts must belong to one tenant")
    if binding.lifecycle_state is not CatalogBindingState.READY:
        raise ValueError("catalog binding must be ready before publication")
    if not semantic_version.approval_ids:
        raise ValueError("semantic version must be approved before publication")
    if contract.formation_status is not ContractFormationStatus.READY_TO_ACTIVATE:
        raise ValueError("managed integration contract must be ready before publication")
    if contract.semantic_version_ref.digest != digest(semantic_version):
        raise ValueError(
            "managed integration contract must reference the approved semantic version"
        )


def _assert_exact_expected_observation_set(
    *,
    intent: CatalogPublicationIntent,
    tenant_id: str,
    references: tuple[CatalogObjectRef, ...],
    observations: tuple[CatalogObjectSnapshot, ...],
    independent_observations: tuple[CatalogObjectSnapshot, ...],
) -> None:
    if len(references) != len(observations):
        raise ValueError("provider references and observations differ in length")
    for reference, observation in zip(references, observations, strict=True):
        if not (
            observation.tenant_key == reference.tenant_key == intent.tenant_id
            and observation.stable_identity == reference.stable_identity
        ):
            raise ValueError("provider observation does not match its reference")
    if observations != independent_observations:
        raise ValueError("provider independent readback differs from the published observation set")
    actual = tuple(
        (
            observation.object_kind,
            observation.logical_identity,
            dict(observation.normalized_payload),
        )
        for observation in observations
    )
    expected = _expected_observation_payloads(intent=intent, tenant_id=tenant_id)
    if sorted(actual, key=digest) != sorted(expected, key=digest):
        raise ValueError("provider readback differs from the intended publication payloads")


def _expected_observation_payloads(
    *, intent: CatalogPublicationIntent, tenant_id: str
) -> tuple[tuple[str, str, dict[str, str]], ...]:
    payloads: list[tuple[str, str, dict[str, str]]] = [
        ("namespace", "namespace", {"name": tenant_id, "namespace": tenant_id})
    ]
    for identity, semantic_object in zip(
        intent.semantic_identities, intent.semantic_objects, strict=True
    ):
        payloads.append(
            (
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
        payloads.append(
            (
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
        payloads.append(
            (
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
    return tuple(payloads)


def _utc_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("clock must return timezone-aware UTC")
    return value.astimezone(UTC)


def _drift_information_kind(
    *, object_kinds: frozenset[str], changed_fields: frozenset[str]
) -> InformationKind:
    if "lineage" in object_kinds:
        return "lineage"
    if "classification" in object_kinds or "classification_ref" in changed_fields:
        return "classification"
    if {"owner_ref", "owner_refs"} & changed_fields:
        return "ownership"
    if {"contract_ref", "provenance_ref"} & changed_fields:
        return "contract"
    if "glossary_term" in object_kinds:
        return "meaning"
    return "identity"


def _changed_observation_details(
    *, before: tuple[CatalogObjectSnapshot, ...], after: tuple[CatalogObjectSnapshot, ...]
) -> tuple[frozenset[str], frozenset[str]]:
    before_by_identity = {item.stable_identity: item for item in before}
    after_by_identity = {item.stable_identity: item for item in after}
    changed_fields: set[str] = set()
    changed_object_kinds: set[str] = set()
    for identity in before_by_identity.keys() | after_by_identity.keys():
        before_item = before_by_identity.get(identity)
        after_item = after_by_identity.get(identity)
        if before_item is None or after_item is None:
            changed_fields.add("identity")
            if before_item is not None:
                changed_object_kinds.add(before_item.object_kind)
            if after_item is not None:
                changed_object_kinds.add(after_item.object_kind)
            continue
        if before_item.object_kind != after_item.object_kind:
            changed_fields.add("identity")
            changed_object_kinds.update((before_item.object_kind, after_item.object_kind))
        fields = before_item.normalized_payload.keys() | after_item.normalized_payload.keys()
        changed_fields.update(
            field
            for field in fields
            if before_item.normalized_payload.get(field) != after_item.normalized_payload.get(field)
        )
        if before_item.normalized_payload != after_item.normalized_payload:
            changed_object_kinds.add(after_item.object_kind)
    return frozenset(changed_fields), frozenset(changed_object_kinds)


def _changed_observation_fields(
    *, before: tuple[CatalogObjectSnapshot, ...], after: tuple[CatalogObjectSnapshot, ...]
) -> frozenset[str]:
    changed_fields, _ = _changed_observation_details(before=before, after=after)
    return changed_fields


def _authority_information_kind(information_kind: InformationKind) -> AuthorityInformationKind:
    return {
        "meaning": AuthorityInformationKind.BUSINESS_MEANING,
        "identity": AuthorityInformationKind.IDENTITY,
        "classification": AuthorityInformationKind.IMPORTED_CLASSIFICATION,
        "ownership": AuthorityInformationKind.PROCESS_SEMANTICS,
        "contract": AuthorityInformationKind.INTEGRITY_CONSTRAINT,
        "lineage": AuthorityInformationKind.RELATIONSHIP,
    }[information_kind]


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
