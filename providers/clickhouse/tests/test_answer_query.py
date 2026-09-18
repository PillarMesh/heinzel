from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from heinzel_provider_clickhouse import (
    ClickHouseAnswerQueryProvider,
    ClickHouseAnswerQuerySettings,
    compose_clickhouse_answer_query_provider,
)
from heinzel_provider_clickhouse.answer_query import _clickhouse_value_type
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import (
    AnswerProductGenerationReference,
    AnswerQueryParameter,
    AnswerQueryReference,
    AnswerQueryTimedOut,
    ReadOnlyAnswerQuery,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from pydantic import SecretStr


class _Response:
    def __init__(
        self,
        lines: tuple[str, ...],
        *,
        status_code: int = 200,
        exception_code: int | None = None,
        read_timeout: bool = False,
    ) -> None:
        self.status_code = status_code
        self.exception_code = exception_code
        self._lines = lines
        self._read_timeout = read_timeout
        self.closed = False
        self.close_failure = False

    def iter_lines(self) -> Iterator[str]:
        for index, line in enumerate(self._lines):
            if self._read_timeout and index == 2:
                raise httpx.ReadTimeout("private-password SELECT secret")
            yield line

    def close(self) -> None:
        self.closed = True
        if self.close_failure:
            raise OSError("private-password")


class _Transport:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def open(
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
        return self.response


def _request(**updates: object) -> ReadOnlyAnswerQuery:
    values: dict[str, object] = {
        "engine_kind": "clickhouse",
        "tenant_id": "tenant-a",
        "statement": (
            "SELECT `region`, sum(`revenue`) FROM `consumption`.`sales` "
            "GROUP BY `region` HAVING uniqExact(`customer_id`) >= {p0:Int64} LIMIT 10"
        ),
        "parameters": (AnswerQueryParameter(name="p0", value_type="integer", value=5),),
        "statement_timeout_seconds": 3,
        "row_ceiling": 10,
        "byte_ceiling": 10_000,
        "consumption_object_refs": (
            AnswerQueryReference(artifact_id="consumption:sales", version=1, digest="b" * 64),
        ),
        "product_generation_refs": (
            AnswerProductGenerationReference(
                product_ref=AnswerQueryReference(
                    artifact_id="product-1", version=1, digest="a" * 64
                ),
                generation=7,
            ),
        ),
    }
    values.update(updates)
    return ReadOnlyAnswerQuery.model_validate(values)


def _settings() -> ClickHouseAnswerQuerySettings:
    return ClickHouseAnswerQuerySettings(
        endpoint="https://clickhouse.test",
        username="answer_runtime",
        password=SecretStr("private-password"),
    )


def _success_response() -> _Response:
    return _Response(
        (
            json.dumps(["region", "revenue", "at"]),
            json.dumps(["String", "Decimal(38, 9)", "DateTime64(6, 'UTC')"]),
            json.dumps(["west", "12.50", "2026-09-11 21:00:00.000000"]),
        )
    )


def _binding(engine: EngineKind = EngineKind.CLICKHOUSE) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="warehouse-1",
        tenant_id="tenant-a",
        engine_kind=engine,
        region="local",
        capability_profile_digest="b" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=3,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        updated_at=datetime(2026, 9, 11, tzinfo=UTC),
        provisioned_at=datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_clickhouse_binds_parameters_enforces_read_only_timeout_and_streams_typed_rows() -> None:
    response = _success_response()
    transport = _Transport(response)
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=transport)

    cursor = provider.execute_read_only(_request())

    call = transport.calls[0]
    assert call["params"] == {
        "readonly": "1",
        "max_execution_time": "3",
        "max_result_rows": "11",
        "result_overflow_mode": "break",
        "cancel_http_readonly_queries_on_client_close": "1",
        "output_format_json_quote_64bit_integers": "0",
        "param_p0": "5",
    }
    headers = call["headers"]
    assert isinstance(headers, Mapping)
    authorization = headers["Authorization"]
    assert authorization == "Basic " + base64.b64encode(b"answer_runtime:private-password").decode(
        "ascii"
    )
    assert (
        call["content"]
        == (_request().statement + " FORMAT JSONCompactEachRowWithNamesAndTypes").encode()
    )
    assert tuple((column.name, column.value_type) for column in cursor.columns) == (
        ("region", "string"),
        ("revenue", "decimal"),
        ("at", "timestamp"),
    )
    assert cursor.fetchone() == (
        "west",
        Decimal("12.50"),
        datetime(2026, 9, 11, 21, tzinfo=UTC),
    )
    assert cursor.fetchone() is None
    cursor.close()
    assert response.closed is True


def test_clickhouse_cursor_cancel_closes_the_stream() -> None:
    response = _success_response()
    cursor = ClickHouseAnswerQueryProvider(
        settings=_settings(), transport=_Transport(response)
    ).execute_read_only(_request())

    cursor.cancel()

    assert response.closed is True


def test_clickhouse_read_timeout_is_typed_and_sanitized() -> None:
    response = _success_response()
    response._read_timeout = True
    cursor = ClickHouseAnswerQueryProvider(
        settings=_settings(), transport=_Transport(response)
    ).execute_read_only(_request())

    with pytest.raises(AnswerQueryTimedOut) as captured:
        cursor.fetchone()

    assert "private-password" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("status_code", "exception_code", "classification"),
    [
        (401, None, "authorization_denied"),
        (403, None, "authorization_denied"),
        (404, None, "statement_rejected"),
        (409, None, "integrity_failure"),
        (429, None, "throttled"),
        (500, None, "transient_unavailable"),
    ],
)
def test_clickhouse_http_failures_are_classified_without_secret_or_statement(
    status_code: int,
    exception_code: int | None,
    classification: str,
) -> None:
    response = _Response((), status_code=status_code, exception_code=exception_code)
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=_Transport(response))

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == classification
    assert "private-password" not in str(captured.value)
    assert "SELECT" not in str(captured.value)


