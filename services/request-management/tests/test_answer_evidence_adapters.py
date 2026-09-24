from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from heinzel_compiler import GovernedQueryPlan
from heinzel_compiler.query_repository import SQLiteQueryPlanRepository
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    AnswerDeliveryAuthorization,
    AnswerExecutionEvidence,
    AnswerProductGenerationReference,
    DeliverGovernedAnswerCommand,
    GovernedAnswerNotVisible,
    GovernedAnswerService,
    GovernedAnswerVerificationError,
    InboxRequest,
    PolicyAdmissionReceipt,
    RepositoryAnswerExecutionReader,
    RepositoryAnswerPlanReader,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteGovernedAnswerRepository,
    SQLitePolicyAdmissionEvidenceReader,
    SQLiteRequestRepository,
    StakeholderQuestion,
    adapt_compiler_query_plan,
    adapt_runtime_execution_receipt,
    adapt_runtime_result_snapshot,
)
from heinzel_request_management.answer_admission import PolicyScanReservation
from heinzel_runtime import (
    AnswerExecutionReceipt,
    AnswerQueryCeilings,
    AnswerQueryColumn,
    AnswerQueryParameter,
    AnswerQueryPlan,
    AnswerQueryReference,
    AnswerQueryScan,
    AnswerQueryScanEstimate,
    AnswerResultSnapshot,
    QueryResultNotFound,
    SQLiteAnswerResultStore,
)
from heinzel_runtime import (
    AnswerProductGenerationReference as RuntimeGenerationReference,
)

NOW = datetime(2026, 9, 12, 7, tzinfo=UTC)


def runtime_plan() -> AnswerQueryPlan:
    generation = RuntimeGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product-revenue", version=4, digest="a" * 64),
        generation=7,
    )
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "query-plan-1",
        "tenant_id": "tenant-a",
        "validation_digest": "b" * 64,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (
            AnswerQueryReference(artifact_id="consumption-1", version=2, digest="c" * 64),
        ),
        "product_generation_refs": (generation,),
        "minimum_group_size": 5,
        "statement": (
            'SELECT "source"."region", SUM("source"."revenue") '
            'FROM "consumption"."sales" AS "source" GROUP BY "source"."region" '
            'HAVING COUNT(DISTINCT "source"."customer_id") >= %s LIMIT 10'
        ),
        "parameters": (AnswerQueryParameter(name="p0", value_type="integer", value=5),),
        "statement_digest": "",
        "parameter_digest": "",
        "estimated_scan": AnswerQueryScanEstimate(rows=10, bytes=500, estimator_version="pg-1"),
        "ceilings": AnswerQueryCeilings(
            row_limit=10,
            scan=AnswerQueryScan(rows=100, bytes=10_000),
            period_scan=AnswerQueryScan(rows=1_000, bytes=100_000),
        ),
        "routing": "per_question_review",
    }
    body["statement_digest"] = digest(body["statement"])
    body["parameter_digest"] = digest(body["parameters"])
    plan_digest = digest(body)
    return AnswerQueryPlan.model_validate(
        {**body, "plan_digest": plan_digest, "signature": f"signed:{plan_digest}"}
    )


def compiler_plan() -> GovernedQueryPlan:
    return GovernedQueryPlan.model_validate(runtime_plan().model_dump(mode="python"), strict=True)


