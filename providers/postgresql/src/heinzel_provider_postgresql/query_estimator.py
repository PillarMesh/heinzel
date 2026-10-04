from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast

import psycopg
from heinzel_compiler import QueryEstimateRequest, QueryScanEstimate
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)


class PostgreSQLQueryEstimatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dsn: SecretStr = Field(min_length=1)
    connect_timeout_seconds: int = Field(ge=1, le=30)
    statement_timeout_seconds: int = Field(ge=1, le=30)


class PostgreSQLRelationSizeQueryEstimatorSettings(BaseModel):
    """Where to measure, and which relation a statement is allowed to be measured against."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dsn: SecretStr = Field(min_length=1)
    connect_timeout_seconds: int = Field(ge=1, le=30)
    statement_timeout_seconds: int = Field(ge=1, le=30)
    namespace: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    relation_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class _EstimateCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def close(self) -> None: ...


class _EstimateConnection(Protocol):
    def execute(self, statement: object, params: tuple[object, ...] = ()) -> _EstimateCursor: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _EstimateConnection]

_IDENTIFIER = r'"[A-Za-z_][A-Za-z0-9_]*"'
_SOURCE_COLUMN = rf'"source"\.{_IDENTIFIER}'
_PROJECTION = (
    rf"(?:{_SOURCE_COLUMN} AS {_IDENTIFIER}|"
    rf"(?:AVG|COUNT|MAX|MIN|SUM)\({_SOURCE_COLUMN}\) AS {_IDENTIFIER})"
)
_PREDICATE = rf"\({_SOURCE_COLUMN} (?:=|>=|<=|<>|>|<) %s\)"
_ORDER = rf"{_IDENTIFIER} (?:ASC|DESC)"
_RESTRICTED_QUERY = re.compile(
    rf"^SELECT {_PROJECTION}(?:, {_PROJECTION})* "
    rf'FROM {_IDENTIFIER}\.{_IDENTIFIER} AS "source"'
    rf"(?: WHERE {_PREDICATE}(?: AND {_PREDICATE})*)?"
    rf"(?: GROUP BY {_SOURCE_COLUMN}(?:, {_SOURCE_COLUMN})*)? "
    rf"HAVING COUNT\(DISTINCT {_SOURCE_COLUMN}\) >= %s"
    rf"(?: ORDER BY {_ORDER}(?:, {_ORDER})*)? LIMIT [1-9][0-9]*$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class PostgreSQLQueryEstimator:
    """Probe PostgreSQL's planner without claiming output estimates are scan estimates."""

    def __init__(
        self,
        *,
        settings: PostgreSQLQueryEstimatorSettings,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def estimate(self, request: QueryEstimateRequest) -> QueryScanEstimate | None:
        parameters = _validate_request(request)
        connection: _EstimateConnection | None = None
        cursor: _EstimateCursor | None = None
        try:
            bounded_dsn = psycopg.conninfo.make_conninfo(
                self._settings.dsn.get_secret_value(),
                connect_timeout=str(self._settings.connect_timeout_seconds),
            )
            connection = connect_attributing_startup_denial(
                self._connect, bounded_dsn, probe=self._startup_denial_probe
            )
            connection.execute("SET TRANSACTION READ ONLY")
            _require_read_only(connection)
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._settings.statement_timeout_seconds * 1000),),
            )
            cursor = connection.execute("EXPLAIN (FORMAT JSON) " + request.statement, parameters)
            _validate_explain(cursor.fetchone())
            # PostgreSQL defines Plan Rows and Plan Width as estimated node output.
            # They cannot conservatively represent storage bytes scanned for policy admission.
            return None
        except ProviderError:
            raise
        except psycopg.Error as error:
            raise _postgresql_estimator_error(error) from None
        except (OSError, TimeoutError):
            raise ProviderError(
                "PostgreSQL query estimate transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "PostgreSQL query estimate returned an invalid response",
                "invalid_provider_response",
            ) from None
        finally:
            if cursor is not None:
                with suppress(Exception):
                    cursor.close()
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()


def _require_read_only(connection: _EstimateConnection) -> None:
    cursor = connection.execute("SHOW transaction_read_only")
    try:
        if cursor.fetchone() != ("on",):
            raise ProviderError(
                "PostgreSQL query estimate requires read-only access",
                "authorization_denied",
            )
    finally:
        with suppress(Exception):
            cursor.close()


