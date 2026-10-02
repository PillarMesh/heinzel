from __future__ import annotations

import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler import (
    GovernedQueryInput,
    GovernedQueryPlan,
    ProductGenerationReference,
    QueryCeilings,
    QueryConsumptionObject,
    QueryDimension,
    QueryMetric,
    QueryOrder,
    QueryReference,
    QueryScan,
    QueryScanEstimate,
    compile_governed_query,
)
from heinzel_compiler.query_repository import SQLiteQueryPlanRepository
from heinzel_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_postgresql import (
    PostgreSQLAnswerGenerationBinding,
    PostgreSQLAnswerGenerationBindingAuthority,
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
)
from heinzel_runtime import (
    AnswerExecutionAuthorization,
    AnswerProductGenerationReference,
    AnswerQueryReference,
    GovernedQueryExecutor,
    QueryGenerationState,
    SQLiteAnswerResultStore,
)
from psycopg import sql
from pydantic import SecretStr


class _Authorizer:
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
        plan_digest: str,
    ) -> AnswerExecutionAuthorization:
        return AnswerExecutionAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
            plan_digest=plan_digest,
            policy_revision=1,
            entitlement_digest="e" * 64,
            row_ceiling=10,
            byte_ceiling=4096,
            statement_timeout_seconds=5,
            result_retention_seconds=3600,
            freshness_observation_ref="freshness-live-1",
            quality_observation_ref="quality-live-1",
        )


class _GenerationReader:
    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState:
        assert reference.product_ref.artifact_id == "product-live-1"
        return QueryGenerationState(addressable=True, current_generation=reference.generation)


class _FixtureGenerationAuthority:
    def __init__(self, binding: PostgreSQLAnswerGenerationBinding) -> None:
        self._binding = binding

    def resolve(
        self,
        *,
        tenant_id: str,
        product_ref: AnswerQueryReference,
        generation: int,
        consumption_object_ref: AnswerQueryReference,
    ) -> PostgreSQLAnswerGenerationBinding | None:
        if (
            tenant_id,
            product_ref,
            generation,
            consumption_object_ref,
        ) != (
            self._binding.tenant_id,
            self._binding.product_ref,
            self._binding.generation,
            self._binding.consumption_object_ref,
        ):
            return None
        return self._binding


def _fixture_generation_authority(
    binding: PostgreSQLAnswerGenerationBinding,
) -> PostgreSQLAnswerGenerationBindingAuthority:
    return _FixtureGenerationAuthority(binding)


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


def _provision_fresh_data(bootstrap_dsn: str, runtime_password: str) -> str:
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE answer_runtime LOGIN PASSWORD {}").format(
                sql.Literal(runtime_password)
            )
        )
        connection.execute("CREATE SCHEMA consumption")
        connection.execute("CREATE SCHEMA private_admin")
        connection.execute(
            "CREATE TABLE consumption.sales "
            "(region text NOT NULL, customer_id bigint NOT NULL, revenue numeric NOT NULL)"
        )
        connection.execute(
            "INSERT INTO consumption.sales VALUES "
            "('west', 1, 10.00), ('west', 2, 20.00), ('east', 3, 99.00)"
        )
        connection.execute("CREATE TABLE private_admin.secrets (secret_value text NOT NULL)")
        connection.execute("INSERT INTO private_admin.secrets VALUES ('unreachable')")
        connection.execute("CREATE ROLE retained_generation_owner NOLOGIN NOSUPERUSER")
        connection.execute("ALTER TABLE consumption.sales OWNER TO retained_generation_owner")
        connection.execute("GRANT USAGE ON SCHEMA consumption TO answer_runtime")
        connection.execute("GRANT SELECT ON consumption.sales TO answer_runtime")
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=parsed["host"],
        port=parsed["port"],
        dbname=parsed["dbname"],
        user="answer_runtime",
        password=runtime_password,
    )


def _compile_plan(signer: QueryPlanSigner) -> GovernedQueryPlan:
    query_input = GovernedQueryInput(
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
        dimensions=(
            QueryDimension(
                dimension_ref=QueryReference(
                    artifact_id="dimension-live-1", version=1, digest="f" * 64
                ),
                column_name="region",
                output_name="region",
            ),
        ),
        filters=(),
        time_window=None,
        ordering=(QueryOrder(output_name="region", direction="ascending"),),
        row_limit=10,
        disclosure_entity_column="customer_id",
        minimum_group_size=2,
        estimated_scan=QueryScanEstimate(rows=3, bytes=256, estimator_version="live-fixture-1"),
        period_scan_consumed=QueryScan(rows=0, bytes=0),
        ceilings=QueryCeilings(
            row_limit=10,
            scan=QueryScan(rows=100, bytes=10_000),
            period_scan=QueryScan(rows=1_000, bytes=100_000),
        ),
    )
    plan = compile_governed_query(query_input, signer=signer)
    assert isinstance(plan, GovernedQueryPlan)
    return plan


