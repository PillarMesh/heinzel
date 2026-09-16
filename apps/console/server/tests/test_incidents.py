from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_console import create_app
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.contracts import ActorRole, IncidentRecoveryCommand
from pillarmesh_console.errors import ConsoleNotFound
from pillarmesh_console.fixture_backend import FixtureConsoleBackend
from pillarmesh_console.governed_adapters import GovernedWorkspaceIdentity
from pillarmesh_console.governed_backend import GovernedConsoleBackend
from pillarmesh_console.operation_handles import InMemoryOperationHandleRepository
from pillarmesh_state import (
    IncidentRecord,
    RecoveryCommandService,
    RunIntent,
    RunService,
    SQLiteIncidentRepository,
    SQLiteRunRepository,
    TriggerWindow,
)
from starlette.testclient import TestClient

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


class _ExternalEffects:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def reconcile_external_effect(self, **command: object) -> str:
        self.calls.append(command)
        return "catalog-publication-a"


def _context(
    *, tenant_id: str = "tenant-a", role: ActorRole = "data_architect"
) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=tenant_id,
        actor_id="architect-a",
        roles=(role,),
        active_role=role,
        session_id="session-a",
    )


def _stack(
    tmp_path: Path, *, external_effects: _ExternalEffects | None = None
) -> tuple[GovernedConsoleBackend, SQLiteIncidentRepository, RunService]:
    run_service = RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=lambda: NOW)
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    recovery = RecoveryCommandService(
        incidents,
        run_service=run_service,
        clock=lambda: NOW,
        external_effects=external_effects,
    )
    backend = GovernedConsoleBackend(
        identity=GovernedWorkspaceIdentity(
            tenant_ref="tenant-a",
            tenant_display_name="Tenant A",
            workspace_ref="workspace-a",
            workspace_display_name="Tenant A workspace",
        ),
        operation_handles=InMemoryOperationHandleRepository(),
        incidents=incidents,
        incident_recovery_commands=recovery,
    )
    return backend, incidents, run_service


def _incident(*, run_id: str, incident_id: str = "incident-a") -> IncidentRecord:
    return IncidentRecord(
        incident_id=incident_id,
        tenant_id="tenant-a",
        revision=1,
        kind="source_unavailable",
        classification="transient",
        last_successful_stage="contract_activation",
        failed_stage="extract",
        user_impact="The latest source interval is not available yet.",
        next_automatic_action="retry_transient_attempt",
        allowed_operator_actions=("retry_transient_attempt",),
        source_service="runtime",
        source_record_ref="attempt:private",
        run_id=run_id,
        run_attempt_number=1,
        evidence_refs=("evidence:private",),
        opened_at=NOW,
        updated_at=NOW,
    )


def test_workspace_reports_state_owned_operation_recovery_only_when_fully_wired(
    tmp_path: Path,
) -> None:
    wired, incidents, _ = _stack(tmp_path)
    reader_only = GovernedConsoleBackend(
        identity=GovernedWorkspaceIdentity(
            tenant_ref="tenant-a",
            tenant_display_name="Tenant A",
            workspace_ref="workspace-a",
            workspace_display_name="Tenant A workspace",
        ),
        operation_handles=InMemoryOperationHandleRepository(),
        incidents=incidents,
    )

    wired_capability = next(
        item
        for item in wired.get_workspace(_context()).capabilities
        if item.capability_id == "operation-retry"
    )
    reader_only_capability = next(
        item
        for item in reader_only.get_workspace(_context()).capabilities
        if item.capability_id == "operation-retry"
    )

    assert wired_capability.state == "ready"
    assert wired_capability.label == "Operation recovery"
    assert wired_capability.dependency is None
    assert reader_only_capability.state == "not_delivered"
    assert reader_only_capability.dependency is not None


