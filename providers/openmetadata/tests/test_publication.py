from __future__ import annotations

from typing import Literal

import pytest
from pillarmesh_contract_model import ArtifactReference, SemanticObject, digest
from pillarmesh_provider_openmetadata import (
    CatalogObjectRef,
    CatalogObjectSnapshot,
    CatalogProviderError,
    OpenMetadataPublicationProvider,
    ProviderHealth,
)
from pillarmesh_provider_openmetadata.models import (
    CatalogFailureClassification,
    GlossaryTermPayload,
)


class _Intent:
    operation_id = "a" * 64
    semantic_version_digest = "b" * 64
    contract_digest = "c" * 64
    semantic_identities = ("identity-a", "identity-b")
    semantic_objects = (
        SemanticObject(
            object_id="customer",
            name="Customer",
            definition="A governed customer.",
            source_refs=("package-a",),
        ),
        SemanticObject(
            object_id="order",
            name="Order",
            definition="A governed order.",
            source_refs=("package-a",),
        ),
    )
    contract_reference = ArtifactReference(artifact_id="contract-a", version=1, digest="c" * 64)


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.term_payloads: list[GlossaryTermPayload] = []
        self._observations: dict[str, CatalogObjectSnapshot] = {}
        self._lose_next_term_response = False

    @property
    def provider_identity_count(self) -> int:
        return sum(reference.endswith("identity-a") for reference in self._observations)

    def lose_next_term_response_after_commit(self) -> None:
        self._lose_next_term_response = True

    def health(self) -> ProviderHealth:
        return ProviderHealth()

    def ensure_tenant_namespace(self, *, tenant_key: str, idempotency_key: str) -> CatalogObjectRef:
        self.calls.append("namespace")
        return self._record(
            tenant_key,
            "namespace",
            "namespace",
            {"name": tenant_key, "namespace": tenant_key},
        )

    def ensure_service_identity(
        self, *, tenant_key: str, identity: str, idempotency_key: str
    ) -> CatalogObjectRef:
        self.calls.append("owner")
        return self._reference(tenant_key, "service_identity", identity)

    def ensure_glossary_term(
        self, *, tenant_key: str, identity: str, **kwargs: object
    ) -> CatalogObjectRef:
        self.calls.append(f"term:{identity}")
        payload = kwargs["payload"]
        assert isinstance(payload, GlossaryTermPayload)
        self.term_payloads.append(payload)
        reference = self._record(
            tenant_key,
            "glossary_term",
            identity,
            {
                "name": payload.name,
                "definition": payload.definition,
                "owner_ref": payload.owner_ref,
                "provenance_ref": payload.provenance_ref,
                "contract_ref": payload.provenance_ref,
            },
        )
        if self._lose_next_term_response:
            self._lose_next_term_response = False
            raise CatalogProviderError("response was lost", classification="transient")
        return reference

    def ensure_classification(self, **kwargs: object) -> CatalogObjectRef:
        self.calls.append("classification")
        payload = kwargs["payload"]
        assert isinstance(payload, object)
        return self._record(
            "tenant-a",
            "classification",
            str(kwargs["identity"]),
            {
                "subject_ref": payload.subject_ref,
                "classification_ref": payload.classification_ref,
                "provenance_ref": payload.provenance_ref,
            },
        )

    def ensure_lineage(self, **kwargs: object) -> CatalogObjectRef:
        self.calls.append("lineage")
        payload = kwargs["payload"]
        assert isinstance(payload, object)
        return self._record(
            "tenant-a",
            "lineage",
            str(kwargs["identity"]),
            {
                "from_ref": payload.from_ref,
                "to_ref": payload.to_ref,
                "producer_ref": payload.producer_ref,
                "evidence_ref": payload.evidence_ref,
            },
        )

    def get_object(self, *, tenant_key: str, identity: str) -> CatalogObjectSnapshot:
        self.calls.append(f"read:{identity}")
        return self._observations[identity]

    @staticmethod
    def _reference(tenant_key: str, object_kind: str, identity: str) -> CatalogObjectRef:
        private_identity = f"{object_kind}:{tenant_key}:{identity}"
        return CatalogObjectRef(
            tenant_key=tenant_key,
            stable_identity=private_identity,
            normalized_digest=digest({"identity": private_identity}),
        )

    def _record(
        self,
        tenant_key: str,
        object_kind: Literal["namespace", "glossary_term", "classification", "lineage"],
        identity: str,
        payload: dict[str, str],
    ) -> CatalogObjectRef:
        reference = self._reference(tenant_key, object_kind, identity)
        self._observations[reference.stable_identity] = CatalogObjectSnapshot(
            tenant_key=tenant_key,
            stable_identity=reference.stable_identity,
            logical_identity=identity,
            object_kind=object_kind,
            normalized_payload=payload,
            normalized_digest=digest(payload),
        )
        return reference


