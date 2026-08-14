from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    ColumnObservation,
    DriftProbe,
    OrderRow,
    ProviderObservation,
    SourceBoundary,
)
from psycopg import sql

from .settings import PostgresSettings

type ColumnMetadata = (
    tuple[str, str, str, str, int | None, int | None]
    | tuple[str, str, str, str, int | None, int | None, int | None]
)
type KeyConstraint = Literal["primary_key", "unique", "none"]


def normalize_columns(rows: Iterable[ColumnMetadata]) -> tuple[ColumnObservation, ...]:
    columns: list[ColumnObservation] = []
    for raw in rows:
        name, data_type, udt_name, nullable, precision, scale = raw[:6]
        max_length = raw[6] if len(raw) == 7 else (3 if name == "currency" else None)
        if udt_name == "int8":
            type_name = "BIGINT"
        elif udt_name == "text":
            type_name = "TEXT"
        elif udt_name == "numeric" and precision is not None and scale is not None:
            type_name = f"NUMERIC({precision},{scale})"
        elif udt_name == "varchar" and max_length is not None:
            type_name = f"VARCHAR({max_length})"
        elif udt_name == "timestamptz":
            type_name = "TIMESTAMPTZ"
        else:
            type_name = data_type.upper()
        columns.append(
            ColumnObservation(name=name, type_name=type_name, nullable=nullable == "YES")
        )
    return tuple(columns)


