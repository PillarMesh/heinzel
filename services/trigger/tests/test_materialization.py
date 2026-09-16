from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_state import RunService, SQLiteRunRepository
from pillarmesh_trigger import (
    ActivatedRunContract,
    BackfillWindowError,
    ContractNotActivatedError,
    DailyTriggerPolicy,
    RunNowPolicy,
    TriggerRunService,
    materialize_bounded_backfill,
    materialize_daily_intent,
    materialize_run_now_intent,
)

NOW = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)


class ContractReader:
    def __init__(self, contract: ActivatedRunContract | None) -> None:
        self.contract = contract

    def load_activated(
        self, tenant_id: str, contract_ref: str, revision: int
    ) -> ActivatedRunContract | None:
        contract = self.contract
        if contract is None:
            return None
        if (contract.tenant_id, contract.contract_ref, contract.revision) != (
            tenant_id,
            contract_ref,
            revision,
        ):
            return None
        return contract


def contract() -> ActivatedRunContract:
    return ActivatedRunContract(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        revision=3,
        plan_digest="2" * 64,
    )


def policy() -> DailyTriggerPolicy:
    return DailyTriggerPolicy(policy_version="daily-v1", hour_utc=8, minute_utc=15)


def test_daily_window_and_run_identity_are_canonical() -> None:
    first = materialize_daily_intent(contract(), policy(), observed_at=NOW)
    replay = materialize_daily_intent(contract(), policy(), observed_at=NOW + timedelta(hours=2))

    assert first == replay
    assert first.trigger_window.starts_at == datetime(2026, 9, 10, 8, 15, tzinfo=UTC)
    assert first.trigger_window.ends_at == datetime(2026, 9, 11, 8, 15, tzinfo=UTC)
    assert first.intent_digest == replay.intent_digest


def test_trigger_policy_version_changes_run_identity() -> None:
    first = materialize_daily_intent(contract(), policy(), observed_at=NOW)
    revised = materialize_daily_intent(
        contract(),
        policy().model_copy(update={"policy_version": "daily-v2"}),
        observed_at=NOW,
    )

    assert first.intent_digest != revised.intent_digest


def test_trigger_service_executes_a_due_window_once_and_denies_missing_authority(
    tmp_path: Path,
) -> None:
    run_service = RunService(
        SQLiteRunRepository(str(tmp_path / "runs.sqlite")),
        clock=lambda: NOW,
    )
    trigger = TriggerRunService(ContractReader(contract()), run_service)

    first = trigger.materialize_daily(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=policy(),
        observed_at=NOW,
    )
    replay = trigger.materialize_daily(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=policy(),
        observed_at=NOW,
    )

    assert replay == first
    with pytest.raises(ContractNotActivatedError, match="contract is unavailable"):
        TriggerRunService(ContractReader(None), run_service).materialize_daily(
            tenant_id="tenant-a",
            contract_ref="contract-a",
            contract_revision=3,
            policy=policy(),
            observed_at=NOW,
        )


def test_run_now_is_canonical_and_materializes_once_for_the_exact_requested_instant(
    tmp_path: Path,
) -> None:
    manual_policy = RunNowPolicy(policy_version="manual-v1")
    first_intent = materialize_run_now_intent(contract(), manual_policy, requested_at=NOW)
    replay_intent = materialize_run_now_intent(contract(), manual_policy, requested_at=NOW)
    later_intent = materialize_run_now_intent(
        contract(), manual_policy, requested_at=NOW + timedelta(microseconds=1)
    )
    trigger = TriggerRunService(
        ContractReader(contract()),
        RunService(
            SQLiteRunRepository(str(tmp_path / "run-now.sqlite")),
            clock=lambda: NOW,
        ),
    )

    first = trigger.materialize_run_now(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=manual_policy,
        requested_at=NOW,
    )
    replay = trigger.materialize_run_now(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=manual_policy,
        requested_at=NOW,
    )

    assert first_intent == replay_intent
    assert later_intent.intent_digest != first_intent.intent_digest
    assert first == replay


def test_bounded_backfill_materializes_aligned_windows_in_order_and_replays_once(
    tmp_path: Path,
) -> None:
    backfill_policy = DailyTriggerPolicy(
        policy_version="daily-v1",
        hour_utc=8,
        minute_utc=15,
        maximum_backfill_windows=2,
    )
    start = datetime(2026, 9, 9, 8, 15, tzinfo=UTC)
    end = datetime(2026, 9, 11, 8, 15, tzinfo=UTC)
    intents = materialize_bounded_backfill(contract(), backfill_policy, start=start, end=end)
    trigger = TriggerRunService(
        ContractReader(contract()),
        RunService(
            SQLiteRunRepository(str(tmp_path / "backfill.sqlite")),
            clock=lambda: NOW,
        ),
    )

    first = trigger.materialize_backfill(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=backfill_policy,
        start=start,
        end=end,
    )
    replay = trigger.materialize_backfill(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=3,
        policy=backfill_policy,
        start=start,
        end=end,
    )

    assert tuple(item.trigger_window.starts_at for item in intents) == (
        start,
        start + timedelta(days=1),
    )
    assert all(item.reason == "backfill" for item in intents)
    assert len({item.intent_digest for item in intents}) == 2
    assert first == replay


@pytest.mark.parametrize(
    ("start", "end", "message"),
    (
        (
            datetime(2026, 9, 9, 8, 15),
            datetime(2026, 9, 10, 8, 15, tzinfo=UTC),
            "timezone-aware UTC",
        ),
        (
            datetime(2026, 9, 11, 8, 15, tzinfo=UTC),
            datetime(2026, 9, 10, 8, 15, tzinfo=UTC),
            "end must follow start",
        ),
        (
            datetime(2026, 9, 9, 8, 16, tzinfo=UTC),
            datetime(2026, 9, 10, 8, 16, tzinfo=UTC),
            "aligned",
        ),
        (
            datetime(2026, 9, 8, 8, 15, tzinfo=UTC),
            datetime(2026, 9, 11, 8, 15, tzinfo=UTC),
            "maximum windows",
        ),
    ),
)
def test_backfill_rejects_invalid_or_unbounded_ranges(
    start: datetime,
    end: datetime,
    message: str,
) -> None:
    backfill_policy = DailyTriggerPolicy(
        policy_version="daily-v1",
        hour_utc=8,
        minute_utc=15,
        maximum_backfill_windows=2,
    )

    with pytest.raises(BackfillWindowError, match=message):
        materialize_bounded_backfill(contract(), backfill_policy, start=start, end=end)
