from __future__ import annotations

from typing import Literal

from heinzel_contract_model import ArtifactModel, digest
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
    revalidate_product_physical_plan,
)
from heinzel_iir import AggregateOperation, ProductIntentIR
from pydantic import ConfigDict, Field, ValidationError

from .clickhouse_sql import emit_generation_scoped_clickhouse
from .postgresql_sql import emit_generation_scoped_postgresql
from .product_semantics import ProductSemanticError, validate_product_semantics
from .restricted_sql import evaluate_project_sum_shape

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_COMPILER_VERSION = "0.1.0"
_LEGALITY_RULE_ID = "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
_LEGALITY_RULE_VERSION = "1"


class ProductPhysicalPlanAuthority(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    contract_ref: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    warehouse_binding_id: str = Field(min_length=1)
    warehouse_binding_revision: int = Field(ge=1)
    source: GenerationScopedProductSource
    target: ProductTarget
    expected_output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)


def compose_product_physical_plan_candidate(
    product_iir: ProductIntentIR,
    *,
    authority: ProductPhysicalPlanAuthority,
    engine: Literal["postgresql", "clickhouse"],
    provider_observation_digest: str,
) -> ProductPhysicalPlan:
    authority = _revalidate_authority(authority)
    try:
        validate_product_semantics(product_iir)
    except ProductSemanticError as error:
        raise ValueError(error.reason) from None
    shape = evaluate_project_sum_shape(product_iir)
    if not all(check.satisfied for check in shape):
        raise ValueError("product IIR is outside the restricted project-sum shape")
    if authority.product_id != product_iir.product_ref:
        raise ValueError("physical plan product identity does not match the product IIR")

    emission = (
        emit_generation_scoped_postgresql(product_iir, authority.source)
        if engine == "postgresql"
        else emit_generation_scoped_clickhouse(product_iir, authority.source)
    )
    aggregate = product_iir.operations[1]
    if not isinstance(aggregate, AggregateOperation):
        raise ValueError("product IIR is outside the restricted project-sum shape")
    plan = ProductPhysicalPlan(
        compiler_version=_COMPILER_VERSION,
        legality_rule_id=_LEGALITY_RULE_ID,
        legality_rule_version=_LEGALITY_RULE_VERSION,
        tenant_id=authority.tenant_id,
        product_id=authority.product_id,
        product_revision=authority.product_revision,
        contract_ref=authority.contract_ref,
        contract_revision=authority.contract_revision,
        contract_digest=authority.contract_digest,
        iir_digest=digest(product_iir),
        provider=engine,
        warehouse_binding_id=authority.warehouse_binding_id,
        warehouse_binding_revision=authority.warehouse_binding_revision,
        provider_observation_digest=provider_observation_digest,
        source=authority.source,
        target=authority.target,
        emitted_statement=emission.statement,
        statement_digest=digest(emission.statement),
        output_columns=(
            *(reference.column_name for reference in aggregate.group_by),
            *(measure.output_name for measure in aggregate.measures),
        ),
        expected_output_schema_digest=authority.expected_output_schema_digest,
        decimal_output_checks=tuple(
            Decimal57OutputCheck(column_name=measure.output_name) for measure in aggregate.measures
        ),
    )
    return revalidate_product_physical_plan(plan)


def _revalidate_authority(
    authority: ProductPhysicalPlanAuthority,
) -> ProductPhysicalPlanAuthority:
    try:
        if type(authority) is not ProductPhysicalPlanAuthority:
            raise TypeError
        if set(vars(authority)) != set(ProductPhysicalPlanAuthority.model_fields):
            raise ValueError
        if type(authority.source) is not GenerationScopedProductSource:
            raise TypeError
        if set(vars(authority.source)) != set(GenerationScopedProductSource.model_fields):
            raise ValueError
        if type(authority.source.field_bindings) is not tuple:
            raise TypeError
        bindings: list[dict[str, object]] = []
        for binding in authority.source.field_bindings:
            if type(binding) is not ProductJsonFieldBinding:
                raise TypeError
            if set(vars(binding)) != set(ProductJsonFieldBinding.model_fields):
                raise ValueError
            bindings.append(dict(vars(binding)))
        if type(authority.target) is not ProductTarget:
            raise TypeError
        if set(vars(authority.target)) != set(ProductTarget.model_fields):
            raise ValueError
        payload: dict[str, object] = dict(vars(authority))
        source_payload: dict[str, object] = dict(vars(authority.source))
        source_payload["field_bindings"] = tuple(bindings)
        payload["source"] = source_payload
        payload["target"] = dict(vars(authority.target))
        return ProductPhysicalPlanAuthority.model_validate(payload, strict=True)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise ValueError("product physical plan authority is invalid") from None
