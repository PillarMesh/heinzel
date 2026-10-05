from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from heinzel_provider_postgresql import (
    PostgreSQLAccessColumnBinding,
    PostgreSQLAccessEffectProvider,
    PostgreSQLAccessSettings,
    PostgreSQLAccessTarget,
)
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    run_access_provider_conformance,
)
from psycopg import sql
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


def _target(**updates: object) -> PostgreSQLAccessTarget:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "product:revenue:v1",
        "role_name": "pm_grant_1",
        "namespace": "consumption",
        "relation_name": "revenue_v1",
        "columns": (
            PostgreSQLAccessColumnBinding(field="region", column_name="region"),
            PostgreSQLAccessColumnBinding(field="revenue", column_name="net_revenue"),
        ),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return PostgreSQLAccessTarget.model_validate(values)


class _Authority:
    def __init__(self, target: PostgreSQLAccessTarget | None = None) -> None:
        self.target = _target() if target is None else target

    def resolve(self, command: AccessEffectCommand) -> PostgreSQLAccessTarget | None:
        return self.target


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.commit_failure: Exception | None = None
        self.execute_failure: Exception | None = None

    def execute(self, statement: object) -> None:
        if self.execute_failure is not None:
            raise self.execute_failure
        # _PostgreSQLAccessConnection.execute takes `object`, so the double receives
        # one; every caller composes SQL, and recording it means saying so.
        assert isinstance(statement, sql.Composable), statement
        self.statements.append(statement.as_string())

    def commit(self) -> None:
        if self.commit_failure is not None:
            raise self.commit_failure
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _provider(connection: _Connection) -> PostgreSQLAccessEffectProvider:
    return PostgreSQLAccessEffectProvider(
        settings=PostgreSQLAccessSettings(
            administrative_dsn=SecretStr("postgresql://private-password")
        ),
        targets=_Authority(),
        connect=lambda _dsn: connection,
    )


def _unavailable_connect(_dsn: str) -> _Connection:
    raise OSError("private-password")


def test_apply_grants_only_bound_columns_to_the_authoritative_role() -> None:
    connection = _Connection()

    result = _provider(connection).enact(_command())

    assert connection.statements == [
        'GRANT USAGE ON SCHEMA "consumption" TO "pm_grant_1"',
        'GRANT SELECT ("region", "net_revenue") ON TABLE '
        '"consumption"."revenue_v1" TO "pm_grant_1"',
    ]
    assert connection.committed is True
    assert connection.closed is True
    assert result.surface == "warehouse"
    assert result.action == "apply"


def test_revoke_removes_column_select_before_schema_usage() -> None:
    connection = _Connection()

    result = _provider(connection).enact(
        _command(action="revoke", idempotency_key="effect-revoke-1")
    )

    assert connection.statements == [
        'REVOKE SELECT ("region", "net_revenue") ON TABLE '
        '"consumption"."revenue_v1" FROM "pm_grant_1"',
        'REVOKE USAGE ON SCHEMA "consumption" FROM "pm_grant_1"',
    ]
    assert result.action == "revoke"


def test_target_must_match_every_command_authority_field() -> None:
    connection = _Connection()
    provider = PostgreSQLAccessEffectProvider(
        settings=PostgreSQLAccessSettings(
            administrative_dsn=SecretStr("postgresql://private-password")
        ),
        targets=_Authority(_target(scope_digest="b" * 64)),
        connect=lambda _dsn: connection,
    )

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command())

    assert captured.value.outcome == "permanent_failure"
    assert connection.statements == []


def test_ambiguous_commit_is_classified_without_leaking_driver_detail() -> None:
    connection = _Connection()
    connection.commit_failure = psycopg.OperationalError("private-password")

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(connection).enact(_command())

    assert captured.value.outcome == "ambiguous_outcome"
    assert "private-password" not in str(captured.value)
    assert connection.rolled_back is True
    assert connection.closed is True


def test_statement_rejection_is_permanent_and_transport_failure_is_transient() -> None:
    rejected = _Connection()
    rejected.execute_failure = psycopg.errors.InsufficientPrivilege("private-password")

    with pytest.raises(AccessEffectProviderError) as captured:
        _provider(rejected).enact(_command())

    assert captured.value.outcome == "permanent_failure"
    assert rejected.rolled_back is True
    assert "private-password" not in str(captured.value)

    provider = PostgreSQLAccessEffectProvider(
        settings=PostgreSQLAccessSettings(
            administrative_dsn=SecretStr("postgresql://private-password")
        ),
        targets=_Authority(),
        connect=_unavailable_connect,
    )
    with pytest.raises(AccessEffectProviderError) as unavailable:
        provider.enact(_command())

    assert unavailable.value.outcome == "transient_failure"
    assert "private-password" not in str(unavailable.value)


def test_target_rejects_identifier_injection_and_duplicate_fields() -> None:
    with pytest.raises(ValidationError):
        _target(role_name='reader"; DROP ROLE admin; --')
    with pytest.raises(ValidationError):
        _target(
            columns=(
                PostgreSQLAccessColumnBinding(field="region", column_name="region"),
                PostgreSQLAccessColumnBinding(field="region", column_name="other_region"),
            )
        )


def test_postgresql_access_provider_conforms_to_replay_contract() -> None:
    connection = _Connection()

    run_access_provider_conformance(
        lambda: _provider(connection),
        lambda **updates: _command(**updates),
    )


_PASSWORD_DSN = (
    "host=warehouse.internal dbname=warehouse user=access_admin "
    "password=private-password sslmode=disable gssencmode=disable"
)


def _provider_rejected_at_startup(
    probe_outcome: Exception,
) -> tuple[PostgreSQLAccessEffectProvider, list[dict[str, object]]]:
    probe_calls: list[dict[str, object]] = []

    def rejected(_dsn: str) -> _Connection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> _Connection:
        probe_calls.append(parameters)
        raise probe_outcome

    provider = PostgreSQLAccessEffectProvider(
        settings=PostgreSQLAccessSettings(administrative_dsn=SecretStr(_PASSWORD_DSN)),
        targets=_Authority(),
        connect=rejected,
        startup_denial_probe=probe,
    )
    return provider, probe_calls


@pytest.mark.parametrize(
    ("probe_outcome", "outcome"),
    (
        (psycopg.errors.InvalidPassword(), "permanent_failure"),
        (psycopg.errors.InvalidAuthorizationSpecification(), "permanent_failure"),
        (psycopg.OperationalError("probe transport failed"), "transient_failure"),
    ),
)
def test_rejected_administrative_credentials_are_permanent_before_any_effect(
    probe_outcome: Exception, outcome: str
) -> None:
    provider, probe_calls = _provider_rejected_at_startup(probe_outcome)

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command())

    assert captured.value.outcome == outcome
    assert len(probe_calls) == 1
    assert "private-password" not in str(captured.value)
