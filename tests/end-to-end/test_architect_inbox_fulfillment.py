from __future__ import annotations

from tests.acceptance.run_request_fulfillment import run_offline_request_fulfillment


def test_revenue_to_cash_architect_inbox_is_governed_end_to_end() -> None:
    first = run_offline_request_fulfillment()
    second = run_offline_request_fulfillment()

    assert first == second
    assert first.schema_version == "1"
    assert first.authorized_answer_status == first.access_status == "ready_for_execution"
    assert first.unauthorized_answer_status == "denied"
    assert first.clarified_outcome_bound is True
    assert first.semantic_formation_decision_separate is True
    assert first.access_scope_narrowed is True
    assert first.distinct_access_authorities is True
    assert first.cross_tenant_denied is True
