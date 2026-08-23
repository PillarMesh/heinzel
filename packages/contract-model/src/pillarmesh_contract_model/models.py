from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class InformationKind(StrEnum):
    BUSINESS_MEANING = "business_meaning"
    PROCESS_SEMANTICS = "process_semantics"
    IMPORTED_GLOSSARY = "imported_glossary"
    IMPORTED_CLASSIFICATION = "imported_classification"
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    METRIC = "metric"
    INTEGRITY_CONSTRAINT = "integrity_constraint"


class SemanticRuleKind(StrEnum):
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    TRANSITION = "transition"
    INTEGRITY_CONSTRAINT = "integrity_constraint"
    METRIC = "metric"


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier contains unsupported characters")
    return value


def _handle(value: str) -> str:
    if not _HANDLE.fullmatch(value):
        raise ValueError("connection handle contains unsupported characters")
    return value


class ProjectionField(ArtifactModel):
    source: str
    destination: str

    @model_validator(mode="after")
    def checked_identifiers(self) -> Self:
        _identifier(self.source)
        _identifier(self.destination)
        return self


FIXED_PROJECTION: tuple[ProjectionField, ...] = (
    ProjectionField(source="order_id", destination="order_id"),
    ProjectionField(source="customer_ref", destination="customer_ref"),
    ProjectionField(source="amount", destination="amount"),
    ProjectionField(source="currency", destination="currency"),
    ProjectionField(source="status", destination="order_status"),
    ProjectionField(source="updated_at", destination="updated_at"),
)


class SourceBinding(ArtifactModel):
    connection_handle: str
    schema_name: str = Field(alias="schema")
    table: str
    primary_key: Literal["order_id"]

    @model_validator(mode="after")
    def checked_values(self) -> Self:
        _handle(self.connection_handle)
        _identifier(self.schema_name)
        _identifier(self.table)
        return self


class DestinationBinding(ArtifactModel):
    connection_handle: str
    database: str
    schema_name: str = Field(alias="schema")
    table: str
    key: Literal["order_id"]

    @model_validator(mode="after")
    def checked_values(self) -> Self:
        _handle(self.connection_handle)
        _identifier(self.database)
        _identifier(self.schema_name)
        _identifier(self.table)
        return self


class IntegrationContract(ArtifactModel):
    schema_version: Literal["1"] = "1"
    contract_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    source: SourceBinding
    destination: DestinationBinding
    projection: tuple[ProjectionField, ...]
    materialization_mode: Literal["snapshot"] = "snapshot"
    commit_behavior: Literal["idempotent_key_upsert"] = "idempotent_key_upsert"
    deletion_behavior: Literal["not_observed"] = "not_observed"
    freshness_seconds: int = Field(gt=0, le=3600)
    data_classification: Literal["synthetic_non_sensitive"] = "synthetic_non_sensitive"
    evidence_retention: Literal["m0_30_days"] = "m0_30_days"
    producer: Literal["pillarmesh-contract-service"] = "pillarmesh-contract-service"

    @model_validator(mode="after")
    def fixed_shape(self) -> Self:
        if self.projection != FIXED_PROJECTION:
            raise ValueError("projection must equal the fixed M0 projection")
        return self

    @property
    def freshness(self) -> timedelta:
        return timedelta(seconds=self.freshness_seconds)


class ContractFormationStatus(StrEnum):
    READY_TO_ACTIVATE = "ready_to_activate"
    NEEDS_APPROVAL = "needs_approval"
    NO_VALID_PLAN = "no_valid_plan"


class ArtifactReference(ArtifactModel):
    artifact_id: str
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FieldMapping(ArtifactModel):
    source_ref: str
    semantic_ref: str
    transformation: Literal["identity", "normalized", "derived"]


class ContractConstraint(ArtifactModel):
    constraint_id: str
    kind: Literal[
        "identity",
        "referential",
        "cardinality",
        "temporal",
        "lifecycle",
        "reconciliation",
        "freshness",
        "access",
    ]
    expression: str


class DestinationProductRequirement(ArtifactModel):
    product_name: str
    warehouse_binding_id: str
    supported_engines: tuple[Literal["postgresql", "clickhouse"], ...]


class FreshnessRequirement(ArtifactModel):
    maximum_age_seconds: int = Field(gt=0)


class QualityPolicy(ArtifactModel):
    required_constraint_ids: tuple[str, ...]
    quarantine_unresolved: Literal[True] = True


