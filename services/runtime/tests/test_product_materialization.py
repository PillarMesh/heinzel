from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_contract_model import digest
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from heinzel_runtime.product_input_cardinality import (
    ProductInputCardinalityEvidence,
    ProductInputReceiptCardinality,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from heinzel_runtime.product_materialization import (
    CatalogPublicationError,
    MaterializationAuthorityError,
    MaterializationObservation,
    MaterializationRequest,
    ProductMaterializationAdmission,
    ProductMaterializationReceipt,
    ProductMaterializationRunner,
    PublicationRecoveryCommand,
    PublicationRecoveryNotAllowedError,
    StalePublicationRevisionError,
)
from heinzel_state import (
    IncidentRecord,
    RecoveryCommand,
    RecoveryCommandService,
    RunService,
    SQLiteIncidentRepository,
    SQLiteRunRepository,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
_LEGALITY_DECISION_DIGEST = "8" * 64
_AUTHORIZATION_SIGNER = ProductExecutionAuthorizationSigner.generate("runtime-authority-1")


def _physical_plan() -> ProductPhysicalPlan:
    statement = 'SELECT 1 AS "revenue_total"'
    return ProductPhysicalPlan(
        compiler_version="compiler-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        contract_ref="contract-a",
        contract_revision=7,
        contract_digest="a" * 64,
        iir_digest="4" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=1,
        provider_observation_digest="5" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="revenue",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="6" * 64,
            landing_receipt_digest="c" * 64,
            observed_source_schema_digest="7" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="amount",
                    json_field="amount",
                    scalar_type="decimal",
                ),
            ),
        ),
        target=ProductTarget(namespace="products", relation_name="revenue"),
        emitted_statement=statement,
        statement_digest=digest(statement),
        output_columns=("revenue_total",),
        expected_output_schema_digest="d" * 64,
        decimal_output_checks=(Decimal57OutputCheck(column_name="revenue_total"),),
    )


def _authorization_verifier() -> ProductExecutionAuthorizationVerifier:
    return ProductExecutionAuthorizationVerifier(
        {"runtime-authority-1": _AUTHORIZATION_SIGNER.public_key}
    )


def _admission(
    *,
    legality_decision_digest: str = _LEGALITY_DECISION_DIGEST,
    cardinality_evidence_digest: str | None = None,
    issued_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=15),
) -> ProductMaterializationAdmission:
    plan = _physical_plan()
    cardinality_digest = cardinality_evidence_digest or digest(_CARDINALITY_EVIDENCE)
    return ProductMaterializationAdmission(
        legality_decision_digest=legality_decision_digest,
        cardinality_evidence_digest=cardinality_digest,
        signed_execution_authorization=_AUTHORIZATION_SIGNER.sign(
            physical_plan=plan,
            legality_decision_digest=legality_decision_digest,
            cardinality_evidence_digest=cardinality_digest,
            issued_at=issued_at,
            expires_at=expires_at,
        ),
    )


def _cardinality_evidence(
    *,
    tenant_id: str = "tenant-a",
    contract_digest: str = "a" * 64,
    plan_digest: str | None = None,
    input_generation_digests: tuple[str, ...] = ("c" * 64,),
) -> ProductInputCardinalityEvidence:
    receipts = tuple(
        ProductInputReceiptCardinality(
            generation_id=f"{index + 1:x}" * 64,
            receipt_digest=receipt_digest,
            record_count=3,
        )
        for index, receipt_digest in enumerate(input_generation_digests)
    )
    total = sum(item.record_count for item in receipts)
    return ProductInputCardinalityEvidence(
        tenant_id=tenant_id,
        contract_ref="contract-a",
        contract_revision=7,
        contract_digest=contract_digest,
        product_plan_digest=plan_digest or digest(_physical_plan()),
        relation_ref="raw_revenue",
        generation_ids=tuple(item.generation_id for item in receipts),
        receipts=receipts,
        total_contributing_row_ceiling=total,
        policy_maximum_contributing_rows=100,
        maximum_scaled_sum=total * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=NOW,
    )


_CARDINALITY_EVIDENCE = _cardinality_evidence()


