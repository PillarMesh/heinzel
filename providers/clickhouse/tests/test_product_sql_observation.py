from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import digest
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_provider_clickhouse import (
    ClickHouseProductMagnitudeObservationRequest,
    ClickHouseProductMagnitudeObserver,
    ClickHouseProductSqlObservationRequest,
    ClickHouseProductSqlObservationSettings,
    ClickHouseProductSqlObserver,
)
from pillarmesh_provider_sdk import (
    InvalidProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlProviderObservationVerifier,
    ProviderError,
)
from pillarmesh_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pydantic import SecretStr

_NOW = datetime(2026, 9, 15, 18, 30, tzinfo=UTC)
_IMAGE_DIGEST = "7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"


class _Response:
    def __init__(
        self,
        lines: tuple[str, ...] = (),
        *,
        status_code: int = 200,
        exception_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.exception_code = exception_code
        self.lines = lines


class _Transport:
    def __init__(self, responses: tuple[_Response | BaseException, ...]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> _Response:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "content": content,
                "timeout_seconds": timeout_seconds,
            }
        )
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        return response


def _settings() -> ClickHouseProductSqlObservationSettings:
    return ClickHouseProductSqlObservationSettings(
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        endpoint="https://clickhouse.test",
        username="product_observer",
        password=SecretStr("private-password"),
    )


def test_observer_rejects_credentials_for_another_binding_before_network_access() -> None:
    transport = _Transport(())
    settings = _settings().model_copy(update={"warehouse_binding_id": "warehouse-2"})
    observer = ClickHouseProductSqlObserver(settings=settings, transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "authorization_denied"
    assert transport.calls == []


def _request(**updates: object) -> ClickHouseProductSqlObservationRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "warehouse_binding_id": "warehouse-1",
        "warehouse_binding_revision": 3,
        "relation_ref": "catalog:raw.revenue_events@7",
        "relation_namespace": "raw",
        "relation_name": "revenue_events",
        "column_names": ("region", "revenue"),
    }
    values.update(updates)
    return ClickHouseProductSqlObservationRequest.model_validate(values)


