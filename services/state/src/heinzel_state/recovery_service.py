from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .incident_models import RecoveryActionEvidence, RecoveryCommand
from .incident_repository import SQLiteIncidentRepository, StaleIncidentRevisionError
from .run_service import RunService


class RecoveryActionNotAllowedError(ValueError):
    def __init__(self, reason: str = "recovery action is not allowed") -> None:
        super().__init__(reason)


class ExternalEffectRecoveryUnavailableError(RuntimeError):
    pass


class ExternalEffectRecoveryCommands(Protocol):
    def reconcile_external_effect(
        self,
        *,
        tenant_id: str,
        source_record_ref: str,
        expected_revision: int,
        command_id: str,
        actor_id: str,
        reason: str,
    ) -> str: ...


class OwningWorkflowRecoveryCommands(Protocol):
    def execute(
        self,
        *,
        tenant_id: str,
        source_record_ref: str,
        expected_revision: int,
        command_id: str,
        actor_id: str,
        reason: str,
    ) -> str: ...


class RecoveryCommandService:
    def __init__(
        self,
        repository: SQLiteIncidentRepository,
        *,
        run_service: RunService,
        clock: Callable[[], datetime],
        external_effects: ExternalEffectRecoveryCommands | None = None,
        compatible_replans: OwningWorkflowRecoveryCommands | None = None,
        contract_supersessions: OwningWorkflowRecoveryCommands | None = None,
    ) -> None:
        self._repository = repository
        self._run_service = run_service
        self._clock = clock
        self._external_effects = external_effects
        self._compatible_replans = compatible_replans
        self._contract_supersessions = contract_supersessions

    def execute(self, command: RecoveryCommand) -> RecoveryActionEvidence:
        replay = self._repository.load_recovery_evidence(command.tenant_id, command.command_id)
        if replay is not None:
            if _evidence_matches_command(replay, command):
                return replay
            raise RecoveryActionNotAllowedError("recovery command conflicts with recorded evidence")

        incident = self._repository.load_current(command.tenant_id, command.incident_id)
        if incident.revision != command.expected_incident_revision:
            raise StaleIncidentRevisionError
        if command.action not in incident.allowed_operator_actions:
            raise RecoveryActionNotAllowedError
        if command.action in {"retry_transient_attempt", "cancel_unstarted_work"} and (
            incident.run_id is None
        ):
            raise RecoveryActionNotAllowedError("recovery action has no authoritative run")

        try:
            if command.action == "retry_transient_attempt":
                assert incident.run_id is not None
                if incident.classification != "transient":
                    raise RecoveryActionNotAllowedError("retry requires a transient incident")
                if incident.run_attempt_number is None:
                    raise RecoveryActionNotAllowedError(
                        "retry does not target the latest failed attempt"
                    )
                retry = self._run_service.request_retry(
                    tenant_id=command.tenant_id,
                    run_id=incident.run_id,
                    failed_attempt_number=incident.run_attempt_number,
                    command_id=command.command_id,
                    incident_id=incident.incident_id,
                    incident_revision=incident.revision,
                    requested_by=command.actor_id,
                    reason=command.reason,
                )
                resulting_record_ref = f"run-retry:{retry.run_id}:{retry.failed_attempt_number}"
            elif command.action == "cancel_unstarted_work":
                assert incident.run_id is not None
                cancellation = self._run_service.cancel_unstarted(
                    tenant_id=command.tenant_id,
                    run_id=incident.run_id,
                    cancelled_by=command.actor_id,
                    reason=command.reason,
                )
                resulting_record_ref = f"run-cancellation:{cancellation.run_id}"
            elif command.action == "reconcile_external_effect":
                external_effects = self._external_effects
                if external_effects is None or incident.source_record_revision is None:
                    raise RecoveryActionNotAllowedError(
                        "external effect recovery authority is unavailable"
                    )
                resulting_record_ref = external_effects.reconcile_external_effect(
                    tenant_id=command.tenant_id,
                    source_record_ref=incident.source_record_ref,
                    expected_revision=incident.source_record_revision,
                    command_id=command.command_id,
                    actor_id=command.actor_id,
                    reason=command.reason,
                )
            else:
                workflow = (
                    self._compatible_replans
                    if command.action == "approve_compatible_replan"
                    else self._contract_supersessions
                )
                if workflow is None or incident.source_record_revision is None:
                    raise RecoveryActionNotAllowedError(
                        "owning workflow recovery authority is unavailable"
                    )
                resulting_record_ref = workflow.execute(
                    tenant_id=command.tenant_id,
                    source_record_ref=incident.source_record_ref,
                    expected_revision=incident.source_record_revision,
                    command_id=command.command_id,
                    actor_id=command.actor_id,
                    reason=command.reason,
                )
        except RecoveryActionNotAllowedError:
            raise
        except (KeyError, ValueError) as error:
            raise RecoveryActionNotAllowedError(str(error)) from None

        evidence = RecoveryActionEvidence(
            command_id=command.command_id,
            tenant_id=command.tenant_id,
            incident_id=incident.incident_id,
            incident_revision=incident.revision,
            action=command.action,
            actor_id=command.actor_id,
            reason=command.reason,
            source_record_ref=incident.source_record_ref,
            resulting_record_ref=resulting_record_ref,
            recorded_at=self._clock(),
        )
        return self._repository.append_recovery_evidence(evidence)


def _evidence_matches_command(evidence: RecoveryActionEvidence, command: RecoveryCommand) -> bool:
    return (
        evidence.command_id,
        evidence.tenant_id,
        evidence.incident_id,
        evidence.incident_revision,
        evidence.action,
        evidence.actor_id,
        evidence.reason,
    ) == (
        command.command_id,
        command.tenant_id,
        command.incident_id,
        command.expected_incident_revision,
        command.action,
        command.actor_id,
        command.reason,
    )
