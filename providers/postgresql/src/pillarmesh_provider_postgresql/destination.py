from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

import psycopg
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    CommitObservation,
    IdempotencyKey,
    LandReceipt,
    ProviderError,
    RawGenerationTarget,
    StagedSegment,
    raw_generation_key,
)
from pillarmesh_provider_sdk.errors import ProviderErrorClassification
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .startup_denial import (
    StartupDenialProbe,
    connect_attributing_startup_denial,
    default_startup_denial_probe,
)


class PostgreSQLLandStoreError(RuntimeError):
    def __init__(self, classification: ProviderErrorClassification) -> None:
        super().__init__("PostgreSQL destination driver failed")
        self.classification = classification


class _PostgreSQLCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...


class _PostgreSQLConnection(Protocol):
    def execute(
        self,
        query: object,
        params: tuple[object, ...] = (),
    ) -> _PostgreSQLCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


type _Connect = Callable[[str], _PostgreSQLConnection]


class PostgreSQLLandStoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dsn: SecretStr
    raw_schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    ledger_schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    ledger_table_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")


class _PostgreSQLLandStore(Protocol):
    """Executes the rows and receipt in one PostgreSQL transaction."""

    def land_transactionally(
        self,
        *,
        receipt: LandReceipt,
        rows: tuple[bytes, ...],
    ) -> LandReceipt: ...

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None: ...


