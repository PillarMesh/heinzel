from __future__ import annotations

import base64
import json
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import canonical_bytes, digest
from heinzel_dbt_adapter import (
    DBT_CORE_VERSION,
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    DbtFailureClassification,
    DbtInvocationAuthority,
    DbtInvocationError,
    DbtInvocationSpec,
    DbtInvoker,
    DbtProcessResult,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pydantic import ValidationError

CONTRACT_DIGEST = "1" * 64
INPUT_DIGESTS = ("2" * 64, "3" * 64)
MANIFEST = b'{"metadata":{"dbt_schema_version":"v12"},"nodes":{}}'
TEST_RESULTS = b'{"metadata":{},"results":[]}'
LINEAGE = b'{"nodes":[],"edges":[]}'


class RecordingRunner:
    def __init__(self, result: DbtProcessResult) -> None:
        self.result = result
        self.invocations: list[DbtInvocationSpec] = []

    def run(self, invocation: DbtInvocationSpec) -> DbtProcessResult:
        self.invocations.append(invocation)
        return self.result


class FailingRunner:
    def run(self, invocation: DbtInvocationSpec) -> DbtProcessResult:
        del invocation
        raise RuntimeError("private warehouse credentials")


def test_v1_compiled_and_signed_model_payloads_are_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    compiled_payload = _model().model_dump(mode="python")
    compiled_payload["schema_version"] = "1"

    with pytest.raises(ValidationError):
        CompiledDbtModel.model_validate(compiled_payload)

    signed_payload = _signed_model(private_key).model_dump(mode="python")
    signed_payload["schema_version"] = "1"

    with pytest.raises(ValidationError):
        SignedCompiledDbtModel.model_validate(signed_payload)


def test_compiled_model_binds_decimal_magnitude_check_to_declared_output() -> None:
    model = CompiledDbtModel(
        model_name="product_orders_v1",
        contract_digest=CONTRACT_DIGEST,
        provider="clickhouse",
        input_generation_digests=INPUT_DIGESTS,
        target_schema="contract_" + "1" * 54,
        output_columns=("region", "total_revenue"),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT region, sum(revenue) AS total_revenue FROM landed GROUP BY region",
    )

    assert model.output_magnitude_checks == (
        DbtDecimalMagnitudeCheck(column_name="total_revenue", precision=57, scale=9),
    )


@pytest.mark.parametrize(
    "checks",
    (
        (
            DbtDecimalMagnitudeCheck(column_name="total_revenue"),
            DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        ),
        (DbtDecimalMagnitudeCheck(column_name="unknown_total"),),
    ),
)
def test_compiled_model_rejects_duplicate_or_unknown_magnitude_check_columns(
    checks: tuple[DbtDecimalMagnitudeCheck, ...],
) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate(
            {
                **_model().model_dump(mode="python"),
                "output_columns": ("total_revenue",),
                "output_magnitude_checks": checks,
            }
        )


def test_compiled_model_rejects_duplicate_output_columns() -> None:
    payload = _model().model_dump(mode="python")
    payload["output_columns"] = ("total_revenue", "total_revenue")

    with pytest.raises(ValidationError):
        CompiledDbtModel.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"column_name": "total_revenue", "precision": 56, "scale": 9},
        {"column_name": "total_revenue", "precision": 57, "scale": 8},
        {"column_name": "total_revenue", "unexpected": "private"},
    ),
)
def test_decimal_magnitude_check_rejects_wrong_shape(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DbtDecimalMagnitudeCheck.model_validate(payload)


def test_decimal_magnitude_check_is_frozen() -> None:
    check = DbtDecimalMagnitudeCheck(column_name="total_revenue")

    with pytest.raises(ValidationError):
        check.column_name = "net_revenue"


def test_magnitude_check_is_part_of_the_signed_canonical_model() -> None:
    private_key = Ed25519PrivateKey.generate()
    base_model = _model().model_copy(update={"output_columns": ("total_revenue", "net_revenue")})
    total_model = base_model.model_copy(
        update={"output_magnitude_checks": (DbtDecimalMagnitudeCheck(column_name="total_revenue"),)}
    )
    net_model = base_model.model_copy(
        update={"output_magnitude_checks": (DbtDecimalMagnitudeCheck(column_name="net_revenue"),)}
    )

    total_signing_bytes = compiled_dbt_model_signing_bytes(total_model)
    net_signing_bytes = compiled_dbt_model_signing_bytes(net_model)

    assert total_signing_bytes != net_signing_bytes
    assert digest(total_model) != digest(net_model)
    assert private_key.sign(total_signing_bytes) != private_key.sign(net_signing_bytes)


def test_signing_revalidates_a_model_copy_bypass() -> None:
    invalid_check = DbtDecimalMagnitudeCheck.model_construct(
        schema_version="1",
        column_name="total_revenue",
        precision=56,
        scale=9,
    )
    invalid_model = _model().model_copy(
        update={
            "output_columns": ("total_revenue",),
            "output_magnitude_checks": (invalid_check,),
        }
    )

    with pytest.raises(ValueError, match="compiled dbt model is invalid"):
        compiled_dbt_model_signing_bytes(invalid_model)


def test_signing_rejects_an_extra_field_added_by_model_copy() -> None:
    invalid_check = DbtDecimalMagnitudeCheck(column_name="total_revenue").model_copy(
        update={"unexpected": "private"}
    )
    invalid_model = _model().model_copy(
        update={
            "output_columns": ("total_revenue",),
            "output_magnitude_checks": (invalid_check,),
        }
    )

    with pytest.raises(ValueError, match="compiled dbt model is invalid"):
        compiled_dbt_model_signing_bytes(invalid_model)


def test_signing_revalidates_a_model_construct_bypass() -> None:
    payload = _model().model_dump(mode="python")
    payload["output_columns"] = ("total_revenue",)
    payload["output_magnitude_checks"] = (DbtDecimalMagnitudeCheck(column_name="unknown_total"),)
    invalid_model = CompiledDbtModel.model_construct(**payload)

    with pytest.raises(ValueError, match="compiled dbt model is invalid"):
        compiled_dbt_model_signing_bytes(invalid_model)


def test_invocation_revalidates_a_signed_model_bypass_before_running() -> None:
    private_key = Ed25519PrivateKey.generate()
    invalid_check = DbtDecimalMagnitudeCheck.model_construct(
        schema_version="1",
        column_name="total_revenue",
        precision=56,
        scale=9,
    )
    invalid_model = _model().model_copy(
        update={
            "output_columns": ("total_revenue",),
            "output_magnitude_checks": (invalid_check,),
        }
    )
    signed_model = SignedCompiledDbtModel.model_construct(
        schema_version="2",
        model=invalid_model,
        model_digest=digest(invalid_model),
        key_id="compiler-1",
        signature=base64.b64encode(private_key.sign(canonical_bytes(invalid_model))).decode(
            "ascii"
        ),
    )
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=signed_model, authority=_authority())

    assert caught.value.classification is DbtFailureClassification.INVALID_SIGNATURE
    assert runner.invocations == []