def _evidence(**updates: object) -> WarehouseValidationEvidence:
    values: dict[str, object] = {
        "evidence_id": "warehouse-validation-1",
        "tenant_id": "tenant-a",
        "binding_id": "warehouse-1",
        "binding_revision": 3,
        "validation_profile": WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        "engine_kind": EngineKind.CLICKHOUSE,
        "engine_version": "25.8.32.4",
        "engine_build_digest": "b" * 64,
        "engine_image_digest": _IMAGE_DIGEST,
        "principal_profile_digest": "1" * 64,
        "namespace_grant_matrix_digest": "2" * 64,
        "tls_probe_digest": "3" * 64,
        "network_isolation_probe_digest": "4" * 64,
        "encryption_at_rest_evidence_digest": "5" * 64,
        "encryption_at_rest_disposition": (EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
        "positive_probe_digest": "6" * 64,
        "denial_probe_digest": "7" * 64,
        "ledger_probe_digest": "8" * 64,
        "monitoring_probe_digest": "9" * 64,
        "capacity_alert_probe_digest": "a" * 64,
        "backup_artifact_digest": "c" * 64,
        "restore_verification_digest": "d" * 64,
        "restore_cleanup_digest": "e" * 64,
        "observed_at": _NOW,
    }
    values.update(updates)
    return WarehouseValidationEvidence.model_validate(values)


def _success_transport() -> _Transport:
    return _Transport(
        (
            _Response(
                (
                    "raw\trevenue_events\tregion\tString\t1",
                    "raw\trevenue_events\trevenue\tDecimal(38, 9)\t2",
                )
            ),
            _Response(),
            _Response(
                (
                    "25.8.32.4\tDecimal(38, 9)\tDecimal(38, 9)\tDecimal(38, 9)"
                    "\twrap\texclude\tno_row\tbinary\tUTF-8",
                )
            ),
        )
    )


def _signed_magnitude_model(
    private_key: Ed25519PrivateKey,
    *,
    checks: tuple[DbtDecimalMagnitudeCheck, ...] = (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        DbtDecimalMagnitudeCheck(column_name="tax"),
    ),
) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="product_revenue_g3",
        contract_digest="1" * 64,
        provider="clickhouse",
        input_generation_digests=("2" * 64,),
        target_schema="product_generation_3",
        output_columns=("region", "total_revenue", "tax"),
        output_magnitude_checks=checks,
        compiled_sql="SELECT region, total_revenue, tax FROM raw.revenue",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-1",
        signature=base64.b64encode(
            private_key.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


def _magnitude_request() -> ClickHouseProductMagnitudeObservationRequest:
    return ClickHouseProductMagnitudeObservationRequest(
        tenant_id="tenant-a",
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        relation_namespace="product_generation_3",
        relation_name="product_revenue_g3",
    )


def test_magnitude_observer_executes_one_ordered_exact_bound_query() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_model = _signed_magnitude_model(private_key)
    transport = _Transport((_Response(("total_revenue", "tax")), _Response(("0\t0",))))
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=signed_model,
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=transport,
        clock=lambda: _NOW,
    )

    observation = observer.observe(_magnitude_request())

    assert observation.model_digest == signed_model.model_digest
    assert tuple(result.declaration for result in observation.results) == (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        DbtDecimalMagnitudeCheck(column_name="tax"),
    )
    assert tuple(result.violation_count for result in observation.results) == (0, 0)
    assert observation.observed_at == _NOW
    assert observation.observation_digest == digest(
        observation.model_dump(exclude={"observation_digest"}, mode="python")
    )
    assert len(transport.calls) == 2
    magnitude_call = transport.calls[1]
    assert magnitude_call["params"] == {
        "readonly": "2",
        "param_lower_bound": "-1000000000000000000000000000000000000000000000000.000000000",
        "param_upper_bound": "1000000000000000000000000000000000000000000000000.000000000",
    }
    content = magnitude_call["content"]
    assert isinstance(content, bytes)
    assert content.count(b"countIf") == 2
    assert content.index(b"`total_revenue`") < content.index(b"`tax`")
    assert b" <= toDecimal256({lower_bound:String}, 9)" in content
    assert b" >= toDecimal256({upper_bound:String}, 9)" in content


@pytest.mark.parametrize("line", ["1\t0", "0\t1"])
def test_magnitude_observer_rejects_each_exclusive_bound_violation(line: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=_signed_magnitude_model(private_key),
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=_Transport((_Response(("total_revenue", "tax")), _Response((line,)))),
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_magnitude_request())

    assert captured.value.classification == "integrity_failure"


@pytest.mark.parametrize("line", ["", "0", "0\ttrue", "0\t-1", "0\t+0", "0\t00", "0\t0\t0"])
def test_magnitude_observer_rejects_malformed_counts(line: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    lines = () if line == "" else (line,)
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=_signed_magnitude_model(private_key),
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=_Transport((_Response(("total_revenue", "tax")), _Response(lines))),
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_magnitude_request())

    assert captured.value.classification == "invalid_provider_response"


def test_magnitude_observer_rejects_an_absent_signed_column_before_the_value_query() -> None:
    private_key = Ed25519PrivateKey.generate()
    transport = _Transport((_Response(("total_revenue",)),))
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=_signed_magnitude_model(private_key),
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=transport,
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_magnitude_request())

    assert captured.value.classification == "invalid_provider_response"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("tampering", ["digest", "check_order"])
def test_magnitude_observer_rejects_a_tampered_signed_declaration_before_network_access(
    tampering: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_model = _signed_magnitude_model(private_key)
    if tampering == "digest":
        signed_model = signed_model.model_copy(update={"model_digest": "f" * 64})
    else:
        signed_model = signed_model.model_copy(
            update={
                "model": signed_model.model.model_copy(
                    update={
                        "output_magnitude_checks": tuple(
                            reversed(signed_model.model.output_magnitude_checks)
                        )
                    }
                )
            }
        )
    transport = _Transport(())
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=signed_model,
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=transport,
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_magnitude_request())

    assert captured.value.classification == "integrity_failure"
    assert transport.calls == []


def test_magnitude_observer_rejects_unknown_request_fields_before_network_access() -> None:
    private_key = Ed25519PrivateKey.generate()
    request = _magnitude_request()
    object.__setattr__(request, "hidden", "untrusted")
    transport = _Transport(())
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=_signed_magnitude_model(private_key),
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=transport,
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(request)

    assert captured.value.classification == "invalid_provider_response"
    assert transport.calls == []


def test_magnitude_observer_classifies_transport_failure() -> None:
    private_key = Ed25519PrivateKey.generate()
    observer = ClickHouseProductMagnitudeObserver(
        settings=_settings(),
        signed_model=_signed_magnitude_model(private_key),
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
        transport=_Transport((httpx.ConnectError("private-password"),)),
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_magnitude_request())

    assert captured.value.classification == "transient_transport"
    assert "private-password" not in str(captured.value)


