from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from heinzel_contract_model import ArtifactModel, ArtifactReference, digest
from pydantic import Field, field_validator, model_validator

from .models import InboxRequest

type Aggregation = Literal["sum", "count", "minimum", "maximum", "average"]
type FilterOperator = Literal["equals", "not_equals", "in", "greater_than", "less_than"]
type DeliveryOutput = Literal["dataset", "table", "dashboard"]


class Grain(ArtifactModel):
    keys: tuple[str, ...] = Field(min_length=1)


class MeasureIntent(ArtifactModel):
    metric_ref: str = Field(min_length=1)
    aggregation: Aggregation


class DimensionIntent(ArtifactModel):
    dimension_ref: str = Field(min_length=1)


class FilterIntent(ArtifactModel):
    dimension_ref: str = Field(min_length=1)
    operator: FilterOperator
    value: str = Field(min_length=1)


class FreshnessObjective(ArtifactModel):
    maximum_age_seconds: int = Field(gt=0)


class DeliveryIntent(ArtifactModel):
    outputs: tuple[DeliveryOutput, ...] = Field(min_length=1)

    @field_validator("outputs")
    @classmethod
    def outputs_are_unique(cls, value: tuple[DeliveryOutput, ...]) -> tuple[DeliveryOutput, ...]:
        if len(value) != len(set(value)):
            raise ValueError("delivery outputs must be unique")
        return value


class ProductIntent(ArtifactModel):
    schema_version: Literal["1"] = "1"
    request_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=16_000)
    business_outcome: str = Field(min_length=1, max_length=4_000)
    source_refs: tuple[str, ...] = Field(min_length=1)
    grain: Grain
    measures: tuple[MeasureIntent, ...] = Field(min_length=1)
    dimensions: tuple[DimensionIntent, ...]
    filters: tuple[FilterIntent, ...]
    freshness: FreshnessObjective
    delivery: DeliveryIntent

    def canonical_digest(self) -> str:
        return digest(self)


class ProductIntentConstraints(ArtifactModel):
    approved_source_refs: tuple[str, ...]
    approved_metric_refs: tuple[str, ...]
    approved_dimension_refs: tuple[str, ...]
    minimum_source_interval_seconds: int = Field(gt=0)


class ProductIntentAuthorityRefs(ArtifactModel):
    """The exact governed records an intent is evaluated against.

    A proposer may name these, but naming grants nothing: approval loads each referenced record
    from its owning authority and derives the constraints from what that record says. A reference
    to a record that does not exist, belongs to another tenant, has a different digest, or is no
    longer current cannot make an intent approvable.
    """

    semantic_version: ArtifactReference
    source_observations: tuple[ArtifactReference, ...] = Field(min_length=1)


class ProductIntentAuthority(Protocol):
    def resolve_constraints(
        self,
        *,
        tenant_id: str,
        authority_refs: ProductIntentAuthorityRefs,
        evaluated_at: datetime,
    ) -> ProductIntentConstraints | None:
        """Return constraints derived from the referenced records, or None if any is unusable."""
        ...


class ProductIntentSourceCoverage(ArtifactModel):
    source_ref: str = Field(min_length=1)
    covered_fields: tuple[str, ...]
    authorized: bool


