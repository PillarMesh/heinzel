from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import psycopg
import pytest
from heinzel_access_control import (
    AccessGrant,
    AccessGrantApplicationService,
    AdmittedAccessProposal,
    CurrentEntitlementSnapshot,
    SQLiteAccessGrantRepository,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_clickhouse import (
    ClickHouseAccessColumnBinding,
    ClickHouseAccessEffectProvider,
    ClickHouseAccessSettings,
    ClickHouseAccessTarget,
)
from heinzel_provider_postgresql import (
    PostgreSQLAccessColumnBinding,
    PostgreSQLAccessEffectProvider,
    PostgreSQLAccessSettings,
    PostgreSQLAccessTarget,
)
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProviderError,
    AccessEffectResult,
    AccessPermission,
)
from heinzel_runtime import AnswerResultAccessEffectProvider, AnswerResultAccessTarget
from psycopg import sql
from pydantic import SecretStr

_POSTGRESQL_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_CLICKHOUSE_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("HEINZEL_RUN_ACCESS_EMULATORS") != "1",
        reason="set HEINZEL_RUN_ACCESS_EMULATORS=1",
    ),
]


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(
    *arguments: str,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        arguments,
        check=check,
        capture_output=True,
        env=process_environment,
    )


def _stop_and_verify_removed(container_name: str) -> None:
    _run("docker", "stop", container_name, check=False)
    deadline = time.monotonic() + 10
    while _run("docker", "inspect", container_name, check=False).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"container cleanup did not complete: {container_name}")
        time.sleep(0.1)


def _command(
    *, provider_resource_ref: str, action: Literal["apply", "revoke"] = "apply"
) -> AccessEffectCommand:
    return AccessEffectCommand.model_validate(
        {
            "tenant_id": "tenant-live",
            "grant_id": "grant-live-1",
            "grant_revision": 1,
            "surface": "warehouse",
            "action": action,
            "idempotency_key": f"warehouse-{action}-live-1",
            "principal_ref": "principal:requester-live",
            "provider_resource_ref": provider_resource_ref,
            "fields": ("region", "revenue"),
            "permissions": ("query", "view"),
            "effective_at": _NOW,
            "expires_at": _NOW + timedelta(hours=1),
            "scope_digest": "a" * 64,
        }
    )


@dataclass(frozen=True)
class _PostgreSQLFixture:
    administrative_dsn: str
    requester_dsn: str


class _PostgreSQLAuthority:
    def __init__(self, target: PostgreSQLAccessTarget) -> None:
        self._target = target

    def resolve(self, command: AccessEffectCommand) -> PostgreSQLAccessTarget:
        return self._target


@contextmanager
def _postgresql_fixture() -> Iterator[_PostgreSQLFixture]:
    container_name = f"heinzel-access-pg-{uuid.uuid4().hex[:12]}"
    bootstrap_password = f"admin-{uuid.uuid4().hex}"
    requester_password = f"requester-{uuid.uuid4().hex}"
    port = _available_loopback_port()
    _run(
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        container_name,
        "--env",
        "POSTGRES_PASSWORD",
        "--tmpfs",
        "/var/lib/postgresql",
        "--publish",
        f"127.0.0.1:{port}:5432",
        _POSTGRESQL_IMAGE,
        environment={"POSTGRES_PASSWORD": bootstrap_password},
    )
    administrative_dsn = f"postgresql://postgres:{bootstrap_password}@127.0.0.1:{port}/postgres"
    requester_dsn = f"postgresql://requester_live:{requester_password}@127.0.0.1:{port}/postgres"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(administrative_dsn):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        with psycopg.connect(administrative_dsn, autocommit=True) as connection:
            connection.execute("CREATE SCHEMA consumption")
            connection.execute("CREATE SCHEMA raw")
            connection.execute(
                "CREATE TABLE consumption.revenue_v1 "
                "(region text, net_revenue numeric, customer_email text)"
            )
            connection.execute(
                "INSERT INTO consumption.revenue_v1 VALUES ('east', 99.00, 'private@example.test')"
            )
            connection.execute("CREATE TABLE raw.source_orders (order_id bigint)")
            connection.execute("CREATE ROLE pm_grant_live NOLOGIN")
            connection.execute(
                sql.SQL("CREATE ROLE requester_live LOGIN PASSWORD {}").format(
                    sql.Literal(requester_password)
                )
            )
            connection.execute("GRANT pm_grant_live TO requester_live")
        yield _PostgreSQLFixture(
            administrative_dsn=administrative_dsn,
            requester_dsn=requester_dsn,
        )
    finally:
        _stop_and_verify_removed(container_name)


