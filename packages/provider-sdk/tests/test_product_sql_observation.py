from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    InvalidProductSqlProviderObservation,
    ProductSqlColumnObservation,
    ProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlProviderObservationVerifier,
    ProductSqlSumSemantics,
    SignedProductSqlProviderObservation,
)
from pydantic import ValidationError


def test_product_sql_observation_is_strict_immutable_and_complete() -> None:
    observation = _observation()

    assert observation.engine == "postgresql"
    assert observation.columns[1].decimal_precision == 38
    with pytest.raises(ValidationError):
        ProductSqlProviderObservation.model_validate(
            {**observation.model_dump(), "warehouse_binding_revision": "4"}, strict=True
        )
    with pytest.raises(ValidationError):
        setattr(observation, "tenant_" + "id", "other")


def test_product_sql_observation_rejects_non_utc_time_and_incomplete_decimal_shape() -> None:
    observation = _observation()

    with pytest.raises(ValidationError, match="UTC"):
        ProductSqlProviderObservation.model_validate(
            {**observation.model_dump(), "observed_at": datetime(2026, 9, 15, 12)}
        )
    with pytest.raises(ValidationError, match="precision and scale"):
        ProductSqlColumnObservation(
            name="revenue",
            logical_type="decimal",
            physical_type="NUMERIC",
            nullable=False,
            decimal_precision=None,
            decimal_scale=None,
            collation=None,
            encoding=None,
        )


def test_product_sql_observation_rejects_duplicate_columns_and_ambiguous_string_semantics() -> None:
    observation = _observation()

    with pytest.raises(ValidationError, match="unique"):
        ProductSqlProviderObservation.model_validate(
            {**observation.model_dump(), "columns": (observation.columns[0],) * 2}
        )
    with pytest.raises(ValidationError, match="collation and encoding"):
        ProductSqlColumnObservation(
            name="region",
            logical_type="string",
            physical_type="TEXT",
            nullable=False,
            decimal_precision=None,
            decimal_scale=None,
            collation=None,
            encoding="UTF8",
        )


def test_sum_observation_can_record_disqualifying_wrap_behavior() -> None:
    semantics = ProductSqlSumSemantics(
        input_physical_type="Decimal(38, 9)",
        accumulator_physical_type="Decimal(38, 9)",
        result_physical_type="Decimal(38, 9)",
        overflow_behavior="wrap",
        null_input_behavior="exclude",
        empty_group_behavior="no_row",
    )

    assert semantics.overflow_behavior == "wrap"


def test_provider_observation_signature_verifies_at_the_maximum_allowed_age() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = ProductSqlProviderObservationSigner("provider-key-1", private_key)
    signed = signer.sign(_observation())
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    verified = verifier.verify(
        signed,
        evaluated_at=_observation().observed_at + timedelta(minutes=5),
    )

    assert verified == _observation()
    assert signed.schema_version == "1"
    assert signed.observation_digest == digest(_observation())


def test_signed_provider_observation_is_strict_frozen_and_forbids_extra_fields() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())

    with pytest.raises(ValidationError):
        signed.key_id = "provider-key-2"
    with pytest.raises(ValidationError):
        SignedProductSqlProviderObservation.model_validate(
            {**signed.model_dump(mode="python"), "unexpected": "private"}
        )


def test_signer_strictly_revalidates_model_copy_and_model_construct_payloads() -> None:
    signer = ProductSqlProviderObservationSigner("provider-key-1", Ed25519PrivateKey.generate())
    copied = _observation().model_copy(update={"warehouse_binding_revision": "4"})
    payload = _observation().model_dump(mode="python")
    payload["observed_at"] = datetime(2026, 9, 15, 12)
    constructed = ProductSqlProviderObservation.model_construct(**payload)

    with pytest.raises(ValueError, match="observation payload is invalid"):
        signer.sign(copied)
    with pytest.raises(ValueError, match="observation payload is invalid"):
        signer.sign(constructed)


