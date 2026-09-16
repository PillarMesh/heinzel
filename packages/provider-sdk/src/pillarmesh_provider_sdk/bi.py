from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable

from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import ConfigDict, Field

from .models import ProviderModel

type BiLifecycleState = Literal["active", "archived"]
type BiVisualIntent = Literal["bar", "line", "number", "table"]


class _BiModel(ProviderModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def dashboard_external_key(*, tenant_id: str, dashboard_id: str, version: int) -> str:
    return (
        "pm-dashboard-"
        + digest(
            {
                "domain": "pillarmesh-dashboard-external-key-v1",
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


class BiDashboardDefinition(_BiModel):
    schema_version: Literal["2"] = "2"
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
    visual_intents: tuple[BiVisualIntent, ...]
    lifecycle_state: BiLifecycleState


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
