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
