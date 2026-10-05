from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

import psycopg
import pytest
from heinzel_compiler import (
    GovernedQueryInput,
    GovernedQueryPlan,
    ProductGenerationReference,
    QueryCeilings,
    QueryConsumptionObject,
    QueryMetric,
    QueryReference,
    QueryScan,
    compile_governed_query,
)
from heinzel_provider_postgresql import (
    PostgreSQLQueryEstimator,
    PostgreSQLQueryEstimatorSettings,
)
from psycopg import sql
from pydantic import SecretStr


class _Signer:
    def sign(self, plan_digest: str) -> str:
        return "test-signature:" + plan_digest


def _postgresql_binary(name: str) -> str:
    configured_directory = os.getenv("HEINZEL_TEST_POSTGRES_BIN_DIR")
    candidate = (
        str(Path(configured_directory) / name)
        if configured_directory is not None
        else shutil.which(name)
    )
    if candidate is None or not Path(candidate).is_file():
        pytest.skip(f"PostgreSQL binary is unavailable: {name}")
    return candidate


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(*arguments: str, timeout_seconds: int = 60) -> None:
    subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        timeout=timeout_seconds,
    )


@contextmanager
def _fresh_postgresql_cluster(root: Path) -> Iterator[str]:
    initdb = _postgresql_binary("initdb")
    pg_ctl = _postgresql_binary("pg_ctl")
    data_directory = root / "postgresql-data"
    bootstrap_password = secrets.token_urlsafe(32)
    password_file = root / "bootstrap-password"
    password_file.write_text(bootstrap_password, encoding="utf-8")
    password_file.chmod(0o600)
    port = _available_port()

    _run(
        initdb,
        "--pgdata",
        str(data_directory),
        "--username=postgres",
        "--auth-host=scram-sha-256",
        "--auth-local=trust",
        "--pwfile",
        str(password_file),
    )
    # `-k` moves the Unix socket off the packaging's compiled-in default, which on Debian and
    # Ubuntu is /var/run/postgresql -- owned by `postgres` and group-writable by `postgres`
    # alone. A developer in that group never notices; a hosted runner's user is not in it, so
    # the server cannot create its socket and `pg_ctl start` exits 1. The DSN below connects
    # over TCP, so nothing depends on where the socket lives.
    #
    # The directory is a short-lived one of its own rather than `root / "socket"`, because a
    # Unix socket path is capped at 107 bytes and `root` is a pytest `tmp_path`: it already
    # carries the temporary root, the test's name and this fixture's subdirectory, which for a
    # long test name leaves the socket over the cap. The postmaster then logs that the path is
    # too long and refuses to start -- a failure that reads as a cluster problem and varies
    # with the length of the test's own name.
    socket_directory = Path(mkdtemp(prefix="hz-pg-"))
    log_file = root / "postgresql.log"
    started = False
    try:
        try:
            _run(
                pg_ctl,
                "--pgdata",
                str(data_directory),
                "--options",
                f"-h 127.0.0.1 -p {port} -k {socket_directory}",
                "--log",
                str(log_file),
                "--wait",
                "start",
            )
        except subprocess.CalledProcessError as failure:
            # `pg_ctl` reports only that the start failed; the reason is in the server's own
            # log, which this harness names and would otherwise discard with the directory.
            server_log = log_file.read_text() if log_file.exists() else "<no server log>"
            raise AssertionError(
                f"the PostgreSQL server did not start: {failure}\nserver log:\n{server_log}"
            ) from failure
        started = True
        yield f"postgresql://postgres:{bootstrap_password}@127.0.0.1:{port}/postgres"
    finally:
        if started:
            with suppress(subprocess.SubprocessError):
                _run(
                    pg_ctl,
                    "--pgdata",
                    str(data_directory),
                    "--mode=immediate",
                    "--wait",
                    "stop",
                )
        shutil.rmtree(socket_directory, ignore_errors=True)


def _provision_estimator(bootstrap_dsn: str, password: str) -> str:
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE query_estimator LOGIN PASSWORD {}").format(sql.Literal(password))
        )
        connection.execute("CREATE SCHEMA consumption")
        connection.execute("CREATE SCHEMA private_admin")
        connection.execute(
            "CREATE TABLE consumption.sales (customer_id bigint NOT NULL, revenue numeric NOT NULL)"
        )
        connection.execute(
            "INSERT INTO consumption.sales VALUES (1, 10.00), (2, 20.00), (3, 99.00)"
        )
        connection.execute("CREATE TABLE private_admin.secrets (secret_value text NOT NULL)")
        connection.execute("GRANT USAGE ON SCHEMA consumption TO query_estimator")
        connection.execute("GRANT SELECT ON consumption.sales TO query_estimator")
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=parsed["host"],
        port=parsed["port"],
        dbname=parsed["dbname"],
        user="query_estimator",
        password=password,
    )


def _query_input() -> GovernedQueryInput:
    return GovernedQueryInput(
        tenant_id="tenant-live-a",
        validation_digest="a" * 64,
        intent_kind="metric_value",
        engine_kind="postgresql",
        consumption_object=QueryConsumptionObject(
            object_ref=QueryReference(artifact_id="consumption-live-1", version=1, digest="b" * 64),
            namespace="consumption",
            relation_name="sales",
        ),
        product_generation_refs=(
            ProductGenerationReference(
                product_ref=QueryReference(
                    artifact_id="product-live-1", version=1, digest="c" * 64
                ),
                generation=1,
            ),
        ),
        metrics=(
            QueryMetric(
                metric_ref=QueryReference(artifact_id="metric-live-1", version=1, digest="d" * 64),
                aggregate="sum",
                column_name="revenue",
                output_name="total_revenue",
            ),
        ),
        dimensions=(),
        filters=(),
        time_window=None,
        ordering=(),
        row_limit=10,
        disclosure_entity_column="customer_id",
        minimum_group_size=2,
        estimated_scan=None,
        period_scan_consumed=QueryScan(rows=0, bytes=0),
        ceilings=QueryCeilings(
            row_limit=10,
            scan=QueryScan(rows=100, bytes=10_000),
            period_scan=QueryScan(rows=1_000, bytes=100_000),
        ),
    )


@pytest.mark.live
def test_native_postgresql_estimator_explains_but_does_not_invent_scan_bytes() -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    with TemporaryDirectory(prefix="heinzel-estimator-postgresql-") as root_text:
        password = secrets.token_urlsafe(32)
        with _fresh_postgresql_cluster(Path(root_text)) as bootstrap_dsn:
            estimator_dsn = _provision_estimator(bootstrap_dsn, password)
            estimator = PostgreSQLQueryEstimator(
                settings=PostgreSQLQueryEstimatorSettings(
                    dsn=SecretStr(estimator_dsn),
                    connect_timeout_seconds=5,
                    statement_timeout_seconds=5,
                )
            )

            plan = compile_governed_query(_query_input(), signer=_Signer(), estimator=estimator)

            assert isinstance(plan, GovernedQueryPlan)
            assert plan.estimated_scan is None
            assert plan.routing == "per_question_review"
            with psycopg.connect(estimator_dsn) as connection:
                assert connection.execute("SHOW transaction_read_only").fetchone() == ("off",)
                for statement in (
                    "INSERT INTO consumption.sales VALUES (4, 1)",
                    "SELECT secret_value FROM private_admin.secrets",
                    "CREATE ROLE illegal_admin",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        connection.execute(statement)
                    connection.rollback()