def _validate_request(request: QueryEstimateRequest) -> tuple[object, ...]:
    if (
        request.engine_kind != "postgresql"
        or type(request.statement) is not str
        or _RESTRICTED_QUERY.fullmatch(request.statement) is None
    ):
        raise ProviderError(
            "PostgreSQL query estimate requires a restricted compiler statement",
            "statement_rejected",
        )
    if request.statement.count("%s") != len(request.parameters):
        raise ProviderError(
            "PostgreSQL query estimate parameters do not match the statement",
            "statement_rejected",
        )
    names = tuple(getattr(parameter, "name", None) for parameter in request.parameters)
    if names != tuple(f"p{index}" for index in range(len(request.parameters))):
        raise ProviderError(
            "PostgreSQL query estimate parameters are not canonical", "statement_rejected"
        )
    _validate_generation_references(request.product_generation_refs)
    return tuple(_parameter_value(parameter) for parameter in request.parameters)


def _parameter_value(parameter: object) -> object:
    value_type = getattr(parameter, "value_type", None)
    value = getattr(parameter, "value", None)
    if value_type == "boolean" and type(value) is bool:
        return value
    if value_type == "integer" and type(value) is int:
        return value
    if value_type == "decimal" and isinstance(value, Decimal) and value.is_finite():
        return value
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "timestamp" and isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    raise ProviderError("PostgreSQL query estimate parameter type is invalid", "statement_rejected")


def _validate_generation_references(references: object) -> None:
    if not isinstance(references, tuple) or not references:
        raise ProviderError(
            "PostgreSQL query estimate generation references are invalid", "statement_rejected"
        )
    for reference in references:
        product = getattr(reference, "product_ref", None)
        generation = getattr(reference, "generation", None)
        artifact_id = getattr(product, "artifact_id", None)
        version = getattr(product, "version", None)
        digest_value = getattr(product, "digest", None)
        if not (
            type(generation) is int
            and generation >= 1
            and isinstance(artifact_id, str)
            and bool(artifact_id)
            and type(version) is int
            and version >= 1
            and isinstance(digest_value, str)
            and _DIGEST.fullmatch(digest_value) is not None
        ):
            raise ProviderError(
                "PostgreSQL query estimate generation references are invalid",
                "statement_rejected",
            )


def _validate_explain(row: tuple[object, ...] | None) -> None:
    if row is None or len(row) != 1 or not isinstance(row[0], list) or len(row[0]) != 1:
        raise ProviderError(
            "PostgreSQL query estimate returned invalid planner output",
            "invalid_provider_response",
        )
    document = row[0][0]
    plan = document.get("Plan") if isinstance(document, dict) else None
    rows = plan.get("Plan Rows") if isinstance(plan, dict) else None
    width = plan.get("Plan Width") if isinstance(plan, dict) else None
    if type(rows) is not int or rows < 0 or type(width) is not int or width < 0:
        raise ProviderError(
            "PostgreSQL query estimate returned invalid planner output",
            "invalid_provider_response",
        )


def _postgresql_estimator_error(error: psycopg.Error) -> ProviderError:
    sqlstate = error.sqlstate or ""
    if sqlstate.startswith("28") or sqlstate == "42501":
        classification: ProviderErrorClassification = "authorization_denied"
    elif sqlstate.startswith(("40", "53", "57", "58")):
        classification = "transient_unavailable"
    elif sqlstate.startswith("08") or isinstance(error, psycopg.OperationalError):
        classification = "transient_transport"
    elif sqlstate.startswith("23"):
        classification = "integrity_failure"
    else:
        classification = "statement_rejected"
    return ProviderError("PostgreSQL query estimate failed", classification)


# Every heap tuple costs at least a 23-byte header MAXALIGNed to 24, plus a 4-byte line pointer
# in its page. Dividing a byte bound by 24 therefore over-counts tuples rather than under-counts
# them, which is the conservative direction for a ceiling: the smaller the divisor, the larger
# the row bound, and a bound that is too large trips a ceiling rather than slipping under one.
_MINIMUM_HEAP_TUPLE_BYTES = 24
_RELATION_SIZE_ESTIMATOR_VERSION = "postgresql-relation-size-v1"


