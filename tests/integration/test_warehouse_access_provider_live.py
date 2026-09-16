from __future__ import annotations

import os
import socket
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
from pillarmesh_provider_clickhouse import (
    ClickHouseAccessColumnBinding,
    ClickHouseAccessEffectProvider,
    ClickHouseAccessSettings,
    ClickHouseAccessTarget,
)
from pillarmesh_provider_postgresql import (
    PostgreSQLAccessColumnBinding,
    PostgreSQLAccessEffectProvider,
    PostgreSQLAccessSettings,
    PostgreSQLAccessTarget,
)
from pillarmesh_provider_sdk import AccessEffectCommand
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
        os.environ.get("PILLARMESH_RUN_ACCESS_EMULATORS") != "1",
        reason="set PILLARMESH_RUN_ACCESS_EMULATORS=1",
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
    container_name = f"pillarmesh-access-pg-{uuid.uuid4().hex[:12]}"
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
    container_name = f"pillarmesh-access-ch-{uuid.uuid4().hex[:12]}"
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
