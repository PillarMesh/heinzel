from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from pillarmesh_compiler import (
    NoValidPlan,
    ProductPhysicalPlanAuthority,
    compile_product_iir,
    compose_product_physical_plan_candidate,
)
from pillarmesh_contract_model import digest
from pillarmesh_execution_graph import (
    GenerationScopedProductSource,
    ProductInputCardinalityEvidence,
    ProductInputCardinalityEvidenceSigner,
    ProductInputCardinalityEvidenceVerifier,
    ProductInputReceiptCardinality,
    ProductJsonFieldBinding,
    ProductTarget,
    SignedProductInputCardinalityEvidence,
)
from pillarmesh_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from pillarmesh_provider_sdk import (
    ProductSqlColumnObservation,
    ProductSqlProviderObservation,
    ProductSqlSumSemantics,
)

_POSTGRESQL_IMAGE_DIGEST = "33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"


def _product() -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="raw",
            relation_name="revenue_events",
            alias="revenue_events",
            columns=(
                ColumnDeclaration(name="region", value_type="string", nullable=False),
                ColumnDeclaration(name="revenue", value_type="decimal", nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(output_name="region", expression=region),
                    NamedExpression(output_name="revenue", expression=revenue),
                )
            ),
            AggregateOperation(
                group_by=(region,),
                measures=(
                    AggregateMeasure(function="sum", argument=revenue, output_name="total_revenue"),
                ),
            ),
        ),
        grain=(region,),
        freshness_seconds=3600,
    )


def _authority() -> ProductPhysicalPlanAuthority:
    return ProductPhysicalPlanAuthority(
        tenant_id="tenant-a",
        product_id="revenue_by_region",
        product_revision=2,
        contract_ref="contract-sales",
        contract_revision=4,
        contract_digest="1" * 64,
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=7,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="raw_sales",
            generation_column="generation_id",
            payload_column="payload",
            generation_id="2" * 64,
            landing_receipt_digest="3" * 64,
            observed_source_schema_digest="4" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="region", json_field="region", scalar_type="string"
                ),
                ProductJsonFieldBinding(
                    logical_field="revenue", json_field="revenue", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(namespace="products", relation_name="revenue_by_region_g1"),
        expected_output_schema_digest="5" * 64,
    )


def _observation() -> ProductSqlProviderObservation:
    return ProductSqlProviderObservation(
        observation_id="observation-1",
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=7,
        relation_ref="relation-revenue-events-v1",
        relation_namespace="raw",
        relation_name="revenue_events",
        engine="postgresql",
        engine_version="18.6",
        engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        engine_build_digest="6" * 64,
        observed_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        columns=(
            ProductSqlColumnObservation(
                name="region",
                logical_type="string",
                physical_type="TEXT",
                nullable=False,
                collation="C",
                encoding="UTF8",
            ),
            ProductSqlColumnObservation(
                name="revenue",
                logical_type="decimal",
                physical_type="NUMERIC(38,9)",
                nullable=False,
                decimal_precision=38,
                decimal_scale=9,
            ),
        ),
        sum_semantics=ProductSqlSumSemantics(
            input_physical_type="NUMERIC(38,9)",
            accumulator_physical_type="INTERNAL",
            result_physical_type="NUMERIC",
            overflow_behavior="promote",
            null_input_behavior="exclude",
            empty_group_behavior="no_row",
        ),
    )


def _signed_cardinality(
    *,
    authority: ProductPhysicalPlanAuthority | None = None,
    evidence_change: dict[str, object] | None = None,
    generation_id: str | None = None,
    receipt_digest: str | None = None,
) -> tuple[SignedProductInputCardinalityEvidence, ProductInputCardinalityEvidenceVerifier]:
    authority = authority or _authority()
    observation = _observation()
    plan = compose_product_physical_plan_candidate(
        _product(),
        authority=authority,
        engine="postgresql",
        provider_observation_digest=digest(observation),
    )
    receipt = ProductInputReceiptCardinality(
        generation_id=generation_id or authority.source.generation_id,
        receipt_digest=receipt_digest or authority.source.landing_receipt_digest,
        record_count=3,
    )
    evidence = ProductInputCardinalityEvidence(
        tenant_id=authority.tenant_id,
        contract_ref=authority.contract_ref,
        contract_revision=authority.contract_revision,
        contract_digest=authority.contract_digest,
        product_plan_digest=digest(plan),
        relation_ref=authority.source.relation_name,
        generation_ids=(receipt.generation_id,),
        receipts=(receipt,),
        total_contributing_row_ceiling=3,
        policy_maximum_contributing_rows=10,
        maximum_scaled_sum=3 * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=datetime(2026, 9, 15, 12, 4, tzinfo=UTC),
    )
    if evidence_change is not None:
        evidence = evidence.model_copy(update=evidence_change)
    signer = ProductInputCardinalityEvidenceSigner.generate("cardinality-authority-1")
    return (
        signer.sign(evidence),
        ProductInputCardinalityEvidenceVerifier({"cardinality-authority-1": signer.public_key}),
    )