@pytest.mark.parametrize(
    ("request_value", "evidence_value"),
    [
        (
            _request().model_construct(
                tenant_id="private-invalid-tenant",
                warehouse_binding_id="warehouse-1",
                warehouse_binding_revision=3,
                relation_ref="catalog:raw.revenue_events@7",
                relation_namespace="not-valid!",
                relation_name="revenue_events",
                column_names=("region", "revenue"),
            ),
            _evidence(),
        ),
        (
            _request(),
            _evidence().model_construct(
                **{
                    **_evidence().model_dump(mode="python"),
                    "engine_build_digest": "private-invalid-digest",
                }
            ),
        ),
    ],
)
def test_observer_translates_invalid_boundary_models_without_leaking_values(
    request_value: ClickHouseProductSqlObservationRequest,
    evidence_value: WarehouseValidationEvidence,
) -> None:
    transport = _Transport(())
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(request_value, warehouse_validation=evidence_value)

    assert captured.value.classification == "invalid_provider_response"
    assert "private-invalid" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert transport.calls == []


def test_observer_translates_invalid_clock_result_without_leaking_values() -> None:
    observer = ClickHouseProductSqlObserver(
        settings=_settings(), transport=_success_transport(), clock=lambda: datetime(2026, 9, 15)
    )

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.__cause__ is None


def test_observer_binds_exact_relation_engine_and_read_only_semantics() -> None:
    transport = _success_transport()
    observer = ClickHouseProductSqlObserver(
        settings=_settings(), transport=transport, clock=lambda: _NOW
    )

    observation = observer.observe(_request(), warehouse_validation=_evidence())

    assert observation.tenant_id == "tenant-a"
    assert observation.warehouse_binding_id == "warehouse-1"
    assert observation.warehouse_binding_revision == 3
    assert observation.relation_ref == "catalog:raw.revenue_events@7"
    assert observation.relation_namespace == "raw"
    assert observation.relation_name == "revenue_events"
    assert observation.engine == "clickhouse"
    assert observation.engine_version == "25.8.32.4"
    assert observation.engine_image_digest == _IMAGE_DIGEST
    assert observation.engine_build_digest == "b" * 64
    assert tuple(column.model_dump() for column in observation.columns) == (
        {
            "name": "region",
            "logical_type": "string",
            "physical_type": "String",
            "nullable": False,
            "decimal_precision": None,
            "decimal_scale": None,
            "collation": "binary",
            "encoding": "UTF-8",
        },
        {
            "name": "revenue",
            "logical_type": "decimal",
            "physical_type": "Decimal(38, 9)",
            "nullable": False,
            "decimal_precision": 38,
            "decimal_scale": 9,
            "collation": None,
            "encoding": None,
        },
    )
    assert observation.sum_semantics.model_dump() == {
        "input_physical_type": "Decimal(38, 9)",
        "accumulator_physical_type": "Decimal(38, 9)",
        "result_physical_type": "Decimal(38, 9)",
        "overflow_behavior": "wrap",
        "null_input_behavior": "exclude",
        "empty_group_behavior": "no_row",
    }
    assert observation.observed_at == _NOW
    assert len(observation.observation_id) == 64
    assert len(transport.calls) == 3
    params = tuple(call["params"] for call in transport.calls)
    contents = tuple(call["content"] for call in transport.calls)
    assert all(isinstance(value, Mapping) and value["readonly"] == "2" for value in params)
    assert all(isinstance(value, bytes) and b"private-password" not in value for value in contents)
    assert isinstance(contents[0], bytes) and b"system.columns" in contents[0]
    assert isinstance(contents[1], bytes) and b"`raw`.`revenue_events`" in contents[1]
    assert isinstance(contents[2], bytes) and b"version()" in contents[2]


