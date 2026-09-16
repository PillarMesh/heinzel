from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self

from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProviderError,
    AccessEffectResult,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _AccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class AnswerResultAccessTarget(_AccessModel):
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    principal_ref: str = Field(min_length=1)
    result_ref: str = Field(min_length=1)
    fields: tuple[str, ...] = Field(min_length=1)
    permissions: tuple[Literal["download", "view"], ...] = Field(min_length=1)
    effective_at: datetime
    expires_at: datetime
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("effective_at", "expires_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @field_validator("fields", "permissions")
    @classmethod
    def scope_is_canonical(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError(f"{getattr(info, 'field_name', 'scope')} must be unique")
        if value != tuple(sorted(value)):
            raise ValueError(f"{getattr(info, 'field_name', 'scope')} must be canonical")
        return value

    @model_validator(mode="after")
    def time_window_is_positive(self) -> Self:
        if self.expires_at <= self.effective_at:
            raise ValueError("expires_at must follow effective_at")
        return self


class AnswerResultAccessTargetAuthority(Protocol):
    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget | None: ...


class AnswerResultAccessAuthorityUnavailable(RuntimeError):
    pass


class AnswerResultAccessEffectProvider:
    surface: Literal["result"] = "result"
    provider_version = "answer-result-access-v1"

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        targets: AnswerResultAccessTargetAuthority,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = connection
        self._targets = targets
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS answer_result_access ("
            "tenant_id TEXT NOT NULL, grant_id TEXT NOT NULL, grant_revision INTEGER NOT NULL, "
            "principal_ref TEXT NOT NULL, result_ref TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, grant_id, grant_revision));"
            "CREATE INDEX IF NOT EXISTS answer_result_access_lookup "
            "ON answer_result_access (tenant_id, principal_ref, result_ref);"
            "CREATE TABLE IF NOT EXISTS answer_result_access_effects ("
            "idempotency_key TEXT PRIMARY KEY, command_digest TEXT NOT NULL, result BLOB NOT NULL);"
        )

    @classmethod
    def in_memory(
        cls,
        *,
        targets: AnswerResultAccessTargetAuthority,
        clock: Callable[[], datetime] | None = None,
    ) -> AnswerResultAccessEffectProvider:
        return cls(sqlite3.connect(":memory:"), targets=targets, clock=clock)

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        try:
            target = self._targets.resolve(command)
        except (AnswerResultAccessAuthorityUnavailable, OSError, TimeoutError):
            raise self._error(command, "transient_failure") from None
        except Exception:
            raise self._error(command, "permanent_failure") from None
        if target is None or not self._matches(command, target):
            raise self._error(command, "permanent_failure")

        command_digest = digest(command)
        existing = self._load_effect(command.idempotency_key)
        if existing is not None:
            recorded_digest, result = existing
            if recorded_digest != command_digest:
                raise self._error(command, "permanent_failure")
            return result

        result = AccessEffectResult(
            surface=self.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.answer-result-access-effect.v1",
                    "provider_version": self.provider_version,
                    "command": command,
                    "target": target,
                }
            ),
        )
        effects_started = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if command.action == "apply":
                self._connection.execute(
                    "INSERT INTO answer_result_access "
                    "(tenant_id, grant_id, grant_revision, principal_ref, result_ref, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        target.tenant_id,
                        target.grant_id,
                        target.grant_revision,
                        target.principal_ref,
                        target.result_ref,
                        canonical_bytes(target),
                    ),
                )
            else:
                self._connection.execute(
                    "DELETE FROM answer_result_access WHERE tenant_id = ? AND grant_id = ? "
                    "AND principal_ref = ? AND result_ref = ?",
                    (
                        target.tenant_id,
                        target.grant_id,
                        target.principal_ref,
                        target.result_ref,
                    ),
                )
            effects_started = True
            self._connection.execute(
                "INSERT INTO answer_result_access_effects "
                "(idempotency_key, command_digest, result) VALUES (?, ?, ?)",
                (command.idempotency_key, command_digest, canonical_bytes(result)),
            )
            self._connection.commit()
        except sqlite3.OperationalError:
            with suppress(Exception):
                self._connection.rollback()
            outcome: AccessEffectFailure = (
                "ambiguous_outcome" if effects_started else "transient_failure"
            )
            raise self._error(command, outcome) from None
        except sqlite3.DatabaseError:
            with suppress(Exception):
                self._connection.rollback()
            raise self._error(command, "permanent_failure") from None
        except Exception:
            with suppress(Exception):
                self._connection.rollback()
            raise self._error(command, "permanent_failure") from None
        return result

    def allows(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        result_ref: str,
        permission: Literal["download", "view"],
    ) -> bool:
        rows = self._connection.execute(
            "SELECT payload FROM answer_result_access WHERE tenant_id = ? "
            "AND principal_ref = ? AND result_ref = ?",
            (tenant_id, principal_ref, result_ref),
        ).fetchall()
        now = self._clock()
        return any(
            (target := AnswerResultAccessTarget.model_validate_json(bytes(row[0]), strict=True))
            and target.effective_at <= now < target.expires_at
            and permission in target.permissions
            for row in rows
        )

    def _load_effect(self, idempotency_key: str) -> tuple[str, AccessEffectResult] | None:
        row = self._connection.execute(
            "SELECT command_digest, result FROM answer_result_access_effects "
            "WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), AccessEffectResult.model_validate_json(bytes(row[1]), strict=True)

    @staticmethod
    def _matches(command: AccessEffectCommand, target: AnswerResultAccessTarget) -> bool:
        return (
            command.surface == "result"
            and target.tenant_id == command.tenant_id
            and target.grant_id == command.grant_id
            and target.grant_revision == command.grant_revision
            and target.principal_ref == command.principal_ref
            and target.result_ref == command.provider_resource_ref
            and target.fields == command.fields
            and target.permissions == command.permissions
            and target.effective_at == command.effective_at
            and target.expires_at == command.expires_at
            and target.scope_digest == command.scope_digest
        )

    @staticmethod
    def _error(
        command: AccessEffectCommand, outcome: AccessEffectFailure
    ) -> AccessEffectProviderError:
        return AccessEffectProviderError(
            outcome=outcome,
            provider_receipt_digest=digest(
                {
                    "domain": "pillarmesh.answer-result-access-effect-error.v1",
                    "command": command,
                    "outcome": outcome,
                }
            ),
        )
