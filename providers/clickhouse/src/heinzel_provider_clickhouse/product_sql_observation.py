from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from heinzel_contract_model import digest
from heinzel_dbt_adapter import (
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from heinzel_provider_sdk import (
    ProductSqlColumnObservation,
    ProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlSumSemantics,
    ProviderError,
    SignedProductSqlProviderObservation,
)
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_warehouse_control import EngineKind, WarehouseValidationEvidence
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .settings import CLICKHOUSE_SERVER_VERSION, CLICKHOUSE_WAREHOUSE_IMAGE

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_IMAGE_DIGEST = CLICKHOUSE_WAREHOUSE_IMAGE.rpartition("sha256:")[2]
_DECIMAL_57_9_EXCLUSIVE_BOUND = "1" + ("0" * 48) + ".000000000"
_EXPECTED_SEMANTICS = (
    CLICKHOUSE_SERVER_VERSION,
    "Decimal(38, 9)",
    "Decimal(38, 9)",
    "Decimal(38, 9)",
    "wrap",
    "exclude",
    "no_row",
    "binary",
    "UTF-8",
)


class _ProductSqlObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClickHouseProductSqlObservationRequest(_ProductSqlObservationModel):
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    relation_ref: str = Field(min_length=1, max_length=255)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    column_names: tuple[str, ...] = Field(min_length=1)

    @field_validator("column_names")
    @classmethod
    def columns_are_canonical_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _is_identifier(name) for name in value) or len(value) != len(set(value)):
            raise ValueError("column names must be unique canonical identifiers")
        return value


class ClickHouseProductSqlObservationSettings(_ProductSqlObservationModel):
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    endpoint: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr
    verify_tls: bool = True
    timeout_seconds: int = Field(default=10, gt=0, le=60)

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_secure_or_loopback(cls, value: str) -> str:
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
            raise ValueError("ClickHouse observation endpoint must be HTTPS or loopback HTTP")
        return value.rstrip("/")


class ClickHouseProductMagnitudeObservationRequest(_ProductSqlObservationModel):
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)


class ClickHouseDecimalMagnitudeObservation(_ProductSqlObservationModel):
    declaration: DbtDecimalMagnitudeCheck
    violation_count: Literal[0] = 0