class ProductIntentCandidate(ArtifactModel):
    schema_version: Literal["1"] = "1"
    candidate_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    intent: ProductIntent
    constraints: ProductIntentConstraints
    source_coverage: tuple[ProductIntentSourceCoverage, ...] = Field(min_length=1)
    unresolved_constraints: tuple[str, ...]
    proposed_by: str = Field(min_length=1)
    proposed_at: datetime
    # The proposer's constraints above are display-only; approval re-derives constraints from the
    # records named here and never trusts the proposer's copy.
    authority_refs: ProductIntentAuthorityRefs | None = None

    @field_validator("proposed_at")
    @classmethod
    def proposed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("proposed_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def candidate_matches_request_and_sources(self) -> ProductIntentCandidate:
        if self.intent.request_id != self.request_id:
            raise ValueError("candidate intent request does not match candidate request")
        coverage_refs = tuple(item.source_ref for item in self.source_coverage)
        if coverage_refs != self.intent.source_refs:
            raise ValueError("candidate source coverage must exactly match intent sources")
        return self


class ProductIntentCandidateConflictError(ValueError):
    pass


class ProductIntentCandidateStaleRevisionError(ValueError):
    pass


class ProductIntentNoValidPlan(ArtifactModel):
    result: Literal["no_valid_plan"] = "no_valid_plan"
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    constraints: tuple[str, ...] = Field(min_length=1)
    smallest_changes: tuple[str, ...] = Field(min_length=1)
    execution_occurred: Literal[False] = False


class ApprovedProductIntent(ArtifactModel):
    schema_version: Literal["1"] = "1"
    approval_id: str = Field(min_length=1)
    intent_revision: int = Field(ge=1)
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    intent: ProductIntent
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    authority_refs: ProductIntentAuthorityRefs | None = None
    constraints: ProductIntentConstraints | None = None

    @field_validator("approved_at")
    @classmethod
    def approved_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("approved_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def intent_matches_approval(self) -> ApprovedProductIntent:
        if self.intent.request_id != self.request_id:
            raise ValueError("intent request does not match approval")
        if self.intent.canonical_digest() != self.intent_digest:
            raise ValueError("intent digest does not match approved intent")
        return self

    @property
    def artifact_reference(self) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=self.approval_id,
            version=self.intent_revision,
            digest=digest(self),
        )


class ProductIntentRepository(Protocol):
    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None: ...

    def record_product_intent_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        approved_by: str,
        approved_at: datetime,
        intent: ProductIntent,
        authority_refs: ProductIntentAuthorityRefs,
        constraints: ProductIntentConstraints,
    ) -> ApprovedProductIntent: ...

    def list_product_intent_approvals(
        self, tenant_id: str, request_id: str
    ) -> tuple[ApprovedProductIntent, ...]: ...

    def load_product_intent_approval(
        self, tenant_id: str, approval_id: str
    ) -> ApprovedProductIntent | None: ...


class ProductIntentCandidateRepository(Protocol):
    def record_product_intent_candidate(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        idempotency_key: str,
        input_digest: str,
        proposed_by: str,
        proposed_at: datetime,
        intent: ProductIntent,
        constraints: ProductIntentConstraints,
        source_coverage: tuple[ProductIntentSourceCoverage, ...],
        unresolved_constraints: tuple[str, ...],
        authority_refs: ProductIntentAuthorityRefs | None = None,
    ) -> ProductIntentCandidate: ...

    def load_current_product_intent_candidate(
        self, tenant_id: str, request_id: str
    ) -> ProductIntentCandidate | None: ...


class ProductIntentCandidateService:
    def __init__(
        self, repository: ProductIntentCandidateRepository, *, clock: Callable[[], datetime]
    ) -> None:
        self._repository = repository
        self._clock = clock

    def propose(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        idempotency_key: str,
        proposed_by: str,
        intent: ProductIntent,
        constraints: ProductIntentConstraints,
        source_coverage: tuple[ProductIntentSourceCoverage, ...],
        unresolved_constraints: tuple[str, ...],
        authority_refs: ProductIntentAuthorityRefs | None = None,
    ) -> ProductIntentCandidate:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        input_digest = digest(
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "request_revision": request_revision,
                "proposed_by": proposed_by,
                "intent": intent,
                "constraints": constraints,
                "source_coverage": source_coverage,
                "unresolved_constraints": unresolved_constraints,
                # Included only when present, so earlier proposals keep their replay digests.
                **({"authority_refs": authority_refs} if authority_refs is not None else {}),
            }
        )
        return self._repository.record_product_intent_candidate(
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=request_revision,
            idempotency_key=idempotency_key,
            input_digest=input_digest,
            proposed_by=proposed_by,
            proposed_at=self._clock(),
            intent=intent,
            constraints=constraints,
            source_coverage=source_coverage,
            unresolved_constraints=unresolved_constraints,
            authority_refs=authority_refs,
        )

    def current_candidate(self, tenant_id: str, request_id: str) -> ProductIntentCandidate | None:
        return self._repository.load_current_product_intent_candidate(tenant_id, request_id)