def _query_generation_binding(
    bootstrap_dsn: str,
    plan: GovernedQueryPlan,
) -> PostgreSQLAnswerGenerationBinding:
    with psycopg.connect(bootstrap_dsn) as connection:
        row = connection.execute(
            "SELECT relation.oid::bigint, relation.relfilenode::bigint, CURRENT_TIMESTAMP "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'consumption' AND relation.relname = 'sales'"
        ).fetchone()
    assert row is not None
    relation_oid, relation_relfilenode, observed_at = row
    assert type(relation_oid) is int
    assert type(relation_relfilenode) is int
    assert isinstance(observed_at, datetime) and observed_at.tzinfo is not None
    generation = plan.product_generation_refs[0]
    receipt_plan_digest = digest(
        {
            "domain": "heinzel-live-fixture-materialization-plan-v1",
            "tenant_id": plan.tenant_id,
            "product_ref": generation.product_ref,
            "generation": generation.generation,
        }
    )
    provider_commit_reference = digest(
        {
            "domain": "heinzel-postgresql-product-generation-v1",
            "tenant_id": plan.tenant_id,
            "product_id": generation.product_ref.artifact_id,
            "product_revision": generation.product_ref.version,
            "product_generation": generation.generation,
            "model_digest": receipt_plan_digest,
            "relation_identity": (str(relation_oid), str(relation_relfilenode)),
        }
    )
    return PostgreSQLAnswerGenerationBinding(
        tenant_id=plan.tenant_id,
        product_ref=AnswerQueryReference.model_validate(
            generation.product_ref.model_dump(mode="python"), strict=True
        ),
        generation=generation.generation,
        consumption_object_ref=AnswerQueryReference.model_validate(
            plan.consumption_object_refs[0].model_dump(mode="python"), strict=True
        ),
        materialization_receipt_ref=AnswerQueryReference(
            artifact_id="materialization-live-1",
            version=generation.generation,
            digest=digest(
                {
                    "receipt_plan_digest": receipt_plan_digest,
                    "provider_commit_reference": provider_commit_reference,
                }
            ),
        ),
        receipt_plan_digest=receipt_plan_digest,
        provider_commit_reference=provider_commit_reference,
        namespace="consumption",
        relation_name="sales",
        retained_until=observed_at.astimezone(UTC) + timedelta(hours=1),
    )


def _assert_runtime_role_denials(runtime_dsn: str) -> None:
    denied_statements = (
        "INSERT INTO consumption.sales VALUES ('illegal', 9, 1)",
        "SELECT secret_value FROM private_admin.secrets",
        "CREATE ROLE illegal_admin",
    )
    for statement in denied_statements:
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            psycopg.connect(runtime_dsn) as connection,
        ):
            connection.execute(statement)


@pytest.mark.live
def test_fresh_postgresql_compiler_plan_executes_and_persists_exact_result() -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    now = datetime.now(UTC)
    with TemporaryDirectory(prefix="heinzel-answer-postgresql-") as root_text:
        root = Path(root_text)
        runtime_password = secrets.token_urlsafe(32)
        with _fresh_postgresql_cluster(root) as bootstrap_dsn:
            runtime_dsn = _provision_fresh_data(bootstrap_dsn, runtime_password)
            private_key = Ed25519PrivateKey.generate()
            signer = QueryPlanSigner("local-acceptance-key", private_key)
            verifier = QueryPlanVerifier({"local-acceptance-key": private_key.public_key()})
            compiled_plan = _compile_plan(signer)
            plan_path = root / "query-plans.sqlite3"
            plan_connection = sqlite3.connect(plan_path)
            SQLiteQueryPlanRepository(plan_connection).save(compiled_plan)
            plan_connection.close()
            reloaded_connection = sqlite3.connect(plan_path)
            plan = SQLiteQueryPlanRepository(reloaded_connection).read(
                compiled_plan.tenant_id, compiled_plan.plan_digest
            )
            assert plan is not None
            store = SQLiteAnswerResultStore.open(root / "answer-results.sqlite3", clock=lambda: now)
            generation_binding = _query_generation_binding(bootstrap_dsn, compiled_plan)
            provider = PostgreSQLAnswerQueryProvider(
                settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(runtime_dsn)),
                generation_authority=_fixture_generation_authority(generation_binding),
            )
            executor = GovernedQueryExecutor(
                store=store,
                signature_verifier=verifier,
                authorizer=_Authorizer(),
                generation_reader=_GenerationReader(),
                provider_resolver=lambda engine_kind: provider,
                clock=lambda: now,
                sleeper=lambda _delay: None,
            )

            receipt = executor.execute(request_id="request-live-1", plan=plan)
            replay = executor.execute(request_id="request-live-1", plan=plan)
            recorded = store.load_execution("tenant-live-a", "request-live-1")
            result = store.read_result("tenant-live-a", receipt.result_ref or "")

            assert receipt.outcome == "succeeded"
            assert canonical_bytes(replay) == canonical_bytes(receipt)
            assert recorded is not None and canonical_bytes(recorded[1]) == canonical_bytes(receipt)
            assert result.columns[0].name == "region"
            assert result.columns[1].name == "total_revenue"
            assert result.columns[1].value_type == "decimal"
            assert result.rows == (("west", "30.00"),)
            assert result.result_digest == receipt.result_digest
            assert store.list_attempts("tenant-live-a", "request-live-1") == (receipt,)
            _assert_runtime_role_denials(runtime_dsn)
            reloaded_connection.close()

        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(runtime_dsn, connect_timeout=1)