class PostgreSQLRelationSizeQueryEstimator:
    """Bound a restricted statement's scan by the measured size of the one relation it reads.

    `PostgreSQLQueryEstimator` beside this one refuses to estimate, and is right to: PostgreSQL's
    Plan Rows and Plan Width are estimated node output and cannot conservatively represent storage
    bytes scanned. This bounds the scan a different way, and the two claims do not conflict.

    The bound, and why it holds:

    - The compiler's restricted statement form reads exactly one relation -- `FROM "ns"."rel" AS
      "source"` -- with no join, subquery or common table expression. `_RESTRICTED_QUERY` is what
      establishes that, and this estimator refuses any statement that does not match it, and any
      statement whose relation is not the one it was configured for.
    - `pg_total_relation_size` is that relation's heap, its indexes and its TOAST, in bytes, as
      they are on disk now. No execution of a statement over one relation can read more bytes
      than that relation occupies, so it is an upper bound on bytes scanned.
    - Rows scanned are at most the tuples those bytes can hold, which is the byte bound divided
      by the smallest a heap tuple can be. See `_MINIMUM_HEAP_TUPLE_BYTES`.

    It needs no statistics, so it does not depend on `ANALYZE` having run -- which matters because
    a product relation is measured as soon as it is materialized. It is loose by design: a
    conservative bound that is too high refuses a query a tighter one would admit, and that is the
    error worth making when the number feeds a policy ceiling.

    What it is not: a measurement of what a particular plan will read. A query reading one page of
    a large relation is bounded by the whole relation. A deployment wanting a tighter bound needs
    per-plan accounting the engine does not offer before execution.
    """

    def __init__(
        self,
        *,
        settings: PostgreSQLRelationSizeQueryEstimatorSettings,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )

    def estimate(self, request: QueryEstimateRequest) -> QueryScanEstimate | None:
        _validate_request(request)
        self._require_configured_relation(request.statement)
        connection: _EstimateConnection | None = None
        cursor: _EstimateCursor | None = None
        try:
            bounded_dsn = psycopg.conninfo.make_conninfo(
                self._settings.dsn.get_secret_value(),
                connect_timeout=str(self._settings.connect_timeout_seconds),
            )
            connection = connect_attributing_startup_denial(
                self._connect, bounded_dsn, probe=self._startup_denial_probe
            )
            connection.execute("SET TRANSACTION READ ONLY")
            _require_read_only(connection)
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._settings.statement_timeout_seconds * 1000),),
            )
            # The relation is named as a parameter, not interpolated: both halves are already
            # constrained to identifier characters, and `to_regclass` returns NULL rather than
            # raising for a relation this role cannot see.
            cursor = connection.execute(
                "SELECT pg_total_relation_size(to_regclass(%s))",
                (f'"{self._settings.namespace}"."{self._settings.relation_name}"',),
            )
            row = cursor.fetchone()
        except ProviderError:
            raise
        except psycopg.Error as error:
            raise _postgresql_estimator_error(error) from None
        except (OSError, TimeoutError):
            raise ProviderError(
                "PostgreSQL relation size transport failed", "transient_transport"
            ) from None
        except Exception:
            raise ProviderError(
                "PostgreSQL relation size returned an invalid response",
                "invalid_provider_response",
            ) from None
        finally:
            if cursor is not None:
                with suppress(Exception):
                    cursor.close()
            if connection is not None:
                with suppress(Exception):
                    connection.rollback()
                with suppress(Exception):
                    connection.close()
        if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
            # `to_regclass` is NULL for a relation that is absent or invisible to this role, and
            # `pg_total_relation_size(NULL)` is NULL. Either way nothing was measured, and an
            # unmeasured scan must not be reported as a bounded one.
            raise ProviderError(
                "PostgreSQL relation size is unavailable for the configured relation",
                "authorization_denied",
            )
        measured_bytes = row[0]
        return QueryScanEstimate(
            rows=measured_bytes // _MINIMUM_HEAP_TUPLE_BYTES,
            bytes=measured_bytes,
            estimator_version=_RELATION_SIZE_ESTIMATOR_VERSION,
        )

    def _require_configured_relation(self, statement: str) -> None:
        """Refuse a statement that reads a relation other than the measured one.

        A bound measured against one relation says nothing about a statement over another, and the
        statement is restricted enough that its single `FROM` clause is exact text rather than
        something to parse.
        """
        expected = f'FROM "{self._settings.namespace}"."{self._settings.relation_name}" AS "source"'
        if expected not in statement:
            raise ProviderError(
                "PostgreSQL relation size estimator was not configured for this statement",
                "statement_rejected",
            )