def _postgresql_query(dsn: str, statement: str) -> tuple[object, ...]:
    with psycopg.connect(dsn, autocommit=True) as connection:
        row = connection.execute(statement).fetchone()
    assert row is not None
    return row


def _postgresql_is_denied(dsn: str, statement: str) -> bool:
    try:
        _postgresql_query(dsn, statement)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def test_live_postgresql_access_is_exact_replay_safe_and_immediately_revocable() -> None:
    with _postgresql_fixture() as fixture:
        target = PostgreSQLAccessTarget(
            tenant_id="tenant-live",
            grant_id="grant-live-1",
            grant_revision=1,
            principal_ref="principal:requester-live",
            provider_resource_ref="relation:consumption.revenue_v1",
            role_name="pm_grant_live",
            namespace="consumption",
            relation_name="revenue_v1",
            columns=(
                PostgreSQLAccessColumnBinding(field="region", column_name="region"),
                PostgreSQLAccessColumnBinding(field="revenue", column_name="net_revenue"),
            ),
            scope_digest="a" * 64,
        )
        settings = PostgreSQLAccessSettings(
            administrative_dsn=SecretStr(fixture.administrative_dsn)
        )
        provider = PostgreSQLAccessEffectProvider(
            settings=settings,
            targets=_PostgreSQLAuthority(target),
        )
        apply = _command(provider_resource_ref=target.provider_resource_ref)
        revoke = _command(
            provider_resource_ref=target.provider_resource_ref,
            action="revoke",
        )

        assert _postgresql_is_denied(
            fixture.requester_dsn,
            "SELECT region, net_revenue FROM consumption.revenue_v1",
        )
        first = provider.enact(apply)
        replay = PostgreSQLAccessEffectProvider(
            settings=settings,
            targets=_PostgreSQLAuthority(target),
        ).enact(apply)

        assert replay == first
        assert _postgresql_query(
            fixture.requester_dsn,
            "SELECT region, net_revenue FROM consumption.revenue_v1",
        ) == ("east", 99)
        assert _postgresql_is_denied(
            fixture.requester_dsn,
            "SELECT customer_email FROM consumption.revenue_v1",
        )
        assert _postgresql_is_denied(
            fixture.requester_dsn,
            "SELECT order_id FROM raw.source_orders",
        )
        assert _postgresql_is_denied(
            fixture.requester_dsn,
            "CREATE TABLE consumption.unauthorized (value text)",
        )

        revoked = provider.enact(revoke)
        revoked_replay = provider.enact(revoke)

        assert revoked_replay == revoked
        assert revoked.provider_receipt_digest != first.provider_receipt_digest
        assert _postgresql_is_denied(
            fixture.requester_dsn,
            "SELECT region, net_revenue FROM consumption.revenue_v1",
        )


_PRODUCT = ArtifactReference(artifact_id="product:revenue", version=1, digest="b" * 64)
_PURPOSE = "Review governed regional revenue"


