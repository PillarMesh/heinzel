from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    AnswerAdmissionEvidence,
    AnswerExecutionEvidence,
    AnswerProductGenerationReference,
    AnswerQueryColumnEvidence,
    AnswerResultEvidence,
    DashboardAnswerAuthority,
    GovernedAnswer,
    GovernedAnswerVerificationError,
    InboxRequest,
    RequestManagementDashboardAnswerAuthorityReader,
    RequestState,
    StakeholderQuestion,
)

NOW = datetime(2026, 9, 12, 12, 1, tzinfo=UTC)
PLAN_DIGEST = "1" * 64


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest="0" * 64)


def _generation() -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(product_ref=_reference("product-revenue"), generation=7)


def _request(**changes: object) -> InboxRequest:
    values: dict[str, object] = {
        "title": "Revenue by region",
        "request_id": "request-1",
        "tenant_id": "tenant-a",
        "requester_id": "requester-a",
        "payload": StakeholderQuestion(purpose="Review revenue", question="Revenue by region?"),
        "state": RequestState.DELIVERED,
        "revision": 4,
        "submitted_at": NOW - timedelta(minutes=10),
        "updated_at": NOW - timedelta(minutes=1),
    }
    return InboxRequest.model_validate(values | changes)


def _snapshot(**changes: object) -> AnswerResultEvidence:
    columns = (
        AnswerQueryColumnEvidence(name="region", value_type="string"),
        AnswerQueryColumnEvidence(name="revenue", value_type="decimal"),
    )
    rows = (("east", "99.00"),)
    values: dict[str, object] = {
        "result_ref": "result-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "plan_digest": PLAN_DIGEST,
        "product_generation_refs": (_generation(),),
        "columns": columns,
        "rows": rows,
        "row_count": 1,
        "byte_count": len(canonical_bytes(rows)),
        "result_schema_digest": digest(columns),
        "result_digest": digest({"columns": columns, "rows": rows}),
        "created_at": NOW - timedelta(minutes=3),
        "expires_at": NOW + timedelta(hours=1),
    }
    return AnswerResultEvidence.model_validate(values | changes)


def _receipt(snapshot: AnswerResultEvidence, **changes: object) -> AnswerExecutionEvidence:
    values: dict[str, object] = {
        "receipt_id": "receipt-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "plan_digest": PLAN_DIGEST,
        "product_generation_refs": (_generation(),),
        "attempt": 1,
        "started_at": NOW - timedelta(minutes=4),
        "completed_at": NOW - timedelta(minutes=2),
        "outcome": "succeeded",
        "row_count": snapshot.row_count,
        "byte_count": snapshot.byte_count,
        "suppressed_group_count": 0,
        "result_schema_digest": snapshot.result_schema_digest,
        "result_digest": snapshot.result_digest,
        "result_ref": snapshot.result_ref,
        "freshness_observation_ref": "freshness-1",
        "quality_observation_ref": "quality-1",
    }
    return AnswerExecutionEvidence.model_validate(values | changes)


def _answer(snapshot: AnswerResultEvidence, **changes: object) -> GovernedAnswer:
    values: dict[str, object] = {
        "answer_id": "answer-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "request_revision": 3,
        "restatement": "Revenue grouped by region.",
        "admission_ref": "admission-1",
        "execution_receipt_ref": "receipt-1",
        "metric_version_refs": (_reference("metric-revenue"),),
        "product_generation_refs": (_generation(),),
        "as_of": NOW - timedelta(minutes=5),
        "freshness_disposition": "current",
        "material_quality_limitations": (),
        "lineage_refs": (_reference("lineage-revenue"),),
        "narrative": "One verified result row is available.",
        "narrative_source": "template",
        "result_ref": snapshot.result_ref,
        "result_digest": snapshot.result_digest,
        "refreshes_answer_ref": None,
        "delivered_at": NOW - timedelta(minutes=1),
    }
    return GovernedAnswer.model_validate(values | changes)


def _admission(**changes: object) -> AnswerAdmissionEvidence:
    values: dict[str, object] = {
        "admission_ref": "admission-1",
        "admission_kind": "policy",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "admission_request_revision": 1,
        "verifying_request_revision": 3,
        "validation_digest": "2" * 64,
        "plan_digest": PLAN_DIGEST,
        "policy_id": "policy-1",
        "policy_revision": 1,
        "policy_snapshot_digest": "3" * 64,
        "entitlement_snapshot_digest": "4" * 64,
    }
    return AnswerAdmissionEvidence.model_validate(values | changes)


class _Requests:
    def __init__(self, request: InboxRequest | None) -> None:
        self.request = request

    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None:
        if self.request is not None and self.request.tenant_id != tenant_id:
            raise KeyError(request_id)
        return self.request


class _Answers:
    def __init__(self, answer: GovernedAnswer | None, latest: GovernedAnswer | None = None) -> None:
        self.answer = answer
        self.latest = latest if latest is not None else answer

    def read(self, tenant_id: str, answer_id: str) -> GovernedAnswer | None:
        del tenant_id, answer_id
        return self.answer

    def list_for_request(self, tenant_id: str, request_id: str) -> tuple[GovernedAnswer, ...]:
        del tenant_id, request_id
        return () if self.latest is None else (self.latest,)


