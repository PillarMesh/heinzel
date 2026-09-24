from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol, Self, cast

import psycopg
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import (
    AcquisitionBoundary,
    AcquisitionCeilingExceeded,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    ColumnObservation,
    CompletedAcquisition,
    ProviderObservation,
    SourceObservationRequest,
)
from heinzel_provider_sdk.errors import (
    AcquisitionProviderErrorClassification,
    AcquisitionProviderReasonCode,
)
from psycopg import sql
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .acquisition_settings import (
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceObjectDeclaration,
)
from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)

_CURSOR_VERSION = "postgresql-incremental-v1"


class _Cursor(Protocol):
    def execute(self, query: object, params: object = None) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def __iter__(self) -> Iterator[tuple[object, ...]]: ...

    def close(self) -> None: ...

    def __enter__(self) -> _Cursor: ...

    def __exit__(self, *args: object) -> object: ...


class _Connection(Protocol):
    def execute(self, query: str) -> object: ...

    def cursor(self, name: str | None = None) -> _Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _Connection]
type _PrivateBoundaryWriter = Callable[[str, str, bytes], None]
type _ValueType = Literal["null", "boolean", "integer", "decimal", "string", "timestamp"]
type _Scalar = bool | int | Decimal | str | datetime | None
type _KeyConstraint = Literal["primary_key", "unique"]
type PostgreSQLCursorKey = bool | int | Decimal | str | datetime
type PostgreSQLCursorKeyType = Literal["boolean", "integer", "decimal", "string", "timestamp"]


class _PostgreSQLCursorIntegrityError(ValueError):
    pass


def _cursor_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental cursor bytes are not canonical"
            )
        decoded[key] = value
    return decoded


class PostgreSQLIncrementalCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    updated_at: datetime
    primary_key: PostgreSQLCursorKey

    @field_validator("updated_at")
    @classmethod
    def requires_utc_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PostgreSQL incremental cursor timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("primary_key")
    @classmethod
    def requires_native_non_null_key(cls, value: PostgreSQLCursorKey) -> PostgreSQLCursorKey:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("PostgreSQL cursor key timestamp must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @classmethod
    def from_payload(
        cls,
        payload: bytes,
        *,
        key_value_type: PostgreSQLCursorKeyType,
    ) -> PostgreSQLIncrementalCursor:
        try:
            decoded = json.loads(payload, object_pairs_hook=_cursor_object)
        except (UnicodeError, json.JSONDecodeError, _PostgreSQLCursorIntegrityError) as error:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental cursor bytes are not canonical"
            ) from error
        if not isinstance(decoded, dict) or set(decoded) != {
            "schema_version",
            "updated_at",
            "primary_key",
        }:
            raise ValueError("PostgreSQL incremental cursor payload is invalid")
        raw_key = decoded["primary_key"]
        if key_value_type == "boolean":
            if type(raw_key) is not bool:
                raise ValueError("PostgreSQL cursor key type does not match approved schema")
            native_key: PostgreSQLCursorKey = raw_key
        elif key_value_type == "integer":
            if type(raw_key) is not int:
                raise ValueError("PostgreSQL cursor key type does not match approved schema")
            native_key = raw_key
        elif key_value_type == "decimal":
            if not isinstance(raw_key, str):
                raise ValueError("PostgreSQL cursor key type does not match approved schema")
            native_key = Decimal(raw_key)
        elif key_value_type == "string":
            if not isinstance(raw_key, str):
                raise ValueError("PostgreSQL cursor key type does not match approved schema")
            native_key = raw_key
        else:
            if not isinstance(raw_key, str):
                raise ValueError("PostgreSQL cursor key type does not match approved schema")
            native_key = datetime.fromisoformat(raw_key.replace("Z", "+00:00"))
        raw_updated_at = decoded["updated_at"]
        if not isinstance(raw_updated_at, str):
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental cursor timestamp is invalid"
            )
        decoded["updated_at"] = _decode_cursor_timestamp(raw_updated_at)
        decoded["primary_key"] = native_key
        try:
            cursor = cls.model_validate(decoded)
        except (ValidationError, ValueError, ArithmeticError) as error:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental cursor bytes are not canonical"
            ) from error
        if canonical_bytes(cursor) != payload:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental cursor bytes are not canonical"
            )
        return cursor


class _PostgreSQLObjectCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    logical_object_ref: str
    position: Literal["before_first", "row"]
    updated_at: datetime
    primary_key: PostgreSQLCursorKey | None

    @field_validator("updated_at")
    @classmethod
    def requires_utc_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PostgreSQL object cursor timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def requires_position_shape(self) -> Self:
        if (self.position == "row") != (self.primary_key is not None):
            raise ValueError("PostgreSQL object cursor position is invalid")
        return self

    def as_row_cursor(self) -> PostgreSQLIncrementalCursor | None:
        if self.position == "before_first":
            return None
        return PostgreSQLIncrementalCursor(
            updated_at=self.updated_at,
            primary_key=cast(PostgreSQLCursorKey, self.primary_key),
        )


class _PostgreSQLIncrementalCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    objects: tuple[_PostgreSQLObjectCursor, ...]

    @field_validator("objects")
    @classmethod
    def requires_unique_objects(
        cls, value: tuple[_PostgreSQLObjectCursor, ...]
    ) -> tuple[_PostgreSQLObjectCursor, ...]:
        object_refs = tuple(item.logical_object_ref for item in value)
        if not object_refs or len(object_refs) != len(set(object_refs)):
            raise ValueError("PostgreSQL checkpoint objects must be non-empty and unique")
        return value

    @classmethod
    def from_payload(
        cls,
        payload: bytes,
        *,
        object_refs: tuple[str, ...],
        key_value_types: tuple[PostgreSQLCursorKeyType, ...],
    ) -> _PostgreSQLIncrementalCheckpoint:
        try:
            decoded = json.loads(payload, object_pairs_hook=_cursor_object)
        except (UnicodeError, json.JSONDecodeError, _PostgreSQLCursorIntegrityError) as error:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental checkpoint bytes are not canonical"
            ) from error
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"schema_version", "objects"}
            or not isinstance(decoded["objects"], list)
            or len(decoded["objects"]) != len(object_refs)
            or len(key_value_types) != len(object_refs)
        ):
            raise _PostgreSQLCursorIntegrityError("PostgreSQL incremental checkpoint is invalid")
        native_objects: list[dict[str, object]] = []
        for raw_object, expected_ref, key_value_type in zip(
            decoded["objects"], object_refs, key_value_types, strict=True
        ):
            if (
                not isinstance(raw_object, dict)
                or set(raw_object)
                != {"logical_object_ref", "position", "updated_at", "primary_key"}
                or raw_object["logical_object_ref"] != expected_ref
                or raw_object["position"] not in {"before_first", "row"}
                or not isinstance(raw_object["updated_at"], str)
            ):
                raise _PostgreSQLCursorIntegrityError(
                    "PostgreSQL incremental checkpoint scope is invalid"
                )
            if raw_object["position"] == "before_first":
                if raw_object["primary_key"] is not None:
                    raise _PostgreSQLCursorIntegrityError(
                        "PostgreSQL before-first cursor key must be null"
                    )
                native_key: PostgreSQLCursorKey | None = None
            else:
                native_key = _decode_cursor_key(raw_object["primary_key"], key_value_type)
            native_objects.append(
                {
                    **raw_object,
                    "updated_at": _decode_cursor_timestamp(raw_object["updated_at"]),
                    "primary_key": native_key,
                }
            )
        try:
            checkpoint = cls.model_validate(
                {"schema_version": decoded["schema_version"], "objects": tuple(native_objects)}
            )
        except (ValidationError, ValueError, ArithmeticError) as error:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental checkpoint is invalid"
            ) from error
        if canonical_bytes(checkpoint) != payload:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL incremental checkpoint bytes are not canonical"
            )
        return checkpoint


