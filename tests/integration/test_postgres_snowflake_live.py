import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import snowflake.connector
from heinzel_authoring_mcp import AppSettings, build_application
from heinzel_contract_model import FIXED_PROJECTION, digest
from heinzel_provider_snowflake import SnowflakeSettings
from psycopg import sql

from tests.acceptance.resource_ledger import (
    PrivateResourceLedger,
    live_diagnostic_ledger_path,
)

_APP_VARIABLES = (
    "HEINZEL_SIGNING_KEY_ID",
    "HEINZEL_SIGNING_PRIVATE_KEY_B64",
    "HEINZEL_POSTGRES_DSN",
    "HEINZEL_POSTGRES_DATABASE",
    "HEINZEL_POSTGRES_CONNECTION_HANDLE",
    "HEINZEL_POSTGRES_SCHEMA",
    "HEINZEL_POSTGRES_TABLE",
    "HEINZEL_SNOWFLAKE_ACCOUNT",
    "HEINZEL_SNOWFLAKE_USER",
    "HEINZEL_SNOWFLAKE_PASSWORD",
    "HEINZEL_SNOWFLAKE_ROLE",
    "HEINZEL_SNOWFLAKE_WAREHOUSE",
    "HEINZEL_SNOWFLAKE_DATABASE",
    "HEINZEL_SNOWFLAKE_SCHEMA",
    "HEINZEL_SNOWFLAKE_STAGE",
    "HEINZEL_SNOWFLAKE_TARGET_TABLE",
    "HEINZEL_SNOWFLAKE_LEDGER_TABLE",
    "HEINZEL_SNOWFLAKE_CONNECTION_HANDLE",
    "HEINZEL_TEST_POSTGRES_FIXTURE_DSN",
    "HEINZEL_LIVE_DIAGNOSTIC_LEDGER_DIR",
)


