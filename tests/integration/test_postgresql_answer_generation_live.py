from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler.query_signing import QueryPlanSigner
from heinzel_provider_postgresql import (
    PostgreSQLAnswerGenerationBinding,
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
)
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import AnswerQueryPlan, ReadOnlyAnswerQuery
from psycopg import sql
from pydantic import SecretStr

from tests.integration.test_postgresql_answer_query_live import (
    _assert_runtime_role_denials,
    _compile_plan,
    _fixture_generation_authority,
    _fresh_postgresql_cluster,
    _provision_fresh_data,
    _query_generation_binding,
)


def _request(plan: AnswerQueryPlan) -> ReadOnlyAnswerQuery:
    return ReadOnlyAnswerQuery(
        tenant_id=plan.tenant_id,
        engine_kind=plan.engine_kind,
        statement=plan.statement,
        parameters=plan.parameters,
        statement_timeout_seconds=5,
        row_ceiling=plan.ceilings.row_limit,
        byte_ceiling=4096,
        consumption_object_refs=plan.consumption_object_refs,
        product_generation_refs=plan.product_generation_refs,
    )


def _provider(
    runtime_dsn: str, binding: PostgreSQLAnswerGenerationBinding
) -> PostgreSQLAnswerQueryProvider:
    return PostgreSQLAnswerQueryProvider(
        settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(runtime_dsn)),
        generation_authority=_fixture_generation_authority(binding),
    )


def _assert_provider_denial(
    provider: PostgreSQLAnswerQueryProvider,
    request: ReadOnlyAnswerQuery,
    *,
    classification: str,
) -> None:
    with pytest.raises(ProviderError) as captured:
        provider.execute_read_only(request)

    assert captured.value.classification == classification


@pytest.mark.live
def test_fresh_postgresql_generation_guard_denies_mutable_or_stale_authority() -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    with TemporaryDirectory(prefix="heinzel-answer-generation-postgresql-") as root_text:
        runtime_password = secrets.token_urlsafe(32)
        with _fresh_postgresql_cluster(Path(root_text)) as bootstrap_dsn:
            runtime_dsn = _provision_fresh_data(bootstrap_dsn, runtime_password)
            compiled = _compile_plan(
                QueryPlanSigner("generation-live-key", Ed25519PrivateKey.generate())
            )
            plan = AnswerQueryPlan.model_validate(compiled.model_dump(mode="python"), strict=True)
            request = _request(plan)
            binding = _query_generation_binding(bootstrap_dsn, compiled)
            provider = _provider(runtime_dsn, binding)

            cursor = provider.execute_read_only(request)
            try:
                rows = tuple(iter(cursor.fetchone, None))
            finally:
                cursor.close()

            assert rows == (("west", Decimal("30.00")),)
            _assert_runtime_role_denials(runtime_dsn)

            privileged_password = secrets.token_urlsafe(32)
            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute(
                    sql.SQL("CREATE ROLE privileged_operator LOGIN SUPERUSER PASSWORD {}").format(
                        sql.Literal(privileged_password)
                    )
                )
            parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
            privileged_dsn = psycopg.conninfo.make_conninfo(
                host=parsed["host"],
                port=parsed["port"],
                dbname=parsed["dbname"],
                user="privileged_operator",
                password=privileged_password,
            )

            def connect_after_role_switch(_dsn: str) -> psycopg.Connection[tuple[object, ...]]:
                connection = psycopg.connect(privileged_dsn, autocommit=True)
                connection.execute("SET ROLE answer_runtime").close()
                connection.autocommit = False
                return connection

            switched_provider = PostgreSQLAnswerQueryProvider(
                settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(privileged_dsn)),
                generation_authority=_fixture_generation_authority(binding),
                connect=connect_after_role_switch,
            )
            _assert_provider_denial(
                switched_provider,
                request,
                classification="authorization_denied",
            )
            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute("DROP ROLE privileged_operator")

            expired = binding.model_copy(
                update={"retained_until": datetime.now(UTC) - timedelta(seconds=1)}
            )
            _assert_provider_denial(
                _provider(runtime_dsn, expired),
                request,
                classification="authorization_denied",
            )

            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute("CREATE ROLE generation_writer LOGIN NOSUPERUSER")
                connection.execute("GRANT UPDATE ON consumption.sales TO generation_writer")
            _assert_provider_denial(provider, request, classification="integrity_failure")
            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute("REVOKE UPDATE ON consumption.sales FROM generation_writer")
                connection.execute("DROP ROLE generation_writer")

            with psycopg.connect(bootstrap_dsn) as connection:
                connection.execute("ALTER TABLE consumption.sales RENAME TO sales_g1")
                connection.execute(
                    "CREATE TABLE consumption.sales_g2 "
                    "(region text NOT NULL, customer_id bigint NOT NULL, revenue numeric NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO consumption.sales_g2 VALUES "
                    "('north', 10, 50.00), ('north', 11, 70.00)"
                )
                connection.execute(
                    "ALTER TABLE consumption.sales_g2 OWNER TO retained_generation_owner"
                )
                connection.execute(
                    "CREATE VIEW consumption.sales AS SELECT * FROM consumption.sales_g2"
                )
                connection.execute("GRANT SELECT ON consumption.sales TO answer_runtime")

            _assert_provider_denial(provider, request, classification="integrity_failure")

            with psycopg.connect(bootstrap_dsn) as connection:
                assert connection.execute(
                    "SELECT region, revenue FROM consumption.sales ORDER BY customer_id"
                ).fetchall() == [("north", Decimal("50.00")), ("north", Decimal("70.00"))]
                connection.execute("DROP VIEW consumption.sales")
                connection.execute(
                    "CREATE TABLE consumption.sales (LIKE consumption.sales_g1 INCLUDING ALL)"
                )
                connection.execute(
                    "INSERT INTO consumption.sales VALUES ('replacement', 20, 100.00)"
                )
                connection.execute(
                    "ALTER TABLE consumption.sales OWNER TO retained_generation_owner"
                )
                connection.execute("GRANT SELECT ON consumption.sales TO answer_runtime")

            _assert_provider_denial(provider, request, classification="integrity_failure")