def _current_entitlement() -> CurrentEntitlementSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "entitlement-live-1",
        "tenant_id": "tenant-live",
        "principal_ref": "principal:requester-live",
        "purpose_digest": digest(_PURPOSE),
        "connected_authority_ref": "authority:policy-live",
        "source_revision": 1,
        "source_payload_digest": "c" * 64,
        "observation_id": "entitlement-observation-live-1",
        "product_version_refs": (_PRODUCT,),
        "semantic_refs": (_PRODUCT,),
        "filter_domains": (),
        "permissions": ("query", "view"),
        "effective_at": _NOW - timedelta(minutes=5),
        "valid_until": _NOW + timedelta(hours=1),
        "resolved_at": _NOW,
    }
    values["snapshot_digest"] = digest(
        {"schema_version": "1"}
        | {
            key: values[key]
            for key in (
                "tenant_id",
                "principal_ref",
                "purpose_digest",
                "connected_authority_ref",
                "source_revision",
                "source_payload_digest",
                "product_version_refs",
                "semantic_refs",
                "filter_domains",
                "permissions",
                "effective_at",
                "valid_until",
            )
        }
    )
    return CurrentEntitlementSnapshot.model_validate(values)


def _admitted_proposal() -> AdmittedAccessProposal:
    return AdmittedAccessProposal.model_validate(
        {
            "tenant_id": "tenant-live",
            "request_id": "request-live-1",
            "proposal_id": "proposal-live-1",
            "proposal_revision": 1,
            "admission_receipt_ref": ArtifactReference(
                artifact_id="admission:access-live-1", version=1, digest="d" * 64
            ),
            "entitlement_snapshot_digest": _current_entitlement().snapshot_digest,
            "principal_ref": "principal:requester-live",
            "purpose": _PURPOSE,
            "data_product_version_ref": _PRODUCT,
            "fields": ("region", "revenue"),
            "classification_refs": (),
            "access_mode": "query",
            "permissions": ("query", "view"),
            "effective_at": _NOW - timedelta(minutes=5),
            "expires_at": _NOW + timedelta(minutes=30),
            "policy_revision": 1,
            "targets": (
                {"surface": "result", "provider_resource_ref": "result:regional-revenue"},
                {
                    "surface": "warehouse",
                    "provider_resource_ref": "relation:consumption.revenue_v1",
                },
            ),
        }
    )


class _AdmittedProposals:
    def read_admitted(self, *, tenant_id: str, request_id: str) -> AdmittedAccessProposal:
        assert (tenant_id, request_id) == ("tenant-live", "request-live-1")
        return _admitted_proposal()


class _Entitlements:
    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot:
        return _current_entitlement()


class _GrantBoundPostgreSQLAuthority:
    """Binds each command's grant revision and scope to one fixed physical relation."""

    def resolve(self, command: AccessEffectCommand) -> PostgreSQLAccessTarget | None:
        if command.provider_resource_ref != "relation:consumption.revenue_v1":
            return None
        return PostgreSQLAccessTarget(
            tenant_id=command.tenant_id,
            grant_id=command.grant_id,
            grant_revision=command.grant_revision,
            principal_ref=command.principal_ref,
            provider_resource_ref=command.provider_resource_ref,
            role_name="pm_grant_live",
            namespace="consumption",
            relation_name="revenue_v1",
            columns=(
                PostgreSQLAccessColumnBinding(field="region", column_name="region"),
                PostgreSQLAccessColumnBinding(field="revenue", column_name="net_revenue"),
            ),
            scope_digest=command.scope_digest,
        )


def _result_permissions(
    permissions: tuple[AccessPermission, ...],
) -> tuple[Literal["download", "view"], ...]:
    """Narrow a command's permissions to the ones a result target accepts.

    `AccessGrantApplicationService._surface_permissions` already filters a result command's
    permissions to exactly these, so this is an identity on anything the service emits. It
    exists because `AccessEffectCommand` carries the whole permission domain while
    `AnswerResultAccessTarget` accepts only two of it, and the provider's own `_matches`
    requires the target to carry the command's permissions unchanged.
    """
    narrowed: list[Literal["download", "view"]] = []
    for permission in permissions:
        if permission == "download" or permission == "view":
            narrowed.append(permission)
    return tuple(narrowed)


