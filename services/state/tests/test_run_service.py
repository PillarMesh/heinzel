from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pillarmesh_state.run_models import RunIntent, TriggerWindow
from pillarmesh_state.run_repository import SQLiteRunRepository
from pillarmesh_state.run_service import RunService

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _intent() -> RunIntent:
    return RunIntent(
        tenant_id="tenant-a",
        contract_id="contract-revenue",
        contract_revision=3,
        plan_digest="a" * 64,
        trigger_policy_version="manual-v1",
        trigger_window=TriggerWindow(
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
        ),
        reason="scheduled",
    )


def _service(path: Path, clock: _Clock) -> RunService:
    return RunService(SQLiteRunRepository(path), clock=clock)


def test_canonical_intent_replay_returns_one_run(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())

    first = service.materialize(_intent())
    second = service.materialize(_intent())

    assert first == second
    assert service.list_runs("tenant-a") == (first,)


def test_changed_authority_has_a_distinct_run_identity(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())

    first = service.materialize(_intent())
    second = service.materialize(_intent().model_copy(update={"plan_digest": "b" * 64}))

    assert first.run_id != second.run_id


def test_only_one_worker_claims_an_active_run(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "runs.sqlite3"
    first_service = _service(path, clock)
    second_service = _service(path, clock)
    run = first_service.materialize(_intent())

    claim = first_service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-a",
        lease_seconds=30,
    )

    with pytest.raises(ValueError, match="already leased"):
        second_service.claim(
            tenant_id="tenant-a",
            run_id=run.run_id,
            worker_id="worker-b",
            lease_seconds=30,
        )
    assert claim.epoch == 1
    assert claim.attempt_number == 1


def test_expired_lease_creates_a_new_attempt_and_fences_old_worker(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    old = service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-a",
        lease_seconds=30,
    )
    clock.now += timedelta(seconds=31)

    current = service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-b",
        lease_seconds=30,
    )

    assert current.attempt_number == 2
    assert current.epoch == 2
    with pytest.raises(ValueError, match="stale run epoch"):
        service.complete(
            tenant_id="tenant-a",
            run_id=run.run_id,
            attempt_number=old.attempt_number,
            epoch=old.epoch,
            worker_id="worker-a",
            outcome="succeeded",
            durable_boundary_ref="materialization-receipt-1",
        )


def test_completion_is_replay_stable_and_rejects_conflict(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-a",
        lease_seconds=30,
    )

    first = service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id="worker-a",
        outcome="succeeded",
        durable_boundary_ref="materialization-receipt-1",
    )
    replay = service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id="worker-a",
        outcome="succeeded",
        durable_boundary_ref="materialization-receipt-1",
    )

    assert replay == first
    with pytest.raises(ValueError, match="completion conflicts"):
        service.complete(
            tenant_id="tenant-a",
            run_id=run.run_id,
            attempt_number=claim.attempt_number,
            epoch=claim.epoch,
            worker_id="worker-a",
            outcome="failed",
            failure_classification="transient",
            durable_boundary_ref="materialization-receipt-1",
        )


def test_cross_tenant_run_access_is_non_enumerating(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())
    run = service.materialize(_intent())

    with pytest.raises(KeyError, match="run is unavailable"):
        service.claim(
            tenant_id="tenant-b",
            run_id=run.run_id,
            worker_id="worker-b",
            lease_seconds=30,
        )


def test_cancel_is_replay_stable_and_prevents_a_worker_claim(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())
    run = service.materialize(_intent())

    first = service.cancel(
        tenant_id="tenant-a",
        run_id=run.run_id,
        cancelled_by="architect-a",
        reason="source contract superseded",
    )
    replay = service.cancel(
        tenant_id="tenant-a",
        run_id=run.run_id,
        cancelled_by="architect-a",
        reason="source contract superseded",
    )

    assert replay == first
    with pytest.raises(ValueError, match="cancelled"):
        service.claim(
            tenant_id="tenant-a",
            run_id=run.run_id,
            worker_id="worker-a",
            lease_seconds=30,
        )


def test_cancel_rejects_cross_tenant_access_and_conflicting_replay(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())
    run = service.materialize(_intent())

    with pytest.raises(KeyError, match="run is unavailable"):
        service.cancel(
            tenant_id="tenant-b",
            run_id=run.run_id,
            cancelled_by="architect-b",
            reason="cancel",
        )
    service.cancel(
        tenant_id="tenant-a",
        run_id=run.run_id,
        cancelled_by="architect-a",
        reason="cancel",
    )
    with pytest.raises(ValueError, match="cancellation conflicts"):
        service.cancel(
            tenant_id="tenant-a",
            run_id=run.run_id,
            cancelled_by="architect-a",
            reason="different reason",
        )


