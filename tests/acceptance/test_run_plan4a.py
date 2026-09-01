from __future__ import annotations

import json
from pathlib import Path

from tests.acceptance.run_plan4a import main, run_plan4a


def test_offline_plan4a_acceptance_proves_postgresql_source_only_runtime_journey(
    tmp_path: Path,
) -> None:
    report = run_plan4a(tmp_path)

    assert report.object_refs == ("account_segments", "orders", "subscriptions")
    assert report.tenant_isolation_proven is True
    assert report.replay_proven is True
    assert report.exact_acknowledgement_proven is True
    assert report.stale_and_cross_tenant_denial_proven is True
    assert report.late_commit_proven is True
    assert report.drift_denial_proven is True
    assert report.least_privilege_proven is True
    assert report.ceiling_refusal_proven is True
    assert report.privacy_proven is True
    assert report.destination_provider_resolutions == 0
    assert report.destination_writes == 0
    assert report.delivery_claimed is False


def test_plan4a_cli_emits_only_the_sanitized_acceptance_report(
    tmp_path: Path,
    capsys: object,
) -> None:
    exit_code = main(("--work-dir", str(tmp_path)))
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["schema_version"] == "1"
    assert payload["source_provider"] == "postgresql"
    assert payload["destination_provider_resolutions"] == 0
    assert payload["destination_writes"] == 0
    assert payload["delivery_claimed"] is False
    assert "batch_id" not in captured.out
    assert "cursor" not in captured.out
