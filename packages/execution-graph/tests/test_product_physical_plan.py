from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import canonical_bytes, digest
from heinzel_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    InvalidProductExecutionAuthorization,
    InvalidProductPhysicalPlan,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
    revalidate_product_physical_plan,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _source() -> GenerationScopedProductSource:
    return GenerationScopedProductSource(
        namespace="raw",
        relation_name="raw_sales",
        generation_column="generation_id",
        payload_column="payload",
        generation_id="1" * 64,
        landing_receipt_digest="2" * 64,
        observed_source_schema_digest="3" * 64,
        field_bindings=(
            ProductJsonFieldBinding(
                logical_field="region", json_field="region", scalar_type="string"
            ),
            ProductJsonFieldBinding(
                logical_field="revenue", json_field="revenue", scalar_type="decimal"
            ),
        ),
    )


def _plan() -> ProductPhysicalPlan:
    statement = (
        'SELECT "region", CAST(SUM(CAST("revenue" AS NUMERIC(57,9))) '
        'AS NUMERIC(57,9)) AS "total_revenue" FROM "raw"."raw_sales" '
        'WHERE "generation_id" = $1 GROUP BY "region"'
    )
    return ProductPhysicalPlan(
        compiler_version="0.1.0",
        legality_rule_id="PRODUCT-SQL-PROJECT-SUM",
        legality_rule_version="1",
        tenant_id="tenant-a",
        product_id="revenue-by-region",
        product_revision=1,
        contract_ref="contract-sales",
        contract_revision=4,
        contract_digest="4" * 64,
        iir_digest="5" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=3,
        provider_observation_digest="6" * 64,
        source=_source(),
        target=ProductTarget(namespace="products", relation_name="revenue_by_region_g1"),
        emitted_statement=statement,
        statement_digest=digest(statement),
        output_columns=("region", "total_revenue"),
        expected_output_schema_digest="7" * 64,
        decimal_output_checks=(Decimal57OutputCheck(column_name="total_revenue"),),
    )


def test_product_physical_plan_is_canonical_and_binds_the_generation_source() -> None:
    plan = _plan()

    assert revalidate_product_physical_plan(plan) == plan
    assert digest(revalidate_product_physical_plan(plan)) == digest(plan)
    assert plan.source.generation_id == "1" * 64
    assert plan.decimal_output_checks[0].precision == 57
    assert plan.decimal_output_checks[0].scale == 9
    assert plan.decimal_output_checks[0].nullable is False


def test_product_physical_plan_rejects_statement_digest_and_duplicate_bindings() -> None:
    with pytest.raises(ValidationError, match="statement digest"):
        _plan().model_copy(update={"statement_digest": "8" * 64}).model_validate(
            _plan().model_copy(update={"statement_digest": "8" * 64}).model_dump()
        )

    binding = _source().field_bindings[0]
    with pytest.raises(ValidationError, match="field bindings"):
        GenerationScopedProductSource(
            **_source().model_dump(exclude={"field_bindings"}),
            field_bindings=(binding, binding),
        )

    check = _plan().decimal_output_checks[0]
    with pytest.raises(ValidationError, match="output checks"):
        ProductPhysicalPlan(
            **_plan().model_dump(exclude={"decimal_output_checks"}),
            decimal_output_checks=(check, check),
        )


def test_generation_source_rejects_one_column_for_generation_and_payload() -> None:
    with pytest.raises(ValidationError, match="generation and payload columns"):
        GenerationScopedProductSource(
            **_source().model_dump(exclude={"payload_column"}),
            payload_column="generation_id",
        )


def test_consumer_revalidation_rejects_invalid_model_copy_and_construct() -> None:
    plan = _plan()
    copied = plan.model_copy(update={"statement_digest": "8" * 64})
    plan_values = {
        field_name: getattr(plan, field_name) for field_name in ProductPhysicalPlan.model_fields
    }
    plan_values["source"] = _source().model_copy(update={"namespace": "raw; DROP TABLE sales"})
    constructed = ProductPhysicalPlan.model_construct(
        **plan_values,
    )

    with pytest.raises(InvalidProductPhysicalPlan, match="invalid"):
        revalidate_product_physical_plan(copied)
    with pytest.raises(InvalidProductPhysicalPlan, match="invalid"):
        revalidate_product_physical_plan(constructed)


