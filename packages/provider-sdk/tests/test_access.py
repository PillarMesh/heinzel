from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_provider_sdk.access import (
    AccessEffectCommand,
    AccessEffectProviderError,
    AccessEffectResult,
    run_access_provider_conformance,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 12, 21, tzinfo=UTC)


def _command(**changes: object) -> AccessEffectCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "surface": "warehouse",
        "action": "apply",
        "idempotency_key": "access-effect-warehouse-apply-1",
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "warehouse:analytics.revenue",
        "fields": ("region", "revenue"),
        "permissions": ("query", "view"),
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "scope_digest": "a" * 64,
    }
    values.update(changes)
    return AccessEffectCommand.model_validate(values)


def test_access_effect_command_rejects_wrong_surface_and_noncanonical_scope() -> None:
    with pytest.raises(ValidationError):
        _command(surface="catalog")

    with pytest.raises(ValidationError, match="canonical"):
        _command(fields=("revenue", "region"))


def test_access_provider_error_exposes_only_retry_classification() -> None:
    error = AccessEffectProviderError(
        outcome="ambiguous_outcome",
        provider_receipt_digest="b" * 64,
    )

    assert str(error) == "access provider effect failed: ambiguous_outcome"
    assert error.outcome == "ambiguous_outcome"


class _ConformantProvider:
    surface = "warehouse"

    def __init__(self) -> None:
        self._results: dict[str, AccessEffectResult] = {}

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        result = AccessEffectResult(
            surface=command.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest="c" * 64,
        )
        return self._results.setdefault(command.idempotency_key, result)


def test_provider_conformance_requires_exact_apply_and_revoke_replay() -> None:
    run_access_provider_conformance(_ConformantProvider, _command)
