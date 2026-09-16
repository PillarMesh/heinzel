from __future__ import annotations

from typing import Literal

from pillarmesh_execution_graph import GenerationScopedProductSource, ProductJsonFieldBinding
from pillarmesh_iir import ProductIntentIR

from .sql_emitter import AggregateDialect, _quote, _render_project_sum


def render_generation_scoped_select(
    product_iir: ProductIntentIR,
    source: GenerationScopedProductSource,
    *,
    engine: Literal["postgresql", "clickhouse"],
    aggregate_dialect: AggregateDialect,
) -> str:
    source_expression = render_generation_scoped_source(product_iir, source, engine=engine)
    return _render_project_sum(
        product_iir,
        aggregate_dialect=aggregate_dialect,
        source_expression=source_expression,
    )


def render_generation_scoped_source(
    product_iir: ProductIntentIR,
    source: GenerationScopedProductSource,
    *,
    engine: Literal["postgresql", "clickhouse"],
) -> str:
    source = _revalidate_source(source)
    declared = tuple(
        (column.name, column.value_type, column.nullable) for column in product_iir.source.columns
    )
    bindings = tuple(
        (binding.logical_field, binding.scalar_type, False) for binding in source.field_bindings
    )
    if declared != bindings:
        raise ValueError(
            "generation-scoped source bindings must exactly match non-null IIR source columns"
        )

    decoded = ", ".join(
        _decode_binding(source, binding, engine=engine) for binding in source.field_bindings
    )
    generation_literal = source.generation_id
    return (
        f"(SELECT {decoded} FROM {_quote(source.namespace)}.{_quote(source.relation_name)} "
        f"WHERE {_quote(source.generation_column)} = '{generation_literal}')"
    )


def _revalidate_source(
    source: GenerationScopedProductSource,
) -> GenerationScopedProductSource:
    try:
        if type(source) is not GenerationScopedProductSource:
            raise TypeError
        if set(vars(source)) != set(GenerationScopedProductSource.model_fields):
            raise ValueError
        if type(source.field_bindings) is not tuple:
            raise TypeError
        binding_payloads: list[dict[str, object]] = []
        for binding in source.field_bindings:
            if type(binding) is not ProductJsonFieldBinding:
                raise TypeError
            if set(vars(binding)) != set(ProductJsonFieldBinding.model_fields):
                raise ValueError
            binding_payloads.append(dict(vars(binding)))
        payload: dict[str, object] = dict(vars(source))
        payload["field_bindings"] = tuple(binding_payloads)
        return GenerationScopedProductSource.model_validate(payload, strict=True)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("generation-scoped source authority is invalid") from None


def _decode_binding(
    source: GenerationScopedProductSource,
    binding: ProductJsonFieldBinding,
    *,
    engine: Literal["postgresql", "clickhouse"],
) -> str:
    payload = _quote(source.payload_column)
    json_field = binding.json_field
    output = _quote(binding.logical_field)
    if engine == "postgresql":
        extracted = f"{payload} ->> '{json_field}'"
        if binding.scalar_type == "string":
            return f"{extracted} AS {output}"
        if binding.scalar_type == "decimal":
            return f"CAST({extracted} AS NUMERIC(38,9)) AS {output}"
    else:
        extracted = f"JSONExtractString({payload}, '{json_field}')"
        if binding.scalar_type == "string":
            return f"{extracted} AS {output}"
        if binding.scalar_type == "decimal":
            return f"CAST({extracted} AS Decimal(38, 9)) AS {output}"
    raise ValueError("generation-scoped source supports only string and decimal bindings")