def test_adapter_ensures_catalog_semantics_and_reads_back_by_stable_identity() -> None:
    client = _Client()
    provider = OpenMetadataPublicationProvider(client)

    references = provider.publish(tenant_id="tenant-a", intent=_Intent())
    observation = provider.observe(tenant_id="tenant-a", reference=references[1])

    assert provider.provider_version == "1.13.3"
    assert tuple(reference.stable_identity for reference in references) == (
        "namespace:tenant-a:namespace",
        "glossary_term:tenant-a:identity-a",
        "glossary_term:tenant-a:identity-b",
        "classification:tenant-a:"
        + digest({"operation_id": _Intent.operation_id, "subject": "identity-a"}),
        "classification:tenant-a:"
        + digest({"operation_id": _Intent.operation_id, "subject": "identity-b"}),
        "lineage:tenant-a:" + digest({"operation_id": _Intent.operation_id, "kind": "lineage"}),
    )
    assert observation.stable_identity == "glossary_term:tenant-a:identity-a"
    assert observation.logical_identity == "identity-a"
    assert client.calls == [
        "namespace",
        "owner",
        "term:identity-a",
        "term:identity-b",
        "classification",
        "classification",
        "lineage",
        "read:glossary_term:tenant-a:identity-a",
    ]


def test_adapter_publishes_actual_approved_meaning_and_exact_contract_provenance() -> None:
    client = _Client()
    provider = OpenMetadataPublicationProvider(client)

    provider.publish(tenant_id="tenant-a", intent=_Intent())

    payload = client.term_payloads[0]
    assert payload.name == "Customer"
    assert payload.definition == "A governed customer."
    assert payload.provenance_ref == _Intent.contract_reference.digest


def test_adapter_reads_back_every_governed_catalog_effect_with_normalized_payloads() -> None:
    client = _Client()
    provider = OpenMetadataPublicationProvider(client)

    references = provider.publish(tenant_id="tenant-a", intent=_Intent())
    observations = tuple(
        provider.observe(tenant_id="tenant-a", reference=reference) for reference in references
    )

    assert {observation.object_kind for observation in observations} == {
        "namespace",
        "glossary_term",
        "classification",
        "lineage",
    }
    normalized_payloads = {
        observation.stable_identity: observation.normalized_payload for observation in observations
    }
    assert normalized_payloads["namespace:tenant-a:namespace"] == {
        "name": "tenant-a",
        "namespace": "tenant-a",
    }
    assert normalized_payloads["glossary_term:tenant-a:identity-a"] == {
        "name": "Customer",
        "definition": "A governed customer.",
        "owner_ref": "runtime",
        "provenance_ref": _Intent.contract_reference.digest,
        "contract_ref": _Intent.contract_reference.digest,
    }
    classification_payload = next(
        payload
        for identity, payload in normalized_payloads.items()
        if identity.startswith("classification:")
    )
    assert classification_payload == {
        "subject_ref": "identity-a",
        "classification_ref": _Intent.contract_digest,
        "provenance_ref": _Intent.semantic_version_digest,
    }
    lineage_payload = next(
        payload
        for identity, payload in normalized_payloads.items()
        if identity.startswith("lineage:")
    )
    assert lineage_payload == {
        "from_ref": "identity-a",
        "to_ref": "identity-b",
        "producer_ref": _Intent.contract_digest,
        "evidence_ref": _Intent.semantic_version_digest,
    }


@pytest.mark.parametrize("classification", ("transient", "conflict"))
def test_adapter_preserves_timeout_and_conflict_failure_classification(
    classification: CatalogFailureClassification,
) -> None:
    class FailingClient(_Client):
        def ensure_tenant_namespace(
            self, *, tenant_key: str, idempotency_key: str
        ) -> CatalogObjectRef:
            raise CatalogProviderError("provider call failed", classification=classification)

    with pytest.raises(CatalogProviderError) as captured:
        OpenMetadataPublicationProvider(FailingClient()).publish(
            tenant_id="tenant-a", intent=_Intent()
        )

    assert captured.value.classification == classification
    assert str(captured.value) == "OpenMetadata publication namespace failed"


def test_adapter_lost_response_after_remote_commit_converges_on_one_identity_and_receipt() -> None:
    client = _Client()
    client.lose_next_term_response_after_commit()
    provider = OpenMetadataPublicationProvider(client)

    with pytest.raises(CatalogProviderError, match="OpenMetadata publication glossary term failed"):
        provider.publish(tenant_id="tenant-a", intent=_Intent())

    references = provider.publish(tenant_id="tenant-a", intent=_Intent())

    assert (
        tuple(reference.stable_identity for reference in references).count(
            "glossary_term:tenant-a:identity-a"
        )
        == 1
    )
    assert client.provider_identity_count == 1