@pytest.mark.parametrize(
    "plan",
    (
        _plan().model_copy(update={"undeclared": "value"}),
        _plan().model_copy(update={"source": _source().model_copy(update={"undeclared": "value"})}),
        _plan().model_copy(
            update={
                "source": _source().model_copy(
                    update={
                        "field_bindings": (
                            _source().field_bindings[0].model_copy(update={"undeclared": "value"}),
                            _source().field_bindings[1],
                        )
                    }
                )
            }
        ),
        _plan().model_copy(
            update={"target": _plan().target.model_copy(update={"undeclared": "value"})}
        ),
        _plan().model_copy(
            update={
                "decimal_output_checks": (
                    _plan().decimal_output_checks[0].model_copy(update={"undeclared": "value"}),
                )
            }
        ),
    ),
)
def test_consumer_revalidation_rejects_undeclared_plan_fields(plan: ProductPhysicalPlan) -> None:
    with pytest.raises(InvalidProductPhysicalPlan, match="invalid"):
        revalidate_product_physical_plan(plan)


def test_consumer_revalidation_rejects_hidden_pydantic_extra_and_type_coercion() -> None:
    hidden_plan = _plan().model_copy()
    object.__setattr__(hidden_plan, "__pydantic_extra__", {"undeclared": "value"})
    coerced_plan = _plan().model_copy(update={"product_revision": "1"})

    with pytest.raises(InvalidProductPhysicalPlan, match="invalid"):
        revalidate_product_physical_plan(hidden_plan)
    with pytest.raises(InvalidProductPhysicalPlan, match="invalid"):
        revalidate_product_physical_plan(coerced_plan)


def test_execution_authorization_is_bound_to_the_exact_physical_plan() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    authorization = ProductExecutionAuthorizationVerifier(
        {"execution-authority-1": signer.public_key}
    ).verify(signed, physical_plan=_plan(), now=NOW)

    assert authorization.physical_plan_digest == digest(_plan())
    assert authorization.contract_digest == _plan().contract_digest
    assert authorization.provider_observation_digest == _plan().provider_observation_digest


def test_execution_authorization_signature_has_a_stable_domain_and_key_binding() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = ProductExecutionAuthorizationSigner("execution-authority-1", private_key)
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    private_key.public_key().verify(
        base64.b64decode(signed.signature, validate=True),
        canonical_bytes(
            {
                "domain": "heinzel-product-execution-authorization-v1",
                "authorization_digest": signed.authorization_digest,
                "key_id": "execution-authority-1",
            }
        ),
    )

    substituted_signer = ProductExecutionAuthorizationSigner.generate("execution-authority-2")
    with pytest.raises(InvalidProductExecutionAuthorization, match="signature"):
        ProductExecutionAuthorizationVerifier(
            {
                "execution-authority-1": signer.public_key,
                "execution-authority-2": substituted_signer.public_key,
            }
        ).verify(
            signed.model_copy(update={"key_id": "execution-authority-2"}),
            physical_plan=_plan(),
            now=NOW,
        )


def test_execution_authorization_binds_legality_and_cardinality_digests() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    baseline = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    changed_legality = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="a" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    changed_cardinality = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="b" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    assert baseline.authorization.legality_decision_digest == "8" * 64
    assert baseline.authorization.cardinality_evidence_digest == "9" * 64
    assert (
        len(
            {
                baseline.authorization_digest,
                changed_legality.authorization_digest,
                changed_cardinality.authorization_digest,
            }
        )
        == 3
    )
    assert len({baseline.signature, changed_legality.signature, changed_cardinality.signature}) == 3


def test_execution_authorization_rejects_tamper_expiry_and_unknown_key() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    tampered = signed.model_copy(
        update={
            "authorization": signed.authorization.model_copy(
                update={"cardinality_evidence_digest": "a" * 64}
            )
        }
    )
    verifier = ProductExecutionAuthorizationVerifier({"execution-authority-1": signer.public_key})

    with pytest.raises(InvalidProductExecutionAuthorization, match="invalid"):
        verifier.verify(tampered, physical_plan=_plan(), now=NOW)
    with pytest.raises(InvalidProductExecutionAuthorization, match="expired"):
        verifier.verify(signed, physical_plan=_plan(), now=NOW + timedelta(minutes=15))
    with pytest.raises(InvalidProductExecutionAuthorization, match="unknown"):
        ProductExecutionAuthorizationVerifier({}).verify(signed, physical_plan=_plan(), now=NOW)


