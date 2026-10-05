from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Protocol, cast

import psycopg
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    ProductSqlColumnObservation,
    ProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlSumSemantics,
    ProviderError,
    SignedProductSqlProviderObservation,
)
from heinzel_provider_sdk.errors import ProviderErrorClassification
from heinzel_warehouse_control import EngineKind, WarehouseValidationEvidence
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)
from .warehouse_settings import POSTGRESQL_WAREHOUSE_IMAGE

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"


class PostgreSQLProductSqlObservationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    dsn: SecretStr


class PostgreSQLProductSqlObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    relation_ref: str = Field(min_length=1, max_length=255)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)


class _Cursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class _Connection(Protocol):
    def execute(self, query: object, params: tuple[object, ...] = ()) -> _Cursor: ...

    def close(self) -> None: ...


class _ConnectProtocol(Protocol):
    def __call__(self, dsn: str) -> _Connection: ...


class PostgreSQLProductSqlObserver:
    def __init__(
        self,
        settings: PostgreSQLProductSqlObservationSettings,
        *,
        connect: _ConnectProtocol | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
        signer: ProductSqlProviderObservationSigner | None = None,
    ) -> None:
        if signer is not None and not isinstance(signer, ProductSqlProviderObservationSigner):
            raise ProviderError(
                "PostgreSQL product SQL observation signer configuration is invalid",
                "permanent_configuration",
            )
        self._settings = settings
        self._connect = connect or cast(_ConnectProtocol, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )
        self._signer = signer

    def observe_signed(
        self,
        request: PostgreSQLProductSqlObservationRequest,
        *,
        warehouse_validation: WarehouseValidationEvidence,
    ) -> SignedProductSqlProviderObservation:
        signer = self._signer
        if not isinstance(signer, ProductSqlProviderObservationSigner):
            raise ProviderError(
                "PostgreSQL product SQL observation signer is unavailable",
                "permanent_configuration",
            )
        observation = self.observe(request, warehouse_validation=warehouse_validation)
        try:
            return signer.sign(observation)
        except Exception:
            raise ProviderError(
                "PostgreSQL product SQL observation signer configuration is invalid",
                "permanent_configuration",
            ) from None

    def observe(
        self,
        request: PostgreSQLProductSqlObservationRequest,
        *,
        warehouse_validation: WarehouseValidationEvidence,
    ) -> ProductSqlProviderObservation:
        _require_authority(
            settings=self._settings,
            request=request,
            warehouse_validation=warehouse_validation,
        )
        connection: _Connection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            context = _read_context(connection)
            _require_validated_engine(context, warehouse_validation=warehouse_validation)
            connection.execute(
                sql.SQL("SELECT 1 FROM {}.{} LIMIT 0").format(
                    sql.Identifier(request.relation_namespace),
                    sql.Identifier(request.relation_name),
                )
            )
            relation_oid = _read_relation(connection, request=request)
            columns = _read_columns(
                connection,
                relation_oid=relation_oid,
                database_encoding=context.database_encoding,
            )
            sum_semantics = _observe_sum_semantics(
                connection,
                decimal_column=_STATEMENT_DECIMAL_INPUT,
            )
            observation_id = (
                "psqlobs-"
                + digest(
                    {
                        "domain": "heinzel-postgresql-product-sql-observation-v1",
                        "warehouse_validation_evidence_id": warehouse_validation.evidence_id,
                        "request": request,
                        "relation_oid": relation_oid,
                        "context": context,
                        "columns": columns,
                        "sum_semantics": sum_semantics,
                    }
                )[:32]
            )
            return ProductSqlProviderObservation(
                observation_id=observation_id,
                tenant_id=request.tenant_id,
                warehouse_binding_id=request.warehouse_binding_id,
                warehouse_binding_revision=request.warehouse_binding_revision,
                relation_ref=request.relation_ref,
                relation_namespace=request.relation_namespace,
                relation_name=request.relation_name,
                engine="postgresql",
                engine_version=context.engine_version,
                engine_image_digest=warehouse_validation.engine_image_digest,
                engine_build_digest=context.engine_build_digest,
                observed_at=context.observed_at,
                columns=columns,
                sum_semantics=sum_semantics,
            )
        except ProviderError:
            raise
        except psycopg.Error as error:
            raise _provider_error(error) from None
        except (ArithmeticError, TypeError, ValueError):
            raise ProviderError(
                "PostgreSQL product SQL observation failed", "invalid_provider_response"
            ) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.execute("ROLLBACK")
                with suppress(Exception):
                    connection.close()