def _cardinality_repository() -> SQLiteProductInputCardinalityEvidenceRepository:
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    repository.record(_CARDINALITY_EVIDENCE)
    return repository


class _CardinalityEvidenceReader:
    def __init__(self, evidence: ProductInputCardinalityEvidence | None) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, str]] = []

    def read(
        self,
        *,
        tenant_id: str,
        evidence_digest: str,
    ) -> ProductInputCardinalityEvidence | None:
        self.calls.append((tenant_id, evidence_digest))
        return self.evidence


class _Warehouse:
    def __init__(self) -> None:
        self.executions = 0
        self.switches = 0

    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        self.executions += 1
        return MaterializationObservation(
            provider_commit_reference="commit-1",
            output_schema_digest=request.expected_output_schema_digest,
            output_row_count=3,
            dbt_manifest_digest="1" * 64,
            dbt_run_results_digest="2" * 64,
            lineage_digest="3" * 64,
            quality_assertion_count=0,
            quality_disposition="not_asserted",
        )

    def switch_consumption_view(
        self, request: MaterializationRequest, observation: MaterializationObservation
    ) -> None:
        del request, observation
        self.switches += 1


class _Catalog:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.receipts: list[ProductMaterializationReceipt] = []

    def publish(
        self, request: MaterializationRequest, receipt: ProductMaterializationReceipt
    ) -> str:
        del request
        self.calls += 1
        self.receipts.append(receipt)
        if self.fail:
            raise CatalogPublicationError("catalog unavailable")
        return "catalog-publication-1"


class _SchemaMismatchWarehouse(_Warehouse):
    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        self.executions += 1
        return MaterializationObservation(
            provider_commit_reference="commit-1",
            output_schema_digest="e" * 64,
            output_row_count=3,
            dbt_manifest_digest="1" * 64,
            dbt_run_results_digest="2" * 64,
            lineage_digest="3" * 64,
            quality_assertion_count=0,
            quality_disposition="not_asserted",
        )


def _request() -> MaterializationRequest:
    return MaterializationRequest(
        run_id="run-1",
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        product_generation=1,
        retention_seconds=3600,
        contract_digest="a" * 64,
        physical_plan=_physical_plan(),
        physical_plan_digest=digest(_physical_plan()),
        compiled_model_digest="b" * 64,
        input_generation_digests=("c" * 64,),
        input_cardinality_evidence_digest=digest(_CARDINALITY_EVIDENCE),
        expected_output_schema_digest="d" * 64,
    )


def _runner_with_observed_effects() -> tuple[
    ProductMaterializationRunner,
    _Warehouse,
    _Catalog,
    _CardinalityEvidenceReader,
]:
    warehouse = _Warehouse()
    catalog = _Catalog()
    reader = _CardinalityEvidenceReader(_CARDINALITY_EVIDENCE)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        cardinality_evidence_reader=reader,
        execution_authorization_verifier=_authorization_verifier(),
        clock=lambda: NOW,
    )
    return runner, warehouse, catalog, reader


def test_valid_execution_authorization_admits_materialization() -> None:
    runner, warehouse, catalog, reader = _runner_with_observed_effects()

    result = runner.materialize(_request(), admission=_admission())

    assert result.publication_pending is False
    assert reader.calls == [("tenant-a", digest(_CARDINALITY_EVIDENCE))]
    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 1


def test_invalid_execution_authorization_fails_before_every_external_effect() -> None:
    runner, warehouse, catalog, reader = _runner_with_observed_effects()
    admission = _admission()
    invalid = admission.model_copy(
        update={
            "signed_execution_authorization": admission.signed_execution_authorization.model_copy(
                update={"signature": "AAAA"}
            )
        }
    )

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(_request(), admission=invalid)

    assert reader.calls == []
    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_execution_admission_with_an_undeclared_field_fails_before_every_external_effect() -> None:
    runner, warehouse, catalog, reader = _runner_with_observed_effects()
    admission = _admission().model_copy(update={"undeclared": "value"})

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(_request(), admission=admission)

    assert reader.calls == []
    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