def test_signed_observation_matches_the_exact_raw_observation_and_verifies() -> None:
    private_key = Ed25519PrivateKey.generate()
    raw_observer = ClickHouseProductSqlObserver(
        settings=_settings(), transport=_success_transport(), clock=lambda: _NOW
    )
    signed_transport = _success_transport()
    signed_observer = ClickHouseProductSqlObserver(
        settings=_settings(),
        transport=signed_transport,
        clock=lambda: _NOW,
        signer=ProductSqlProviderObservationSigner("clickhouse-provider-1", private_key),
    )

    raw = raw_observer.observe(_request(), warehouse_validation=_evidence())
    signed = signed_observer.observe_signed(_request(), warehouse_validation=_evidence())
    verified = ProductSqlProviderObservationVerifier(
        {"clickhouse-provider-1": private_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    ).verify(signed, evaluated_at=_NOW)

    assert signed.observation == raw
    assert signed.observation_digest == digest(raw)
    assert verified == raw
    assert len(signed_transport.calls) == 3


def test_signed_observation_rejects_signer_key_substitution() -> None:
    private_key = Ed25519PrivateKey.generate()
    substituted_key = Ed25519PrivateKey.generate()
    observer = ClickHouseProductSqlObserver(
        settings=_settings(),
        transport=_success_transport(),
        clock=lambda: _NOW,
        signer=ProductSqlProviderObservationSigner("clickhouse-provider-1", private_key),
    )
    signed = observer.observe_signed(_request(), warehouse_validation=_evidence()).model_copy(
        update={"key_id": "clickhouse-provider-2"}
    )
    verifier = ProductSqlProviderObservationVerifier(
        {"clickhouse-provider-2": substituted_key.public_key()},
        maximum_observation_age=timedelta(minutes=5),
    )

    with pytest.raises(InvalidProductSqlProviderObservation, match="signature"):
        verifier.verify(signed, evaluated_at=_NOW)


def test_signed_observation_requires_a_signer_before_transport() -> None:
    transport = _Transport(())
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe_signed(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "permanent_configuration"
    assert transport.calls == []


def test_observer_rejects_malformed_signer_configuration_before_transport() -> None:
    transport = _Transport(())

    with pytest.raises(ProviderError) as captured:
        ClickHouseProductSqlObserver(
            settings=_settings(),
            transport=transport,
            signer=cast(ProductSqlProviderObservationSigner, object()),
        )

    assert captured.value.classification == "permanent_configuration"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("updates", "classification"),
    [
        ({"tenant_id": "tenant-b"}, "integrity_failure"),
        ({"binding_id": "warehouse-2"}, "integrity_failure"),
        ({"binding_revision": 2}, "integrity_failure"),
        ({"engine_kind": EngineKind.POSTGRESQL}, "integrity_failure"),
        ({"engine_version": "25.8.31.1"}, "integrity_failure"),
        ({"engine_image_digest": "f" * 64}, "integrity_failure"),
    ],
)
def test_observer_rejects_mismatched_warehouse_evidence_before_network_access(
    updates: dict[str, object], classification: str
) -> None:
    transport = _Transport(())
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence(**updates))

    assert captured.value.classification == classification
    assert transport.calls == []


def test_observer_classifies_relation_read_denial_without_leaking_details() -> None:
    transport = _Transport(
        (
            _Response(
                (
                    "raw\trevenue_events\tregion\tString\t1",
                    "raw\trevenue_events\trevenue\tDecimal(38, 9)\t2",
                )
            ),
            _Response(status_code=403),
        )
    )
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "authorization_denied"
    assert "private-password" not in str(captured.value)
    assert "revenue_events" not in str(captured.value)


def test_observer_classifies_transport_failure() -> None:
    transport = _Transport((httpx.ConnectError("private-password"),))
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "transient_transport"
    assert "private-password" not in str(captured.value)


@pytest.mark.parametrize(
    "lines",
    [
        ("raw\trevenue_events\tregion\tNullable(String)\t1",),
        (
            "raw\trevenue_events\tregion\tString\t1",
            "raw\trevenue_events\trevenue\tDecimal(18, 2)\t2",
        ),
        (
            "raw\trevenue_events\tregion\tString\t1",
            "raw\trevenue_events\tother\tDecimal(38, 9)\t2",
        ),
        ("raw\trevenue_events\tregion\tString\tnot-an-integer",),
    ],
)
def test_observer_rejects_malformed_or_incomplete_physical_columns(lines: tuple[str, ...]) -> None:
    transport = _Transport((_Response(lines),))
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "invalid_provider_response"


def test_observer_rejects_semantic_probe_that_disagrees_with_pinned_engine() -> None:
    transport = _success_transport()
    transport._responses = iter(
        (
            _Response(
                (
                    "raw\trevenue_events\tregion\tString\t1",
                    "raw\trevenue_events\trevenue\tDecimal(38, 9)\t2",
                )
            ),
            _Response(),
            _Response(
                (
                    "25.8.32.4\tDecimal(38, 9)\tDecimal256(9)\tDecimal256(9)"
                    "\tpromote\texclude\tno_row\tbinary\tUTF-8",
                )
            ),
        )
    )
    observer = ClickHouseProductSqlObserver(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert captured.value.classification == "integrity_failure"
