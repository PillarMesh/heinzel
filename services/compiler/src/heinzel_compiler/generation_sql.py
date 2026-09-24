from __future__ import annotations

from typing import Literal

from heinzel_execution_graph import GenerationScopedProductSource, ProductJsonFieldBinding
from heinzel_iir import ProductIntentIR

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
    equals = "OPERATOR(pg_catalog.=)" if engine == "postgresql" else "="
    return (
        f"(SELECT {decoded} FROM {_quote(source.namespace)}.{_quote(source.relation_name)} "
        f"WHERE {_quote(source.generation_column)} {equals} '{generation_literal}')"
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
        return _decode_postgresql_binding(source, binding, payload=payload, output=output)
    else:
        extracted = f"JSONExtractString({payload}, '{json_field}')"
        if binding.scalar_type == "string":
            return f"{extracted} AS {output}"
        if binding.scalar_type == "decimal":
            return f"CAST({extracted} AS Decimal(38, 9)) AS {output}"
    raise ValueError("generation-scoped source supports only string and decimal bindings")


# A canonical Decimal(38,9) literal: an optional minus sign, 1-29 integer digits with no
# leading zero, and at most nine fractional digits. PostgreSQL's numeric input is far more
# permissive -- it accepts NaN, hex (0x10), underscores (1_000), exponents (1e3), leading plus
# signs and surrounding whitespace, and it rounds excess scale -- so the statement refuses any
# value that is not already in this exact form instead of relying on the cast.
_POSTGRESQL_CANONICAL_DECIMAL = r"^-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?$"


def _decode_postgresql_binding(
    source: GenerationScopedProductSource,
    binding: ProductJsonFieldBinding,
    *,
    payload: str,
    output: str,
) -> str:
    """Decode one landing field, failing the whole statement on any value outside its contract.

    Each field must be present as a JSON string. A missing key, JSON null, number, boolean,
    array or object is refused rather than coerced to text. The refusal casts a message that
    names the row's generation to NUMERIC, which always raises SQLSTATE 22P02; referencing the
    generation column keeps the planner from folding that cast into a constant that would fail
    every statement. String fields are grouped under the C collation explicitly, so grouping
    never depends on the database's default collation.
    """
    # Every function, operator, type and collation is qualified to pg_catalog. An unqualified name
    # resolves through search_path, so a session with `search_path = <schema>, pg_catalog` could
    # substitute a hostile jsonb_typeof or `~` and let "0x10" through as 16 -- verified live.
    node = f"{payload} OPERATOR(pg_catalog.->) '{binding.json_field}'"
    text = f"{payload} OPERATOR(pg_catalog.->>) '{binding.json_field}'"
    is_string = f"pg_catalog.jsonb_typeof({node}) OPERATOR(pg_catalog.=) 'string'"
    refusal = (
        f"'heinzel refused a {binding.scalar_type} landing value in generation ' "
        f"OPERATOR(pg_catalog.||) {_quote(source.generation_column)}"
    )
    if binding.scalar_type == "string":
        return (
            f"(CASE WHEN {is_string} THEN {text} "
            f"ELSE CAST(CAST({refusal} AS NUMERIC) AS pg_catalog.text) END) "
            f'COLLATE pg_catalog."C" AS {output}'
        )
    if binding.scalar_type == "decimal":
        return (
            f"CAST(CASE WHEN {is_string} "
            f"AND {text} OPERATOR(pg_catalog.~) '{_POSTGRESQL_CANONICAL_DECIMAL}' "
            f"THEN {text} ELSE {refusal} END AS NUMERIC(38,9)) AS {output}"
        )
    raise ValueError("generation-scoped source supports only string and decimal bindings")