def test_invocation_revalidates_a_model_construct_bypass_before_running() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _model().model_dump(mode="python")
    payload["output_columns"] = ("total_revenue",)
    payload["output_magnitude_checks"] = (DbtDecimalMagnitudeCheck(column_name="unknown_total"),)
    invalid_model = CompiledDbtModel.model_construct(**payload)
    signed_model = SignedCompiledDbtModel.model_construct(
        schema_version="2",
        model=invalid_model,
        model_digest=digest(invalid_model),
        key_id="compiler-1",
        signature=base64.b64encode(private_key.sign(canonical_bytes(invalid_model))).decode(
            "ascii"
        ),
    )
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=signed_model, authority=_authority())

    assert caught.value.classification is DbtFailureClassification.INVALID_SIGNATURE
    assert runner.invocations == []


def test_invoke_verifies_authority_and_records_dbt_artifact_digests() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = _signed_model(private_key)
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )

    receipt = invoker.invoke(signed_model=signed, authority=_authority())

    assert runner.invocations == [
        DbtInvocationSpec(
            required_dbt_version=DBT_CORE_VERSION,
            arguments=(
                "dbt",
                "build",
                "--no-use-colors",
                "--select",
                "product_orders_v1",
                "--target",
                "postgresql",
            ),
            model=signed.model,
        )
    ]
    assert receipt.model_digest == digest(signed.model)
    assert receipt.manifest_digest == sha256(MANIFEST).hexdigest()
    assert receipt.run_results_digest == sha256(TEST_RESULTS).hexdigest()
    assert receipt.lineage_digest == sha256(LINEAGE).hexdigest()
    assert receipt.quality_assertion_count == 0
    assert receipt.quality_disposition == "not_asserted"


@pytest.mark.parametrize(
    ("status", "expected_disposition"),
    (("pass", "passed"), ("warn", "limited")),
)
def test_quality_disposition_requires_actual_passing_dbt_test_result(
    status: str, expected_disposition: str
) -> None:
    private_key = Ed25519PrivateKey.generate()
    test_results = json.dumps(
        {
            "metadata": {},
            "results": [{"unique_id": "test.project.required_field", "status": status}],
        }
    ).encode()
    result = DbtProcessResult(
        return_code=0,
        observed_dbt_version=DBT_CORE_VERSION,
        manifest=MANIFEST,
        test_results=test_results,
        lineage=LINEAGE,
    )
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        runner=RecordingRunner(result),
    )

    receipt = invoker.invoke(signed_model=_signed_model(private_key), authority=_authority())

    assert receipt.quality_assertion_count == 1
    assert receipt.quality_disposition == expected_disposition