@pytest.mark.parametrize(
    ("engine", "expected_decimal_type"),
    (("postgresql", "NUMERIC(38,9)"), ("clickhouse", "Decimal(38, 9)")),
)
def test_composer_builds_an_exact_generation_scoped_physical_plan(
    engine: Literal["postgresql", "clickhouse"],
    expected_decimal_type: str,
) -> None:
    product = _product()
    observation_digest = "6" * 64

    plan = compose_product_physical_plan_candidate(
        product,
        authority=_authority(),
        engine=engine,
        provider_observation_digest=observation_digest,
    )

    assert plan.provider == engine
    assert plan.iir_digest == digest(product)
    assert plan.provider_observation_digest == observation_digest
    assert plan.source == _authority().source
    assert plan.statement_digest == digest(plan.emitted_statement)
    assert f"WHERE \"generation_id\" = '{'2' * 64}'" in plan.emitted_statement
    assert expected_decimal_type in plan.emitted_statement
    assert tuple(check.column_name for check in plan.decimal_output_checks) == ("total_revenue",)


def test_generation_gate_passes_without_admitting_or_executing_the_candidate() -> None:
    observation = _observation()

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
    )

    assert isinstance(result, NoValidPlan)
    assert result.preconditions[12].status == "satisfied"
    assert tuple(item.status for item in result.preconditions[13:]) == (
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
    )
    assert result.execution_occurred is False


def test_signed_cardinality_evidence_satisfies_only_the_bound_cardinality_gate() -> None:
    observation = _observation()
    signed_cardinality, verifier = _signed_cardinality()

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
        signed_cardinality_evidence=signed_cardinality,
        cardinality_evidence_verifier=verifier,
    )

    assert isinstance(result, NoValidPlan)
    assert result.preconditions[12].status == "satisfied"
    assert result.preconditions[13].status == "satisfied"
    assert tuple(item.status for item in result.preconditions[14:]) == (
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
        "unsatisfied",
    )
    assert result.execution_occurred is False


def test_raw_cardinality_evidence_does_not_satisfy_the_authority_gate() -> None:
    observation = _observation()
    signed_cardinality, _ = _signed_cardinality()

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
        cardinality_evidence=signed_cardinality.evidence,
    )

    assert result.preconditions[12].status == "satisfied"
    assert result.preconditions[13].status == "unsatisfied"


@pytest.mark.parametrize("case_kind", ("tampered_signature", "future", "raw_and_signed"))
def test_cardinality_provenance_fails_closed(case_kind: str) -> None:
    observation = _observation()
    signed_cardinality, verifier = _signed_cardinality(
        evidence_change=(
            {"created_at": datetime(2026, 9, 15, 12, 6, tzinfo=UTC)}
            if case_kind == "future"
            else None
        )
    )
    if case_kind == "tampered_signature":
        signed_cardinality = signed_cardinality.model_copy(update={"signature": "not-base64!"})

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
        cardinality_evidence=(
            signed_cardinality.evidence if case_kind == "raw_and_signed" else None
        ),
        signed_cardinality_evidence=signed_cardinality,
        cardinality_evidence_verifier=verifier,
    )

    assert result.preconditions[12].status == "satisfied"
    assert result.preconditions[13].status == "unsatisfied"


@pytest.mark.parametrize(
    "change",
    (
        {"tenant_id": "tenant-b"},
        {"contract_ref": "contract-other"},
        {"contract_revision": 5},
        {"contract_digest": "8" * 64},
        {"product_plan_digest": "8" * 64},
        {"relation_ref": "raw_other"},
    ),
)
def test_signed_cardinality_evidence_must_match_every_physical_plan_parent(
    change: dict[str, object],
) -> None:
    observation = _observation()
    signed_cardinality, verifier = _signed_cardinality(evidence_change=change)

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
        signed_cardinality_evidence=signed_cardinality,
        cardinality_evidence_verifier=verifier,
    )

    assert result.preconditions[13].status == "unsatisfied"


@pytest.mark.parametrize(
    "change_kind",
    ("generation_id", "receipt_digest"),
)
def test_signed_cardinality_evidence_must_match_the_exact_generation_receipt(
    change_kind: str,
) -> None:
    observation = _observation()
    signed_cardinality, verifier = _signed_cardinality(
        generation_id="8" * 64 if change_kind == "generation_id" else None,
        receipt_digest="8" * 64 if change_kind == "receipt_digest" else None,
    )

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority(),
        signed_cardinality_evidence=signed_cardinality,
        cardinality_evidence_verifier=verifier,
    )

    assert result.preconditions[13].status == "unsatisfied"


