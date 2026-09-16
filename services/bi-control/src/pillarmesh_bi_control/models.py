from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pillarmesh_contract_model import ArtifactModel, ArtifactReference, FreshnessRequirement, digest
from pillarmesh_provider_sdk.bi import BiLifecycleState, BiVisualIntent, dashboard_external_key
from pillarmesh_request_management import (
    DashboardAnswerAuthority as DashboardAnswerAuthority,
)
from pillarmesh_request_management import (
    FreshnessDisposition as FreshnessDisposition,
)
from pydantic import ConfigDict, Field, PositiveInt, field_validator, model_validator

type DashboardId = Annotated[str, Field(min_length=1)]
type PrincipalReference = Annotated[str, Field(min_length=1)]
type DataProductVersionReference = ArtifactReference
type MetricVersionReference = ArtifactReference
type DimensionReference = ArtifactReference
type DashboardFilter = ArtifactReference
type VisualIntent = BiVisualIntent
type DrillPath = tuple[DimensionReference, ...]
type FreshnessObjective = FreshnessRequirement
type AccessPolicyReference = ArtifactReference
type ReportDeliveryPolicy = ArtifactReference
type DashboardAcceptanceTest = ArtifactReference
type DashboardLifecycleState = Literal["draft", "certified", "archived"]
type DashboardAccessState = Literal[
    "pending", "active", "expired", "revocation_pending", "revoked", "failed"
]


class _BiControlModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DashboardContract(_BiControlModel):
    dashboard_id: DashboardId
    version: PositiveInt
    owner: PrincipalReference
    audience: tuple[PrincipalReference, ...]
    data_product_versions: tuple[DataProductVersionReference, ...]
    metric_versions: tuple[MetricVersionReference, ...]
    dimensions: tuple[DimensionReference, ...]
    filters: tuple[DashboardFilter, ...]
    visual_intents: tuple[VisualIntent, ...]
    drill_paths: tuple[DrillPath, ...]
    freshness_requirement: FreshnessObjective
    access_policy: AccessPolicyReference
    report_delivery_policy: ReportDeliveryPolicy | None
    acceptance_tests: tuple[DashboardAcceptanceTest, ...]
    lifecycle_state: DashboardLifecycleState