@pytest.mark.parametrize(
    ("materialization_request", "admission"),
    [
        (
            _request().model_copy(
                update={
                    "physical_plan": _physical_plan().model_copy(update={"product_revision": 2}),
                    "physical_plan_digest": digest(
                        _physical_plan().model_copy(update={"product_revision": 2})
                    ),
                }
            ),
            _admission(),
        ),
        (
            _request(),
            _admission().model_copy(update={"legality_decision_digest": "9" * 64}),
        ),
        (
            _request(),
            _admission().model_copy(update={"cardinality_evidence_digest": "9" * 64}),
        ),
    ],
    ids=("physical-plan", "legality-decision", "cardinality-evidence"),
)
def test_execution_authorization_digest_mismatch_fails_before_every_external_effect(
    materialization_request: MaterializationRequest,
    admission: ProductMaterializationAdmission,
) -> None:
    runner, warehouse, catalog, reader = _runner_with_observed_effects()

    with pytest.raises(MaterializationAuthorityError, match="authorization"):
        runner.materialize(materialization_request, admission=admission)

    assert reader.calls == []
    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


@pytest.mark.parametrize(
    "admission",
    [
        _admission(issued_at=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(minutes=15)),
        _admission(issued_at=NOW - timedelta(minutes=15), expires_at=NOW),
    ],
    ids=("future", "expired-at-boundary"),
)
def test_inactive_execution_authorization_fails_before_every_external_effect(
    admission: ProductMaterializationAdmission,
) -> None:
    runner, warehouse, catalog, reader = _runner_with_observed_effects()

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(_request(), admission=admission)

    assert reader.calls == []
    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_missing_cardinality_authority_fails_before_every_external_effect() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog()
    reader = _CardinalityEvidenceReader(None)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        clock=lambda: NOW,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=reader,
    )

    with pytest.raises(MaterializationAuthorityError, match="cardinality evidence"):
        runner.materialize(_request(), admission=_admission())

    assert reader.calls == [("tenant-a", digest(_CARDINALITY_EVIDENCE))]
    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_unavailable_cardinality_reader_fails_before_every_external_effect() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog()
    repository = _cardinality_repository()
    repository._connection.close()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        clock=lambda: NOW,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=repository,
    )

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(_request(), admission=_admission())

    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


@pytest.mark.parametrize(
    "evidence",
    [
        _cardinality_evidence(tenant_id="tenant-b"),
        _cardinality_evidence(contract_digest="e" * 64),
        _cardinality_evidence(plan_digest="f" * 64),
        _cardinality_evidence(input_generation_digests=("9" * 64,)),
    ],
    ids=("tenant", "contract", "plan", "input-generations"),
)
def test_mismatched_cardinality_evidence_fails_before_every_external_effect(
    evidence: ProductInputCardinalityEvidence,
) -> None:
    warehouse = _Warehouse()
    catalog = _Catalog()
    request = _request().model_copy(update={"input_cardinality_evidence_digest": digest(evidence)})
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        clock=lambda: NOW,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_CardinalityEvidenceReader(evidence),
    )

    with pytest.raises(MaterializationAuthorityError, match="does not match"):
        runner.materialize(
            request,
            admission=_admission(cardinality_evidence_digest=digest(evidence)),
        )

    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_invalid_cardinality_policy_invariants_fail_before_every_external_effect() -> None:
    invalid = _CARDINALITY_EVIDENCE.model_copy(
        update={
            "policy_maximum_contributing_rows": 2,
            "decimal_input_precision": 37,
        }
    )
    request = _request().model_copy(update={"input_cardinality_evidence_digest": digest(invalid)})
    warehouse = _Warehouse()
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        clock=lambda: NOW,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_CardinalityEvidenceReader(invalid),
    )

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(
            request,
            admission=_admission(cardinality_evidence_digest=digest(invalid)),
        )

    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_substituted_cardinality_digest_fails_before_every_external_effect() -> None:
    substituted = _CARDINALITY_EVIDENCE.model_copy(update={"authority_ref": "substituted"})
    warehouse = _Warehouse()
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        clock=lambda: NOW,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_CardinalityEvidenceReader(substituted),
    )

    with pytest.raises(MaterializationAuthorityError, match="does not match"):
        runner.materialize(_request(), admission=_admission())

    assert warehouse.executions == 0
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_commits_validates_switches_and_publishes_one_exact_product() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    result = runner.materialize(_request(), admission=_admission())

    assert result.receipt.output_row_count == 3
    assert result.receipt.provider_commit_reference == "commit-1"
    assert (
        result.receipt.input_cardinality_evidence_digest
        == _request().input_cardinality_evidence_digest
    )
    assert result.publication_ref == "catalog-publication-1"
    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 1
    assert catalog.receipts == [result.receipt]