def test_clickhouse_server_timeout_code_is_typed() -> None:
    response = _Response((), status_code=500, exception_code=159)
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=_Transport(response))

    with pytest.raises(AnswerQueryTimedOut):
        provider.execute_read_only(_request())


def test_clickhouse_cleanup_failure_never_replaces_classified_provider_error() -> None:
    response = _Response((), status_code=403)
    response.close_failure = True
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=_Transport(response))

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "authorization_denied"
    assert "private-password" not in str(captured.value)


def test_clickhouse_rejects_noncanonical_statement_before_opening_credentials() -> None:
    transport = _Transport(_success_response())
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request(statement="SELECT 1; DROP TABLE private_table"))

    assert captured.value.classification == "statement_rejected"
    assert transport.calls == []


def test_clickhouse_rejects_malformed_column_schema() -> None:
    response = _Response((json.dumps(["value"]), json.dumps(["Map(String,String)"])))
    provider = ClickHouseAnswerQueryProvider(settings=_settings(), transport=_Transport(response))

    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert response.closed is True


def test_clickhouse_composition_resolves_only_ready_matching_answer_credentials() -> None:
    class Authority:
        def resolve_answer_runtime(
            self, *, tenant_id: str, binding_id: str, binding_revision: int
        ) -> ClickHouseAnswerQuerySettings:
            assert (tenant_id, binding_id, binding_revision) == ("tenant-a", "warehouse-1", 3)
            return _settings()

    provider = compose_clickhouse_answer_query_provider(
        binding=_binding(),
        settings_authority=Authority(),
        transport=_Transport(_success_response()),
    )
    assert provider.engine_kind == "clickhouse"

    with pytest.raises(ProviderError) as captured:
        compose_clickhouse_answer_query_provider(
            binding=_binding(EngineKind.POSTGRESQL),
            settings_authority=Authority(),
            transport=_Transport(_success_response()),
        )
    assert captured.value.classification == "authorization_denied"


@pytest.mark.parametrize(
    ("raw_type", "expected_fragment"),
    (
        ("Float64", "floating-point"),
        ("Float32", "floating-point"),
        ("Nullable(Float64)", "floating-point"),
        ("IntervalDay", "interval"),
        ("IntervalMonth", "interval"),
    ),
)
def test_inexact_and_unit_bearing_columns_are_refused(
    raw_type: str, expected_fragment: str
) -> None:
    """A float cannot carry exact decimal semantics, and an interval is not a bare integer.

    Both previously slipped through on a prefix match: `Float` was mapped to `decimal`, and
    `IntervalDay` matched the `Int` prefix and was mapped to `integer` with its unit dropped.
    """
    with pytest.raises(ProviderError) as raised:
        _clickhouse_value_type(raw_type)

    assert expected_fragment in str(raised.value)


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    (
        ("Int64", "integer"),
        ("UInt8", "integer"),
        ("Decimal(38, 9)", "decimal"),
        ("Nullable(Decimal(38, 9))", "decimal"),
        ("String", "string"),
        ("Bool", "boolean"),
        ("DateTime64(3)", "timestamp"),
    ),
)
def test_supported_columns_keep_their_logical_type(raw_type: str, expected: str) -> None:
    assert _clickhouse_value_type(raw_type) == expected
