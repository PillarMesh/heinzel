from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from heinzel_compiler import ProductGenerationReference, QueryEstimateRequest, QueryReference
from heinzel_compiler.sql_models import SqlParameter
from heinzel_provider_postgresql.query_estimator import (
    PostgreSQLQueryEstimator,
    PostgreSQLQueryEstimatorSettings,
    PostgreSQLRelationSizeQueryEstimator,
    PostgreSQLRelationSizeQueryEstimatorSettings,
)
from heinzel_provider_sdk import ProviderError
from pydantic import SecretStr, ValidationError

STATEMENT = (
    'SELECT "source"."region" AS "region", '
    'SUM("source"."revenue") AS "revenue" '
    'FROM "consumption"."sales" AS "source" '
    'WHERE ("source"."recorded_at" >= %s) '
    'GROUP BY "source"."region" '
    'HAVING COUNT(DISTINCT "source"."customer_id") >= %s '
    'ORDER BY "revenue" DESC LIMIT 10'
)


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        plan = {"Node Type": "Aggregate", "Plan Rows": 3, "Plan Width": 40}
        self.row = row or ([{"Plan": plan}],)
        self.executed: tuple[object, tuple[object, ...]] | None = None
        self.failure: psycopg.Error | None = None
        self.closed = False

    def execute(self, statement: object, params: tuple[object, ...]) -> None:
        if self.failure is not None:
            raise self.failure
        self.executed = (statement, params)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self.explain = _Cursor(row)
        self.control: list[tuple[object, tuple[object, ...]]] = []
        self.rolled_back = False
        self.closed = False
        self.transaction_read_only = "on"

    def execute(self, statement: object, params: tuple[object, ...] = ()) -> _Cursor:
        self.control.append((statement, params))
        if statement == "SHOW transaction_read_only":
            return _Cursor((self.transaction_read_only,))
        if isinstance(statement, str) and statement.startswith("EXPLAIN "):
            self.explain.execute(statement, params)
            return self.explain
        return _Cursor()

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _settings() -> PostgreSQLQueryEstimatorSettings:
    return PostgreSQLQueryEstimatorSettings(
        dsn=SecretStr("postgresql://estimator:private-password@warehouse/db"),
        connect_timeout_seconds=4,
        statement_timeout_seconds=2,
    )


def _request(**updates: object) -> QueryEstimateRequest:
    values: dict[str, object] = {
        "engine_kind": "postgresql",
        "statement": STATEMENT,
        "parameters": (
            SqlParameter(
                name="p0",
                value_type="timestamp",
                value=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            SqlParameter(name="p1", value_type="integer", value=5),
        ),
        "product_generation_refs": (
            ProductGenerationReference(
                product_ref=QueryReference(artifact_id="product-1", version=1, digest="a" * 64),
                generation=7,
            ),
        ),
    }
    values.update(updates)
    return QueryEstimateRequest.model_validate(values)


def test_estimator_runs_restricted_explain_read_only_but_returns_scan_unavailable() -> None:
    connection = _Connection()
    opened_dsn = ""

    def connect(dsn: str) -> _Connection:
        nonlocal opened_dsn
        opened_dsn = dsn
        return connection

    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=connect)

    estimate = estimator.estimate(_request())

    assert estimate is None
    assert psycopg.conninfo.conninfo_to_dict(opened_dsn)["connect_timeout"] == "4"
    assert connection.control == [
        ("SET TRANSACTION READ ONLY", ()),
        ("SHOW transaction_read_only", ()),
        ("SELECT set_config('statement_timeout', %s, true)", ("2000",)),
        (
            "EXPLAIN (FORMAT JSON) " + STATEMENT,
            (datetime(2026, 9, 1, tzinfo=UTC), 5),
        ),
    ]
    assert connection.rolled_back is True
    assert connection.closed is True


def test_estimator_fails_closed_when_read_only_transaction_cannot_be_observed() -> None:
    connection = _Connection()
    connection.transaction_read_only = "off"
    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=lambda _dsn: connection)

    with pytest.raises(ProviderError) as captured:
        estimator.estimate(_request())

    assert captured.value.classification == "authorization_denied"
    assert connection.rolled_back is True
    assert connection.closed is True


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_sleep(10)",
        'SELECT "source"."value" FROM "consumption"."sales" AS "source"',
        STATEMENT + "; DELETE FROM private_table",
        STATEMENT.replace(" LIMIT 10", " UNION SELECT 1 LIMIT 10"),
        STATEMENT.replace("%s", "5", 1),
    ],
)
def test_estimator_rejects_noncompiler_statements_before_opening_credentials(
    statement: str,
) -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=connect)

    with pytest.raises(ProviderError) as captured:
        estimator.estimate(_request(statement=statement))

    assert captured.value.classification == "statement_rejected"
    assert opened is False