class _PostgreSQLContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    engine_version: str
    engine_build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_encoding: str = Field(min_length=1, max_length=32)
    session_timezone: str = Field(min_length=1, max_length=128)
    session_timezone_offset_seconds: int
    observed_at: datetime


# Product statements read a JSON landing relation and cast each decimal field to NUMERIC(38,9)
# themselves, so SUM semantics are observed for that statement input type rather than for a
# table column. A landing relation has no decimal column of its own.
_STATEMENT_DECIMAL_INPUT = ProductSqlColumnObservation(
    name="statement_decimal_input",
    logical_type="decimal",
    physical_type="NUMERIC(38,9)",
    nullable=False,
    decimal_precision=38,
    decimal_scale=9,
)


def _require_authority(
    *,
    settings: PostgreSQLProductSqlObservationSettings,
    request: PostgreSQLProductSqlObservationRequest,
    warehouse_validation: WarehouseValidationEvidence,
) -> None:
    if request.tenant_id != settings.tenant_id:
        raise ProviderError("PostgreSQL product SQL observation failed", "authorization_denied")
    if warehouse_validation.tenant_id != request.tenant_id:
        raise ProviderError("PostgreSQL product SQL observation failed", "authorization_denied")
    if (
        warehouse_validation.binding_id != request.warehouse_binding_id
        or warehouse_validation.binding_revision != request.warehouse_binding_revision
        or warehouse_validation.engine_kind is not EngineKind.POSTGRESQL
        or warehouse_validation.engine_image_digest != _pinned_image_digest()
    ):
        raise ProviderError("PostgreSQL product SQL observation failed", "integrity_failure")


def _read_context(connection: _Connection) -> _PostgreSQLContext:
    row = connection.execute(
        "SELECT current_setting('server_version_num'), current_setting('server_version'), "
        "current_setting('TimeZone'), pg_encoding_to_char(database.encoding), "
        "EXTRACT(timezone FROM transaction_timestamp())::integer, transaction_timestamp() "
        "FROM pg_catalog.pg_database AS database "
        "WHERE database.datname = current_database()"
    ).fetchone()
    if row is None or len(row) != 6:
        raise ValueError("PostgreSQL context observation is invalid")
    if not all(isinstance(row[index], str) for index in range(4)):
        raise ValueError("PostgreSQL context observation is invalid")
    server_version_number = cast(str, row[0])
    server_version_text = cast(str, row[1])
    timezone_offset = row[4]
    observed_at = row[5]
    if (
        type(timezone_offset) is not int
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
    ):
        raise ValueError("PostgreSQL observation timestamp is invalid")
    return _PostgreSQLContext(
        engine_version=_release_version(server_version_number),
        engine_build_digest=digest(
            {
                "domain": "heinzel-postgresql-engine-build-v1",
                "version": {
                    "server_version_num": server_version_number,
                    "server_version": server_version_text,
                },
            }
        ),
        database_encoding=cast(str, row[3]),
        session_timezone=cast(str, row[2]),
        session_timezone_offset_seconds=timezone_offset,
        observed_at=observed_at.astimezone(UTC),
    )


def _release_version(server_version_number: str) -> str:
    if not server_version_number.isascii() or not server_version_number.isdigit():
        raise ValueError("PostgreSQL server version is invalid")
    number = int(server_version_number)
    if number < 100_000:
        raise ValueError("PostgreSQL server version is unsupported")
    return f"{number // 10_000}.{number % 10_000}"


def _require_validated_engine(
    context: _PostgreSQLContext,
    *,
    warehouse_validation: WarehouseValidationEvidence,
) -> None:
    if (
        context.engine_version != warehouse_validation.engine_version
        or context.engine_build_digest != warehouse_validation.engine_build_digest
        or context.session_timezone not in {"UTC", "Etc/UTC"}
        or context.session_timezone_offset_seconds != 0
        or context.observed_at + timedelta(seconds=5) < warehouse_validation.observed_at
    ):
        raise ProviderError("PostgreSQL product SQL observation failed", "integrity_failure")


