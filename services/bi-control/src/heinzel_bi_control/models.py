from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, Self

from heinzel_contract_model import ArtifactModel, ArtifactReference, FreshnessRequirement, digest
from heinzel_provider_sdk.bi import BiLifecycleState, BiVisualIntent, dashboard_external_key
from heinzel_request_management import (
    DashboardAnswerAuthority as DashboardAnswerAuthority,
)
from heinzel_request_management import (
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


# The intents drawn along an axis, which is what makes a dimension a precondition for them.
_AXIS_INTENTS: Final = frozenset({"bar", "line"})


class DashboardMetricProjection(_BiControlModel):
    """How one approved metric is read from the relation the dashboard queries.

    Beside `metric_refs` rather than instead of them, because the two answer different questions.
    The reference says which approved metric this is, and is what the contract is verified against;
    this says which column carries it and how it aggregates, which is what a BI provider needs to
    ask for it. A provider given only the reference can name a dataset and not query it.
    """

    semantic_ref: ArtifactReference
    aggregate: Literal["average", "count", "maximum", "minimum", "sum"]
    column_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    output_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class DashboardDimensionProjection(_BiControlModel):
    """How one approved dimension is read from the relation the dashboard queries."""

    semantic_ref: ArtifactReference
    column_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    output_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class DashboardDesiredState(_BiControlModel):
    schema_version: Literal["5"] = "5"
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
    # The same metrics and dimensions, as the approved query binding reads them. At least one of
    # each, because a dashboard that names no metric is one no provider can query and this is the
    # artifact a provider receipt is taken against.
    metric_projections: tuple[DashboardMetricProjection, ...] = Field(min_length=1)
    dimension_projections: tuple[DashboardDimensionProjection, ...]
    visual_intents: tuple[BiVisualIntent, ...] = Field(min_length=1)
    lifecycle_state: BiLifecycleState

    @model_validator(mode="after")
    def every_visual_intent_can_be_drawn(self) -> DashboardDesiredState:
        #
        # A bar and a line are drawn along an axis, and a provider handed either with no dimension
        # can only create a chart that draws nothing -- which it would do while this artifact
        # recorded a dashboard that was applied. Refused here instead, while it is still a desired
        # state. A number and a table need none: both render the metric over the whole relation,
        # which a running Superset was confirmed to do.
        if self.dimension_projections:
            return self
        plotted = tuple(intent for intent in self.visual_intents if intent in _AXIS_INTENTS)
        if plotted:
            raise ValueError(f"{plotted[0]} needs a dimension to plot its metric along")
        return self

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
                    "domain": "heinzel-dashboard-dataset-v1",
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


type DashboardPublicationState = Literal["pending", "published", "expired", "failed"]
type DashboardPublicationAttemptOutcome = Literal["published", "expired", "failed"]
type DashboardPublicationFailureCode = Literal[
    "authority_unavailable",
    "authority_invalid",
    "no_valid_plan",
    "stale_revision",
    "provider_unavailable",
    "provider_ambiguous",
    "provider_rejected",
]

#
# Only a failure whose cause can plausibly clear on its own is retried. An ambiguous provider
# outcome is deliberately not retryable: the external effect may already have landed, and nothing
# in the receipt distinguishes that from a call that never arrived, so resolving it is an operator's
# decision rather than this workflow's.
_RETRYABLE_FAILURE_CODES: frozenset[str] = frozenset(
    {"authority_unavailable", "provider_unavailable"}
)


class DashboardPublicationIntent(_BiControlModel):
    schema_version: Literal["1"] = "1"
    intent_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1)
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    request_id: str = Field(min_length=1)
    answer_id: str = Field(min_length=1)
    expected_revision: PositiveInt
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_expires_at: datetime
    declared_at: datetime

    @field_validator("result_expires_at", "declared_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must be UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def deadline_follows_declaration(self) -> Self:
        #
        # An intent declared at or after its deadline could never run, and declaring one would
        # record a publication that was expired before it existed. The deadline is the source
        # snapshot's expiry, which the answer authority only projects while the snapshot is still
        # readable, so a well-formed declaration always has a future deadline.
        if self.result_expires_at <= self.declared_at:
            raise ValueError("publication deadline must follow its declaration")
        return self

    @property
    def intent_digest(self) -> str:
        return digest(self)

    def publish_command(self) -> PublishDashboardCommand:
        return PublishDashboardCommand(
            tenant_id=self.tenant_id,
            dashboard_id=self.dashboard_id,
            dashboard_version=self.dashboard_version,
            request_id=self.request_id,
            answer_id=self.answer_id,
            expected_revision=self.expected_revision,
        )


class DeclareDashboardPublicationCommand(_BiControlModel):
    """Ask for a dashboard to be published from a delivered answer.

    The command deliberately carries no deadline. The window to publish is the source snapshot's
    retention, which is read from the answer authority when the intent is declared; letting a caller
    state it would let the caller grant itself a window the retention never allowed.
    """

    schema_version: Literal["1"] = "1"
    intent_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1)
    dashboard_id: DashboardId
    dashboard_version: PositiveInt
    request_id: str = Field(min_length=1)
    answer_id: str = Field(min_length=1)
    expected_revision: PositiveInt


