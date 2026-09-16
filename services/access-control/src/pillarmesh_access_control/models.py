from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel, ArtifactReference, digest
from pydantic import Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"

type EntitlementDecision = Literal["active", "revoked"]
type EntitlementPermission = Literal["dashboard", "download", "query", "view"]
type AccessMode = Literal["dashboard", "export", "query"]
type AccessGrantState = Literal[
    "pending", "active", "expired", "revocation_pending", "revoked", "failed"
]
type AccessEffectSurface = Literal["result", "superset", "warehouse"]
type AccessEffectAction = Literal["apply", "revoke"]
type AccessEffectOutcome = Literal[
    "succeeded", "transient_failure", "permanent_failure", "ambiguous_outcome"
]

_ACCESS_MODE_PERMISSION: dict[AccessMode, EntitlementPermission] = {
    "dashboard": "dashboard",
    "export": "download",
    "query": "query",
}


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _reference_key(reference: ArtifactReference) -> tuple[str, int, str]:
    return reference.artifact_id, reference.version, reference.digest


class ConnectedAuthorityProvenance(ArtifactModel):
    connected_authority_ref: str = Field(min_length=1)
    connection_binding_ref: str = Field(min_length=1)
    source_revision: int = Field(gt=0)
    source_payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    authentication_method: Literal["mutual_tls", "signed_response"]
    authentication_key_ref: str = Field(min_length=1)
    authentication_evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    adapter_ref: str = Field(min_length=1)