def _read_relation(
    connection: _Connection,
    *,
    request: PostgreSQLProductSqlObservationRequest,
) -> int:
    row = connection.execute(
        "SELECT relation.oid, relation.relkind, "
        "has_schema_privilege(current_user, namespace.oid, 'USAGE'), "
        "has_table_privilege(current_user, relation.oid, 'SELECT') "
        "FROM pg_catalog.pg_class AS relation "
        "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "WHERE namespace.nspname = %s AND relation.relname = %s "
        "AND relation.relkind IN ('r', 'p')",
        (request.relation_namespace, request.relation_name),
    ).fetchone()
    if row is None or len(row) != 4 or row[1] not in {"r", "p"}:
        raise ProviderError("PostgreSQL product SQL observation failed", "integrity_failure")
    if type(row[2]) is not bool or type(row[3]) is not bool:
        raise ValueError("PostgreSQL relation privileges are invalid")
    if not row[2] or not row[3]:
        raise ProviderError("PostgreSQL product SQL observation failed", "authorization_denied")
    if type(row[0]) is not int or row[0] <= 0:
        raise ValueError("PostgreSQL relation identity is invalid")
    return row[0]


def _read_columns(
    connection: _Connection,
    *,
    relation_oid: int,
    database_encoding: str,
) -> tuple[ProductSqlColumnObservation, ...]:
    rows = connection.execute(
        "SELECT attribute.attnum, attribute.attname, "
        "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
        # PostgreSQL 18 sets attnotnull for a NOT NULL ... NOT VALID constraint, although existing
        # rows may still be NULL, so a column is non-null only if no unvalidated not-null
        # constraint stands behind attnotnull.
        "NOT (attribute.attnotnull AND NOT EXISTS ("
        "SELECT 1 FROM pg_catalog.pg_constraint AS not_null "
        "WHERE not_null.conrelid = attribute.attrelid AND not_null.contype = 'n' "
        "AND not_null.conkey = ARRAY[attribute.attnum] AND NOT not_null.convalidated)), "
        "type.typname, "
        "information_schema._pg_numeric_precision(attribute.atttypid, attribute.atttypmod), "
        "information_schema._pg_numeric_scale(attribute.atttypid, attribute.atttypmod), "
        "collation_namespace.nspname, column_collation.collname, "
        "pg_encoding_to_char(database.encoding) "
        "FROM pg_catalog.pg_attribute AS attribute "
        "JOIN pg_catalog.pg_type AS type ON type.oid = attribute.atttypid "
        "JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid "
        "JOIN pg_catalog.pg_database AS database ON database.datname = current_database() "
        "LEFT JOIN pg_catalog.pg_collation AS column_collation "
        "ON column_collation.oid = NULLIF(attribute.attcollation, 0) "
        "LEFT JOIN pg_catalog.pg_namespace AS collation_namespace "
        "ON collation_namespace.oid = column_collation.collnamespace "
        "WHERE attribute.attrelid = %s AND attribute.attnum > 0 "
        "AND NOT attribute.attisdropped ORDER BY attribute.attnum",
        (relation_oid,),
    ).fetchall()
    if not rows or any(len(row) != 10 for row in rows):
        raise ValueError("PostgreSQL column observation is invalid")
    ordinals: list[int] = []
    for row in rows:
        ordinal = row[0]
        if type(ordinal) is not int or ordinal <= 0:
            raise ValueError("PostgreSQL column order is invalid")
        ordinals.append(ordinal)
    if tuple(ordinals) != tuple(sorted(set(ordinals))):
        raise ValueError("PostgreSQL column order is invalid")
    columns = tuple(_column(row, database_encoding=database_encoding) for row in rows)
    if tuple(column.name for column in columns) != tuple(str(row[1]) for row in rows):
        raise ValueError("PostgreSQL column names are invalid")
    return columns


