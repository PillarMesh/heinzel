from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlparse

import httpx
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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class ClickHouseLandStoreError(RuntimeError):
    def __init__(self, classification: ProviderErrorClassification) -> None:
        super().__init__("ClickHouse destination driver failed")
        self.classification = classification


class ClickHouseLandStoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    endpoint: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr
    raw_database_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    ledger_database_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    ledger_table_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    verify_tls: bool = True

    @field_validator("endpoint")
    @classmethod
    def requires_secure_or_loopback_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.scheme != "https" and not loopback_http)
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ClickHouse endpoint must be HTTPS or loopback HTTP")
        return value.rstrip("/")


class _ClickHouseLandClient(Protocol):
    def execute(self, statement: str) -> bytes: ...

    def query_lines(self, statement: str) -> tuple[str, ...]: ...

    def close(self) -> None: ...


class _ClickHouseLandStore(Protocol):
    def insert_segment(
        self,
        *,
        receipt: LandReceipt,
        rows: tuple[bytes, ...],
        insert_token: str,
    ) -> None: ...

    def record_receipt(self, *, receipt: LandReceipt) -> LandReceipt: ...

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None: ...

    def inspect_segment(self, *, receipt: LandReceipt, insert_token: str) -> bool: ...


class ClickHouseLandStoreSettingsAuthority(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        binding_revision: int,
    ) -> ClickHouseLandStoreSettings: ...


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class _ClickHouseHTTPClient:
    def __init__(self, settings: ClickHouseLandStoreSettings) -> None:
        self._client = httpx.Client(
            base_url=settings.endpoint,
            auth=(settings.username, settings.password.get_secret_value()),
            headers={"content-type": "text/plain; charset=utf-8"},
            timeout=10,
            trust_env=False,
            verify=settings.verify_tls,
        )

    def execute(self, statement: str) -> bytes:
        try:
            response = self._client.post("/", content=statement.encode())
        except httpx.TransportError:
            raise ClickHouseLandStoreError("transient_transport") from None
        if response.status_code in {401, 403}:
            raise ClickHouseLandStoreError("authorization_denied")
        if response.status_code == 429:
            raise ClickHouseLandStoreError("throttled")
        if response.status_code >= 500:
            raise ClickHouseLandStoreError("transient_unavailable")
        if response.status_code != 200:
            raise ClickHouseLandStoreError("statement_rejected")
        return response.content

    def query_lines(self, statement: str) -> tuple[str, ...]:
        payload = self.execute(statement.rstrip().rstrip(";") + " FORMAT TabSeparatedRaw")
        try:
            return tuple(payload.decode().splitlines())
        except UnicodeDecodeError:
            raise ClickHouseLandStoreError("invalid_provider_response") from None

    def close(self) -> None:
        self._client.close()


def _clickhouse_row(row: bytes) -> str:
    try:
        decoded = json.loads(row)
    except (UnicodeError, json.JSONDecodeError):
        raise ClickHouseLandStoreError("invalid_provider_response") from None
    if not isinstance(decoded, dict) or canonical_bytes(decoded) != row:
        raise ClickHouseLandStoreError("invalid_provider_response")
    return row.decode("utf-8")