def runtime_result(plan: AnswerQueryPlan | None = None) -> AnswerResultSnapshot:
    query_plan = plan or runtime_plan()
    columns = (
        AnswerQueryColumn(name="region", value_type="string"),
        AnswerQueryColumn(name="revenue", value_type="decimal"),
    )
    rows = (("West", Decimal("12.50")),)
    return AnswerResultSnapshot(
        result_ref="result-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=query_plan.plan_digest,
        product_generation_refs=query_plan.product_generation_refs,
        columns=columns,
        rows=rows,
        row_count=1,
        byte_count=len(canonical_bytes(rows)),
        result_schema_digest=digest(columns),
        result_digest=digest({"columns": columns, "rows": rows}),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def runtime_receipt(
    plan: AnswerQueryPlan | None = None, result: AnswerResultSnapshot | None = None
) -> AnswerExecutionReceipt:
    query_plan = plan or runtime_plan()
    snapshot = result or runtime_result(query_plan)
    return AnswerExecutionReceipt(
        receipt_id="receipt-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=query_plan.plan_digest,
        product_generation_refs=query_plan.product_generation_refs,
        attempt=1,
        started_at=NOW - timedelta(seconds=1),
        completed_at=NOW,
        outcome="succeeded",
        row_count=snapshot.row_count,
        byte_count=snapshot.byte_count,
        suppressed_group_count=0,
        result_schema_digest=snapshot.result_schema_digest,
        result_digest=snapshot.result_digest,
        result_ref=snapshot.result_ref,
        freshness_observation_ref="freshness-1",
        quality_observation_ref="quality-1",
    )


def expected_generations(
    plan: AnswerQueryPlan | None = None,
) -> tuple[AnswerProductGenerationReference, ...]:
    query_plan = plan or runtime_plan()
    return tuple(
        AnswerProductGenerationReference.model_validate(
            reference.model_dump(mode="python"), strict=True
        )
        for reference in query_plan.product_generation_refs
    )


def test_real_runtime_and_compiler_models_project_to_exact_delivery_evidence() -> None:
    runtime_query_plan = runtime_plan()
    compiled_query_plan = GovernedQueryPlan.model_validate(
        runtime_query_plan.model_dump(mode="python"), strict=True
    )
    snapshot = runtime_result(runtime_query_plan)
    receipt = runtime_receipt(runtime_query_plan, snapshot)
    generations = expected_generations(runtime_query_plan)

    receipt_evidence = adapt_runtime_execution_receipt(
        receipt,
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=runtime_query_plan.plan_digest,
        product_generation_refs=generations,
    )
    result_evidence = adapt_runtime_result_snapshot(
        snapshot,
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=runtime_query_plan.plan_digest,
        product_generation_refs=generations,
    )
    plan_evidence = adapt_compiler_query_plan(
        compiled_query_plan,
        tenant_id="tenant-a",
        plan_digest=runtime_query_plan.plan_digest,
        product_generation_refs=generations,
    )

    assert receipt_evidence.receipt_id == receipt.receipt_id
    assert result_evidence.result_digest == snapshot.result_digest
    assert isinstance(result_evidence.rows[0][1], Decimal)
    assert digest({"columns": result_evidence.columns, "rows": result_evidence.rows}) == digest(
        {"columns": snapshot.columns, "rows": snapshot.rows}
    )
    assert plan_evidence.plan_digest == compiled_query_plan.plan_digest
    assert plan_evidence.ceilings.row_limit == 10
    assert plan_evidence.ceilings.scan.bytes == 10_000
    assert plan_evidence.ceilings.period_scan.bytes == 100_000


def test_execution_classification_enum_exactly_matches_runtime_contract() -> None:
    runtime_property = AnswerExecutionReceipt.model_json_schema()["properties"][
        "provider_error_classification"
    ]
    evidence_property = AnswerExecutionEvidence.model_json_schema()["properties"][
        "provider_error_classification"
    ]

    assert _enum_values(evidence_property) == _enum_values(runtime_property)


def test_decimal_result_keeps_its_digest_through_runtime_storage_and_adapter() -> None:
    query_plan = runtime_plan()
    snapshot = runtime_result(query_plan)
    receipt = runtime_receipt(query_plan, snapshot)
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    store.record_attempt(input_digest="d" * 64, receipt=receipt, snapshot=snapshot)
    stored_snapshot = store.read_result("tenant-a", snapshot.result_ref)

    evidence = adapt_runtime_result_snapshot(
        stored_snapshot,
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest=query_plan.plan_digest,
        product_generation_refs=expected_generations(query_plan),
    )

    assert evidence.result_digest == snapshot.result_digest
    assert digest({"columns": evidence.columns, "rows": evidence.rows}) == snapshot.result_digest


def _enum_values(schema: object) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    values = schema.get("enum", ())
    return {str(value) for value in values} | set().union(
        *(_enum_values(value) for value in schema.values())
    )


@pytest.mark.parametrize("model_kind", ["receipt", "result"])
def test_runtime_adapter_denies_cross_tenant_or_request_evidence_without_enumeration(
    model_kind: str,
) -> None:
    query_plan = runtime_plan()
    snapshot = runtime_result(query_plan)

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        if model_kind == "receipt":
            adapt_runtime_execution_receipt(
                runtime_receipt(query_plan, snapshot),
                tenant_id="tenant-b",
                request_id="request-1",
                plan_digest=query_plan.plan_digest,
                product_generation_refs=expected_generations(query_plan),
            )
        else:
            adapt_runtime_result_snapshot(
                snapshot,
                tenant_id="tenant-b",
                request_id="request-1",
                plan_digest=query_plan.plan_digest,
                product_generation_refs=expected_generations(query_plan),
            )


def test_compiler_adapter_denies_cross_tenant_plan_without_enumeration() -> None:
    query_plan = runtime_plan()

    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        adapt_compiler_query_plan(
            compiler_plan(),
            tenant_id="tenant-b",
            plan_digest=query_plan.plan_digest,
            product_generation_refs=expected_generations(query_plan),
        )


@pytest.mark.parametrize("mismatch", ["digest", "generation"])
def test_all_adapters_reject_mismatched_plan_or_generation_authority(mismatch: str) -> None:
    runtime_query_plan = runtime_plan()
    snapshot = runtime_result(runtime_query_plan)
    receipt = runtime_receipt(runtime_query_plan, snapshot)
    expected_digest = "f" * 64 if mismatch == "digest" else runtime_query_plan.plan_digest
    expected = (
        (
            AnswerProductGenerationReference(
                product_ref=expected_generations(runtime_query_plan)[0].product_ref,
                generation=8,
            ),
        )
        if mismatch == "generation"
        else expected_generations(runtime_query_plan)
    )

    with pytest.raises(GovernedAnswerVerificationError, match=r"digest|generation"):
        adapt_runtime_execution_receipt(
            receipt,
            tenant_id="tenant-a",
            request_id="request-1",
            plan_digest=expected_digest,
            product_generation_refs=expected,
        )
    with pytest.raises(GovernedAnswerVerificationError, match=r"digest|generation"):
        adapt_runtime_result_snapshot(
            snapshot,
            tenant_id="tenant-a",
            request_id="request-1",
            plan_digest=expected_digest,
            product_generation_refs=expected,
        )
    with pytest.raises(GovernedAnswerVerificationError, match=r"digest|generation"):
        adapt_compiler_query_plan(
            compiler_plan(),
            tenant_id="tenant-a",
            plan_digest=expected_digest,
            product_generation_refs=expected,
        )


class _CurrentAuthority:
    def __init__(self, plan_digest: str) -> None:
        self._plan_digest = plan_digest

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> AnswerDeliveryAuthorization:
        return AnswerDeliveryAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            requester_id=requester_id,
            request_revision=4,
            plan_digest=plan_digest,
            policy_id="policy-1",
            policy_revision=3,
            policy_snapshot_digest="6" * 64,
            entitlement_snapshot_digest="7" * 64,
            policy_current=True,
            entitlement_current=True,
            valid_until=NOW + timedelta(minutes=30),
            row_ceiling=10,
            byte_ceiling=10_000,
            minimum_group_size=5,
            freshness_observation_ref="freshness-1",
            quality_observation_ref="quality-1",
            freshness_disposition="current",
            metric_version_refs=(
                ArtifactReference(artifact_id="metric-revenue", version=1, digest="8" * 64),
            ),
            material_quality_limitations=(),
            lineage_refs=(),
            as_of=NOW,
            restatement="Revenue by region.",
            approved_narrative_terms=("revenue", "region"),
        )


