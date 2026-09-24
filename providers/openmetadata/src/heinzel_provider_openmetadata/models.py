from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel, digest
from pydantic import Field, field_serializer, model_validator

type CatalogFailureClassification = Literal[
    "transient",
    "throttled",
    "authentication",
    "authorization",
    "conflict",
    "invalid_request",
    "permanent",
]


class CatalogProviderError(RuntimeError):
    def __init__(self, message: str, *, classification: CatalogFailureClassification) -> None:
        self.classification = classification
        super().__init__(message)


class CatalogObjectRef(ArtifactModel):
    tenant_key: str = Field(min_length=1)
    stable_identity: str = Field(min_length=1)
    normalized_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


type NormalizedPayloadValue = str | tuple[str, ...]


class CatalogObjectSnapshot(ArtifactModel):
    tenant_key: str = Field(min_length=1)
    stable_identity: str = Field(min_length=1)
    logical_identity: str = Field(min_length=1)
    object_kind: Literal["namespace", "glossary_term", "classification", "lineage"]
    normalized_payload: Mapping[str, NormalizedPayloadValue]
    normalized_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_serializer("normalized_payload")
    def serialized_normalized_payload(
        self, normalized_payload: Mapping[str, NormalizedPayloadValue]
    ) -> dict[str, NormalizedPayloadValue]:
        return dict(normalized_payload)

    @model_validator(mode="after")
    def freeze_and_verify_normalized_payload(self) -> Self:
        normalized_payload = MappingProxyType(dict(self.normalized_payload))
        if digest(normalized_payload) != self.normalized_digest:
            raise ValueError("normalized payload digest does not match its canonical content")
        object.__setattr__(self, "normalized_payload", normalized_payload)
        return self


class ProviderHealth(ArtifactModel):
    status: Literal["healthy"] = "healthy"
    provider_version: Literal["1.13.3"] = "1.13.3"


class ProviderBuildIdentity(ArtifactModel):
    provider_version: Literal["1.13.3"]
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    build_timestamp: int = Field(gt=0)


class GlossaryTermPayload(ArtifactModel):
    name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    owner_ref: str = Field(min_length=1)
    provenance_ref: str = Field(min_length=1)


class ClassificationPayload(ArtifactModel):
    subject_ref: str = Field(min_length=1)
    classification_ref: str = Field(min_length=1)
    provenance_ref: str = Field(min_length=1)


class LineagePayload(ArtifactModel):
    from_ref: str = Field(min_length=1)
    to_ref: str = Field(min_length=1)
    producer_ref: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