def test_estimator_rejects_wrong_engine_and_noncanonical_parameters_before_connecting() -> None:
    opened = False

    def connect(_dsn: str) -> _Connection:
        nonlocal opened
        opened = True
        return _Connection()

    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=connect)

    with pytest.raises(ProviderError, match="restricted") as engine_error:
        estimator.estimate(_request(engine_kind="clickhouse"))
    changed = _request().model_copy(
        update={
            "parameters": (
                SqlParameter(name="other", value_type="decimal", value=Decimal("1.5")),
                *_request().parameters[1:],
            )
        }
    )
    with pytest.raises(ProviderError, match="canonical") as parameter_error:
        estimator.estimate(changed)
    with pytest.raises(ProviderError, match="match") as count_error:
        estimator.estimate(_request().model_copy(update={"parameters": _request().parameters[:1]}))

    assert engine_error.value.classification == "statement_rejected"
    assert parameter_error.value.classification == "statement_rejected"
    assert count_error.value.classification == "statement_rejected"
    assert opened is False


@pytest.mark.parametrize(
    "row",
    [
        ([],),
        ([{"Plan": {"Node Type": "Aggregate", "Plan Rows": -1, "Plan Width": 40}}],),
        ([{"Plan": {"Node Type": "Aggregate", "Plan Rows": 3}}],),
        ([{"Plan Rows": 3, "Plan Width": 40}],),
        ("private plan",),
    ],
)
def test_estimator_rejects_malformed_explain_output(row: tuple[object, ...]) -> None:
    connection = _Connection(row)
    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=lambda _dsn: connection)

    with pytest.raises(ProviderError) as captured:
        estimator.estimate(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert "private plan" not in str(captured.value)
    assert connection.rolled_back is True
    assert connection.closed is True


@pytest.mark.parametrize(
    ("failure", "classification"),
    [
        (psycopg.OperationalError("private-password"), "transient_transport"),
        (psycopg.errors.TooManyConnections("private-password"), "transient_unavailable"),
        (psycopg.errors.InsufficientPrivilege("private-password"), "authorization_denied"),
        (psycopg.errors.SyntaxError("private statement"), "statement_rejected"),
    ],
)
def test_estimator_classifies_driver_failures_without_disclosure(
    failure: psycopg.Error, classification: str
) -> None:
    connection = _Connection()
    connection.explain.failure = failure
    estimator = PostgreSQLQueryEstimator(settings=_settings(), connect=lambda _dsn: connection)

    with pytest.raises(ProviderError) as captured:
        estimator.estimate(_request())

    assert captured.value.classification == classification
    assert "private" not in str(captured.value)
    assert connection.rolled_back is True
    assert connection.closed is True


def test_estimator_settings_are_strict_and_timeout_is_bounded() -> None:
    with pytest.raises(ValidationError):
        PostgreSQLQueryEstimatorSettings.model_validate(
            {
                "dsn": "postgresql://estimator@warehouse/db",
                "connect_timeout_seconds": 4,
                "statement_timeout_seconds": 0,
            }
        )
    with pytest.raises(ValidationError):
        PostgreSQLQueryEstimatorSettings.model_validate(
            {
                "dsn": "postgresql://estimator@warehouse/db",
                "connect_timeout_seconds": 4,
                "statement_timeout_seconds": 2,
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("probe_outcome", "classification"),
    (
        (psycopg.errors.InvalidPassword(), "authorization_denied"),
        (psycopg.OperationalError("probe transport failed"), "transient_transport"),
    ),
)
def test_estimator_attributes_only_structured_startup_rejections(
    probe_outcome: Exception, classification: str
) -> None:
    probe_calls: list[dict[str, object]] = []

    def rejected(_dsn: str) -> _Connection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> _Connection:
        probe_calls.append(parameters)
        raise probe_outcome

    estimator = PostgreSQLQueryEstimator(
        settings=_settings().model_copy(
            update={
                "dsn": SecretStr(
                    "host=warehouse.internal dbname=db user=estimator "
                    "password=private-password sslmode=disable gssencmode=disable"
                )
            }
        ),
        connect=rejected,
        startup_denial_probe=probe,
    )

    with pytest.raises(ProviderError) as captured:
        estimator.estimate(_request())

    assert captured.value.classification == classification
    assert [call["connect_timeout"] for call in probe_calls] == [4.0]
    assert "private" not in str(captured.value)


_RELATION_SETTINGS = PostgreSQLRelationSizeQueryEstimatorSettings(
    dsn=SecretStr("postgresql://estimator@127.0.0.1/heinzel"),
    connect_timeout_seconds=5,
    statement_timeout_seconds=5,
    namespace="consumption",
    relation_name="sales",
)


class _SizeCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.closed = False

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def close(self) -> None:
        self.closed = True


class _SizeConnection:
    """Answers the size query, and records the control statements asked before it."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.control: list[tuple[object, tuple[object, ...]]] = []
        self.rolled_back = False
        self.closed = False
        self.transaction_read_only = "on"

    def execute(self, statement: object, params: tuple[object, ...] = ()) -> _SizeCursor:
        self.control.append((statement, params))
        if statement == "SHOW transaction_read_only":
            return _SizeCursor((self.transaction_read_only,))
        if isinstance(statement, str) and statement.startswith("SELECT pg_total_relation_size"):
            return _SizeCursor(self.row)
        return _SizeCursor(None)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _relation_size_estimator(
    connection: _SizeConnection,
    *,
    settings: PostgreSQLRelationSizeQueryEstimatorSettings = _RELATION_SETTINGS,
) -> PostgreSQLRelationSizeQueryEstimator:
    return PostgreSQLRelationSizeQueryEstimator(
        settings=settings,
        connect=lambda dsn: connection,
        # Returns the same connection rather than nothing: a probe hands back something the
        # caller closes, and one that returned `None` could only ever be a probe nobody calls.
        startup_denial_probe=lambda **_: connection,
    )


def test_the_relation_size_bound_is_the_relation_the_statement_reads() -> None:
    """The measured bytes are the bound, and the rows follow from them, not from statistics."""
    connection = _SizeConnection((8192,))

    estimate = _relation_size_estimator(connection).estimate(_request())

    assert estimate is not None
    assert estimate.bytes == 8192
    # A heap tuple costs at least a 24-byte MAXALIGNed header, so a page cannot hold more than
    # this many. Over-counting is the conservative direction for a ceiling.
    assert estimate.rows == 8192 // 24
    assert estimate.estimator_version == "postgresql-relation-size-v1"


def test_the_relation_size_bound_is_measured_in_a_read_only_transaction() -> None:
    """A bound measured by a session that could write is a bound measured by a writer."""
    connection = _SizeConnection((8192,))

    _relation_size_estimator(connection).estimate(_request())

    assert ("SET TRANSACTION READ ONLY", ()) in connection.control
    assert connection.rolled_back and connection.closed


def test_an_empty_relation_bounds_the_scan_at_nothing() -> None:
    """A relation occupying no pages can be scanned for no bytes, which is a bound, not a gap."""
    estimate = _relation_size_estimator(_SizeConnection((0,))).estimate(_request())

    assert estimate is not None
    assert (estimate.rows, estimate.bytes) == (0, 0)


def test_a_statement_over_another_relation_is_refused() -> None:
    """A bound measured against one relation says nothing about a statement over another."""
    elsewhere = _RELATION_SETTINGS.model_copy(update={"relation_name": "other_sales"})

    with pytest.raises(ProviderError) as refusal:
        _relation_size_estimator(_SizeConnection((8192,)), settings=elsewhere).estimate(_request())

    assert refusal.value.classification == "statement_rejected"


def test_an_unmeasurable_relation_is_refused_rather_than_bounded_at_zero() -> None:
    """`to_regclass` is NULL for a relation this role cannot see, and so is its size.

    Reporting that as a zero-byte scan would admit a query over a relation the estimator could
    not even find, under a ceiling it never checked anything against.
    """
    with pytest.raises(ProviderError) as refusal:
        _relation_size_estimator(_SizeConnection((None,))).estimate(_request())

    assert refusal.value.classification == "authorization_denied"


def test_a_session_that_is_not_read_only_is_refused() -> None:
    connection = _SizeConnection((8192,))
    connection.transaction_read_only = "off"

    with pytest.raises(ProviderError) as refusal:
        _relation_size_estimator(connection).estimate(_request())

    assert refusal.value.classification == "authorization_denied"


def test_a_statement_outside_the_compiler_allowlist_is_refused() -> None:
    """The single-relation bound holds for the restricted form and for nothing else."""
    joined = _request(
        statement='SELECT 1 FROM "consumption"."sales" JOIN "x"."y" ON true', parameters=()
    )

    with pytest.raises(ProviderError) as refusal:
        _relation_size_estimator(_SizeConnection((8192,))).estimate(joined)

    assert refusal.value.classification == "statement_rejected"