def test_signer_rejects_undeclared_nested_fields_added_by_model_copy() -> None:
    signer = ProductSqlProviderObservationSigner("provider-key-1", Ed25519PrivateKey.generate())
    invalid_column = _observation().columns[0].model_copy(update={"unexpected": "private"})
    copied = _observation().model_copy(
        update={"columns": (invalid_column, *_observation().columns[1:])}
    )

    with pytest.raises(ValueError, match="observation payload is invalid"):
        signer.sign(copied)


@pytest.mark.parametrize(
    "mutation",
    ("tampered_observation", "tampered_digest", "malformed_signature"),
)
def test_verifier_rejects_tampering_and_malformed_base64(mutation: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())
    if mutation == "tampered_observation":
        signed = signed.model_copy(
            update={"observation": signed.observation.model_copy(update={"engine_version": "18.7"})}
        )
    elif mutation == "tampered_digest":
        signed = signed.model_copy(update={"observation_digest": "f" * 64})
    else:
        signed = signed.model_copy(update={"signature": "not-base64!"})
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    with pytest.raises(InvalidProductSqlProviderObservation):
        verifier.verify(signed, evaluated_at=_observation().observed_at)


def test_verifier_rejects_unknown_provider_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())
    verifier = ProductSqlProviderObservationVerifier(
        {}, maximum_observation_age=timedelta(minutes=5)
    )

    with pytest.raises(InvalidProductSqlProviderObservation, match="unknown"):
        verifier.verify(signed, evaluated_at=_observation().observed_at)


def test_verifier_revalidates_a_constructed_envelope_before_trusting_it() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())
    invalid_observation = signed.observation.model_copy(update={"warehouse_binding_revision": "4"})
    invalid_signed = SignedProductSqlProviderObservation.model_construct(
        **{
            **signed.model_dump(mode="python"),
            "observation": invalid_observation,
        }
    )
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    with pytest.raises(InvalidProductSqlProviderObservation, match="envelope is invalid"):
        verifier.verify(invalid_signed, evaluated_at=_observation().observed_at)


def test_verifier_rejects_stale_or_future_observations() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    with pytest.raises(InvalidProductSqlProviderObservation, match="stale"):
        verifier.verify(
            signed,
            evaluated_at=_observation().observed_at + timedelta(minutes=5, microseconds=1),
        )
    with pytest.raises(InvalidProductSqlProviderObservation, match="future"):
        verifier.verify(
            signed,
            evaluated_at=_observation().observed_at - timedelta(microseconds=1),
        )


@pytest.mark.parametrize("maximum_age", (timedelta(0), timedelta(seconds=-1)))
def test_verifier_requires_a_positive_maximum_observation_age(
    maximum_age: timedelta,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ProductSqlProviderObservationVerifier({}, maximum_observation_age=maximum_age)


@pytest.mark.parametrize(
    "evaluated_at",
    (
        datetime(2026, 9, 15, 12),
        datetime(2026, 9, 15, 13, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_verifier_requires_a_timezone_aware_utc_evaluation_time(
    evaluated_at: datetime,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductSqlProviderObservationSigner("provider-key-1", private_key).sign(_observation())
    verifier = ProductSqlProviderObservationVerifier(
        {"provider-key-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        verifier.verify(signed, evaluated_at=evaluated_at)


def _observation() -> ProductSqlProviderObservation:
    return ProductSqlProviderObservation(
        observation_id="product-sql-observation-1",
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-a",
        warehouse_binding_revision=4,
        relation_ref="relation-revenue-events-v1",
        relation_namespace="raw",
        relation_name="revenue_events",
        engine="postgresql",
        engine_version="18.6",
        engine_image_digest="a" * 64,
        engine_build_digest="b" * 64,
        observed_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        columns=(
            ProductSqlColumnObservation(
                name="region",
                logical_type="string",
                physical_type="TEXT",
                nullable=False,
                decimal_precision=None,
                decimal_scale=None,
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
                collation=None,
                encoding=None,
            ),
        ),
        sum_semantics=ProductSqlSumSemantics(
            input_physical_type="NUMERIC(38,9)",
            accumulator_physical_type="NUMERIC",
            result_physical_type="NUMERIC",
            overflow_behavior="error",
            null_input_behavior="exclude",
            empty_group_behavior="no_row",
        ),
    )
