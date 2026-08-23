from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Protocol

from pillarmesh_contract_model import ArtifactReference, SemanticObject, digest

from .client import OpenMetadataClient
from .models import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    ClassificationPayload,
    GlossaryTermPayload,
    LineagePayload,
    ProviderHealth,
)


class _PublicationIntent(Protocol):
    operation_id: str
    semantic_version_digest: str
    contract_digest: str
    semantic_identities: tuple[str, ...]
    semantic_objects: tuple[SemanticObject, ...]
    contract_reference: ArtifactReference


class _OpenMetadataPublicationClient(Protocol):
    def health(self) -> ProviderHealth: ...

    def ensure_tenant_namespace(
        self, *, tenant_key: str, idempotency_key: str
    ) -> CatalogObjectRef: ...

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef: ...

    def ensure_glossary_term(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: GlossaryTermPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def ensure_classification(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: ClassificationPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def ensure_lineage(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: LineagePayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...

    def get_object(self, *, tenant_key: str, identity: str) -> CatalogObjectSnapshot: ...


class OpenMetadataPublicationProvider:
    """Adapts provider operations while keeping remote identifiers inside the client."""

    def __init__(self, client: _OpenMetadataPublicationClient) -> None:
        self._client = client

    @property
    def provider_version(self) -> str:
        return self._client.health().provider_version

    def publish(
        self, *, tenant_id: str, intent: _PublicationIntent
    ) -> tuple[CatalogObjectRef, ...]:
        namespace_reference = _publication_step(
            "namespace",
            lambda: self._client.ensure_tenant_namespace(
                tenant_key=tenant_id, idempotency_key=intent.operation_id
            ),
        )
        _publication_step(
            "service identity",
            lambda: self._client.ensure_service_identity(
                tenant_key=tenant_id,
                identity="runtime",
                idempotency_key=intent.operation_id,
            ),
        )
        term_references: list[CatalogObjectRef] = []
        for identity, semantic_object in zip(
            intent.semantic_identities, intent.semantic_objects, strict=True
        ):
            term_references.append(
                _publication_step(
                    "glossary term",
                    partial(
                        self._client.ensure_glossary_term,
                        tenant_key=tenant_id,
                        identity=identity,
                        payload=GlossaryTermPayload(
                            name=semantic_object.name,
                            definition=semantic_object.definition,
                            owner_ref="runtime",
                            provenance_ref=intent.contract_reference.digest,
                        ),
                        idempotency_key=intent.operation_id,
                    ),
                )
            )

        classification_references: list[CatalogObjectRef] = []
        for identity in intent.semantic_identities:
            classification_references.append(
                _publication_step(
                    "classification",
                    partial(
                        self._client.ensure_classification,
                        tenant_key=tenant_id,
                        identity=digest({"operation_id": intent.operation_id, "subject": identity}),
                        payload=ClassificationPayload(
                            subject_ref=identity,
                            classification_ref=intent.contract_digest,
                            provenance_ref=intent.semantic_version_digest,
                        ),
                        idempotency_key=intent.operation_id,
                    ),
                )
            )
        lineage_references: tuple[CatalogObjectRef, ...] = ()
        if len(intent.semantic_identities) > 1:
            lineage_references = (
                _publication_step(
                    "lineage",
                    lambda: self._client.ensure_lineage(
                        tenant_key=tenant_id,
                        identity=digest({"operation_id": intent.operation_id, "kind": "lineage"}),
                        payload=LineagePayload(
                            from_ref=intent.semantic_identities[0],
                            to_ref=intent.semantic_identities[1],
                            producer_ref=intent.contract_digest,
                            evidence_ref=intent.semantic_version_digest,
                        ),
                        idempotency_key=intent.operation_id,
                    ),
                ),
            )
        return (
            namespace_reference,
            *term_references,
            *classification_references,
            *lineage_references,
        )

    def observe(self, *, tenant_id: str, reference: CatalogObjectRef) -> CatalogObjectSnapshot:
        observation = self._client.get_object(
            tenant_key=tenant_id, identity=reference.stable_identity
        )
        return observation


def openmetadata_publication_provider(
    client: OpenMetadataClient,
) -> OpenMetadataPublicationProvider:
    return OpenMetadataPublicationProvider(client)


def _publication_step[Result](label: str, operation: Callable[[], Result]) -> Result:
    try:
        return operation()
    except CatalogProviderError as error:
        raise CatalogProviderError(
            f"OpenMetadata publication {label} failed",
            classification=error.classification,
        ) from None
