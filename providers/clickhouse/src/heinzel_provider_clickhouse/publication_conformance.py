from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol, Self
from urllib.parse import urlparse
from uuid import UUID

import httpx
from heinzel_contract_model import digest
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .settings import CLICKHOUSE_SERVER_VERSION

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_GENERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,59}_g(?P<generation>[1-9][0-9]*)$")
_SEALED_WRITE_PRIVILEGES = ("ALTER", "DROP TABLE", "INSERT")
_EXPECTED_PARTIAL_REVOKES = ("INSERT", "ALTER TABLE", "ALTER VIEW", "DROP TABLE")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClickHousePublicationConformanceSettings(_StrictModel):
    endpoint: str = Field(min_length=1)
    administration_username: str = Field(min_length=1)
    administration_password: SecretStr
    transformation_username: str = Field(min_length=1)
    transformation_password: SecretStr
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
            raise ValueError("ClickHouse publication probe endpoint must be HTTPS or loopback HTTP")
        return value.rstrip("/")


class ClickHousePublicationConformanceRequest(_StrictModel):
    product_database: str = Field(pattern=_IDENTIFIER_PATTERN)
    consumption_database: str = Field(pattern=_IDENTIFIER_PATTERN)
    initial_generation_table: str = Field(pattern=_IDENTIFIER_PATTERN)
    replacement_generation_table: str = Field(pattern=_IDENTIFIER_PATTERN)
    stable_view_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    transformation_role: str = Field(pattern=_IDENTIFIER_PATTERN)

    @model_validator(mode="after")
    def generations_are_ordered_and_distinct(self) -> Self:
        initial = _GENERATION_NAME.fullmatch(self.initial_generation_table)
        replacement = _GENERATION_NAME.fullmatch(self.replacement_generation_table)
        if initial is None or replacement is None:
            raise ValueError("publication probe tables must have generation-numbered names")
        initial_prefix = self.initial_generation_table.rsplit("_g", maxsplit=1)[0]
        replacement_prefix = self.replacement_generation_table.rsplit("_g", maxsplit=1)[0]
        if initial_prefix != replacement_prefix or int(replacement.group("generation")) <= int(
            initial.group("generation")
        ):
            raise ValueError("publication probe replacement must advance the same product")
        return self


class ClickHousePublicationConformanceEvidence(_StrictModel):
    schema_version: Literal["1"] = "1"
    engine_version: Literal["25.8.32.4"]
    initial_generation_uuid: str = Field(min_length=36, max_length=36)
    replacement_generation_uuid: str = Field(min_length=36, max_length=36)
    generation_engine: Literal["MergeTree"]
    initial_create_query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_create_query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_view_target: str = Field(min_length=3, max_length=127)
    sealed_write_privileges: tuple[Literal["ALTER", "DROP TABLE", "INSERT"], ...]
    view_ambiguous_outcome_reconciled: bool
    seal_ambiguous_outcome_reconciled: bool
    observed_at: datetime
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication evidence timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def evidence_is_canonical(self) -> Self:
        if self.initial_generation_uuid == self.replacement_generation_uuid:
            raise ValueError("generation identities must be distinct")
        if self.sealed_write_privileges != _SEALED_WRITE_PRIVILEGES:
            raise ValueError("publication evidence must prove every write privilege sealed")
        expected = digest(self.model_dump(exclude={"evidence_digest"}, mode="python"))
        if self.evidence_digest != expected:
            raise ValueError("publication evidence digest does not match")
        return self


class ClickHousePublicationConformanceResponse(Protocol):
    status_code: int
    exception_code: int | None
    lines: tuple[str, ...]


class ClickHousePublicationConformanceTransport(Protocol):
    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHousePublicationConformanceResponse: ...


class _HttpxResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        raw_exception_code = response.headers.get("X-ClickHouse-Exception-Code")
        try:
            self.exception_code = None if raw_exception_code is None else int(raw_exception_code)
        except ValueError:
            self.exception_code = None
        self.lines = tuple(response.text.splitlines())


class HttpxClickHousePublicationConformanceTransport:
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
    ) -> ClickHousePublicationConformanceResponse:
        return _HttpxResponse(
            self._client.post(
                url,
                headers=headers,
                params=params,
                content=content,
                timeout=timeout_seconds,
            )
        )


