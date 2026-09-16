from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Literal, Protocol, Self
from urllib.parse import urlparse

import httpx
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProviderError,
    AccessEffectResult,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"


class _AccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClickHouseAccessColumnBinding(_AccessModel):
    field: str = Field(min_length=1)
    column_name: str = Field(pattern=_IDENTIFIER_PATTERN)


class ClickHouseAccessTarget(_AccessModel):
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    provider_resource_ref: str = Field(min_length=1)
    role_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    database: str = Field(pattern=_IDENTIFIER_PATTERN)
    table_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    columns: tuple[ClickHouseAccessColumnBinding, ...] = Field(min_length=1)
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_scoped_role: Literal[True] = True

    @model_validator(mode="after")
    def columns_are_canonical_and_unique(self) -> Self:
        fields = tuple(binding.field for binding in self.columns)
        physical_columns = tuple(binding.column_name for binding in self.columns)
        if fields != tuple(sorted(fields)) or len(fields) != len(set(fields)):
            raise ValueError("access target fields must use unique canonical order")
        if len(physical_columns) != len(set(physical_columns)):
            raise ValueError("access target columns must be unique")
        return self


class ClickHouseAccessSettings(_AccessModel):
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
            raise ValueError("ClickHouse access endpoint must be an HTTPS or loopback HTTP origin")
        return value.rstrip("/")


class ClickHouseAccessTargetAuthority(Protocol):
    def resolve(self, command: AccessEffectCommand) -> ClickHouseAccessTarget | None: ...


class ClickHouseAccessAuthorityUnavailable(RuntimeError):
    pass


class ClickHouseAccessAuthorityInvalid(RuntimeError):
    pass


class ClickHouseAccessResponse(Protocol):
    status_code: int
    exception_code: int | None


class ClickHouseAccessTransport(Protocol):
    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseAccessResponse: ...


class _HttpxClickHouseAccessResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        raw_code = response.headers.get("X-ClickHouse-Exception-Code")
        try:
            self.exception_code = None if raw_code is None else int(raw_code)
        except ValueError:
            self.exception_code = None


class HttpxClickHouseAccessTransport:
    def __init__(self, *, verify_tls: bool = True, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(verify=verify_tls, trust_env=False)

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHouseAccessResponse:
        response = self._client.post(
            url,
            headers=headers,
            content=content,
            timeout=timeout_seconds,
        )
        return _HttpxClickHouseAccessResponse(response)


class ClickHouseAccessEffectProvider:
    surface: Literal["warehouse"] = "warehouse"
    provider_version = "clickhouse-access-v1"

    def __init__(
        self,
        *,
        settings: ClickHouseAccessSettings,
        targets: ClickHouseAccessTargetAuthority,
        transport: ClickHouseAccessTransport | None = None,
    ) -> None:
        self._settings = ClickHouseAccessSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._targets = targets
        self._transport = transport or HttpxClickHouseAccessTransport(
            verify_tls=settings.verify_tls
        )

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        try:
            target = self._targets.resolve(command)
        except (ClickHouseAccessAuthorityUnavailable, OSError, TimeoutError):
            raise self._error(command, "transient_failure") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None
        if target is None or not self._matches(command, target):
            raise self._error(command, "permanent_failure")
        try:
            response = self._transport.execute(
                url=self._settings.endpoint + "/",
                headers=self._headers(),
                content=self._statement(command, target).encode("utf-8"),
                timeout_seconds=float(self._settings.timeout_seconds),
            )
        except httpx.ConnectError:
            raise self._error(command, "transient_failure") from None
        except (httpx.TransportError, OSError, TimeoutError):
            raise self._error(command, "ambiguous_outcome") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None
        if response.status_code < 200 or response.status_code >= 300:
            outcome: AccessEffectFailure = (
                "ambiguous_outcome"
                if response.status_code in {408, 429} or response.status_code >= 500
                else "permanent_failure"
            )
            raise self._error(command, outcome)
        return AccessEffectResult(
            surface=self.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.clickhouse-access-effect.v1",
                    "provider_version": self.provider_version,
                    "command": command,
                    "target": target,
                }
            ),
        )

    def _headers(self) -> Mapping[str, str]:
        token = base64.b64encode(
            f"{self._settings.username}:{self._settings.password.get_secret_value()}".encode()
        ).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "text/plain; charset=utf-8"}

    @staticmethod
    def _matches(command: AccessEffectCommand, target: ClickHouseAccessTarget) -> bool:
        return (
            command.surface == "warehouse"
            and target.tenant_id == command.tenant_id
            and target.grant_id == command.grant_id
            and target.grant_revision == command.grant_revision
            and target.principal_ref == command.principal_ref
            and target.provider_resource_ref == command.provider_resource_ref
            and target.scope_digest == command.scope_digest
            and tuple(binding.field for binding in target.columns) == command.fields
        )

    @staticmethod
    def _statement(command: AccessEffectCommand, target: ClickHouseAccessTarget) -> str:
        columns = ", ".join(f"`{binding.column_name}`" for binding in target.columns)
        verb = "GRANT" if command.action == "apply" else "REVOKE"
        direction = "TO" if command.action == "apply" else "FROM"
        return (
            f"{verb} SELECT({columns}) ON `{target.database}`.`{target.table_name}` "
            f"{direction} `{target.role_name}`"
        )

    @staticmethod
    def _error(
        command: AccessEffectCommand, outcome: AccessEffectFailure
    ) -> AccessEffectProviderError:
        return AccessEffectProviderError(
            outcome=outcome,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.clickhouse-access-effect-error.v1",
                    "command": command,
                    "outcome": outcome,
                }
            ),
        )