class _Admissions:
    def __init__(self, admission: AnswerAdmissionEvidence | None) -> None:
        self.admission = admission

    def read_admission(
        self, tenant_id: str, request_id: str, admission_ref: str
    ) -> AnswerAdmissionEvidence | None:
        del tenant_id, request_id, admission_ref
        return self.admission


class _Executions:
    def __init__(
        self, receipt: AnswerExecutionEvidence | None, snapshot: AnswerResultEvidence | None
    ) -> None:
        self.receipt = receipt
        self.snapshot = snapshot

    def read_receipt(self, *args: object, **kwargs: object) -> AnswerExecutionEvidence | None:
        del args, kwargs
        return self.receipt

    def read_result(self, *args: object, **kwargs: object) -> AnswerResultEvidence | None:
        del args, kwargs
        return self.snapshot


def _reader(
    *,
    request: InboxRequest | None = None,
    answer: GovernedAnswer | None = None,
    latest: GovernedAnswer | None = None,
    admission: AnswerAdmissionEvidence | None = None,
    receipt: AnswerExecutionEvidence | None = None,
    snapshot: AnswerResultEvidence | None = None,
) -> RequestManagementDashboardAnswerAuthorityReader:
    snapshot = snapshot or _snapshot()
    answer = answer or _answer(snapshot)
    return RequestManagementDashboardAnswerAuthorityReader(
        requests=_Requests(request or _request()),
        answers=_Answers(answer, latest),
        admissions=_Admissions(admission or _admission()),
        executions=_Executions(receipt or _receipt(snapshot), snapshot),
        clock=lambda: NOW,
    )


def test_current_delivered_answer_projects_only_dashboard_authority() -> None:
    authority = _reader().read_exact(
        tenant_id="tenant-a", request_id="request-1", answer_id="answer-1"
    )

    assert authority == DashboardAnswerAuthority(
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=3,
        answer_id="answer-1",
        title="Revenue by region",
        execution_receipt_ref="receipt-1",
        result_ref="result-1",
        result_digest=_snapshot().result_digest,
        product_generation_refs=(_generation(),),
        metric_version_refs=(_reference("metric-revenue"),),
        as_of=NOW - timedelta(minutes=5),
        freshness_disposition="current",
        delivered_at=NOW - timedelta(minutes=1),
        result_expires_at=NOW + timedelta(hours=1),
    )
    assert "statement" not in authority.model_dump()
    assert "plan_digest" not in authority.model_dump()


@pytest.mark.parametrize(
    "reader",
    (
        _reader(request=_request(state=RequestState.VERIFYING)),
        _reader(request=_request(title=None)),
        _reader(request=_request(revision=5)),
        _reader(answer=_answer(_snapshot(), answer_id="answer-2")),
        _reader(latest=_answer(_snapshot(), answer_id="answer-2", request_revision=4)),
    ),
)
def test_missing_cross_identity_or_stale_authority_is_not_visible(
    reader: RequestManagementDashboardAnswerAuthorityReader,
) -> None:
    assert (
        reader.read_exact(tenant_id="tenant-a", request_id="request-1", answer_id="answer-1")
        is None
    )


def test_missing_or_cross_tenant_request_is_not_visible() -> None:
    snapshot = _snapshot()
    answer = _answer(snapshot)
    missing = RequestManagementDashboardAnswerAuthorityReader(
        requests=_Requests(None),
        answers=_Answers(answer),
        admissions=_Admissions(_admission()),
        executions=_Executions(_receipt(snapshot), snapshot),
        clock=lambda: NOW,
    )
    cross_tenant = RequestManagementDashboardAnswerAuthorityReader(
        requests=_Requests(_request(tenant_id="tenant-b")),
        answers=_Answers(answer),
        admissions=_Admissions(_admission()),
        executions=_Executions(_receipt(snapshot), snapshot),
        clock=lambda: NOW,
    )

    assert (
        missing.read_exact(tenant_id="tenant-a", request_id="request-1", answer_id="answer-1")
        is None
    )
    assert (
        cross_tenant.read_exact(tenant_id="tenant-a", request_id="request-1", answer_id="answer-1")
        is None
    )


@pytest.mark.parametrize(
    ("reader", "message"),
    (
        (_reader(admission=_admission(verifying_request_revision=4)), "admission"),
        (_reader(receipt=_receipt(_snapshot(), outcome="provider_failed")), "successful"),
        (_reader(receipt=_receipt(_snapshot(), result_ref="result-other")), "reference"),
        (_reader(snapshot=_snapshot(row_count=2)), "row count"),
        (_reader(snapshot=_snapshot(expires_at=NOW)), "expired"),
    ),
)
def test_present_corrupt_evidence_raises_typed_verification_error(
    reader: RequestManagementDashboardAnswerAuthorityReader, message: str
) -> None:
    with pytest.raises(GovernedAnswerVerificationError, match=message):
        reader.read_exact(tenant_id="tenant-a", request_id="request-1", answer_id="answer-1")