class DashboardPublicationAttempt(_BiControlModel):
    schema_version: Literal["1"] = "1"
    intent_id: str = Field(min_length=1, max_length=256)
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: PositiveInt
    outcome: DashboardPublicationAttemptOutcome
    failure_code: DashboardPublicationFailureCode | None = None
    dashboard_revision: PositiveInt | None = None
    desired_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def outcome_carries_its_own_evidence(self) -> Self:
        if (self.dashboard_revision is None) != (self.desired_digest is None):
            raise ValueError("an applied attempt records both its revision and desired digest")
        applied = self.dashboard_revision is not None
        if self.outcome == "published":
            if self.failure_code is not None:
                raise ValueError("a published attempt records no failure code")
            if not applied:
                raise ValueError("a published attempt records the revision it applied")
            return self
        if applied:
            raise ValueError("only a published attempt records an applied revision")
        if self.outcome == "failed":
            if self.failure_code is None:
                raise ValueError("a failed attempt records its failure code")
            return self
        if self.failure_code is not None:
            raise ValueError("an expired attempt records no failure code")
        return self

    @property
    def retryable(self) -> bool:
        return self.failure_code in _RETRYABLE_FAILURE_CODES


class DashboardPublicationRecord(_BiControlModel):
    schema_version: Literal["1"] = "1"
    intent: DashboardPublicationIntent
    state: DashboardPublicationState
    attempts: tuple[DashboardPublicationAttempt, ...] = ()

    @model_validator(mode="after")
    def state_matches_its_attempts(self) -> Self:
        for position, attempt in enumerate(self.attempts, start=1):
            if attempt.attempt != position:
                raise ValueError("publication attempts must be contiguous from one")
            if (
                attempt.intent_id != self.intent.intent_id
                or attempt.intent_digest != self.intent.intent_digest
            ):
                raise ValueError("publication attempt does not belong to its intent")
        for attempt in self.attempts[:-1]:
            #
            # Only a failure that could clear is followed by a further attempt. Any other outcome
            # settles the publication, so finding one mid-history means the record is not the
            # history of one publication.
            if attempt.outcome != "failed" or not attempt.retryable:
                raise ValueError("only a retryable failure is followed by a further attempt")
        last = self.attempts[-1] if self.attempts else None
        if self.state == "pending":
            if last is not None and not (last.outcome == "failed" and last.retryable):
                raise ValueError("a pending publication's last attempt is a retryable failure")
            return self
        if last is None:
            raise ValueError("a settled publication records the attempt that settled it")
        if self.state == "failed":
            if last.outcome != "failed" or last.retryable:
                raise ValueError("a failed publication ends on a terminal failure")
            return self
        if last.outcome != self.state:
            raise ValueError("publication state does not match its last attempt")
        return self