def test_active_worker_cannot_complete_after_operator_cancellation(tmp_path: Path) -> None:
    service = _service(tmp_path / "runs.sqlite3", _Clock())
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-a",
        lease_seconds=30,
    )
    service.cancel(
        tenant_id="tenant-a",
        run_id=run.run_id,
        cancelled_by="architect-a",
        reason="operator stopped run",
    )

    with pytest.raises(ValueError, match="cancelled"):
        service.complete(
            tenant_id="tenant-a",
            run_id=run.run_id,
            attempt_number=claim.attempt_number,
            epoch=claim.epoch,
            worker_id="worker-a",
            outcome="succeeded",
            durable_boundary_ref="land-receipt-1",
        )


@pytest.mark.parametrize(
    ("classification", "retry_allowed"),
    (("transient", True), ("permanent", False)),
)
def test_only_transient_failure_allows_a_new_attempt(
    tmp_path: Path,
    classification: Literal["transient", "permanent"],
    retry_allowed: bool,
) -> None:
    service = _service(tmp_path / f"{classification}.sqlite3", _Clock())
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a",
        run_id=run.run_id,
        worker_id="worker-a",
        lease_seconds=30,
    )
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id="worker-a",
        outcome="failed",
        failure_classification=classification,
        durable_boundary_ref="land-receipt-1",
    )

    if retry_allowed:
        retry = service.claim(
            tenant_id="tenant-a",
            run_id=run.run_id,
            worker_id="worker-b",
            lease_seconds=30,
        )
        assert retry.attempt_number == 2
    else:
        with pytest.raises(ValueError, match="already terminal"):
            service.claim(
                tenant_id="tenant-a",
                run_id=run.run_id,
                worker_id="worker-b",
                lease_seconds=30,
            )


def test_current_attempt_check_passes_only_for_the_live_lease_holder(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    old = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )

    service.require_current_attempt(tenant_id="tenant-a", claim=old)
    clock.now += timedelta(seconds=30)
    with pytest.raises(ValueError, match="run lease expired"):
        service.require_current_attempt(tenant_id="tenant-a", claim=old)

    current = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30
    )
    with pytest.raises(ValueError, match="stale run epoch"):
        service.require_current_attempt(tenant_id="tenant-a", claim=old)
    service.require_current_attempt(tenant_id="tenant-a", claim=current)
    with pytest.raises(KeyError):
        service.require_current_attempt(tenant_id="tenant-b", claim=current)


def test_current_attempt_check_refuses_completed_and_cancelled_attempts(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="failed",
        failure_classification="transient",
        durable_boundary_ref="none",
    )
    with pytest.raises(ValueError, match="run attempt is already complete"):
        service.require_current_attempt(tenant_id="tenant-a", claim=claim)

    retry = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30
    )
    service.cancel(
        tenant_id="tenant-a", run_id=run.run_id, cancelled_by="operator-a", reason="stop"
    )
    with pytest.raises(ValueError, match="run is cancelled"):
        service.require_current_attempt(tenant_id="tenant-a", claim=retry)


def test_lifecycle_snapshot_projects_every_attempt_and_its_last_durable_boundary(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    (pending,) = service.describe_runs("tenant-a")
    assert (pending.status, pending.attempts, pending.last_durable_boundary_ref) == (
        "pending",
        (),
        None,
    )

    first = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )
    assert service.describe_runs("tenant-a")[0].status == "leased"
    clock.now += timedelta(seconds=30)
    assert service.describe_runs("tenant-a")[0].status == "lease_expired"

    second = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30
    )
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=second.attempt_number,
        epoch=second.epoch,
        worker_id=second.worker_id,
        outcome="failed",
        failure_classification="transient",
        durable_boundary_ref="acquisition_prepared:prepared-1",
    )
    retryable = service.describe_runs("tenant-a")[0]
    assert retryable.status == "retryable"
    assert retryable.last_durable_boundary_ref == "acquisition_prepared:prepared-1"
    assert [(item.claim, item.completion is None) for item in retryable.attempts] == [
        (first, True),
        (second, False),
    ]

    third = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-c", lease_seconds=30
    )
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=third.attempt_number,
        epoch=third.epoch,
        worker_id=third.worker_id,
        outcome="succeeded",
        durable_boundary_ref="land_acknowledged:checkpoint-1",
    )
    (succeeded,) = service.describe_runs("tenant-a")
    assert succeeded.run == run
    assert succeeded.status == "succeeded"
    assert [item.claim.epoch for item in succeeded.attempts] == [1, 2, 3]
    assert succeeded.last_durable_boundary_ref == "land_acknowledged:checkpoint-1"
    assert service.describe_runs("tenant-b") == ()


