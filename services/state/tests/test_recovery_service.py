from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from pillarmesh_state import (
    IncidentRecord,
    RecoveryActionNotAllowedError,
    RecoveryCommand,
    RecoveryCommandService,
    SQLiteIncidentRepository,
    SQLiteRunRepository,
    StaleIncidentRevisionError,
)
from pillarmesh_state.run_models import RunIntent, TriggerWindow
from pillarmesh_state.run_service import RunService

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class _ExternalEffects:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def reconcile_external_effect(self, **command: object) -> str:
        self.calls.append(command)
        return "catalog-publication-a"


class _OwningWorkflowCommands:
    def __init__(self, resulting_record_ref: str) -> None:
        self.resulting_record_ref = resulting_record_ref
        self.calls: list[dict[str, object]] = []

    def execute(self, **command: object) -> str:
        self.calls.append(command)
        return self.resulting_record_ref


def _intent() -> RunIntent:
    return RunIntent(
        tenant_id="tenant-a",
        contract_id="contract-revenue",
        contract_revision=3,
        plan_digest="a" * 64,
        trigger_policy_version="manual-v1",
        trigger_window=TriggerWindow(starts_at=NOW, ends_at=NOW + timedelta(hours=1)),
        reason="scheduled",
    )


def _incident(
    *,
    run_id: str | None,
    run_attempt_number: int | None,
    revision: int = 1,
    kind: str = "source_unavailable",
    classification: str = "transient",
    actions: tuple[str, ...] = ("retry_transient_attempt",),
) -> IncidentRecord:
    return IncidentRecord.model_validate(
        {
            "incident_id": "incident-a",
            "tenant_id": "tenant-a",
            "revision": revision,
            "kind": kind,
            "classification": classification,
            "last_successful_stage": "contract_activation",
            "failed_stage": "extract",
            "user_impact": "The current source interval has not been loaded.",
            "next_automatic_action": (
                "retry_transient_attempt"
                if classification == "transient" and "retry_transient_attempt" in actions
                else None
            ),
            "allowed_operator_actions": actions,
            "source_service": "runtime",
            "source_record_ref": "attempt:run-a:1",
            "run_id": run_id,
            "run_attempt_number": run_attempt_number,
            "evidence_refs": ("evidence:attempt:run-a:1",),
            "opened_at": NOW,
            "updated_at": NOW + timedelta(seconds=revision - 1),
        }
    )


def _command(*, revision: int = 1, action: str = "retry_transient_attempt") -> RecoveryCommand:
    return RecoveryCommand.model_validate(
        {
            "command_id": "recovery-command-a",
            "tenant_id": "tenant-a",
            "incident_id": "incident-a",
            "expected_incident_revision": revision,
            "action": action,
            "actor_id": "architect-a",
            "reason": "Retry after the source recovered.",
        }
    )


def _services(
    tmp_path: Path,
    clock: _Clock | None = None,
) -> tuple[RunService, SQLiteIncidentRepository, RecoveryCommandService]:
    clock = clock or _Clock()
    run_service = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    recovery = RecoveryCommandService(incidents, run_service=run_service, clock=clock)
    return run_service, incidents, recovery


def _failed_attempt(
    run_service: RunService,
    run_id: str,
    *,
    classification: str = "transient",
) -> None:
    claim = run_service.claim(
        tenant_id="tenant-a", run_id=run_id, worker_id="worker-a", lease_seconds=30
    )
    run_service.complete(
        tenant_id="tenant-a",
        run_id=run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="failed",
        failure_classification=cast(Literal["transient", "permanent"], classification),
        durable_boundary_ref="land-receipt-1",
    )


def test_transient_retry_requests_next_attempt_on_the_same_run(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )

    evidence = recovery.execute(_command())
    next_claim = run_service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-b", lease_seconds=30
    )

    assert evidence.resulting_record_ref == f"run-retry:{run.run_id}:1"
    assert next_claim.run_id == run.run_id
    assert next_claim.attempt_number == 2
    assert run_service.list_runs("tenant-a") == (run,)


