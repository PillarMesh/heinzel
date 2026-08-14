from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import snowflake.connector
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    ColumnObservation,
    CommitReceipt,
    OrderRow,
    ProviderError,
    ProviderObservation,
    SegmentManifest,
    VisibilityProof,
)

from .settings import SnowflakeSettings

_STAGE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
type KeyConstraint = Literal["primary_key", "unique", "none"]
type ObjectKind = Literal["base_table", "view", "unknown"]
type Classification = Literal["retryable", "throttled", "authorization", "permanent", "ambiguous"]

# Statement-level rejections cannot succeed on a replay; everything else (transport
# resets, suspended warehouses, throttling, driver-level failures) can.
_PERMANENT_ERRORS = (
    snowflake.connector.errors.ProgrammingError,
    snowflake.connector.errors.IntegrityError,
    snowflake.connector.errors.DataError,
    snowflake.connector.errors.NotSupportedError,
)


def _classification(error: BaseException) -> Classification:
    return "permanent" if isinstance(error, _PERMANENT_ERRORS) else "retryable"


@contextmanager
def _classified(message: str) -> Iterator[None]:
    """Present driver failures as classified ProviderErrors.

    The runtime's bounded retry and its non-conforming decision both key off
    ProviderError.classification, so a raw driver exception escaping this boundary
    silently disables retries and fails the run terminally.
    """
    try:
        yield
    except ProviderError:
        raise
    except Exception as error:
        raise ProviderError(message, _classification(error)) from error


