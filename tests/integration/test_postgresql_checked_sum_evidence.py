"""Live checked SUM evidence for PostgreSQL activation of PRODUCT-SQL-V2-PROJECT-SUM-001.

Activation needs an observation, not an argument: what the pinned PostgreSQL engine actually
returns for the emitted product statement at each numeric boundary the checked SUM relies on.

The live test runs every case against the exact pinned image, through the statement the
compiler emits, and compares the result with the committed evidence bundle. Setting
``HEINZEL_WRITE_CHECKED_SUM_EVIDENCE=1`` rewrites the bundle from the run instead. The engine
context and SUM semantics are read with the PostgreSQL provider's own observers, so the bundle
records what the compiler's provenance path would observe.

The offline tests keep the bundle honest without an engine. They fail when the emitted statement
no longer matches the one the evidence was captured against, when a required case is missing,
or when a recorded digest does not match its recorded value. The bundle never carries a DSN,
credential, port, or container name.
"""

from __future__ import annotations

import json
import os
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
from heinzel_compiler import product_compiler
from heinzel_compiler.postgresql_sql import emit_generation_scoped_postgresql
from heinzel_contract_model import digest
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
from heinzel_provider_postgresql.product_materialization import (
    _DECIMAL_57_9_EXCLUSIVE_BOUND,
)
from heinzel_provider_postgresql.product_sql_observation import (
    _observe_sum_semantics,
    _pinned_image_digest,
    _read_context,
)
from heinzel_provider_postgresql.warehouse_settings import POSTGRESQL_WAREHOUSE_IMAGE
from heinzel_provider_sdk import ProductSqlColumnObservation

_LEGALITY = Path(__file__).parents[2] / "services" / "compiler" / "legality" / "product-sql"
_BUNDLE_PATH = _LEGALITY / "fixtures" / "postgresql-live-checked-sum-evidence.json"
_RUN_LIVE = os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_WRITE_EVIDENCE = os.environ.get("HEINZEL_WRITE_CHECKED_SUM_EVIDENCE") == "1"
_RULE_ID = "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
_SELECTED_GENERATION = "a" * 64
_OTHER_GENERATION = "d" * 64

_MAX_INPUT = "99999999999999999999999999999.999999999"
_MAX_RESULT = "9" * 48 + "." + "9" * 9


# Statement cases run the emitted product statement over inserted landing rows. Each row is
# (generation, raw JSON payload text), so a case can express a missing key, a JSON null, a JSON
# number or any other value the landing contract forbids.
def _row(
    revenue: object, *, region: object = "g", generation: str = _SELECTED_GENERATION
) -> tuple[str, str]:
    return generation, json.dumps({"region": region, "revenue": revenue})