def _request_authority(
    plan_digest: str,
) -> tuple[SQLiteRequestRepository, SQLiteAnswerAdmissionRepository, RequestManagementService]:
    requests = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(requests, clock=lambda: NOW)
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
    investigating = service.transition(
        "tenant-a",
        "request-1",
        RequestState.INVESTIGATING,
        actor_id="architect-a",
        expected_revision=1,
    )
    admissions = SQLiteAnswerAdmissionRepository(requests)
    admissions.record_and_transition(
        PolicyAdmissionReceipt(
            admission_id="admission-1",
            tenant_id="tenant-a",
            request_id="request-1",
            request_revision=investigating.revision,
            validation_digest="b" * 64,
            restatement_acceptance_ref="restatement-1",
            plan_digest=plan_digest,
            policy_id="policy-1",
            policy_revision=3,
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
    service.transition(
        "tenant-a",
        "request-1",
        RequestState.VERIFYING,
        actor_id="system:answer-runtime",
        expected_revision=3,
    )
    return requests, admissions, service


def test_policy_admission_projection_preserves_receipt_revision_and_proves_verifying_lineage() -> (
    None
):
    query_plan = runtime_plan()
    requests, admissions, _ = _request_authority(query_plan.plan_digest)
    reader = SQLitePolicyAdmissionEvidenceReader(admissions, requests)

    evidence = reader.read_admission("tenant-a", "request-1", "admission-1")

    assert evidence is not None
    assert evidence.admission_request_revision == 2
    assert evidence.verifying_request_revision == 4
    assert evidence.validation_digest == "b" * 64
    assert evidence.policy_revision == 3
    assert evidence.entitlement_snapshot_digest == "7" * 64
    assert reader.read_admission("tenant-b", "request-1", "admission-1") is None


def test_policy_admission_projection_rejects_missing_executing_to_verifying_lineage() -> None:
    query_plan = runtime_plan()
    requests, admissions, _ = _request_authority(query_plan.plan_digest)
    requests.connection.execute(
        "DELETE FROM transition_events WHERE tenant_id = ? AND request_id = ? "
        "AND request_revision = ?",
        ("tenant-a", "request-1", 4),
    )
    requests.connection.commit()

    with pytest.raises(GovernedAnswerVerificationError, match="exact verifying transition"):
        SQLitePolicyAdmissionEvidenceReader(admissions, requests).read_admission(
            "tenant-a", "request-1", "admission-1"
        )


def test_repository_readers_deny_mismatched_plan_request_and_receipt_bindings() -> None:
    query_plan = runtime_plan()
    plan_repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    plan_repository.save(compiler_plan())
    snapshot = runtime_result(query_plan)
    execution_receipt = runtime_receipt(query_plan, snapshot)
    result_store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    result_store.record_attempt(
        input_digest="d" * 64,
        receipt=execution_receipt,
        snapshot=snapshot,
    )
    execution_reader = RepositoryAnswerExecutionReader(
        result_store, missing_result_error=QueryResultNotFound
    )

    assert (
        RepositoryAnswerPlanReader(plan_repository).read_plan("tenant-b", query_plan.plan_digest)
        is None
    )
    assert (
        execution_reader.read_receipt(
            "tenant-a",
            "request-other",
            execution_receipt.receipt_id,
            query_plan.plan_digest,
            expected_generations(query_plan),
        )
        is None
    )
    assert (
        execution_reader.read_receipt(
            "tenant-a",
            "request-1",
            "receipt-other",
            query_plan.plan_digest,
            expected_generations(query_plan),
        )
        is None
    )
    with pytest.raises(GovernedAnswerVerificationError, match="plan digest"):
        execution_reader.read_receipt(
            "tenant-a",
            "request-1",
            execution_receipt.receipt_id,
            "f" * 64,
            expected_generations(query_plan),
        )


def test_repository_backed_real_model_chain_delivers_once_and_denies_other_tenant() -> None:
    query_plan = runtime_plan()
    compiled_plan = compiler_plan()
    snapshot = runtime_result(query_plan)
    execution_receipt = runtime_receipt(query_plan, snapshot)
    requests, admissions, request_service = _request_authority(query_plan.plan_digest)
    plan_repository = SQLiteQueryPlanRepository(sqlite3.connect(":memory:"))
    plan_repository.save(compiled_plan)
    result_store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    result_store.record_attempt(
        input_digest="d" * 64,
        receipt=execution_receipt,
        snapshot=snapshot,
    )
    service = GovernedAnswerService(
        repository=SQLiteGovernedAnswerRepository(requests),
        admission_reader=SQLitePolicyAdmissionEvidenceReader(admissions, requests),
        execution_reader=RepositoryAnswerExecutionReader(
            result_store, missing_result_error=QueryResultNotFound
        ),
        plan_reader=RepositoryAnswerPlanReader(plan_repository),
        authorization_rechecker=_CurrentAuthority(query_plan.plan_digest),
        clock=lambda: NOW,
    )
    command = DeliverGovernedAnswerCommand(
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=4,
        requester_id="requester-a",
        actor_id="system:answer-verifier",
        admission_ref="admission-1",
        execution_receipt_ref="receipt-1",
    )

    first = service.deliver(command)
    replay = service.deliver(command)

    assert replay == first
    assert first.result_digest == snapshot.result_digest
    assert request_service.get("tenant-a", "request-1").state is RequestState.DELIVERED
    with pytest.raises(GovernedAnswerNotVisible, match="answer delivery authority is not visible"):
        service.read_for_request("tenant-b", "requester-a", "request-1")


@pytest.mark.parametrize(
    ("failure", "returns_missing"),
    [(QueryResultNotFound("expired"), True), (RuntimeError("programmer failure"), False)],
)
def test_result_reader_only_maps_the_injected_missing_result_error(
    failure: RuntimeError, returns_missing: bool
) -> None:
    class _FailingStore:
        def load_execution(
            self, tenant_id: str, request_id: str
        ) -> tuple[str, AnswerExecutionReceipt] | None:
            return None

        def read_result(self, tenant_id: str, result_ref: str) -> AnswerResultSnapshot:
            raise failure

    reader = RepositoryAnswerExecutionReader(
        _FailingStore(), missing_result_error=QueryResultNotFound
    )

    def call() -> object:
        return reader.read_result(
            "tenant-a",
            "request-1",
            "result-1",
            runtime_plan().plan_digest,
            expected_generations(),
        )

    if returns_missing:
        assert call() is None
    else:
        with pytest.raises(RuntimeError, match="programmer failure"):
            call()
