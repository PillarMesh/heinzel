"""Pin the exact, ordered field list of this component's durable artifacts.

Each artifact is a persisted or exchanged contract. Adding, removing, renaming or reordering a
field changes that contract, so it must be a deliberate edit that updates this table as well.
"""

from __future__ import annotations

from heinzel_runtime import AnswerExecutionReceipt


def test_answer_execution_receipt_fields_are_exact_and_ordered() -> None:
    assert tuple(AnswerExecutionReceipt.model_fields) == (
        "schema_version",
        "receipt_id",
        "tenant_id",
        "request_id",
        "plan_digest",
        "principal_class",
        "product_generation_refs",
        "attempt",
        "started_at",
        "completed_at",
        "outcome",
        "provider_error_classification",
        "row_count",
        "byte_count",
        "suppressed_group_count",
        "result_schema_digest",
        "result_digest",
        "result_ref",
        "freshness_observation_ref",
        "quality_observation_ref",
    )
