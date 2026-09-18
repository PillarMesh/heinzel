from __future__ import annotations

import base64
import json
import math
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_runtime import (
    AnswerQueryColumn,
    AnswerQueryCursor,
    AnswerQueryParameter,
    AnswerQueryTimedOut,
    ReadOnlyAnswerQuery,
)
from heinzel_runtime.answer_models import AnswerQueryValue
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_CLICKHOUSE_PARAMETER_TYPES = {
    "boolean": "Bool",
    "decimal": "Decimal(38, 9)",
    "integer": "Int64",
    "string": "String",
    "timestamp": "DateTime64(6, 'UTC')",
}


class ClickHouseAnswerQuerySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    principal_class: Literal["answer_runtime"] = "answer_runtime"
    endpoint: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr
    verify_tls: bool = True

    @field_validator("endpoint")
    @classmethod
    def requires_secure_or_loopback_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.scheme != "https" and not loopback_http)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ClickHouse answer endpoint must be an HTTPS or loopback HTTP origin")
        return value.rstrip("/")


class ClickHouseAnswerQuerySettingsAuthority(Protocol):
    def resolve_answer_runtime(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> ClickHouseAnswerQuerySettings: ...


class ClickHouseAnswerResponse(Protocol):
    status_code: int
    exception_code: int | None

    def iter_lines(self) -> Iterator[str]: ...

    def close(self) -> None: ...


class ClickHouseAnswerTransport(Protocol):
    def open(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseAnswerResponse: ...


class _HttpxAnswerResponse:
    def __init__(
        self,
        manager: AbstractContextManager[httpx.Response],
        response: httpx.Response,
    ) -> None:
        self._manager = manager
        self._response = response
        self.status_code = response.status_code
        raw_code = response.headers.get("X-ClickHouse-Exception-Code")
        try:
            self.exception_code = None if raw_code is None else int(raw_code)
        except ValueError:
            self.exception_code = None
        self._closed = False

    def iter_lines(self) -> Iterator[str]:
        yield from self._response.iter_lines()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._manager.__exit__(None, None, None)


class HttpxClickHouseAnswerTransport:
    def __init__(self, *, verify_tls: bool = True, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(verify=verify_tls, trust_env=False)

    def open(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseAnswerResponse:
        manager = self._client.stream(
            "POST",
            url,
            headers=headers,
            params=params,
            content=content,
            timeout=timeout_seconds,
        )
        response = manager.__enter__()
        return _HttpxAnswerResponse(manager, response)


class _ClickHouseAnswerCursor:
    suppressed_group_count = 0

    def __init__(
        self,
        *,
        response: ClickHouseAnswerResponse,
        lines: Iterator[str],
        columns: tuple[AnswerQueryColumn, ...],
    ) -> None:
        self._response = response
        self._lines = lines
        self.columns = columns
        self._closed = False

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None:
        try:
            line = next(self._lines)
        except StopIteration:
            return None
        except httpx.ReadTimeout:
            raise AnswerQueryTimedOut("ClickHouse answer query timed out") from None
        except httpx.TransportError:
            raise ProviderError(
                "ClickHouse answer query transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse answer query returned invalid rows", "invalid_provider_response"
            ) from None
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            raise ProviderError(
                "ClickHouse answer query returned invalid rows", "invalid_provider_response"
            ) from None
        if not isinstance(candidate, list) or len(candidate) != len(self.columns):
            raise ProviderError(
                "ClickHouse answer query returned invalid rows", "invalid_provider_response"
            )
        return tuple(
            _typed_value(column.value_type, value)
            for column, value in zip(self.columns, candidate, strict=True)
        )

    def cancel(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._response.close()


class ClickHouseAnswerQueryProvider:
    engine_kind: Literal["clickhouse"] = "clickhouse"

    def __init__(
        self,
        *,
        settings: ClickHouseAnswerQuerySettings,
        transport: ClickHouseAnswerTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or HttpxClickHouseAnswerTransport(
            verify_tls=settings.verify_tls
        )

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        _validate_query(request)
        params = {
            "readonly": "1",
            "max_execution_time": str(request.statement_timeout_seconds),
            "max_result_rows": str(request.row_ceiling + 1),
            "result_overflow_mode": "break",
            "cancel_http_readonly_queries_on_client_close": "1",
            "output_format_json_quote_64bit_integers": "0",
            **{
                f"param_{parameter.name}": _parameter_text(parameter)
                for parameter in request.parameters
            },
        }
        token = base64.b64encode(
            (self._settings.username + ":" + self._settings.password.get_secret_value()).encode(
                "utf-8"
            )
        ).decode("ascii")
        try:
            response = self._transport.open(
                url=self._settings.endpoint + "/",
                headers={"Authorization": f"Basic {token}"},
                params=params,
                content=(request.statement + " FORMAT JSONCompactEachRowWithNamesAndTypes").encode(
                    "utf-8"
                ),
                timeout_seconds=float(request.statement_timeout_seconds + 1),
            )
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError(
                "ClickHouse answer query transport timed out", "transient_transport"
            ) from None
        except (httpx.TransportError, OSError):
            raise ProviderError(
                "ClickHouse answer query transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse answer query transport failed", "invalid_provider_response"
            ) from None
        try:
            _raise_for_status(response)
            lines = response.iter_lines()
            columns = _read_columns(lines)
        except (AnswerQueryTimedOut, ProviderError):
            _close_response(response)
            raise
        except httpx.ReadTimeout:
            _close_response(response)
            raise AnswerQueryTimedOut("ClickHouse answer query timed out") from None
        except httpx.TransportError:
            _close_response(response)
            raise ProviderError(
                "ClickHouse answer query transport failed", "transient_transport"
            ) from None
        except Exception:
            _close_response(response)
            raise ProviderError(
                "ClickHouse answer query returned an invalid schema",
                "invalid_provider_response",
            ) from None

        return _ClickHouseAnswerCursor(response=response, lines=lines, columns=columns)


def _close_response(response: ClickHouseAnswerResponse) -> None:
    with suppress(Exception):
        response.close()


def compose_clickhouse_answer_query_provider(
    *,
    binding: WarehouseBinding,
    settings_authority: ClickHouseAnswerQuerySettingsAuthority,
    transport: ClickHouseAnswerTransport | None = None,
) -> ClickHouseAnswerQueryProvider:
    if (
        binding.engine_kind is not EngineKind.CLICKHOUSE
        or binding.lifecycle_state is not WarehouseBindingState.READY
    ):
        raise ProviderError(
            "ClickHouse answer query binding is not authorized", "authorization_denied"
        )
    try:
        settings = settings_authority.resolve_answer_runtime(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
        )
    except ProviderError:
        raise
    except Exception:
        raise ProviderError(
            "ClickHouse answer runtime credential resolution failed",
            "authorization_denied",
        ) from None
    return ClickHouseAnswerQueryProvider(settings=settings, transport=transport)


def _validate_query(request: ReadOnlyAnswerQuery) -> None:
    if (
        request.engine_kind != "clickhouse"
        or request.principal_class != "answer_runtime"
        or request.read_only is not True
        or not request.statement.startswith("SELECT ")
        or ";" in request.statement
        or " FORMAT " in request.statement.upper()
    ):
        raise ProviderError(
            "answer query is not a read-only compiled statement", "statement_rejected"
        )
    expected_names = tuple(f"p{index}" for index in range(len(request.parameters)))
    if tuple(parameter.name for parameter in request.parameters) != expected_names:
        raise ProviderError("answer query parameters are not canonical", "statement_rejected")
    for parameter in request.parameters:
        placeholder = (
            "{" + parameter.name + ":" + _CLICKHOUSE_PARAMETER_TYPES[parameter.value_type] + "}"
        )
        if request.statement.count(placeholder) != 1:
            raise ProviderError(
                "ClickHouse answer query parameters do not match the statement",
                "statement_rejected",
            )


def _parameter_text(parameter: AnswerQueryParameter) -> str:
    value = parameter.value
    if parameter.value_type == "boolean" and type(value) is bool:
        return "1" if value else "0"
    if parameter.value_type == "integer" and type(value) is int:
        return str(value)
    if parameter.value_type == "decimal" and isinstance(value, Decimal) and value.is_finite():
        return str(value)
    if parameter.value_type == "string" and isinstance(value, str):
        return value
    if (
        parameter.value_type == "timestamp"
        and isinstance(value, datetime)
        and value.tzinfo is not None
    ):
        return value.astimezone(UTC).isoformat()
    raise ProviderError("answer query parameter type is invalid", "statement_rejected")


def _raise_for_status(response: ClickHouseAnswerResponse) -> None:
    if response.exception_code == 159 or response.status_code == 408:
        raise AnswerQueryTimedOut("ClickHouse answer query timed out")
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in {401, 403}:
        classification: ProviderErrorClassification = "authorization_denied"
    elif status == 409:
        classification = "integrity_failure"
    elif status == 429:
        classification = "throttled"
    elif status >= 500:
        classification = "transient_unavailable"
    elif 400 <= status < 500:
        classification = "statement_rejected"
    else:
        classification = "invalid_provider_response"
    raise ProviderError("ClickHouse answer query failed", classification)


def _read_columns(lines: Iterator[str]) -> tuple[AnswerQueryColumn, ...]:
    try:
        names = json.loads(next(lines))
        raw_types = json.loads(next(lines))
    except (StopIteration, TypeError, ValueError):
        raise ProviderError(
            "ClickHouse answer query returned an invalid schema", "invalid_provider_response"
        ) from None
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
        or len(set(names)) != len(names)
        or not isinstance(raw_types, list)
        or len(raw_types) != len(names)
        or not all(isinstance(value, str) for value in raw_types)
    ):
        raise ProviderError(
            "ClickHouse answer query returned an invalid schema", "invalid_provider_response"
        )
    return tuple(
        AnswerQueryColumn(name=name, value_type=_clickhouse_value_type(raw_type))
        for name, raw_type in zip(names, raw_types, strict=True)
    )


def _clickhouse_value_type(
    raw_type: str,
) -> Literal["boolean", "decimal", "integer", "string", "timestamp"]:
    value = raw_type
    while value.startswith("Nullable(") and value.endswith(")"):
        value = value[9:-1]
    if value == "Bool":
        return "boolean"
    # Interval types are named IntervalDay, IntervalMonth and so on, so a bare "Int"
    # prefix would type them as plain integers and discard the unit. Refuse them
    # before the prefix test rather than after it.
    if value.startswith("Interval"):
        raise ProviderError(
            "ClickHouse answer query returned an interval column", "invalid_provider_response"
        )
    if value.startswith(("Int", "UInt")):
        return "integer"
    # Float is deliberately not decimal. A binary float cannot carry the exact
    # Decimal(38,9) semantics the answer contract states, and AnswerQueryValueType has
    # no float member, so admitting one here would present an inexact value as exact.
    if value.startswith("Float"):
        raise ProviderError(
            "ClickHouse answer query returned a floating-point column",
            "invalid_provider_response",
        )
    if value.startswith("Decimal"):
        return "decimal"
    if value.startswith(("String", "FixedString", "LowCardinality(String", "UUID")):
        return "string"
    if value.startswith(("DateTime", "Date")):
        return "timestamp"
    raise ProviderError(
        "ClickHouse answer query returned an unsupported schema", "invalid_provider_response"
    )


def _typed_value(value_type: str, value: object) -> AnswerQueryValue:
    if value is None:
        return None
    if value_type == "boolean" and type(value) is bool:
        return value
    if value_type == "integer" and type(value) is int:
        return value
    if value_type == "decimal" and isinstance(value, (str, int, float)) and type(value) is not bool:
        try:
            converted = Decimal(str(value))
        except InvalidOperation:
            pass
        else:
            if converted.is_finite() and not (
                isinstance(value, float) and not math.isfinite(value)
            ):
                return converted
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "timestamp" and isinstance(value, str):
        try:
            converted_timestamp = datetime.fromisoformat(value.replace(" ", "T", 1))
        except ValueError:
            pass
        else:
            if converted_timestamp.tzinfo is None:
                converted_timestamp = converted_timestamp.replace(tzinfo=UTC)
            return converted_timestamp.astimezone(UTC)
    raise ProviderError(
        "ClickHouse answer query returned a value outside its schema",
        "invalid_provider_response",
    )
