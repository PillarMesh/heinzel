from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import ArtifactReference, canonical_bytes, digest
from pillarmesh_request_management import (
    AnswerAdmissionEvidence,
    AnswerDeliveryAuthorization,
    AnswerExecutionEvidence,
    AnswerPlanCeilingsEvidence,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    AnswerQueryColumnEvidence,
    AnswerResultEvidence,
    AnswerScanCeilingEvidence,
    DeliverGovernedAnswerCommand,
    GovernedAnswer,
    GovernedAnswerConflict,
    GovernedAnswerNotVisible,
    GovernedAnswerService,
    GovernedAnswerStaleRevision,
    GovernedAnswerVerificationError,
    InboxRequest,
    PolicyAdmissionReceipt,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteGovernedAnswerRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
    StakeholderQuestion,
)
from pillarmesh_request_management.answer_admission import PolicyScanReservation

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
DIGEST = "0" * 64
PLAN_DIGEST = "1" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def generation(generation_number: int = 7) -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(
        product_ref=reference("product-revenue"), generation=generation_number
    )


def result() -> AnswerResultEvidence:
    columns = (
        AnswerQueryColumnEvidence(name="region", value_type="string"),
        AnswerQueryColumnEvidence(name="net_revenue", value_type="decimal"),
    )
    rows = (("West", "12.50"),)
    return AnswerResultEvidence(
        result_ref="result-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=PLAN_DIGEST,
        product_generation_refs=(generation(),),
        columns=columns,
        rows=rows,
        row_count=1,
        byte_count=len(canonical_bytes(rows)),
        result_schema_digest=digest(columns),
        result_digest=digest({"columns": columns, "rows": rows}),
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def receipt(snapshot: AnswerResultEvidence | None = None) -> AnswerExecutionEvidence:
    snapshot = snapshot or result()
    return AnswerExecutionEvidence(
        receipt_id="receipt-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=PLAN_DIGEST,
        product_generation_refs=(generation(),),
        attempt=1,
        started_at=NOW - timedelta(minutes=2),
        completed_at=NOW - timedelta(minutes=1),
        outcome="succeeded",
        row_count=snapshot.row_count,
        byte_count=snapshot.byte_count,
        suppressed_group_count=0,
        result_schema_digest=snapshot.result_schema_digest,
        result_digest=snapshot.result_digest,
        result_ref=snapshot.result_ref,
        freshness_observation_ref="freshness-7",
        quality_observation_ref="quality-7",
    )


def plan() -> AnswerPlanEvidence:
    return AnswerPlanEvidence(
        tenant_id="tenant-a",
        validation_digest="5" * 64,
        plan_digest=PLAN_DIGEST,
        product_generation_refs=(generation(),),
        minimum_group_size=5,
        statement="SELECT region, SUM(net_revenue) FROM revenue GROUP BY region "
        "HAVING COUNT(DISTINCT customer_id) >= :p1",
        ceilings=AnswerPlanCeilingsEvidence(
            row_limit=10,
            scan=AnswerScanCeilingEvidence(rows=100, bytes=10_000),
            period_scan=AnswerScanCeilingEvidence(rows=1_000, bytes=100_000),
        ),
    )


def reviewed_draft(plan_digest: str | None = PLAN_DIGEST) -> StakeholderAnswerDraft:
    return StakeholderAnswerDraft(
        answer_text="For West, net revenue was 12.50.",
        governed_dataset_refs=(reference("product-revenue"),),
        metric_refs=(reference("metric-net-revenue"),),
        as_of=NOW - timedelta(minutes=1),
        freshness_disposition="current",
        material_quality_limitations=(reference("quality-limit-1"),),
        lineage_refs=(reference("lineage-revenue"),),
        disclosure_classifications=(),
        plan_digest=plan_digest,
    )


def admission(*, plan_digest: str = PLAN_DIGEST) -> AnswerAdmissionEvidence:
    return AnswerAdmissionEvidence(
        admission_ref="admission-1",
        admission_kind="reviewed",
        tenant_id="tenant-a",
        request_id="request-1",
        admission_request_revision=2,
        verifying_request_revision=4,
        validation_digest="5" * 64,
        plan_digest=plan_digest,
        policy_id="policy-1",
        policy_revision=1,
        policy_snapshot_digest="2" * 64,
        entitlement_snapshot_digest="3" * 64,
        reviewed_answer=reviewed_draft(plan_digest),
    )


def authorization(**changes: object) -> AnswerDeliveryAuthorization:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "requester_id": "requester-a",
        "request_revision": 4,
        "plan_digest": PLAN_DIGEST,
        "policy_id": "policy-1",
        "policy_revision": 1,
        "policy_snapshot_digest": "2" * 64,
        "entitlement_snapshot_digest": "3" * 64,
        "policy_current": True,
        "entitlement_current": True,
        "valid_until": NOW + timedelta(minutes=30),
        "row_ceiling": 10,
        "byte_ceiling": 4096,
        "minimum_group_size": 5,
        "freshness_observation_ref": "freshness-7",
        "quality_observation_ref": "quality-7",
        "freshness_disposition": "current",
        "metric_version_refs": (reference("metric-net-revenue"),),
        "material_quality_limitations": (reference("quality-limit-1"),),
        "lineage_refs": (reference("lineage-revenue"),),
        "as_of": NOW - timedelta(minutes=1),
        "restatement": "Net revenue by region for the approved reporting period.",
        "approved_narrative_terms": ("net revenue", "region"),
    }
    values.update(changes)
    return AnswerDeliveryAuthorization.model_validate(values)