def test_read_receipt_classifies_corrupt_persisted_authority() -> None:
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    runner.materialize(_request(), admission=_admission())
    runner._connection.execute(
        "UPDATE product_materializations_v4 SET payload = ?",
        (b"not-json",),
    )
    runner._connection.commit()

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.read_receipt(
            tenant_id="tenant-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
        )


def test_catalog_failure_retries_publication_without_rerunning_transform() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog(fail=True)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    first = runner.materialize(_request(), admission=_admission())

    assert first.publication_ref is None
    assert first.publication_pending is True
    catalog.fail = False
    replay = runner.materialize(_request(), admission=_admission())
    assert replay.receipt == first.receipt
    assert replay.publication_ref == "catalog-publication-1"
    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 2


def test_public_recovery_retries_only_catalog_publication_and_records_actor_reason() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog(fail=True)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    pending = runner.materialize(request, admission=_admission())
    status = runner.read_publication_recovery(
        tenant_id=request.tenant_id,
        materialization_key=request.materialization_key,
    )
    catalog.fail = False

    evidence = runner.retry_publication(
        PublicationRecoveryCommand(
            command_id="publication-recovery-a",
            tenant_id=request.tenant_id,
            materialization_key=request.materialization_key,
            expected_revision=status.revision,
            actor_id="architect-a",
            reason="Catalog connectivity is healthy again.",
        )
    )

    assert pending.publication_pending is True
    assert evidence.actor_id == "architect-a"
    assert evidence.reason == "Catalog connectivity is healthy again."
    assert evidence.publication_ref == "catalog-publication-1"
    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 2
    recovered = runner.read_publication_recovery(
        tenant_id=request.tenant_id,
        materialization_key=request.materialization_key,
    )
    assert recovered.state == "published"
    assert recovered.revision == status.revision + 1


def test_publication_recovery_fails_closed_when_cardinality_authority_disappears() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog(fail=True)
    reader = _CardinalityEvidenceReader(_CARDINALITY_EVIDENCE)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=reader,
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())
    reader.evidence = None

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.retry_publication(
            PublicationRecoveryCommand(
                command_id="publication-recovery-a",
                tenant_id=request.tenant_id,
                materialization_key=request.materialization_key,
                expected_revision=1,
                actor_id="architect-a",
                reason="Catalog connectivity is healthy again.",
            )
        )

    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 1


def test_public_recovery_rejects_stale_revision_without_catalog_effect() -> None:
    catalog = _Catalog(fail=True)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())
    catalog.fail = False

    with pytest.raises(StalePublicationRevisionError):
        runner.retry_publication(
            PublicationRecoveryCommand(
                command_id="publication-recovery-a",
                tenant_id=request.tenant_id,
                materialization_key=request.materialization_key,
                expected_revision=2,
                actor_id="architect-a",
                reason="Catalog connectivity is healthy again.",
            )
        )

    assert catalog.calls == 1


def test_public_recovery_is_idempotent_and_conflicting_replay_is_denied() -> None:
    catalog = _Catalog(fail=True)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())
    catalog.fail = False
    command = PublicationRecoveryCommand(
        command_id="publication-recovery-a",
        tenant_id=request.tenant_id,
        materialization_key=request.materialization_key,
        expected_revision=1,
        actor_id="architect-a",
        reason="Catalog connectivity is healthy again.",
    )

    first = runner.retry_publication(command)
    replay = runner.retry_publication(command)

    assert replay == first
    assert catalog.calls == 2
    with pytest.raises(PublicationRecoveryNotAllowedError, match="conflicts"):
        runner.retry_publication(command.model_copy(update={"reason": "Different reason."}))