_STATEMENT_CASES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("zero", "a zero input sums to exactly zero", (_row("0"),)),
    ("negative", "a negative input is preserved with its sign", (_row("-42.125000000"),)),
    (
        "exact_scale_nine",
        "nine fractional digits survive the widening cast unchanged",
        (_row("0.123456789"),),
    ),
    (
        "maximum_positive_input",
        "the largest Decimal(38,9) input is accepted and summed exactly",
        (_row(_MAX_INPUT),),
    ),
    (
        "maximum_negative_input",
        "the smallest Decimal(38,9) input is accepted and summed exactly",
        (_row("-" + _MAX_INPUT),),
    ),
    (
        "safe_two_row_maximum_sum",
        "two maximum inputs sum past Decimal(38,9) and fit the widened Decimal(57,9) result",
        (_row(_MAX_INPUT), _row(_MAX_INPUT)),
    ),
    (
        "safe_two_row_minimum_sum",
        "two minimum inputs sum past Decimal(38,9) and fit the widened Decimal(57,9) result",
        (_row("-" + _MAX_INPUT), _row("-" + _MAX_INPUT)),
    ),
    (
        "input_at_exclusive_upper_bound",
        "an input of 10^29 exceeds Decimal(38,9) and the statement is refused",
        (_row("1" + "0" * 29),),
    ),
    (
        "input_at_exclusive_lower_bound",
        "an input of -10^29 exceeds Decimal(38,9) and the statement is refused",
        (_row("-1" + "0" * 29),),
    ),
    (
        "empty_group",
        "a selected generation with no rows yields no group, not a zero-valued group",
        (_row("999", generation=_OTHER_GENERATION),),
    ),
    (
        "excluded_generation",
        "rows from another generation never contribute to the selected generation's sum",
        (_row("1"), _row("999", generation=_OTHER_GENERATION)),
    ),
    (
        "invalid_rows_in_another_generation_do_not_refuse",
        "a malformed row outside the selected generation neither contributes nor refuses",
        (_row("1"), _row("NaN", region=None, generation=_OTHER_GENERATION)),
    ),
    ("nan_measure", "NaN is refused before any cast", (_row("NaN"),)),
    ("infinity_measure", "Infinity is refused before any cast", (_row("Infinity"),)),
    ("hex_measure", "a hex literal PostgreSQL would read as 16 is refused", (_row("0x10"),)),
    ("underscore_measure", "an underscore-grouped number is refused", (_row("1_000"),)),
    ("exponent_measure", "exponent notation is refused", (_row("1e3"),)),
    ("whitespace_measure", "surrounding whitespace is refused", (_row(" 7 "),)),
    ("plus_sign_measure", "a leading plus sign is refused", (_row("+5"),)),
    ("leading_zero_measure", "a leading zero is refused", (_row("007"),)),
    (
        "excess_scale_measure",
        "a tenth fractional digit is refused instead of being rounded",
        (_row("1.1234567895"),),
    ),
    ("json_number_measure", "a JSON number is refused instead of coerced", (_row(12.5),)),
    ("null_measure", "a JSON null measure is refused", (_row(None),)),
    (
        "missing_measure",
        "a missing measure key is refused",
        ((_SELECTED_GENERATION, json.dumps({"region": "g"})),),
    ),
    ("null_group", "a JSON null group key is refused", (_row("1", region=None),)),
    (
        "missing_group",
        "a missing group key is refused",
        ((_SELECTED_GENERATION, json.dumps({"revenue": "1"})),),
    ),
    ("non_string_group", "a JSON number group key is refused", (_row("1", region=1),)),
    (
        "trailing_newline_measure",
        "a trailing newline is refused; PostgreSQL's regex end anchor does not skip it",
        (_row("1\n"),),
    ),
    (
        "arabic_indic_digit_measure",
        "a non-ASCII decimal digit is refused; the pattern's digit range is ASCII only",
        (_row("\u0663"),),
    ),
    ("fullwidth_digit_measure", "a fullwidth digit is refused", (_row("\uff11"),)),
    (
        "binary_collation_groups",
        "case variants are distinct groups ordered bytewise under the C collation",
        (
            _row("1", region="b"),
            _row("1", region="A"),
            _row("1", region="a"),
            _row("1", region="B"),
        ),
    ),
)

# Shadowed-session cases run the same statement in a session whose search_path puts a hostile
# schema before pg_catalog. That schema replaces jsonb_typeof, sum, and the ~, =, ->>, and ||
# operators with versions that would accept anything. The statement names every one of them in
# pg_catalog, so its results must be unaffected.
_SHADOWING_SETUP = (
    "CREATE SCHEMA shadow",
    "CREATE FUNCTION shadow.jsonb_typeof(jsonb) RETURNS text LANGUAGE sql AS $$ SELECT 'string' $$",
    "CREATE FUNCTION shadow.always(text, text) RETURNS boolean LANGUAGE sql AS $$ SELECT true $$",
    "CREATE OPERATOR shadow.~ (LEFTARG = text, RIGHTARG = text, FUNCTION = shadow.always)",
    "CREATE OPERATOR shadow.= (LEFTARG = text, RIGHTARG = text, FUNCTION = shadow.always)",
    "CREATE FUNCTION shadow.get(jsonb, text) RETURNS text LANGUAGE sql AS $$ SELECT '1' $$",
    "CREATE OPERATOR shadow.->> (LEFTARG = jsonb, RIGHTARG = text, FUNCTION = shadow.get)",
    "CREATE FUNCTION shadow.cat(text, text) RETURNS text LANGUAGE sql AS $$ SELECT '1' $$",
    "CREATE OPERATOR shadow.|| (LEFTARG = text, RIGHTARG = text, FUNCTION = shadow.cat)",
    "CREATE FUNCTION shadow.acc(numeric, numeric) RETURNS numeric LANGUAGE sql AS $$ SELECT 999 $$",
    "CREATE AGGREGATE shadow.sum(numeric) (SFUNC = shadow.acc, STYPE = numeric)",
)
_SHADOWED_CASES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "shadowed_search_path_valid_rows",
        "a hostile search_path does not change the result for valid rows",
        (_row("10.25", region="east"), _row("2.75", region="east")),
    ),
    (
        "shadowed_search_path_hex_measure",
        "a hostile search_path cannot make the guard accept a hex literal",
        (_row("0x10"),),
    ),
    (
        "shadowed_search_path_other_generation",
        "a hostile search_path cannot widen the generation filter",
        (_row("1"), _row("999", generation=_OTHER_GENERATION)),
    ),
)

