"""Live checked SUM evidence for PostgreSQL activation of PRODUCT-SQL-V2-PROJECT-SUM-001.

Milestone M3 needs an observation, not an argument: what the pinned PostgreSQL engine actually
returns for the emitted product statement at each numeric boundary the proof note relies on.

The live test runs every case against the exact pinned image, through the statement the
compiler emits, and compares the result with the committed evidence bundle. Setting
``PILLARMESH_WRITE_CHECKED_SUM_EVIDENCE=1`` rewrites the bundle from the run instead. The engine
context and SUM semantics are read with the PostgreSQL provider's own observers, so the bundle
records what the compiler's provenance path would observe.

The offline tests keep the bundle honest without an engine. They fail when the emitted statement
no longer matches the one the evidence was captured against, when a case the milestone requires
is missing, when a recorded digest does not match its recorded value, or when the per-case
report disagrees with the bundle. The bundle never carries a DSN, credential, port, or container
name.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import psycopg
import pytest
from pillarmesh_compiler import product_compiler
from pillarmesh_compiler.postgresql_sql import emit_generation_scoped_postgresql
from pillarmesh_contract_model import digest
from pillarmesh_execution_graph import GenerationScopedProductSource, ProductJsonFieldBinding
from pillarmesh_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from pillarmesh_provider_postgresql.product_sql_observation import (
    _observe_sum_semantics,
    _pinned_image_digest,
    _read_context,
)
from pillarmesh_provider_postgresql.warehouse_settings import POSTGRESQL_WAREHOUSE_IMAGE
from pillarmesh_provider_sdk import ProductSqlColumnObservation

_LEGALITY = Path(__file__).parents[2] / "services" / "compiler" / "legality" / "product-sql"
_BUNDLE_PATH = _LEGALITY / "fixtures" / "postgresql-live-checked-sum-evidence.json"
_REPORT_PATH = (
    _LEGALITY / "proof-notes" / "PRODUCT-SQL-V2-PROJECT-SUM-001-POSTGRESQL-LIVE-EVIDENCE.md"
)
_RUN_LIVE = os.environ.get("PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_WRITE_EVIDENCE = os.environ.get("PILLARMESH_WRITE_CHECKED_SUM_EVIDENCE") == "1"
_RULE_ID = "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
_SELECTED_GENERATION = "a" * 64
_OTHER_GENERATION = "d" * 64

_MAX_INPUT = "99999999999999999999999999999.999999999"
_MAX_RESULT = "9" * 48 + "." + "9" * 9

# Statement cases run the emitted product statement over inserted landing rows. Each row is
# (generation, region, revenue-as-JSON-string).
_STATEMENT_CASES: tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...] = (
    ("zero", "a zero input sums to exactly zero", ((_SELECTED_GENERATION, "g", "0"),)),
    (
        "negative",
        "a negative input is preserved with its sign",
        ((_SELECTED_GENERATION, "g", "-42.125000000"),),
    ),
    (
        "exact_scale_nine",
        "nine fractional digits survive the widening cast unchanged",
        ((_SELECTED_GENERATION, "g", "0.123456789"),),
    ),
    (
        "maximum_positive_input",
        "the largest Decimal(38,9) input is accepted and summed exactly",
        ((_SELECTED_GENERATION, "g", _MAX_INPUT),),
    ),
    (
        "maximum_negative_input",
        "the smallest Decimal(38,9) input is accepted and summed exactly",
        ((_SELECTED_GENERATION, "g", "-" + _MAX_INPUT),),
    ),
    (
        "safe_two_row_maximum_sum",
        "two maximum inputs sum past Decimal(38,9) and fit the widened Decimal(57,9) result",
        ((_SELECTED_GENERATION, "g", _MAX_INPUT), (_SELECTED_GENERATION, "g", _MAX_INPUT)),
    ),
    (
        "safe_two_row_minimum_sum",
        "two minimum inputs sum past Decimal(38,9) and fit the widened Decimal(57,9) result",
        (
            (_SELECTED_GENERATION, "g", "-" + _MAX_INPUT),
            (_SELECTED_GENERATION, "g", "-" + _MAX_INPUT),
        ),
    ),
    (
        "input_at_exclusive_upper_bound",
        "an input of 10^29 exceeds Decimal(38,9) and the statement is refused",
        ((_SELECTED_GENERATION, "g", "1" + "0" * 29),),
    ),
    (
        "input_at_exclusive_lower_bound",
        "an input of -10^29 exceeds Decimal(38,9) and the statement is refused",
        ((_SELECTED_GENERATION, "g", "-1" + "0" * 29),),
    ),
    (
        "empty_group",
        "a selected generation with no rows yields no group, not a zero-valued group",
        ((_OTHER_GENERATION, "g", "999"),),
    ),
    (
        "excluded_generation",
        "rows from another generation never contribute to the selected generation's sum",
        ((_SELECTED_GENERATION, "g", "1"), (_OTHER_GENERATION, "g", "999")),
    ),
)

# Result-cast probes observe the exact expression the statement applies to its SUM,
# CAST(value AS NUMERIC(57,9)), at its bounds. Driving the SUM itself to 10^48 would take on the
# order of 10^19 maximum-valued rows, so the bound is observed on the cast directly.
_CAST_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "result_cast_at_maximum",
        "the largest Decimal(57,9) value is accepted by the result cast",
        _MAX_RESULT,
    ),
    (
        "result_cast_at_minimum",
        "the smallest Decimal(57,9) value is accepted by the result cast",
        "-" + _MAX_RESULT,
    ),
    (
        "result_cast_at_exclusive_upper_bound",
        "10^48 overflows the Decimal(57,9) result cast and is refused",
        "1" + "0" * 48,
    ),
    (
        "result_cast_at_exclusive_lower_bound",
        "-10^48 overflows the Decimal(57,9) result cast and is refused",
        "-1" + "0" * 48,
    ),
)

_REQUIRED_CASE_IDS = frozenset(
    {case_id for case_id, _, _ in _STATEMENT_CASES} | {case_id for case_id, _, _ in _CAST_PROBES}
)


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
    return GenerationScopedProductSource(
        namespace="raw",
        relation_name="raw_sales",
        generation_column="generation_id",
        payload_column="payload",
        generation_id=_SELECTED_GENERATION,
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


def _statement() -> str:
    return emit_generation_scoped_postgresql(_product(), _source()).statement


def _load_bundle() -> dict[str, object]:
    loaded: object = json.loads(_BUNDLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _bundle_cases() -> tuple[dict[str, object], ...]:
    cases = _load_bundle()["cases"]
    assert isinstance(cases, list)
    return tuple(case for case in cases if isinstance(case, dict))


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _pinned_postgresql() -> Iterator[str]:
    name = f"pillarmesh-checked-sum-pg-{uuid.uuid4().hex[:12]}"
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
            POSTGRESQL_WAREHOUSE_IMAGE,
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
                    connection.execute("SELECT 1")
                    break
            except psycopg.Error:
                if time.monotonic() >= deadline:
                    raise RuntimeError("pinned PostgreSQL did not become ready") from None
                time.sleep(0.2)
        yield dsn
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def _run_statement_case(
    connection: psycopg.Connection[tuple[object, ...]],
    rows: tuple[tuple[str, str, str], ...],
) -> dict[str, object]:
    connection.execute("TRUNCATE raw.raw_sales")
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO raw.raw_sales (generation_id, payload) VALUES (%s, %s::jsonb)",
            tuple(
                (generation, json.dumps({"region": region, "revenue": revenue}))
                for generation, region, revenue in rows
            ),
        )
    connection.commit()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT region, total_revenue::text FROM ({_statement()}) AS result "
                "ORDER BY region"
            )
            observed_rows = [[row[0], row[1]] for row in cursor.fetchall()]
            description = cursor.description
        connection.rollback()
    except psycopg.Error as error:
        connection.rollback()
        return {"classification": "statement_rejected", "sqlstate": error.sqlstate}
    assert description is not None
    return {"classification": "rows", "rows": observed_rows}


def _output_column_type(connection: psycopg.Connection[tuple[object, ...]]) -> str:
    """Read the declared type of the statement's measure column from the real catalog."""
    connection.execute(
        f"CREATE TEMPORARY TABLE checked_sum_shape AS SELECT * FROM ({_statement()}) AS result "
        "WITH NO DATA"
    )
    row = connection.execute(
        "SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) "
        "FROM pg_catalog.pg_attribute AS attribute "
        "WHERE attribute.attrelid = 'pg_temp.checked_sum_shape'::regclass "
        "AND attribute.attname = 'total_revenue'"
    ).fetchone()
    connection.rollback()
    assert row is not None
    return str(row[0])