@pytest.mark.live
def test_new_postgres_row_reaches_real_snowflake_visibility(
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    missing = tuple(name for name in _APP_VARIABLES if not os.getenv(name))
    if missing:
        pytest.skip("full dedicated account settings are not configured: " + ", ".join(missing))
    # Every other field comes from the HEINZEL_ environment the skip above
    # requires, which mypy cannot see; the service's own entry points carry
    # this same suppression.
    settings = AppSettings(  # type: ignore[call-arg]
        state_path=tmp_path / "state.db", output_dir=tmp_path / "output"
    )
    checked_snowflake = SnowflakeSettings(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password,
        role=settings.snowflake_role,
        warehouse=settings.snowflake_warehouse,
        database=settings.snowflake_database,
        schema_name=settings.snowflake_schema,
        stage=settings.snowflake_stage,
        target_table=settings.snowflake_target_table,
        ledger_table=settings.snowflake_ledger_table,
        connection_handle=settings.snowflake_connection_handle,
    )
    target = (
        f"{checked_snowflake.database}.{checked_snowflake.schema_name}."
        f"{checked_snowflake.target_table}"
    )
    acceptance_key = time.time_ns() // 1_000
    fixture_label = f"diagnostic-{secrets.token_hex(12)}"
    contract_id = f"contract-{secrets.token_hex(12)}"
    resource_ledger = PrivateResourceLedger(
        live_diagnostic_ledger_path(os.environ, "postgres-snowflake"),
        clock=lambda: datetime.now(UTC),
    )
    source_resource = resource_ledger.register(
        kind="source_row",
        exact_identifier=(
            f"postgresql:{settings.postgres_connection_handle}:"
            f"{os.environ['HEINZEL_POSTGRES_DATABASE']}."
            f"{settings.postgres_schema}.{settings.postgres_table}:"
            f"order_id={acceptance_key}"
        ),
        retention_seconds=30 * 24 * 60 * 60,
        cleanup_operation="delete_synthetic_rows",
    )
    state_resource = resource_ledger.register(
        kind="local_state",
        exact_identifier=str(settings.state_path),
        retention_seconds=0,
        cleanup_operation="delete_local_state",
    )
    output_resource = resource_ledger.register(
        kind="local_output",
        exact_identifier=str(settings.output_dir),
        retention_seconds=0,
        cleanup_operation="delete_local_state",
    )
    fixture_inserted = False
    terminal_success = False
    stage_resource: str | None = None
    batch_id: str | None = None
    test_state = "failed"
    snowflake_connection = snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password.get_secret_value(),
        role=settings.snowflake_role,
        warehouse=settings.snowflake_warehouse,
        database=settings.snowflake_database,
        schema=settings.snowflake_schema,
    )
    try:
        with snowflake_connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {target} WHERE order_id = %s", (acceptance_key,))
            absent_row = cursor.fetchone()
            assert absent_row is not None
            assert absent_row[0] == 0
    finally:
        snowflake_connection.close()

    try:
        resource_ledger.mark_attempted(source_resource)
        resource_ledger.persist(run_state="running")
        with psycopg.connect(os.environ["HEINZEL_TEST_POSTGRES_FIXTURE_DSN"]) as connection:
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.{} "
                    "(order_id, customer_ref, amount, currency, status, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(
                    sql.Identifier(settings.postgres_schema),
                    sql.Identifier(settings.postgres_table),
                ),
                (
                    acceptance_key,
                    fixture_label,
                    Decimal("10.50"),
                    "USD",
                    "acceptance",
                    datetime.now(UTC),
                ),
            )
        fixture_inserted = True
        resource_ledger.mark_created(source_resource)

        resource_ledger.mark_attempted(state_resource)
        resource_ledger.mark_attempted(output_resource)
        resource_ledger.persist(run_state="running")
        application = build_application(settings)
        created = application.create_draft(
            {
                "contract_id": contract_id,
                "version": 1,
                "source": {
                    "connection_handle": settings.postgres_connection_handle,
                    "schema": settings.postgres_schema,
                    "table": settings.postgres_table,
                    "primary_key": "order_id",
                },
                "destination": {
                    "connection_handle": settings.snowflake_connection_handle,
                    "database": settings.snowflake_database,
                    "schema": settings.snowflake_schema,
                    "table": settings.snowflake_target_table,
                    "key": "order_id",
                },
                "projection": [item.model_dump() for item in FIXED_PROJECTION],
                "freshness_seconds": 300,
            }
        )
        verified = application.verify(contract_id, 1)
        activation_identity = digest(
            {
                "domain": "heinzel-activation-v1",
                "contract_digest": created["contract_digest"],  # type: ignore[index]
                "summary_digest": verified["summary_digest"],  # type: ignore[index]
                "acceptance_key": acceptance_key,
            }
        )
        run_id = f"run-{activation_identity[:24]}"
        graph = verified["activation_summary"]["signed_graph"]["graph"]  # type: ignore[index]
        batch_id = digest(
            {
                "domain": "heinzel-batch-v1",
                "run_id": run_id,
                "graph": graph,
            }
        )[:32]
        stage_resource = resource_ledger.register(
            kind="staged_segment",
            exact_identifier=(
                f"@{checked_snowflake.connection_handle}:"
                f"{checked_snowflake.database}.{checked_snowflake.schema_name}."
                f"{checked_snowflake.stage}/runs/{batch_id}"
            ),
            retention_seconds=24 * 60 * 60,
            cleanup_operation="delete_staged_segments",
        )
        target_resource = resource_ledger.register(
            kind="target_row",
            exact_identifier=(
                f"snowflake:{checked_snowflake.connection_handle}:{target}:"
                f"order_id={acceptance_key}"
            ),
            retention_seconds=30 * 24 * 60 * 60,
            cleanup_operation="delete_synthetic_rows",
        )
        ledger_resource = resource_ledger.register(
            kind="commit_ledger_entry",
            exact_identifier=(
                f"{checked_snowflake.database}.{checked_snowflake.schema_name}."
                f"{checked_snowflake.ledger_table}:batch_id={batch_id}:"
                f"connection={checked_snowflake.connection_handle}"
            ),
            retention_seconds=30 * 24 * 60 * 60,
            cleanup_operation="delete_synthetic_rows",
        )
        resource_ledger.mark_attempted(stage_resource)
        resource_ledger.mark_quarantined(stage_resource)
        resource_ledger.mark_attempted(target_resource)
        resource_ledger.mark_attempted(ledger_resource)
        resource_ledger.persist(run_state="running")
        run = application.activate(
            created["contract_digest"],  # type: ignore[index]
            verified["summary_digest"],  # type: ignore[index]
            acceptance_key,
        )
        trace = application.get_trace(run["run_id"])  # type: ignore[index]
        assert run["batch_id"] == batch_id  # type: ignore[index]
        resource_ledger.mark_created(stage_resource)
        resource_ledger.mark_scheduled(stage_resource, retention_seconds=24 * 60 * 60)
        resource_ledger.mark_created(target_resource)
        resource_ledger.mark_created(ledger_resource)

        terminal_success = run["state"] == "succeeded"  # type: ignore[index]
        assert terminal_success
        assert trace[-1]["event_type"] == "terminal_success"  # type: ignore[index]
        test_state = "succeeded"
    finally:
        commit_recorded = False
        commit_attempted = False
        if settings.state_path.exists():
            with closing(sqlite3.connect(settings.state_path)) as connection:
                if batch_id is None:
                    row = connection.execute(
                        "SELECT batch_id FROM runs WHERE batch_id IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    if row is not None:
                        batch_id = str(row[0])
                commit_recorded = bool(
                    connection.execute(
                        "SELECT COUNT(*) FROM artifacts WHERE kind = 'commit_receipt'"
                    ).fetchone()[0]
                )
                commit_attempted = bool(
                    connection.execute(
                        "SELECT COUNT(*) FROM evidence_events WHERE event_type = 'commit_attempted'"
                    ).fetchone()[0]
                )
        if batch_id is not None and stage_resource is None:
            stage_resource = resource_ledger.register(
                kind="staged_segment",
                exact_identifier=(
                    f"@{checked_snowflake.connection_handle}:"
                    f"{checked_snowflake.database}.{checked_snowflake.schema_name}."
                    f"{checked_snowflake.stage}/runs/{batch_id}"
                ),
                retention_seconds=24 * 60 * 60,
                cleanup_operation="delete_staged_segments",
            )
            resource_ledger.mark_attempted(stage_resource)
            resource_ledger.mark_quarantined(stage_resource)
        if (commit_recorded or commit_attempted) and batch_id is not None:
            for kind, exact_identifier in (
                (
                    "target_row",
                    f"snowflake:{checked_snowflake.connection_handle}:{target}:"
                    f"order_id={acceptance_key}",
                ),
                (
                    "commit_ledger_entry",
                    f"{checked_snowflake.database}.{checked_snowflake.schema_name}."
                    f"{checked_snowflake.ledger_table}:batch_id={batch_id}:"
                    f"connection={checked_snowflake.connection_handle}",
                ),
            ):
                resource = resource_ledger.register(
                    kind=kind,
                    exact_identifier=exact_identifier,
                    retention_seconds=30 * 24 * 60 * 60,
                    cleanup_operation="delete_synthetic_rows",
                )
                resource_ledger.mark_attempted(resource)
        if fixture_inserted and not commit_attempted:
            with psycopg.connect(os.environ["HEINZEL_TEST_POSTGRES_FIXTURE_DSN"]) as connection:
                connection.execute(
                    sql.SQL("DELETE FROM {}.{} WHERE order_id = %s").format(
                        sql.Identifier(settings.postgres_schema),
                        sql.Identifier(settings.postgres_table),
                    ),
                    (acceptance_key,),
                )
            resource_ledger.mark_completed(source_resource)
        if stage_resource is not None and not terminal_success:
            resource_ledger.mark_quarantined(stage_resource)
        if settings.output_dir.exists():
            resource_ledger.mark_created(output_resource)
            shutil.rmtree(settings.output_dir)
            resource_ledger.mark_completed(output_resource)
        if settings.state_path.exists():
            resource_ledger.mark_created(state_resource)
            settings.state_path.unlink()
            resource_ledger.mark_completed(state_resource)
        resource_ledger.persist(run_state=test_state)
        digests = ",".join(
            str(item["resource_digest"]) for item in resource_ledger.sanitized_dispositions()
        )
        record_property("heinzel_resource_digests", digests)
