"""Pin the exact, ordered field list of this component's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field changes that contract, so it must be a deliberate edit that updates this table as well.
"""

from __future__ import annotations

from heinzel_compiler import GovernedQueryPlan


def test_governed_query_plan_fields_are_exact_and_ordered() -> None:
    assert tuple(GovernedQueryPlan.model_fields) == (
        "schema_version",
        "plan_id",
        "tenant_id",
        "validation_digest",
        "engine_kind",
        "compiler_version",
        "allowlist_version",
        "consumption_object_refs",
        "product_generation_refs",
        "minimum_group_size",
        "statement",
        "parameters",
        "statement_digest",
        "parameter_digest",
        "estimated_scan",
        "ceilings",
        "routing",
        "plan_digest",
        "signature",
    )
