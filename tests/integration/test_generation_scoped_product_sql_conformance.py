from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import httpx
import psycopg
import pytest
from heinzel_compiler.clickhouse_sql import emit_generation_scoped_clickhouse
from heinzel_compiler.postgresql_sql import emit_generation_scoped_postgresql
from heinzel_execution_graph import GenerationScopedProductSource, ProductJsonFieldBinding
from heinzel_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)

_FIXTURE_PATH = (
    Path(__file__).parents[2]
    / "services"
    / "compiler"
    / "legality"
    / "product-sql"
    / "fixtures"
    / "generation-scoped-json-conformance.json"
)
_POSTGRESQL_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_CLICKHOUSE_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
_RUN_LIVE = os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") == "1"


def _fixture() -> dict[str, object]:
    loaded: object = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert all(isinstance(key, str) for key in loaded)
    return cast(dict[str, object], loaded)


def _product() -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="raw",
            relation_name="revenue_events",
            alias="revenue_events",
            columns=(
                ColumnDeclaration(name="region", value_type="string", nullable=False),
                ColumnDeclaration(name="revenue", value_type="decimal", nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(output_name="region", expression=region),
                    NamedExpression(output_name="revenue", expression=revenue),
                )
            ),
            AggregateOperation(
                group_by=(region,),
                measures=(
                    AggregateMeasure(function="sum", argument=revenue, output_name="total_revenue"),
                ),
            ),
        ),
        grain=(region,),
        freshness_seconds=3600,
    )


def _source() -> GenerationScopedProductSource:
    fixture = _fixture()
    generation_id = fixture["selected_generation_id"]
    assert isinstance(generation_id, str)
    return GenerationScopedProductSource(
        namespace="raw",
        relation_name="raw_sales",
        generation_column="generation_id",
        payload_column="payload",
        generation_id=generation_id,
        landing_receipt_digest="b" * 64,
        observed_source_schema_digest="c" * 64,
        field_bindings=(
            ProductJsonFieldBinding(
                logical_field="region", json_field="region", scalar_type="string"
            ),
            ProductJsonFieldBinding(
                logical_field="revenue", json_field="revenue", scalar_type="decimal"
            ),
        ),
    )


def _statement(engine: Literal["postgresql", "clickhouse"]) -> str:
    if engine == "postgresql":
        return emit_generation_scoped_postgresql(_product(), _source()).statement
    return emit_generation_scoped_clickhouse(_product(), _source()).statement


def _cases() -> tuple[dict[str, object], ...]:
    cases = _fixture()["cases"]
    assert isinstance(cases, list)
    return tuple(case for case in cases if isinstance(case, dict))


def test_generation_json_fixture_is_complete_and_keeps_invalid_inputs_unapproved() -> None:
    fixture = _fixture()
    cases = _cases()

    assert fixture["schema_version"] == "1"
    assert fixture["rule_id"] == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
    assert fixture["candidate_only"] is True
    assert len(cases) == 20
    assert {case["case_id"] for case in cases} == {
        "valid_rows",
        "missing_group",
        "null_group",
        "missing_measure",
        "null_measure",
        "malformed_decimal",
        "wrong_type_decimal",
        "wrong_generation_excluded",
        "empty_selected_generation",
        "decimal_zero",
        "decimal_negative",
        "decimal_scale_nine",
        "decimal_max_positive",
        "decimal_max_negative",
        "decimal_outside_precision",
        "decimal_excess_scale",
        "decimal_safe_multirow_sum",
        "case_variant_groups",
        "unicode_composed_decomposed_groups",
        "trailing_space_groups",
    }
    for case in cases:
        expected = case["expected"]
        assert isinstance(expected, dict)
        assert set(expected) == {"postgresql", "clickhouse"}
        assert case.get("admissible") is False
        if case["business_outcome"] == "equivalent_rows":
            assert expected["postgresql"] == expected["clickhouse"]
        if case["business_outcome"] == "equivalent_row_set":
            postgresql = expected["postgresql"]
            clickhouse = expected["clickhouse"]
            assert isinstance(postgresql, dict)
            assert isinstance(clickhouse, dict)
            assert postgresql["classification"] == clickhouse["classification"] == "rows"
            assert sorted(postgresql["rows"], key=repr) == sorted(clickhouse["rows"], key=repr)