def _column(
    row: tuple[object, ...],
    *,
    database_encoding: str,
) -> ProductSqlColumnObservation:
    name = row[1]
    physical_type = row[2]
    nullable = row[3]
    type_name = row[4]
    if (
        not isinstance(name, str)
        or not isinstance(physical_type, str)
        or type(nullable) is not bool
    ):
        raise ValueError("PostgreSQL column metadata is invalid")
    if type_name in {"json", "jsonb"}:
        return ProductSqlColumnObservation(
            name=name,
            logical_type="json",
            physical_type=physical_type.upper(),
            nullable=nullable,
        )
    if type_name == "numeric" and (type(row[5]) is not int or type(row[6]) is not int):
        return ProductSqlColumnObservation(
            name=name,
            logical_type="other",
            physical_type=physical_type.upper(),
            nullable=nullable,
        )
    if type_name == "numeric":
        precision = row[5]
        scale = row[6]
        if type(precision) is not int or type(scale) is not int:
            raise ValueError("PostgreSQL numeric column metadata is invalid")
        if (
            not 1 <= precision <= 76
            or not 0 <= scale <= precision
            or physical_type != f"numeric({precision},{scale})"
        ):
            raise ProviderError("PostgreSQL product SQL observation failed", "statement_rejected")
        return ProductSqlColumnObservation(
            name=name,
            logical_type="decimal",
            physical_type=physical_type.upper(),
            nullable=nullable,
            decimal_precision=precision,
            decimal_scale=scale,
            collation=None,
            encoding=None,
        )
    if type_name not in {"text", "varchar"}:
        return ProductSqlColumnObservation(
            name=name,
            logical_type="other",
            physical_type=physical_type.upper(),
            nullable=nullable,
        )
    if (type_name == "text" and physical_type != "text") or (
        type_name == "varchar" and not physical_type.startswith("character varying")
    ):
        raise ValueError("PostgreSQL string type observation is inconsistent")
    if not isinstance(row[7], str) or not isinstance(row[8], str) or row[9] != database_encoding:
        raise ValueError("PostgreSQL string semantics are invalid")
    return ProductSqlColumnObservation(
        name=name,
        logical_type="string",
        physical_type=physical_type.upper(),
        nullable=nullable,
        decimal_precision=None,
        decimal_scale=None,
        collation=str(row[8]) if row[7] == "pg_catalog" else f"{row[7]}.{row[8]}",
        encoding=database_encoding,
    )


def _observe_sum_semantics(
    connection: _Connection,
    *,
    decimal_column: ProductSqlColumnObservation,
) -> ProductSqlSumSemantics:
    aggregate_row = connection.execute(
        "SELECT pg_catalog.format_type(aggregate.aggtranstype, NULL), "
        "pg_catalog.format_type(procedure.prorettype, NULL) "
        "FROM pg_catalog.pg_aggregate AS aggregate "
        "JOIN pg_catalog.pg_proc AS procedure ON procedure.oid = aggregate.aggfnoid "
        "WHERE procedure.proname = 'sum' AND procedure.proargtypes = '1700'::oidvector"
    ).fetchone()
    if aggregate_row is None or aggregate_row != ("internal", "numeric"):
        raise ProviderError("PostgreSQL product SQL observation failed", "integrity_failure")
    precision = decimal_column.decimal_precision
    scale = decimal_column.decimal_scale
    if precision is None or scale is None:
        raise ValueError("PostgreSQL decimal shape is incomplete")
    integer_digits = precision - scale
    maximum_text = "9" * integer_digits
    if scale:
        maximum_text += "." + "9" * scale
    with localcontext() as context:
        context.prec = precision + 1
        expected_sum = Decimal(maximum_text) * 2
    numeric_type = sql.SQL("numeric({precision}, {scale})").format(
        precision=sql.Literal(precision),
        scale=sql.Literal(scale),
    )
    probe_row = connection.execute(
        sql.SQL(
            "WITH inputs(value, group_key) AS (VALUES "
            "(%s::{numeric_type}, 1), (%s::{numeric_type}, 1), "
            "(NULL::{numeric_type}, 1)), "
            "nonempty AS (SELECT SUM(value) AS result, COUNT(value) AS present FROM inputs), "
            "empty_groups AS (SELECT SUM(value) FROM inputs WHERE FALSE GROUP BY group_key) "
            "SELECT result::text, present, (SELECT COUNT(*) FROM empty_groups) FROM nonempty"
        ).format(numeric_type=numeric_type),
        (maximum_text, maximum_text),
    ).fetchone()
    if (
        probe_row is None
        or len(probe_row) != 3
        or str(probe_row[0]) != format(expected_sum, "f")
        or probe_row[1] != 2
        or probe_row[2] != 0
    ):
        raise ProviderError("PostgreSQL product SQL observation failed", "integrity_failure")
    return ProductSqlSumSemantics(
        input_physical_type=decimal_column.physical_type,
        accumulator_physical_type="INTERNAL",
        result_physical_type="NUMERIC",
        overflow_behavior="promote",
        null_input_behavior="exclude",
        empty_group_behavior="no_row",
    )


def _pinned_image_digest() -> str:
    prefix = "@sha256:"
    if prefix not in POSTGRESQL_WAREHOUSE_IMAGE:
        raise ValueError("PostgreSQL warehouse image is not digest-pinned")
    value = POSTGRESQL_WAREHOUSE_IMAGE.rsplit(prefix, 1)[1]
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("PostgreSQL warehouse image digest is invalid")
    return value


def _provider_error(error: psycopg.Error) -> ProviderError:
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
    return ProviderError("PostgreSQL product SQL observation failed", classification)
