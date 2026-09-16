from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pillarmesh_provider_clickhouse import (
    ClickHouseAccessColumnBinding,
    ClickHouseAccessEffectProvider,
    ClickHouseAccessSettings,
    ClickHouseAccessTarget,
)
from pillarmesh_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    run_access_provider_conformance,
)
from pydantic import SecretStr, ValidationError

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _command(**updates: object) -> AccessEffectCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "surface": "warehouse",
        "action": "apply",
        "idempotency_key": "effect-apply-1",
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "product:revenue:v1",
        "fields": ("region", "revenue"),
        "permissions": ("query", "view"),
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return AccessEffectCommand.model_validate(values)


def _target(**updates: object) -> ClickHouseAccessTarget:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "product:revenue:v1",
        "role_name": "pm_grant_1",
        "database": "consumption",
        "table_name": "revenue_v1",
        "columns": (
            ClickHouseAccessColumnBinding(field="region", column_name="region"),
            ClickHouseAccessColumnBinding(field="revenue", column_name="net_revenue"),
        ),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return ClickHouseAccessTarget.model_validate(values)


class _Authority:
    def __init__(self, target: ClickHouseAccessTarget | None = None) -> None:
        self.target = _target() if target is None else target

    def resolve(self, command: AccessEffectCommand) -> ClickHouseAccessTarget | None:
        return self.target


class _Response:
    def __init__(self, status_code: int = 200, exception_code: int | None = None) -> None:
        self.status_code = status_code
        self.exception_code = exception_code


class _Transport:
    def __init__(self, response: _Response | Exception | None = None) -> None:
        self.response = response or _Response()
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> _Response:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "content": content,
                "timeout_seconds": timeout_seconds,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _provider(transport: _Transport) -> ClickHouseAccessEffectProvider:
    return ClickHouseAccessEffectProvider(
        settings=ClickHouseAccessSettings(
            endpoint="https://clickhouse.test",
            username="access_admin",
            password=SecretStr("private-password"),
        ),
        targets=_Authority(),
        transport=transport,
    )


def test_apply_grants_only_authority_mapped_columns() -> None:
    transport = _Transport()

    result = _provider(transport).enact(_command())

    assert transport.calls[0]["content"] == (
        b"GRANT SELECT(`region`, `net_revenue`) ON `consumption`.`revenue_v1` TO `pm_grant_1`"
    )
    assert result.surface == "warehouse"
    assert result.action == "apply"


def test_revoke_uses_the_same_exact_grant_scope() -> None:
    transport = _Transport()

    result = _provider(transport).enact(
        _command(action="revoke", idempotency_key="effect-revoke-1")
    )

    assert transport.calls[0]["content"] == (
        b"REVOKE SELECT(`region`, `net_revenue`) ON `consumption`.`revenue_v1` FROM `pm_grant_1`"
    )
    assert result.action == "revoke"


def test_scope_mismatch_is_permanent_before_transport() -> None:
    transport = _Transport()
    provider = ClickHouseAccessEffectProvider(
        settings=ClickHouseAccessSettings(
            endpoint="https://clickhouse.test",
            username="access_admin",
            password=SecretStr("private-password"),
        ),
        targets=_Authority(_target(scope_digest="b" * 64)),
        transport=transport,
    )

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command())

    assert captured.value.outcome == "permanent_failure"
    assert transport.calls == []


def test_timeout_after_dispatch_is_ambiguous_and_sanitized() -> None:
    transport = _Transport(httpx.ReadTimeout("private-password"))

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(transport).enact(_command())

    assert captured.value.outcome == "ambiguous_outcome"
    assert "private-password" not in str(captured.value)


def test_provider_rejection_is_permanent_and_server_failure_is_ambiguous() -> None:
    with pytest.raises(AccessEffectProviderError) as denied:
        _provider(_Transport(_Response(status_code=403))).enact(_command())
    assert denied.value.outcome == "permanent_failure"

    with pytest.raises(AccessEffectProviderError) as uncertain:
        _provider(_Transport(_Response(status_code=503))).enact(_command())
    assert uncertain.value.outcome == "ambiguous_outcome"


def test_target_and_endpoint_reject_injection_or_insecure_remote_transport() -> None:
    with pytest.raises(ValidationError):
        _target(role_name="reader`; DROP ROLE admin; --")
    with pytest.raises(ValidationError):
        ClickHouseAccessSettings(
            endpoint="http://clickhouse.example.com",
            username="access_admin",
            password=SecretStr("private-password"),
        )


def test_clickhouse_access_provider_conforms_to_replay_contract() -> None:
    transport = _Transport()

    run_access_provider_conformance(
        lambda: _provider(transport),
        lambda **updates: _command(**updates),
    )