def test_execution_authorization_rejects_undeclared_envelope_and_authorization_fields() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    verifier = ProductExecutionAuthorizationVerifier({"execution-authority-1": signer.public_key})

    with pytest.raises(InvalidProductExecutionAuthorization, match="invalid"):
        verifier.verify(
            signed.model_copy(update={"undeclared": "value"}),
            physical_plan=_plan(),
            now=NOW,
        )
    with pytest.raises(InvalidProductExecutionAuthorization, match="invalid"):
        verifier.verify(
            signed.model_copy(
                update={
                    "authorization": signed.authorization.model_copy(update={"undeclared": "value"})
                }
            ),
            physical_plan=_plan(),
            now=NOW,
        )

    hidden_signed = signed.model_copy()
    object.__setattr__(hidden_signed, "__pydantic_extra__", {"undeclared": "value"})
    with pytest.raises(InvalidProductExecutionAuthorization, match="invalid"):
        verifier.verify(hidden_signed, physical_plan=_plan(), now=NOW)

    coerced_authorization = signed.authorization.model_copy(update={"issued_at": NOW.isoformat()})
    with pytest.raises(InvalidProductExecutionAuthorization, match="invalid"):
        verifier.verify(
            signed.model_copy(update={"authorization": coerced_authorization}),
            physical_plan=_plan(),
            now=NOW,
        )


def test_execution_authorization_rejects_malformed_signature_and_non_utc_time() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    verifier = ProductExecutionAuthorizationVerifier({"execution-authority-1": signer.public_key})

    with pytest.raises(InvalidProductExecutionAuthorization, match="signature"):
        verifier.verify(
            signed.model_copy(update={"signature": "not-base64!"}),
            physical_plan=_plan(),
            now=NOW,
        )
    with pytest.raises(InvalidProductExecutionAuthorization, match="signature"):
        verifier.verify(
            signed.model_copy(
                update={"signature": signed.signature[:4] + "\n" + signed.signature[4:]}
            ),
            physical_plan=_plan(),
            now=NOW,
        )
    with pytest.raises(InvalidProductExecutionAuthorization, match="verification time"):
        verifier.verify(
            signed,
            physical_plan=_plan(),
            now=NOW.astimezone(timezone(timedelta(hours=1))),
        )


def test_execution_authorization_rejects_another_plan_and_invalid_time_window() -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    signed = signer.sign(
        physical_plan=_plan(),
        legality_decision_digest="8" * 64,
        cardinality_evidence_digest="9" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    another_plan = ProductPhysicalPlan(
        **_plan().model_dump(exclude={"product_revision"}), product_revision=2
    )

    with pytest.raises(InvalidProductExecutionAuthorization, match="physical plan"):
        ProductExecutionAuthorizationVerifier({"execution-authority-1": signer.public_key}).verify(
            signed, physical_plan=another_plan, now=NOW
        )
    with pytest.raises(ValueError, match="expiry"):
        signer.sign(
            physical_plan=_plan(),
            legality_decision_digest="8" * 64,
            cardinality_evidence_digest="9" * 64,
            issued_at=NOW,
            expires_at=NOW,
        )

    with pytest.raises(InvalidProductExecutionAuthorization, match="not yet valid"):
        ProductExecutionAuthorizationVerifier({"execution-authority-1": signer.public_key}).verify(
            signed, physical_plan=_plan(), now=NOW - timedelta(microseconds=1)
        )


@pytest.mark.parametrize("timestamp_field", ("issued_at", "expires_at"))
def test_execution_authorization_signer_rejects_non_utc_timestamps(
    timestamp_field: str,
) -> None:
    signer = ProductExecutionAuthorizationSigner.generate("execution-authority-1")
    timestamps: dict[str, datetime] = {
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    timestamps[timestamp_field] = timestamps[timestamp_field].astimezone(
        timezone(timedelta(hours=1))
    )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        signer.sign(
            physical_plan=_plan(),
            legality_decision_digest="8" * 64,
            cardinality_evidence_digest="9" * 64,
            **timestamps,
        )
