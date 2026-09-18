from __future__ import annotations

from pathlib import Path

import pytest
from heinzel_request_management import ApprovedProductIntent, MeasureIntent

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
    assert journey.product_iir.freshness_seconds == 86_400
    assert journey.compiler_outcome.result == "no_valid_plan"
    assert journey.compiler_outcome.rule_id == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert journey.compiler_outcome.execution_occurred is False
    # Approval took its constraints from the seeded governed records, not from the proposer.
    assert journey.approved_intent.authority_refs == journey.candidate.authority_refs
    assert journey.approved_intent.constraints is not None
    assert journey.approved_intent.constraints.approved_source_refs == ("source-live-a",)
    assert journey.approved_intent.constraints.approved_metric_refs == ("total-revenue",)
    assert journey.approved_intent.constraints.minimum_source_interval_seconds == 86_400
    # The approved intent is activated as an acquisition contract that cites it exactly, over the
    # same source observation approval was evaluated against, and replay is stable.
    assert journey.activation.contract.product_intent_ref == (
        journey.approved_intent.artifact_reference
    )
    assert journey.activation_replay == journey.activation
    assert journey.approved_intent.authority_refs is not None
    assert journey.activation.contract.source_observation_ref == (
        journey.approved_intent.authority_refs.source_observations[0].artifact_id
    )
    assert journey.activation.contract.source_binding_ref in (
        journey.approved_intent.intent.source_refs
    )
    assert (
        tuple(item.status for item in journey.compiler_outcome.preconditions)
        == ("satisfied",) * 6 + ("unsatisfied",) * 5 + ("satisfied",) + ("unsatisfied",) * 6
    )
    assert tuple(item.reason for item in journey.compiler_outcome.preconditions[6:]) == (
        "bind the provider observation to the expected tenant, warehouse, relation, and digest",
        "bind the pinned engine version, image, and build to the provider observation",
        "use a provider observation no older than ten minutes at evaluation time",
        "observe the non-null text generation and jsonb payload columns of the landing "
        "relation the statement reads",
        "observe the pinned engine-specific SUM input, accumulator, result, overflow, null, "
        "and empty-group semantics",
        "prove NUMERIC(38,9) sums fit exact Decimal(57,9) arithmetic at the signed ledger ceiling",
        "bind the physical source to exactly one authoritative generation and receipt digest",
        "bind the contributing row ceiling to owning-service cardinality evidence",
        "enforce checked Decimal(57,9) result magnitude at runtime on the pinned PostgreSQL engine",
        "authenticate the observation with provider-owned provenance authority",
        "review live checked SUM on the pinned PostgreSQL engine; "
        "cross-engine equivalence is not claimed",
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
