from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

import psycopg
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.errors import ProviderErrorClassification
from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from heinzel_compiler import QueryEstimateRequest, QueryScanEstimate


class PostgreSQLQueryEstimatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dsn: SecretStr = Field(min_length=1)
    connect_timeout_seconds: int = Field(ge=1, le=30)
    statement_timeout_seconds: int = Field(ge=1, le=30)


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
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)

    def estimate(self, request: QueryEstimateRequest) -> QueryScanEstimate | None:
        parameters = _validate_request(request)
        connection: _EstimateConnection | None = None
        cursor: _EstimateCursor | None = None
        try:
            bounded_dsn = psycopg.conninfo.make_conninfo(
                self._settings.dsn.get_secret_value(),
                connect_timeout=str(self._settings.connect_timeout_seconds),
            )
            connection = self._connect(bounded_dsn)
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