def test_public_recovery_denies_cross_tenant_and_already_published_materialization() -> None:
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())

    with pytest.raises(LookupError, match="unavailable"):
        runner.read_publication_recovery(
            tenant_id="tenant-b", materialization_key=request.materialization_key
        )
    with pytest.raises(PublicationRecoveryNotAllowedError, match="not pending"):
        runner.retry_publication(
            PublicationRecoveryCommand(
                command_id="publication-recovery-a",
                tenant_id=request.tenant_id,
                materialization_key=request.materialization_key,
                expected_revision=2,
                actor_id="architect-a",
                reason="Retry publication.",
            )
        )


def test_state_recovery_command_reconciles_the_runtime_publication_boundary(
    tmp_path: Path,
) -> None:
    catalog = _Catalog(fail=True)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())
    status = runner.read_publication_recovery(
        tenant_id=request.tenant_id,
        materialization_key=request.materialization_key,
    )
    catalog.fail = False
    incidents = SQLiteIncidentRepository(tmp_path / "incidents.sqlite3")
    incidents.append(
        IncidentRecord(
            incident_id="catalog-pending-a",
            tenant_id=request.tenant_id,
            revision=1,
            kind="catalog_pending",
            classification="ambiguous_outcome",
            last_successful_stage="transform",
            failed_stage="catalog_publication",
            user_impact="The committed product is not visible in the managed catalog.",
            next_automatic_action="reconcile_external_effect",
            allowed_operator_actions=("reconcile_external_effect",),
            source_service="runtime",
            source_record_ref=request.materialization_key,
            source_record_revision=status.revision,
            evidence_refs=("materialization-receipt-a",),
            opened_at=NOW,
            updated_at=NOW,
        ),
        expected_current_revision=0,
    )
    recovery = RecoveryCommandService(
        incidents,
        run_service=RunService(SQLiteRunRepository(tmp_path / "runs.sqlite3"), clock=lambda: NOW),
        clock=lambda: NOW,
        external_effects=runner,
    )

    evidence = recovery.execute(
        RecoveryCommand(
            command_id="catalog-recovery-a",
            tenant_id=request.tenant_id,
            incident_id="catalog-pending-a",
            expected_incident_revision=1,
            action="reconcile_external_effect",
            actor_id="architect-a",
            reason="The managed catalog is healthy again.",
        )
    )

    assert evidence.resulting_record_ref == "catalog-publication-1"
    assert (
        runner.read_publication_recovery(
            tenant_id=request.tenant_id,
            materialization_key=request.materialization_key,
        ).state
        == "published"
    )


def test_schema_mismatch_never_switches_or_publishes() -> None:
    warehouse = _SchemaMismatchWarehouse()
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="output schema"):
        runner.materialize(_request(), admission=_admission())

    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_replay_with_changed_authority_is_rejected() -> None:
    warehouse = _Warehouse()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())

    changed = request.model_copy(update={"compiled_model_digest": "f" * 64})
    with pytest.raises(ValueError, match="materialization replay conflicts"):
        runner.materialize(changed, admission=_admission())


def test_replay_with_another_valid_cardinality_evidence_digest_is_rejected() -> None:
    warehouse = _Warehouse()
    reader = _CardinalityEvidenceReader(_CARDINALITY_EVIDENCE)
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=reader,
        clock=lambda: NOW,
    )
    request = _request()
    runner.materialize(request, admission=_admission())
    replacement = _CARDINALITY_EVIDENCE.model_copy(update={"authority_ref": "replacement"})
    reader.evidence = replacement
    changed = request.model_copy(update={"input_cardinality_evidence_digest": digest(replacement)})

    with pytest.raises(ValueError, match="materialization replay conflicts"):
        runner.materialize(changed, admission=_admission())

    assert warehouse.executions == 1


def test_receipt_digest_is_stable_on_replay() -> None:
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )

    first = runner.materialize(_request(), admission=_admission())
    second = runner.materialize(_request(), admission=_admission())

    assert digest(first.receipt) == digest(second.receipt)
    assert first.receipt.execution_authorization_digest == digest(
        _admission().signed_execution_authorization
    )
    assert first.receipt.legality_decision_digest == _LEGALITY_DECISION_DIGEST