def _failed_run(run_service: RunService, *, marker: str = "a") -> str:
    run = run_service.materialize(
        RunIntent(
            tenant_id="tenant-a",
            contract_id="contract-a",
            contract_revision=1,
            plan_digest=marker * 64,
            trigger_policy_version="manual-v1",
            trigger_window=TriggerWindow(starts_at=NOW, ends_at=NOW + timedelta(hours=1)),
            reason="run_now",
        )
    )
    claim = run_service.claim(
        tenant_id="tenant-a", run_id=run.run_id, worker_id="worker-a", lease_seconds=60
    )
    run_service.complete(
        tenant_id="tenant-a",
        run_id=run.run_id,
        attempt_number=claim.attempt_number,
        epoch=claim.epoch,
        worker_id=claim.worker_id,
        outcome="failed",
        failure_classification="transient",
        durable_boundary_ref="boundary-a",
    )
    return run.run_id


def test_incident_projection_exposes_operational_context_without_private_references(
    tmp_path: Path,
) -> None:
    backend, incidents, run_service = _stack(tmp_path)
    incidents.append(_incident(run_id=_failed_run(run_service)), expected_current_revision=0)

    projected = backend.get_incidents(_context()).incidents[0]

    assert projected.failed_stage == "extract"
    assert projected.incident_id != "incident-a"
    assert projected.incident_id.startswith("op_")
    assert projected.allowed_operator_actions == ("retry_transient_attempt",)
    assert set(projected.model_dump()) == {
        "incident_id",
        "revision",
        "kind",
        "classification",
        "last_successful_stage",
        "failed_stage",
        "user_impact",
        "next_automatic_action",
        "allowed_operator_actions",
        "opened_at",
        "updated_at",
        "recovery_recorded",
    }
    with pytest.raises(ConsoleNotFound):
        backend.get_operation(_context(), projected.incident_id)


def test_recovery_uses_trusted_actor_and_suppresses_repeated_action(tmp_path: Path) -> None:
    backend, incidents, run_service = _stack(tmp_path)
    run_id = _failed_run(run_service)
    incidents.append(_incident(run_id=run_id), expected_current_revision=0)
    incident_ref = backend.get_incidents(_context()).incidents[0].incident_id

    recovered = backend.recover_incident(
        _context(),
        incident_ref,
        IncidentRecoveryCommand(
            expected_revision=1,
            action="retry_transient_attempt",
            active_role="data_architect",
            reason="The source is healthy again.",
        ),
        idempotency_key="idempotency-recovery-a",
    )

    evidence = incidents.list_recovery_evidence("tenant-a", "incident-a")
    assert evidence[0].actor_id == "architect-a"
    assert evidence[0].reason == "The source is healthy again."
    assert evidence[0].command_id != "idempotency-recovery-a"
    assert recovered.recovery_recorded is True
    assert recovered.allowed_operator_actions == ()
    assert recovered.next_automatic_action is None


def test_projection_never_exposes_undelivered_owner_actions(tmp_path: Path) -> None:
    backend, incidents, _ = _stack(tmp_path)
    terminal = IncidentRecord.model_validate(
        _incident(run_id="unused-run").model_dump()
        | {
            "kind": "no_valid_plan",
            "classification": "no_valid_plan",
            "next_automatic_action": None,
            "allowed_operator_actions": ("approve_compatible_replan",),
            "run_id": None,
            "run_attempt_number": None,
        }
    )
    incidents.append(terminal, expected_current_revision=0)

    projected = backend.get_incidents(_context()).incidents[0]

    assert projected.allowed_operator_actions == ()


def test_catalog_pending_projects_and_executes_owner_reconciliation(tmp_path: Path) -> None:
    external_effects = _ExternalEffects()
    backend, incidents, _ = _stack(tmp_path, external_effects=external_effects)
    pending = IncidentRecord.model_validate(
        _incident(run_id="placeholder").model_dump()
        | {
            "kind": "catalog_pending",
            "classification": "ambiguous_outcome",
            "last_successful_stage": "transform",
            "failed_stage": "catalog_publication",
            "next_automatic_action": "reconcile_external_effect",
            "allowed_operator_actions": ("reconcile_external_effect",),
            "source_record_ref": "a" * 64,
            "source_record_revision": 4,
            "run_id": None,
            "run_attempt_number": None,
        }
    )
    incidents.append(pending, expected_current_revision=0)
    projected = backend.get_incidents(_context()).incidents[0]

    recovered = backend.recover_incident(
        _context(),
        projected.incident_id,
        IncidentRecoveryCommand(
            expected_revision=projected.revision,
            action="reconcile_external_effect",
            active_role="data_architect",
            reason="The catalog is healthy again.",
        ),
        idempotency_key="catalog-recovery-a",
    )

    assert projected.allowed_operator_actions == ("reconcile_external_effect",)
    assert recovered.recovery_recorded is True
    assert len(external_effects.calls) == 1