def _run_cast_probe(
    connection: psycopg.Connection[tuple[object, ...]], value: str
) -> dict[str, object]:
    try:
        row = connection.execute("SELECT CAST(%s::numeric AS NUMERIC(57,9))::text", (value,))
        observed = row.fetchone()
        connection.rollback()
    except psycopg.Error as error:
        connection.rollback()
        return {"classification": "cast_rejected", "sqlstate": error.sqlstate}
    assert observed is not None
    return {"classification": "accepted", "value": observed[0]}


def _observe_engine(
    connection: psycopg.Connection[tuple[object, ...]],
) -> dict[str, object]:
    context = _read_context(connection)  # type: ignore[arg-type]
    semantics = _observe_sum_semantics(
        connection,  # type: ignore[arg-type]
        decimal_column=ProductSqlColumnObservation(
            name="revenue",
            logical_type="decimal",
            physical_type="NUMERIC(38,9)",
            nullable=False,
            decimal_precision=38,
            decimal_scale=9,
        ),
    )
    connection.rollback()
    return {
        "engine": "postgresql",
        "engine_version": context.engine_version,
        "engine_image": POSTGRESQL_WAREHOUSE_IMAGE.rsplit("@", 1)[0],
        "engine_image_digest": _pinned_image_digest(),
        "engine_build_digest": context.engine_build_digest,
        "database_encoding": context.database_encoding,
        "sum_semantics": semantics.model_dump(mode="json"),
    }


