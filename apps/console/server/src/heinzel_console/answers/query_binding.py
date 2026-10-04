"""Which column answers which approved term, read from the durable query binding.

A governed query names columns. Which column carries which approved metric or dimension is
not the compiler's to decide and not a caller's to assert: it is recorded in the approved
product query binding, and this reads it back. A caller that assembled the mapping itself
could ask for a column the owner never bound a term to.

This lived under `tests/acceptance/` and was composed nowhere, so the mapping it exists to
enforce was asserted by hand at every call site that needed one. The quickstart image ships
`apps`, `packages`, `providers` and `services` but not `tests`, so a console in a container
could not reach it at all.
"""

from __future__ import annotations

from typing import Literal, Protocol

from heinzel_compiler import (
    ProductGenerationReference,
    QueryConsumptionObject,
    QueryDimension,
    QueryMetric,
    QueryReference,
)
from heinzel_contract_model import ArtifactModel, ArtifactReference
from heinzel_semantic_registry import ApprovedProductQueryBinding
from pydantic import ConfigDict, Field

__all__ = [
    "DurableProductQueryBindingReader",
    "GovernedQueryBindingProjection",
    "ProductQueryBindingRepositoryReader",
    "ProductQueryBindingUnavailable",
]


class ProductQueryBindingUnavailable(RuntimeError):
    pass


class ProductQueryBindingRepositoryReader(Protocol):
    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductQueryBinding | None: ...


class GovernedQueryBindingProjection(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_ref: ArtifactReference
    semantic_version_ref: ArtifactReference
    materialization_receipt_ref: ArtifactReference
    lineage_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_kind: Literal["postgresql", "clickhouse"]
    consumption_object: QueryConsumptionObject
    product_generation_ref: ProductGenerationReference
    metrics: tuple[QueryMetric, ...]
    dimensions: tuple[QueryDimension, ...]
    disclosure_entity_column: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class DurableProductQueryBindingReader:
    def __init__(self, repository: ProductQueryBindingRepositoryReader) -> None:
        self._repository = repository

    def read(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
        metric_refs: tuple[ArtifactReference, ...],
        dimension_refs: tuple[ArtifactReference, ...],
    ) -> GovernedQueryBindingProjection:
        binding = self._repository.read_current(
            tenant_id=tenant_id,
            product_ref=product_ref,
            generation=generation,
        )
        if (
            binding is None
            or binding.tenant_id != tenant_id
            or binding.product_ref != product_ref
            or binding.generation != generation
            or not metric_refs
            or len(metric_refs) != len(set(metric_refs))
            or len(dimension_refs) != len(set(dimension_refs))
        ):
            raise ProductQueryBindingUnavailable("query binding unavailable")
        metric_bindings = {item.semantic_ref: item for item in binding.metric_bindings}
        dimension_bindings = {item.semantic_ref: item for item in binding.dimension_bindings}
        try:
            metrics = tuple(
                QueryMetric(
                    metric_ref=_query_reference(reference),
                    aggregate=metric_bindings[reference].aggregate,
                    column_name=metric_bindings[reference].column_name,
                    output_name=metric_bindings[reference].output_name,
                )
                for reference in metric_refs
            )
            dimensions = tuple(
                QueryDimension(
                    dimension_ref=_query_reference(reference),
                    column_name=dimension_bindings[reference].column_name,
                    output_name=dimension_bindings[reference].output_name,
                )
                for reference in dimension_refs
            )
        except KeyError:
            raise ProductQueryBindingUnavailable("query binding unavailable") from None
        return GovernedQueryBindingProjection(
            tenant_id=tenant_id,
            declaration_digest=binding.declaration_digest,
            contract_ref=binding.contract_ref,
            semantic_version_ref=binding.semantic_version_ref,
            materialization_receipt_ref=binding.materialization_receipt_ref,
            lineage_digest=binding.lineage_digest,
            engine_kind=binding.engine_kind,
            consumption_object=QueryConsumptionObject(
                object_ref=_query_reference(binding.consumption_object_ref),
                namespace=binding.namespace,
                relation_name=binding.relation_name,
            ),
            product_generation_ref=ProductGenerationReference(
                product_ref=_query_reference(binding.product_ref),
                generation=binding.generation,
            ),
            metrics=metrics,
            dimensions=dimensions,
            disclosure_entity_column=binding.disclosure_entity_column,
        )


def _query_reference(reference: ArtifactReference) -> QueryReference:
    return QueryReference.model_validate(reference.model_dump(mode="python"), strict=True)