def test_no_valid_plan_rejects_generic_retry_without_changing_evidence(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    terminal = _incident(
        run_id=None,
        run_attempt_number=None,
        kind="no_valid_plan",
        classification="no_valid_plan",
        actions=("approve_compatible_replan",),
    )
    incidents.append(terminal, expected_current_revision=0)

    with pytest.raises(RecoveryActionNotAllowedError, match="not allowed"):
        recovery.execute(_command())

    assert incidents.load_current("tenant-a", "incident-a") == terminal
    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == ()
    assert run_service.list_runs("tenant-a") == ()


@pytest.mark.parametrize(
    ("action", "resulting_record_ref"),
    (
        ("approve_compatible_replan", "plan:revenue-v2"),
        ("supersede_contract", "contract:revenue:4"),
    ),
)
def test_terminal_recovery_delegates_to_the_exact_owning_workflow(
    tmp_path: Path,
    action: str,
    resulting_record_ref: str,
) -> None:
    clock = _Clock()
    run_service = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    commands = _OwningWorkflowCommands(resulting_record_ref)
    recovery = RecoveryCommandService(
        incidents,
        run_service=run_service,
        clock=clock,
        compatible_replans=commands if action == "approve_compatible_replan" else None,
        contract_supersessions=commands if action == "supersede_contract" else None,
    )
    incident = _incident(
        run_id=None,
        run_attempt_number=None,
        kind="no_valid_plan" if action == "approve_compatible_replan" else "schema_drift",
        classification="no_valid_plan" if action == "approve_compatible_replan" else "permanent",
        actions=(action,),
    ).model_copy(update={"source_record_revision": 3})
    incidents.append(incident, expected_current_revision=0)
    command = _command(action=action)

    evidence = recovery.execute(command)

    assert evidence.resulting_record_ref == resulting_record_ref
    assert commands.calls == [
        {
            "tenant_id": "tenant-a",
            "source_record_ref": incident.source_record_ref,
            "expected_revision": 3,
            "command_id": command.command_id,
            "actor_id": command.actor_id,
            "reason": command.reason,
        }
    ]
    assert run_service.list_runs("tenant-a") == ()


def test_terminal_recovery_without_owning_authority_records_no_evidence(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    incident = _incident(
        run_id=None,
        run_attempt_number=None,
        kind="no_valid_plan",
        classification="no_valid_plan",
        actions=("approve_compatible_replan",),
    ).model_copy(update={"source_record_revision": 3})
    incidents.append(incident, expected_current_revision=0)

    with pytest.raises(RecoveryActionNotAllowedError, match="authority is unavailable"):
        recovery.execute(_command(action="approve_compatible_replan"))

    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == ()
    assert run_service.list_runs("tenant-a") == ()


def test_stale_incident_revision_has_no_run_or_evidence_effect(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1, revision=2),
        expected_current_revision=1,
    )

    with pytest.raises(StaleIncidentRevisionError):
        recovery.execute(_command(revision=1))

    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == ()
    assert run_service.list_retry_requests("tenant-a", run.run_id) == ()


def test_live_run_state_overrides_a_stale_retry_projection(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id, classification="permanent")
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )

    with pytest.raises(RecoveryActionNotAllowedError, match="transient failure"):
        recovery.execute(_command())

    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == ()


def test_retry_rejects_an_incident_for_a_different_attempt(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=2), expected_current_revision=0
    )

    with pytest.raises(RecoveryActionNotAllowedError, match="latest failed attempt"):
        recovery.execute(_command())


def test_cancel_unstarted_work_rejects_a_run_that_has_been_claimed(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    incidents.append(
        _incident(
            run_id=run.run_id,
            run_attempt_number=None,
            actions=("cancel_unstarted_work",),
        ),
        expected_current_revision=0,
    )
    run_service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
    )

    with pytest.raises(RecoveryActionNotAllowedError, match="already started"):
        recovery.execute(_command(action="cancel_unstarted_work"))


def test_cancel_unstarted_work_records_actor_reason_and_blocks_claim(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    incidents.append(
        _incident(
            run_id=run.run_id,
            run_attempt_number=None,
            actions=("cancel_unstarted_work",),
        ),
        expected_current_revision=0,
    )
    command = _command(action="cancel_unstarted_work")

    evidence = recovery.execute(command)

    assert evidence.actor_id == command.actor_id
    assert evidence.reason == command.reason
    with pytest.raises(ValueError, match="cancelled"):
        run_service.claim(
            tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=30
        )


def test_same_recovery_command_replays_exact_evidence(tmp_path: Path) -> None:
    clock = _Clock()
    run_service, incidents, recovery = _services(tmp_path, clock)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )

    first = recovery.execute(_command())
    clock.now += timedelta(minutes=1)
    replay = recovery.execute(_command())

    assert replay == first
    assert len(run_service.list_retry_requests("tenant-a", run.run_id)) == 1
    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == (first,)