def test_generation_json_fixture_is_bound_to_the_exact_current_statements() -> None:
    fixture = _fixture()
    statements = fixture["statements"]
    assert isinstance(statements, dict)

    assert statements == {
        "postgresql": _statement("postgresql"),
        "clickhouse": _statement("clickhouse"),
    }


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _postgresql() -> Iterator[str]:
    name = f"heinzel-product-sql-pg-{uuid.uuid4().hex[:12]}"
    password = f"postgres-{uuid.uuid4().hex}"
    port = _available_port()
    subprocess.run(
        (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--publish",
            f"127.0.0.1:{port}:5432",
            _POSTGRESQL_IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as connection:
                    version = connection.execute("SHOW server_version").fetchone()
                    if version is not None and str(version[0]).startswith("18.6"):
                        break
            except psycopg.Error:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL conformance emulator did not become ready")
            time.sleep(0.2)
        yield dsn
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


@contextmanager
def _clickhouse() -> Iterator[tuple[str, tuple[str, str]]]:
    name = f"heinzel-product-sql-ch-{uuid.uuid4().hex[:12]}"
    password = f"clickhouse-{uuid.uuid4().hex}"
    port = _available_port()
    subprocess.run(
        (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env",
            "CLICKHOUSE_USER=administrator",
            "--env",
            f"CLICKHOUSE_PASSWORD={password}",
            "--env",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--publish",
            f"127.0.0.1:{port}:8123",
            _CLICKHOUSE_IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    auth = ("administrator", password)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                response = httpx.post(endpoint, auth=auth, content="SELECT version()", timeout=2)
                if response.status_code == 200 and response.text.strip() == "25.8.32.4":
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickHouse conformance emulator did not become ready")
            time.sleep(0.2)
        yield endpoint, auth
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def _expected(case: dict[str, object], engine: str) -> dict[str, object]:
    expected = case["expected"]
    assert isinstance(expected, dict)
    result = expected[engine]
    assert isinstance(result, dict)
    return result


def _decimal_text(value: object) -> str:
    return format(Decimal(str(value)), ".9f")


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_pinned_postgresql_generation_json_conformance() -> None:
    with _postgresql() as dsn, psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA raw")
        connection.execute(
            "CREATE TABLE raw.raw_sales (generation_id text NOT NULL, payload jsonb NOT NULL)"
        )
        connection.commit()
        failures: list[tuple[object, object, object]] = []
        for case in _cases():
            connection.execute("TRUNCATE raw.raw_sales")
            rows = case["rows"]
            assert isinstance(rows, list)
            connection.cursor().executemany(
                "INSERT INTO raw.raw_sales (generation_id, payload) VALUES (%s, %s::jsonb)",
                tuple((row["generation_id"], row["payload"]) for row in rows),
            )
            connection.commit()
            try:
                observed_rows = connection.execute(
                    f"SELECT * FROM ({_statement('postgresql')}) AS result "
                    'ORDER BY "region" NULLS FIRST'
                ).fetchall()
                observed = {
                    "classification": "rows",
                    "rows": [
                        [row[0], None if row[1] is None else _decimal_text(row[1])]
                        for row in observed_rows
                    ],
                }
            except psycopg.Error:
                connection.rollback()
                observed = {"classification": "statement_rejected"}

            expected = _expected(case, "postgresql")
            if observed != expected:
                failures.append((case["case_id"], expected, observed))

        assert failures == []


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_pinned_clickhouse_generation_json_conformance() -> None:
    with _clickhouse() as (endpoint, auth):
        for statement in (
            "CREATE DATABASE raw",
            "CREATE TABLE raw.raw_sales (generation_id String, payload String) "
            "ENGINE = MergeTree ORDER BY generation_id",
        ):
            response = httpx.post(endpoint, auth=auth, content=statement, timeout=10)
            response.raise_for_status()
        failures: list[tuple[object, object, object]] = []
        for case in _cases():
            response = httpx.post(
                endpoint,
                auth=auth,
                content="TRUNCATE TABLE raw.raw_sales",
                timeout=10,
            )
            response.raise_for_status()
            rows = case["rows"]
            assert isinstance(rows, list)
            if rows:
                payload = "\n".join(
                    json.dumps(
                        {"generation_id": row["generation_id"], "payload": row["payload"]},
                        separators=(",", ":"),
                    )
                    for row in rows
                )
                response = httpx.post(
                    endpoint,
                    auth=auth,
                    content=(
                        "INSERT INTO raw.raw_sales (generation_id, payload) FORMAT JSONEachRow\n"
                        + payload
                    ),
                    timeout=10,
                )
                response.raise_for_status()
            response = httpx.post(
                endpoint,
                auth=auth,
                content=(
                    f'SELECT * FROM ({_statement("clickhouse")}) ORDER BY "region" '
                    "FORMAT TabSeparated"
                ),
                timeout=10,
            )
            if response.status_code == 200:
                observed = {
                    "classification": "rows",
                    "rows": [
                        [
                            None if fields[0] == r"\N" else fields[0],
                            None if fields[1] == r"\N" else _decimal_text(fields[1]),
                        ]
                        for fields in (
                            line.split("\t") for line in response.text.splitlines() if line
                        )
                    ],
                }
            else:
                observed = {"classification": "statement_rejected"}

            expected = _expected(case, "clickhouse")
            if observed != expected:
                failures.append((case["case_id"], expected, observed))

        assert failures == []
