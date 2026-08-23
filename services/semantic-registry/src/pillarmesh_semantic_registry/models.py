from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, Self

from pillarmesh_contract_model import ArtifactModel, InformationKind
from pillarmesh_contract_service import BusinessProcessManifest, ProcessPackageReceipt
from pydantic import Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class CandidateKind(StrEnum):
    ENTITY = "entity"
    EVENT = "event"
    STATE = "state"
    RELATIONSHIP = "relationship"
    IDENTITY_RULE = "identity_rule"
    INTEGRITY_CONSTRAINT = "integrity_constraint"
    METRIC = "metric"
    CLASSIFICATION = "classification"


class AuthoritySourceKind(StrEnum):
    OWNER_DECISION = "owner_decision"
    APPROVED_SEMANTIC_VERSION = "approved_semantic_version"
    PROCESS_PACKAGE = "process_package"
    DECLARED_CATALOG_AUTHORITY = "declared_catalog_authority"


class AuthorityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"


class ResolutionReasonCode(StrEnum):
    RESOLVED_BY_PRECEDENCE = "resolved_by_precedence"
    CROSS_KIND_CONFLICT = "cross_kind_conflict"
    SAME_RANK_DISAGREEMENT = "same_rank_disagreement"
    NO_ADMITTED_AUTHORITY = "no_admitted_authority"
    EXPIRED_OBSERVATION = "expired_observation"
    INADMISSIBLE_SOURCE = "inadmissible_source"


class AuthorityObservation(ArtifactModel):
    schema_version: Literal["1"] = "1"
    observation_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    information_kind: InformationKind
    source_kind: AuthoritySourceKind
    subject_ref: str = Field(min_length=1)
    assertion: str = Field(min_length=1)
    authority_ref: str = Field(min_length=1)
    observed_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime
    valid_until: datetime

    @field_validator("observed_at", "valid_until")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def requires_ordered_validity_window(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError(
                "valid_until must be later than observed_at; observation is not current"
            )
        return self


class AuthorityResolution(ArtifactModel):
    resolution_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    status: AuthorityResolutionStatus
    selected_observation_digest: str | None
    considered_observation_digests: tuple[str, ...]
    required_authority_ref: str | None
    reason_code: ResolutionReasonCode

    @field_validator("selected_observation_digest")
    @classmethod
    def validates_selected_digest(cls, value: str | None) -> str | None:
        if value is not None and len(value) != 64:
            raise ValueError("selected observation digest must contain 64 hexadecimal characters")
        if value is not None and any(character not in "0123456789abcdef" for character in value):
            raise ValueError("selected observation digest must contain 64 hexadecimal characters")
        return value

    @field_validator("considered_observation_digests")
    @classmethod
    def validates_considered_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
                raise ValueError(
                    "considered observation digests must contain 64 hexadecimal characters"
                )
        if len(value) != len(set(value)):
            raise ValueError("considered observation digests must be unique")
        return value

    @model_validator(mode="after")
    def selected_digest_is_considered(self) -> Self:
        if (
            self.selected_observation_digest is not None
            and self.selected_observation_digest not in self.considered_observation_digests
        ):
            raise ValueError("selected observation digest must be considered")
        return self


class CandidateProvenance(ArtifactModel):
    source_kind: Literal["manifest", "narrative_marker"]
    source_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_path: str = Field(min_length=1)
    narrative_line_start: int | None = Field(default=None, ge=1)
    narrative_line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def requires_consistent_line_range(self) -> Self:
        start = self.narrative_line_start
        end = self.narrative_line_end
        if self.source_kind == "manifest":
            if start is not None or end is not None:
                raise ValueError("manifest provenance must not include a narrative line range")
            return self
        if start is None or end is None or end < start:
            raise ValueError("narrative provenance requires an ordered line range")
        return self


class SemanticCandidate(ArtifactModel):
    candidate_id: str = Field(min_length=1)
    kind: CandidateKind
    name: str = Field(min_length=1)
    proposed_definition: str | None
    related_refs: tuple[str, ...]
    provenance: CandidateProvenance
    confidence: Decimal = Field(ge=0, le=1)


class SemanticCandidateSet(ArtifactModel):
    schema_version: Literal["1"] = "1"
    set_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    package_id: str = Field(min_length=1)
    package_version: int = Field(ge=1)
    original_digest: str = Field(pattern=_DIGEST_PATTERN)
    manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    extractor_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    candidates: tuple[SemanticCandidate, ...]
    unresolved_questions: tuple[str, ...]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class SemanticCandidateExtractor(Protocol):
    def extract(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        manifest: BusinessProcessManifest,
        original: bytes,
    ) -> SemanticCandidateSet: ...