class SnowflakeProvider:
    def __init__(
        self,
        settings: SnowflakeSettings,
        *,
        connect: Callable[..., Any] = snowflake.connector.connect,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._clock = clock or (lambda: datetime.now(UTC))

    def _connection(self) -> Any:
        return self._connect(
            account=self._settings.account,
            user=self._settings.user,
            password=self._settings.password.get_secret_value(),
            role=self._settings.role,
            warehouse=self._settings.warehouse,
            database=self._settings.database,
            schema=self._settings.schema_name,
            autocommit=True,
            session_parameters={"QUERY_TAG": "pillarmesh-m0"},
        )

    def _qualified(self, object_name: str) -> str:
        return f"{self._settings.database}.{self._settings.schema_name}.{object_name}"

    def _stage_prefix(self, manifest: SegmentManifest) -> str:
        if not _STAGE_COMPONENT.fullmatch(manifest.batch_id):
            raise ProviderError("batch identity is unsafe for a stage path", "permanent")
        return f"@{self._qualified(self._settings.stage)}/runs/{manifest.batch_id}"

    def _stage_path(self, manifest: SegmentManifest) -> str:
        filename = manifest.segment_name
        if not _STAGE_COMPONENT.fullmatch(filename):
            raise ProviderError("segment filename is unsafe for a stage path", "permanent")
        return f"{self._stage_prefix(manifest)}/{filename}"

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    def observe(self) -> ProviderObservation:
        with (
            _classified("Snowflake observation failed"),
            closing(self._connection()) as connection,
            connection.cursor() as cursor,
        ):
            object_kind = self._object_kind(cursor, self._settings.target_table)
            rows = self._column_rows(cursor, self._settings.target_table)
            if not rows:
                raise ProviderError("declared Snowflake target table does not exist", "permanent")
            columns = tuple(self._normalize_column(row) for row in rows)
            key_name, key_constraint = self._key_constraint(cursor, self._settings.target_table)
            ledger_object_kind = self._object_kind(cursor, self._settings.ledger_table)
            ledger_rows = self._column_rows(cursor, self._settings.ledger_table)
            ledger_columns = tuple(self._normalize_column(row) for row in ledger_rows)
            ledger_key_name, ledger_key_constraint = self._key_constraint(
                cursor, self._settings.ledger_table
            )
        object_identity = (
            "sf:"
            + digest(
                {
                    "account": self._settings.account,
                    "database": self._settings.database,
                    "schema": self._settings.schema_name,
                    "table": self._settings.target_table,
                }
            )[:32]
        )
        return ProviderObservation(
            provider="snowflake",
            connection_handle=self._settings.connection_handle,
            object_identity=object_identity,
            object_kind=object_kind,
            schema_digest=digest(columns),
            columns=columns,
            key_name=key_name,
            key_type=next(
                (column.type_name for column in columns if column.name == key_name), None
            ),
            key_nullable=next(
                (column.nullable for column in columns if column.name == key_name), None
            ),
            key_constraint=key_constraint,
            stable_key_order=None,
            read_only=None,
            capabilities=(
                "commit_ledger",
                "idempotent_merge",
                "stage_write",
                "visibility_query",
            ),
            observed_at=self._clock(),
            snapshot_semantics="unknown",
            commit_ledger_object_kind=ledger_object_kind,
            commit_ledger_columns=ledger_columns,
            commit_ledger_key_name=ledger_key_name,
            commit_ledger_key_constraint=ledger_key_constraint,
            evidence_safe=True,
        )

    def _object_kind(self, cursor: Any, table_name: str) -> ObjectKind:
        cursor.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s",
            (self._settings.database, self._settings.schema_name, table_name),
        )
        row = cursor.fetchone()
        if row is None:
            return "unknown"
        if row[0] == "BASE TABLE":
            return "base_table"
        if row[0] == "VIEW":
            return "view"
        return "unknown"

    def _column_rows(self, cursor: Any, table_name: str) -> list[tuple[Any, ...]]:
        cursor.execute(
            "SELECT column_name, data_type, is_nullable, numeric_precision, "
            "numeric_scale, character_maximum_length, datetime_precision "
            "FROM information_schema.columns "
            "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (self._settings.database, self._settings.schema_name, table_name),
        )
        return list(cursor.fetchall())

    def _key_constraint(self, cursor: Any, table_name: str) -> tuple[str | None, KeyConstraint]:
        cursor.execute(
            "SELECT kcu.column_name, tc.constraint_type "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_catalog = tc.constraint_catalog "
            "AND kcu.constraint_schema = tc.constraint_schema "
            "AND kcu.constraint_name = tc.constraint_name "
            "WHERE tc.table_catalog = %s AND tc.table_schema = %s AND tc.table_name = %s "
            "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
            "ORDER BY CASE tc.constraint_type WHEN 'PRIMARY KEY' THEN 0 ELSE 1 END, "
            "kcu.ordinal_position",
            (self._settings.database, self._settings.schema_name, table_name),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            return None, "none"
        constraint: KeyConstraint = "primary_key" if rows[0][1] == "PRIMARY KEY" else "unique"
        return str(rows[0][0]).lower(), constraint

    @staticmethod
    def _normalize_column(row: tuple[Any, ...]) -> ColumnObservation:
        name, data_type, nullable, precision, scale, max_length, datetime_precision = row
        if data_type == "NUMBER" and precision is not None and scale is not None:
            type_name = f"NUMBER({precision},{scale})"
        elif data_type in {"TEXT", "VARCHAR"} and max_length is not None:
            type_name = f"VARCHAR({max_length})"
        elif data_type.startswith("TIMESTAMP_TZ") and datetime_precision is not None:
            type_name = f"TIMESTAMP_TZ({datetime_precision})"
        else:
            type_name = str(data_type)
        return ColumnObservation(
            name=str(name).lower(), type_name=type_name, nullable=nullable == "YES"
        )

    def stage(self, segment: Path, manifest: SegmentManifest) -> None:
        if self._file_digest(segment) != manifest.segment_digest:
            raise ProviderError("segment digest does not match manifest", "permanent")
        uri = segment.resolve().as_uri()
        if "'" in uri or "\n" in uri or "\r" in uri:
            raise ProviderError("segment path cannot be represented safely", "permanent")
        statement = (
            f"PUT '{uri}' {self._stage_prefix(manifest)} AUTO_COMPRESS=FALSE OVERWRITE=FALSE"
        )
        with (
            _classified("Snowflake stage upload failed"),
            closing(self._connection()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(statement)

    def _lookup(self, manifest: SegmentManifest) -> tuple[str, datetime, str] | None:
        ledger = self._qualified(self._settings.ledger_table)
        with (
            _classified("Snowflake commit ledger lookup failed"),
            closing(self._connection()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                f"SELECT manifest_digest, committed_at FROM {ledger} WHERE batch_id = %s",
                (manifest.batch_id,),
            )
            row = cursor.fetchone()
            query_id = cursor.sfqid
        if row is None:
            return None
        committed_at = row[1]
        if committed_at.tzinfo is None:
            committed_at = committed_at.replace(tzinfo=UTC)
        return str(row[0]), committed_at, query_id

    def _receipt(
        self,
        manifest: SegmentManifest,
        committed_at: datetime,
        query_ids: tuple[str, ...],
        affected_rows: int,
        *,
        replayed: bool,
    ) -> CommitReceipt:
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=digest(manifest),
            query_ids=query_ids,
            affected_rows=affected_rows,
            ledger_identity=digest(
                {
                    "provider": "snowflake",
                    "ledger": self._qualified(self._settings.ledger_table),
                }
            ),
            committed_at=committed_at,
            replayed=replayed,
        )

    def _resolve_existing(
        self, manifest: SegmentManifest, existing: tuple[str, datetime, str]
    ) -> CommitReceipt:
        manifest_digest = digest(manifest)
        if existing[0] != manifest_digest:
            raise ProviderError(
                "batch identity already exists with a conflicting manifest", "permanent"
            )
        return self._receipt(manifest, existing[1], (existing[2],), affected_rows=0, replayed=True)

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        existing = self._lookup(manifest)
        if existing is not None:
            return self._resolve_existing(manifest, existing)

        target = self._qualified(self._settings.target_table)
        ledger = self._qualified(self._settings.ledger_table)
        manifest_digest = digest(manifest)
        query_ids: list[str] = []
        with closing(self._connection()) as connection, connection.cursor() as cursor:
            try:
                cursor.execute("BEGIN")
                query_ids.append(cursor.sfqid)
                cursor.execute(self._merge_statement(target, self._stage_path(manifest)))
                query_ids.append(cursor.sfqid)
                affected_rows = max(0, int(getattr(cursor, "rowcount", 0) or 0))
                cursor.execute(
                    f"INSERT INTO {ledger} (batch_id, manifest_digest, committed_at) "
                    "VALUES (%s, %s, CURRENT_TIMESTAMP())",
                    (manifest.batch_id, manifest_digest),
                )
                query_ids.append(cursor.sfqid)
            except Exception as error:
                # The rollback is best-effort: if the connection is what failed, letting
                # its error escape would discard the classified cause below.
                with suppress(Exception):
                    cursor.execute("ROLLBACK")
                raise ProviderError(
                    "Snowflake transactional merge failed", _classification(error)
                ) from error
            try:
                cursor.execute("COMMIT")
                query_ids.append(cursor.sfqid)
            except Exception as error:
                resolved = self._lookup(manifest)
                if resolved is not None:
                    return self._resolve_existing(manifest, resolved)
                raise ProviderError(
                    "Snowflake commit response was lost and the ledger is absent", "retryable"
                ) from error
        return self._receipt(
            manifest,
            self._clock(),
            tuple(query_ids),
            affected_rows=affected_rows,
            replayed=False,
        )

    @staticmethod
    def _merge_statement(target: str, stage_path: str) -> str:
        return f"""
            MERGE INTO {target} AS target
            USING (
                SELECT
                    $1::NUMBER(19,0) AS order_id,
                    $2::VARCHAR(65535) AS customer_ref,
                    $3::NUMBER(18,2) AS amount,
                    $4::VARCHAR(3) AS currency,
                    $5::VARCHAR(65535) AS order_status,
                    $6::TIMESTAMP_TZ(6) AS updated_at
                FROM {stage_path}
            ) AS source
            ON target.order_id = source.order_id
            WHEN MATCHED THEN UPDATE SET
                customer_ref = source.customer_ref,
                amount = source.amount,
                currency = source.currency,
                order_status = source.order_status,
                updated_at = source.updated_at
            WHEN NOT MATCHED THEN INSERT
                (order_id, customer_ref, amount, currency, order_status, updated_at)
            VALUES
                (source.order_id, source.customer_ref, source.amount, source.currency,
                    source.order_status, source.updated_at)
        """

    def verify_visibility(self, manifest: SegmentManifest, acceptance_key: int) -> VisibilityProof:
        target = self._qualified(self._settings.target_table)
        with (
            _classified("Snowflake visibility query failed"),
            closing(self._connection()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT order_id, customer_ref, amount, currency, order_status, updated_at "
                f"FROM {target} WHERE order_id = %s",
                (acceptance_key,),
            )
            row = cursor.fetchone()
            query_id = cursor.sfqid
        if row is None:
            raise ProviderError("acceptance key is not visible in Snowflake", "permanent")
        value_digest = digest(
            OrderRow.model_validate(
                dict(
                    zip(
                        ("order_id", "customer_ref", "amount", "currency", "status", "updated_at"),
                        row,
                        strict=True,
                    )
                )
            )
        )
        if value_digest != manifest.acceptance_value_digest:
            raise ProviderError("acceptance row value digest does not match manifest", "permanent")
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=value_digest,
            query_id=query_id,
            verified_at=self._clock(),
        )
