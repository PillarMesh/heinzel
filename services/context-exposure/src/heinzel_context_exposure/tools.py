from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol, Self

from heinzel_contract_model import ArtifactReference
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .delegation import (
    AgentAccessDenied,
    AgentInvocationGuard,
    AuthorizedAgentContext,
    DelegatedAgentContext,
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CATALOG_HANDLE_PATTERN = r"^catalog-[0-9a-f]{20}$"

type AgentRequestState = Literal[
    "submitted",
    "clarifying",
    "investigating",
    "proposed",
    "awaiting_approval",
    "executing",
    "verifying",
    "delivered",
    "monitoring",
    "rejected",
    "no_valid_plan",
    "cancelled",
    "failed",
    "retired",
]
type AgentAnswerValue = str | int | float | Decimal | bool | datetime | None
type AgentCatalogAssetType = Literal["data_product", "metric"]
type AgentImpactChangeType = Literal[
    "source_drift",
    "metric_version_change",
    "contract_supersession",
    "generation_failure",
    "policy_change",
    "grant_change",
    "retirement",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class AgentInvocationProvenance(_StrictModel):
    schema_version: Literal["1"] = "1"
    delegation_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    agent_client_ref: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)
    delegation_authority_ref: str = Field(min_length=1, max_length=512)
    delegation_authority_revision: int = Field(gt=0)
    entitlement_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_id: str = Field(min_length=1, max_length=512)
    policy_revision: int = Field(gt=0)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    invoked_at: datetime

    @field_validator("invoked_at")
    @classmethod
    def invoked_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "invoked_at")


class AgentCallLimits(_StrictModel):
    row_ceiling: int = Field(gt=0)
    result_byte_ceiling: int = Field(gt=0)
    scan_byte_ceiling: int = Field(gt=0)
    period_scan_byte_ceiling: int = Field(gt=0)
    statement_timeout_seconds: int = Field(gt=0)


class AgentRequestView(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=16_000)
    state: AgentRequestState
    revision: int = Field(gt=0)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "updated_at")


class AgentAnswerColumn(_StrictModel):
    name: str = Field(min_length=1, max_length=512)
    value_type: Literal["boolean", "decimal", "integer", "string", "timestamp"]


class AgentAnswerGeneration(_StrictModel):
    product_ref: ArtifactReference
    generation: int = Field(gt=0)


class AgentStatementProvenance(_StrictModel):
    statement_digest: str = Field(pattern=_DIGEST_PATTERN)
    result_digest: str = Field(pattern=_DIGEST_PATTERN)
    metric_version_refs: tuple[ArtifactReference, ...]
    product_generations: tuple[AgentAnswerGeneration, ...]
    lineage_refs: tuple[ArtifactReference, ...]
    as_of: datetime
    freshness_disposition: Literal["current", "within_slo", "stale", "unknown"]
    quality_limitation_refs: tuple[ArtifactReference, ...]
    delivered_at: datetime

    @field_validator("as_of", "delivered_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))


class AgentAnswer(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    answer_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=16_000)
    restatement: str = Field(min_length=1, max_length=4_000)
    narrative: str = Field(min_length=1, max_length=16_000)
    columns: tuple[AgentAnswerColumn, ...] = Field(min_length=1, max_length=256)
    rows: tuple[tuple[AgentAnswerValue, ...], ...]
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    provenance: AgentStatementProvenance

    @model_validator(mode="after")
    def result_shape_is_consistent(self) -> Self:
        if self.row_count != len(self.rows):
            raise ValueError("row_count must match rows")
        column_count = len(self.columns)
        if any(len(row) != column_count for row in self.rows):
            raise ValueError("each result row must match the declared columns")
        return self


class AgentAnswerExplanation(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    answer_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=16_000)
    restatement: str = Field(min_length=1, max_length=4_000)
    narrative: str = Field(min_length=1, max_length=16_000)
    provenance: AgentStatementProvenance

    @classmethod
    def from_answer(cls, answer: AgentAnswer) -> AgentAnswerExplanation:
        return cls(
            request_id=answer.request_id,
            answer_id=answer.answer_id,
            title=answer.title,
            restatement=answer.restatement,
            narrative=answer.narrative,
            provenance=answer.provenance,
        )


class AgentCatalogItem(_StrictModel):
    asset_handle: str = Field(pattern=_CATALOG_HANDLE_PATTERN)
    asset_type: AgentCatalogAssetType
    name: str = Field(min_length=1, max_length=512)
    description: str = Field(min_length=1, max_length=4_000)
    owner_label: str = Field(min_length=1, max_length=512)
    freshness_label: str = Field(min_length=1, max_length=512)


class AgentMetricDescription(_StrictModel):
    metric_handle: str = Field(pattern=_CATALOG_HANDLE_PATTERN)
    name: str = Field(min_length=1, max_length=512)
    description: str = Field(min_length=1, max_length=4_000)
    definition: str = Field(min_length=1, max_length=4_000)
    unit: str = Field(min_length=1, max_length=128)
    grain: str = Field(min_length=1, max_length=1_000)
    dimension_labels: tuple[str, ...] = Field(max_length=256)
    freshness_label: str = Field(min_length=1, max_length=512)
    owner_label: str = Field(min_length=1, max_length=512)

    @field_validator("dimension_labels")
    @classmethod
    def dimension_labels_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 512 for item in value) or len(value) != len(set(value)):
            raise ValueError("dimension labels must be non-empty, bounded, and unique")
        return value


