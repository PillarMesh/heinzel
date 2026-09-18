from __future__ import annotations

from typing import Protocol

from heinzel_contract_model import digest
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_state import (
    IncidentFailureClassification,
    IncidentPersistenceError,
    IncidentRecord,
)

from .answer_models import AnswerExecutionReceipt


class AnswerExecutionIncidentRepository(Protocol):
    def append(
        self,
        incident: IncidentRecord,
        *,
        expected_current_revision: int,
    ) -> IncidentRecord: ...


class AnswerExecutionIncidentProjectionUnavailable(RuntimeError):
    pass


class AnswerExecutionIncidentProjector:
    """Project a durable terminal query receipt into state-owned operator visibility."""

    def __init__(self, repository: AnswerExecutionIncidentRepository) -> None:
        self._repository = repository

    def project(self, receipt: AnswerExecutionReceipt) -> IncidentRecord | None:
        authoritative = AnswerExecutionReceipt.model_validate(
            receipt.model_dump(mode="python"), strict=True
        )
        if authoritative.outcome == "succeeded":
            return None
        classification = _incident_classification(authoritative)
        incident = IncidentRecord(
            incident_id="incident-query-"
            + digest(
                {
                    "domain": "heinzel-query-failure-incident-v1",
                    "tenant_id": authoritative.tenant_id,
                    "request_id": authoritative.request_id,
                    "plan_digest": authoritative.plan_digest,
                }
            )[:24],
            tenant_id=authoritative.tenant_id,
            revision=1,
            kind="query_failure",
            classification=classification,
            last_successful_stage=None,
            failed_stage="governed_query",
            user_impact=_user_impact(classification),
            next_automatic_action=None,
            allowed_operator_actions=(),
            source_service="runtime",
            source_record_ref=authoritative.receipt_id,
            run_id=None,
            run_attempt_number=None,
            evidence_refs=(authoritative.receipt_id,),
            opened_at=authoritative.started_at,
            updated_at=authoritative.completed_at,
        )
        try:
            return self._repository.append(incident, expected_current_revision=0)
        except IncidentPersistenceError:
            raise AnswerExecutionIncidentProjectionUnavailable(
                "query incident persistence is unavailable"
            ) from None


def _incident_classification(
    receipt: AnswerExecutionReceipt,
) -> IncidentFailureClassification:
    if receipt.outcome == "timed_out":
        return "transient"
    if receipt.outcome == "generation_unavailable":
        return "conflict"
    if receipt.outcome in {"ceiling_exceeded", "aborted"}:
        return "permanent"
    provider_classification = receipt.provider_error_classification
    if provider_classification is None:
        return "integrity_failure"
    return _provider_incident_classification(provider_classification)


def _provider_incident_classification(
    classification: ProviderErrorClassification,
) -> IncidentFailureClassification:
    if classification in {
        "retryable",
        "throttled",
        "transient_transport",
        "transient_unavailable",
    }:
        return "transient"
    if classification in {"ambiguous", "ambiguous_outcome"}:
        return "ambiguous_outcome"
    if classification in {"authorization", "authorization_denied"}:
        return "authorization_denied"
    if classification in {"invalid_provider_response", "integrity_failure"}:
        return "integrity_failure"
    return "permanent"


def _user_impact(classification: IncidentFailureClassification) -> str:
    if classification == "transient":
        return "The requested answer is delayed because the warehouse is temporarily unavailable."
    if classification == "authorization_denied":
        return "The requested answer could not be calculated with the current access authority."
    if classification == "conflict":
        return "The requested answer could not be calculated from the approved data generation."
    if classification in {"ambiguous_outcome", "integrity_failure"}:
        return "The requested answer could not be verified and remains unavailable."
    return "The requested answer could not be calculated under the approved query."