def test_invalid_compiler_signature_is_rejected_before_invocation() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = _signed_model(private_key).model_copy(update={"signature": "not-base64"})
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=signed, authority=_authority())

    assert caught.value.classification is DbtFailureClassification.INVALID_SIGNATURE
    assert runner.invocations == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("contract_digest", "4" * 64),
        ("provider", "clickhouse"),
        ("input_generation_digests", ("5" * 64,)),
    ),
)
def test_authority_digest_or_provider_mismatch_is_rejected_before_invocation(
    field: str,
    value: object,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )
    authority = _authority().model_copy(update={field: value})

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=_signed_model(private_key), authority=authority)

    assert caught.value.classification is DbtFailureClassification.AUTHORITY_MISMATCH
    assert runner.invocations == []


def test_compiler_signed_model_cannot_target_another_contract_schema() -> None:
    private_key = Ed25519PrivateKey.generate()
    runner = RecordingRunner(_successful_result())
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )
    model = _model().model_copy(update={"target_schema": "contract_" + "9" * 54})

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=_signed_model(private_key, model), authority=_authority())

    assert caught.value.classification is DbtFailureClassification.AUTHORITY_MISMATCH
    assert runner.invocations == []


def test_nonzero_dbt_exit_is_classified_without_exposing_process_output() -> None:
    private_key = Ed25519PrivateKey.generate()
    runner = RecordingRunner(
        DbtProcessResult(
            return_code=2,
            observed_dbt_version=DBT_CORE_VERSION,
            manifest=b"private manifest",
            test_results=b"private tests",
            lineage=b"private lineage",
        )
    )
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()}, runner=runner
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=_signed_model(private_key), authority=_authority())

    assert caught.value.classification is DbtFailureClassification.NONZERO_EXIT
    assert "private" not in str(caught.value)


def test_runner_failure_is_sanitized_without_retaining_its_cause() -> None:
    private_key = Ed25519PrivateKey.generate()
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        runner=FailingRunner(),
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=_signed_model(private_key), authority=_authority())

    assert caught.value.classification is DbtFailureClassification.INVOCATION_FAILED
    assert "private warehouse credentials" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "result",
    (
        DbtProcessResult(
            return_code=0,
            observed_dbt_version="1.9.0",
            manifest=MANIFEST,
            test_results=TEST_RESULTS,
            lineage=LINEAGE,
        ),
        DbtProcessResult(
            return_code=0,
            observed_dbt_version=DBT_CORE_VERSION,
            manifest=b"not-json",
            test_results=TEST_RESULTS,
            lineage=LINEAGE,
        ),
    ),
)
def test_malformed_or_wrong_version_output_is_classified(result: DbtProcessResult) -> None:
    private_key = Ed25519PrivateKey.generate()
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        runner=RecordingRunner(result),
    )

    with pytest.raises(DbtInvocationError) as caught:
        invoker.invoke(signed_model=_signed_model(private_key), authority=_authority())

    assert caught.value.classification is DbtFailureClassification.MALFORMED_OUTPUT


def _model() -> CompiledDbtModel:
    return CompiledDbtModel(
        model_name="product_orders_v1",
        contract_digest=CONTRACT_DIGEST,
        provider="postgresql",
        input_generation_digests=INPUT_DIGESTS,
        target_schema="contract_" + "1" * 54,
        compiled_sql='SELECT "order_id" FROM "landed_orders"',
    )


def _signed_model(
    private_key: Ed25519PrivateKey,
    model: CompiledDbtModel | None = None,
) -> SignedCompiledDbtModel:
    model = model or _model()
    signature = private_key.sign(canonical_bytes(model))
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-1",
        signature=base64.b64encode(signature).decode("ascii"),
    )


def _authority(
    *,
    contract_digest: str = CONTRACT_DIGEST,
    provider: str = "postgresql",
    input_generation_digests: tuple[str, ...] = INPUT_DIGESTS,
) -> DbtInvocationAuthority:
    return DbtInvocationAuthority.model_validate(
        {
            "contract_digest": contract_digest,
            "provider": provider,
            "input_generation_digests": input_generation_digests,
        }
    )


def _successful_result() -> DbtProcessResult:
    return DbtProcessResult(
        return_code=0,
        observed_dbt_version=DBT_CORE_VERSION,
        manifest=MANIFEST,
        test_results=TEST_RESULTS,
        lineage=LINEAGE,
    )