def test_lifecycle_snapshot_reports_permanent_failure_and_cancellation(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    failed_run = service.materialize(_intent())
    cancelled_run = service.materialize(_intent().model_copy(update={"plan_digest": "b" * 64}))
    claim = service.claim(
        tenant_id="tenant-a", run_id=failed_run.run_id, worker_id="worker-a", lease_seconds=30
    )
    service.complete(
        tenant_id="tenant-a",
        run_id=failed_run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="failed",
        failure_classification="permanent",
        durable_boundary_ref="none",
    )
    service.cancel(
        tenant_id="tenant-a", run_id=cancelled_run.run_id, cancelled_by="operator-a", reason="stop"
    )

    statuses = {item.run.run_id: item for item in service.describe_runs("tenant-a")}

    assert statuses[failed_run.run_id].status == "failed"
    assert statuses[cancelled_run.run_id].status == "cancelled"
    assert statuses[cancelled_run.run_id].cancellation is not None


def test_describing_runs_does_not_wait_on_a_worker_holding_the_write_lock(tmp_path: Path) -> None:
    import sqlite3
    import time

    path = tmp_path / "runs.sqlite3"
    service = _service(path, _Clock())
    service.materialize(_intent())
    worker = sqlite3.connect(path, isolation_level=None)
    worker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        (snapshot,) = service.describe_runs("tenant-a")
        elapsed = time.monotonic() - started
    finally:
        worker.execute("ROLLBACK")
        worker.close()

    assert snapshot.status == "pending"
    assert elapsed < 1


def test_a_live_lease_can_be_extended_so_a_long_stage_keeps_its_attempt(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "runs.sqlite3"
    service = _service(path, clock)
    other = _service(path, clock)
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )

    clock.now += timedelta(seconds=20)
    extension = service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=30)

    assert extension.attempt_number == claim.attempt_number
    assert extension.epoch == claim.epoch
    assert extension.extension_number == 1
    assert extension.lease_expires_at == clock.now + timedelta(seconds=30)
    # Past the original expiry, the attempt is still the live one and no one else may claim.
    clock.now += timedelta(seconds=20)
    service.require_current_attempt(tenant_id="tenant-a", claim=claim)
    with pytest.raises(ValueError, match="already leased"):
        other.claim(tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30)
    assert service.describe_runs("tenant-a")[0].status == "leased"
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="succeeded",
        durable_boundary_ref="land_acknowledged:checkpoint-1",
    )


def test_an_extended_lease_still_expires_and_fences_its_worker(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )
    service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=60)

    clock.now += timedelta(seconds=61)

    with pytest.raises(ValueError, match="run lease expired"):
        service.require_current_attempt(tenant_id="tenant-a", claim=claim)
    assert service.describe_runs("tenant-a")[0].status == "lease_expired"
    successor = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30
    )
    assert successor.epoch == 2
    with pytest.raises(ValueError, match="stale run epoch"):
        service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=30)


def test_an_extension_never_shortens_a_lease_or_revives_a_finished_attempt(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=60
    )

    with pytest.raises(ValueError, match="lease extension must extend the lease"):
        service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=30)

    second = service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=90)
    assert second.extension_number == 1
    service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="failed",
        failure_classification="transient",
        durable_boundary_ref="none",
    )
    with pytest.raises(ValueError, match="run attempt is already complete"):
        service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=120)


def test_lease_extensions_are_projected_with_the_attempt(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "runs.sqlite3", clock)
    run = service.materialize(_intent())
    claim = service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )
    service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=60)
    service.extend_lease(tenant_id="tenant-a", claim=claim, lease_seconds=120)

    (snapshot,) = service.describe_runs("tenant-a")
    (attempt,) = snapshot.attempts

    assert attempt.claim.lease_expires_at == NOW + timedelta(seconds=30)
    assert attempt.lease_expires_at == NOW + timedelta(seconds=120)
    assert attempt.lease_extensions == 2