def _decode_cursor_key(
    raw_key: object,
    key_value_type: PostgreSQLCursorKeyType,
) -> PostgreSQLCursorKey:
    if key_value_type == "boolean":
        if type(raw_key) is not bool:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL cursor key type does not match approved schema"
            )
        return raw_key
    if key_value_type == "integer":
        if type(raw_key) is not int:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL cursor key type does not match approved schema"
            )
        return raw_key
    if key_value_type == "decimal":
        if not isinstance(raw_key, str):
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL cursor key type does not match approved schema"
            )
        try:
            return Decimal(raw_key)
        except ArithmeticError as error:
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL cursor decimal key is invalid"
            ) from error
    if key_value_type == "string":
        if not isinstance(raw_key, str):
            raise _PostgreSQLCursorIntegrityError(
                "PostgreSQL cursor key type does not match approved schema"
            )
        return raw_key
    if not isinstance(raw_key, str):
        raise _PostgreSQLCursorIntegrityError(
            "PostgreSQL cursor key type does not match approved schema"
        )
    return _decode_cursor_timestamp(raw_key)


def _decode_cursor_timestamp(raw_value: str) -> datetime:
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _PostgreSQLCursorIntegrityError("PostgreSQL cursor timestamp is invalid") from error


def _require_driver_cursor_values(
    updated_at: object,
    primary_key: object,
    *,
    key_value_type: PostgreSQLCursorKeyType,
) -> tuple[datetime, PostgreSQLCursorKey]:
    if (
        not isinstance(updated_at, datetime)
        or updated_at.tzinfo is None
        or updated_at.utcoffset() is None
    ):
        raise _provider_error("invalid_provider_response")
    if key_value_type == "timestamp" and isinstance(primary_key, datetime):
        if primary_key.tzinfo is None or primary_key.utcoffset() is None:
            raise _provider_error("invalid_provider_response")
        return updated_at.astimezone(UTC), primary_key.astimezone(UTC)
    valid_native_key = (
        (key_value_type == "boolean" and type(primary_key) is bool)
        or (key_value_type == "integer" and type(primary_key) is int)
        or (key_value_type == "decimal" and isinstance(primary_key, Decimal))
        or (key_value_type == "string" and isinstance(primary_key, str))
    )
    if not valid_native_key:
        raise _provider_error("invalid_provider_response")
    return updated_at.astimezone(UTC), cast(PostgreSQLCursorKey, primary_key)


@dataclass(frozen=True, slots=True)
class _ObservedObject:
    declaration: PostgreSQLSourceObjectDeclaration
    relation_oid: str
    database_name: str
    fields: tuple[AcquisitionField, ...]
    key_constraint: _KeyConstraint
    key_not_null: bool
    key_access_method: str
    key_column_count: int
    least_privilege: bool


@dataclass(frozen=True, slots=True)
class _SnapshotObject:
    schema: AcquisitionObjectSchema
    cursor: _Cursor
    boundary: AcquisitionBoundary
    row_names: tuple[str, ...]
    key_position: int
    updated_at_position: int


@dataclass(frozen=True, slots=True)
class _IncrementalObject:
    schema: AcquisitionObjectSchema
    declaration: PostgreSQLSourceObjectDeclaration
    boundary: AcquisitionBoundary
    lower: _PostgreSQLObjectCursor
    upper: _PostgreSQLObjectCursor


def _provider_error(
    reason_code: AcquisitionProviderReasonCode,
) -> AcquisitionProviderError:
    classification_by_reason: dict[
        AcquisitionProviderReasonCode, AcquisitionProviderErrorClassification
    ] = {
        "ambiguous_outcome": "ambiguous_outcome",
        "authorization_denied": "authorization_denied",
        "integrity_failure": "integrity_failure",
        "invalid_provider_response": "invalid_provider_response",
        "permanent_configuration": "permanent_configuration",
        "provider_unavailable": "transient_unavailable",
        "rate_limited": "throttled",
        "statement_rejected": "statement_rejected",
        "transport_failure": "transient_transport",
    }
    classification = classification_by_reason[reason_code]
    return AcquisitionProviderError(
        provider_kind="postgresql",
        classification=classification,
        reason_code=reason_code,
    )


def _translate_driver_error(error: psycopg.Error) -> AcquisitionProviderError:
    sqlstate = error.sqlstate or ""
    if isinstance(
        error,
        (
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.InvalidAuthorizationSpecification,
            psycopg.errors.InvalidPassword,
        ),
    ) or sqlstate.startswith("28"):
        return _provider_error("authorization_denied")
    if sqlstate == "53300":
        return _provider_error("rate_limited")
    if isinstance(error, psycopg.InterfaceError) or sqlstate.startswith("08"):
        return _provider_error("transport_failure")
    if sqlstate.startswith(("40", "53", "57", "58")) or isinstance(error, psycopg.OperationalError):
        return _provider_error("provider_unavailable")
    if isinstance(error, psycopg.DataError):
        return _provider_error("invalid_provider_response")
    if isinstance(error, psycopg.IntegrityError):
        return _provider_error("integrity_failure")
    if isinstance(error, psycopg.DatabaseError):
        return _provider_error("statement_rejected")
    return _provider_error("provider_unavailable")


def _acquisition_field(row: tuple[object, ...]) -> AcquisitionField:
    if len(row) != 4:
        raise ValueError("PostgreSQL column metadata shape is invalid")
    name, _data_type, udt_name, nullable = row
    if not isinstance(name, str) or not isinstance(udt_name, str) or nullable not in {"YES", "NO"}:
        raise ValueError("PostgreSQL column metadata values are invalid")
    if udt_name in {"int2", "int4", "int8"}:
        value_type: _ValueType = "integer"
    elif udt_name == "numeric":
        value_type = "decimal"
    elif udt_name == "bool":
        value_type = "boolean"
    elif udt_name in {"text", "varchar"}:
        value_type = "string"
    elif udt_name == "timestamptz":
        value_type = "timestamp"
    else:
        raise ValueError("PostgreSQL column type is not admitted")
    return AcquisitionField(name=name, value_type=value_type, nullable=nullable == "YES")


def _require_scalar(value: object, field: AcquisitionField) -> _Scalar:
    if value is None:
        if field.nullable:
            return None
        raise ValueError("PostgreSQL returned null for a non-null field")
    if field.value_type == "boolean" and type(value) is bool:
        return value
    if field.value_type == "integer" and type(value) is int:
        return value
    if field.value_type == "decimal" and isinstance(value, Decimal):
        return value
    if field.value_type == "string" and isinstance(value, str):
        return value
    if field.value_type == "timestamp" and isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PostgreSQL returned a naive timestamp")
        return value.astimezone(UTC)
    raise ValueError("PostgreSQL returned a scalar outside the approved schema")


