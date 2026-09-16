from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pillarmesh_state import IncidentRecord, RecoveryCommand
from pillarmesh_state.incident_models import (
    IncidentFailureClassification,
    IncidentKind,
    IncidentOperatorAction,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 12, 18, tzinfo=UTC)


def incident(**overrides: object) -> IncidentRecord:
    values: dict[str, object] = {
        "incident_id": "incident-a",
        "tenant_id": "tenant-a",
        "revision": 1,
        "kind": "source_unavailable",
        "classification": "transient",
        "last_successful_stage": "contract_activation",
        "failed_stage": "extract",
        "user_impact": "The current source interval has not been loaded.",
        "next_automatic_action": "retry_transient_attempt",
        "allowed_operator_actions": ("retry_transient_attempt",),
        "source_service": "runtime",
        "source_record_ref": "attempt:run-a:1",
        "run_id": "run-a",
        "run_attempt_number": 1,
        "evidence_refs": ("evidence:attempt:run-a:1",),
        "opened_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return IncidentRecord.model_validate(values)


@pytest.mark.parametrize(
    "kind",
    (
        "stuck_lease",
        "source_unavailable",
        "checkpoint_conflict",
        "schema_drift",
        "no_valid_plan",
        "transform_rejection",
        "catalog_pending",
        "query_failure",
        "dashboard_drift",
        "revocation_pending",
    ),
)
def test_incident_contract_accepts_each_governed_kind(kind: IncidentKind) -> None:
    classification: IncidentFailureClassification = (
        "no_valid_plan" if kind == "no_valid_plan" else "transient"
    )
    next_action = None if kind == "no_valid_plan" else "retry_transient_attempt"
    operator_actions: tuple[IncidentOperatorAction, ...] = (
        ("approve_compatible_replan",) if kind == "no_valid_plan" else ("retry_transient_attempt",)
    )

    record = incident(
        kind=kind,
        classification=classification,
        next_automatic_action=next_action,
        allowed_operator_actions=operator_actions,
    )

    assert record.kind == kind
    assert record.tenant_id == "tenant-a"
    assert record.revision == 1


def test_incident_is_strict_frozen_utc_and_attributable() -> None:
    record = incident()

    with pytest.raises(ValidationError, match="frozen"):
        record.revision = 2
    with pytest.raises(ValidationError, match="extra"):
        IncidentRecord.model_validate({**record.model_dump(), "payload": "private"})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        incident(updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="at least 1 item"):
        incident(evidence_refs=())


@pytest.mark.parametrize("classification", ("permanent", "no_valid_plan"))
def test_terminal_incident_cannot_offer_retry(
    classification: IncidentFailureClassification,
) -> None:
    with pytest.raises(ValidationError, match="retry_transient_attempt"):
        incident(
            kind="no_valid_plan" if classification == "no_valid_plan" else "query_failure",
            classification=classification,
        )


def test_no_valid_plan_has_exact_classification_and_no_automatic_action() -> None:
    with pytest.raises(ValidationError, match="no_valid_plan incident"):
        incident(kind="no_valid_plan", classification="permanent", next_automatic_action=None)
    with pytest.raises(ValidationError, match="cannot have an automatic action"):
        incident(
            kind="no_valid_plan",
            classification="no_valid_plan",
            next_automatic_action="reconcile_external_effect",
            allowed_operator_actions=("approve_compatible_replan",),
        )


def test_contract_activation_can_precede_cancellation_of_unstarted_work() -> None:
    record = incident(
        last_successful_stage="contract_activation",
        allowed_operator_actions=(
            "retry_transient_attempt",
            "cancel_unstarted_work",
        ),
    )

    assert "cancel_unstarted_work" in record.allowed_operator_actions


def test_incident_rejects_unknown_kind_and_duplicate_evidence() -> None:
    with pytest.raises(ValidationError):
        incident(kind=cast(IncidentKind, "credential_leak"))
    with pytest.raises(ValidationError, match="evidence_refs must be unique"):
        incident(evidence_refs=("evidence:a", "evidence:a"))


def test_incident_requires_ordered_revision_timestamps_and_unique_actions() -> None:
    with pytest.raises(ValidationError, match="updated_at cannot precede opened_at"):
        incident(updated_at=datetime(2026, 9, 12, 17, tzinfo=UTC))
    with pytest.raises(ValidationError, match="allowed_operator_actions must be unique"):
        incident(
            allowed_operator_actions=(
                "retry_transient_attempt",
                "retry_transient_attempt",
            )
        )


def test_recovery_command_requires_actor_reason_and_rejects_unknown_actions() -> None:
    values = {
        "command_id": "recovery-command-a",
        "tenant_id": "tenant-a",
        "incident_id": "incident-a",
        "expected_incident_revision": 1,
        "action": "retry_transient_attempt",
        "actor_id": "architect-a",
        "reason": "Source connectivity recovered.",
    }

    with pytest.raises(ValidationError):
        RecoveryCommand.model_validate({**values, "reason": ""})
    assert (
        RecoveryCommand.model_validate({**values, "action": "reconcile_external_effect"}).action
        == "reconcile_external_effect"
    )
    with pytest.raises(ValidationError):
        RecoveryCommand.model_validate({**values, "action": "restart_everything"})
    with pytest.raises(ValidationError, match="extra"):
        RecoveryCommand.model_validate({**values, "provider_payload": {"retry": True}})


def test_retry_projection_requires_exact_run_attempt_correlation() -> None:
    with pytest.raises(ValidationError, match="exact run attempt"):
        incident(run_id=None, run_attempt_number=None)
    with pytest.raises(ValidationError, match="run_attempt_number requires run_id"):
        incident(
            classification="authorization_denied",
            next_automatic_action=None,
            allowed_operator_actions=(),
            run_id=None,
            run_attempt_number=1,
        )
