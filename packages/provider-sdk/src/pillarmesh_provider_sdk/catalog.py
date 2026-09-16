from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self, runtime_checkable

from pillarmesh_contract_model import ArtifactReference, digest
from pydantic import ConfigDict, Field, field_validator, model_validator

from .models import ProviderModel

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class _CatalogModel(ProviderModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def catalog_product_external_key(
    *, tenant_id: str, product_id: str, product_revision: int, generation: int
) -> str:
    if not tenant_id or not product_id or product_revision < 1 or generation < 1:
        raise ValueError("catalog product identity must be complete")
    return (
        "pm-product-"
        + digest(
            {
                "domain": "pillarmesh-catalog-product-external-key-v1",
                "tenant_id": tenant_id,
                "product_id": product_id,
                "product_revision": product_revision,
                "generation": generation,
            }
        )[:24]
    )


def catalog_warehouse_service_external_key(*, tenant_id: str, warehouse_binding_id: str) -> str:
    if not tenant_id or not warehouse_binding_id:
        raise ValueError("catalog warehouse service identity must be complete")
    return (
        "pm_warehouse_"
        + digest(
            {
                "domain": "pillarmesh-catalog-warehouse-service-external-key-v1",
                "tenant_id": tenant_id,
                "warehouse_binding_id": warehouse_binding_id,
            }
        )[:24]
    )


class CatalogColumn(_CatalogModel):
    name: str = Field(min_length=1)
    type_name: str = Field(min_length=1)
    nullable: bool
    description: str | None = Field(default=None, min_length=1)


class CatalogLineageSource(_CatalogModel):
    source_ref: str = Field(min_length=1)
    freshness_observation_ref: ArtifactReference
    freshness_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    watermark_at: datetime
    observed_at: datetime

    @field_validator("watermark_at", "observed_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("catalog freshness timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def watermark_precedes_observation(self) -> Self:
        if self.watermark_at > self.observed_at:
            raise ValueError("catalog freshness watermark cannot follow observation")
        return self


class CatalogProductDefinition(_CatalogModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    idempotency_key: str = Field(pattern=_DIGEST_PATTERN)
    stable_external_key: str = Field(pattern=r"^pm-product-[0-9a-f]{24}$")
    catalog_binding_id: str = Field(min_length=1)
    catalog_revision: int = Field(ge=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_refs: tuple[str, ...] = Field(min_length=1)
    namespace: str = Field(min_length=1)
    relation_name: str = Field(min_length=1)
    columns: tuple[CatalogColumn, ...] = Field(min_length=1)
    lineage_sources: tuple[CatalogLineageSource, ...] = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    semantic_version_digest: str = Field(pattern=_DIGEST_PATTERN)
    materialization_receipt_ref: ArtifactReference
    materialization_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    publication_authority_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_collections_are_canonical(self) -> Self:
        expected_external_key = catalog_product_external_key(
            tenant_id=self.tenant_id,
            product_id=self.product_id,
            product_revision=self.product_revision,
            generation=self.generation,
        )
        if self.stable_external_key != expected_external_key:
            raise ValueError("stable external key does not match product identity")
        column_names = tuple(column.name for column in self.columns)
        if len(column_names) != len(set(column_names)):
            raise ValueError("column names must be unique")
        if len(self.owner_refs) != len(set(self.owner_refs)) or any(
            not owner_ref for owner_ref in self.owner_refs
        ):
            raise ValueError("owner references must be unique and nonempty")
        if self.owner_refs != tuple(sorted(self.owner_refs)):
            raise ValueError("owner references must be canonically ordered")
        source_refs = tuple(source.source_ref for source in self.lineage_sources)
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("lineage source references must be unique")
        if source_refs != tuple(sorted(source_refs)):
            raise ValueError("lineage sources must be canonically ordered")
        freshness_refs = tuple(source.freshness_observation_ref for source in self.lineage_sources)
        if len(freshness_refs) != len(set(freshness_refs)):
            raise ValueError("freshness observation references must be unique")
        return self


type CatalogWarehouseProviderKind = Literal["postgresql", "clickhouse"]


class CatalogWarehouseHierarchyAuthority(_CatalogModel):
    """Exact managed warehouse hierarchy that owns a published table."""

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    warehouse_binding_id: str = Field(min_length=1)
    warehouse_binding_revision: int = Field(ge=1)
    warehouse_provider: CatalogWarehouseProviderKind
    database_service_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    database_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    schema_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
    table_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")

    @model_validator(mode="after")
    def database_service_is_tenant_and_binding_scoped(self) -> Self:
        expected = catalog_warehouse_service_external_key(
            tenant_id=self.tenant_id,
            warehouse_binding_id=self.warehouse_binding_id,
        )
        if self.database_service_name != expected:
            raise ValueError("catalog warehouse service name does not match binding authority")
        return self


class CatalogNativeTableDefinition(_CatalogModel):
    """Product definition bound to its authoritative managed warehouse location."""

    schema_version: Literal["1"] = "1"
    product: CatalogProductDefinition
    warehouse: CatalogWarehouseHierarchyAuthority

    @model_validator(mode="after")
    def product_matches_warehouse_authority(self) -> Self:
        if self.product.tenant_id != self.warehouse.tenant_id:
            raise ValueError("catalog table product and warehouse must share one tenant")
        if (
            self.product.namespace != self.warehouse.schema_name
            or self.product.relation_name != self.warehouse.table_name
        ):
            raise ValueError("catalog table relation must match warehouse hierarchy authority")
        return self


class CatalogNativeTableObservation(_CatalogModel):
    """Exact OpenMetadata readback of one governed native warehouse table."""

    schema_version: Literal["1"] = "1"
    definition: CatalogNativeTableDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    table_fully_qualified_name: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def observation_matches_definition(self) -> Self:
        if self.definition_digest != digest(self.definition):
            raise ValueError("catalog table observation digest does not match its definition")
        hierarchy = self.definition.warehouse
        expected_fqn = ".".join(
            (
                hierarchy.database_service_name,
                hierarchy.database_name,
                hierarchy.schema_name,
                hierarchy.table_name,
            )
        )
        if self.table_fully_qualified_name != expected_fqn:
            raise ValueError("catalog table observation does not match warehouse hierarchy")
        return self


class CatalogProductObservation(_CatalogModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    stable_external_key: str = Field(pattern=r"^pm-product-[0-9a-f]{24}$")
    definition: CatalogProductDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def observed_authority_is_exact(self) -> Self:
        if (
            self.tenant_id != self.definition.tenant_id
            or self.stable_external_key != self.definition.stable_external_key
        ):
            raise ValueError("catalog observation identity does not match its definition")
        if self.definition_digest != digest(self.definition):
            raise ValueError("catalog observation digest does not match its definition")
        return self


@runtime_checkable
class CatalogProductProvider(Protocol):
    @property
    def provider_kind(self) -> Literal["openmetadata"]: ...

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation: ...

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation: ...
