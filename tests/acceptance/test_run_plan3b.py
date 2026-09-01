from __future__ import annotations

import json

import pytest

from tests.acceptance.run_plan3b import main, run_offline_plan3b


def test_offline_plan3b_harness_witnesses_all_control_plane_cases() -> None:
    result = run_offline_plan3b()

    assert result.authorized_answer_status == "ready_for_execution"
    assert result.missing_data_dependency == "data_product_change"
    assert result.unauthorized_answer_status == "denied"
    assert result.access_status == "ready_for_execution"
    assert result.clarified_outcome_bound is True
    assert result.plan2_decision_separate is True
    assert result.access_scope_narrowed is True
    assert result.distinct_access_authorities is True
    assert result.requester_candidate_hidden is True
    assert result.cross_tenant_denied is True
    assert result.unrelated_actor_denied is True
    assert result.external_effects == (
        "credentials:0",
        "grants:0",
        "messages:0",
        "provider_calls:0",
        "sql:0",
    )
    assert len(result.evidence_package_digest) == 64


def test_command_prints_only_sanitized_terminal_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "schema_version": "1",
        "status": "complete",
        "evidence_digest": run_offline_plan3b().evidence_package_digest,
    }
    assert "Net revenue" not in output
    assert "tenant-a" not in output
