from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator, model_validator

from .models import ProviderModel

type AccessEffectSurface = Literal["result", "superset", "warehouse"]
type AccessEffectAction = Literal["apply", "revoke"]
type AccessEffectFailure = Literal["transient_failure", "permanent_failure", "ambiguous_outcome"]
type AccessPermission = Literal["dashboard", "download", "query", "view"]

_ACCESS_EFFECT_FAILURES = frozenset(("transient_failure", "permanent_failure", "ambiguous_outcome"))


class _AccessModel(ProviderModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class AccessEffectCommand(_AccessModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    grant_id: str = Field(min_length=1)
    grant_revision: int = Field(ge=1)
    surface: AccessEffectSurface
    action: AccessEffectAction
    idempotency_key: str = Field(min_length=1)
    principal_ref: str = Field(min_length=1)
    provider_resource_ref: str = Field(min_length=1)
    fields: tuple[str, ...] = Field(min_length=1)
    permissions: tuple[AccessPermission, ...] = Field(min_length=1)
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
    def time_window_is_positive(self) -> AccessEffectCommand:
        if self.expires_at <= self.effective_at:
            raise ValueError("expires_at must follow effective_at")
        return self


class AccessEffectResult(_AccessModel):
    schema_version: Literal["1"] = "1"
    surface: AccessEffectSurface
    action: AccessEffectAction
    idempotency_key: str = Field(min_length=1)
    provider_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AccessEffectProviderError(RuntimeError):
    outcome: AccessEffectFailure
    provider_receipt_digest: str

    def __init__(self, *, outcome: AccessEffectFailure, provider_receipt_digest: str) -> None:
        if outcome not in _ACCESS_EFFECT_FAILURES:
            raise ValueError("outcome must be an access effect failure")
        if len(provider_receipt_digest) != 64 or any(
            character not in "0123456789abcdef" for character in provider_receipt_digest
        ):
            raise ValueError("provider_receipt_digest must be a lowercase SHA-256 digest")
        super().__init__(f"access provider effect failed: {outcome}")
        self.outcome = outcome
        self.provider_receipt_digest = provider_receipt_digest


@runtime_checkable
class AccessEffectProvider(Protocol):
    @property
    def surface(self) -> AccessEffectSurface: ...

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult: ...


def run_access_provider_conformance(
    provider_factory: Callable[[], AccessEffectProvider],
    command_factory: Callable[..., AccessEffectCommand],
) -> None:
    provider = provider_factory()
    apply_command = command_factory(surface=provider.surface, action="apply")
    applied = provider.enact(apply_command)
    assert provider.enact(apply_command) == applied
    assert (applied.surface, applied.action, applied.idempotency_key) == (
        apply_command.surface,
        apply_command.action,
        apply_command.idempotency_key,
    )

    revoke_command = command_factory(
        surface=provider.surface,
        action="revoke",
        idempotency_key="access-effect-revoke-conformance",
    )
    revoked = provider.enact(revoke_command)
    assert provider.enact(revoke_command) == revoked
    assert (revoked.surface, revoked.action, revoked.idempotency_key) == (
        revoke_command.surface,
        revoke_command.action,
        revoke_command.idempotency_key,
    )
