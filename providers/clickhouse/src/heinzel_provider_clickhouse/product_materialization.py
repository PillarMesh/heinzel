from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from heinzel_contract_model import ArtifactModel, digest
from heinzel_dbt_adapter import (
    DbtFailureClassification,
    DbtInvocationAuthority,
    DbtInvocationError,
    DbtInvocationReceipt,
    DbtInvoker,
    SignedCompiledDbtModel,
)
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_runtime import MaterializationObservation, MaterializationRequest
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .product_sql_observation import (
    ClickHouseProductMagnitudeObservation,
    ClickHouseProductMagnitudeObservationRequest,
    ClickHouseProductMagnitudeObserver,
    ClickHouseProductSqlObservationSettings,
)

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_GENERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}_g(?P<generation>[1-9][0-9]*)$")
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SEALED_WRITE_PRIVILEGES = ("ALTER", "DROP TABLE", "INSERT")
_EXPECTED_PARTIAL_REVOKES = ("INSERT", "ALTER TABLE", "ALTER VIEW", "DROP TABLE")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClickHouseMaterializationSettings(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    endpoint: str = Field(min_length=1)
    administration_username: str = Field(min_length=1)
    administration_password: SecretStr
    transformation_username: str = Field(min_length=1)
    transformation_password: SecretStr
    transformation_role: str = Field(pattern=_IDENTIFIER_PATTERN)
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
            raise ValueError("ClickHouse materialization endpoint must be HTTPS or loopback HTTP")
        return value.rstrip("/")


class ClickHouseMaterializedColumn(ArtifactModel):
    ordinal: int = Field(ge=1)
    name: str = Field(pattern=_IDENTIFIER_PATTERN)
    data_type: str = Field(min_length=1)


def clickhouse_materialized_schema_digest(
    columns: tuple[ClickHouseMaterializedColumn, ...],
) -> str:
    return digest(columns)


class ClickHouseMaterializationCommit(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_revision: int = Field(ge=1)
    product_generation: int = Field(ge=1)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_uuid: str = Field(min_length=36, max_length=36)
    normalized_create_query: str = Field(min_length=1)
    columns: tuple[ClickHouseMaterializedColumn, ...] = Field(min_length=1)
    row_count: int = Field(ge=0)
    dbt_receipt: DbtInvocationReceipt
    magnitude_observation: ClickHouseProductMagnitudeObservation
    sealed_write_privileges: tuple[Literal["ALTER", "DROP TABLE", "INSERT"], ...]
    exact_partial_revokes: tuple[Literal["INSERT", "ALTER TABLE", "ALTER VIEW", "DROP TABLE"], ...]
    seal_ambiguous_outcome_reconciled: bool
    committed_at: datetime
    commit_reference: str = Field(pattern=_DIGEST_PATTERN)

    @field_validator("committed_at")
    @classmethod
    def committed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ClickHouse materialization commit timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authority_is_canonical(self) -> ClickHouseMaterializationCommit:
        if self.sealed_write_privileges != _SEALED_WRITE_PRIVILEGES:
            raise ValueError("ClickHouse materialization commit must bind every sealed write")
        if self.exact_partial_revokes != _EXPECTED_PARTIAL_REVOKES:
            raise ValueError("ClickHouse materialization commit must bind exact partial revokes")
        expected = digest(self.model_dump(exclude={"commit_reference"}, mode="python"))
        if self.commit_reference != expected:
            raise ValueError("ClickHouse materialization commit reference does not match")
        return self

    def runtime_observation(self) -> MaterializationObservation:
        return MaterializationObservation(
            provider_commit_reference=self.commit_reference,
            output_schema_digest=clickhouse_materialized_schema_digest(self.columns),
            output_row_count=self.row_count,
            dbt_manifest_digest=self.dbt_receipt.manifest_digest,
            dbt_run_results_digest=self.dbt_receipt.run_results_digest,
            lineage_digest=self.dbt_receipt.lineage_digest,
            quality_assertion_count=self.dbt_receipt.quality_assertion_count,
            quality_disposition=self.dbt_receipt.quality_disposition,
        )


class ClickHouseMaterializationResponse(Protocol):
    status_code: int
    exception_code: int | None
    lines: tuple[str, ...]


class ClickHouseMaterializationTransport(Protocol):
    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseMaterializationResponse: ...


class _HttpxResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        raw_code = response.headers.get("X-ClickHouse-Exception-Code")
        try:
            self.exception_code = None if raw_code is None else int(raw_code)
        except ValueError:
            self.exception_code = None
        self.lines = tuple(response.text.splitlines())


class HttpxClickHouseMaterializationTransport:
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
    ) -> ClickHouseMaterializationResponse:
        return _HttpxResponse(
            self._client.post(
                url,
                headers=headers,
                params=params,
                content=content,
                timeout=timeout_seconds,
            )
        )


class ClickHouseMaterializationWarehouse:
    """Materialize and seal one signed generation without inventing publication atomicity."""

    def __init__(
        self,
        *,
        settings: ClickHouseMaterializationSettings,
        observation_settings: ClickHouseProductSqlObservationSettings,
        signed_model: SignedCompiledDbtModel,
        trusted_compiler_keys: Mapping[str, Ed25519PublicKey],
        invoker: DbtInvoker,
        transport: ClickHouseMaterializationTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = ClickHouseMaterializationSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._observation_settings = ClickHouseProductSqlObservationSettings.model_validate(
            observation_settings.model_dump(mode="python"), strict=True
        )
        self._signed_model = signed_model
        self._trusted_compiler_keys = dict(trusted_compiler_keys)
        self._invoker = invoker
        self._transport = transport or HttpxClickHouseMaterializationTransport(
            verify_tls=settings.verify_tls
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(self, request: MaterializationRequest) -> MaterializationObservation:
        return self.materialize(request).runtime_observation()

    def materialize(self, request: MaterializationRequest) -> ClickHouseMaterializationCommit:
        self._require_authority(request)
        self._require_fresh_target()
        receipt = self._invoke(request)
        relation_uuid, normalized_create_query = self._observe_identity()
        columns = self._observe_columns()
        schema_digest = clickhouse_materialized_schema_digest(columns)
        if schema_digest != request.expected_output_schema_digest:
            raise ProviderError(
                "ClickHouse materialized schema does not match compiler authority",
                "integrity_failure",
            )
        row_count = self._observe_row_count()
        magnitude = ClickHouseProductMagnitudeObserver(
            settings=self._observation_settings,
            signed_model=self._signed_model,
            trusted_compiler_keys=self._trusted_compiler_keys,
            transport=self._transport,
            clock=self._clock,
        ).observe(
            ClickHouseProductMagnitudeObservationRequest(
                tenant_id=self._settings.tenant_id,
                warehouse_binding_id=self._settings.warehouse_binding_id,
                warehouse_binding_revision=self._settings.warehouse_binding_revision,
                relation_namespace=self._signed_model.model.target_schema,
                relation_name=self._signed_model.model.model_name,
            )
        )
        seal_reconciled = self._seal_generation()
        self._require_write_state(expected=False)
        self._require_partial_revokes()
        payload: dict[str, object] = {
            "schema_version": "1",
            "tenant_id": request.tenant_id,
            "product_id": request.product_id,
            "product_revision": request.product_revision,
            "product_generation": request.product_generation,
            "relation_namespace": self._signed_model.model.target_schema,
            "relation_name": self._signed_model.model.model_name,
            "relation_uuid": relation_uuid,
            "normalized_create_query": normalized_create_query,
            "columns": columns,
            "row_count": row_count,
            "dbt_receipt": receipt,
            "magnitude_observation": magnitude,
            "sealed_write_privileges": _SEALED_WRITE_PRIVILEGES,
            "exact_partial_revokes": _EXPECTED_PARTIAL_REVOKES,
            "seal_ambiguous_outcome_reconciled": seal_reconciled,
            "committed_at": self._clock(),
        }
        try:
            return ClickHouseMaterializationCommit.model_validate(
                {**payload, "commit_reference": digest(payload)}, strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse returned an invalid materialization commit", "invalid_provider_response"
            ) from None

    def switch_consumption_view(
        self,
        request: MaterializationRequest,
        observation: MaterializationObservation,
    ) -> None:
        raise ProviderError(
            "ClickHouse publication requires the exact provider commit receipt",
            "permanent_configuration",
        )

    def _require_authority(self, request: MaterializationRequest) -> None:
        model = self._signed_model.model
        physical_plan = request.physical_plan
        match = _GENERATION_NAME.fullmatch(model.model_name)
        if (
            request.tenant_id != self._settings.tenant_id
            or request.compiled_model_digest != self._signed_model.model_digest
            or model.contract_digest != request.contract_digest
            or model.provider != "clickhouse"
            or model.input_generation_digests != request.input_generation_digests
            or match is None
            or int(match.group("generation")) != request.product_generation
            or physical_plan.tenant_id != request.tenant_id
            or physical_plan.product_id != request.product_id
            or physical_plan.product_revision != request.product_revision
            or physical_plan.contract_digest != request.contract_digest
            or physical_plan.provider != "clickhouse"
            or physical_plan.warehouse_binding_id != self._settings.warehouse_binding_id
            or physical_plan.warehouse_binding_revision != self._settings.warehouse_binding_revision
            or physical_plan.target.namespace != model.target_schema
            or physical_plan.target.relation_name != model.model_name
            or physical_plan.expected_output_schema_digest != request.expected_output_schema_digest
            or self._observation_settings.tenant_id != self._settings.tenant_id
            or self._observation_settings.warehouse_binding_id
            != self._settings.warehouse_binding_id
            or self._observation_settings.warehouse_binding_revision
            != self._settings.warehouse_binding_revision
            or self._observation_settings.endpoint != self._settings.endpoint
        ):
            raise ProviderError(
                "ClickHouse materialization authority does not match the request",
                "authorization_denied",
            )

    def _require_fresh_target(self) -> None:
        response = self._query(
            "SELECT count() FROM system.tables WHERE database = {database:String} "
            "AND name = {table:String} FORMAT TabSeparatedRaw",
            parameters=self._relation_parameters(),
        )
        if response.lines != ("0",):
            raise ProviderError(
                "ClickHouse materialization requires a fresh generation target",
                "integrity_failure",
            )

    def _invoke(self, request: MaterializationRequest) -> DbtInvocationReceipt:
        try:
            receipt = self._invoker.invoke(
                signed_model=self._signed_model,
                authority=DbtInvocationAuthority(
                    contract_digest=request.contract_digest,
                    provider="clickhouse",
                    input_generation_digests=request.input_generation_digests,
                ),
            )
        except DbtInvocationError as error:
            classification: ProviderErrorClassification
            match error.classification:
                case DbtFailureClassification.INVALID_SIGNATURE:
                    classification = "integrity_failure"
                case DbtFailureClassification.AUTHORITY_MISMATCH:
                    classification = "authorization_denied"
                case DbtFailureClassification.MALFORMED_OUTPUT:
                    classification = "invalid_provider_response"
                case (
                    DbtFailureClassification.INVOCATION_FAILED
                    | DbtFailureClassification.NONZERO_EXIT
                ):
                    classification = "ambiguous_outcome"
            raise ProviderError(
                "ClickHouse materialization did not produce verified dbt evidence",
                classification,
            ) from None
        expected = DbtInvocationReceipt(
            model_digest=self._signed_model.model_digest,
            contract_digest=request.contract_digest,
            provider="clickhouse",
            input_generation_digests=request.input_generation_digests,
            dbt_version=receipt.dbt_version,
            manifest_digest=receipt.manifest_digest,
            run_results_digest=receipt.run_results_digest,
            lineage_digest=receipt.lineage_digest,
            quality_assertion_count=receipt.quality_assertion_count,
            quality_disposition=receipt.quality_disposition,
        )
        if receipt != expected:
            raise ProviderError("ClickHouse dbt receipt authority is invalid", "integrity_failure")
        return receipt

    def _observe_identity(self) -> tuple[str, str]:
        response = self._query(
            "SELECT uuid, engine, create_table_query FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String} "
            "FORMAT TabSeparatedRaw",
            parameters=self._relation_parameters(),
        )
        if len(response.lines) != 1:
            raise ProviderError(
                "ClickHouse generation identity is unavailable", "invalid_provider_response"
            )
        values = response.lines[0].split("\t", maxsplit=2)
        if len(values) != 3:
            raise ProviderError(
                "ClickHouse generation identity is invalid", "invalid_provider_response"
            )
        relation_uuid, engine, create_query = values
        normalized = _normalized_statement(create_query)
        expected_name = (
            f"{self._signed_model.model.target_schema}.{self._signed_model.model.model_name}"
        )
        try:
            canonical_uuid = str(UUID(relation_uuid)) == relation_uuid
        except ValueError:
            canonical_uuid = False
        if (
            not canonical_uuid
            or engine != "MergeTree"
            or expected_name not in normalized
            or "ENGINE = MergeTree" not in normalized
        ):
            raise ProviderError(
                "ClickHouse dbt output is not the authorized MergeTree generation",
                "integrity_failure",
            )
        return relation_uuid, normalized

    def _observe_columns(self) -> tuple[ClickHouseMaterializedColumn, ...]:
        response = self._query(
            "SELECT position, name, type FROM system.columns "
            "WHERE database = {database:String} AND table = {table:String} "
            "ORDER BY position FORMAT TabSeparatedRaw",
            parameters=self._relation_parameters(),
        )
        try:
            columns = tuple(_column(line) for line in response.lines)
        except ValueError:
            raise ProviderError(
                "ClickHouse materialized schema is invalid", "invalid_provider_response"
            ) from None
        if not columns or tuple(column.ordinal for column in columns) != tuple(
            range(1, len(columns) + 1)
        ):
            raise ProviderError(
                "ClickHouse materialized schema is invalid", "invalid_provider_response"
            )
        return columns

    def _observe_row_count(self) -> int:
        response = self._query(
            f"SELECT count() FROM `{self._signed_model.model.target_schema}`."
            f"`{self._signed_model.model.model_name}` FORMAT TabSeparatedRaw"
        )
        if len(response.lines) != 1 or not _is_canonical_nonnegative_integer(response.lines[0]):
            raise ProviderError("ClickHouse row count is invalid", "invalid_provider_response")
        return int(response.lines[0])

    def _seal_generation(self) -> bool:
        target = self._quoted_target()
        privileges = ", ".join(_SEALED_WRITE_PRIVILEGES)
        try:
            self._effect(
                f"REVOKE {privileges} ON {target} FROM `{self._settings.transformation_role}`"
            )
        except _AmbiguousEffect:
            self._require_write_state(expected=False)
            self._require_partial_revokes()
            return True
        return False

    def _require_write_state(self, *, expected: bool) -> None:
        for privilege in _SEALED_WRITE_PRIVILEGES:
            response = self._query(
                f"CHECK GRANT {privilege} ON {self._quoted_target()}", transformation=True
            )
            if response.lines != (("1",) if expected else ("0",)):
                raise ProviderError(
                    "ClickHouse materialization write seal is not effective", "integrity_failure"
                )
        if self._query(
            f"CHECK GRANT SELECT ON {self._quoted_target()}", transformation=True
        ).lines != ("1",):
            raise ProviderError(
                "ClickHouse materialization seal removed required reads", "integrity_failure"
            )

    def _require_partial_revokes(self) -> None:
        response = self._query(
            "SELECT access_type, database, table, is_partial_revoke FROM system.grants "
            f"WHERE role_name = {_string_literal(self._settings.transformation_role)} "
            "AND database = {database:String} AND table = {table:String} "
            "AND is_partial_revoke = 1 ORDER BY access_type FORMAT TabSeparatedRaw",
            parameters=self._relation_parameters(),
        )
        expected = tuple(
            f"{privilege}\t{self._signed_model.model.target_schema}\t"
            f"{self._signed_model.model.model_name}\t1"
            for privilege in _EXPECTED_PARTIAL_REVOKES
        )
        if response.lines != expected:
            raise ProviderError(
                "ClickHouse materialization seal is not an exact partial revoke",
                "integrity_failure",
            )

    def _relation_parameters(self) -> Mapping[str, str]:
        return {
            "param_database": self._signed_model.model.target_schema,
            "param_table": self._signed_model.model.model_name,
        }

    def _quoted_target(self) -> str:
        return f"`{self._signed_model.model.target_schema}`.`{self._signed_model.model.model_name}`"

    def _query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str] | None = None,
        transformation: bool = False,
    ) -> ClickHouseMaterializationResponse:
        username = (
            self._settings.transformation_username
            if transformation
            else self._settings.administration_username
        )
        password = (
            self._settings.transformation_password
            if transformation
            else self._settings.administration_password
        )
        return self._execute(
            statement,
            username=username,
            password=password,
            parameters={"readonly": "0" if transformation else "2", **(parameters or {})},
            ambiguous=False,
        )

    def _effect(self, statement: str) -> None:
        self._execute(
            statement,
            username=self._settings.administration_username,
            password=self._settings.administration_password,
            parameters={"readonly": "0"},
            ambiguous=True,
        )

    def _execute(
        self,
        statement: str,
        *,
        username: str,
        password: SecretStr,
        parameters: Mapping[str, str],
        ambiguous: bool,
    ) -> ClickHouseMaterializationResponse:
        try:
            response = self._transport.execute(
                url=self._settings.endpoint + "/",
                headers=_headers(username, password),
                params=parameters,
                content=statement.encode(),
                timeout_seconds=float(self._settings.timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.TransportError, OSError, TimeoutError):
            if ambiguous:
                raise _AmbiguousEffect from None
            raise ProviderError(
                "ClickHouse materialization transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse materialization transport returned an invalid response",
                "invalid_provider_response",
            ) from None
        _raise_for_status(response)
        return response


class _AmbiguousEffect(Exception):
    pass


def _column(line: str) -> ClickHouseMaterializedColumn:
    values = line.split("\t")
    if len(values) != 3 or not values[0].isdigit() or values[0].startswith("0"):
        raise ValueError("invalid ClickHouse column")
    return ClickHouseMaterializedColumn(ordinal=int(values[0]), name=values[1], data_type=values[2])


def _normalized_statement(statement: str) -> str:
    return " ".join(statement.replace("`", "").split())


def _is_canonical_nonnegative_integer(value: str) -> bool:
    return value == "0" or (value.isdigit() and not value.startswith("0"))


def _string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _headers(username: str, password: SecretStr) -> Mapping[str, str]:
    credential = base64.b64encode(f"{username}:{password.get_secret_value()}".encode()).decode(
        "ascii"
    )
    return {
        "Authorization": f"Basic {credential}",
        "Content-Type": "text/plain; charset=utf-8",
    }


def _raise_for_status(response: ClickHouseMaterializationResponse) -> None:
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
    raise ProviderError("ClickHouse materialization query failed", classification)


__all__ = [
    "ClickHouseMaterializationCommit",
    "ClickHouseMaterializationResponse",
    "ClickHouseMaterializationSettings",
    "ClickHouseMaterializationTransport",
    "ClickHouseMaterializationWarehouse",
    "ClickHouseMaterializedColumn",
    "HttpxClickHouseMaterializationTransport",
    "clickhouse_materialized_schema_digest",
]