def test_same_http_idempotency_key_cannot_cross_replay_between_incidents(tmp_path: Path) -> None:
    backend, incidents, run_service = _stack(tmp_path)
    incidents.append(
        _incident(run_id=_failed_run(run_service), incident_id="incident-a"),
        expected_current_revision=0,
    )
    incidents.append(
        _incident(
            run_id=_failed_run(run_service, marker="b"),
            incident_id="incident-b",
        ),
        expected_current_revision=0,
    )
    first_ref, second_ref = (
        incident.incident_id for incident in backend.get_incidents(_context()).incidents
    )
    command = IncidentRecoveryCommand(
        expected_revision=1,
        action="retry_transient_attempt",
        active_role="data_architect",
        reason="Source access restored.",
    )

    backend.recover_incident(
        _context(), first_ref, command, idempotency_key="shared-http-idempotency-key"
    )
    backend.recover_incident(
        _context(), second_ref, command, idempotency_key="shared-http-idempotency-key"
    )

    first_evidence = incidents.list_recovery_evidence("tenant-a", "incident-a")[0]
    second_evidence = incidents.list_recovery_evidence("tenant-a", "incident-b")[0]
    assert first_evidence.command_id != second_evidence.command_id


def test_cross_tenant_incident_read_is_non_enumerating(tmp_path: Path) -> None:
    backend, incidents, run_service = _stack(tmp_path)
    incidents.append(_incident(run_id=_failed_run(run_service)), expected_current_revision=0)
    incident_ref = backend.get_incidents(_context()).incidents[0].incident_id

    with pytest.raises(ConsoleNotFound):
        backend.recover_incident(
            _context(tenant_id="tenant-b"),
            incident_ref,
            IncidentRecoveryCommand(
                expected_revision=1,
                action="retry_transient_attempt",
                active_role="data_architect",
                reason="The source is healthy again.",
            ),
            idempotency_key="idempotency-recovery-foreign",
        )


def test_fixture_incident_routes_are_empty_and_non_enumerating() -> None:
    with TestClient(
        create_app(
            backend=FixtureConsoleBackend(),
            context_provider=lambda _: _context(tenant_id="tenant-primary"),
            allowed_origin="http://testserver",
        )
    ) as client:
        listing = client.get("/api/v1/incidents")
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        unavailable = client.post(
            "/api/v1/incidents/incident-a/recovery",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": token,
                "Idempotency-Key": "idempotency-incident-route",
            },
            json={
                "expected_revision": 1,
                "action": "retry_transient_attempt",
                "active_role": "data_architect",
                "reason": "The source is healthy again.",
            },
        )
        blank_reason = client.post(
            "/api/v1/incidents/incident-a/recovery",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": token,
                "Idempotency-Key": "idempotency-incident-blank",
            },
            json={
                "expected_revision": 1,
                "action": "retry_transient_attempt",
                "active_role": "data_architect",
                "reason": "   ",
            },
        )

    assert listing.status_code == 200
    assert listing.json()["data"]["incidents"] == []
    assert unavailable.status_code == 404
    assert blank_reason.status_code == 422
    assert blank_reason.json()["error"]["field"] == "reason"


def test_incident_routes_are_hidden_from_requesters(tmp_path: Path) -> None:
    backend, _, _ = _stack(tmp_path)
    with TestClient(
        create_app(
            backend=backend,
            context_provider=lambda _: _context(role="requester"),
            allowed_origin="http://testserver",
        )
    ) as client:
        response = client.get("/api/v1/incidents")

    assert response.status_code == 404