class SignedDashboardContract(_BiControlModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    contract: DashboardContract
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def contract_digest_matches_payload(self) -> SignedDashboardContract:
        if self.contract_digest != digest(self.contract):
            raise ValueError("dashboard contract digest does not match its payload")
        return self


class DashboardDatasetConnectionBinding(_BiControlModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    engine_kind: Literal["postgresql", "clickhouse"]
    consumption_object_ref: ArtifactReference
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    relation_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    warehouse_binding_id: str = Field(min_length=1)
    warehouse_binding_revision: int = Field(ge=1)
    warehouse_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_secret_ref: str = Field(pattern=r"^secret://[A-Za-z0-9_./:-]+$")


class PublishDashboardCommand(_BiControlModel):
    tenant_id: str = Field(min_length=1)
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    request_id: str = Field(min_length=1)
    answer_id: str = Field(min_length=1)
    expected_revision: PositiveInt


class DashboardPublication(_BiControlModel):
    schema_version: Literal["1"] = "1"
    dashboard_id: DashboardId
    version: PositiveInt
    title: str = Field(min_length=1)
    source_request_id: str = Field(min_length=1)
    data_product_version_ref: ArtifactReference
    lifecycle_state: BiLifecycleState
    as_of: datetime
    freshness_disposition: FreshnessDisposition
    published_at: datetime

    @field_validator("as_of", "published_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("dashboard publication timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)


class DashboardAccessAuthorization(_BiControlModel):
    schema_version: Literal["1"] = "1"
    authorization_id: str = Field(min_length=1)
    revision: PositiveInt
    state: DashboardAccessState
    tenant_id: str = Field(min_length=1)
    access_request_id: str = Field(min_length=1)
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    data_product_version_ref: ArtifactReference
    principal_ref: PrincipalReference
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_at: datetime
    expires_at: datetime
    verified_at: datetime

    @field_validator("effective_at", "expires_at", "verified_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must be UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> Self:
        if self.expires_at <= self.effective_at:
            raise ValueError("authorization expiry must follow its effective time")
        return self

    @property
    def authorization_digest(self) -> str:
        return digest(self.model_dump(mode="python", exclude={"verified_at"}))


class DashboardLinkIssueCommand(_BiControlModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    principal_ref: PrincipalReference
    purpose: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def purpose_digest_matches_purpose(self) -> Self:
        if self.purpose_digest != digest(self.purpose):
            raise ValueError("dashboard link purpose digest does not match purpose")
        return self


class DashboardLink(_BiControlModel):
    schema_version: Literal["1"] = "1"
    reference: str = Field(pattern=r"^dashboard-link:[A-Za-z0-9_-]{32,128}$")
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("expires_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class DashboardPrivateTarget(_BiControlModel):
    external_url: str = Field(min_length=1)


class DashboardLinkSession(_BiControlModel):
    schema_version: Literal["1"] = "1"
    reference: str = Field(pattern=r"^dashboard-link:[A-Za-z0-9_-]{32,128}$")
    tenant_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    dashboard_revision: PositiveInt
    desired_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization: DashboardAccessAuthorization
    principal_ref: PrincipalReference
    purpose: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_url: str = Field(min_length=1)
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def link_timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must be UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authority_matches_session(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("link expiry must follow issuance")
        if (
            self.authorization.tenant_id != self.tenant_id
            or self.authorization.dashboard_id != self.dashboard_id
            or self.authorization.dashboard_version != self.dashboard_version
            or self.authorization.principal_ref != self.principal_ref
            or self.authorization.purpose_digest != self.purpose_digest
            or digest(self.purpose) != self.purpose_digest
        ):
            raise ValueError("link authorization does not match its session")
        return self


class DashboardDesiredState(_BiControlModel):
    schema_version: Literal["4"] = "4"
    tenant_id: str = Field(min_length=1)
    dashboard_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    prior_desired_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_key_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    contract_signature: str = Field(min_length=1)
    source_answer: DashboardAnswerAuthority
    dataset_product_ref: ArtifactReference
    dataset_generation: int = Field(ge=1)
    consumption_object_ref: ArtifactReference
    materialization_receipt_ref: ArtifactReference
    product_publication_ref: ArtifactReference
    dataset_namespace: str = Field(min_length=1)
    dataset_relation_name: str = Field(min_length=1)
    warehouse_binding_id: str = Field(min_length=1)
    warehouse_binding_revision: int = Field(ge=1)
    warehouse_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_secret_ref: str = Field(pattern=r"^secret://[A-Za-z0-9_./:-]+$")
    metric_refs: tuple[ArtifactReference, ...]
    dimension_refs: tuple[ArtifactReference, ...]
    filter_refs: tuple[ArtifactReference, ...]
    visual_intents: tuple[BiVisualIntent, ...] = Field(min_length=1)
    lifecycle_state: BiLifecycleState

    @model_validator(mode="after")
    def requires_revision_chain(self) -> DashboardDesiredState:
        if (self.revision == 1) != (self.prior_desired_digest is None):
            raise ValueError("only the first desired revision omits its prior digest")
        if len(self.source_answer.product_generation_refs) != 1:
            raise ValueError("dashboard desired state requires one product generation")
        generation = self.source_answer.product_generation_refs[0]
        if (
            self.source_answer.tenant_id != self.tenant_id
            or self.source_answer.title != self.title
            or self.source_answer.metric_version_refs != self.metric_refs
            or generation.product_ref != self.dataset_product_ref
            or generation.generation != self.dataset_generation
            or self.consumption_object_ref.version != self.dataset_generation
            or self.materialization_receipt_ref.version != self.dataset_generation
            or self.product_publication_ref.version != self.dataset_generation
        ):
            raise ValueError("dashboard desired state authority references do not match")
        return self

    @property
    def desired_digest(self) -> str:
        return digest(self)

    @property
    def stable_external_key(self) -> str:
        return dashboard_external_key(
            tenant_id=self.tenant_id,
            dashboard_id=self.dashboard_id,
            version=self.version,
        )

    @property
    def dataset_stable_key(self) -> str:
        return (
            "pm-dataset-"
            + digest(
                {
                    "domain": "pillarmesh-dashboard-dataset-v1",
                    "tenant_id": self.tenant_id,
                    "consumption_object_ref": self.consumption_object_ref,
                }
            )[:24]
        )


class DashboardProviderReceipt(_BiControlModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    dashboard_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    stable_external_key: str = Field(pattern=r"^pm-dashboard-[0-9a-f]{24}$")
    desired_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: BiLifecycleState
    external_url: str = Field(min_length=1)
    provider_kind: Literal["superset"] = "superset"
    provider_version: str = Field(min_length=1)
    applied_at: datetime

    @field_validator("applied_at")
    @classmethod
    def requires_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("applied_at must be timezone-aware UTC")
        return value.astimezone(UTC)
