from __future__ import annotations

import math
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol, cast

import psycopg
from pillarmesh_dbt_adapter import DbtDecimalMagnitudeCheck
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_provider_sdk.errors import ProviderErrorClassification
from pillarmesh_runtime import (
    AnswerQueryColumn,
    AnswerQueryCursor,
    AnswerQueryParameter,
    AnswerQueryReference,
    AnswerQueryTimedOut,
    ReadOnlyAnswerQuery,
)
from pillarmesh_runtime.answer_models import AnswerQueryValue
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from psycopg import sql
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .product_materialization import (
    _postgresql_decimal_magnitude_checks,
    _postgresql_product_generation_commit_reference,
)

_POSTGRESQL_TYPES: dict[int, Literal["boolean", "decimal", "integer", "string", "timestamp"]] = {
    16: "boolean",
    20: "integer",
    21: "integer",
    23: "integer",
    700: "decimal",
    701: "decimal",
    1700: "decimal",
    18: "string",
    19: "string",
    25: "string",
    1042: "string",
    1043: "string",
    2950: "string",
    1114: "timestamp",
    1184: "timestamp",
}


_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_IDENTIFIER_CONTENT = r"[a-z][a-z0-9_]{0,62}"
_IDENTIFIER = rf'"{_IDENTIFIER_CONTENT}"'
_SOURCE_COLUMN = rf'"source"\.{_IDENTIFIER}'
_PROJECTION = (
    rf"(?:{_SOURCE_COLUMN} AS {_IDENTIFIER}|"
    rf"(?:AVG|COUNT|MAX|MIN|SUM)\({_SOURCE_COLUMN}\) AS {_IDENTIFIER})"
)
_PREDICATE = rf"\({_SOURCE_COLUMN} (?:=|>=|<=|<>|>|<) %s\)"
_ORDER = rf"{_IDENTIFIER} (?:ASC|DESC)"
_RESTRICTED_QUERY = re.compile(
    rf"^SELECT {_PROJECTION}(?:, {_PROJECTION})* "
    rf'FROM "(?P<namespace>{_IDENTIFIER_CONTENT})"\.'
    rf'"(?P<relation>{_IDENTIFIER_CONTENT})" AS "source"'
    rf"(?: WHERE {_PREDICATE}(?: AND {_PREDICATE})*)?"
    rf"(?: GROUP BY {_SOURCE_COLUMN}(?:, {_SOURCE_COLUMN})*)? "
    rf"HAVING COUNT\(DISTINCT {_SOURCE_COLUMN}\) >= %s"
    rf"(?: ORDER BY {_ORDER}(?:, {_ORDER})*)? LIMIT [1-9][0-9]*$"
)


class PostgreSQLAnswerGenerationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    product_ref: AnswerQueryReference
    generation: int = Field(ge=1)
    consumption_object_ref: AnswerQueryReference
    materialization_receipt_ref: AnswerQueryReference
    receipt_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_commit_reference: str = Field(pattern=_DIGEST_PATTERN)
    namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    retained_until: datetime
    output_magnitude_checks: tuple[DbtDecimalMagnitudeCheck, ...] = ()

    @field_validator("retained_until")
    @classmethod
    def retained_until_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("generation retention must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def receipt_matches_generation(self) -> PostgreSQLAnswerGenerationBinding:
        if self.materialization_receipt_ref.version != self.generation:
            raise ValueError("materialization receipt must match the generation")
        checked_columns = tuple(check.column_name for check in self.output_magnitude_checks)
        if len(checked_columns) != len(set(checked_columns)):
            raise ValueError("generation magnitude checks must be unique")
        return self


class PostgreSQLAnswerQuerySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    principal_class: Literal["answer_runtime"] = "answer_runtime"
    dsn: SecretStr


class PostgreSQLAnswerQuerySettingsAuthority(Protocol):
    def resolve_answer_runtime(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> PostgreSQLAnswerQuerySettings: ...


class PostgreSQLAnswerGenerationBindingAuthority(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        product_ref: AnswerQueryReference,
        generation: int,
        consumption_object_ref: AnswerQueryReference,
    ) -> PostgreSQLAnswerGenerationBinding | None: ...


class _PostgreSQLQueryCursor(Protocol):
    itersize: int

    @property
    def description(self) -> object: ...

    def execute(self, statement: object, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def close(self) -> None: ...


class _PostgreSQLQueryConnection(Protocol):
    def execute(
        self, statement: object, params: tuple[object, ...] = ()
    ) -> _PostgreSQLQueryCursor: ...

    def cursor(self, name: str) -> _PostgreSQLQueryCursor: ...

    def cancel(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _PostgreSQLQueryConnection]


class _PostgreSQLAnswerCursor:
    suppressed_group_count = 0

    def __init__(
        self,
        *,
        connection: _PostgreSQLQueryConnection,
        cursor: _PostgreSQLQueryCursor,
        columns: tuple[AnswerQueryColumn, ...],
    ) -> None:
        self._connection = connection
        self._cursor = cursor
        self.columns = columns
        self._closed = False

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None:
        try:
            row = self._cursor.fetchone()
        except psycopg.errors.QueryCanceled:
            raise AnswerQueryTimedOut("PostgreSQL answer query timed out") from None
        except psycopg.Error as error:
            raise _postgresql_provider_error(error) from None
        except Exception:
            raise ProviderError(
                "PostgreSQL answer query returned invalid rows", "invalid_provider_response"
            ) from None
        if row is None:
            return None
        if not isinstance(row, tuple) or len(row) != len(self.columns):
            raise ProviderError(
                "PostgreSQL answer query returned invalid rows", "invalid_provider_response"
            )
        return tuple(
            _typed_value(column.value_type, value)
            for column, value in zip(self.columns, row, strict=True)
        )

    def cancel(self) -> None:
        with suppress(Exception):
            self._connection.cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._cursor.close()
        with suppress(Exception):
            self._connection.rollback()
        with suppress(Exception):
            self._connection.close()


class PostgreSQLAnswerQueryProvider:
    engine_kind: Literal["postgresql"] = "postgresql"

    def __init__(
        self,
        *,
        settings: PostgreSQLAnswerQuerySettings,
        generation_authority: PostgreSQLAnswerGenerationBindingAuthority,
        connect: _Connect | None = None,
        fetch_size: int = 128,
    ) -> None:
        if fetch_size < 1 or fetch_size > 10_000:
            raise ValueError("PostgreSQL answer query fetch size is out of range")
        self._settings = PostgreSQLAnswerQuerySettings.model_validate(
            settings.model_dump(mode="python"), strict=True
        )
        self._generation_authority = generation_authority
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._fetch_size = fetch_size

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        _validate_query(request, expected_engine="postgresql")
        if request.statement.count("%s") != len(request.parameters):
            raise ProviderError(
                "PostgreSQL answer query parameters do not match the statement",
                "statement_rejected",
            )
        parameters = _bound_parameters(request.parameters)
        generation_binding = _resolve_generation_binding(self._generation_authority, request)
        _require_bound_source_relation(request.statement, generation_binding)
        connection: _PostgreSQLQueryConnection | None = None
        cursor: _PostgreSQLQueryCursor | None = None
        try:
            connection = self._connect(self._settings.dsn.get_secret_value())
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            _require_read_only(connection)
            _require_direct_runtime_session(connection)
            _require_answer_runtime_role(connection)
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(request.statement_timeout_seconds * 1000),),
            )
            _require_generation_relation(connection, generation_binding)
            cursor = connection.cursor(name="pillarmesh_answer_query")
            cursor.itersize = self._fetch_size
            cursor.execute(request.statement, parameters)
            columns = _columns(cursor.description)
            return _PostgreSQLAnswerCursor(
                connection=connection,
                cursor=cursor,
                columns=columns,
            )
        except psycopg.errors.QueryCanceled:
            _close_failed(connection, cursor)
            raise AnswerQueryTimedOut("PostgreSQL answer query timed out") from None
        except psycopg.Error as error:
            _close_failed(connection, cursor)
            raise _postgresql_provider_error(error) from None
        except ProviderError:
            _close_failed(connection, cursor)
            raise
        except (OSError, TimeoutError):
            _close_failed(connection, cursor)
            raise ProviderError(
                "PostgreSQL answer query transport failed", "transient_transport"
            ) from None
        except Exception:
            _close_failed(connection, cursor)
            raise ProviderError(
                "PostgreSQL answer query failed", "invalid_provider_response"
            ) from None


def compose_postgresql_answer_query_provider(
    *,
    binding: WarehouseBinding,
    settings_authority: PostgreSQLAnswerQuerySettingsAuthority,
    generation_authority: PostgreSQLAnswerGenerationBindingAuthority,
    connect: _Connect | None = None,
    fetch_size: int = 128,
) -> PostgreSQLAnswerQueryProvider:
    if (
        binding.engine_kind is not EngineKind.POSTGRESQL
        or binding.lifecycle_state is not WarehouseBindingState.READY
    ):
        raise ProviderError(
            "PostgreSQL answer query binding is not authorized", "authorization_denied"
        )
    try:
        settings = settings_authority.resolve_answer_runtime(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
        )
    except ProviderError:
        raise
    except Exception:
        raise ProviderError(
            "PostgreSQL answer runtime credential resolution failed",
            "authorization_denied",
        ) from None
    return PostgreSQLAnswerQueryProvider(
        settings=settings,
        generation_authority=generation_authority,
        connect=connect,
        fetch_size=fetch_size,
    )


def _validate_query(
    request: ReadOnlyAnswerQuery,
    *,
    expected_engine: Literal["postgresql", "clickhouse"],
) -> None:
    if (
        request.engine_kind != expected_engine
        or request.principal_class != "answer_runtime"
        or request.read_only is not True
        or not request.statement.startswith("SELECT ")
        or ";" in request.statement
    ):
        raise ProviderError(
            "answer query is not a read-only compiled statement", "statement_rejected"
        )
    expected_names = tuple(f"p{index}" for index in range(len(request.parameters)))
    if tuple(parameter.name for parameter in request.parameters) != expected_names:
        raise ProviderError("answer query parameters are not canonical", "statement_rejected")


def _resolve_generation_binding(
    authority: PostgreSQLAnswerGenerationBindingAuthority,
    request: ReadOnlyAnswerQuery,
) -> PostgreSQLAnswerGenerationBinding:
    if len(request.product_generation_refs) != 1 or len(request.consumption_object_refs) != 1:
        raise ProviderError(
            "PostgreSQL answer query requires one exact generation binding",
            "authorization_denied",
        )
    generation_ref = request.product_generation_refs[0]
    consumption_ref = request.consumption_object_refs[0]
    try:
        candidate = authority.resolve(
            tenant_id=request.tenant_id,
            product_ref=generation_ref.product_ref,
            generation=generation_ref.generation,
            consumption_object_ref=consumption_ref,
        )
        if candidate is not None:
            if not set(vars(candidate)).issubset(type(candidate).model_fields):
                raise ValueError("generation binding contains undeclared fields")
            for check in candidate.output_magnitude_checks:
                if not set(vars(check)).issubset(type(check).model_fields):
                    raise ValueError("generation magnitude check contains undeclared fields")
        binding = (
            None
            if candidate is None
            else PostgreSQLAnswerGenerationBinding.model_validate(
                candidate.model_dump(mode="python"), strict=True
            )
        )
    except ProviderError:
        raise
    except (AttributeError, TypeError, ValidationError, ValueError):
        raise ProviderError(
            "PostgreSQL answer query generation authority is invalid",
            "integrity_failure",
        ) from None
    except Exception:
        raise ProviderError(
            "PostgreSQL answer query generation authority failed",
            "transient_unavailable",
        ) from None
    if (
        binding is None
        or binding.tenant_id != request.tenant_id
        or binding.product_ref != generation_ref.product_ref
        or binding.generation != generation_ref.generation
        or binding.consumption_object_ref != consumption_ref
    ):
        raise ProviderError(
            "PostgreSQL answer query generation authority is unavailable",
            "authorization_denied",
        )
    return binding


def _require_bound_source_relation(
    statement: str,
    binding: PostgreSQLAnswerGenerationBinding,
) -> None:
    match = _RESTRICTED_QUERY.fullmatch(statement)
    if match is None:
        raise ProviderError(
            "PostgreSQL answer query is not a restricted compiler statement",
            "statement_rejected",
        )
    if (
        match.group("namespace") != binding.namespace
        or match.group("relation") != binding.relation_name
    ):
        raise ProviderError(
            "PostgreSQL answer query does not name its authorized generation",
            "authorization_denied",
        )


def _require_read_only(connection: _PostgreSQLQueryConnection) -> None:
    cursor = connection.execute("SHOW transaction_read_only")
    try:
        if cursor.fetchone() != ("on",):
            raise ProviderError(
                "PostgreSQL answer query requires read-only execution",
                "authorization_denied",
            )
    finally:
        with suppress(Exception):
            cursor.close()


def _require_answer_runtime_role(connection: _PostgreSQLQueryConnection) -> None:
    cursor = connection.execute(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER"
    )
    try:
        row = cursor.fetchone()
    finally:
        with suppress(Exception):
            cursor.close()
    if (
        row is None
        or len(row) != 6
        or any(type(value) is not bool for value in row)
        or row != (True, False, False, False, False, False)
    ):
        raise ProviderError(
            "PostgreSQL answer query requires a least-privilege runtime role",
            "authorization_denied",
        )


def _require_direct_runtime_session(connection: _PostgreSQLQueryConnection) -> None:
    cursor = connection.execute("SELECT SESSION_USER, CURRENT_USER")
    try:
        row = cursor.fetchone()
    finally:
        with suppress(Exception):
            cursor.close()
    if (
        row is None
        or len(row) != 2
        or not all(isinstance(value, str) and value for value in row)
        or row[0] != row[1]
    ):
        raise ProviderError(
            "PostgreSQL answer query requires a direct runtime session",
            "authorization_denied",
        )


def _require_generation_relation(
    connection: _PostgreSQLQueryConnection,
    binding: PostgreSQLAnswerGenerationBinding,
) -> None:
    lock_cursor = connection.execute(
        sql.SQL("LOCK TABLE {}.{} IN ACCESS SHARE MODE").format(
            sql.Identifier(binding.namespace),
            sql.Identifier(binding.relation_name),
        )
    )
    with suppress(Exception):
        lock_cursor.close()
    cursor = connection.execute(
        "SELECT c.oid::bigint, c.relfilenode::bigint, c.relkind, c.relpersistence, "
        "c.relrowsecurity, c.relforcerowsecurity, owner.rolcanlogin, owner.rolsuper, "
        "CURRENT_TIMESTAMP, EXISTS ("
        "SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.rolcanlogin "
        "AND NOT role.rolsuper AND (pg_has_role(role.oid, c.relowner, 'MEMBER') "
        "OR has_table_privilege(role.oid, c.oid, 'INSERT') "
        "OR has_table_privilege(role.oid, c.oid, 'UPDATE') "
        "OR has_table_privilege(role.oid, c.oid, 'DELETE') "
        "OR has_table_privilege(role.oid, c.oid, 'TRUNCATE'))) "
        "FROM pg_catalog.pg_class AS c "
        "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = c.relnamespace "
        "JOIN pg_catalog.pg_roles AS owner ON owner.oid = c.relowner "
        "WHERE namespace.nspname = %s AND c.relname = %s",
        (binding.namespace, binding.relation_name),
    )
    try:
        row = cursor.fetchone()
    finally:
        with suppress(Exception):
            cursor.close()
    if row is None or len(row) != 10:
        raise ProviderError("PostgreSQL answer generation relation is invalid", "integrity_failure")
    (
        relation_oid,
        relation_relfilenode,
        relation_kind,
        relation_persistence,
        row_security,
        force_row_security,
        owner_can_login,
        owner_is_superuser,
        server_time,
        writable_login_exists,
    ) = row
    if (
        type(relation_oid) is not int
        or type(relation_relfilenode) is not int
        or relation_kind != "r"
        or relation_persistence != "p"
        or type(row_security) is not bool
        or row_security
        or type(force_row_security) is not bool
        or force_row_security
        or type(owner_can_login) is not bool
        or owner_can_login
        or type(owner_is_superuser) is not bool
        or owner_is_superuser
        or not isinstance(server_time, datetime)
        or server_time.tzinfo is None
        or server_time.utcoffset() is None
        or type(writable_login_exists) is not bool
        or writable_login_exists
    ):
        raise ProviderError("PostgreSQL answer generation relation is invalid", "integrity_failure")
    magnitude_checks: tuple[tuple[DbtDecimalMagnitudeCheck, int], ...] = ()
    if binding.output_magnitude_checks:
        magnitude_checks = _postgresql_decimal_magnitude_checks(
            connection,
            target_schema=binding.namespace,
            model_name=binding.relation_name,
            checks=binding.output_magnitude_checks,
            observed_column_names=_observed_generation_columns(connection, relation_oid),
        )
    expected_commit_reference = _postgresql_product_generation_commit_reference(
        tenant_id=binding.tenant_id,
        product_id=binding.product_ref.artifact_id,
        product_revision=binding.product_ref.version,
        product_generation=binding.generation,
        model_digest=binding.receipt_plan_digest,
        relation_identity=(str(relation_oid), str(relation_relfilenode)),
        magnitude_checks=magnitude_checks,
    )
    if expected_commit_reference != binding.provider_commit_reference:
        raise ProviderError("PostgreSQL answer generation identity changed", "integrity_failure")
    if server_time.astimezone(UTC) >= binding.retained_until:
        raise ProviderError(
            "PostgreSQL answer generation retention expired", "authorization_denied"
        )


def _observed_generation_columns(
    connection: _PostgreSQLQueryConnection,
    relation_oid: int,
) -> set[str]:
    cursor = connection.execute(
        "SELECT array_agg(attribute.attname ORDER BY attribute.attnum) "
        "FROM pg_catalog.pg_attribute AS attribute "
        "WHERE attribute.attrelid = %s AND attribute.attnum > 0 "
        "AND NOT attribute.attisdropped",
        (relation_oid,),
    )
    try:
        row = cursor.fetchone()
    finally:
        with suppress(Exception):
            cursor.close()
    if (
        row is None
        or len(row) != 1
        or not isinstance(row[0], (list, tuple))
        or not row[0]
        or any(not isinstance(column, str) or not column for column in row[0])
        or len(row[0]) != len(set(row[0]))
    ):
        raise ProviderError(
            "PostgreSQL answer generation columns are invalid",
            "invalid_provider_response",
        )
    return set(row[0])


def _bound_parameters(parameters: tuple[AnswerQueryParameter, ...]) -> tuple[object, ...]:
    return tuple(_parameter_value(parameter) for parameter in parameters)


def _parameter_value(parameter: AnswerQueryParameter) -> object:
    value = parameter.value
    if parameter.value_type == "boolean" and type(value) is bool:
        return value
    if parameter.value_type == "integer" and type(value) is int:
        return value
    if parameter.value_type == "decimal" and isinstance(value, Decimal):
        return value
    if parameter.value_type == "string" and isinstance(value, str):
        return value
    if (
        parameter.value_type == "timestamp"
        and isinstance(value, datetime)
        and value.tzinfo is not None
    ):
        return value.astimezone(UTC)
    raise ProviderError("answer query parameter type is invalid", "statement_rejected")


def _columns(
    description: object,
) -> tuple[AnswerQueryColumn, ...]:
    if not isinstance(description, (tuple, list)) or not description:
        raise ProviderError(
            "PostgreSQL answer query returned no schema", "invalid_provider_response"
        )
    columns: list[AnswerQueryColumn] = []
    for item in description:
        name = getattr(item, "name", None)
        type_code = getattr(item, "type_code", None)
        value_type = _POSTGRESQL_TYPES.get(type_code) if isinstance(type_code, int) else None
        if not isinstance(name, str) or not name or value_type is None:
            raise ProviderError(
                "PostgreSQL answer query returned an unsupported schema",
                "invalid_provider_response",
            )
        columns.append(AnswerQueryColumn(name=name, value_type=value_type))
    if len({column.name for column in columns}) != len(columns):
        raise ProviderError(
            "PostgreSQL answer query returned duplicate columns", "invalid_provider_response"
        )
    return tuple(columns)


def _typed_value(value_type: str, value: object) -> AnswerQueryValue:
    if value is None:
        return None
    if value_type == "boolean" and type(value) is bool:
        return value
    if value_type == "integer" and type(value) is int:
        return value
    if value_type == "decimal" and isinstance(value, Decimal) and value.is_finite():
        return value
    if value_type == "decimal" and type(value) is int:
        return Decimal(value)
    if value_type == "decimal" and isinstance(value, float) and math.isfinite(value):
        return Decimal(str(value))
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "timestamp" and isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    raise ProviderError(
        "PostgreSQL answer query returned a value outside its schema",
        "invalid_provider_response",
    )


def _postgresql_provider_error(error: psycopg.Error) -> ProviderError:
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
    return ProviderError("PostgreSQL answer query failed", classification)


def _close_failed(
    connection: _PostgreSQLQueryConnection | None,
    cursor: _PostgreSQLQueryCursor | None,
) -> None:
    if cursor is not None:
        with suppress(Exception):
            cursor.close()
    if connection is not None:
        with suppress(Exception):
            connection.rollback()
        with suppress(Exception):
            connection.close()