class _ResultAuthority:
    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget:
        return AnswerResultAccessTarget(
            tenant_id=command.tenant_id,
            grant_id=command.grant_id,
            grant_revision=command.grant_revision,
            principal_ref=command.principal_ref,
            result_ref=command.provider_resource_ref,
            fields=command.fields,
            permissions=_result_permissions(command.permissions),
            effective_at=command.effective_at,
            expires_at=command.expires_at,
            scope_digest=command.scope_digest,
        )


class _RecordedProvider:
    """Delegates to the real provider and records only what it returned."""

    def __init__(self, provider: PostgreSQLAccessEffectProvider) -> None:
        self._provider = provider
        self.enactments: list[tuple[str, AccessEffectFailure | Literal["succeeded"]]] = []

    @property
    def surface(self) -> Literal["warehouse"]:
        return self._provider.surface

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        try:
            result = self._provider.enact(command)
        except AccessEffectProviderError as error:
            self.enactments.append((command.action, error.outcome))
            raise
        self.enactments.append((command.action, "succeeded"))
        return result


def _grant_service(
    administrative_dsn: str,
) -> tuple[AccessGrantApplicationService, SQLiteAccessGrantRepository, _RecordedProvider]:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _RecordedProvider(
        PostgreSQLAccessEffectProvider(
            settings=PostgreSQLAccessSettings(administrative_dsn=SecretStr(administrative_dsn)),
            targets=_GrantBoundPostgreSQLAuthority(),
        )
    )
    service = AccessGrantApplicationService(
        grants=repository,
        admitted_proposals=_AdmittedProposals(),
        entitlements=_Entitlements(),
        providers=(
            AnswerResultAccessEffectProvider.in_memory(
                targets=_ResultAuthority(), clock=lambda: _NOW
            ),
            provider,
        ),
        clock=lambda: _NOW,
    )
    return service, repository, provider


def _apply_live_grant(service: AccessGrantApplicationService) -> AccessGrant:
    return service.apply(
        tenant_id="tenant-live", request_id="request-live-1", grant_id="grant-live-1"
    )


_GRANT_ACTIONS: tuple[Literal["apply", "revoke"], ...] = ("apply", "revoke")


def _receipt_outcomes(
    repository: SQLiteAccessGrantRepository,
) -> tuple[tuple[str, str, str], ...]:
    grant = repository.load_current("tenant-live", "grant-live-1")
    assert grant is not None
    return tuple(
        sorted(
            (receipt.surface, receipt.action, receipt.outcome)
            for revision in range(1, grant.revision + 1)
            for action in _GRANT_ACTIONS
            for receipt in repository.effect_receipts(
                "tenant-live", "grant-live-1", revision, action=action
            )
        )
    )


def _with_password(dsn: str, password: str) -> str:
    return psycopg.conninfo.make_conninfo(dsn, password=password)


def _column_select_is_granted(administrative_dsn: str) -> bool:
    row = _postgresql_query(
        administrative_dsn,
        "SELECT has_column_privilege('pm_grant_live', 'consumption.revenue_v1', "
        "'region', 'SELECT')",
    )
    return row == (True,)


