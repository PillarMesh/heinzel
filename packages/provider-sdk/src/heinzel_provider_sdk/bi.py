from __future__ import annotations

from collections.abc import Callable
from typing import Final, Literal, Protocol, runtime_checkable

from heinzel_contract_model import canonical_bytes, digest
from pydantic import ConfigDict, Field, model_validator

from .models import ProviderModel

type BiLifecycleState = Literal["active", "archived"]
type BiVisualIntent = Literal["bar", "line", "number", "table"]
type BiAggregate = Literal["average", "count", "maximum", "minimum", "sum"]
# The intents drawn along an axis, which is what makes a dimension a precondition for them.
_AXIS_INTENTS: Final = frozenset({"bar", "line"})


class _BiModel(ProviderModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def dashboard_external_key(*, tenant_id: str, dashboard_id: str, version: int) -> str:
    return (
        "pm-dashboard-"
        + digest(
            {
                "domain": "heinzel-dashboard-external-key-v1",
                "tenant_id": tenant_id,
                "dashboard_id": dashboard_id,
                "version": version,
            }
        )[:24]
    )


class BiDataset(_BiModel):
    stable_key: str = Field(min_length=1)
    generation: int = Field(ge=1)
    namespace: str = Field(min_length=1)
    relation_name: str = Field(min_length=1)


class BiMetricProjection(_BiModel):
    """How one governed metric is read from the relation, for a provider that must query it.

    A provider is given the semantic reference as well, and that reference is the governed
    identity; this is the physical reading of it. Without the column and the aggregate a provider
    can create a dataset and cannot ask it anything, which is a dashboard that exists and renders
    nothing.
    """

    semantic_ref: str = Field(min_length=1)
    aggregate: BiAggregate
    column_name: str = Field(min_length=1)
    output_name: str = Field(min_length=1)


class BiDimensionProjection(_BiModel):
    """How one governed dimension is read from the relation."""

    semantic_ref: str = Field(min_length=1)
    column_name: str = Field(min_length=1)
    output_name: str = Field(min_length=1)


class BiDashboardDefinition(_BiModel):
    schema_version: Literal["3"] = "3"
    tenant_id: str = Field(min_length=1)
    dashboard_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    stable_external_key: str = Field(pattern=r"^pm-dashboard-[0-9a-f]{24}$")
    desired_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_desired_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_signature: str = Field(min_length=1)
    dataset_stable_key: str = Field(min_length=1)
    dataset_generation: int = Field(ge=1)
    dataset_namespace: str = Field(min_length=1)
    dataset_relation_name: str = Field(min_length=1)
    connection_secret_ref: str = Field(pattern=r"^secret://[A-Za-z0-9_./:-]+$")
    metric_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    filter_refs: tuple[str, ...]
    metric_projections: tuple[BiMetricProjection, ...] = Field(min_length=1)
    dimension_projections: tuple[BiDimensionProjection, ...]
    visual_intents: tuple[BiVisualIntent, ...]
    lifecycle_state: BiLifecycleState

    @model_validator(mode="after")
    def every_visual_intent_can_be_drawn(self) -> BiDashboardDefinition:
        #
        # A bar and a line are drawn along an axis, so a provider given either with no dimension
        # can only create a chart that draws nothing. bi-control refuses this before it stores a
        # desired state; it is refused again here because this is the boundary a provider trusts.
        if self.dimension_projections:
            return self
        plotted = tuple(intent for intent in self.visual_intents if intent in _AXIS_INTENTS)
        if plotted:
            raise ValueError(f"{plotted[0]} needs a dimension to plot its metric along")
        return self


class BiApplyResult(_BiModel):
    schema_version: Literal["1"] = "1"
    stable_external_key: str = Field(pattern=r"^pm-dashboard-[0-9a-f]{24}$")
    desired_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: BiLifecycleState
    external_url: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)


@runtime_checkable
class BiProvider(Protocol):
    @property
    def provider_kind(self) -> Literal["superset"]: ...

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult: ...


def run_bi_provider_conformance(
    provider_factory: Callable[[], BiProvider],
    definition_factory: Callable[..., BiDashboardDefinition],
) -> None:
    provider = provider_factory()
    created_definition = definition_factory()
    created = provider.apply(created_definition)
    replay = provider.apply(created_definition)
    assert canonical_bytes(replay) == canonical_bytes(created)

    updated_definition = definition_factory(
        revision=2,
        desired_digest="c" * 64,
        prior_desired_digest=created_definition.desired_digest,
        visual_intents=("line",),
    )
    updated = provider.apply(updated_definition)
    assert updated.desired_digest == updated_definition.desired_digest
    assert updated.stable_external_key == created.stable_external_key

    archived_definition = definition_factory(
        revision=3,
        desired_digest="d" * 64,
        prior_desired_digest=updated_definition.desired_digest,
        visual_intents=("line",),
        lifecycle_state="archived",
    )
    archived = provider.apply(archived_definition)
    assert archived.lifecycle_state == "archived"