# Result-cast probes observe the exact expression the statement applies to its SUM,
# CAST(value AS NUMERIC(57,9)), at its bounds. Driving the SUM itself to 10^48 would take on the
# order of 10^19 maximum-valued rows, so the bound is observed on the cast directly. The NaN probe
# records that the cast alone does NOT refuse NaN; the statement's input guard is what keeps NaN
# from reaching it.
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
    (
        "result_cast_of_nan",
        "the result cast alone accepts NaN, so NaN must be refused before it",
        "NaN",
    ),
)

# Runtime magnitude probes evaluate the PostgreSQL provider's post-materialization violation
# predicate -- value IS NULL OR value <= -10^48 OR value >= 10^48 -- with its own bound constant.
_MAGNITUDE_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "runtime_magnitude_accepts_maximum",
        "the largest Decimal(57,9) value is not a runtime magnitude violation",
        _MAX_RESULT,
    ),
    (
        "runtime_magnitude_flags_exclusive_bound",
        "10^48 is a runtime magnitude violation",
        "1" + "0" * 48,
    ),
    (
        "runtime_magnitude_flags_nan",
        "NaN is a runtime magnitude violation, because PostgreSQL orders NaN above every number",
        "NaN",
    ),
)

_REQUIRED_CASE_IDS = frozenset(
    {case_id for case_id, _, _ in _STATEMENT_CASES}
    | {case_id for case_id, _, _ in _SHADOWED_CASES}
    | {case_id for case_id, _, _ in _CAST_PROBES}
    | {case_id for case_id, _, _ in _MAGNITUDE_PROBES}
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
    name = f"heinzel-checked-sum-pg-{uuid.uuid4().hex[:12]}"
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
    rows: tuple[tuple[str, str], ...],
    *,
    shadowed: bool = False,
) -> dict[str, object]:
    connection.execute("TRUNCATE raw.raw_sales")
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO raw.raw_sales (generation_id, payload) VALUES (%s, %s::jsonb)", rows
        )
    connection.commit()
    try:
        with connection.cursor() as cursor:
            if shadowed:
                cursor.execute("SET LOCAL search_path = shadow, pg_catalog")
            cursor.execute(
                f"SELECT region, pg_catalog.textin(pg_catalog.numeric_out(total_revenue)) "
                f'FROM ({_statement()}) AS result ORDER BY region COLLATE pg_catalog."C"'
            )
            observed_rows = [[row[0], row[1]] for row in cursor.fetchall()]
        connection.rollback()
    except psycopg.Error as error:
        connection.rollback()
        return {"classification": "statement_rejected", "sqlstate": error.sqlstate}
    return {"classification": "rows", "rows": observed_rows}


def _output_shape(connection: psycopg.Connection[tuple[object, ...]]) -> dict[str, str]:
    """Read the declared measure type and grouped-column collation from the real catalog."""
    connection.execute(
        f"CREATE TEMPORARY TABLE checked_sum_shape AS SELECT * FROM ({_statement()}) AS result "
        "WITH NO DATA"
    )
    rows = connection.execute(
        "SELECT attribute.attname, "
        "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
        "coalesce(column_collation.collname, '') "
        "FROM pg_catalog.pg_attribute AS attribute "
        "LEFT JOIN pg_catalog.pg_collation AS column_collation "
        "ON column_collation.oid = NULLIF(attribute.attcollation, 0) "
        "WHERE attribute.attrelid = 'pg_temp.checked_sum_shape'::regclass "
        "AND attribute.attnum > 0"
    ).fetchall()
    connection.rollback()
    columns = {str(row[0]): (str(row[1]), str(row[2])) for row in rows}
    return {
        "measure_column_type": columns["total_revenue"][0],
        "group_column_type": columns["region"][0],
        "group_column_collation": columns["region"][1],
    }


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


