from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import (
    AcquisitionTransientError,
    LeasedRunExecutor,
    RunLease,
    RunLeaseLostError,
    RunStage,
    RunStageFailedError,
    classify_run_stage_failure,
)
from heinzel_state import RunAttemptClaim, RunService
from heinzel_state.run_models import (
    RunAttemptCompletion,
    RunAttemptLeaseExtension,
    RunIntent,
    TriggerWindow,
)
from heinzel_state.run_repository import SQLiteRunRepository

_NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
_TENANT = "tenant-a"


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


class _IdempotentEffect:
    """A durable effect keyed by its name: repeating it returns the first result."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.effects: list[int] = []
        self.invocations: list[int] = []

    def __call__(self, lease: RunLease) -> str:
        self.invocations.append(lease.claim.epoch)
        if not self.effects:
            self.effects.append(lease.claim.epoch)
        return f"{self.name}-receipt"


def _runs(tmp_path: Path, clock: _Clock) -> tuple[RunService, str]:
    runs = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)
    run = runs.materialize(
        RunIntent(
            tenant_id=_TENANT,
            contract_id="contract-revenue",
            contract_revision=1,
            plan_digest="a" * 64,
            trigger_policy_version="daily-v1",
            trigger_window=TriggerWindow(starts_at=_NOW, ends_at=_NOW + timedelta(days=1)),
            reason="scheduled",
        )
    )
    return runs, run.run_id


def test_a_run_executes_its_stages_once_and_completes_at_the_last_boundary(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    prepare, land = _IdempotentEffect("prepared"), _IdempotentEffect("checkpoint")

    outcome = LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
        tenant_id=_TENANT,
        run_id=run_id,
        stages=(RunStage("acquisition_prepared", prepare), RunStage("land_acknowledged", land)),
    )

    assert outcome.boundaries == (
        ("acquisition_prepared", "prepared-receipt"),
        ("land_acknowledged", "checkpoint-receipt"),
    )
    assert outcome.completion.outcome == "succeeded"
    assert outcome.completion.durable_boundary_ref == "land_acknowledged:checkpoint-receipt"
    assert (outcome.claim.epoch, prepare.invocations, land.invocations) == (1, [1], [1])


def test_a_worker_that_loses_its_lease_stops_at_the_next_boundary_and_a_successor_resumes(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    prepare, land = _IdempotentEffect("prepared"), _IdempotentEffect("checkpoint")

    def prepare_then_stall(lease: RunLease) -> str:
        receipt = prepare(lease)
        clock.now += timedelta(seconds=61)  # the worker stalls past its lease after the effect
        return receipt

    with pytest.raises(RunLeaseLostError, match="run lease expired"):
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", prepare_then_stall),
                RunStage("land_acknowledged", land),
            ),
        )
    assert land.invocations == []

    resumed = LeasedRunExecutor(runs, worker_id="worker-b", lease_seconds=60).execute(
        tenant_id=_TENANT,
        run_id=run_id,
        stages=(RunStage("acquisition_prepared", prepare), RunStage("land_acknowledged", land)),
    )

    # The successor replays the proven boundary without a second effect, then finishes.
    assert (resumed.claim.attempt_number, resumed.claim.epoch) == (2, 2)
    assert prepare.invocations == [1, 2]
    assert prepare.effects == [1]
    assert land.effects == [2]
    assert resumed.completion.durable_boundary_ref == "land_acknowledged:checkpoint-receipt"


def test_a_superseded_worker_cannot_run_later_stages_or_record_a_terminal_outcome(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    land = _IdempotentEffect("checkpoint")
    successor: list[RunAttemptClaim] = []

    def superseded_during_prepare(lease: RunLease) -> str:
        clock.now += timedelta(seconds=61)
        successor.append(
            runs.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60)
        )
        return "prepared-receipt"

    with pytest.raises(RunLeaseLostError, match="stale run epoch"):
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", superseded_during_prepare),
                RunStage("land_acknowledged", land),
            ),
        )

    assert land.invocations == []
    runs.require_current_attempt(tenant_id=_TENANT, claim=successor[0])


def test_a_transient_stage_failure_is_recorded_retryable_at_the_last_proven_boundary(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    prepare = _IdempotentEffect("prepared")

    def unavailable(_lease: RunLease) -> str:
        raise ProviderError("destination unavailable", "transient_unavailable")

    with pytest.raises(RunStageFailedError) as failed:
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", prepare),
                RunStage("land_acknowledged", unavailable),
            ),
        )

    completion = failed.value.completion
    assert (completion.outcome, completion.failure_classification) == ("failed", "transient")
    assert completion.durable_boundary_ref == "acquisition_prepared:prepared-receipt"
    assert failed.value.boundary == "land_acknowledged"
    assert "destination unavailable" not in str(failed.value)
    # A transient failure leaves the run claimable for a new attempt.
    retry = runs.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60)
    assert retry.epoch == 2


def test_a_permanent_stage_failure_makes_the_run_terminal(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)

    def denied(_lease: RunLease) -> str:
        raise ProviderError("denied", "authorization_denied")

    with pytest.raises(RunStageFailedError) as failed:
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT, run_id=run_id, stages=(RunStage("acquisition_prepared", denied),)
        )

    assert failed.value.completion.failure_classification == "permanent"
    assert failed.value.completion.durable_boundary_ref == "none"
    with pytest.raises(ValueError, match="already terminal"):
        runs.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60)


def test_a_cancelled_run_stops_before_its_next_stage(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    land = _IdempotentEffect("checkpoint")

    def cancelled_during_prepare(_lease: RunLease) -> str:
        runs.cancel(tenant_id=_TENANT, run_id=run_id, cancelled_by="operator-a", reason="stop")
        return "prepared-receipt"

    with pytest.raises(RunLeaseLostError, match="run is cancelled"):
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", cancelled_during_prepare),
                RunStage("land_acknowledged", land),
            ),
        )

    assert land.invocations == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderError("x", "transient_transport"), "transient"),
        (ProviderError("x", "transient_unavailable"), "transient"),
        (ProviderError("x", "throttled"), "transient"),
        (ProviderError("x", "ambiguous_outcome"), "transient"),
        (ProviderError("x", "retryable"), "transient"),
        (ProviderError("x", "ambiguous"), "transient"),
        (AcquisitionTransientError("provider_unavailable"), "transient"),
        (ProviderError("x", "authorization_denied"), "permanent"),
        (ProviderError("x", "integrity_failure"), "permanent"),
        (ValueError("unclassified"), "permanent"),
    ],
)
def test_unclassified_failures_are_permanent_so_they_reach_an_operator(
    error: Exception, expected: str
) -> None:
    assert classify_run_stage_failure(error) == expected


def test_stage_boundary_names_must_be_unique(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    effect = _IdempotentEffect("prepared")

    with pytest.raises(ValueError, match="unique"):
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", effect),
                RunStage("acquisition_prepared", effect),
            ),
        )
    assert effect.invocations == []


def test_a_stage_failure_keeps_its_cause_for_diagnosis(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    bug = TypeError("unexpected payload shape")

    def broken(_lease: RunLease) -> str:
        raise bug

    with pytest.raises(RunStageFailedError) as failed:
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT, run_id=run_id, stages=(RunStage("acquisition_prepared", broken),)
        )

    assert failed.value.__cause__ is bug
    assert failed.value.error_type == "TypeError"


def test_a_stage_failure_found_after_lease_loss_still_carries_the_failure(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    bug = TypeError("unexpected payload shape")

    def slow_then_broken(_lease: RunLease) -> str:
        clock.now += timedelta(seconds=61)
        raise bug

    with pytest.raises(RunLeaseLostError, match="run lease expired") as lost:
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(RunStage("acquisition_prepared", slow_then_broken),),
        )

    assert lost.value.__cause__ is bug


class _ConflictingCompletions(RunService):
    # Mirrors RunService.complete rather than swallowing it in **kwargs: the override
    # is only substitutable if it accepts the same keywords, and the `# type: ignore`
    # that stood here silenced nothing -- **kwargs is already wider.
    def complete(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_number: int,
        epoch: int,
        worker_id: str,
        outcome: Literal["succeeded", "failed"],
        durable_boundary_ref: str,
        failure_classification: Literal["transient", "permanent"] | None = None,
    ) -> RunAttemptCompletion:
        raise ValueError("run completion conflicts with recorded outcome")


def test_a_completion_conflict_is_not_reported_as_benign_lease_loss(tmp_path: Path) -> None:
    clock = _Clock()
    runs = _ConflictingCompletions(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)
    run_id = _runs(tmp_path, clock)[1]

    with pytest.raises(ValueError, match="conflicts with recorded outcome") as raised:
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(RunStage("acquisition_prepared", _IdempotentEffect("prepared")),),
        )

    assert not isinstance(raised.value, RunLeaseLostError)


def test_a_stage_longer_than_its_lease_keeps_the_attempt_by_renewing(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    land = _IdempotentEffect("checkpoint")

    def long_stage(lease: RunLease) -> str:
        for _ in range(3):
            clock.now += timedelta(seconds=45)
            lease.extend()
        return "prepared-receipt"

    outcome = LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
        tenant_id=_TENANT,
        run_id=run_id,
        stages=(
            RunStage("acquisition_prepared", long_stage),
            RunStage("land_acknowledged", land),
        ),
    )

    # Without renewal the 135 seconds of work would have outlived the 60 second lease.
    assert land.effects == [1]
    assert outcome.completion.outcome == "succeeded"
    (snapshot,) = runs.describe_runs(_TENANT)
    assert snapshot.attempts[0].lease_extensions == 3


def test_a_stage_cannot_renew_a_lease_another_worker_has_taken(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)
    land = _IdempotentEffect("checkpoint")

    def superseded_stage(lease: RunLease) -> str:
        clock.now += timedelta(seconds=61)
        runs.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60)
        lease.extend()
        raise AssertionError("a superseded stage must not continue")

    with pytest.raises(RunLeaseLostError, match="stale run epoch"):
        LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(
                RunStage("acquisition_prepared", superseded_stage),
                RunStage("land_acknowledged", land),
            ),
        )

    assert land.invocations == []


def test_an_unavailable_renewal_does_not_permanently_fail_a_live_attempt(tmp_path: Path) -> None:
    clock = _Clock()
    runs, run_id = _runs(tmp_path, clock)

    class _RenewalUnavailable(RunService):
        def extend_lease(
            self, *, tenant_id: str, claim: RunAttemptClaim, lease_seconds: int
        ) -> RunAttemptLeaseExtension:
            raise OSError("state store unavailable")

    unavailable = _RenewalUnavailable(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)

    def renewing_stage(lease: RunLease) -> str:
        lease.extend()
        raise AssertionError("renewal must not be reported as success")

    with pytest.raises(RunStageFailedError) as failed:
        LeasedRunExecutor(unavailable, worker_id="worker-a", lease_seconds=60).execute(
            tenant_id=_TENANT,
            run_id=run_id,
            stages=(RunStage("acquisition_prepared", renewing_stage),),
        )

    # A state-service outage is retryable: the attempt itself was never fenced.
    assert failed.value.completion.failure_classification == "transient"
    assert (
        runs.claim(tenant_id=_TENANT, run_id=run_id, worker_id="worker-b", lease_seconds=60).epoch
        == 2
    )