class ClickHousePublicationConformanceProbe:
    def __init__(
        self,
        *,
        settings: ClickHousePublicationConformanceSettings,
        transport: ClickHousePublicationConformanceTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = ClickHousePublicationConformanceSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._transport = transport or HttpxClickHousePublicationConformanceTransport(
            verify_tls=settings.verify_tls
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self, request: ClickHousePublicationConformanceRequest
    ) -> ClickHousePublicationConformanceEvidence:
        request = self._validated_request(request)
        self._require_pinned_engine()
        initial_identity = self._generation_identity(
            database=request.product_database,
            table=request.initial_generation_table,
        )
        replacement_identity = self._generation_identity(
            database=request.product_database,
            table=request.replacement_generation_table,
        )
        if initial_identity[0] == replacement_identity[0]:
            raise ProviderError(
                "ClickHouse publication generations share one physical identity",
                "integrity_failure",
            )
        self._require_view_target(request, expected_table=request.initial_generation_table)
        self._require_write_state(request, expected=True)

        seal_reconciled = self._seal_generation(request)
        self._require_write_state(request, expected=False)
        self._require_partial_revokes(request)
        view_reconciled = self._replace_view(request)
        self._require_view_target(request, expected_table=request.replacement_generation_table)

        observed_at = self._clock()
        payload: dict[str, object] = {
            "schema_version": "1",
            "engine_version": CLICKHOUSE_SERVER_VERSION,
            "initial_generation_uuid": initial_identity[0],
            "replacement_generation_uuid": replacement_identity[0],
            "generation_engine": "MergeTree",
            "initial_create_query_digest": digest(initial_identity[2]),
            "replacement_create_query_digest": digest(replacement_identity[2]),
            "observed_view_target": (
                f"{request.product_database}.{request.replacement_generation_table}"
            ),
            "sealed_write_privileges": _SEALED_WRITE_PRIVILEGES,
            "view_ambiguous_outcome_reconciled": view_reconciled,
            "seal_ambiguous_outcome_reconciled": seal_reconciled,
            "observed_at": observed_at,
        }
        try:
            return ClickHousePublicationConformanceEvidence.model_validate(
                {**payload, "evidence_digest": digest(payload)}, strict=True
            )
        except (TypeError, ValueError):
            raise ProviderError(
                "ClickHouse publication conformance evidence is invalid",
                "invalid_provider_response",
            ) from None

    @staticmethod
    def _validated_request(
        request: ClickHousePublicationConformanceRequest,
    ) -> ClickHousePublicationConformanceRequest:
        try:
            if not set(vars(request)).issubset(type(request).model_fields):
                raise ValueError("publication request contains undeclared fields")
            return ClickHousePublicationConformanceRequest.model_validate(
                request.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ProviderError(
                "ClickHouse publication conformance request is invalid",
                "invalid_provider_response",
            ) from None

    def _require_pinned_engine(self) -> None:
        response = self._query("SELECT version() FORMAT TabSeparatedRaw")
        if response.lines != (CLICKHOUSE_SERVER_VERSION,):
            raise ProviderError(
                "ClickHouse publication probe engine version is not pinned",
                "integrity_failure",
            )

    def _generation_identity(self, *, database: str, table: str) -> tuple[str, str, str]:
        response = self._query(
            "SELECT uuid, engine, create_table_query FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String} "
            "FORMAT TabSeparatedRaw",
            parameters={"param_database": database, "param_table": table},
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
        normalized_query = _normalized_statement(create_query)
        expected_identity = f"{database}.{table}"
        try:
            uuid_is_canonical = str(UUID(relation_uuid)) == relation_uuid
        except ValueError:
            uuid_is_canonical = False
        if (
            not uuid_is_canonical
            or engine != "MergeTree"
            or expected_identity not in normalized_query
            or "ENGINE = MergeTree" not in normalized_query
        ):
            raise ProviderError(
                "ClickHouse generation is not an immutable publication candidate",
                "integrity_failure",
            )
        return relation_uuid, engine, normalized_query

    def _replace_view(self, request: ClickHousePublicationConformanceRequest) -> bool:
        statement = (
            f"CREATE OR REPLACE VIEW `{request.consumption_database}`.`{request.stable_view_name}` "
            f"AS SELECT * FROM `{request.product_database}`."
            f"`{request.replacement_generation_table}`"
        )
        try:
            self._effect(statement)
        except _AmbiguousEffect:
            self._require_view_target(request, expected_table=request.replacement_generation_table)
            return True
        return False

    def _seal_generation(self, request: ClickHousePublicationConformanceRequest) -> bool:
        privileges = ", ".join(_SEALED_WRITE_PRIVILEGES)
        statement = (
            f"REVOKE {privileges} ON `{request.product_database}`."
            f"`{request.replacement_generation_table}` FROM `{request.transformation_role}`"
        )
        try:
            self._effect(statement)
        except _AmbiguousEffect:
            self._require_write_state(request, expected=False)
            self._require_partial_revokes(request)
            return True
        return False

    def _require_view_target(
        self,
        request: ClickHousePublicationConformanceRequest,
        *,
        expected_table: str,
    ) -> None:
        response = self._query(
            "SELECT create_table_query FROM system.tables "
            "WHERE database = {database:String} AND name = {table:String} "
            "FORMAT TabSeparatedRaw",
            parameters={
                "param_database": request.consumption_database,
                "param_table": request.stable_view_name,
            },
        )
        if len(response.lines) != 1:
            raise ProviderError(
                "ClickHouse stable view definition is unavailable", "invalid_provider_response"
            )
        normalized = _normalized_statement(response.lines[0])
        expected_prefix = f"CREATE VIEW {request.consumption_database}.{request.stable_view_name} "
        expected_suffix = f" AS SELECT * FROM {request.product_database}.{expected_table}"
        if (
            not normalized.startswith(expected_prefix)
            or not normalized.endswith(expected_suffix)
            or normalized.count(" AS SELECT ") != 1
        ):
            raise ProviderError(
                "ClickHouse stable view does not name the expected generation",
                "integrity_failure",
            )

    def _require_write_state(
        self, request: ClickHousePublicationConformanceRequest, *, expected: bool
    ) -> None:
        target = f"`{request.product_database}`.`{request.replacement_generation_table}`"
        for privilege in _SEALED_WRITE_PRIVILEGES:
            response = self._query(
                f"CHECK GRANT {privilege} ON {target}",
                transformation=True,
            )
            if response.lines != (("1",) if expected else ("0",)):
                raise ProviderError(
                    "ClickHouse generation write seal does not match effective grants",
                    "integrity_failure",
                )
        response = self._query(f"CHECK GRANT SELECT ON {target}", transformation=True)
        if response.lines != ("1",):
            raise ProviderError(
                "ClickHouse generation seal removed required read access",
                "integrity_failure",
            )

    def _require_partial_revokes(self, request: ClickHousePublicationConformanceRequest) -> None:
        role_literal = _string_literal(request.transformation_role)
        response = self._query(
            "SELECT access_type, database, table, is_partial_revoke FROM system.grants "
            f"WHERE role_name = {role_literal} AND database = "
            "{database:String} AND table = {table:String} AND is_partial_revoke = 1 "
            "ORDER BY access_type FORMAT TabSeparatedRaw",
            parameters={
                "param_database": request.product_database,
                "param_table": request.replacement_generation_table,
            },
        )
        expected = tuple(
            f"{privilege}\t{request.product_database}\t{request.replacement_generation_table}\t1"
            for privilege in _EXPECTED_PARTIAL_REVOKES
        )
        if response.lines != expected:
            raise ProviderError(
                "ClickHouse generation write seal is not an exact partial revoke",
                "integrity_failure",
            )

    def _query(
        self,
        statement: str,
        *,
        parameters: Mapping[str, str] | None = None,
        transformation: bool = False,
    ) -> ClickHousePublicationConformanceResponse:
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
        try:
            self._execute(
                statement,
                username=self._settings.administration_username,
                password=self._settings.administration_password,
                parameters={"readonly": "0"},
                ambiguous=True,
            )
        except _AmbiguousEffect:
            raise

    def _execute(
        self,
        statement: str,
        *,
        username: str,
        password: SecretStr,
        parameters: Mapping[str, str],
        ambiguous: bool,
    ) -> ClickHousePublicationConformanceResponse:
        try:
            response = self._transport.execute(
                url=self._settings.endpoint + "/",
                headers=_headers(username, password),
                params=parameters,
                content=statement.encode("utf-8"),
                timeout_seconds=float(self._settings.timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.TransportError, OSError, TimeoutError):
            if ambiguous:
                raise _AmbiguousEffect from None
            raise ProviderError(
                "ClickHouse publication conformance transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "ClickHouse publication conformance transport returned an invalid response",
                "invalid_provider_response",
            ) from None
        _raise_for_status(response)
        return response


class _AmbiguousEffect(Exception):
    pass


def _headers(username: str, password: SecretStr) -> Mapping[str, str]:
    credential = base64.b64encode(f"{username}:{password.get_secret_value()}".encode()).decode(
        "ascii"
    )
    return {
        "Authorization": f"Basic {credential}",
        "Content-Type": "text/plain; charset=utf-8",
    }


def _normalized_statement(statement: str) -> str:
    return " ".join(statement.replace("`", "").split())


def _string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _raise_for_status(response: ClickHousePublicationConformanceResponse) -> None:
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
    raise ProviderError("ClickHouse publication conformance probe failed", classification)


__all__ = [
    "ClickHousePublicationConformanceEvidence",
    "ClickHousePublicationConformanceProbe",
    "ClickHousePublicationConformanceRequest",
    "ClickHousePublicationConformanceResponse",
    "ClickHousePublicationConformanceSettings",
    "ClickHousePublicationConformanceTransport",
    "HttpxClickHousePublicationConformanceTransport",
]