class ProductIntentApprovalService:
    def __init__(
        self,
        repository: ProductIntentRepository,
        *,
        clock: Callable[[], datetime],
        authority: ProductIntentAuthority,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._authority = authority

    def approve(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        approved_by: str,
        intent: ProductIntent,
        authority_refs: ProductIntentAuthorityRefs | None,
    ) -> ApprovedProductIntent | ProductIntentNoValidPlan:
        """Approve an intent against constraints this service derives from governed authority.

        Constraints are never accepted from the caller. They are resolved from the exact semantic
        version and source observations named by `authority_refs`, as those records stand now.
        """
        try:
            request = self._repository.load(tenant_id, request_id)
        except KeyError:
            raise KeyError("request is unavailable") from None
        if request is None:
            raise KeyError("request is unavailable")
        if request.revision != request_revision:
            raise ValueError("request revision is stale")
        if intent.request_id != request_id:
            raise ValueError("intent request does not match approval request")

        if authority_refs is None:
            return _authority_refusal(
                intent,
                request_revision,
                "governed authority references are required",
            )
        evaluated_at = self._clock()
        constraints = self._authority.resolve_constraints(
            tenant_id=tenant_id,
            authority_refs=authority_refs,
            evaluated_at=evaluated_at,
        )
        if constraints is None:
            return _authority_refusal(
                intent,
                request_revision,
                "governed authority references are unavailable, foreign, altered, or not current",
            )
        refusal = _evaluate_constraints(
            intent=intent,
            request_revision=request_revision,
            constraints=constraints,
        )
        if refusal is not None:
            return refusal
        return self._repository.record_product_intent_approval(
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=request_revision,
            approved_by=approved_by,
            approved_at=evaluated_at,
            intent=intent,
            authority_refs=authority_refs,
            constraints=constraints,
        )

    def list_for_request(
        self, tenant_id: str, request_id: str
    ) -> tuple[ApprovedProductIntent, ...]:
        try:
            return self._repository.list_product_intent_approvals(tenant_id, request_id)
        except KeyError:
            raise KeyError("request is unavailable") from None

    def resolve(self, tenant_id: str, reference: ArtifactReference) -> ApprovedProductIntent | None:
        """Return the tenant's approval that exactly matches `reference`, or None.

        Downstream authorities cite an approval by reference. A reference whose identity, revision,
        or digest does not match a recorded approval for this tenant resolves to nothing.
        """
        approval = self._repository.load_product_intent_approval(tenant_id, reference.artifact_id)
        if (
            approval is None
            or approval.tenant_id != tenant_id
            or approval.artifact_reference != reference
        ):
            return None
        return approval


def _authority_refusal(
    intent: ProductIntent, request_revision: int, reason: str
) -> ProductIntentNoValidPlan:
    return ProductIntentNoValidPlan(
        request_id=intent.request_id,
        request_revision=request_revision,
        constraints=(reason,),
        smallest_changes=(
            "Re-propose the intent against the current approved semantic version and current "
            "source observations for this tenant.",
        ),
    )


def _evaluate_constraints(
    *,
    intent: ProductIntent,
    request_revision: int,
    constraints: ProductIntentConstraints,
) -> ProductIntentNoValidPlan | None:
    failures: list[str] = []
    changes: list[str] = []
    approved_metrics = frozenset(constraints.approved_metric_refs)
    approved_dimensions = frozenset(constraints.approved_dimension_refs)
    approved_sources = frozenset(constraints.approved_source_refs)

    for source_ref in intent.source_refs:
        if source_ref not in approved_sources:
            failures.append(f"source {source_ref} is not approved")
            changes.append("Select an approved source or request source authorization.")

    for measure in intent.measures:
        if measure.metric_ref not in approved_metrics:
            failures.append(f"metric {measure.metric_ref} is not approved")
            changes.append("Select an approved metric or request semantic approval.")
    used_dimensions = tuple(item.dimension_ref for item in intent.dimensions) + tuple(
        item.dimension_ref for item in intent.filters
    )
    for dimension_ref in used_dimensions:
        if dimension_ref not in approved_dimensions:
            failures.append(f"dimension {dimension_ref} is not approved")
            changes.append("Select an approved dimension or request semantic approval.")
    if intent.freshness.maximum_age_seconds < constraints.minimum_source_interval_seconds:
        failures.append(
            "requested freshness "
            f"{intent.freshness.maximum_age_seconds}s is below the source minimum interval "
            f"{constraints.minimum_source_interval_seconds}s"
        )
        changes.append(
            "Increase the freshness objective to the source minimum interval or improve the source."
        )

    if not failures:
        return None
    return ProductIntentNoValidPlan(
        request_id=intent.request_id,
        request_revision=request_revision,
        constraints=tuple(failures),
        smallest_changes=tuple(dict.fromkeys(changes)),
    )