class ClickHouseLandStore:
    """Credential-scoped ClickHouse raw insert and reconciled receipt store."""

    def __init__(
        self,
        settings: ClickHouseLandStoreSettings,
        *,
        client: _ClickHouseLandClient | None = None,
        after_insert_hook: Callable[[], None] = lambda: None,
    ) -> None:
        self._settings = settings
        self._client = client or _ClickHouseHTTPClient(settings)
        self._after_insert_hook = after_insert_hook

    def insert_segment(
        self,
        *,
        receipt: LandReceipt,
        rows: tuple[bytes, ...],
        insert_token: str,
    ) -> None:
        envelopes = tuple(
            json.dumps(
                {
                    "generation_id": receipt.generation_id,
                    "row_ordinal": row_ordinal,
                    "segment_digest": receipt.segment_digest,
                    "insert_token": insert_token,
                    "payload": _clickhouse_row(row),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            for row_ordinal, row in enumerate(rows)
        )
        statement = (
            f"INSERT INTO `{self._settings.raw_database_name}`.`{receipt.target_table_ref}` "
            "(generation_id, row_ordinal, segment_digest, insert_token, payload) "
            f"SETTINGS insert_deduplication_token={_clickhouse_string(insert_token)} "
            "FORMAT JSONEachRow\n" + "\n".join(envelopes)
        )
        self._client.execute(statement)
        self._after_insert_hook()

    def record_receipt(self, *, receipt: LandReceipt) -> LandReceipt:
        payload = json.dumps(
            {
                "idempotency_key": receipt.idempotency_key,
                "generation_id": receipt.generation_id,
                "receipt_payload": receipt.model_dump_json(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._client.execute(
            f"INSERT INTO `{self._settings.ledger_database_name}`."
            f"`{self._settings.ledger_table_name}` FORMAT JSONEachRow\n{payload}"
        )
        return receipt

    def inspect_receipt(self, *, idempotency_key: str) -> LandReceipt | None:
        rows = self._client.query_lines(
            f"SELECT receipt_payload FROM `{self._settings.ledger_database_name}`."
            f"`{self._settings.ledger_table_name}` WHERE idempotency_key = "
            f"{_clickhouse_string(idempotency_key)} ORDER BY generation_id LIMIT 1"
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ClickHouseLandStoreError("integrity_failure")
        try:
            return LandReceipt.model_validate_json(rows[0], strict=True)
        except (TypeError, ValueError):
            raise ClickHouseLandStoreError("integrity_failure") from None

    def inspect_segment(self, *, receipt: LandReceipt, insert_token: str) -> bool:
        rows = self._client.query_lines(
            "SELECT count(), uniqExact(segment_digest), min(segment_digest), "
            f"min(insert_token) FROM `{self._settings.raw_database_name}`."
            f"`{receipt.target_table_ref}` WHERE generation_id = "
            f"{_clickhouse_string(receipt.generation_id)}"
        )
        if len(rows) != 1:
            raise ClickHouseLandStoreError("invalid_provider_response")
        parts = rows[0].split("\t")
        if len(parts) != 4:
            raise ClickHouseLandStoreError("invalid_provider_response")
        try:
            count = int(parts[0])
            unique_digests = int(parts[1])
        except ValueError:
            raise ClickHouseLandStoreError("invalid_provider_response") from None
        if count == 0:
            return False
        return (
            count == receipt.record_count
            and unique_digests == 1
            and parts[2] == receipt.segment_digest
            and parts[3] == insert_token
        )

    def close(self) -> None:
        with suppress(Exception):
            self._client.close()


def _clock() -> datetime:
    return datetime.now(UTC)


def clickhouse_insert_token(*, idempotency_key: str, segment_digest: str) -> str:
    return digest(
        {
            "domain": "pillarmesh-clickhouse-insert-token-v1",
            "idempotency_key": idempotency_key,
            "segment_digest": segment_digest,
        }
    )


def _same_request(left: LandReceipt, right: LandReceipt) -> bool:
    return left.model_dump(exclude={"committed_at"}) == right.model_dump(exclude={"committed_at"})


class ClickHouseDestinationProvider:
    provider_kind: Literal["clickhouse"] = "clickhouse"

    def __init__(
        self,
        *,
        store: _ClickHouseLandStore,
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
            existing = self._store.inspect_receipt(idempotency_key=idempotency_key)
            if existing is not None:
                if not _same_request(existing, receipt):
                    raise ProviderError(
                        "ClickHouse LAND idempotency key conflicts with a different generation",
                        "integrity_failure",
                    )
                return existing
            insert_token = clickhouse_insert_token(
                idempotency_key=idempotency_key,
                segment_digest=segment.segment_digest,
            )
            if self._store.inspect_segment(receipt=receipt, insert_token=insert_token):
                return self._store.record_receipt(receipt=receipt)
            self._store.insert_segment(
                receipt=receipt,
                rows=segment.rows,
                insert_token=insert_token,
            )
            committed = self._store.record_receipt(receipt=receipt)
        except ClickHouseLandStoreError as error:
            raise ProviderError(str(error), error.classification) from None
        except TimeoutError:
            try:
                existing = self._store.inspect_receipt(idempotency_key=idempotency_key)
                if existing is not None and _same_request(existing, receipt):
                    return existing
                insert_token = clickhouse_insert_token(
                    idempotency_key=idempotency_key,
                    segment_digest=segment.segment_digest,
                )
                if self._store.inspect_segment(receipt=receipt, insert_token=insert_token):
                    reconciled = self._store.record_receipt(receipt=receipt)
                    if _same_request(reconciled, receipt):
                        return reconciled
            except ClickHouseLandStoreError as error:
                raise ProviderError(str(error), error.classification) from None
            except OSError:
                raise ProviderError(
                    "ClickHouse LAND reconciliation transport failed",
                    "transient_transport",
                ) from None
            raise ProviderError(
                "ClickHouse LAND outcome is ambiguous",
                "ambiguous_outcome",
            ) from None
        except OSError:
            raise ProviderError("ClickHouse LAND transport failed", "transient_transport") from None
        except ProviderError:
            raise
        if not _same_request(committed, receipt):
            raise ProviderError(
                "ClickHouse LAND idempotency key conflicts with a different generation",
                "integrity_failure",
            )
        return committed

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation:
        try:
            stored = self._store.inspect_receipt(idempotency_key=receipt.idempotency_key)
        except ClickHouseLandStoreError as error:
            raise ProviderError(str(error), error.classification) from None
        except OSError:
            raise ProviderError(
                "ClickHouse commit inspection transport failed",
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
        insert_token = clickhouse_insert_token(
            idempotency_key=idempotency_key,
            segment_digest=segment.segment_digest,
        )
        return LandReceipt(
            receipt_id=digest(
                {
                    "domain": "pillarmesh-clickhouse-land-receipt-v1",
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
            provider_commit_ref=insert_token,
            committed_at=self._clock(),
        )

    @staticmethod
    def _validate(*, segment: StagedSegment, target: RawGenerationTarget) -> None:
        if not segment.rows:
            raise ProviderError("ClickHouse cannot LAND an empty segment", "statement_rejected")
        if segment.schema_digest != target.schema_digest:
            raise ProviderError("ClickHouse LAND schema mismatch", "statement_rejected")


def compose_clickhouse_destination_provider(
    *,
    binding: WarehouseBinding,
    settings_authority: ClickHouseLandStoreSettingsAuthority,
    client: _ClickHouseLandClient | None = None,
) -> ClickHouseDestinationProvider:
    if (
        binding.engine_kind is not EngineKind.CLICKHOUSE
        or binding.lifecycle_state is not WarehouseBindingState.READY
    ):
        raise ProviderError(
            "ClickHouse destination binding is not authorized for LAND",
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
            "ClickHouse destination settings resolution failed",
            "authorization_denied",
        ) from None
    return ClickHouseDestinationProvider(
        store=ClickHouseLandStore(settings, client=client),
    )
