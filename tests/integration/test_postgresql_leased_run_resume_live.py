"""A due run crashes after a durable boundary, loses its lease, and a successor resumes it.

The acceptance ledger's run-lifecycle row recorded that state owns run identity, leases and epochs,
and each runner resumes from its own durable boundaries, but no runtime work executed under a
state-owned run. This journey closes that seam on the digest-pinned PostgreSQL 18.6 image.

A daily trigger materializes one state-owned run for an activated contract. `LeasedRunExecutor`
claims it and runs two replay-stable stages: composed acquisition preparation, then LAND with
checkpoint acknowledgement. The first worker proves the preparation boundary, then stalls past its
lease before LAND. A second worker claims epoch 2, replays preparation without reaching the source,
lands once and commits the checkpoint. The stalled worker can neither start LAND nor record an
outcome, and the completed run cannot be claimed again.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from pillarmesh_runtime import (
    ActivatedAcquisitionRunContracts,
    GenerationLedger,
    LeasedRunExecutor,
    RunLease,
    RunLeaseLostError,
    RunStage,
)
from pillarmesh_state import RunAttemptClaim, RunService
from pillarmesh_state.run_repository import SQLiteRunRepository
from pillarmesh_trigger import DailyTriggerPolicy, TriggerRunService

from tests.acceptance.run_plan4a import _MutableClock
from tests.integration.test_postgresql_acquisition_land_live import (
    _BINDING_REF,
    _CONTRACT_REF,
    _NOW,
    _TENANT,
    _TRIGGER_WINDOW,
    _composed_acquisition,
    _DestinationBindings,
    _intent,
    _land_receipt_count,
    _landing,
    _raw_rows,
)
from tests.integration.test_postgresql_checked_sum_evidence import _pinned_postgresql
from tests.integration.test_postgresql_product_materialization_live import (
    _provision_cluster,
    _role_dsn,
)

_RUN_LIVE = os.environ.get("PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_LEASE_SECONDS = 60
_DAILY = DailyTriggerPolicy(policy_version="daily-v1", hour_utc=0, minute_utc=0)


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_a_due_run_resumes_from_its_durable_boundary_after_lease_loss(tmp_path: Path) -> None:
    acquisition_clock = _MutableClock(_NOW)
    run_clock = _MutableClock(_NOW)
    with _pinned_postgresql() as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        landing_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=landing_password,
            materialization_password=secrets.token_urlsafe(24),
        )
        with _composed_acquisition(
            tmp_path,
            _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password),
            acquisition_clock,
        ) as composed:
            runs = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=run_clock)
            triggers = TriggerRunService(
                ActivatedAcquisitionRunContracts(composed.lifecycles), runs
            )
            # Observed a day after the landed window's start, the daily policy's due window is the
            # one the acquisition contract runs for.
            observed_at = _NOW + timedelta(days=1)
            run = triggers.materialize_daily(
                tenant_id=_TENANT,
                contract_ref=_CONTRACT_REF,
                contract_revision=composed.record.revision,
                policy=_DAILY,
                observed_at=observed_at,
            )
            duplicate = triggers.materialize_daily(
                tenant_id=_TENANT,
                contract_ref=_CONTRACT_REF,
                contract_revision=composed.record.revision,
                policy=_DAILY,
                observed_at=observed_at + timedelta(hours=1),
            )
            window = run.intent.trigger_window
            trigger_window = f"{window.starts_at:%Y-%m-%dT%H:%M:%SZ}/P1D"

            destination_bindings = _DestinationBindings()
            ledger = GenerationLedger.in_memory()
            landing_dsn = _role_dsn(bootstrap_dsn, "landing_runtime", landing_password)
            land_application = _landing(
                composed, landing_dsn, destination_bindings, ledger, acquisition_clock
            )
            preparations: list[int] = []

            def prepare(lease: RunLease) -> str:
                preparations.append(lease.claim.epoch)
                preparation = composed.application.run_now(
                    tenant_id=_TENANT,
                    contract_ref=_CONTRACT_REF,
                    trigger_window=trigger_window,
                    acquisition_mode="snapshot",
                )
                assert preparation.prepared_receipt is not None
                return preparation.prepared_receipt.prepared_receipt_id

            def land(_lease: RunLease) -> str:
                preparation = composed.application.run_now(
                    tenant_id=_TENANT,
                    contract_ref=_CONTRACT_REF,
                    trigger_window=trigger_window,
                    acquisition_mode="snapshot",
                )
                landed = asyncio.run(
                    land_application.land(
                        trigger_window=trigger_window,
                        intent=_intent(composed, preparation),
                        preparation=preparation,
                    )
                )
                return landed.checkpoint_receipt.checkpoint_receipt_id

            def prepare_then_crash(lease: RunLease) -> str:
                receipt = prepare(lease)
                run_clock.value += timedelta(seconds=_LEASE_SECONDS + 1)
                return receipt

            stalled_claims: list[RunAttemptClaim] = []

            def record_claim(lease: RunLease) -> str:
                stalled_claims.append(lease.claim)
                return prepare_then_crash(lease)

            with pytest.raises(RunLeaseLostError, match="run lease expired") as lost:
                LeasedRunExecutor(runs, worker_id="worker-a", lease_seconds=_LEASE_SECONDS).execute(
                    tenant_id=_TENANT,
                    run_id=run.run_id,
                    stages=(
                        RunStage("acquisition_prepared", record_claim),
                        RunStage("land_acknowledged", land),
                    ),
                )
            rows_after_crash = _raw_rows(bootstrap_dsn)
            evidence_after_crash = composed.evidence.list_acquisition_receipts(_TENANT)
            # A source row committed after the proven preparation. Replay must not see it: the
            # successor lands the batch the first attempt prepared, not a fresh read.
            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute(
                    "INSERT INTO source_data.sales VALUES (4, 'north', 104, 7.00, %s)",
                    (_NOW,),
                )

            resumed = LeasedRunExecutor(
                runs, worker_id="worker-b", lease_seconds=_LEASE_SECONDS
            ).execute(
                tenant_id=_TENANT,
                run_id=run.run_id,
                stages=(
                    RunStage("acquisition_prepared", prepare),
                    RunStage("land_acknowledged", land),
                ),
            )
            rows_after_resume = _raw_rows(bootstrap_dsn)
            receipts_after_resume = _land_receipt_count(bootstrap_dsn)
            checkpoint = composed.state.load_checkpoint(
                _TENANT, composed.record.contract.contract_digest, _BINDING_REF
            )
            evidence_after_resume = composed.evidence.list_acquisition_receipts(_TENANT)

            stale = stalled_claims[0]
            with pytest.raises(ValueError, match="stale run epoch"):
                runs.complete(
                    tenant_id=_TENANT,
                    run_id=run.run_id,
                    attempt_number=stale.attempt_number,
                    epoch=stale.epoch,
                    worker_id=stale.worker_id,
                    outcome="succeeded",
                    durable_boundary_ref="acquisition_prepared:forged",
                )
            with pytest.raises(ValueError, match="already terminal"):
                runs.claim(
                    tenant_id=_TENANT,
                    run_id=run.run_id,
                    worker_id="worker-c",
                    lease_seconds=_LEASE_SECONDS,
                )

    # One run per canonical due intent, for exactly the window the contract acquires.
    assert duplicate == run
    assert trigger_window == _TRIGGER_WINDOW

    # The crash left a proven preparation and nothing landed.
    assert str(lost.value) == "run lease expired"
    assert rows_after_crash == []
    assert [receipt.outcome for receipt in evidence_after_crash] == ["prepared"]

    # The successor resumed under a new epoch and replayed preparation without a second source read.
    assert (resumed.claim.attempt_number, resumed.claim.epoch) == (2, 2)
    assert preparations == [1, 2]
    assert [boundary for boundary, _ in resumed.boundaries] == [
        "acquisition_prepared",
        "land_acknowledged",
    ]
    assert resumed.boundaries[0][1] == evidence_after_crash[0].prepared_receipt_ref
    assert resumed.completion.durable_boundary_ref == (
        f"land_acknowledged:{resumed.boundaries[1][1]}"
    )
    # Three rows, not four: the row inserted after the crash was never read.
    assert len(rows_after_resume) == 3
    assert receipts_after_resume == 1
    assert checkpoint.revision == 1
    assert sorted(receipt.outcome for receipt in evidence_after_resume) == [
        "acknowledged",
        "prepared",
        "prepared",
        "prepared",
    ]