def _capture() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    with _pinned_postgresql() as dsn, psycopg.connect(dsn) as connection:
        connection.execute("CREATE SCHEMA raw")
        connection.execute(
            "CREATE TABLE raw.raw_sales (generation_id text NOT NULL, payload jsonb NOT NULL)"
        )
        connection.commit()
        engine = _observe_engine(connection)
        output_column_type = _output_column_type(connection)
        for case_id, claim, rows in _STATEMENT_CASES:
            landing_rows = [
                {"generation_id": generation, "region": region, "revenue": revenue}
                for generation, region, revenue in rows
            ]
            observed = _run_statement_case(connection, rows)
            cases.append(
                {
                    "case_id": case_id,
                    "kind": "statement",
                    "claim": claim,
                    "input": landing_rows,
                    "input_digest": digest(landing_rows),
                    "observed": observed,
                    "result_digest": digest(observed),
                }
            )
        for case_id, claim, value in _CAST_PROBES:
            observed = _run_cast_probe(connection, value)
            cases.append(
                {
                    "case_id": case_id,
                    "kind": "result_cast_probe",
                    "claim": claim,
                    "input": value,
                    "input_digest": digest(value),
                    "observed": observed,
                    "result_digest": digest(observed),
                }
            )
    statement = _statement()
    return {
        "schema_version": "1",
        "rule_id": _RULE_ID,
        "activation_scope": "postgresql",
        "cross_engine_equivalence_claimed": False,
        "engine": engine,
        "statement_digest": digest(statement),
        "statement": statement,
        "output_column_type": output_column_type,
        "cases": cases,
    }


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_pinned_postgresql_checked_sum_matches_the_recorded_evidence() -> None:
    captured = _capture()
    if _WRITE_EVIDENCE:
        _BUNDLE_PATH.write_text(json.dumps(captured, indent=2) + "\n", encoding="utf-8")
    recorded = _load_bundle()

    assert captured["engine"] == recorded["engine"]
    assert captured["statement_digest"] == recorded["statement_digest"]
    assert captured["output_column_type"] == recorded["output_column_type"]
    assert captured["cases"] == recorded["cases"]