class AdmissionReader:
    def __init__(self, value: AnswerAdmissionEvidence | None = None) -> None:
        self.value = value if value is not None else admission()

    def read_admission(
        self, tenant_id: str, request_id: str, admission_ref: str
    ) -> AnswerAdmissionEvidence | None:
        if tenant_id != "tenant-a" or request_id != "request-1" or admission_ref != "admission-1":
            return None
        return self.value


class ExecutionReader:
    def __init__(
        self,
        execution_receipt: AnswerExecutionEvidence | None = None,
        snapshot: AnswerResultEvidence | None = None,
    ) -> None:
        self.execution_receipt = execution_receipt or receipt()
        self.snapshot: AnswerResultEvidence | None = snapshot or result()

    def read_receipt(
        self,
        tenant_id: str,
        request_id: str,
        receipt_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerExecutionEvidence | None:
        if tenant_id != "tenant-a" or request_id != "request-1" or receipt_ref != "receipt-1":
            return None
        return self.execution_receipt

    def read_result(
        self,
        tenant_id: str,
        request_id: str,
        result_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerResultEvidence | None:
        if tenant_id != "tenant-a" or request_id != "request-1" or result_ref != "result-1":
            return None
        return self.snapshot


class PlanReader:
    def __init__(self, value: AnswerPlanEvidence | None = None) -> None:
        self.value = value or plan()

    def read_plan(self, tenant_id: str, plan_digest: str) -> AnswerPlanEvidence | None:
        if tenant_id != "tenant-a" or plan_digest != PLAN_DIGEST:
            return None
        return self.value


class Rechecker:
    def __init__(self, value: AnswerDeliveryAuthorization | None = None) -> None:
        self.value = value or authorization()
        self.calls = 0

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> AnswerDeliveryAuthorization:
        self.calls += 1
        return self.value


class RevokedRechecker(Rechecker):
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> AnswerDeliveryAuthorization:
        self.calls += 1
        return authorization(policy_current=False)


class DownloadDeniedRechecker(Rechecker):
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> AnswerDeliveryAuthorization:
        if required_permission == "download":
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        return super().recheck(
            tenant_id=tenant_id,
            request_id=request_id,
            requester_id=requester_id,
            plan_digest=plan_digest,
            required_permission=required_permission,
        )


def command(**changes: object) -> DeliverGovernedAnswerCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "request_revision": 4,
        "requester_id": "requester-a",
        "actor_id": "system:answer-verifier",
        "admission_ref": "admission-1",
        "execution_receipt_ref": "receipt-1",
        "model_narrative": "For West, net revenue was 12.50.",
        "refreshes_answer_ref": None,
    }
    values.update(changes)
    return DeliverGovernedAnswerCommand.model_validate(values)


def service(
    *,
    admissions: AdmissionReader | None = None,
    executions: ExecutionReader | None = None,
    plans: PlanReader | None = None,
    rechecker: Rechecker | None = None,
) -> tuple[GovernedAnswerService, SQLiteGovernedAnswerRepository, Rechecker]:
    delivery, repository, authority, _, _ = _service_components(
        admissions=admissions,
        executions=executions,
        plans=plans,
        rechecker=rechecker,
    )
    return delivery, repository, authority


def _service_components(
    *,
    admissions: AdmissionReader | None = None,
    executions: ExecutionReader | None = None,
    plans: PlanReader | None = None,
    rechecker: Rechecker | None = None,
) -> tuple[
    GovernedAnswerService,
    SQLiteGovernedAnswerRepository,
    Rechecker,
    RequestManagementService,
    SQLiteRequestRepository,
]:
    requests, request_service = _verifying_request()
    repository = SQLiteGovernedAnswerRepository(requests)
    authority = rechecker or Rechecker()
    delivery = GovernedAnswerService(
        repository=repository,
        admission_reader=admissions or AdmissionReader(),
        execution_reader=executions or ExecutionReader(),
        plan_reader=plans or PlanReader(),
        authorization_rechecker=authority,
        clock=lambda: NOW,
    )
    return delivery, repository, authority, request_service, requests


def atomic_service() -> tuple[
    GovernedAnswerService,
    SQLiteGovernedAnswerRepository,
    RequestManagementService,
    SQLiteRequestRepository,
]:
    delivery, repository, _, request_service, requests = _service_components()
    return delivery, repository, request_service, requests


def _verifying_request() -> tuple[SQLiteRequestRepository, RequestManagementService]:
    requests = SQLiteRequestRepository.open(":memory:")
    request_service = RequestManagementService(requests, clock=lambda: NOW)
    requests.save(
        InboxRequest(
            request_id="request-1",
            tenant_id="tenant-a",
            requester_id="requester-a",
            payload=StakeholderQuestion(purpose="monthly close", question="What is revenue?"),
            state=RequestState.SUBMITTED,
            revision=1,
            submitted_at=NOW - timedelta(minutes=10),
            updated_at=NOW - timedelta(minutes=10),
        )
    )
    investigating = request_service.transition(
        "tenant-a",
        "request-1",
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=1,
    )
    SQLiteAnswerAdmissionRepository(requests).record_and_transition(
        PolicyAdmissionReceipt(
            admission_id="policy-admission-1",
            tenant_id="tenant-a",
            request_id="request-1",
            request_revision=investigating.revision,
            validation_digest="5" * 64,
            restatement_acceptance_ref="restatement-1",
            plan_digest=PLAN_DIGEST,
            policy_id="policy-1",
            policy_revision=1,
            policy_digest="6" * 64,
            entitlement_snapshot_digest="7" * 64,
            period_scan_consumed=None,
            created_at=NOW - timedelta(minutes=5),
        ),
        actor_id="system:answer-policy",
        reservation=PolicyScanReservation.for_calendar_month(
            reserved_scan=500,
            period_budget=100_000,
            at=NOW - timedelta(minutes=5),
        ),
    )
    request_service.transition(
        "tenant-a",
        "request-1",
        RequestState.VERIFYING,
        actor_id="system:answer-runtime",
        expected_revision=3,
    )
    return requests, request_service


def test_answer_and_delivered_transition_commit_in_one_transaction() -> None:
    delivery, repository, request_service, _ = atomic_service()

    answer = delivery.deliver(command())

    request = request_service.get("tenant-a", "request-1")
    assert request.state is RequestState.DELIVERED
    assert request.revision == 5
    assert repository.read("tenant-a", answer.answer_id) == answer
    transitions = request_service.list_transition_history("tenant-a", "request-1")
    assert tuple((item.from_state, item.to_state) for item in transitions[-2:]) == (
        (RequestState.EXECUTING, RequestState.VERIFYING),
        (RequestState.VERIFYING, RequestState.DELIVERED),
    )


def test_answer_insert_failure_rolls_back_request_transition_and_history() -> None:
    delivery, repository, request_service, requests = atomic_service()
    history = request_service.list_transition_history("tenant-a", "request-1")
    requests.connection.execute(
        "CREATE TRIGGER fail_governed_answer BEFORE INSERT ON governed_answers "
        "BEGIN SELECT RAISE(ABORT, 'forced answer failure'); END"
    )
    requests.connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced answer failure"):
        delivery.deliver(command())

    request = request_service.get("tenant-a", "request-1")
    assert request.state is RequestState.VERIFYING
    assert request.revision == 4
    assert request_service.list_transition_history("tenant-a", "request-1") == history
    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_concurrent_request_revision_change_rolls_back_answer() -> None:
    delivery, repository, request_service, _ = atomic_service()
    request_service.transition(
        "tenant-a",
        "request-1",
        RequestState.DELIVERED,
        actor_id="other-verifier",
        expected_revision=4,
    )

    with pytest.raises(GovernedAnswerStaleRevision, match="stale"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_verified_readable_result_creates_a_durable_governed_answer() -> None:
    delivery, repository, rechecker = service()

    answer = delivery.deliver(command())

    assert answer.answer_id != "pending"
    assert answer.execution_receipt_ref == "receipt-1"
    assert answer.result_digest == result().result_digest
    assert answer.product_generation_refs == (generation(),)
    assert answer.narrative == "For West, net revenue was 12.50."
    assert answer.narrative_source == "model"
    assert "suppressed_group_count" not in answer.model_dump()
    assert repository.read("tenant-a", answer.answer_id) == answer
    assert rechecker.calls == 1


def test_definition_answer_schema_keeps_null_result_linkage() -> None:
    delivery, _, _ = service()
    metric_answer = delivery.deliver(command())
    payload = metric_answer.model_dump(mode="python")
    payload.update(
        {
            "intent_kind": "definition",
            "execution_receipt_ref": None,
            "product_generation_refs": (),
            "result_ref": None,
            "result_digest": None,
        }
    )

    definition = GovernedAnswer.model_validate(payload, strict=True)

    assert definition.result_ref is None
    assert definition.result_digest is None


def test_authorized_request_read_returns_answer_metadata_without_reading_expired_result() -> None:
    executions = ExecutionReader()
    delivery, _, _ = service(executions=executions)
    expected = delivery.deliver(command())
    executions.snapshot = None

    answer = delivery.read_for_request("tenant-a", "requester-a", "request-1")

    assert answer == expected


def test_download_requires_separate_current_permission() -> None:
    creator, repository, _ = service()
    expected = creator.deliver(command())
    delivery = GovernedAnswerService(
        repository=repository,
        admission_reader=AdmissionReader(),
        execution_reader=ExecutionReader(),
        plan_reader=PlanReader(),
        authorization_rechecker=DownloadDeniedRechecker(),
        clock=lambda: NOW,
    )

    assert delivery.read_for_request("tenant-a", "requester-a", "request-1") == expected
    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.read_for_download("tenant-a", "requester-a", "request-1")


def test_revoked_request_read_is_denied_without_enumeration() -> None:
    creator, repository, _ = service()
    creator.deliver(command())
    delivery = GovernedAnswerService(
        repository=repository,
        admission_reader=AdmissionReader(),
        execution_reader=ExecutionReader(),
        plan_reader=PlanReader(),
        authorization_rechecker=RevokedRechecker(),
        clock=lambda: NOW,
    )

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.read_for_request("tenant-a", "requester-a", "request-1")


@pytest.mark.parametrize(
    ("tenant_id", "request_id"),
    [("tenant-b", "request-1"), ("tenant-a", "request-other")],
)
def test_cross_tenant_or_request_read_is_denied_without_enumeration(
    tenant_id: str, request_id: str
) -> None:
    delivery, _, _ = service()
    delivery.deliver(command())

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.read_for_request(tenant_id, "requester-a", request_id)


def test_another_requester_cannot_read_answer_metadata() -> None:
    delivery, _, _ = service()
    delivery.deliver(command())

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.read_for_request("tenant-a", "requester-b", "request-1")


def test_another_requester_cannot_read_the_answer() -> None:
    delivery, _, _ = service()
    delivery.deliver(command())

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.read_for_request("tenant-a", "requester-b", "request-1")


def test_exact_delivery_replay_returns_the_byte_identical_answer_without_rereading_result() -> None:
    execution_reader = ExecutionReader()
    delivery, _, rechecker, request_service, _ = _service_components(executions=execution_reader)
    expected = delivery.deliver(command())
    execution_reader.snapshot = None
    history = request_service.list_transition_history("tenant-a", "request-1")

    replayed = delivery.deliver(command())

    assert canonical_bytes(replayed) == canonical_bytes(expected)
    assert rechecker.calls == 2
    assert request_service.list_transition_history("tenant-a", "request-1") == history


def test_exact_delivery_replay_rechecks_revoked_authority_and_retains_the_record() -> None:
    creator, repository, _ = service()
    expected = creator.deliver(command())
    revoked = GovernedAnswerService(
        repository=repository,
        admission_reader=AdmissionReader(),
        execution_reader=ExecutionReader(),
        plan_reader=PlanReader(),
        authorization_rechecker=RevokedRechecker(),
        clock=lambda: NOW,
    )

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        revoked.deliver(command())

    assert repository.read("tenant-a", expected.answer_id) == expected


@pytest.mark.parametrize("refresh_ref", ["answer-missing", "answer-foreign"])
def test_refresh_rejects_missing_or_cross_tenant_answer_lineage(refresh_ref: str) -> None:
    delivery, repository, _, _, requests = _service_components()
    if refresh_ref == "answer-foreign":
        foreign_creator, _, _ = service()
        foreign = foreign_creator.deliver(command()).model_copy(
            update={
                "answer_id": refresh_ref,
                "tenant_id": "tenant-b",
                "request_id": "request-foreign",
            }
        )
        requests.connection.execute(
            "INSERT INTO governed_answers "
            "(answer_id, tenant_id, request_id, request_revision, command_digest, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                foreign.answer_id,
                foreign.tenant_id,
                foreign.request_id,
                foreign.request_revision,
                digest(foreign),
                canonical_bytes(foreign),
            ),
        )
        requests.connection.commit()

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.deliver(command(refreshes_answer_ref=refresh_ref))

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_conflicting_delivery_replay_is_rejected() -> None:
    delivery, _, _ = service()
    delivery.deliver(command())

    with pytest.raises(GovernedAnswerConflict, match="replay"):
        delivery.deliver(command(model_narrative="Different narrative."))


@pytest.mark.parametrize("tenant_id", ["tenant-b", "tenant-a"])
def test_unreadable_or_cross_tenant_result_is_denied_without_enumeration(tenant_id: str) -> None:
    executions = ExecutionReader()
    if tenant_id == "tenant-a":
        executions.snapshot = None
    delivery, repository, rechecker = service(executions=executions)

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        delivery.deliver(command(tenant_id=tenant_id))

    assert repository.list_for_request(tenant_id, "request-1") == ()
    assert rechecker.calls == 0


def test_failed_execution_receipt_never_creates_an_answer() -> None:
    failed = receipt().model_copy(
        update={
            "outcome": "ceiling_exceeded",
            "result_schema_digest": None,
            "result_digest": None,
            "result_ref": None,
        }
    )
    delivery, repository, _ = service(executions=ExecutionReader(execution_receipt=failed))

    with pytest.raises(GovernedAnswerVerificationError, match="successful execution"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_expired_result_never_creates_an_answer() -> None:
    snapshot = result().model_copy(update={"expires_at": NOW})
    delivery, repository, _ = service(executions=ExecutionReader(snapshot=snapshot))

    with pytest.raises(GovernedAnswerVerificationError, match="no longer readable"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


@pytest.mark.parametrize(
    ("kind", "replacement", "message"),
    [
        ("result_digest", "3" * 64, "result digest"),
        ("result_generation", (generation(8),), "generation"),
        ("receipt_rows", 11, "row ceiling"),
        ("receipt_bytes", 5000, "byte ceiling"),
        ("freshness", "freshness-other", "freshness"),
        ("quality", "quality-other", "quality"),
    ],
)
def test_mismatched_execution_evidence_is_rejected(
    kind: str, replacement: object, message: str
) -> None:
    execution_receipt = receipt()
    snapshot = result()
    authority = authorization()
    if kind == "result_digest":
        execution_receipt = execution_receipt.model_copy(update={"result_digest": replacement})
    elif kind == "result_generation":
        snapshot = snapshot.model_copy(update={"product_generation_refs": replacement})
    elif kind == "receipt_rows":
        execution_receipt = execution_receipt.model_copy(update={"row_count": replacement})
    elif kind == "receipt_bytes":
        execution_receipt = execution_receipt.model_copy(update={"byte_count": replacement})
    elif kind == "freshness":
        authority = authority.model_copy(update={"freshness_observation_ref": replacement})
    else:
        authority = authority.model_copy(update={"quality_observation_ref": replacement})
    delivery, repository, _ = service(
        executions=ExecutionReader(execution_receipt=execution_receipt, snapshot=snapshot),
        rechecker=Rechecker(authority),
    )

    with pytest.raises(GovernedAnswerVerificationError, match=message):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_plan_without_sql_level_suppression_is_rejected() -> None:
    unsuppressed = plan().model_copy(update={"statement": "SELECT SUM(net_revenue) FROM revenue"})
    delivery, repository, _ = service(plans=PlanReader(unsuppressed))

    with pytest.raises(GovernedAnswerVerificationError, match="suppression"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_plan_validation_digest_must_match_the_owning_admission() -> None:
    mismatched = plan().model_copy(update={"validation_digest": "9" * 64})
    delivery, repository, _ = service(plans=PlanReader(mismatched))

    with pytest.raises(GovernedAnswerVerificationError, match="validation digest"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


@pytest.mark.parametrize(
    "authority",
    [
        authorization(policy_current=False),
        authorization(entitlement_current=False),
        authorization(valid_until=NOW),
        authorization(policy_revision=2),
        authorization(entitlement_snapshot_digest="9" * 64),
    ],
)
def test_stale_policy_or_entitlement_recheck_is_rejected(
    authority: AnswerDeliveryAuthorization,
) -> None:
    delivery, repository, _ = service(rechecker=Rechecker(authority))

    with pytest.raises(GovernedAnswerVerificationError, match="policy and entitlement"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


@pytest.mark.parametrize(
    "authority",
    [
        authorization(freshness_disposition="stale"),
        authorization(material_quality_limitations=()),
    ],
)
def test_reviewed_freshness_and_quality_must_match_current_rechecks(
    authority: AnswerDeliveryAuthorization,
) -> None:
    delivery, repository, _ = service(rechecker=Rechecker(authority))

    with pytest.raises(GovernedAnswerVerificationError, match="freshness and quality"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


@pytest.mark.parametrize("draft_plan_digest", [None, "4" * 64])
def test_reviewed_factual_answer_requires_the_exact_cited_plan_digest(
    draft_plan_digest: str | None,
) -> None:
    reviewed = admission().model_copy(update={"reviewed_answer": reviewed_draft(draft_plan_digest)})
    delivery, repository, _ = service(admissions=AdmissionReader(reviewed))

    with pytest.raises(GovernedAnswerVerificationError, match=r"reviewed answer.*plan digest"):
        delivery.deliver(command())

    assert repository.list_for_request("tenant-a", "request-1") == ()


def test_unbound_legacy_answer_draft_omits_the_new_plan_citation() -> None:
    assert "plan_digest" not in reviewed_draft(None).model_dump()
    assert reviewed_draft().model_dump()["plan_digest"] == PLAN_DIGEST


@pytest.mark.parametrize(
    "unsupported_narrative",
    [
        "For West, net revenue was 999.",
        "For West, net revenue was 12.50 on 2026-08-31.",
        "For Acme Corporation, net revenue was 12.50.",
    ],
)
def test_unsupported_model_facts_are_discarded_for_a_deterministic_template(
    unsupported_narrative: str,
) -> None:
    delivery, _, _ = service()

    answer = delivery.deliver(command(model_narrative=unsupported_narrative))

    assert answer.narrative == "1 verified result row is available."
    assert answer.narrative_source == "template"
