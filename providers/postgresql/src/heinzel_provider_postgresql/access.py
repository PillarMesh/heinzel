from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Literal, Protocol, Self, cast

import psycopg
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProviderError,
    AccessEffectResult,
)
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .startup_denial import (
    AUTHORIZATION_REJECTIONS,
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"


class _AccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PostgreSQLAccessColumnBinding(_AccessModel):
    field: str = Field(min_length=1)
    column_name: str = Field(pattern=_IDENTIFIER_PATTERN)


class PostgreSQLAccessTarget(_AccessModel):
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    provider_resource_ref: str = Field(min_length=1)
    role_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    columns: tuple[PostgreSQLAccessColumnBinding, ...] = Field(min_length=1)
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


class PostgreSQLAccessSettings(_AccessModel):
    administrative_dsn: SecretStr


class PostgreSQLAccessTargetAuthority(Protocol):
    def resolve(self, command: AccessEffectCommand) -> PostgreSQLAccessTarget | None: ...


class PostgreSQLAccessAuthorityUnavailable(RuntimeError):
    pass


class PostgreSQLAccessAuthorityInvalid(RuntimeError):
    pass


class _PostgreSQLAccessConnection(Protocol):
    def execute(self, statement: object) -> object: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _PostgreSQLAccessConnection]


class PostgreSQLAccessEffectProvider:
    surface: Literal["warehouse"] = "warehouse"
    provider_version = "postgresql-access-v1"

    def __init__(
        self,
        *,
        settings: PostgreSQLAccessSettings,
        targets: PostgreSQLAccessTargetAuthority,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings = PostgreSQLAccessSettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._targets = targets
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        try:
            target = self._targets.resolve(command)
        except (PostgreSQLAccessAuthorityUnavailable, OSError, TimeoutError):
            raise self._error(command, "transient_failure") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None
        if target is None or not self._matches(command, target):
            raise self._error(command, "permanent_failure")
        statements = self._statements(command, target)
        connection: _PostgreSQLAccessConnection | None = None
        effects_started = False
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.administrative_dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            for statement in statements:
                connection.execute(statement)
                effects_started = True
            connection.commit()
        except AUTHORIZATION_REJECTIONS:
            # Authorization rejections subclass OperationalError; retrying a credential the server
            # rejected cannot succeed, so it is permanent unless an effect may already have landed.
            self._rollback_and_close(connection)
            rejection: AccessEffectFailure = (
                "ambiguous_outcome" if effects_started else "permanent_failure"
            )
            raise self._error(command, rejection) from None
        except (psycopg.OperationalError, OSError, TimeoutError):
            self._rollback_and_close(connection)
            outcome: AccessEffectFailure = (
                "ambiguous_outcome" if effects_started else "transient_failure"
            )
            raise self._error(command, outcome) from None
        except psycopg.Error:
            self._rollback_and_close(connection)
            raise self._error(command, "permanent_failure") from None
        except Exception:
            self._rollback_and_close(connection)
            raise self._error(command, "permanent_failure") from None
        with suppress(Exception):
            connection.close()
        return AccessEffectResult(
            surface=self.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "heinzel.postgresql-access-effect.v1",
                    "provider_version": self.provider_version,
                    "command": command,
                    "target": target,
                }
            ),
        )

    @staticmethod
    def _matches(command: AccessEffectCommand, target: PostgreSQLAccessTarget) -> bool:
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
    def _statements(
        command: AccessEffectCommand,
        target: PostgreSQLAccessTarget,
    ) -> tuple[sql.Composed, sql.Composed]:
        columns = sql.SQL(", ").join(
            sql.Identifier(binding.column_name) for binding in target.columns
        )
        relation = sql.SQL("{}.{}").format(
            sql.Identifier(target.namespace),
            sql.Identifier(target.relation_name),
        )
        role = sql.Identifier(target.role_name)
        if command.action == "apply":
            return (
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(target.namespace), role
                ),
                sql.SQL("GRANT SELECT ({}) ON TABLE {} TO {}").format(columns, relation, role),
            )
        return (
            sql.SQL("REVOKE SELECT ({}) ON TABLE {} FROM {}").format(columns, relation, role),
            sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                sql.Identifier(target.namespace), role
            ),
        )

    @staticmethod
    def _rollback_and_close(connection: _PostgreSQLAccessConnection | None) -> None:
        if connection is None:
            return
        with suppress(Exception):
            connection.rollback()
        with suppress(Exception):
            connection.close()

    @staticmethod
    def _error(
        command: AccessEffectCommand, outcome: AccessEffectFailure
    ) -> AccessEffectProviderError:
        return AccessEffectProviderError(
            outcome=outcome,
            provider_receipt_digest=digest(
                {
                    "domain": "heinzel.postgresql-access-effect-error.v1",
                    "command": command,
                    "outcome": outcome,
                }
            ),
        )