def test_live_rejected_administrative_credential_fails_the_grant_terminally() -> None:
    with _postgresql_fixture() as fixture:
        closed_port_dsn = psycopg.conninfo.make_conninfo(
            fixture.administrative_dsn, port=_available_loopback_port(), connect_timeout=2
        )
        unreachable_service, unreachable_repository, unreachable_provider = _grant_service(
            closed_port_dsn
        )
        unreachable = _apply_live_grant(unreachable_service)
        unreachable_retry = unreachable_service.reconcile(
            tenant_id="tenant-live", grant_id="grant-live-1"
        )

        rejected_service, rejected_repository, rejected_provider = _grant_service(
            _with_password(fixture.administrative_dsn, "not-the-administrative-password")
        )
        rejected = _apply_live_grant(rejected_service)
        rejected_replay = rejected_service.reconcile(
            tenant_id="tenant-live", grant_id="grant-live-1"
        )
        granted_after_rejection = _column_select_is_granted(fixture.administrative_dsn)
        requester_denied_after_rejection = _postgresql_is_denied(
            fixture.requester_dsn, "SELECT region, net_revenue FROM consumption.revenue_v1"
        )

        accepted_service, _accepted_repository, accepted_provider = _grant_service(
            fixture.administrative_dsn
        )
        accepted = _apply_live_grant(accepted_service)
        requester_row = _postgresql_query(
            fixture.requester_dsn, "SELECT region, net_revenue FROM consumption.revenue_v1"
        )

    assert unreachable.state == unreachable_retry.state == "pending"
    assert unreachable_provider.enactments == [
        ("apply", "transient_failure"),
        ("apply", "transient_failure"),
    ]
    assert _receipt_outcomes(unreachable_repository) == (
        ("result", "apply", "succeeded"),
        ("warehouse", "apply", "transient_failure"),
        ("warehouse", "apply", "transient_failure"),
    )

    assert rejected.state == rejected_replay.state == "failed"
    assert rejected.failed_action == "revoke"
    assert rejected_replay == rejected
    assert rejected_provider.enactments == [
        ("apply", "permanent_failure"),
        ("revoke", "permanent_failure"),
    ]
    assert _receipt_outcomes(rejected_repository) == (
        ("result", "apply", "succeeded"),
        ("result", "revoke", "succeeded"),
        ("warehouse", "apply", "permanent_failure"),
        ("warehouse", "revoke", "permanent_failure"),
    )
    assert granted_after_rejection is False
    assert requester_denied_after_rejection is True

    assert accepted.state == "active"
    assert accepted_provider.enactments == [("apply", "succeeded")]
    assert requester_row == ("east", 99)


@dataclass(frozen=True)
class _ClickHouseFixture:
    endpoint: str
    administrative_username: str
    administrative_password: str
    requester_username: str
    requester_password: str


class _ClickHouseAuthority:
    def __init__(self, target: ClickHouseAccessTarget) -> None:
        self._target = target

    def resolve(self, command: AccessEffectCommand) -> ClickHouseAccessTarget:
        return self._target


def _clickhouse_request(
    fixture: _ClickHouseFixture,
    statement: str,
    *,
    requester: bool = False,
) -> httpx.Response:
    username = fixture.requester_username if requester else fixture.administrative_username
    password = fixture.requester_password if requester else fixture.administrative_password
    return httpx.post(
        fixture.endpoint,
        auth=(username, password),
        content=statement,
        timeout=10,
        trust_env=False,
    )


def _clickhouse_is_denied(fixture: _ClickHouseFixture, statement: str) -> bool:
    response = _clickhouse_request(fixture, statement, requester=True)
    return (
        response.status_code != 200 and response.headers.get("X-ClickHouse-Exception-Code") == "497"
    )