def test_evidence_was_captured_against_the_statement_the_compiler_emits_now() -> None:
    bundle = _load_bundle()

    assert bundle["statement"] == _statement()
    assert bundle["statement_digest"] == digest(_statement())


def test_evidence_covers_every_case_the_milestone_requires() -> None:
    assert {case["case_id"] for case in _bundle_cases()} == _REQUIRED_CASE_IDS


def test_evidence_is_scoped_to_postgresql_and_claims_nothing_about_clickhouse() -> None:
    bundle = _load_bundle()
    engine = bundle["engine"]
    assert isinstance(engine, dict)

    assert bundle["rule_id"] == _RULE_ID
    assert bundle["activation_scope"] == "postgresql"
    assert bundle["cross_engine_equivalence_claimed"] is False
    assert engine["engine"] == "postgresql"
    assert engine["engine_image_digest"] == _pinned_image_digest()
    assert "clickhouse" not in json.dumps(bundle).lower()


def test_evidence_is_for_the_engine_identity_the_compiler_admits() -> None:
    """Checked SUM evidence only counts for the exact engine precondition 8 would admit.

    The harness starts the provider's pinned warehouse image, but the compiler pins its own
    version and image digest separately. If the two ever diverge, recaptured evidence would
    describe an engine the compiler refuses.
    """
    engine = _load_bundle()["engine"]
    assert isinstance(engine, dict)

    assert engine["engine_version"] == product_compiler._ENGINE_VERSIONS["postgresql"]
    assert engine["engine_image_digest"] == product_compiler._ENGINE_IMAGE_DIGESTS["postgresql"]


def test_every_recorded_digest_matches_its_recorded_value() -> None:
    for case in _bundle_cases():
        assert case["input_digest"] == digest(case["input"]), case["case_id"]
        assert case["result_digest"] == digest(case["observed"]), case["case_id"]


def test_evidence_carries_no_connection_detail_or_credential() -> None:
    text = _BUNDLE_PATH.read_text(encoding="utf-8")

    assert "postgresql://" not in text
    assert "password" not in text.lower()
    assert "127.0.0.1" not in text
    assert "pillarmesh-checked-sum-pg-" not in text


def test_report_states_exactly_what_the_bundle_recorded() -> None:
    """The reviewer reads the report; it must not drift from the evidence it summarises."""
    report = _REPORT_PATH.read_text(encoding="utf-8")
    for case in _bundle_cases():
        observed = case["observed"]
        assert isinstance(observed, dict)
        row = re.search(rf"^\| `{case['case_id']}` \|(?P<rest>.*)$", report, re.MULTILINE)
        assert row is not None, f"{case['case_id']} is missing from the report"
        rest = row.group("rest")
        assert str(case["result_digest"])[:16] in rest, case["case_id"]
        stated = rest.split("|")[1].strip()
        classification = observed["classification"]
        if classification in {"statement_rejected", "cast_rejected"}:
            assert stated == f"refused, SQLSTATE `{observed['sqlstate']}`", case["case_id"]
        elif classification == "accepted":
            assert stated == f"accepted `{observed['value']}`", case["case_id"]
        else:
            returned = cast(list[list[str]], observed["rows"])
            expected = (
                ", ".join(f"`{group}` \u2192 `{value}`" for group, value in returned)
                if returned
                else "no rows"
            )
            assert stated == expected, case["case_id"]