class PostgresProvider:
    def __init__(
        self,
        settings: PostgresSettings,
        *,
        connect: Callable[..., Any] = psycopg.connect,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def _connection(self) -> Any:
        return self._connect(self._settings.dsn.get_secret_value())

    def _metadata(
        self, connection: Any
    ) -> tuple[
        str,
        str,
        tuple[ColumnObservation, ...],
        tuple[str | None, KeyConstraint | None, bool | None, bool],
        bool | None,
    ]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.oid::text, current_database() "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r'",
                (self._settings.schema_name, self._settings.table_name),
            )
            identity_row = cursor.fetchone()
            if identity_row is None:
                raise ValueError("declared PostgreSQL base table does not exist")
            cursor.execute(
                "SELECT column_name, data_type, udt_name, is_nullable, "
                "numeric_precision, numeric_scale, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (self._settings.schema_name, self._settings.table_name),
            )
            columns = normalize_columns(cursor.fetchall())
            cursor.execute(
                "SELECT a.attname, "
                "CASE c.contype WHEN 'p' THEN 'primary_key' WHEN 'u' THEN 'unique' END, "
                "a.attnotnull, am.amname "
                "FROM pg_catalog.pg_constraint c "
                "JOIN pg_catalog.pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                "JOIN pg_catalog.pg_class i ON i.oid = c.conindid "
                "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
                "WHERE c.conrelid = %s::oid AND c.contype IN ('p', 'u') "
                "AND cardinality(c.conkey) = 1 "
                "AND a.attname = %s "
                "ORDER BY (c.contype = 'p') DESC, c.oid LIMIT 1",
                (identity_row[0], self._settings.key_name),
            )
            constraint_row = cursor.fetchone()
            cursor.execute(
                "SELECT has_table_privilege(current_user, target.relation_oid, 'SELECT') "
                "AND NOT ("
                "has_table_privilege(current_user, target.relation_oid, 'INSERT') OR "
                "has_table_privilege(current_user, target.relation_oid, 'UPDATE') OR "
                "has_table_privilege(current_user, target.relation_oid, 'DELETE') OR "
                "has_table_privilege(current_user, target.relation_oid, 'TRUNCATE') OR "
                "has_table_privilege(current_user, target.relation_oid, 'REFERENCES') OR "
                "has_table_privilege(current_user, target.relation_oid, 'TRIGGER') OR "
                "has_any_column_privilege(current_user, target.relation_oid, 'INSERT') OR "
                "has_any_column_privilege(current_user, target.relation_oid, 'UPDATE') OR "
                "has_any_column_privilege(current_user, target.relation_oid, 'REFERENCES')) "
                "FROM (VALUES (%s::oid)) AS target(relation_oid)",
                (identity_row[0],),
            )
            privilege_row = cursor.fetchone()
        database_identity = digest({"database": identity_row[1]})[:16]
        object_identity = f"pg:{database_identity}:{identity_row[0]}"
        if constraint_row is None:
            key: tuple[str | None, KeyConstraint | None, bool | None, bool] = (
                None,
                "none",
                None,
                False,
            )
        else:
            key_name = str(constraint_row[0])
            constraint_value = str(constraint_row[1])
            constraint: KeyConstraint | None = (
                "primary_key"
                if constraint_value == "primary_key"
                else "unique"
                if constraint_value == "unique"
                else None
            )
            nullable = not bool(constraint_row[2])
            stable = (
                constraint in {"primary_key", "unique"}
                and not nullable
                and constraint_row[3] == "btree"
            )
            key = (key_name, constraint, nullable, stable)
        read_only = bool(privilege_row[0]) if privilege_row is not None else None
        return object_identity, identity_row[1], columns, key, read_only

    def observe(self) -> ProviderObservation:
        with closing(self._connection()) as connection:
            object_identity, _database, columns, key, read_only = self._metadata(connection)
        key_name, key_constraint, key_nullable, stable_key_order = key
        return ProviderObservation(
            provider="postgresql",
            connection_handle=self._settings.connection_handle,
            object_identity=object_identity,
            object_kind="base_table",
            schema_digest=digest(columns),
            columns=columns,
            key_name=key_name,
            key_type=next(
                (column.type_name for column in columns if column.name == key_name),
                None,
            ),
            key_nullable=key_nullable,
            key_constraint=key_constraint,
            stable_key_order=stable_key_order,
            read_only=read_only,
            capabilities=("drift_probe", "snapshot_read", "stable_primary_key_order"),
            observed_at=self._clock(),
            snapshot_semantics="snapshot",
            commit_ledger_object_kind=None,
            commit_ledger_columns=None,
            commit_ledger_key_name=None,
            commit_ledger_key_constraint=None,
            evidence_safe=True,
        )

    def drift_probe(self) -> DriftProbe:
        observation = self.observe()
        return DriftProbe(
            object_identity=observation.object_identity,
            schema_digest=observation.schema_digest,
        )

    def read_snapshot(self) -> tuple[SourceBoundary, Iterable[OrderRow]]:
        connection = self._connection()
        opened_at = self._clock()
        try:
            connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            object_identity, _database, columns, _key, _read_only = self._metadata(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_current_snapshot()::text")
                snapshot_identity = cursor.fetchone()[0]
                bounds_query = sql.SQL(
                    "SELECT min({key}), max({key}), count(*) FROM {table}"
                ).format(
                    key=sql.Identifier(self._settings.key_name),
                    table=sql.Identifier(self._settings.schema_name, self._settings.table_name),
                )
                cursor.execute(bounds_query)
                key_min, key_max, row_count = cursor.fetchone()
                if row_count > 10_000:
                    raise ValueError(f"row ceiling exceeded: {row_count} > 10000")
            read_query = sql.SQL(
                "SELECT order_id, customer_ref, amount, currency, status, updated_at "
                "FROM {table} ORDER BY {key}"
            ).format(
                table=sql.Identifier(self._settings.schema_name, self._settings.table_name),
                key=sql.Identifier(self._settings.key_name),
            )
            read_cursor = connection.cursor(name="pillarmesh_m0_snapshot")
            read_cursor.execute(read_query)
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        boundary = SourceBoundary(
            object_identity=object_identity,
            schema_digest=digest(columns),
            snapshot_identity=snapshot_identity,
            key_range_digest=digest({"key_min": key_min, "key_max": key_max}),
            row_count=row_count,
            query_shape_digest=digest(
                {
                    "columns": (
                        "order_id",
                        "customer_ref",
                        "amount",
                        "currency",
                        "status",
                        "updated_at",
                    ),
                    "order_by": self._settings.key_name,
                }
            ),
            opened_at=opened_at,
            closed_at=self._clock(),
        )

        return boundary, _SnapshotRows(connection, read_cursor, boundary, self._clock)


class _SnapshotRows:
    _NAMES = ("order_id", "customer_ref", "amount", "currency", "status", "updated_at")

    def __init__(
        self,
        connection: Any,
        cursor: Any,
        opened_boundary: SourceBoundary,
        clock: Callable[[], datetime],
    ) -> None:
        self._connection = connection
        self._cursor = iter(cursor)
        self._raw_cursor = cursor
        self._opened_boundary = opened_boundary
        self._clock = clock
        self._completed_boundary: SourceBoundary | None = None
        self._closed = False

    def __iter__(self) -> _SnapshotRows:
        return self

    def __next__(self) -> OrderRow:
        if self._closed:
            raise StopIteration
        try:
            row = next(self._cursor)
        except StopIteration:
            self._connection.commit()
            self._completed_boundary = self._opened_boundary.model_copy(
                update={"closed_at": self._clock()}
            )
            self._close()
            raise
        except BaseException:
            self.abort()
            raise
        return OrderRow.model_validate(dict(zip(self._NAMES, row, strict=True)))

    def final_boundary(self) -> SourceBoundary:
        if self._completed_boundary is None:
            raise RuntimeError("snapshot rows have not been consumed completely")
        return self._completed_boundary

    def abort(self) -> None:
        if not self._closed:
            self._connection.rollback()
            self._close()

    def _close(self) -> None:
        self._raw_cursor.close()
        self._connection.close()
        self._closed = True
