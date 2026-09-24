from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from .models import StateModel, _require_utc

type IncidentKind = Literal[
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
]
type IncidentFailureClassification = Literal[
    "transient",
    "permanent",
    "ambiguous_outcome",
    "authorization_denied",
    "integrity_failure",
    "conflict",
    "no_valid_plan",
]
type IncidentStage = Literal[
    "request_intake",
    "contract_activation",
    "extract",
    "land",
    "transform",
    "catalog_publication",
    "governed_query",
    "dashboard_publication",
    "access_revocation",
]
type IncidentAutomaticAction = Literal[
    "retry_transient_attempt",
    "reconcile_external_effect",
]
type IncidentOperatorAction = Literal[
    "retry_transient_attempt",
    "cancel_unstarted_work",
    "approve_compatible_replan",
    "reconcile_external_effect",
    "supersede_contract",
]
type RunRecoveryAction = Literal[
    "retry_transient_attempt",
    "cancel_unstarted_work",
    "approve_compatible_replan",
    "reconcile_external_effect",
    "supersede_contract",
]


class IncidentRecord(StateModel):
    schema_version: Literal["1"] = "1"
    incident_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    kind: IncidentKind
    classification: IncidentFailureClassification
    last_successful_stage: IncidentStage | None = None
    failed_stage: IncidentStage
    user_impact: str = Field(min_length=1)
    next_automatic_action: IncidentAutomaticAction | None = None
    allowed_operator_actions: tuple[IncidentOperatorAction, ...] = Field(
        default=(),
        description="Actions admitted from run state by the recovery command service.",
    )
    source_service: str = Field(min_length=1)
    source_record_ref: str = Field(min_length=1)
    source_record_revision: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, min_length=1)
    run_attempt_number: int | None = Field(default=None, ge=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    opened_at: datetime
    updated_at: datetime

    @field_validator("opened_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_references_are_nonempty_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not reference for reference in value):
            raise ValueError("evidence_refs cannot contain an empty reference")
        if len(value) != len(set(value)):
            raise ValueError("evidence_refs must be unique")
        return value

    @field_validator("allowed_operator_actions")
    @classmethod
    def operator_actions_are_unique(
        cls, value: tuple[IncidentOperatorAction, ...]
    ) -> tuple[IncidentOperatorAction, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_operator_actions must be unique")
        return value

    @model_validator(mode="after")
    def actions_match_failure_state(self) -> Self:
        if self.updated_at < self.opened_at:
            raise ValueError("updated_at cannot precede opened_at")

        is_no_valid_plan = self.kind == "no_valid_plan"
        if is_no_valid_plan != (self.classification == "no_valid_plan"):
            raise ValueError("no_valid_plan incident requires the no_valid_plan classification")

        offers_retry = (
            self.next_automatic_action == "retry_transient_attempt"
            or "retry_transient_attempt" in self.allowed_operator_actions
        )
        if offers_retry and self.classification != "transient":
            raise ValueError("retry_transient_attempt requires a transient incident")

        if (
            self.classification in {"permanent", "no_valid_plan"}
            and self.next_automatic_action is not None
        ):
            raise ValueError("terminal incidents cannot have an automatic action")

        if (
            self.next_automatic_action == "reconcile_external_effect"
            and self.classification not in {"transient", "ambiguous_outcome"}
        ):
            raise ValueError("reconcile_external_effect requires a transient or ambiguous incident")

        if offers_retry and (self.run_id is None or self.run_attempt_number is None):
            raise ValueError("retry_transient_attempt requires an exact run attempt")
        if "cancel_unstarted_work" in self.allowed_operator_actions and self.run_id is None:
            raise ValueError("cancel_unstarted_work requires a run")
        offers_reconciliation = (
            self.next_automatic_action == "reconcile_external_effect"
            or "reconcile_external_effect" in self.allowed_operator_actions
        )
        if offers_reconciliation and self.source_record_revision is None:
            raise ValueError("reconcile_external_effect requires an exact source revision")
        if self.run_attempt_number is not None and self.run_id is None:
            raise ValueError("run_attempt_number requires run_id")

        return self


class RecoveryCommand(StateModel):
    schema_version: Literal["1"] = "1"
    command_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    expected_incident_revision: int = Field(ge=1)
    action: RunRecoveryAction
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RecoveryActionEvidence(StateModel):
    schema_version: Literal["1"] = "1"
    command_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    incident_revision: int = Field(ge=1)
    action: RunRecoveryAction
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_record_ref: str = Field(min_length=1)
    resulting_record_ref: str = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)