class PostgreSQLAcquisitionProvider:
    def __init__(
        self,
        settings: PostgreSQLAcquisitionSettings,
        *,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
        clock: Callable[[], datetime] | None = None,
        private_boundary_reference_factory: Callable[[str, str], str],
        private_boundary_writer: _PrivateBoundaryWriter,
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._private_boundary_reference_factory = private_boundary_reference_factory
        self._private_boundary_writer = private_boundary_writer
        self._declarations = {item.logical_object_ref: item for item in settings.objects}
        approved_relations = tuple(
            sorted((item.schema_name, item.table_name) for item in settings.objects)
        )
        self._approved_relation_schemas = tuple(item[0] for item in approved_relations)
        self._approved_relation_names = tuple(item[1] for item in approved_relations)

    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        connection: _Connection | None = None
        try:
            declarations = self._resolve_declarations(request.object_refs)
            connection = self._open_connection()
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            observations: list[AcquisitionObjectObservation] = []
            for declaration in declarations:
                observed = self._inspect_object(connection, declaration)
                self._require_admitted_metadata(observed)
                columns = tuple(
                    ColumnObservation(
                        name=field.name,
                        type_name=field.value_type.upper(),
                        nullable=field.nullable,
                    )
                    for field in observed.fields
                )
                observations.append(
                    AcquisitionObjectObservation(
                        logical_object_ref=declaration.logical_object_ref,
                        provider_observation=ProviderObservation(
                            provider="postgresql",
                            connection_handle=self._settings.connection_handle,
                            object_identity=digest(
                                {
                                    "database": observed.database_name,
                                    "relation_oid": observed.relation_oid,
                                }
                            ),
                            object_kind="base_table",
                            schema_digest=digest(observed.fields),
                            columns=columns,
                            key_name=declaration.key_name,
                            key_type=next(
                                field.value_type.upper()
                                for field in observed.fields
                                if field.name == declaration.key_name
                            ),
                            key_nullable=False,
                            key_constraint=observed.key_constraint,
                            stable_key_order=True,
                            read_only=True,
                            capabilities=("incremental", "reconciliation", "snapshot"),
                            observed_at=self._clock(),
                            snapshot_semantics="snapshot",
                            commit_ledger_object_kind=None,
                            commit_ledger_columns=None,
                            commit_ledger_key_name=None,
                            commit_ledger_key_constraint=None,
                            evidence_safe=True,
                        ),
                    )
                )
            connection.commit()
            connection.close()
            connection = None
            return AcquisitionSourceObservation(
                tenant_id=request.tenant_id,
                source_binding_ref=request.source_binding_ref,
                provider_kind="postgresql",
                object_observations=tuple(observations),
            )
        except AcquisitionProviderError:
            if connection is not None:
                _abort_connection(connection)
            raise
        except psycopg.Error as error:
            if connection is not None:
                _abort_connection(connection)
            raise _translate_driver_error(error) from None
        except (TypeError, ValueError, ValidationError):
            if connection is not None:
                _abort_connection(connection)
            raise _provider_error("permanent_configuration") from None
        except Exception:
            if connection is not None:
                _abort_connection(connection)
            raise _provider_error("integrity_failure") from None

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _PostgreSQLSnapshotSession:
        if intent.acquisition_mode == "incremental":
            return self._open_incremental_checkpoint(intent, schemas, private_cursor)
        reconciliation = intent.acquisition_mode == "reconciliation"
        valid_snapshot = (
            intent.acquisition_mode == "snapshot"
            and intent.prior_checkpoint_revision == 0
            and private_cursor is None
        )
        valid_reconciliation = (
            reconciliation
            and intent.prior_checkpoint_revision > 0
            and private_cursor is not None
            and intent.prior_checkpoint_digest == hashlib.sha256(private_cursor).hexdigest()
        )
        if not valid_snapshot and not valid_reconciliation:
            raise _provider_error("permanent_configuration")
        connection: _Connection | None = None
        opened_cursors: list[_Cursor] = []
        try:
            declarations = self._resolve_declarations(intent.object_refs)
            if tuple(schema.logical_object_ref for schema in schemas) != intent.object_refs:
                raise ValueError("PostgreSQL schemas do not match intent objects")
            if reconciliation:
                key_value_types: list[PostgreSQLCursorKeyType] = []
                for declaration, object_schema in zip(declarations, schemas, strict=True):
                    self._require_schema_declaration(declaration, object_schema)
                    key_field = next(
                        field
                        for field in object_schema.fields
                        if field.name == declaration.key_name
                    )
                    if key_field.value_type not in {
                        "boolean",
                        "integer",
                        "decimal",
                        "string",
                        "timestamp",
                    }:
                        raise ValueError("PostgreSQL cursor key type is not admitted")
                    key_value_types.append(cast(PostgreSQLCursorKeyType, key_field.value_type))
                _PostgreSQLIncrementalCheckpoint.from_payload(
                    cast(bytes, private_cursor),
                    object_refs=intent.object_refs,
                    key_value_types=tuple(key_value_types),
                )
            connection = self._open_connection()
            opened_at = self._clock()
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_current_snapshot()::text")
                snapshot_row = cursor.fetchone()
                cursor.execute("SELECT transaction_timestamp()")
                snapshot_time_row = cursor.fetchone()
            if (
                snapshot_row is None
                or len(snapshot_row) != 1
                or not isinstance(snapshot_row[0], str)
                or snapshot_time_row is None
                or len(snapshot_time_row) != 1
                or not isinstance(snapshot_time_row[0], datetime)
            ):
                raise ValueError("PostgreSQL snapshot identity is invalid")
            snapshot_identity = snapshot_row[0]
            lag_bound = (
                snapshot_time_row[0].astimezone(UTC) - self._settings.max_write_transaction_duration
            )
            prepared: list[
                tuple[
                    AcquisitionObjectSchema,
                    PostgreSQLSourceObjectDeclaration,
                    AcquisitionBoundary,
                    _PostgreSQLObjectCursor | None,
                ]
            ] = []
            snapshot_seed_cursors: list[_PostgreSQLObjectCursor] = []
            total_count = 0
            for declaration, object_schema in zip(declarations, schemas, strict=True):
                self._require_schema_declaration(declaration, object_schema)
                key_field = next(
                    field for field in object_schema.fields if field.name == declaration.key_name
                )
                if key_field.value_type not in {
                    "boolean",
                    "integer",
                    "decimal",
                    "string",
                    "timestamp",
                }:
                    raise ValueError("PostgreSQL cursor key type is not admitted")
                key_value_type = cast(PostgreSQLCursorKeyType, key_field.value_type)
                observed = self._inspect_object(connection, declaration)
                self._require_admitted_metadata(observed)
                if observed.fields != object_schema.fields:
                    raise ValueError("PostgreSQL approved schema drifted")
                if not reconciliation:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            sql.SQL(
                                "/* snapshot_cursor_upper */ SELECT {}, {} FROM {} WHERE {} <= %s "
                                "ORDER BY {} DESC, {} DESC LIMIT 1"
                            ).format(
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.key_name),
                                sql.Identifier(declaration.schema_name, declaration.table_name),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.key_name),
                            ),
                            (lag_bound,),
                        )
                        seed_cursor_row = cursor.fetchone()
                    if seed_cursor_row is None:
                        seed_cursor = _PostgreSQLObjectCursor(
                            logical_object_ref=declaration.logical_object_ref,
                            position="before_first",
                            updated_at=lag_bound,
                            primary_key=None,
                        )
                        key_min, key_max, row_count = self._capture_before_first_bounds(
                            connection,
                            declaration,
                            lag_bound,
                        )
                    else:
                        if len(seed_cursor_row) != 2:
                            raise ValueError("PostgreSQL snapshot cursor is invalid")
                        updated_at, primary_key = _require_driver_cursor_values(
                            seed_cursor_row[0],
                            seed_cursor_row[1],
                            key_value_type=key_value_type,
                        )
                        seed_cursor = _PostgreSQLObjectCursor(
                            logical_object_ref=declaration.logical_object_ref,
                            position="row",
                            updated_at=updated_at,
                            primary_key=primary_key,
                        )
                        key_min, key_max, row_count = self._capture_snapshot_bounds(
                            connection,
                            declaration,
                            cast(PostgreSQLIncrementalCursor, seed_cursor.as_row_cursor()),
                        )
                    snapshot_seed_cursors.append(seed_cursor)
                else:
                    seed_cursor = None
                    key_min, key_max, row_count = self._capture_bounds(connection, declaration)
                total_count += row_count
                if total_count > intent.record_ceiling:
                    raise AcquisitionCeilingExceeded(
                        logical_object_ref=declaration.logical_object_ref,
                        limit_kind="records",
                        ceiling=intent.record_ceiling,
                    )
                snapshot_identity_digest = digest({"snapshot_identity": snapshot_identity})
                key_range_digest = digest({"key_min": key_min, "key_max": key_max})
                if seed_cursor is None:
                    row_predicate: dict[str, object] | None = None
                elif seed_cursor.position == "row":
                    row_predicate = {
                        "upper_columns": (
                            declaration.source_updated_at_field,
                            declaration.key_name,
                        ),
                        "upper_operator": "<=",
                    }
                else:
                    row_predicate = {
                        "upper_columns": (declaration.source_updated_at_field,),
                        "upper_operator": "<=",
                    }
                query_shape_digest = digest(
                    {
                        "columns": declaration.field_names,
                        "order_by": declaration.key_name,
                        "cursor_position": (None if seed_cursor is None else seed_cursor.position),
                        "row_predicate": row_predicate,
                    }
                )
                upper_cursor_digest = (
                    digest(seed_cursor)
                    if seed_cursor is not None
                    else digest(
                        {
                            "snapshot_identity_digest": snapshot_identity_digest,
                            "key_range_digest": key_range_digest,
                            "query_shape_digest": query_shape_digest,
                        }
                    )
                )
                private_reference = self._private_boundary_reference_factory(
                    intent.tenant_id, declaration.logical_object_ref
                )
                self._private_boundary_writer(
                    intent.tenant_id,
                    private_reference,
                    canonical_bytes(
                        {
                            "database_digest": digest(observed.database_name),
                            "relation_oid": observed.relation_oid,
                            "snapshot_identity": snapshot_identity,
                            "key_min": key_min,
                            "key_max": key_max,
                            "row_count": row_count,
                        }
                    ),
                )
                prepared.append(
                    (
                        object_schema,
                        declaration,
                        AcquisitionBoundary(
                            logical_object_ref=declaration.logical_object_ref,
                            acquisition_mode=intent.acquisition_mode,
                            schema_digest=object_schema.schema_digest,
                            lower_cursor_digest=(
                                intent.prior_checkpoint_digest if reconciliation else None
                            ),
                            upper_cursor_digest=upper_cursor_digest,
                            query_shape_digest=query_shape_digest,
                            snapshot_identity_digest=snapshot_identity_digest,
                            key_range_digest=key_range_digest,
                            private_boundary_ref=private_reference,
                            record_count=row_count,
                            opened_at=opened_at,
                            closed_at=opened_at,
                        ),
                        seed_cursor,
                    )
                )
            candidate_cursor_payload = (
                cast(bytes, private_cursor)
                if reconciliation
                else canonical_bytes(
                    _PostgreSQLIncrementalCheckpoint(objects=tuple(snapshot_seed_cursors))
                )
            )
            checkpoint_digest = hashlib.sha256(candidate_cursor_payload).hexdigest()
            prepared = [
                (
                    object_schema,
                    declaration,
                    boundary.model_copy(update={"upper_cursor_digest": checkpoint_digest}),
                    seed_cursor,
                )
                for object_schema, declaration, boundary, seed_cursor in prepared
            ]
            snapshot_objects: list[_SnapshotObject] = []
            for position, (object_schema, declaration, boundary, seed_cursor) in enumerate(
                prepared
            ):
                cursor_kind = "reconciliation" if reconciliation else "snapshot"
                row_cursor = connection.cursor(name=f"heinzel_{cursor_kind}_{position:04d}")
                opened_cursors.append(row_cursor)
                if seed_cursor is None:
                    row_cursor.execute(
                        sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                            sql.SQL(", ").join(
                                sql.Identifier(field_name) for field_name in declaration.field_names
                            ),
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.key_name),
                        )
                    )
                elif seed_cursor.position == "row":
                    row_cursor.execute(
                        sql.SQL(
                            "/* snapshot_rows */ SELECT {} FROM {} "
                            "WHERE ({}, {}) <= (%s, %s) ORDER BY {}"
                        ).format(
                            sql.SQL(", ").join(
                                sql.Identifier(field_name) for field_name in declaration.field_names
                            ),
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                            sql.Identifier(declaration.key_name),
                        ),
                        (
                            seed_cursor.updated_at,
                            cast(PostgreSQLCursorKey, seed_cursor.primary_key),
                        ),
                    )
                else:
                    row_cursor.execute(
                        sql.SQL(
                            "/* snapshot_rows_before_first */ SELECT {} FROM {} "
                            "WHERE {} <= %s ORDER BY {}"
                        ).format(
                            sql.SQL(", ").join(
                                sql.Identifier(field_name) for field_name in declaration.field_names
                            ),
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                        ),
                        (seed_cursor.updated_at,),
                    )
                snapshot_objects.append(
                    _SnapshotObject(
                        schema=object_schema,
                        cursor=row_cursor,
                        boundary=boundary,
                        row_names=declaration.field_names,
                        key_position=declaration.field_names.index(declaration.key_name),
                        updated_at_position=declaration.field_names.index(
                            declaration.source_updated_at_field
                        ),
                    )
                )
            return _PostgreSQLSnapshotSession(
                connection=connection,
                objects=tuple(snapshot_objects),
                clock=self._clock,
                cursor_version=_CURSOR_VERSION,
                candidate_cursor_payload=candidate_cursor_payload,
            )
        except _PostgreSQLCursorIntegrityError:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("integrity_failure") from None
        except AcquisitionCeilingExceeded:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise
        except AcquisitionProviderError:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise
        except psycopg.Error as error:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _translate_driver_error(error) from None
        except (TypeError, ValueError, ValidationError):
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("permanent_configuration") from None
        except Exception:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("integrity_failure") from None

    def _open_incremental_checkpoint(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _PostgreSQLSnapshotSession:
        if (
            intent.prior_checkpoint_revision < 1
            or private_cursor is None
            or intent.prior_checkpoint_digest != hashlib.sha256(private_cursor).hexdigest()
            or tuple(schema.logical_object_ref for schema in schemas) != intent.object_refs
        ):
            raise _provider_error("permanent_configuration")
        connection: _Connection | None = None
        opened_cursors: list[_Cursor] = []
        try:
            declarations = self._resolve_declarations(intent.object_refs)
            key_value_types: list[PostgreSQLCursorKeyType] = []
            for declaration, object_schema in zip(declarations, schemas, strict=True):
                self._require_schema_declaration(declaration, object_schema)
                key_field = next(
                    field for field in object_schema.fields if field.name == declaration.key_name
                )
                if key_field.value_type not in {
                    "boolean",
                    "integer",
                    "decimal",
                    "string",
                    "timestamp",
                }:
                    raise ValueError("PostgreSQL cursor key type is not admitted")
                key_value_types.append(cast(PostgreSQLCursorKeyType, key_field.value_type))
            lower_checkpoint = _PostgreSQLIncrementalCheckpoint.from_payload(
                private_cursor,
                object_refs=intent.object_refs,
                key_value_types=tuple(key_value_types),
            )
            connection = self._open_connection()
            opened_at = self._clock()
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_current_snapshot()::text")
                snapshot_row = cursor.fetchone()
                cursor.execute("SELECT transaction_timestamp()")
                snapshot_time_row = cursor.fetchone()
            oldest_xact_row: tuple[object, ...] | None = None
            connection.execute("SAVEPOINT heinzel_xact_horizon")
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT min(xact_start) FROM pg_catalog.pg_stat_activity "
                        "WHERE pid <> pg_backend_pid() AND xact_start IS NOT NULL"
                    )
                    oldest_xact_row = cursor.fetchone()
            except psycopg.errors.InsufficientPrivilege:
                connection.execute("ROLLBACK TO SAVEPOINT heinzel_xact_horizon")
            finally:
                connection.execute("RELEASE SAVEPOINT heinzel_xact_horizon")
            if (
                snapshot_row is None
                or len(snapshot_row) != 1
                or not isinstance(snapshot_row[0], str)
                or snapshot_time_row is None
                or len(snapshot_time_row) != 1
                or not isinstance(snapshot_time_row[0], datetime)
            ):
                raise ValueError("PostgreSQL incremental snapshot metadata is invalid")
            snapshot_time = snapshot_time_row[0].astimezone(UTC)
            lag_bound = snapshot_time - self._settings.max_write_transaction_duration
            upper_bound_is_exclusive = False
            oldest_xact_start = None
            if oldest_xact_row is not None and len(oldest_xact_row) == 1:
                candidate_oldest = oldest_xact_row[0]
                if isinstance(candidate_oldest, datetime):
                    oldest_xact_start = candidate_oldest.astimezone(UTC)
                    if oldest_xact_start <= lag_bound:
                        lag_bound = oldest_xact_start
                        upper_bound_is_exclusive = True
                elif candidate_oldest is not None:
                    raise ValueError("PostgreSQL transaction horizon is invalid")

            prepared: list[_IncrementalObject] = []
            candidate_objects: list[_PostgreSQLObjectCursor] = []
            total_count = 0
            for declaration, object_schema, lower, key_value_type in zip(
                declarations,
                schemas,
                lower_checkpoint.objects,
                key_value_types,
                strict=True,
            ):
                observed = self._inspect_object(connection, declaration)
                self._require_admitted_metadata(observed)
                if observed.fields != object_schema.fields:
                    raise ValueError("PostgreSQL approved schema drifted")
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            "/* incremental_upper */ SELECT {}, {} FROM {} WHERE {} {} %s "
                            "ORDER BY {} DESC, {} DESC LIMIT 1"
                        ).format(
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.SQL("<" if upper_bound_is_exclusive else "<="),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                        ),
                        (lag_bound,),
                    )
                    upper_row = cursor.fetchone()
                if upper_row is None:
                    upper = (
                        lower
                        if lower.position == "row"
                        else _PostgreSQLObjectCursor(
                            logical_object_ref=declaration.logical_object_ref,
                            position="before_first",
                            updated_at=lag_bound,
                            primary_key=None,
                        )
                    )
                else:
                    if len(upper_row) != 2:
                        raise ValueError("PostgreSQL incremental upper cursor is invalid")
                    updated_at, primary_key = _require_driver_cursor_values(
                        upper_row[0],
                        upper_row[1],
                        key_value_type=key_value_type,
                    )
                    upper = _PostgreSQLObjectCursor(
                        logical_object_ref=declaration.logical_object_ref,
                        position="row",
                        updated_at=updated_at,
                        primary_key=primary_key,
                    )
                    lower_row = lower.as_row_cursor()
                    upper_row_cursor = cast(PostgreSQLIncrementalCursor, upper.as_row_cursor())
                    if lower_row is not None and (
                        upper_row_cursor.updated_at,
                        upper_row_cursor.primary_key,
                    ) < (lower_row.updated_at, lower_row.primary_key):
                        upper = lower

                with connection.cursor() as cursor:
                    if lower.position == "row":
                        lower_row = cast(PostgreSQLIncrementalCursor, lower.as_row_cursor())
                        upper_row_cursor = cast(PostgreSQLIncrementalCursor, upper.as_row_cursor())
                        cursor.execute(
                            sql.SQL(
                                "/* incremental_count */ SELECT count(*) FROM {} "
                                "WHERE ({}, {}) > (%s, %s) AND ({}, {}) <= (%s, %s)"
                            ).format(
                                sql.Identifier(declaration.schema_name, declaration.table_name),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.key_name),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.key_name),
                            ),
                            (
                                lower_row.updated_at,
                                lower_row.primary_key,
                                upper_row_cursor.updated_at,
                                upper_row_cursor.primary_key,
                            ),
                        )
                    elif upper.position == "row":
                        upper_row_cursor = cast(PostgreSQLIncrementalCursor, upper.as_row_cursor())
                        cursor.execute(
                            sql.SQL(
                                "/* incremental_count_before_first */ SELECT count(*) FROM {} "
                                "WHERE ({}, {}) <= (%s, %s)"
                            ).format(
                                sql.Identifier(declaration.schema_name, declaration.table_name),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.Identifier(declaration.key_name),
                            ),
                            (upper_row_cursor.updated_at, upper_row_cursor.primary_key),
                        )
                    else:
                        cursor.execute(
                            sql.SQL(
                                "/* incremental_count_before_first */ SELECT count(*) FROM {} "
                                "WHERE {} {} %s"
                            ).format(
                                sql.Identifier(declaration.schema_name, declaration.table_name),
                                sql.Identifier(declaration.source_updated_at_field),
                                sql.SQL("<" if upper_bound_is_exclusive else "<="),
                            ),
                            (upper.updated_at,),
                        )
                    count_row = cursor.fetchone()
                if count_row is None or len(count_row) != 1 or type(count_row[0]) is not int:
                    raise ValueError("PostgreSQL incremental count is invalid")
                record_count = count_row[0]
                total_count += record_count
                if total_count > intent.record_ceiling:
                    raise AcquisitionCeilingExceeded(
                        logical_object_ref=declaration.logical_object_ref,
                        limit_kind="records",
                        ceiling=intent.record_ceiling,
                    )
                row_predicate: dict[str, object]
                if lower.position == "row":
                    row_predicate = {
                        "lower_columns": (
                            declaration.source_updated_at_field,
                            declaration.key_name,
                        ),
                        "lower_operator": ">",
                        "upper_columns": (
                            declaration.source_updated_at_field,
                            declaration.key_name,
                        ),
                        "upper_operator": "<=",
                    }
                elif upper.position == "row":
                    row_predicate = {
                        "lower_columns": None,
                        "lower_operator": None,
                        "upper_columns": (
                            declaration.source_updated_at_field,
                            declaration.key_name,
                        ),
                        "upper_operator": "<=",
                    }
                else:
                    row_predicate = {
                        "lower_columns": None,
                        "lower_operator": None,
                        "upper_columns": (declaration.source_updated_at_field,),
                        "upper_operator": "<" if upper_bound_is_exclusive else "<=",
                    }
                query_shape_digest = digest(
                    {
                        "columns": declaration.field_names,
                        "lower_cursor_position": lower.position,
                        "upper_cursor_position": upper.position,
                        "upper_selection": {
                            "columns": (declaration.source_updated_at_field,),
                            "operator": "<" if upper_bound_is_exclusive else "<=",
                        },
                        "row_predicate": row_predicate,
                        "order_by": (
                            declaration.source_updated_at_field,
                            declaration.key_name,
                        ),
                    }
                )
                private_reference = self._private_boundary_reference_factory(
                    intent.tenant_id, declaration.logical_object_ref
                )
                self._private_boundary_writer(
                    intent.tenant_id,
                    private_reference,
                    canonical_bytes(
                        {
                            "snapshot_identity": snapshot_row[0],
                            "lag_bound": lag_bound,
                            "upper_bound_operator": (
                                "exclusive" if upper_bound_is_exclusive else "inclusive"
                            ),
                            "oldest_xact_start": oldest_xact_start,
                            "record_count": record_count,
                        }
                    ),
                )
                prepared.append(
                    _IncrementalObject(
                        schema=object_schema,
                        declaration=declaration,
                        lower=lower,
                        upper=upper,
                        boundary=AcquisitionBoundary(
                            logical_object_ref=declaration.logical_object_ref,
                            acquisition_mode="incremental",
                            schema_digest=object_schema.schema_digest,
                            lower_cursor_digest=intent.prior_checkpoint_digest,
                            upper_cursor_digest=digest(upper),
                            query_shape_digest=query_shape_digest,
                            snapshot_identity_digest=digest({"snapshot_identity": snapshot_row[0]}),
                            key_range_digest=None,
                            private_boundary_ref=private_reference,
                            record_count=record_count,
                            opened_at=opened_at,
                            closed_at=opened_at,
                        ),
                    )
                )
                candidate_objects.append(upper)

            candidate_cursor_payload = canonical_bytes(
                _PostgreSQLIncrementalCheckpoint(objects=tuple(candidate_objects))
            )
            checkpoint_digest = hashlib.sha256(candidate_cursor_payload).hexdigest()
            prepared = [
                _IncrementalObject(
                    schema=item.schema,
                    declaration=item.declaration,
                    boundary=item.boundary.model_copy(
                        update={"upper_cursor_digest": checkpoint_digest}
                    ),
                    lower=item.lower,
                    upper=item.upper,
                )
                for item in prepared
            ]
            snapshot_objects: list[_SnapshotObject] = []
            for position, item in enumerate(prepared):
                declaration = item.declaration
                row_cursor = connection.cursor(name=f"heinzel_incremental_{position:04d}")
                opened_cursors.append(row_cursor)
                projection = sql.SQL(", ").join(
                    sql.Identifier(field_name) for field_name in declaration.field_names
                )
                if item.lower.position == "row":
                    lower_row = cast(PostgreSQLIncrementalCursor, item.lower.as_row_cursor())
                    upper_row_cursor = cast(PostgreSQLIncrementalCursor, item.upper.as_row_cursor())
                    row_cursor.execute(
                        sql.SQL(
                            "/* incremental_rows */ SELECT {} FROM {} "
                            "WHERE ({}, {}) > (%s, %s) AND ({}, {}) <= (%s, %s) "
                            "ORDER BY {}, {}"
                        ).format(
                            projection,
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                        ),
                        (
                            lower_row.updated_at,
                            lower_row.primary_key,
                            upper_row_cursor.updated_at,
                            upper_row_cursor.primary_key,
                        ),
                    )
                elif item.upper.position == "row":
                    upper_row_cursor = cast(PostgreSQLIncrementalCursor, item.upper.as_row_cursor())
                    row_cursor.execute(
                        sql.SQL(
                            "/* incremental_rows_before_first */ SELECT {} FROM {} "
                            "WHERE ({}, {}) <= (%s, %s) ORDER BY {}, {}"
                        ).format(
                            projection,
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                        ),
                        (upper_row_cursor.updated_at, upper_row_cursor.primary_key),
                    )
                else:
                    row_cursor.execute(
                        sql.SQL(
                            "/* incremental_rows_before_first */ SELECT {} FROM {} "
                            "WHERE {} {} %s ORDER BY {}, {}"
                        ).format(
                            projection,
                            sql.Identifier(declaration.schema_name, declaration.table_name),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.SQL("<" if upper_bound_is_exclusive else "<="),
                            sql.Identifier(declaration.source_updated_at_field),
                            sql.Identifier(declaration.key_name),
                        ),
                        (item.upper.updated_at,),
                    )
                snapshot_objects.append(
                    _SnapshotObject(
                        schema=item.schema,
                        cursor=row_cursor,
                        boundary=item.boundary,
                        row_names=declaration.field_names,
                        key_position=declaration.field_names.index(declaration.key_name),
                        updated_at_position=declaration.field_names.index(
                            declaration.source_updated_at_field
                        ),
                    )
                )
            return _PostgreSQLSnapshotSession(
                connection=connection,
                objects=tuple(snapshot_objects),
                clock=self._clock,
                cursor_version=_CURSOR_VERSION,
                candidate_cursor_payload=candidate_cursor_payload,
            )
        except _PostgreSQLCursorIntegrityError:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("integrity_failure") from None
        except AcquisitionCeilingExceeded:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise
        except AcquisitionProviderError:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise
        except psycopg.Error as error:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _translate_driver_error(error) from None
        except (TypeError, ValueError, ValidationError):
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("permanent_configuration") from None
        except Exception:
            if connection is not None:
                _abort_connection(connection, opened_cursors)
            raise _provider_error("integrity_failure") from None

    def _open_connection(self) -> _Connection:
        return connect_attributing_startup_denial(
            self._connect,
            self._settings.dsn.get_secret_value(),
            probe=self._startup_denial_probe,
        )

    def _resolve_declarations(
        self, object_refs: tuple[str, ...]
    ) -> tuple[PostgreSQLSourceObjectDeclaration, ...]:
        try:
            return tuple(self._declarations[object_ref] for object_ref in object_refs)
        except KeyError:
            raise _provider_error("permanent_configuration") from None

    @staticmethod
    def _require_schema_declaration(
        declaration: PostgreSQLSourceObjectDeclaration,
        schema: AcquisitionObjectSchema,
    ) -> None:
        if (
            declaration.field_names != tuple(field.name for field in schema.fields)
            or schema.record_key_fields != (declaration.key_name,)
            or schema.source_updated_at_field != declaration.source_updated_at_field
        ):
            raise ValueError("PostgreSQL schema does not match the source declaration")

    def _inspect_object(
        self,
        connection: _Connection,
        declaration: PostgreSQLSourceObjectDeclaration,
    ) -> _ObservedObject:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.oid::text, current_database(), c.relkind "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (declaration.schema_name, declaration.table_name),
            )
            identity = cursor.fetchone()
            if identity is None or len(identity) != 3:
                raise ValueError("PostgreSQL declared relation does not exist")
            relation_oid, database_name, object_kind = identity
            if not isinstance(relation_oid, str) or not isinstance(database_name, str):
                raise ValueError("PostgreSQL relation identity is invalid")
            if object_kind != "r":
                raise ValueError("PostgreSQL relation is not a base table")
            cursor.execute(
                "SELECT column_name, data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = ANY(%s) "
                "ORDER BY array_position(%s::text[], column_name)",
                (
                    declaration.schema_name,
                    declaration.table_name,
                    list(declaration.field_names),
                    list(declaration.field_names),
                ),
            )
            fields = tuple(_acquisition_field(row) for row in cursor.fetchall())
            if tuple(field.name for field in fields) != declaration.field_names:
                raise ValueError("PostgreSQL declared projection is unavailable")
            cursor.execute(
                "SELECT a.attname, "
                "CASE c.contype WHEN 'p' THEN 'primary_key' WHEN 'u' THEN 'unique' END, "
                "a.attnotnull, am.amname, cardinality(c.conkey) "
                "FROM pg_catalog.pg_constraint c "
                "JOIN pg_catalog.pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "JOIN pg_catalog.pg_class i ON i.oid = c.conindid "
                "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
                "WHERE c.conrelid = %s::oid AND c.contype IN ('p', 'u') "
                "AND a.attname = %s ORDER BY (c.contype = 'p') DESC, c.oid LIMIT 1",
                (relation_oid, declaration.key_name),
            )
            key = cursor.fetchone()
            if key is None or len(key) != 5 or key[0] != declaration.key_name:
                raise ValueError("PostgreSQL stable key is unavailable")
            # PostgreSQL 17 introduced MAINTAIN; older servers cannot grant that privilege.
            cursor.execute(
                "WITH authority(relation_oid, approved_columns, approved_relation_schemas, "
                "approved_relation_names, unrelated_schema) AS "
                "(VALUES (%s::oid, %s::text[], %s::text[], %s::text[], %s::text)), "
                "undeclared_relations(relation_oid) AS ("
                "SELECT relation.oid FROM pg_catalog.pg_class relation "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace CROSS JOIN authority a "
                "WHERE relation.relkind IN ('r', 'v', 'm', 'f', 'p') "
                "AND namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_' AND NOT EXISTS ("
                "SELECT 1 FROM unnest(a.approved_relation_schemas, "
                "a.approved_relation_names) approved(schema_name, relation_name) "
                "WHERE approved.schema_name = namespace.nspname "
                "AND approved.relation_name = relation.relname)), "
                "undeclared_sequences(sequence_oid) AS ("
                "SELECT relation.oid FROM pg_catalog.pg_class relation "
                "JOIN pg_catalog.pg_namespace namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE relation.relkind = 'S' "
                "AND namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_') "
                "SELECT session_user = current_user "
                "AND NOT r.rolsuper AND NOT r.rolcreatedb AND NOT r.rolcreaterole "
                "AND NOT r.rolreplication AND NOT r.rolbypassrls "
                "AND NOT has_table_privilege(current_user, a.relation_oid, 'SELECT') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM unnest(a.approved_columns) approved(column_name) "
                "WHERE NOT has_column_privilege(current_user, a.relation_oid, "
                "approved.column_name, 'SELECT')) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_attribute attribute "
                "WHERE attribute.attrelid = a.relation_oid AND attribute.attnum > 0 "
                "AND NOT attribute.attisdropped "
                "AND NOT attribute.attname = ANY(a.approved_columns) "
                "AND has_column_privilege(current_user, a.relation_oid, "
                "attribute.attname, 'SELECT')) "
                "AND NOT ("
                "has_table_privilege(current_user, a.relation_oid, 'INSERT') OR "
                "has_table_privilege(current_user, a.relation_oid, 'UPDATE') OR "
                "has_table_privilege(current_user, a.relation_oid, 'DELETE') OR "
                "has_table_privilege(current_user, a.relation_oid, 'TRUNCATE') OR "
                "has_table_privilege(current_user, a.relation_oid, 'REFERENCES') OR "
                "has_table_privilege(current_user, a.relation_oid, 'TRIGGER') OR "
                "(CASE WHEN current_setting('server_version_num')::integer >= 170000 "
                "THEN has_table_privilege(current_user, a.relation_oid, 'MAINTAIN') "
                "ELSE FALSE END) OR "
                "has_any_column_privilege(current_user, a.relation_oid, 'INSERT') OR "
                "has_any_column_privilege(current_user, a.relation_oid, 'UPDATE') OR "
                "has_any_column_privilege(current_user, a.relation_oid, 'REFERENCES')) "
                "AND NOT has_database_privilege(current_user, current_database(), 'CREATE') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_namespace namespace "
                "WHERE namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_' "
                "AND has_schema_privilege(current_user, namespace.oid, 'CREATE')) "
                "AND EXISTS (SELECT 1 FROM pg_catalog.pg_namespace unrelated "
                "WHERE unrelated.nspname = a.unrelated_schema) "
                "AND NOT has_schema_privilege(current_user, a.unrelated_schema, 'USAGE') "
                "AND NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_namespace namespace "
                "WHERE namespace.nspname <> ALL(a.approved_relation_schemas) "
                "AND namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_' "
                "AND has_schema_privilege(current_user, namespace.oid, 'USAGE')) "
                "AND NOT EXISTS (SELECT 1 FROM undeclared_relations undeclared WHERE "
                "has_table_privilege(current_user, undeclared.relation_oid, 'SELECT') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'INSERT') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'UPDATE') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'DELETE') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'TRUNCATE') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'REFERENCES') OR "
                "has_table_privilege(current_user, undeclared.relation_oid, 'TRIGGER') OR "
                "(CASE WHEN current_setting('server_version_num')::integer >= 170000 "
                "THEN has_table_privilege(current_user, undeclared.relation_oid, 'MAINTAIN') "
                "ELSE FALSE END) OR "
                "has_any_column_privilege(current_user, undeclared.relation_oid, 'SELECT') OR "
                "has_any_column_privilege(current_user, undeclared.relation_oid, 'INSERT') OR "
                "has_any_column_privilege(current_user, undeclared.relation_oid, 'UPDATE') OR "
                "has_any_column_privilege(current_user, undeclared.relation_oid, 'REFERENCES')) "
                "AND NOT EXISTS (SELECT 1 FROM undeclared_sequences sequence WHERE "
                "has_sequence_privilege(current_user, sequence.sequence_oid, 'SELECT') OR "
                "has_sequence_privilege(current_user, sequence.sequence_oid, 'USAGE') OR "
                "has_sequence_privilege(current_user, sequence.sequence_oid, 'UPDATE')) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_auth_members membership "
                "JOIN pg_catalog.pg_roles member ON member.oid = membership.member "
                "WHERE member.rolname = current_user AND membership.admin_option) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_roles candidate "
                "WHERE candidate.rolname <> current_user "
                "AND pg_has_role(current_user, candidate.oid, 'MEMBER') "
                "AND (candidate.rolsuper OR candidate.rolcreatedb OR candidate.rolcreaterole OR "
                "candidate.rolreplication OR candidate.rolbypassrls OR "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members membership "
                "WHERE membership.member = candidate.oid AND membership.admin_option) OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'SELECT') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'INSERT') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'UPDATE') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'DELETE') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'TRUNCATE') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'REFERENCES') OR "
                "has_table_privilege(candidate.rolname, a.relation_oid, 'TRIGGER') OR "
                "(CASE WHEN current_setting('server_version_num')::integer >= 170000 "
                "THEN has_table_privilege(candidate.rolname, a.relation_oid, 'MAINTAIN') "
                "ELSE FALSE END) OR "
                "has_any_column_privilege(candidate.rolname, a.relation_oid, 'INSERT') OR "
                "has_any_column_privilege(candidate.rolname, a.relation_oid, 'UPDATE') OR "
                "has_any_column_privilege(candidate.rolname, a.relation_oid, 'REFERENCES') OR "
                "has_database_privilege(candidate.rolname, current_database(), 'CREATE') OR "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace namespace "
                "WHERE namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_' "
                "AND has_schema_privilege(candidate.rolname, namespace.oid, 'CREATE')) OR "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_attribute attribute "
                "WHERE attribute.attrelid = a.relation_oid AND attribute.attnum > 0 "
                "AND NOT attribute.attisdropped "
                "AND NOT attribute.attname = ANY(a.approved_columns) "
                "AND has_column_privilege(candidate.rolname, a.relation_oid, "
                "attribute.attname, 'SELECT')) OR "
                "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace namespace "
                "WHERE namespace.nspname <> ALL(a.approved_relation_schemas) "
                "AND namespace.nspname <> 'information_schema' "
                "AND namespace.nspname !~ '^pg_' "
                "AND has_schema_privilege(candidate.rolname, namespace.oid, 'USAGE')) OR "
                "EXISTS (SELECT 1 FROM undeclared_relations undeclared WHERE "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'SELECT') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'INSERT') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'UPDATE') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'DELETE') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'TRUNCATE') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, "
                "'REFERENCES') OR "
                "has_table_privilege(candidate.rolname, undeclared.relation_oid, 'TRIGGER') OR "
                "(CASE WHEN current_setting('server_version_num')::integer >= 170000 "
                "THEN has_table_privilege(candidate.rolname, undeclared.relation_oid, "
                "'MAINTAIN') ELSE FALSE END) OR "
                "has_any_column_privilege(candidate.rolname, undeclared.relation_oid, "
                "'SELECT') OR "
                "has_any_column_privilege(candidate.rolname, undeclared.relation_oid, "
                "'INSERT') OR "
                "has_any_column_privilege(candidate.rolname, undeclared.relation_oid, "
                "'UPDATE') OR "
                "has_any_column_privilege(candidate.rolname, undeclared.relation_oid, "
                "'REFERENCES')) OR "
                "EXISTS (SELECT 1 FROM undeclared_sequences sequence WHERE "
                "has_sequence_privilege(candidate.rolname, sequence.sequence_oid, 'SELECT') OR "
                "has_sequence_privilege(candidate.rolname, sequence.sequence_oid, 'USAGE') OR "
                "has_sequence_privilege(candidate.rolname, sequence.sequence_oid, 'UPDATE')))) "
                "FROM pg_catalog.pg_roles r CROSS JOIN authority a "
                "WHERE r.rolname = current_user",
                (
                    relation_oid,
                    list(declaration.field_names),
                    list(self._approved_relation_schemas),
                    list(self._approved_relation_names),
                    self._settings.unrelated_schema_name,
                ),
            )
            privilege = cursor.fetchone()
        least_privilege = privilege is not None and len(privilege) == 1 and privilege[0] is True
        key_constraint = str(key[1])
        if key_constraint not in {"primary_key", "unique"}:
            raise ValueError("PostgreSQL key constraint is invalid")
        return _ObservedObject(
            declaration=declaration,
            relation_oid=relation_oid,
            database_name=database_name,
            fields=fields,
            key_constraint=cast(_KeyConstraint, key_constraint),
            key_not_null=key[2] is True,
            key_access_method=str(key[3]),
            key_column_count=int(cast(int, key[4])),
            least_privilege=least_privilege,
        )

    @staticmethod
    def _require_admitted_metadata(observed: _ObservedObject) -> None:
        declaration = observed.declaration
        updated_at = next(
            field for field in observed.fields if field.name == declaration.source_updated_at_field
        )
        if (
            observed.key_constraint not in {"primary_key", "unique"}
            or not observed.key_not_null
            or observed.key_access_method != "btree"
            or observed.key_column_count != 1
            or updated_at.value_type != "timestamp"
            or updated_at.nullable
        ):
            raise ValueError("PostgreSQL acquisition metadata is not admitted")
        if not observed.least_privilege:
            raise _provider_error("authorization_denied")

    @staticmethod
    def _capture_bounds(
        connection: _Connection,
        declaration: PostgreSQLSourceObjectDeclaration,
    ) -> tuple[object, object, int]:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT min({}), max({}), count(*) FROM {}").format(
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.schema_name, declaration.table_name),
                )
            )
            bounds = cursor.fetchone()
        if bounds is None or len(bounds) != 3 or type(bounds[2]) is not int or bounds[2] < 0:
            raise ValueError("PostgreSQL snapshot bounds are invalid")
        return bounds[0], bounds[1], bounds[2]

    @staticmethod
    def _capture_snapshot_bounds(
        connection: _Connection,
        declaration: PostgreSQLSourceObjectDeclaration,
        upper_cursor: PostgreSQLIncrementalCursor,
    ) -> tuple[object, object, int]:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "/* snapshot_bounds */ SELECT min({}), max({}), count(*) FROM {} "
                    "WHERE ({}, {}) <= (%s, %s)"
                ).format(
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.schema_name, declaration.table_name),
                    sql.Identifier(declaration.source_updated_at_field),
                    sql.Identifier(declaration.key_name),
                ),
                (upper_cursor.updated_at, upper_cursor.primary_key),
            )
            bounds = cursor.fetchone()
        if bounds is None or len(bounds) != 3 or type(bounds[2]) is not int or bounds[2] < 0:
            raise ValueError("PostgreSQL snapshot bounds are invalid")
        return bounds[0], bounds[1], bounds[2]

    @staticmethod
    def _capture_before_first_bounds(
        connection: _Connection,
        declaration: PostgreSQLSourceObjectDeclaration,
        lag_bound: datetime,
    ) -> tuple[object, object, int]:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "/* snapshot_bounds_before_first */ SELECT min({}), max({}), count(*) "
                    "FROM {} WHERE {} <= %s"
                ).format(
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.key_name),
                    sql.Identifier(declaration.schema_name, declaration.table_name),
                    sql.Identifier(declaration.source_updated_at_field),
                ),
                (lag_bound,),
            )
            bounds = cursor.fetchone()
        if bounds is None or bounds != (None, None, 0):
            raise ValueError("PostgreSQL before-first snapshot bounds are invalid")
        return None, None, 0


