from __future__ import annotations

from pathlib import Path

import pytest
from pillarmesh_request_management import ApprovedProductIntent, MeasureIntent

from tests.acceptance.run_request_to_product import (
    build_product_iir,
    execute_postgresql_request_to_product,
    require_empty_product_publication_authority,
)


def test_user_approval_precedes_product_compilation_and_no_execution_occurs(
    tmp_path: Path,
) -> None:
    journey = execute_postgresql_request_to_product(tmp_path)

    assert journey.product_publications_before_request == 0
    assert journey.product_publications_before_compilation == 0
    assert journey.product_publications_after_compilation == 0
    assert journey.candidate.request_id == journey.request_id
    assert journey.candidate.intent == journey.approved_intent.intent
    assert journey.approved_intent.request_id == journey.request_id
    assert journey.approved_intent.request_revision == journey.request_revision
    assert journey.approved_intent.intent.delivery.outputs == ("dataset", "table", "dashboard")
    assert journey.product_iir.freshness_seconds == 3_600
    assert journey.compiler_outcome.result == "no_valid_plan"
    assert journey.compiler_outcome.rule_id == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert journey.compiler_outcome.execution_occurred is False
    assert (
        tuple(item.status for item in journey.compiler_outcome.preconditions)
        == ("satisfied",) * 6 + ("unsatisfied",) * 5 + ("satisfied",) + ("unsatisfied",) * 6
    )
    assert tuple(item.reason for item in journey.compiler_outcome.preconditions[6:]) == (
        "bind the provider observation to the expected tenant, warehouse, relation, and digest",
        "bind the pinned engine version, image, and build to the provider observation",
        "use a provider observation no older than ten minutes at evaluation time",
        "observe the exact non-null binary string and Decimal(38,9) physical columns",
        "observe the pinned engine-specific SUM input, accumulator, result, overflow, null, "
        "and empty-group semantics",
        "prove NUMERIC(38,9) sums fit exact Decimal(57,9) arithmetic at the signed ledger ceiling",
        "bind the physical source to exactly one authoritative generation and receipt digest",
        "bind the contributing row ceiling to owning-service cardinality evidence",
        "enforce checked Decimal(57,9) result magnitude on every engine at runtime",
        "authenticate the observation with provider-owned provenance authority",
        "review live cross-engine checked SUM equivalence on both pinned engines",
        "independent legality review has not approved this rule",
    )


def test_request_to_product_runner_rejects_a_conflicting_preexisting_publication() -> None:
    class PreexistingPublicationAuthority:
        def list_publications(self, *, tenant_id: str) -> tuple[object, ...]:
            assert tenant_id == "tenant-a"
            return (object(),)

    with pytest.raises(ValueError, match="empty product publication authority"):
        require_empty_product_publication_authority(
            PreexistingPublicationAuthority(), tenant_id="tenant-a"
        )


def test_product_iir_builder_rejects_an_approval_for_a_different_metric(
    tmp_path: Path,
) -> None:
    journey = execute_postgresql_request_to_product(tmp_path)
    mismatched_intent = journey.approved_intent.intent.model_copy(
        update={"measures": (MeasureIntent(metric_ref="gross_margin", aggregation="sum"),)}
    )
    mismatched_approval = journey.approved_intent.model_copy(
        update={
            "intent": mismatched_intent,
            "intent_digest": mismatched_intent.canonical_digest(),
        }
    )
    approval = ApprovedProductIntent.model_validate(mismatched_approval)

    with pytest.raises(ValueError, match="does not match the revenue product IIR mapping"):
        build_product_iir(approval)
