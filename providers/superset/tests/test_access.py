from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pillarmesh_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    run_access_provider_conformance,
)
from pillarmesh_provider_superset import (
    CredentialScopedSupersetAccessEffectProvider,
    SupersetAccessTarget,
    SupersetCredentials,
    SupersetHttpResponse,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _command(**updates: object) -> AccessEffectCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "surface": "superset",
        "action": "apply",
        "idempotency_key": "effect-apply-1",
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "dashboard:revenue",
        "fields": ("region", "revenue"),
        "permissions": ("dashboard", "view"),
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return AccessEffectCommand.model_validate(values)


def _target(**updates: object) -> SupersetAccessTarget:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "dashboard:revenue",
        "dashboard_id": 31,
        "grant_scoped_role_id": 17,
        "base_role_ids": (2, 5),
        "credential_secret_ref": "secret://tenant-a/superset",
        "fields": ("region", "revenue"),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return SupersetAccessTarget.model_validate(values)


class _Authority:
    def __init__(self, target: SupersetAccessTarget | None = None) -> None:
        self.target = _target() if target is None else target

    def resolve(self, command: AccessEffectCommand) -> SupersetAccessTarget | None:
        return self.target


class _Credentials:
    def resolve(self, *, secret_reference: str) -> SupersetCredentials:
        assert secret_reference == "secret://tenant-a/superset"
        return SupersetCredentials(
            base_url="https://superset.test",
            username="access-admin",
            password="private-password",
            database_uri="postgresql://unused",
        )


class _Transport:
    def __init__(self, *, roles: tuple[int, ...] = (2, 5)) -> None:
        self.roles = roles
        self.calls: list[dict[str, object]] = []
        self.fail_dashboard_request: Exception | None = None
        self.fail_role_update: Exception | None = None
        self.dashboard_status = 200
        self.ignore_role_update = False

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: object | None = None,
        params: Mapping[str, str] | None = None,
    ) -> SupersetHttpResponse:
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json, "params": params}
        )
        if url.endswith("/api/v1/security/login"):
            return SupersetHttpResponse(200, {"access_token": "session-token"})
        if url.endswith("/api/v1/security/csrf_token/"):
            return SupersetHttpResponse(200, {"result": "csrf-token"})
        if self.fail_dashboard_request is not None:
            raise self.fail_dashboard_request
        if method == "GET":
            return SupersetHttpResponse(
                self.dashboard_status, {"result": {"roles": list(self.roles)}}
            )
        if self.fail_role_update is not None:
            raise self.fail_role_update
        if self.dashboard_status >= 400:
            return SupersetHttpResponse(self.dashboard_status, None)
        assert isinstance(json, dict)
        roles = json.get("roles")
        assert isinstance(roles, list)
        if not self.ignore_role_update:
            self.roles = tuple(roles)
        return SupersetHttpResponse(200, {"id": 31})


def _provider(
    transport: _Transport,
    *,
    target: SupersetAccessTarget | None = None,
) -> CredentialScopedSupersetAccessEffectProvider:
    return CredentialScopedSupersetAccessEffectProvider(
        targets=_Authority(target),
        credentials=_Credentials(),
        transport=transport,
    )


def test_apply_adds_only_the_authority_bound_grant_role() -> None:
    transport = _Transport()

    result = _provider(transport).enact(_command())

    assert transport.roles == (2, 5, 17)
    assert next(call["json"] for call in transport.calls if call["method"] == "PUT") == {
        "roles": [2, 5, 17]
    }
    assert result.surface == "superset"
    assert result.action == "apply"
    dashboard_reads = [
        call
        for call in transport.calls
        if call["method"] == "GET" and str(call["url"]).endswith("/api/v1/dashboard/31")
    ]
    assert len(dashboard_reads) == 2


def test_apply_refuses_receipt_when_superset_does_not_persist_roles() -> None:
    transport = _Transport()
    transport.ignore_role_update = True

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(transport).enact(_command())

    assert captured.value.outcome == "ambiguous_outcome"


def test_revoke_removes_only_the_authority_bound_grant_role() -> None:
    transport = _Transport(roles=(2, 5, 17))

    result = _provider(transport).enact(
        _command(action="revoke", idempotency_key="effect-revoke-1")
    )

    assert transport.roles == (2, 5)
    assert next(call["json"] for call in transport.calls if call["method"] == "PUT") == {
        "roles": [2, 5]
    }
    assert result.action == "revoke"


def test_target_mismatch_is_denied_before_credentials_or_transport() -> None:
    transport = _Transport()

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(transport, target=_target(scope_digest="b" * 64)).enact(_command())

    assert captured.value.outcome == "permanent_failure"
    assert transport.calls == []


def test_unrecognized_role_drift_is_denied_without_overwriting_it() -> None:
    transport = _Transport(roles=(2, 5, 23))

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(transport).enact(_command())

    assert captured.value.outcome == "permanent_failure"
    assert transport.roles == (2, 5, 23)
    assert all(call["method"] != "PUT" for call in transport.calls)


def test_timeout_after_role_update_is_ambiguous_and_sanitized() -> None:
    transport = _Transport()
    transport.fail_role_update = httpx.ReadTimeout("private-password")

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(transport).enact(_command())

    assert captured.value.outcome == "ambiguous_outcome"
    assert "private-password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_read_failure_classification_preserves_retry_safety() -> None:
    unavailable = _Transport()
    unavailable.fail_dashboard_request = httpx.ConnectError("private-password")
    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(unavailable).enact(_command())
    assert captured.value.outcome == "transient_failure"

    denied = _Transport()
    denied.dashboard_status = 403
    with pytest.raises(AccessEffectProviderError) as rejected:
        _provider(denied).enact(_command())
    assert rejected.value.outcome == "permanent_failure"


def test_target_rejects_noncanonical_or_overlapping_roles() -> None:
    with pytest.raises(ValidationError):
        _target(base_role_ids=(5, 2))
    with pytest.raises(ValidationError):
        _target(base_role_ids=(2, 17))
    with pytest.raises(ValidationError):
        _target(permissions=("query", "view"))


def test_superset_access_provider_conforms_to_replay_contract() -> None:
    transport = _Transport()

    run_access_provider_conformance(
        lambda: _provider(transport),
        lambda **updates: _command(**updates),
    )