def test_replay_rejects_a_different_trusted_authorization_envelope() -> None:
    alternate_signer = ProductExecutionAuthorizationSigner.generate("runtime-authority-2")
    verifier = ProductExecutionAuthorizationVerifier(
        {
            "runtime-authority-1": _AUTHORIZATION_SIGNER.public_key,
            "runtime-authority-2": alternate_signer.public_key,
        }
    )
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=_Catalog(),
        execution_authorization_verifier=verifier,
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    request = _request()
    admission = _admission()
    runner.materialize(request, admission=admission)
    substituted = admission.model_copy(
        update={
            "signed_execution_authorization": alternate_signer.sign(
                physical_plan=request.physical_plan,
                legality_decision_digest=admission.legality_decision_digest,
                cardinality_evidence_digest=admission.cardinality_evidence_digest,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=15),
            )
        }
    )

    with pytest.raises(ValueError, match="recorded execution admission"):
        runner.materialize(request, admission=substituted)


def test_durable_admission_allows_view_and_catalog_recovery_after_expiry() -> None:
    current_time = [NOW]
    fail_after_receipt = [True]
    warehouse = _Warehouse()
    catalog = _Catalog()

    def fault_hook(checkpoint: str) -> None:
        if checkpoint == "after_commit_receipt" and fail_after_receipt.pop():
            raise RuntimeError("simulated crash")

    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: current_time[0],
        fault_hook=fault_hook,
    )
    request = _request()
    admission = _admission(expires_at=NOW + timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner.materialize(request, admission=admission)
    current_time[0] = NOW + timedelta(seconds=1)

    recovered = runner.materialize(request, admission=admission)

    assert recovered.publication_pending is False
    assert warehouse.executions == 1
    assert warehouse.switches == 1
    assert catalog.calls == 1


def test_replay_reverifies_a_stored_and_resubmitted_admission_before_view_switch() -> None:
    fail_after_receipt = [True]
    warehouse = _Warehouse()
    catalog = _Catalog()

    def fault_hook(checkpoint: str) -> None:
        if checkpoint == "after_commit_receipt" and fail_after_receipt.pop():
            raise RuntimeError("simulated crash")

    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
        fault_hook=fault_hook,
    )
    request = _request()
    admission = _admission()
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner.materialize(request, admission=admission)

    corrupt_signed = admission.signed_execution_authorization.model_copy(
        update={"signature": "AAAA"}
    )
    corrupt_admission = admission.model_copy(
        update={"signed_execution_authorization": corrupt_signed}
    )
    payload_row = runner._connection.execute(
        "SELECT payload FROM product_materializations_v4"
    ).fetchone()
    assert payload_row is not None
    payload = json.loads(bytes(payload_row[0]))
    payload["admission"] = corrupt_admission.model_dump(mode="json")
    payload["receipt"]["execution_authorization_digest"] = digest(corrupt_signed)
    runner._connection.execute(
        "UPDATE product_materializations_v4 SET payload = ?",
        (json.dumps(payload).encode(),),
    )

    with pytest.raises(MaterializationAuthorityError, match="invalid or unavailable"):
        runner.materialize(request, admission=corrupt_admission)

    assert warehouse.executions == 1
    assert warehouse.switches == 0
    assert catalog.calls == 0


def test_receipt_reader_returns_only_the_exact_recorded_generation() -> None:
    runner = ProductMaterializationRunner.in_memory(
        warehouse=_Warehouse(),
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    result = runner.materialize(_request(), admission=_admission())

    exact = runner.read_receipt(
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        product_generation=1,
    )
    absent = runner.read_receipt(
        tenant_id="tenant-a",
        product_id="product-revenue",
        product_revision=1,
        product_generation=2,
    )

    assert exact == result.receipt
    assert absent is None


def test_materialization_ledger_retains_multiple_generations_of_one_product_revision() -> None:
    warehouse = _Warehouse()
    catalog = _Catalog()
    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=catalog,
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
    )
    first_request = _request()
    second_request = first_request.model_copy(update={"run_id": "run-2", "product_generation": 2})

    first = runner.materialize(first_request, admission=_admission())
    second = runner.materialize(second_request, admission=_admission())

    assert first.receipt.product_generation == 1
    assert second.receipt.product_generation == 2
    assert (
        runner.read_receipt(
            tenant_id="tenant-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
        )
        == first.receipt
    )
    assert (
        runner.read_receipt(
            tenant_id="tenant-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=2,
        )
        == second.receipt
    )
    assert warehouse.executions == 2
    assert warehouse.switches == 2
    assert catalog.calls == 2