@pytest.mark.parametrize(
    "authority",
    (
        _authority().model_copy(update={"product_id": "other_product"}),
        _authority().model_copy(
            update={
                "source": _authority().source.model_copy(update={"landing_receipt_digest": "bad"})
            }
        ),
        ProductPhysicalPlanAuthority.model_construct(
            **{
                field_name: getattr(_authority(), field_name)
                for field_name in ProductPhysicalPlanAuthority.model_fields
                if field_name != "tenant_id"
            }
        ),
    ),
)
def test_generation_gate_rejects_unbound_or_bypassed_authority(
    authority: ProductPhysicalPlanAuthority,
) -> None:
    observation = _observation()

    result = compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=observation,
        expected_provider_observation_digest=digest(observation),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=authority,
    )

    assert result.preconditions[12].status == "unsatisfied"
    assert result.execution_occurred is False


class _Omitted:
    """Sentinel for an input the caller left out entirely, as distinct from `None`."""


_OMITTED = _Omitted()


def _compile_admission(
    *,
    authority: ProductPhysicalPlanAuthority | _Omitted | None = _OMITTED,
    observation: ProductSqlProviderObservation | _Omitted | None = _OMITTED,
    expected_observation_digest: str | _Omitted | None = _OMITTED,
) -> NoValidPlan:
    """Compile with every admission input bound, so a single override is attributable.

    `None` is a real value here: passing it omits that input from the compile call, which is
    what several of these cases need to assert. Anything left unset uses the bound default.
    """
    resolved_observation = _observation() if isinstance(observation, _Omitted) else observation
    resolved_digest = digest(resolved_observation) if resolved_observation is not None else None
    return compile_product_iir(
        _product(),
        engine="postgresql",
        provider_observation=resolved_observation,
        expected_provider_observation_digest=(
            resolved_digest
            if isinstance(expected_observation_digest, _Omitted)
            else expected_observation_digest
        ),
        expected_tenant_id="tenant-a",
        expected_warehouse_binding_id="warehouse-a",
        expected_warehouse_binding_revision=7,
        expected_relation_ref="relation-revenue-events-v1",
        expected_relation_namespace="raw",
        expected_engine_image_digest=_POSTGRESQL_IMAGE_DIGEST,
        expected_engine_build_digest="6" * 64,
        evaluated_at=datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        physical_plan_authority=_authority() if isinstance(authority, _Omitted) else authority,
    )


def test_the_baseline_admission_inputs_satisfy_the_generation_gate() -> None:
    result = _compile_admission()

    assert result.preconditions[12].status == "satisfied"
    assert result.preconditions[6].status == "satisfied"


def test_admission_requires_an_authority() -> None:
    assert _compile_admission(authority=None).preconditions[12].status == "unsatisfied"


def test_without_a_provider_observation_the_compiler_refuses_before_the_generation_gate() -> None:
    """The admission chain is never reached, so its None guard cannot be exercised from here.

    This is why mutating that guard survives focused mutation testing: the public entry point
    refuses first, with a single precondition and no physical candidate.
    """
    result = _compile_admission(observation=None)

    assert len(result.preconditions) == 1
    assert result.preconditions[0].status == "unsatisfied"
    assert result.preconditions[0].reason == "restricted product SQL has no approved legality rule"
    assert result.execution_occurred is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-b"),
        ("warehouse_binding_id", "warehouse-b"),
        ("warehouse_binding_revision", 8),
    ),
)
def test_each_authority_binding_is_required_to_match_on_its_own(field: str, value: object) -> None:
    """One field differs at a time, and strict revalidation stays satisfied.

    Without the provenance assertion these cases could pass for the wrong reason, the way
    `test_each_sum_null_semantic_is_independently_required` does.
    """
    result = _compile_admission(authority=_authority().model_copy(update={field: value}))

    assert result.preconditions[6].status == "satisfied"
    assert result.preconditions[12].status == "unsatisfied"


def test_admission_requires_the_observed_engine_to_match_on_its_own() -> None:
    result = _compile_admission(
        observation=_observation().model_copy(update={"engine": "clickhouse"})
    )

    assert result.preconditions[6].status == "satisfied"
    assert result.preconditions[12].status == "unsatisfied"


def test_admission_requires_the_observation_digest_to_match() -> None:
    """A mismatched digest breaks provenance as well, so this case is not isolated."""
    result = _compile_admission(expected_observation_digest="7" * 64)

    assert result.preconditions[6].status == "unsatisfied"
    assert result.preconditions[12].status == "unsatisfied"