class ClickHouseProductMagnitudeObservation(_ProductSqlObservationModel):
    schema_version: Literal["1"] = "1"
    engine: Literal["clickhouse"] = "clickhouse"
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    model_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    results: tuple[ClickHouseDecimalMagnitudeObservation, ...] = Field(min_length=1)
    observed_at: datetime
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("magnitude observation timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def digest_and_order_are_canonical(self) -> ClickHouseProductMagnitudeObservation:
        names = tuple(result.declaration.column_name for result in self.results)
        if len(names) != len(set(names)):
            raise ValueError("magnitude observation columns must be unique")
        expected = digest(self.model_dump(exclude={"observation_digest"}, mode="python"))
        if self.observation_digest != expected:
            raise ValueError("magnitude observation digest does not match")
        return self


class ClickHouseProductSqlObservationResponse(Protocol):
    status_code: int
    exception_code: int | None
    lines: tuple[str, ...]


class ClickHouseProductSqlObservationTransport(Protocol):
    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseProductSqlObservationResponse: ...


class _HttpxObservationResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        raw_code = response.headers.get("X-ClickHouse-Exception-Code")
        try:
            self.exception_code = None if raw_code is None else int(raw_code)
        except ValueError:
            self.exception_code = None
        self.lines = tuple(response.text.splitlines())


class HttpxClickHouseProductSqlObservationTransport:
    def __init__(self, *, verify_tls: bool = True, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(verify=verify_tls, trust_env=False)

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseProductSqlObservationResponse:
        return _HttpxObservationResponse(
            self._client.post(
                url,
                headers=headers,
                params=params,
                content=content,
                timeout=timeout_seconds,
            )
        )


class ClickHouseProductMagnitudeObserver:
    """Observe signed Decimal(57,9) output bounds in one read-only ClickHouse query."""

    def __init__(
        self,
        *,
        settings: ClickHouseProductSqlObservationSettings,
        signed_model: SignedCompiledDbtModel,
        trusted_compiler_keys: Mapping[str, Ed25519PublicKey],
        transport: ClickHouseProductSqlObservationTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = ClickHouseProductSqlObservationSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._signed_model = signed_model
        self._trusted_compiler_keys = dict(trusted_compiler_keys)
        self._transport = transport or HttpxClickHouseProductSqlObservationTransport(
            verify_tls=settings.verify_tls
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def observe(
        self, request: ClickHouseProductMagnitudeObservationRequest
    ) -> ClickHouseProductMagnitudeObservation:
        try:
            if not set(vars(request)).issubset(type(request).model_fields):
                raise ValueError("magnitude request contains undeclared fields")
            request = ClickHouseProductMagnitudeObservationRequest.model_validate(
                request.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse magnitude observation received an invalid boundary model",
                "invalid_provider_response",
            ) from None
        if (
            self._settings.tenant_id != request.tenant_id
            or self._settings.warehouse_binding_id != request.warehouse_binding_id
            or self._settings.warehouse_binding_revision != request.warehouse_binding_revision
        ):
            raise ProviderError(
                "ClickHouse magnitude observation credentials are not authorized for the binding",
                "authorization_denied",
            )
        signed_model = _validated_signed_clickhouse_model(
            self._signed_model,
            trusted_compiler_keys=self._trusted_compiler_keys,
            relation_namespace=request.relation_namespace,
            relation_name=request.relation_name,
        )
        checks = signed_model.model.output_magnitude_checks
        if not checks:
            raise ProviderError(
                "ClickHouse magnitude observation has no signed declarations",
                "integrity_failure",
            )
        self._require_columns(request, checks=checks)
        violation_counts = self._observe_magnitudes(request, checks=checks)
        if any(count != 0 for count in violation_counts):
            raise ProviderError(
                "ClickHouse product output violates its signed decimal magnitude bound",
                "integrity_failure",
            )
        try:
            observed_at = self._clock()
            payload: dict[str, object] = {
                "schema_version": "1",
                "engine": "clickhouse",
                "tenant_id": request.tenant_id,
                "warehouse_binding_id": request.warehouse_binding_id,
                "warehouse_binding_revision": request.warehouse_binding_revision,
                "relation_namespace": request.relation_namespace,
                "relation_name": request.relation_name,
                "model_digest": signed_model.model_digest,
                "results": tuple(
                    ClickHouseDecimalMagnitudeObservation(
                        declaration=check,
                        violation_count=0,
                    )
                    for check in checks
                ),
                "observed_at": observed_at,
            }
            return ClickHouseProductMagnitudeObservation.model_validate(
                {**payload, "observation_digest": digest(payload)}, strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse returned an invalid magnitude observation",
                "invalid_provider_response",
            ) from None

    def _require_columns(
        self,
        request: ClickHouseProductMagnitudeObservationRequest,
        *,
        checks: tuple[DbtDecimalMagnitudeCheck, ...],
    ) -> None:
        predicates = ", ".join("{" + f"column_{index}:String" + "}" for index in range(len(checks)))
        response = self._execute(
            (
                "SELECT name FROM system.columns WHERE database = {database:String} "
                "AND table = {table:String} "
                f"AND name IN ({predicates}) FORMAT TabSeparatedRaw"
            ),
            parameters={
                "param_database": request.relation_namespace,
                "param_table": request.relation_name,
                **{
                    f"param_column_{index}": check.column_name for index, check in enumerate(checks)
                },
            },
        )
        observed_names = response.lines
        expected_names = tuple(check.column_name for check in checks)
        if len(observed_names) != len(set(observed_names)) or set(observed_names) != set(
            expected_names
        ):
            raise ProviderError(
                "ClickHouse magnitude check column is absent from product output",
                "invalid_provider_response",
            )

    def _observe_magnitudes(
        self,
        request: ClickHouseProductMagnitudeObservationRequest,
        *,
        checks: tuple[DbtDecimalMagnitudeCheck, ...],
    ) -> tuple[int, ...]:
        expressions = ", ".join(
            (
                f"countIf(isNull(`{check.column_name}`) OR "
                f"toDecimal256(`{check.column_name}`, 9) "
                "<= toDecimal256({lower_bound:String}, 9) OR "
                f"toDecimal256(`{check.column_name}`, 9) "
                ">= toDecimal256({upper_bound:String}, 9))"
            )
            for check in checks
        )
        response = self._execute(
            (
                f"SELECT {expressions} FROM `{request.relation_namespace}`."
                f"`{request.relation_name}` FORMAT TabSeparatedRaw"
            ),
            parameters={
                "param_lower_bound": "-" + _DECIMAL_57_9_EXCLUSIVE_BOUND,
                "param_upper_bound": _DECIMAL_57_9_EXCLUSIVE_BOUND,
            },
        )
        if len(response.lines) != 1:
            raise ProviderError(
                "ClickHouse magnitude check result is invalid",
                "invalid_provider_response",
            )
        raw_counts = response.lines[0].split("\t")
        if len(raw_counts) != len(checks) or any(
            not _is_canonical_nonnegative_integer(value) for value in raw_counts
        ):
            raise ProviderError(
                "ClickHouse magnitude check result is invalid",
                "invalid_provider_response",
            )
        return tuple(int(value) for value in raw_counts)

    def _execute(
        self, statement: str, *, parameters: Mapping[str, str] | None = None
    ) -> ClickHouseProductSqlObservationResponse:
        try:
            response = self._transport.execute(
                url=self._settings.endpoint + "/",
                headers=_observation_headers(self._settings),
                params={"readonly": "2", **(parameters or {})},
                content=statement.encode("utf-8"),
                timeout_seconds=float(self._settings.timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.TransportError, OSError, TimeoutError):
            raise ProviderError(
                "ClickHouse magnitude observation transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse magnitude observation transport returned an invalid response",
                "invalid_provider_response",
            ) from None
        _raise_for_status(response)
        return response


class ClickHouseProductSqlObserver:
    def __init__(
        self,
        *,
        settings: ClickHouseProductSqlObservationSettings,
        transport: ClickHouseProductSqlObservationTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        signer: ProductSqlProviderObservationSigner | None = None,
    ) -> None:
        if signer is not None and not isinstance(signer, ProductSqlProviderObservationSigner):
            raise ProviderError(
                "ClickHouse product SQL observation signer configuration is invalid",
                "permanent_configuration",
            )
        self._settings = ClickHouseProductSqlObservationSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._transport = transport or HttpxClickHouseProductSqlObservationTransport(
            verify_tls=settings.verify_tls
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._signer = signer

    def observe_signed(
        self,
        request: ClickHouseProductSqlObservationRequest,
        *,
        warehouse_validation: WarehouseValidationEvidence,
    ) -> SignedProductSqlProviderObservation:
        if self._signer is None:
            raise ProviderError(
                "ClickHouse product SQL observation signer is not configured",
                "permanent_configuration",
            )
        observation = self.observe(request, warehouse_validation=warehouse_validation)
        try:
            return self._signer.sign(observation)
        except Exception:
            raise ProviderError(
                "ClickHouse product SQL observation signer configuration is invalid",
                "permanent_configuration",
            ) from None

    def observe(
        self,
        request: ClickHouseProductSqlObservationRequest,
        *,
        warehouse_validation: WarehouseValidationEvidence,
    ) -> ProductSqlProviderObservation:
        try:
            request = ClickHouseProductSqlObservationRequest.model_validate(
                request.model_dump(mode="python"), strict=True
            )
            warehouse_validation = WarehouseValidationEvidence.model_validate(
                warehouse_validation.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse product SQL observation received an invalid boundary model",
                "invalid_provider_response",
            ) from None
        if (
            self._settings.tenant_id != request.tenant_id
            or self._settings.warehouse_binding_id != request.warehouse_binding_id
            or self._settings.warehouse_binding_revision != request.warehouse_binding_revision
        ):
            raise ProviderError(
                "ClickHouse product observation credentials are not authorized for the binding",
                "authorization_denied",
            )
        self._validate_authority(request, warehouse_validation)

        columns = self._observe_columns(request)
        self._validate_relation_read(request)
        semantics = self._observe_semantics()
        try:
            observed_at = self._clock()
            observation_id = digest(
                {
                    "domain": "heinzel-clickhouse-product-sql-observation-v1",
                    "request": request,
                    "warehouse_validation": warehouse_validation,
                    "observed_at": observed_at,
                    "columns": columns,
                    "sum_semantics": semantics,
                }
            )
            return ProductSqlProviderObservation(
                observation_id=observation_id,
                tenant_id=request.tenant_id,
                warehouse_binding_id=request.warehouse_binding_id,
                warehouse_binding_revision=request.warehouse_binding_revision,
                relation_ref=request.relation_ref,
                relation_namespace=request.relation_namespace,
                relation_name=request.relation_name,
                engine="clickhouse",
                engine_version=warehouse_validation.engine_version,
                engine_image_digest=warehouse_validation.engine_image_digest,
                engine_build_digest=warehouse_validation.engine_build_digest,
                observed_at=observed_at,
                columns=columns,
                sum_semantics=semantics,
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse returned an invalid product SQL observation",
                "invalid_provider_response",
            ) from None

    @staticmethod
    def _validate_authority(
        request: ClickHouseProductSqlObservationRequest,
        evidence: WarehouseValidationEvidence,
    ) -> None:
        if (
            evidence.tenant_id != request.tenant_id
            or evidence.binding_id != request.warehouse_binding_id
            or evidence.binding_revision != request.warehouse_binding_revision
            or evidence.engine_kind is not EngineKind.CLICKHOUSE
            or evidence.engine_version != CLICKHOUSE_SERVER_VERSION
            or evidence.engine_image_digest != _IMAGE_DIGEST
        ):
            raise ProviderError(
                "ClickHouse warehouse validation evidence does not match the observation request",
                "integrity_failure",
            )

    def _observe_columns(
        self, request: ClickHouseProductSqlObservationRequest
    ) -> tuple[ProductSqlColumnObservation, ...]:
        column_predicates = ", ".join(
            "{" + f"column_{index}:String" + "}" for index in range(len(request.column_names))
        )
        response = self._execute(
            (
                "SELECT database, table, name, type, position FROM system.columns "
                "WHERE database = {database:String} AND table = {table:String} "
                f"AND name IN ({column_predicates}) ORDER BY position FORMAT TabSeparatedRaw"
            ),
            parameters={
                "param_database": request.relation_namespace,
                "param_table": request.relation_name,
                **{
                    f"param_column_{index}": name for index, name in enumerate(request.column_names)
                },
            },
        )
        try:
            parsed = tuple(
                _parse_column_line(
                    line,
                    expected_namespace=request.relation_namespace,
                    expected_relation=request.relation_name,
                )
                for line in response.lines
            )
        except (TypeError, ValueError):
            raise ProviderError(
                "ClickHouse returned invalid product relation metadata",
                "invalid_provider_response",
            ) from None
        if tuple(column.name for column in parsed) != request.column_names:
            raise ProviderError(
                "ClickHouse returned incomplete product relation metadata",
                "invalid_provider_response",
            )
        return parsed

    def _validate_relation_read(self, request: ClickHouseProductSqlObservationRequest) -> None:
        columns = ", ".join(f"`{name}`" for name in request.column_names)
        response = self._execute(
            f"SELECT {columns} FROM `{request.relation_namespace}`.`{request.relation_name}` "
            "LIMIT 0 FORMAT TabSeparatedRaw"
        )
        if response.lines:
            raise ProviderError(
                "ClickHouse returned rows for a zero-row product relation probe",
                "invalid_provider_response",
            )

    def _observe_semantics(self) -> ProductSqlSumSemantics:
        response = self._execute(_SEMANTICS_QUERY)
        if len(response.lines) != 1:
            raise ProviderError(
                "ClickHouse returned invalid product SQL semantics",
                "invalid_provider_response",
            )
        observed = tuple(response.lines[0].split("\t"))
        if observed != _EXPECTED_SEMANTICS:
            raise ProviderError(
                "ClickHouse product SQL semantics disagree with the pinned engine",
                "integrity_failure",
            )
        return ProductSqlSumSemantics(
            input_physical_type=observed[1],
            accumulator_physical_type=observed[2],
            result_physical_type=observed[3],
            overflow_behavior="wrap",
            null_input_behavior="exclude",
            empty_group_behavior="no_row",
        )

    def _execute(
        self, statement: str, *, parameters: Mapping[str, str] | None = None
    ) -> ClickHouseProductSqlObservationResponse:
        try:
            response = self._transport.execute(
                url=self._settings.endpoint + "/",
                headers=self._headers(),
                params={"readonly": "2", **(parameters or {})},
                content=statement.encode("utf-8"),
                timeout_seconds=float(self._settings.timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.TransportError, OSError, TimeoutError):
            raise ProviderError(
                "ClickHouse product SQL observation transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse product SQL observation transport returned an invalid response",
                "invalid_provider_response",
            ) from None
        _raise_for_status(response)
        return response

    def _headers(self) -> Mapping[str, str]:
        return _observation_headers(self._settings)


def _observation_headers(settings: ClickHouseProductSqlObservationSettings) -> Mapping[str, str]:
    credential = base64.b64encode(
        f"{settings.username}:{settings.password.get_secret_value()}".encode()
    ).decode("ascii")
    return {
        "Authorization": f"Basic {credential}",
        "Content-Type": "text/plain; charset=utf-8",
    }


def _validated_signed_clickhouse_model(
    signed_model: SignedCompiledDbtModel,
    *,
    trusted_compiler_keys: Mapping[str, Ed25519PublicKey],
    relation_namespace: str,
    relation_name: str,
) -> SignedCompiledDbtModel:
    try:
        if not set(vars(signed_model)).issubset(type(signed_model).model_fields):
            raise ValueError("signed model contains undeclared fields")
        signing_bytes = compiled_dbt_model_signing_bytes(signed_model.model)
        validated = SignedCompiledDbtModel.model_validate(
            signed_model.model_dump(mode="python"), strict=True
        )
        public_key = trusted_compiler_keys.get(validated.key_id)
        if public_key is None:
            raise InvalidSignature
        signature = base64.b64decode(validated.signature, validate=True)
        public_key.verify(signature, signing_bytes)
    except (AttributeError, binascii.Error, InvalidSignature, TypeError, ValueError):
        raise ProviderError(
            "ClickHouse magnitude observation model authority is invalid",
            "integrity_failure",
        ) from None
    if (
        digest(validated.model) != validated.model_digest
        or validated.model.provider != "clickhouse"
        or validated.model.target_schema != relation_namespace
        or validated.model.model_name != relation_name
    ):
        raise ProviderError(
            "ClickHouse magnitude observation model authority does not match the relation",
            "integrity_failure",
        )
    return validated


def _is_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 63
        and value[0].islower()
        and value.isascii()
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    )


def _is_canonical_nonnegative_integer(value: str) -> bool:
    return value == "0" or (value.isascii() and value.isdecimal() and not value.startswith("0"))


def _parse_column_line(
    line: str, *, expected_namespace: str, expected_relation: str
) -> ProductSqlColumnObservation:
    namespace, relation, name, raw_type, raw_position = line.split("\t")
    if namespace != expected_namespace or relation != expected_relation or int(raw_position) < 1:
        raise ValueError("column identity is invalid")
    if raw_type == "String":
        return ProductSqlColumnObservation(
            name=name,
            logical_type="string",
            physical_type=raw_type,
            nullable=False,
            collation="binary",
            encoding="UTF-8",
        )
    if raw_type == "Decimal(38, 9)":
        return ProductSqlColumnObservation(
            name=name,
            logical_type="decimal",
            physical_type=raw_type,
            nullable=False,
            decimal_precision=38,
            decimal_scale=9,
        )
    raise ValueError("column type is outside the admitted profile")


def _raise_for_status(response: ClickHouseProductSqlObservationResponse) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status in {401, 403}:
        classification: ProviderErrorClassification = "authorization_denied"
    elif status == 409:
        classification = "integrity_failure"
    elif status == 429:
        classification = "throttled"
    elif status >= 500 or response.exception_code == 159:
        classification = "transient_unavailable"
    elif 400 <= status < 500:
        classification = "statement_rejected"
    else:
        classification = "invalid_provider_response"
    raise ProviderError("ClickHouse product SQL observation failed", classification)


_SEMANTICS_QUERY = """SELECT
version(),
toTypeName(toDecimal128(1, 9)),
toTypeName(sum(value)),
toTypeName(sum(value)),
if(
    (SELECT sum(value) FROM (
        SELECT toDecimal128('99999999999999999999999999999.999999999', 9) AS value
        UNION ALL
        SELECT toDecimal128('99999999999999999999999999999.999999999', 9) AS value
    )) < 0,
    'wrap',
    'invalid'
),
if(
    (SELECT sum(value) FROM (
        SELECT CAST(NULL AS Nullable(Decimal(38, 9))) AS value
        UNION ALL
        SELECT toNullable(toDecimal128(1, 9)) AS value
    )) = toDecimal256(1, 9),
    'exclude',
    'invalid'
),
if(
    (SELECT count() FROM (
        SELECT sum(toDecimal128(number, 9)) FROM numbers(0) GROUP BY number
    )) = 0,
    'no_row',
    'invalid'
),
if(hex('é') = 'C3A9' AND 'A' < 'a' AND lengthUTF8('é') = 1, 'binary', 'invalid'),
if(hex('é') = 'C3A9' AND lengthUTF8('é') = 1, 'UTF-8', 'invalid')
FROM (SELECT toDecimal128(number, 9) AS value FROM numbers(1))
FORMAT TabSeparatedRaw"""


__all__ = [
    "ClickHouseDecimalMagnitudeObservation",
    "ClickHouseProductMagnitudeObservation",
    "ClickHouseProductMagnitudeObservationRequest",
    "ClickHouseProductMagnitudeObserver",
    "ClickHouseProductSqlObservationRequest",
    "ClickHouseProductSqlObservationResponse",
    "ClickHouseProductSqlObservationSettings",
    "ClickHouseProductSqlObservationTransport",
    "ClickHouseProductSqlObserver",
    "HttpxClickHouseProductSqlObservationTransport",
]