class _PostgreSQLSnapshotSession:
    def __init__(
        self,
        *,
        connection: _Connection,
        objects: tuple[_SnapshotObject, ...],
        clock: Callable[[], datetime],
        cursor_version: str,
        candidate_cursor_payload: bytes,
    ) -> None:
        self._connection = connection
        self._objects = objects
        self._clock = clock
        self._cursor_version = cursor_version
        self._candidate_cursor_payload = candidate_cursor_payload
        self._object_position = 0
        self._current_iterator: Iterator[tuple[object, ...]] | None = None
        self._record_counts = [0 for _item in objects]
        self._completed: CompletedAcquisition | None = None
        self._closed = False

    def __iter__(self) -> _PostgreSQLSnapshotSession:
        return self

    def __next__(self) -> AcquisitionRecord:
        if self._closed:
            raise StopIteration
        while self._object_position < len(self._objects):
            snapshot_object = self._objects[self._object_position]
            if self._current_iterator is None:
                self._current_iterator = iter(snapshot_object.cursor)
            try:
                row = next(self._current_iterator)
            except StopIteration:
                if (
                    self._record_counts[self._object_position]
                    != snapshot_object.boundary.record_count
                ):
                    self._abort_safely()
                    raise _provider_error("invalid_provider_response") from None
                self._object_position += 1
                self._current_iterator = None
                continue
            except psycopg.Error as error:
                self._abort_safely()
                raise _translate_driver_error(error) from None
            except Exception:
                self._abort_safely()
                raise _provider_error("invalid_provider_response") from None
            try:
                record = self._record(snapshot_object, row)
                self._record_counts[self._object_position] += 1
                if (
                    self._record_counts[self._object_position]
                    > snapshot_object.boundary.record_count
                ):
                    raise ValueError("PostgreSQL returned more rows than its captured count")
                return record
            except (TypeError, ValueError, ValidationError):
                self._abort_safely()
                raise _provider_error("invalid_provider_response") from None
        self._finish()
        raise StopIteration

    def complete(self) -> CompletedAcquisition:
        if self._completed is None:
            raise AcquisitionSessionIncomplete()
        return self._completed

    def abort(self) -> None:
        self._abort_safely()

    def _record(
        self,
        snapshot_object: _SnapshotObject,
        row: tuple[object, ...],
    ) -> AcquisitionRecord:
        if len(row) != len(snapshot_object.schema.fields):
            raise ValueError("PostgreSQL row shape does not match approved projection")
        values = tuple(
            _require_scalar(value, field)
            for field, value in zip(snapshot_object.schema.fields, row, strict=True)
        )
        key = values[snapshot_object.key_position]
        updated_at = values[snapshot_object.updated_at_position]
        if not isinstance(updated_at, datetime):
            raise ValueError("PostgreSQL updated timestamp is invalid")
        return AcquisitionRecord(
            logical_object_ref=snapshot_object.schema.logical_object_ref,
            record_key=digest(
                {
                    "logical_object_ref": snapshot_object.schema.logical_object_ref,
                    "key": key,
                }
            ),
            source_created_at=None,
            source_updated_at=updated_at,
            fields=tuple(
                AcquisitionFieldValue(name=name, value=value)
                for name, value in zip(snapshot_object.row_names, values, strict=True)
            ),
        )

    def _finish(self) -> None:
        try:
            self._connection.commit()
        except psycopg.Error:
            self._abort_safely()
            raise _provider_error("ambiguous_outcome") from None
        except Exception:
            self._abort_safely()
            raise _provider_error("ambiguous_outcome") from None
        closed_at = self._clock()
        boundaries = tuple(
            item.boundary.model_copy(update={"closed_at": closed_at}) for item in self._objects
        )
        self._completed = CompletedAcquisition(
            boundaries=boundaries,
            cursor_version=self._cursor_version,
            candidate_cursor_payload=self._candidate_cursor_payload,
        )
        self._close_safely()

    def _abort_safely(self) -> None:
        if self._closed:
            return
        with suppress(Exception):
            self._connection.rollback()
        self._close_safely()

    def _close_safely(self) -> None:
        for item in self._objects:
            with suppress(Exception):
                item.cursor.close()
        with suppress(Exception):
            self._connection.close()
        self._closed = True


def _abort_connection(
    connection: _Connection,
    cursors: list[_Cursor] | None = None,
) -> None:
    with suppress(Exception):
        connection.rollback()
    for cursor in cursors or ():
        with suppress(Exception):
            cursor.close()
    with suppress(Exception):
        connection.close()