def _run_magnitude_probe(
    connection: psycopg.Connection[tuple[object, ...]], value: str
) -> dict[str, object]:
    row = connection.execute(
        "SELECT v IS NULL OR v <= %s OR v >= %s FROM (SELECT %s::numeric AS v) AS probe",
        (-_DECIMAL_57_9_EXCLUSIVE_BOUND, _DECIMAL_57_9_EXCLUSIVE_BOUND, value),
    ).fetchone()
    connection.rollback()
    assert row is not None and type(row[0]) is bool
    return {"classification": "violation" if row[0] else "within_bound"}


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
        output_shape = _output_shape(connection)
        for setup in _SHADOWING_SETUP:
            connection.execute(setup)
        connection.commit()
        for case_id, claim, rows in _STATEMENT_CASES:
            landing_rows = [
                {"generation_id": generation, "payload": payload} for generation, payload in rows
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
        for case_id, claim, rows in _SHADOWED_CASES:
            landing_rows = [
                {"generation_id": generation, "payload": payload} for generation, payload in rows
            ]
            observed = _run_statement_case(connection, rows, shadowed=True)
            cases.append(
                {
                    "case_id": case_id,
                    "kind": "shadowed_statement",
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
        for case_id, claim, value in _MAGNITUDE_PROBES:
            observed = _run_magnitude_probe(connection, value)
            cases.append(
                {
                    "case_id": case_id,
                    "kind": "runtime_magnitude_probe",
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
        "output_shape": output_shape,
        "cases": cases,
    }


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1")
def test_pinned_postgresql_checked_sum_matches_the_recorded_evidence() -> None:
    captured = _capture()
    if _WRITE_EVIDENCE:
        _BUNDLE_PATH.write_text(json.dumps(captured, indent=2) + "\n", encoding="utf-8")
    recorded = _load_bundle()

    assert captured["engine"] == recorded["engine"]
    assert captured["statement_digest"] == recorded["statement_digest"]
    assert captured["output_shape"] == recorded["output_shape"]
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
    assert "heinzel-checked-sum-pg-" not in text


def test_every_malformed_landing_value_is_refused_by_the_statement_guard() -> None:
    """Refusals of malformed input must come from the decode guard (22P02), never from coercion.

    If a malformed value produced rows, or were refused only by an overflow in a later cast, the
    guard would not be doing what the proof note says.
    """
    refused = {
        "input_at_exclusive_upper_bound",
        "input_at_exclusive_lower_bound",
        "nan_measure",
        "infinity_measure",
        "hex_measure",
        "underscore_measure",
        "exponent_measure",
        "whitespace_measure",
        "plus_sign_measure",
        "leading_zero_measure",
        "excess_scale_measure",
        "json_number_measure",
        "null_measure",
        "missing_measure",
        "null_group",
        "missing_group",
        "non_string_group",
        "trailing_newline_measure",
        "arabic_indic_digit_measure",
        "fullwidth_digit_measure",
        "shadowed_search_path_hex_measure",
    }
    observed = {case["case_id"]: case["observed"] for case in _bundle_cases()}

    for case_id in refused:
        assert observed[case_id] == {"classification": "statement_rejected", "sqlstate": "22P02"}, (
            case_id
        )


def test_grouping_uses_the_binary_collation_the_rule_claims() -> None:
    shape = _load_bundle()["output_shape"]
    assert isinstance(shape, dict)

    assert shape["group_column_collation"] == "C"
    assert shape["measure_column_type"] == "numeric(57,9)"


def test_a_hostile_search_path_does_not_change_the_statement_result() -> None:
    """The same statement returns the same answer in a session with a shadowing search_path."""
    observed = {case["case_id"]: case["observed"] for case in _bundle_cases()}

    assert observed["shadowed_search_path_valid_rows"] == {
        "classification": "rows",
        "rows": [["east", "13.000000000"]],
    }
    assert observed["shadowed_search_path_other_generation"] == observed["excluded_generation"]