class PostgreSQLLandStoreSettingsAuthority(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> PostgreSQLLandStoreSettings: ...


def _postgresql_store_error(error: psycopg.Error) -> PostgreSQLLandStoreError:
    sqlstate = error.sqlstate or ""
    # Authorization rejections are OperationalError subclasses, so they must be matched before the
    # generic transport check or a rejected credential would be retried.
    if sqlstate.startswith("28") or sqlstate == "42501":
        classification: ProviderErrorClassification = "authorization_denied"
    elif isinstance(error, (psycopg.InterfaceError, psycopg.OperationalError)):
        classification = "transient_transport"
    elif sqlstate.startswith("23"):
        classification = "integrity_failure"
    elif sqlstate.startswith(("53", "57", "58")):
        classification = "transient_unavailable"
    else:
        classification = "statement_rejected"
    return PostgreSQLLandStoreError(classification)


def _canonical_row_text(row: bytes) -> str:
    try:
        decoded = json.loads(row)
    except (UnicodeError, json.JSONDecodeError):
        raise PostgreSQLLandStoreError("invalid_provider_response") from None
    if not isinstance(decoded, dict) or canonical_bytes(decoded) != row:
        raise PostgreSQLLandStoreError("invalid_provider_response")
    return row.decode("utf-8")


class PostgreSQLLandStore:
    """Credential-scoped PostgreSQL rows and receipt transaction."""

    def __init__(
        self,
        settings: PostgreSQLLandStoreSettings,
        *,
        connect: _Connect | None = None,
        startup_denial_probe: StartupDenialProbe | None = None,
        after_commit_hook: Callable[[], None] = lambda: None,
    ) -> None:
        self._settings = settings
        self._connect = connect or cast(_Connect, psycopg.connect)
        self._startup_denial_probe = default_startup_denial_probe(
            connect=connect, probe=startup_denial_probe
        )
        self._after_commit_hook = after_commit_hook

    def land_transactionally(
        self,
        *,
        receipt: LandReceipt,
        rows: tuple[bytes, ...],
    ) -> LandReceipt:
        connection: _PostgreSQLConnection | None = None
        committed = False
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            connection.execute("BEGIN")
            existing = self._load_receipt(connection, receipt.idempotency_key)
            if existing is not None:
                connection.commit()
                committed = True
                return existing
            insert_row = sql.SQL(
                "INSERT INTO {}.{} "
                "(generation_id, row_ordinal, segment_digest, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)"
            ).format(
                sql.Identifier(self._settings.raw_schema_name),
                sql.Identifier(receipt.target_table_ref),
            )
            for row_ordinal, row in enumerate(rows):
                connection.execute(
                    insert_row,
                    (
                        receipt.generation_id,
                        row_ordinal,
                        receipt.segment_digest,
                        _canonical_row_text(row),
                    ),
                )
            insert_receipt = sql.SQL(
                "INSERT INTO {}.{} (idempotency_key, generation_id, receipt_payload) "
                "VALUES (%s, %s, %s::jsonb)"
            ).format(
                sql.Identifier(self._settings.ledger_schema_name),
                sql.Identifier(self._settings.ledger_table_name),
            )
            connection.execute(
                insert_receipt,
                (receipt.idempotency_key, receipt.generation_id, receipt.model_dump_json()),
            )
            connection.commit()
            committed = True
            self._after_commit_hook()
            return receipt
        except psycopg.Error as error:
            raise _postgresql_store_error(error) from None
        finally:
            if connection is not None and not committed:
                with suppress(Exception):
                    connection.rollback()
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None:
        connection: _PostgreSQLConnection | None = None
        try:
            connection = connect_attributing_startup_denial(
                self._connect,
                self._settings.dsn.get_secret_value(),
                probe=self._startup_denial_probe,
            )
            return self._load_receipt(connection, idempotency_key)
        except psycopg.Error as error:
            raise _postgresql_store_error(error) from None
        finally:
            if connection is not None:
                with suppress(Exception):
                    connection.close()

    def _load_receipt(
        self,
        connection: _PostgreSQLConnection,
        idempotency_key: str,
    ) -> LandReceipt | None:
        query = sql.SQL(
            "SELECT receipt_payload::text FROM {}.{} WHERE idempotency_key = %s"
        ).format(
            sql.Identifier(self._settings.ledger_schema_name),
            sql.Identifier(self._settings.ledger_table_name),
        )
        row = connection.execute(query, (idempotency_key,)).fetchone()
        if row is None:
            return None
        payload = row[0]
        if not isinstance(payload, (str, bytes, bytearray)):
            raise PostgreSQLLandStoreError("integrity_failure")
        try:
            return LandReceipt.model_validate_json(payload, strict=True)
        except (TypeError, ValueError):
            raise PostgreSQLLandStoreError("integrity_failure") from None


def _clock() -> datetime:
    return datetime.now(UTC)


def _same_request(left: LandReceipt, right: LandReceipt) -> bool:
    return left.model_dump(exclude={"committed_at"}) == right.model_dump(exclude={"committed_at"})


class PostgreSQLDestinationProvider:
    provider_kind: Literal["postgresql"] = "postgresql"

    def __init__(
        self,
        *,
        store: _PostgreSQLLandStore,
        clock: Callable[[], datetime] = _clock,
    ) -> None:
        self._store = store
        self._clock = clock

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
    ) -> LandReceipt:
        self._validate(segment=segment, target=target)
        receipt = self._receipt(
            segment=segment,
            target=target,
            idempotency_key=idempotency_key,
        )
        try:
            committed = self._store.land_transactionally(receipt=receipt, rows=segment.rows)
        except PostgreSQLLandStoreError as error:
            raise ProviderError(str(error), error.classification) from None
        except TimeoutError:
            try:
                existing = self._store.inspect_receipt(idempotency_key=idempotency_key)
            except PostgreSQLLandStoreError as error:
                raise ProviderError(str(error), error.classification) from None
            except OSError:
                raise ProviderError(
                    "PostgreSQL LAND reconciliation transport failed",
                    "transient_transport",
                ) from None
            if existing is not None and _same_request(existing, receipt):
                return existing
            raise ProviderError(
                "PostgreSQL LAND outcome is ambiguous",
                "ambiguous_outcome",
            ) from None
        except OSError:
            raise ProviderError(
                "PostgreSQL LAND transport failed",
                "transient_transport",
            ) from None
        except ProviderError:
            raise
        if not _same_request(committed, receipt):
            raise ProviderError(
                "PostgreSQL LAND idempotency key conflicts with a different generation",
                "integrity_failure",
            )
        return committed

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation:
        try:
            stored = self._store.inspect_receipt(idempotency_key=receipt.idempotency_key)
        except PostgreSQLLandStoreError as error:
            raise ProviderError(str(error), error.classification) from None
        except OSError:
            raise ProviderError(
                "PostgreSQL commit inspection transport failed",
                "transient_transport",
            ) from None
        if stored is None or not _same_request(stored, receipt):
            return CommitObservation(outcome="not_found", observed_at=self._clock())
        return CommitObservation(
            outcome="committed",
            receipt_digest=digest(stored),
            observed_at=self._clock(),
        )

    def _receipt(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
    ) -> LandReceipt:
        generation_id = raw_generation_key(target=target, segment_digest=segment.segment_digest)
        return LandReceipt(
            receipt_id=digest(
                {
                    "domain": "pillarmesh-postgresql-land-receipt-v1",
                    "generation_id": generation_id,
                    "idempotency_key": idempotency_key,
                }
            ),
            idempotency_key=idempotency_key,
            tenant_id=target.tenant_id,
            contract_ref=target.contract_ref,
            contract_revision=target.contract_revision,
            trigger_window=target.trigger_window,
            destination_binding_ref=target.destination_binding_ref,
            logical_object_ref=target.logical_object_ref,
            target_table_ref=target.table_ref,
            generation_id=generation_id,
            segment_digest=segment.segment_digest,
            schema_digest=segment.schema_digest,
            record_count=segment.record_count,
            provider_commit_ref=generation_id,
            committed_at=self._clock(),
        )

    @staticmethod
    def _validate(*, segment: StagedSegment, target: RawGenerationTarget) -> None:
        if not segment.rows:
            raise ProviderError("PostgreSQL cannot LAND an empty segment", "statement_rejected")
        if segment.schema_digest != target.schema_digest:
            raise ProviderError("PostgreSQL LAND schema mismatch", "statement_rejected")


def compose_postgresql_destination_provider(
    *,
    binding: WarehouseBinding,
    settings_authority: PostgreSQLLandStoreSettingsAuthority,
    connect: _Connect | None = None,
) -> PostgreSQLDestinationProvider:
    if (
        binding.engine_kind is not EngineKind.POSTGRESQL
        or binding.lifecycle_state is not WarehouseBindingState.READY
    ):
        raise ProviderError(
            "PostgreSQL destination binding is not authorized for LAND",
            "authorization_denied",
        )
    try:
        settings = settings_authority.resolve(
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
        )
    except ProviderError:
        raise
    except Exception:
        raise ProviderError(
            "PostgreSQL destination settings resolution failed",
            "authorization_denied",
        ) from None
    return PostgreSQLDestinationProvider(
        store=PostgreSQLLandStore(settings, connect=connect),
    )
