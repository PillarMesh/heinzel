from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from .canonical import digest
from .models import ArtifactModel, ArtifactReference

type ImpactSubjectKind = Literal[
    "source_drift",
    "metric_version_change",
    "contract_supersession",
    "generation_failure",
    "policy_change",
    "grant_change",
    "retirement",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_AUTHORITY_DIGEST_DOMAIN = "pillarmesh.impact-authority-snapshot.v1"


class _ImpactAdmissionModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)


class ImpactSubject(_ImpactAdmissionModel):
    subject_kind: ImpactSubjectKind
    subject_ref: str = Field(min_length=1)
    change_subject_digest: str = Field(pattern=_DIGEST_PATTERN)


class ImpactApprovalRequirement(_ImpactAdmissionModel):
    authority_ref: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    affected_subject_ref: str = Field(min_length=1)


class ImpactAuthoritySnapshot(_ImpactAdmissionModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    subject: ImpactSubject
    source_record_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    derived_approval_requirements: tuple[ImpactApprovalRequirement, ...]

    @field_validator("source_record_refs")
    @classmethod
    def source_records_are_canonical_and_unambiguous(
        cls, value: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        identities: dict[tuple[str, int], str] = {}
        for reference in value:
            identity = (reference.artifact_id, reference.version)
            prior_digest = identities.get(identity)
            if prior_digest is not None:
                if prior_digest == reference.digest:
                    raise ValueError("source record references must be unique")
                raise ValueError("source record version has conflicting digests")
            identities[identity] = reference.digest
        return tuple(sorted(value, key=lambda item: (item.artifact_id, item.version, item.digest)))

    @field_validator("derived_approval_requirements")
    @classmethod
    def requirements_are_canonical_and_unambiguous(
        cls, value: tuple[ImpactApprovalRequirement, ...]
    ) -> tuple[ImpactApprovalRequirement, ...]:
        decisions: dict[tuple[str, str], str] = {}
        for requirement in value:
            identity = (requirement.authority_ref, requirement.affected_subject_ref)
            prior_reason = decisions.get(identity)
            if prior_reason is not None:
                if prior_reason == requirement.reason_code:
                    raise ValueError("derived approval requirements must be unique")
                raise ValueError("derived approval requirement has conflicting reasons")
            decisions[identity] = requirement.reason_code
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.authority_ref,
                    item.reason_code,
                    item.affected_subject_ref,
                ),
            )
        )

    @property
    def authority_digest(self) -> str:
        return digest(
            {
                "domain": _AUTHORITY_DIGEST_DOMAIN,
                "authority_snapshot": self,
            }
        )


class ImpactAdmissionBinding(_ImpactAdmissionModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    subject: ImpactSubject
    graph_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    authority_snapshot: ImpactAuthoritySnapshot
    authority_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def binds_one_exact_authority_snapshot(self) -> Self:
        if self.tenant_id != self.authority_snapshot.tenant_id:
            raise ValueError("impact binding tenant does not match authority snapshot tenant")
        if self.subject != self.authority_snapshot.subject:
            raise ValueError("impact binding subject does not match authority snapshot subject")
        if self.authority_snapshot_digest != self.authority_snapshot.authority_digest:
            raise ValueError("impact binding authority snapshot digest is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        graph_snapshot_digest: str,
        authority_snapshot: ImpactAuthoritySnapshot,
    ) -> Self:
        return cls(
            tenant_id=authority_snapshot.tenant_id,
            subject=authority_snapshot.subject,
            graph_snapshot_digest=graph_snapshot_digest,
            authority_snapshot=authority_snapshot,
            authority_snapshot_digest=authority_snapshot.authority_digest,
        )
