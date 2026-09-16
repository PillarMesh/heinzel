from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_contract_model import ArtifactModel, ArtifactReference
from pydantic import ConfigDict, Field, field_validator

from .answer_delivery import (
    AnswerAdmissionReader,
    AnswerExecutionReader,
    verify_answer_result_contents,
)
from .answer_errors import GovernedAnswerVerificationError
from .answer_models import AnswerProductGenerationReference, GovernedAnswer
from .models import InboxRequest, RequestState

DashboardProductGenerationReference = AnswerProductGenerationReference


class DashboardAnswerAuthority(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    request_revision: int = Field(ge=1)
    answer_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=16_000)
    execution_receipt_ref: str = Field(min_length=1)
    result_ref: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_generation_refs: tuple[AnswerProductGenerationReference, ...]
    metric_version_refs: tuple[ArtifactReference, ...]
    as_of: datetime
    freshness_disposition: Literal["current", "stale", "unknown", "not_applicable"]
    delivered_at: datetime

    @field_validator("as_of", "delivered_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime, info: object) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            field_name = getattr(info, "field_name", "timestamp")
            raise ValueError(f"{field_name} must be timezone-aware UTC")
        return value.astimezone(UTC)


class DashboardRequestReader(Protocol):
    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None: ...


class DashboardGovernedAnswerReader(Protocol):
    def read(self, tenant_id: str, answer_id: str) -> GovernedAnswer | None: ...

    def list_for_request(self, tenant_id: str, request_id: str) -> tuple[GovernedAnswer, ...]: ...


class RequestManagementDashboardAnswerAuthorityReader:
    """Project current delivered answer evidence without exposing its executable plan."""

    def __init__(
        self,
        *,
        requests: DashboardRequestReader,
        answers: DashboardGovernedAnswerReader,
        admissions: AnswerAdmissionReader,
        executions: AnswerExecutionReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._requests = requests
        self._answers = answers
        self._admissions = admissions
        self._executions = executions
        self._clock = clock

    def read_exact(
        self, *, tenant_id: str, request_id: str, answer_id: str
    ) -> DashboardAnswerAuthority | None:
        try:
            request = self._requests.load(tenant_id, request_id)
        except KeyError:
            return None
        answer = self._answers.read(tenant_id, answer_id)
        if request is None or answer is None:
            return None
        if not self._is_current_identity(
            tenant_id=tenant_id,
            request_id=request_id,
            answer_id=answer_id,
            request=request,
            answer=answer,
        ):
            return None
        title = request.title
        if title is None:
            return None
        answers = self._answers.list_for_request(tenant_id, request_id)
        if not answers or answers[-1] != answer:
            return None

        admission = self._admissions.read_admission(tenant_id, request_id, answer.admission_ref)
        if admission is None or (
            admission.tenant_id != tenant_id
            or admission.request_id != request_id
            or admission.admission_ref != answer.admission_ref
            or admission.verifying_request_revision != answer.request_revision
        ):
            raise GovernedAnswerVerificationError(
                "dashboard answer admission evidence does not match delivery"
            )
        execution_receipt_ref = answer.execution_receipt_ref
        result_ref = answer.result_ref
        result_digest = answer.result_digest
        if execution_receipt_ref is None or result_ref is None or result_digest is None:
            raise GovernedAnswerVerificationError(
                "dashboard answer metric value lacks execution and result evidence"
            )
        receipt = self._executions.read_receipt(
            tenant_id,
            request_id,
            execution_receipt_ref,
            admission.plan_digest,
            answer.product_generation_refs,
        )
        snapshot = self._executions.read_result(
            tenant_id,
            request_id,
            result_ref,
            admission.plan_digest,
            answer.product_generation_refs,
        )
        if receipt is None or snapshot is None:
            raise GovernedAnswerVerificationError(
                "dashboard answer execution or result evidence is unavailable"
            )
        if receipt.outcome != "succeeded":
            raise GovernedAnswerVerificationError(
                "dashboard answer requires a successful execution receipt"
            )
        if receipt.receipt_id != execution_receipt_ref:
            raise GovernedAnswerVerificationError(
                "dashboard answer execution receipt reference does not match delivery"
            )
        if not (
            receipt.result_ref == snapshot.result_ref == result_ref
            and receipt.result_digest == snapshot.result_digest == result_digest
        ):
            raise GovernedAnswerVerificationError(
                "dashboard answer result reference or digest does not match delivery"
            )
        verify_answer_result_contents(receipt, snapshot)
        now = self._clock()
        if not (
            answer.as_of <= answer.delivered_at
            and snapshot.created_at <= receipt.completed_at <= answer.delivered_at <= now
        ):
            raise GovernedAnswerVerificationError(
                "dashboard answer evidence timestamps do not match delivery"
            )
        if snapshot.expires_at <= now:
            raise GovernedAnswerVerificationError("dashboard answer result evidence has expired")

        return DashboardAnswerAuthority(
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=answer.request_revision,
            answer_id=answer.answer_id,
            title=title,
            execution_receipt_ref=execution_receipt_ref,
            result_ref=result_ref,
            result_digest=result_digest,
            product_generation_refs=answer.product_generation_refs,
            metric_version_refs=answer.metric_version_refs,
            as_of=answer.as_of,
            freshness_disposition=answer.freshness_disposition,
            delivered_at=answer.delivered_at,
        )

    @staticmethod
    def _is_current_identity(
        *,
        tenant_id: str,
        request_id: str,
        answer_id: str,
        request: InboxRequest,
        answer: GovernedAnswer,
    ) -> bool:
        return (
            request.tenant_id == tenant_id
            and request.request_id == request_id
            and request.title is not None
            and request.state is RequestState.DELIVERED
            and answer.tenant_id == tenant_id
            and answer.request_id == request_id
            and answer.answer_id == answer_id
            and answer.intent_kind == "metric_value"
            and request.revision == answer.request_revision + 1
        )