def test_replayed_command_cannot_change_the_actor_reason(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )
    recovery.execute(_command())
    conflicting = _command().model_copy(update={"reason": "Use a different reason."})

    with pytest.raises(RecoveryActionNotAllowedError, match="conflicts"):
        recovery.execute(conflicting)


def test_retry_converges_when_evidence_write_fails_after_run_request(tmp_path: Path) -> None:
    run_path = tmp_path / "runs.sqlite3"
    incident_path = tmp_path / "incidents.sqlite3"
    clock = _Clock()
    run_service = RunService(SQLiteRunRepository(run_path), clock=clock)
    incidents = SQLiteIncidentRepository(incident_path)
    recovery = RecoveryCommandService(incidents, run_service=run_service, clock=clock)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )
    with sqlite3.connect(incident_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_recovery_evidence "
            "BEFORE INSERT ON incident_recovery_evidence "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(RuntimeError, match="append recovery evidence"):
        recovery.execute(_command())
    assert len(run_service.list_retry_requests("tenant-a", run.run_id)) == 1
    with sqlite3.connect(incident_path) as connection:
        connection.execute("DROP TRIGGER reject_recovery_evidence")

    evidence = recovery.execute(_command())

    assert evidence.command_id == "recovery-command-a"
    assert len(run_service.list_retry_requests("tenant-a", run.run_id)) == 1


def test_cross_tenant_command_is_non_enumerating(tmp_path: Path) -> None:
    run_service, incidents, recovery = _services(tmp_path)
    run = run_service.materialize(_intent())
    _failed_attempt(run_service, run.run_id)
    incidents.append(
        _incident(run_id=run.run_id, run_attempt_number=1), expected_current_revision=0
    )
    foreign = _command().model_copy(update={"tenant_id": "tenant-b"})

    with pytest.raises(LookupError, match="incident is unavailable"):
        recovery.execute(foreign)


def test_catalog_reconciliation_delegates_exact_owner_revision_and_replays_evidence(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    run_service = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=clock)
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    external_effects = _ExternalEffects()
    recovery = RecoveryCommandService(
        incidents,
        run_service=run_service,
        clock=clock,
        external_effects=external_effects,
    )
    pending = IncidentRecord.model_validate(
        _incident(run_id="placeholder", run_attempt_number=1).model_dump()
        | {
            "run_id": None,
            "run_attempt_number": None,
            "kind": "catalog_pending",
            "classification": "ambiguous_outcome",
            "allowed_operator_actions": ("reconcile_external_effect",),
            "next_automatic_action": "reconcile_external_effect",
            "source_record_ref": "a" * 64,
            "source_record_revision": 7,
            "failed_stage": "catalog_publication",
        }
    )
    incidents.append(pending, expected_current_revision=0)
    command = _command(action="reconcile_external_effect")

    first = recovery.execute(command)
    replay = recovery.execute(command)

    assert replay == first
    assert first.resulting_record_ref == "catalog-publication-a"
    assert external_effects.calls == [
        {
            "tenant_id": "tenant-a",
            "source_record_ref": "a" * 64,
            "expected_revision": 7,
            "command_id": "recovery-command-a",
            "actor_id": "architect-a",
            "reason": "Retry after the source recovered.",
        }
    ]


def test_catalog_reconciliation_is_denied_without_owner_command_wiring(tmp_path: Path) -> None:
    _, incidents, recovery = _services(tmp_path)
    pending = IncidentRecord.model_validate(
        _incident(run_id="placeholder", run_attempt_number=1).model_dump()
        | {
            "run_id": None,
            "run_attempt_number": None,
            "kind": "catalog_pending",
            "classification": "ambiguous_outcome",
            "allowed_operator_actions": ("reconcile_external_effect",),
            "next_automatic_action": "reconcile_external_effect",
            "source_record_ref": "a" * 64,
            "source_record_revision": 1,
            "failed_stage": "catalog_publication",
        }
    )
    incidents.append(pending, expected_current_revision=0)

    with pytest.raises(RecoveryActionNotAllowedError) as raised:
        recovery.execute(_command(action="reconcile_external_effect"))

    assert str(raised.value) == "external effect recovery authority is unavailable"
    assert incidents.list_recovery_evidence("tenant-a", "incident-a") == ()