def test_materialization_ledger_refuses_nonempty_legacy_identity_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE product_materializations ("
        "materialization_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
        "product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, payload BLOB NOT NULL, "
        "UNIQUE (tenant_id, product_id, product_revision))"
    )
    connection.execute(
        "INSERT INTO product_materializations VALUES (?, ?, ?, ?, ?)",
        ("legacy-key", "tenant-a", "product-revenue", 1, b"legacy"),
    )

    with pytest.raises(ValueError, match="legacy materialization ledger"):
        ProductMaterializationRunner(
            connection,
            warehouse=_Warehouse(),
            catalog=_Catalog(),
            execution_authorization_verifier=_authorization_verifier(),
            cardinality_evidence_reader=_cardinality_repository(),
            clock=lambda: NOW,
        )


def test_materialization_ledger_refuses_nonempty_pre_cardinality_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE product_materializations_v2 ("
        "materialization_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
        "product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
        "product_generation INTEGER NOT NULL, payload BLOB NOT NULL, "
        "UNIQUE (tenant_id, product_id, product_revision, product_generation))"
    )
    connection.execute(
        "INSERT INTO product_materializations_v2 VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-key", "tenant-a", "product-revenue", 1, 1, b"legacy"),
    )

    with pytest.raises(ValueError, match="legacy materialization ledger"):
        ProductMaterializationRunner(
            connection,
            warehouse=_Warehouse(),
            catalog=_Catalog(),
            execution_authorization_verifier=_authorization_verifier(),
            cardinality_evidence_reader=_cardinality_repository(),
            clock=lambda: NOW,
        )


def test_materialization_ledger_refuses_nonempty_pre_authorization_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE product_materializations_v3 ("
        "materialization_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
        "product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
        "product_generation INTEGER NOT NULL, payload BLOB NOT NULL, "
        "UNIQUE (tenant_id, product_id, product_revision, product_generation))"
    )
    connection.execute(
        "INSERT INTO product_materializations_v3 VALUES (?, ?, ?, ?, ?, ?)",
        ("legacy-key", "tenant-a", "product-revenue", 1, 1, b"legacy"),
    )

    with pytest.raises(ValueError, match="legacy materialization ledger"):
        ProductMaterializationRunner(
            connection,
            warehouse=_Warehouse(),
            catalog=_Catalog(),
            execution_authorization_verifier=_authorization_verifier(),
            cardinality_evidence_reader=_cardinality_repository(),
            clock=lambda: NOW,
        )


def test_crash_after_receipt_resumes_without_rerunning_transform() -> None:
    warehouse = _Warehouse()
    should_fail = True

    def fault(checkpoint: str) -> None:
        nonlocal should_fail
        if checkpoint == "after_commit_receipt" and should_fail:
            should_fail = False
            raise RuntimeError("worker stopped")

    runner = ProductMaterializationRunner.in_memory(
        warehouse=warehouse,
        catalog=_Catalog(),
        execution_authorization_verifier=_authorization_verifier(),
        cardinality_evidence_reader=_cardinality_repository(),
        clock=lambda: NOW,
        fault_hook=fault,
    )

    with pytest.raises(RuntimeError, match="worker stopped"):
        runner.materialize(_request(), admission=_admission())
    assert (
        runner.read_receipt(
            tenant_id="tenant-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
        )
        is None
    )
    recovered = runner.materialize(_request(), admission=_admission())

    assert recovered.publication_pending is False
    assert (
        runner.read_receipt(
            tenant_id="tenant-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
        )
        == recovered.receipt
    )
    assert warehouse.executions == 1
    assert warehouse.switches == 1