class TriggerRequirement(ArtifactModel):
    cadence: Literal["daily"] = "daily"
    run_now_allowed: bool


class AccessPolicy(ArtifactModel):
    classification_refs: tuple[str, ...]
    required_approver_refs: tuple[str, ...]


class EvidencePolicy(ArtifactModel):
    retain_decision_history: Literal[True] = True
    retain_publication_receipts: Literal[True] = True


class FailurePolicy(ArtifactModel):
    unresolved_meaning: Literal["no_valid_plan"] = "no_valid_plan"
    integrity_violation: Literal["quarantine"] = "quarantine"


class Unknown(ArtifactModel):
    reason: str


class ContractNoValidPlan(ArtifactModel):
    result: Literal["no_valid_plan"] = "no_valid_plan"
    constraints: tuple[str, ...]
    smallest_changes: tuple[str, ...]
    execution_occurred: Literal[False] = False


class ManagedIntegrationContract(ArtifactModel):
    schema_version: Literal["2"] = "2"
    contract_id: str
    tenant_id: str
    version: int = Field(ge=1)
    formation_status: Literal[ContractFormationStatus.READY_TO_ACTIVATE]
    semantic_version_ref: ArtifactReference
    source_observation_refs: tuple[ArtifactReference, ...]
    mappings: tuple[FieldMapping, ...]
    integrity_constraints: tuple[ContractConstraint, ...]
    destination_product: DestinationProductRequirement
    freshness: FreshnessRequirement
    quality: QualityPolicy
    trigger_policy: TriggerRequirement
    access_policy: AccessPolicy
    evidence_policy: EvidencePolicy
    failure_policy: FailurePolicy
    approval_ids: tuple[str, ...]


class SemanticObject(ArtifactModel):
    object_id: str
    name: str
    definition: str
    source_refs: tuple[str, ...]


class SemanticRule(ArtifactModel):
    rule_id: str
    kind: SemanticRuleKind
    expression: str
    source_refs: tuple[str, ...]


class AuthorityBinding(ArtifactModel):
    information_kind: InformationKind
    subject_ref: str
    authority_ref: str
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedSemanticVersion(ArtifactModel):
    schema_version: Literal["1"] = "1"
    semantic_version_id: str
    tenant_id: str
    version: int = Field(ge=1)
    process_package_ref: ArtifactReference
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: tuple[SemanticObject, ...]
    events: tuple[SemanticObject, ...]
    states: tuple[SemanticObject, ...]
    relationships: tuple[SemanticObject, ...]
    identity_rules: tuple[SemanticRule, ...]
    constraints: tuple[SemanticRule, ...]
    metrics: tuple[SemanticObject, ...]
    classifications: tuple[SemanticObject, ...]
    authority_bindings: tuple[AuthorityBinding, ...]
    approval_ids: tuple[str, ...]
    created_at: datetime

    @model_validator(mode="after")
    def requires_utc_created_at(self) -> Self:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        return self

    def model_copy(self, *, update: Mapping[str, object] | None = None, deep: bool = False) -> Self:
        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.model_dump())


class ContractFormationResult(ArtifactModel):
    status: ContractFormationStatus
    contract: ManagedIntegrationContract | None
    no_valid_plan: ContractNoValidPlan | None
    missing_approval_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def result_matches_status(self) -> Self:
        if self.status is ContractFormationStatus.READY_TO_ACTIVATE:
            if self.contract is None or self.no_valid_plan is not None:
                raise ValueError("ready result requires only a contract")
        elif self.status is ContractFormationStatus.NO_VALID_PLAN:
            if self.no_valid_plan is None or self.contract is not None:
                raise ValueError("no-valid-plan result requires only constraints")
        elif self.contract is not None or self.no_valid_plan is not None:
            raise ValueError("needs-approval result cannot carry a terminal artifact")
        return self


class ContractFormationInput(ArtifactModel):
    tenant_id: str
    semantic_version_ref: ArtifactReference
    source_observation_refs: tuple[ArtifactReference, ...]
    destination_product: DestinationProductRequirement
    freshness: FreshnessRequirement
    quality: QualityPolicy
    trigger_policy: TriggerRequirement
    access_policy: AccessPolicy
    evidence_policy: EvidencePolicy
    failure_policy: FailurePolicy
    approval_ids: tuple[str, ...]
    identity_rules: tuple[tuple[str, str | Unknown], ...] = ()
    required_approval_ids: tuple[str, ...] = ()