@contextmanager
def _clickhouse_fixture() -> Iterator[_ClickHouseFixture]:
    container_name = f"heinzel-access-ch-{uuid.uuid4().hex[:12]}"
    administrative_password = f"admin-{uuid.uuid4().hex}"
    requester_password = f"requester-{uuid.uuid4().hex}"
    port = _available_loopback_port()
    _run(
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        container_name,
        "--env",
        "CLICKHOUSE_USER=access_admin",
        "--env",
        "CLICKHOUSE_PASSWORD",
        "--env",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
        "--tmpfs",
        "/var/lib/clickhouse",
        "--publish",
        f"127.0.0.1:{port}:8123",
        _CLICKHOUSE_IMAGE,
        environment={"CLICKHOUSE_PASSWORD": administrative_password},
    )
    fixture = _ClickHouseFixture(
        endpoint=f"http://127.0.0.1:{port}",
        administrative_username="access_admin",
        administrative_password=administrative_password,
        requester_username="requester_live",
        requester_password=requester_password,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                response = _clickhouse_request(fixture, "SELECT 1")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickHouse access emulator did not become ready")
            time.sleep(0.2)
        setup = (
            "CREATE DATABASE consumption",
            "CREATE DATABASE raw",
            "CREATE TABLE consumption.revenue_v1 "
            "(region String, net_revenue Decimal(12, 2), customer_email String) "
            "ENGINE = MergeTree ORDER BY region",
            "INSERT INTO consumption.revenue_v1 VALUES ('east', 99.00, 'private@example.test')",
            "CREATE TABLE raw.source_orders (order_id UInt64) ENGINE = MergeTree ORDER BY order_id",
            "CREATE ROLE pm_grant_live",
            "CREATE USER requester_live IDENTIFIED WITH plaintext_password "
            f"BY '{requester_password}'",
            "GRANT pm_grant_live TO requester_live",
            "SET DEFAULT ROLE pm_grant_live TO requester_live",
        )
        for statement in setup:
            response = _clickhouse_request(fixture, statement)
            response.raise_for_status()
        yield fixture
    finally:
        _stop_and_verify_removed(container_name)


def test_live_clickhouse_access_is_exact_replay_safe_and_immediately_revocable() -> None:
    with _clickhouse_fixture() as fixture:
        target = ClickHouseAccessTarget(
            tenant_id="tenant-live",
            grant_id="grant-live-1",
            grant_revision=1,
            principal_ref="principal:requester-live",
            provider_resource_ref="relation:consumption.revenue_v1",
            role_name="pm_grant_live",
            database="consumption",
            table_name="revenue_v1",
            columns=(
                ClickHouseAccessColumnBinding(field="region", column_name="region"),
                ClickHouseAccessColumnBinding(field="revenue", column_name="net_revenue"),
            ),
            scope_digest="a" * 64,
        )
        settings = ClickHouseAccessSettings(
            endpoint=fixture.endpoint,
            username=fixture.administrative_username,
            password=SecretStr(fixture.administrative_password),
            verify_tls=False,
        )
        provider = ClickHouseAccessEffectProvider(
            settings=settings,
            targets=_ClickHouseAuthority(target),
        )
        apply = _command(provider_resource_ref=target.provider_resource_ref)
        revoke = _command(
            provider_resource_ref=target.provider_resource_ref,
            action="revoke",
        )

        assert _clickhouse_is_denied(
            fixture,
            "SELECT region, net_revenue FROM consumption.revenue_v1",
        )
        first = provider.enact(apply)
        replay = ClickHouseAccessEffectProvider(
            settings=settings,
            targets=_ClickHouseAuthority(target),
        ).enact(apply)

        assert replay == first
        permitted = _clickhouse_request(
            fixture,
            "SELECT region, net_revenue FROM consumption.revenue_v1 FORMAT TSV",
            requester=True,
        )
        assert permitted.status_code == 200
        assert permitted.text == "east\t99\n"
        assert _clickhouse_is_denied(
            fixture,
            "SELECT customer_email FROM consumption.revenue_v1",
        )
        assert _clickhouse_is_denied(
            fixture,
            "SELECT order_id FROM raw.source_orders",
        )
        assert _clickhouse_is_denied(
            fixture,
            "ALTER TABLE consumption.revenue_v1 ADD COLUMN unauthorized String",
        )

        revoked = provider.enact(revoke)
        revoked_replay = provider.enact(revoke)

        assert revoked_replay == revoked
        assert revoked.provider_receipt_digest != first.provider_receipt_digest
        assert _clickhouse_is_denied(
            fixture,
            "SELECT region, net_revenue FROM consumption.revenue_v1",
        )