class EntitlementFilterDomain(ArtifactModel):
    dimension_ref: ArtifactReference
    values: tuple[str, ...] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def values_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("filter domain values must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("filter domain values must not contain duplicates")
        return tuple(sorted(value))


class _EnterpriseEntitlementScope(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    principal_ref: str = Field(min_length=1)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    decision: EntitlementDecision
    product_version_refs: tuple[ArtifactReference, ...]
    semantic_refs: tuple[ArtifactReference, ...]
    filter_domains: tuple[EntitlementFilterDomain, ...]
    permissions: tuple[EntitlementPermission, ...]
    effective_at: datetime
    valid_until: datetime

    @field_validator("effective_at", "valid_until")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("product_version_refs", "semantic_refs")
    @classmethod
    def references_are_canonical(
        cls, value: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        if len(value) != len(set(value)):
            raise ValueError("entitlement references must not contain duplicates")
        return tuple(sorted(value, key=_reference_key))

    @field_validator("permissions")
    @classmethod
    def permissions_are_canonical(
        cls, value: tuple[EntitlementPermission, ...]
    ) -> tuple[EntitlementPermission, ...]:
        if len(value) != len(set(value)):
            raise ValueError("permissions must not contain duplicates")
        return tuple(sorted(value))

    @field_validator("filter_domains")
    @classmethod
    def filter_domains_are_canonical(
        cls, value: tuple[EntitlementFilterDomain, ...]
    ) -> tuple[EntitlementFilterDomain, ...]:
        dimensions = tuple(item.dimension_ref for item in value)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("filter domains must not repeat a dimension")
        return tuple(sorted(value, key=lambda item: _reference_key(item.dimension_ref)))

    @model_validator(mode="after")
    def active_scope_is_complete(self) -> Self:
        if self.valid_until <= self.effective_at:
            raise ValueError("valid_until must be after effective_at")
        scope = (
            self.product_version_refs,
            self.semantic_refs,
            self.permissions,
        )
        if self.decision == "active" and any(not values for values in scope):
            raise ValueError("active entitlement scope must be nonempty")
        if self.decision == "revoked" and any(
            (self.product_version_refs, self.semantic_refs, self.filter_domains, self.permissions)
        ):
            raise ValueError("revoked entitlement scope must be empty")
        return self


class EnterpriseEntitlementAssertion(_EnterpriseEntitlementScope):
    provenance: ConnectedAuthorityProvenance

    def assertion_digest(self) -> str:
        return digest(
            {
                "schema_version": self.schema_version,
                "tenant_id": self.tenant_id,
                "principal_ref": self.principal_ref,
                "purpose_digest": self.purpose_digest,
                "decision": self.decision,
                "product_version_refs": self.product_version_refs,
                "semantic_refs": self.semantic_refs,
                "filter_domains": self.filter_domains,
                "permissions": self.permissions,
                "effective_at": self.effective_at,
                "valid_until": self.valid_until,
                "provenance": self.provenance,
            }
        )


class EnterpriseEntitlementObservation(EnterpriseEntitlementAssertion):
    observation_id: str = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "recorded_at")


class CurrentEntitlementSnapshot(ArtifactModel):
    schema_version: Literal["1"] = "1"
    snapshot_id: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    principal_ref: str = Field(min_length=1)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    connected_authority_ref: str = Field(min_length=1)
    source_revision: int = Field(gt=0)
    source_payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    observation_id: str = Field(min_length=1)
    product_version_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    semantic_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    filter_domains: tuple[EntitlementFilterDomain, ...]
    permissions: tuple[EntitlementPermission, ...] = Field(min_length=1)
    effective_at: datetime
    valid_until: datetime
    resolved_at: datetime

    @field_validator("effective_at", "valid_until", "resolved_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    def semantic_digest(self) -> str:
        return digest(
            {
                "schema_version": self.schema_version,
                "tenant_id": self.tenant_id,
                "principal_ref": self.principal_ref,
                "purpose_digest": self.purpose_digest,
                "connected_authority_ref": self.connected_authority_ref,
                "source_revision": self.source_revision,
                "source_payload_digest": self.source_payload_digest,
                "product_version_refs": self.product_version_refs,
                "semantic_refs": self.semantic_refs,
                "filter_domains": self.filter_domains,
                "permissions": self.permissions,
                "effective_at": self.effective_at,
                "valid_until": self.valid_until,
            }
        )

    @model_validator(mode="after")
    def digest_matches_semantic_authority(self) -> Self:
        if self.snapshot_digest != self.semantic_digest():
            raise ValueError("snapshot digest does not match semantic authority")
        return self


class AccessEffectTarget(ArtifactModel):
    surface: AccessEffectSurface
    provider_resource_ref: str = Field(min_length=1)


class ManualAccessRevocation(ArtifactModel):
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=512)
    requested_at: datetime
    base_revision: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_is_explanatory(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manual revocation reason must not be blank")
        return value

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "requested_at")


class AccessGrant(ArtifactModel):
    schema_version: Literal["1"] = "1"
    grant_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    state: AccessGrantState
    principal_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    data_product_version_ref: ArtifactReference
    fields: tuple[str, ...] = Field(min_length=1)
    classification_refs: tuple[ArtifactReference, ...]
    access_mode: AccessMode
    permissions: tuple[EntitlementPermission, ...] = Field(min_length=1)
    effective_at: datetime
    expires_at: datetime
    policy_revision: int = Field(gt=0)
    admission_receipt_ref: ArtifactReference
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    effect_targets: tuple[AccessEffectTarget, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    failed_action: AccessEffectAction | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    manual_revocation: ManualAccessRevocation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    created_at: datetime
    updated_at: datetime

    @field_validator("effective_at", "expires_at", "created_at", "updated_at")
    @classmethod
    def grant_timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("fields")
    @classmethod
    def fields_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("grant fields must be nonempty and unique")
        return tuple(sorted(value))

    @field_validator("classification_refs")
    @classmethod
    def classifications_are_canonical(
        cls, value: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        if len(value) != len(set(value)):
            raise ValueError("grant classifications must be unique")
        return tuple(sorted(value, key=_reference_key))

    @field_validator("effect_targets")
    @classmethod
    def effect_targets_are_canonical(
        cls, value: tuple[AccessEffectTarget, ...]
    ) -> tuple[AccessEffectTarget, ...]:
        surfaces = tuple(target.surface for target in value)
        if len(surfaces) != len(set(surfaces)):
            raise ValueError("grant effect targets must not repeat a surface")
        return tuple(sorted(value, key=lambda target: target.surface))

    @field_validator("permissions")
    @classmethod
    def grant_permissions_are_canonical(
        cls, value: tuple[EntitlementPermission, ...]
    ) -> tuple[EntitlementPermission, ...]:
        if len(value) != len(set(value)):
            raise ValueError("grant permissions must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def authority_and_time_are_consistent(self) -> Self:
        if self.purpose_digest != digest(self.purpose):
            raise ValueError("grant purpose digest does not match purpose")
        if self.expires_at <= self.effective_at:
            raise ValueError("grant expiry must follow its effective time")
        if self.created_at > self.updated_at:
            raise ValueError("grant updated_at cannot precede created_at")
        required_permission = _ACCESS_MODE_PERMISSION[self.access_mode]
        if required_permission not in self.permissions or "view" not in self.permissions:
            raise ValueError("grant permissions do not authorize its access mode")
        if self.state == "failed" and self.failed_action is None:
            raise ValueError("failed grant requires its failed action")
        if self.state != "failed" and self.failed_action is not None:
            raise ValueError("non-failed grant cannot carry a failed action")
        return self


class AdmittedAccessProposal(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    proposal_revision: int = Field(ge=1)
    admission_receipt_ref: ArtifactReference
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    principal_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=512)
    data_product_version_ref: ArtifactReference
    fields: tuple[str, ...] = Field(min_length=1)
    classification_refs: tuple[ArtifactReference, ...]
    access_mode: AccessMode
    permissions: tuple[EntitlementPermission, ...] = Field(min_length=1)
    effective_at: datetime
    expires_at: datetime
    policy_revision: int = Field(gt=0)
    targets: tuple[AccessEffectTarget, ...] = Field(min_length=1)

    @field_validator("effective_at", "expires_at")
    @classmethod
    def proposal_timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("fields")
    @classmethod
    def proposal_fields_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("proposal fields must be nonempty and unique")
        return tuple(sorted(value))

    @field_validator("classification_refs")
    @classmethod
    def proposal_classifications_are_canonical(
        cls, value: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        if len(value) != len(set(value)):
            raise ValueError("proposal classifications must be unique")
        return tuple(sorted(value, key=_reference_key))

    @field_validator("permissions")
    @classmethod
    def proposal_permissions_are_canonical(
        cls, value: tuple[EntitlementPermission, ...]
    ) -> tuple[EntitlementPermission, ...]:
        if len(value) != len(set(value)):
            raise ValueError("proposal permissions must be unique")
        return tuple(sorted(value))

    @field_validator("targets")
    @classmethod
    def proposal_targets_are_canonical(
        cls, value: tuple[AccessEffectTarget, ...]
    ) -> tuple[AccessEffectTarget, ...]:
        surfaces = tuple(target.surface for target in value)
        if len(surfaces) != len(set(surfaces)):
            raise ValueError("proposal targets must not repeat a surface")
        return tuple(sorted(value, key=lambda target: target.surface))

    @model_validator(mode="after")
    def proposal_scope_is_complete(self) -> Self:
        if self.expires_at <= self.effective_at:
            raise ValueError("proposal expiry must follow its effective time")
        required_permission = _ACCESS_MODE_PERMISSION[self.access_mode]
        if required_permission not in self.permissions or "view" not in self.permissions:
            raise ValueError("proposal permissions do not authorize its access mode")
        surfaces = {target.surface for target in self.targets}
        if "result" not in surfaces:
            raise ValueError("proposal requires a result target")
        if self.access_mode == "dashboard" and "superset" not in surfaces:
            raise ValueError("dashboard proposal requires a superset target")
        return self


class AccessEffectReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    effect_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    surface: AccessEffectSurface
    action: AccessEffectAction
    attempt: int = Field(ge=1)
    outcome: AccessEffectOutcome
    provider_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def effect_timestamp_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "recorded_at")