class AgentImpactItem(_StrictModel):
    label: str = Field(min_length=1, max_length=512)
    asset_type: str = Field(min_length=1, max_length=128)
    owner_label: str = Field(min_length=1, max_length=512)


class AgentImpactApprover(_StrictModel):
    authority_label: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=1_000)


class AgentImpactView(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    change_type: AgentImpactChangeType
    subject_label: str = Field(min_length=1, max_length=512)
    analyzed_at: datetime
    validated_impacts: tuple[AgentImpactItem, ...] = Field(max_length=1_000)
    possible_impacts: tuple[AgentImpactItem, ...] = Field(max_length=1_000)
    affected_owner_labels: tuple[str, ...] = Field(max_length=1_000)
    added_approvers: tuple[AgentImpactApprover, ...] = Field(max_length=1_000)

    @field_validator("analyzed_at")
    @classmethod
    def analyzed_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "analyzed_at")

    @field_validator("affected_owner_labels")
    @classmethod
    def owner_labels_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 512 for item in value) or value != tuple(sorted(set(value))):
            raise ValueError("affected owner labels must use bounded canonical order")
        return value


class AuthorizedCatalogItem(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    item: AgentCatalogItem


class AuthorizedMetricDescription(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    metric: AgentMetricDescription


class AuthorizedImpactView(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    impact: AgentImpactView


class AskQuestionCommand(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=16_000)
    purpose: str = Field(min_length=1, max_length=512)
    question: str = Field(min_length=1, max_length=4_000)
    provenance: AgentInvocationProvenance
    limits: AgentCallLimits


class ReplyToClarificationCommand(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(gt=0)
    clarification: str = Field(min_length=1, max_length=8_000)
    provenance: AgentInvocationProvenance


class AgentRequestQuery(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)
    provenance: AgentInvocationProvenance


class ListMyRequestsQuery(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    limit: int = Field(gt=0, le=100)
    provenance: AgentInvocationProvenance


class SearchCatalogQuery(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    query: str = Field(min_length=1, max_length=1_000)
    limit: int = Field(gt=0, le=50)
    provenance: AgentInvocationProvenance


class DescribeMetricQuery(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    metric_handle: str = Field(pattern=_CATALOG_HANDLE_PATTERN)
    provenance: AgentInvocationProvenance


class GetImpactQuery(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)
    provenance: AgentInvocationProvenance


class AgentRequestBoundary(Protocol):
    def ask_question(self, command: AskQuestionCommand) -> AgentRequestView: ...

    def reply_to_clarification(self, command: ReplyToClarificationCommand) -> AgentRequestView: ...

    def get_answer(self, query: AgentRequestQuery) -> AgentAnswer | None: ...

    def explain_answer(self, query: AgentRequestQuery) -> AgentAnswerExplanation | None: ...

    def list_my_requests(self, query: ListMyRequestsQuery) -> tuple[AgentRequestView, ...]: ...


class AgentCatalogBoundary(Protocol):
    def search_catalog(self, query: SearchCatalogQuery) -> tuple[AuthorizedCatalogItem, ...]: ...

    def describe_metric(self, query: DescribeMetricQuery) -> AuthorizedMetricDescription | None: ...


class AgentImpactBoundary(Protocol):
    def get_impact(self, query: GetImpactQuery) -> AuthorizedImpactView | None: ...


class AgentInterface:
    def __init__(
        self,
        *,
        context: DelegatedAgentContext,
        guard: AgentInvocationGuard,
        requests: AgentRequestBoundary,
        catalog: AgentCatalogBoundary,
        impacts: AgentImpactBoundary,
    ) -> None:
        self._context = DelegatedAgentContext.model_validate(
            context.model_dump(mode="python"), strict=True
        )
        self._guard = guard
        self._requests = requests
        self._catalog = catalog
        self._impacts = impacts

    def search_catalog(self, *, query: str, limit: int = 20) -> tuple[AgentCatalogItem, ...]:
        untrusted = _CatalogSearchInput(query=query, limit=limit)
        authorized = self._guard.authorize(self._context)
        _require_metadata_disclosure(authorized)
        results = self._catalog.search_catalog(
            SearchCatalogQuery(
                tenant_id=authorized.tenant_id,
                principal_ref=authorized.principal_ref,
                query=untrusted.query,
                limit=untrusted.limit,
                provenance=_provenance(authorized),
            )
        )
        if len(results) > untrusted.limit:
            raise ValueError("catalog boundary exceeded the requested result limit")
        items: list[AgentCatalogItem] = []
        for result in results:
            scoped = AuthorizedCatalogItem.model_validate(_payload(result), strict=True)
            _validate_scope(scoped.tenant_id, scoped.principal_ref, authorized)
            items.append(scoped.item)
        return tuple(items)

    def describe_metric(self, *, metric_handle: str) -> AgentMetricDescription:
        untrusted = _MetricInput(metric_handle=metric_handle)
        authorized = self._guard.authorize(self._context)
        _require_metadata_disclosure(authorized)
        result = self._catalog.describe_metric(
            DescribeMetricQuery(
                tenant_id=authorized.tenant_id,
                principal_ref=authorized.principal_ref,
                metric_handle=untrusted.metric_handle,
                provenance=_provenance(authorized),
            )
        )
        if result is None:
            raise KeyError("metric is not visible")
        scoped = AuthorizedMetricDescription.model_validate(_payload(result), strict=True)
        _validate_scope(scoped.tenant_id, scoped.principal_ref, authorized)
        if scoped.metric.metric_handle != untrusted.metric_handle:
            raise ValueError("metric result does not match the requested metric")
        return scoped.metric

    def get_impact(self, *, request_id: str) -> AgentImpactView:
        untrusted = _RequestInput(request_id=request_id)
        authorized = self._guard.authorize(self._context)
        _require_metadata_disclosure(authorized)
        result = self._impacts.get_impact(
            GetImpactQuery(
                tenant_id=authorized.tenant_id,
                principal_ref=authorized.principal_ref,
                request_id=untrusted.request_id,
                provenance=_provenance(authorized),
            )
        )
        if result is None:
            raise KeyError("impact analysis is not visible")
        scoped = AuthorizedImpactView.model_validate(_payload(result), strict=True)
        _validate_scope(scoped.tenant_id, scoped.principal_ref, authorized)
        if scoped.impact.request_id != untrusted.request_id:
            raise ValueError("impact result does not match the requested request")
        return scoped.impact

    def ask_question(self, *, title: str, question: str) -> AgentRequestView:
        untrusted = _QuestionInput(title=title, question=question)
        authorized = self._guard.authorize(self._context)
        result = self._requests.ask_question(
            AskQuestionCommand(
                tenant_id=authorized.tenant_id,
                title=untrusted.title,
                purpose=authorized.purpose,
                question=untrusted.question,
                provenance=_provenance(authorized),
                limits=_limits(authorized),
            )
        )
        return AgentRequestView.model_validate(_payload(result), strict=True)

    def reply_to_clarification(
        self, *, request_id: str, expected_revision: int, clarification: str
    ) -> AgentRequestView:
        untrusted = _ClarificationInput(
            request_id=request_id,
            expected_revision=expected_revision,
            clarification=clarification,
        )
        authorized = self._guard.authorize(self._context)
        result = self._requests.reply_to_clarification(
            ReplyToClarificationCommand(
                tenant_id=authorized.tenant_id,
                request_id=untrusted.request_id,
                expected_revision=untrusted.expected_revision,
                clarification=untrusted.clarification,
                provenance=_provenance(authorized),
            )
        )
        request = AgentRequestView.model_validate(_payload(result), strict=True)
        if request.request_id != untrusted.request_id:
            raise ValueError("clarification result does not match the requested request")
        return request

    def get_answer(self, *, request_id: str) -> AgentAnswer:
        query_input = _RequestInput(request_id=request_id)
        authorized = self._guard.authorize(self._context)
        if authorized.policy.model_disclosure != "results":
            raise AgentAccessDenied("model_disclosure_denied")
        result = self._requests.get_answer(
            AgentRequestQuery(
                tenant_id=authorized.tenant_id,
                request_id=query_input.request_id,
                provenance=_provenance(authorized),
            )
        )
        if result is None:
            raise KeyError("answer is not visible")
        answer = AgentAnswer.model_validate(_payload(result), strict=True)
        if answer.request_id != query_input.request_id:
            raise ValueError("answer does not match the requested request")
        if answer.row_count > authorized.policy.row_ceiling:
            raise ValueError("answer exceeds current row ceiling")
        if answer.byte_count > authorized.policy.byte_ceiling:
            raise ValueError("answer exceeds current result byte ceiling")
        return answer

    def explain_answer(self, *, request_id: str) -> AgentAnswerExplanation:
        query_input = _RequestInput(request_id=request_id)
        authorized = self._guard.authorize(self._context)
        if authorized.policy.model_disclosure == "none":
            raise AgentAccessDenied("model_disclosure_denied")
        result = self._requests.explain_answer(
            AgentRequestQuery(
                tenant_id=authorized.tenant_id,
                request_id=query_input.request_id,
                provenance=_provenance(authorized),
            )
        )
        if result is None:
            raise KeyError("answer explanation is not visible")
        explanation = AgentAnswerExplanation.model_validate(_payload(result), strict=True)
        if explanation.request_id != query_input.request_id:
            raise ValueError("answer explanation does not match the requested request")
        return explanation

    def list_my_requests(self, *, limit: int = 50) -> tuple[AgentRequestView, ...]:
        untrusted = _ListInput(limit=limit)
        authorized = self._guard.authorize(self._context)
        results = self._requests.list_my_requests(
            ListMyRequestsQuery(
                tenant_id=authorized.tenant_id,
                principal_ref=authorized.principal_ref,
                limit=untrusted.limit,
                provenance=_provenance(authorized),
            )
        )
        if len(results) > untrusted.limit:
            raise ValueError("request boundary exceeded the requested result limit")
        return tuple(
            AgentRequestView.model_validate(_payload(item), strict=True) for item in results
        )


class _QuestionInput(_StrictModel):
    title: str = Field(min_length=1, max_length=16_000)
    question: str = Field(min_length=1, max_length=4_000)


class _ClarificationInput(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(gt=0)
    clarification: str = Field(min_length=1, max_length=8_000)


class _RequestInput(_StrictModel):
    request_id: str = Field(min_length=1, max_length=512)


class _ListInput(_StrictModel):
    limit: int = Field(gt=0, le=100)


class _CatalogSearchInput(_StrictModel):
    query: str = Field(min_length=1, max_length=1_000)
    limit: int = Field(gt=0, le=50)


class _MetricInput(_StrictModel):
    metric_handle: str = Field(pattern=_CATALOG_HANDLE_PATTERN)


def _provenance(context: AuthorizedAgentContext) -> AgentInvocationProvenance:
    return AgentInvocationProvenance(
        delegation_id=context.delegation_id,
        principal_ref=context.principal_ref,
        agent_client_ref=context.agent_client_ref,
        purpose_digest=context.purpose_digest,
        delegation_authority_ref=context.delegation.authority_ref,
        delegation_authority_revision=context.delegation.authority_revision,
        entitlement_snapshot_digest=context.entitlement_snapshot_digest,
        policy_id=context.policy.policy_id,
        policy_revision=context.policy.revision,
        policy_digest=context.policy_digest,
        invoked_at=context.authorized_at,
    )


def _limits(context: AuthorizedAgentContext) -> AgentCallLimits:
    return AgentCallLimits(
        row_ceiling=context.policy.row_ceiling,
        result_byte_ceiling=context.policy.byte_ceiling,
        scan_byte_ceiling=context.policy.scan_ceiling,
        period_scan_byte_ceiling=context.policy.period_scan_budget,
        statement_timeout_seconds=context.policy.statement_timeout,
    )


def _require_metadata_disclosure(context: AuthorizedAgentContext) -> None:
    if context.policy.model_disclosure == "none":
        raise AgentAccessDenied("model_disclosure_denied")


def _validate_scope(tenant_id: str, principal_ref: str, context: AuthorizedAgentContext) -> None:
    if tenant_id != context.tenant_id or principal_ref != context.principal_ref:
        raise ValueError("owning-service result does not match the current authorization scope")


def _payload(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value
