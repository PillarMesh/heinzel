from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import psycopg
from heinzel_connection_broker import (
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_evidence import SQLiteAcquisitionEvidenceWriter, SQLiteStore
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSourceObservation,
    SourceObservationRequest,
    acquisition_intent_key,
    encode_canonical_jsonl,
)
from heinzel_runtime import (
    AcquisitionCeilingExceeded,
    AcquisitionDeclaredActivation,
    AcquisitionDriftError,
    AcquisitionOwnershipError,
    AcquisitionPreparationResult,
    AcquisitionRunner,
    AcquisitionStaleRevision,
    ActivatedAcquisitionContract,
    ReferenceFactory,
    compose_activated_acquisition_contract,
    opaque_reference_factory,
)
from heinzel_state import (
    AcquisitionStateNotFoundError,
    LocalAcquisitionArtifactStore,
    SQLiteAcquisitionStateRepository,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_OBJECT_REFS = ("account_segments", "orders", "subscriptions")
_CONTRACT_REF = "contract:managed-business-data:v1"
_CONTRACT_DIGEST = "c" * 64
_BINDING_REF = "source-binding:managed-postgresql"
_OBSERVATION_REF = "source-observation:managed-postgresql"
_CONSUMER_REF = "strict-source-acknowledgement-consumer"
_CAPABILITY_DIGEST = "a" * 64
_CURSOR_LAG = timedelta(minutes=5)


class SourceAcquisitionAcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    source_provider: Literal["postgresql"] = "postgresql"
    object_refs: tuple[str, ...]
    tenant_isolation_proven: bool
    replay_proven: bool
    exact_acknowledgement_proven: bool
    stale_and_cross_tenant_denial_proven: bool
    late_commit_proven: bool
    drift_denial_proven: bool
    least_privilege_proven: bool
    ceiling_refusal_proven: bool
    privacy_proven: bool
    destination_provider_resolutions: int = Field(ge=0)
    destination_writes: int = Field(ge=0)
    delivery_claimed: Literal[False] = False

    @model_validator(mode="after")
    def requires_source_only_success(self) -> Self:
        if self.object_refs != _OBJECT_REFS:
            raise ValueError("Plan 4A object scope is incomplete")
        if self.destination_provider_resolutions or self.destination_writes:
            raise ValueError("Plan 4A cannot claim a destination effect")
        return self


@dataclass(frozen=True, slots=True)
class SourceAcquisitionJourney:
    report: SourceAcquisitionAcceptanceReport
    tenant_a_initial: AcquisitionPreparationResult
    tenant_a_replay: AcquisitionPreparationResult
    tenant_b_initial: AcquisitionPreparationResult
    initial_checkpoint: AcquisitionCheckpointReceipt
    acknowledgement_replay: AcquisitionCheckpointReceipt
    late_checkpoint: AcquisitionCheckpointReceipt
    first_incremental_batch_id: str
    late_incremental_batch_id: str
    provider_object_identities: dict[str, tuple[str, ...]]
    denial_reason_codes: dict[str, str]
    artifact_store: LocalAcquisitionArtifactStore
    consumer: _StrictAcknowledgementConsumer
    provider_resolutions_before_replay: int
    provider_resolutions_at_replay: int
    artifact_count_before_ceiling: int
    artifact_count_after_ceiling: int
    checkpoint_revision_before_ceiling: int
    checkpoint_revision_after_ceiling: int
    destination_provider_resolutions: int
    destination_writes: int
    private_canaries: tuple[bytes, ...]
    public_payload: bytes


@dataclass(slots=True)
class _Table:
    logical_object_ref: str
    relation_oid: str
    fields: tuple[AcquisitionField, ...]
    rows: list[tuple[object, ...]]

    @property
    def key_name(self) -> str:
        return self.fields[0].name

    @property
    def updated_at_name(self) -> str:
        return self.fields[-1].name

    @property
    def columns(self) -> list[tuple[object, ...]]:
        type_metadata = {
            "boolean": ("boolean", "bool"),
            "decimal": ("numeric", "numeric"),
            "integer": ("bigint", "int8"),
            "string": ("text", "text"),
            "timestamp": ("timestamp with time zone", "timestamptz"),
        }
        return [
            (
                field.name,
                type_metadata[field.value_type][0],
                type_metadata[field.value_type][1],
                "YES" if field.nullable else "NO",
            )
            for field in self.fields
        ]


@dataclass(frozen=True, slots=True)
class _PendingWrite:
    logical_object_ref: str
    row: tuple[object, ...]
    xact_start: datetime


class _FakePostgreSQLDatabase:
    def __init__(self) -> None:
        self.database_name = "provider-database-canary"
        self.snapshot_identity = "provider-snapshot-canary"
        self.dsn = "postgresql://reader:credential-canary@private-host-canary/source"
        self.now = _START
        self.write_capability = False
        self.pending_write: _PendingWrite | None = None
        self.connections: list[_FakeConnection] = []
        self.tables = _initial_tables()

    def connect(self, _dsn: str) -> _FakeConnection:
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection

    def attempt_write(self) -> None:
        if not self.write_capability:
            raise psycopg.errors.InsufficientPrivilege("write denied")

    def insert(self, logical_object_ref: str, row: tuple[object, ...]) -> None:
        self.tables[logical_object_ref].rows.append(row)

    def open_writer(
        self,
        logical_object_ref: str,
        row: tuple[object, ...],
        *,
        xact_start: datetime,
    ) -> None:
        if self.pending_write is not None:
            raise RuntimeError("writer already open")
        self.pending_write = _PendingWrite(logical_object_ref, row, xact_start)

    def commit_writer(self) -> None:
        if self.pending_write is None:
            raise RuntimeError("writer is not open")
        self.insert(self.pending_write.logical_object_ref, self.pending_write.row)
        self.pending_write = None


class _FakeConnection:
    def __init__(self, database: _FakePostgreSQLDatabase) -> None:
        self.database = database
        self.snapshot_rows = {
            object_ref: list(table.rows) for object_ref, table in database.tables.items()
        }
        self.current_table: _Table | None = None
        self.calls: list[tuple[str, object, str | None]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, query: str) -> None:
        self.calls.append((query, None, None))

    def cursor(self, name: str | None = None) -> _FakeCursor:
        return _FakeCursor(self, name=name)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True

    def table_from_query(self, rendered: str) -> _Table:
        matches = [
            table
            for table in self.database.tables.values()
            if f'"{table.logical_object_ref}"' in rendered
        ]
        if len(matches) != 1:
            raise RuntimeError("fake driver could not resolve one declared table")
        return matches[0]


class _FakeCursor:
    def __init__(self, connection: _FakeConnection, *, name: str | None) -> None:
        self.connection = connection
        self.name = name
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(self, query: object, params: object = None) -> None:
        rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.connection.calls.append((rendered, params, self.name))
        if self.name is not None:
            self._execute_record_query(rendered, params)
            return
        database = self.connection.database
        if "FROM pg_catalog.pg_class c" in rendered:
            if not isinstance(params, tuple) or len(params) != 2:
                raise RuntimeError("invalid relation lookup")
            table = database.tables[str(params[1])]
            self.connection.current_table = table
            self.rows = [(table.relation_oid, database.database_name, "r")]
        elif "information_schema.columns" in rendered:
            table = self._current_table()
            self.rows = table.columns
        elif "pg_catalog.pg_constraint" in rendered:
            table = self._current_table()
            self.rows = [(table.key_name, "primary_key", True, "btree", 1)]
        elif "rolsuper" in rendered:
            table = self._current_table()
            exact_projection = (
                isinstance(params, tuple)
                and len(params) == 5
                and params[0] == table.relation_oid
                and params[1] == [field.name for field in table.fields]
                and params[2] == ["source", "source", "source"]
                and params[3] == list(_OBJECT_REFS)
                and params[4] == "private_unrelated"
            )
            self.rows = [(exact_projection and not database.write_capability,)]
        elif "pg_current_snapshot" in rendered:
            self.rows = [(database.snapshot_identity,)]
        elif "transaction_timestamp()" in rendered:
            self.rows = [(database.now,)]
        elif "pg_catalog.pg_stat_activity" in rendered:
            oldest = database.pending_write.xact_start if database.pending_write else None
            self.rows = [(oldest,)]
        elif "snapshot_cursor_upper" in rendered:
            self.rows = self._upper_at_timestamp(rendered, params)
        elif "snapshot_bounds_before_first" in rendered:
            self.rows = [(None, None, 0)]
        elif "snapshot_bounds" in rendered:
            self.rows = [self._bounded_snapshot_counts(rendered, params)]
        elif "incremental_upper" in rendered:
            self.rows = self._upper_at_timestamp(rendered, params)
        elif "incremental_count_before_first" in rendered:
            self.rows = [(len(self._incremental_rows(rendered, params, before_first=True)),)]
        elif "incremental_count" in rendered:
            self.rows = [(len(self._incremental_rows(rendered, params, before_first=False)),)]
        elif "SELECT min" in rendered:
            table = self.connection.table_from_query(rendered)
            keys = [
                _row_key(row) for row in self.connection.snapshot_rows[table.logical_object_ref]
            ]
            self.rows = [(min(keys), max(keys), len(keys))] if keys else [(None, None, 0)]
        else:
            raise RuntimeError("fake driver received an unsupported query")

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self.rows)

    def close(self) -> None:
        return None

    def _current_table(self) -> _Table:
        if self.connection.current_table is None:
            raise RuntimeError("metadata table was not selected")
        return self.connection.current_table

    def _source_rows(self, rendered: str) -> tuple[_Table, list[tuple[object, ...]]]:
        table = self.connection.table_from_query(rendered)
        return table, self.connection.snapshot_rows[table.logical_object_ref]

    def _upper_at_timestamp(self, rendered: str, params: object) -> list[tuple[object, ...]]:
        _table, rows = self._source_rows(rendered)
        if not isinstance(params, tuple) or len(params) != 1 or not isinstance(params[0], datetime):
            raise RuntimeError("invalid upper-bound parameters")
        bound = params[0]
        eligible = [
            _row_compound_key(row)
            for row in rows
            if (
                _row_updated_at(row) < bound
                if " < %s" in rendered
                else _row_updated_at(row) <= bound
            )
        ]
        return [max(eligible)] if eligible else []

    def _bounded_snapshot_counts(self, rendered: str, params: object) -> tuple[object, object, int]:
        _table, rows = self._source_rows(rendered)
        if not isinstance(params, tuple) or len(params) != 2:
            raise RuntimeError("invalid snapshot bounds")
        upper = (_require_datetime(params[0]), _require_integer(params[1]))
        keys = [_row_key(row) for row in rows if _row_compound_key(row) <= upper]
        return (min(keys), max(keys), len(keys)) if keys else (None, None, 0)

    def _incremental_rows(
        self,
        rendered: str,
        params: object,
        *,
        before_first: bool,
    ) -> list[tuple[object, ...]]:
        _table, rows = self._source_rows(rendered)
        if not isinstance(params, tuple):
            raise RuntimeError("invalid incremental bounds")
        if before_first and len(params) == 2:
            upper = (_require_datetime(params[0]), _require_integer(params[1]))
            return [row for row in rows if _row_compound_key(row) <= upper]
        if before_first and len(params) == 1:
            upper_timestamp = _require_datetime(params[0])
            return [
                row
                for row in rows
                if (
                    _row_updated_at(row) < upper_timestamp
                    if " < %s" in rendered
                    else _row_updated_at(row) <= upper_timestamp
                )
            ]
        if not before_first and len(params) == 4:
            lower = (_require_datetime(params[0]), _require_integer(params[1]))
            upper = (_require_datetime(params[2]), _require_integer(params[3]))
            return [row for row in rows if lower < _row_compound_key(row) <= upper]
        raise RuntimeError("invalid incremental bound shape")

    def _execute_record_query(self, rendered: str, params: object) -> None:
        _table, rows = self._source_rows(rendered)
        if "snapshot_rows_before_first" in rendered:
            if not isinstance(params, tuple) or len(params) != 1:
                raise RuntimeError("invalid before-first snapshot")
            upper_timestamp = _require_datetime(params[0])
            selected = [row for row in rows if _row_updated_at(row) <= upper_timestamp]
            self.rows = sorted(selected, key=_row_key)
        elif "snapshot_rows" in rendered:
            if not isinstance(params, tuple) or len(params) != 2:
                raise RuntimeError("invalid snapshot query")
            upper = (_require_datetime(params[0]), _require_integer(params[1]))
            selected = [row for row in rows if _row_compound_key(row) <= upper]
            self.rows = sorted(selected, key=_row_key)
        elif "incremental_rows_before_first" in rendered:
            self.rows = sorted(
                self._incremental_rows(rendered, params, before_first=True),
                key=_row_compound_key,
            )
        elif "incremental_rows" in rendered:
            self.rows = sorted(
                self._incremental_rows(rendered, params, before_first=False),
                key=_row_compound_key,
            )
        else:
            self.rows = sorted(rows, key=_row_key)


class _CursorCipher:
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        return b"cipher:" + tenant_id.encode() + b":" + plaintext[::-1]

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        prefix = b"cipher:" + tenant_id.encode() + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("cursor tenant mismatch")
        return ciphertext.removeprefix(prefix)[::-1]


class _StrictAcknowledgementConsumer:
    def __init__(
        self,
        artifact_store: LocalAcquisitionArtifactStore,
        *,
        clock: _MutableClock,
        reference_factory: ReferenceFactory,
    ) -> None:
        self._artifact_store = artifact_store
        self._clock = clock
        self._reference_factory = reference_factory
        self._records: dict[str, tuple[AcquisitionRecord, ...]] = {}
        self.destination_writes = 0

    def acknowledge(
        self,
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> AcquisitionAcknowledgement:
        receipt = preparation.prepared_receipt
        manifest = preparation.batch_manifest
        if receipt is None or manifest is None:
            raise ValueError("strict consumer requires a prepared acquisition")
        manifest_reader = self._artifact_store.open_verified(
            tenant_id=intent.tenant_id,
            artifact_digest=receipt.batch_manifest_digest,
        )
        try:
            manifest_payload = manifest_reader.read()
        finally:
            manifest_reader.close()
        admitted_manifest = AcquisitionBatchManifest.model_validate_json(
            manifest_payload,
            strict=True,
        )
        if canonical_bytes(admitted_manifest) != manifest_payload or admitted_manifest != manifest:
            raise ValueError("manifest artifact is not exact")

        records: list[AcquisitionRecord] = []
        for segment in manifest.segment_manifests:
            reader = self._artifact_store.open_verified(
                tenant_id=intent.tenant_id,
                artifact_digest=segment.content_digest,
            )
            try:
                payload = reader.read()
            finally:
                reader.close()
            if hashlib.sha256(payload).hexdigest() != segment.content_digest:
                raise ValueError("segment content digest mismatch")
            if len(payload) != segment.encoded_bytes:
                raise ValueError("segment encoded byte count mismatch")
            lines = payload.splitlines()
            if len(lines) != segment.record_count:
                raise ValueError("segment record count mismatch")
            segment_records: list[AcquisitionRecord] = []
            for line in lines:
                record = AcquisitionRecord.model_validate_json(line, strict=True)
                if canonical_bytes(record) != line:
                    raise ValueError("segment record is not canonical")
                segment_records.append(record)
                records.append(record)
            canonical_writer = io.BytesIO()
            encoded_segment = encode_canonical_jsonl(
                segment_records,
                canonical_writer,
                record_ceiling=segment.record_count,
                encoded_byte_ceiling=segment.encoded_bytes,
            )
            if canonical_writer.getvalue() != payload:
                raise ValueError("segment payload is not exact canonical JSONL")
            if (
                encoded_segment.content_digest != segment.content_digest
                or encoded_segment.record_set_digest != segment.record_set_digest
                or encoded_segment.record_count != segment.record_count
                or encoded_segment.encoded_bytes != segment.encoded_bytes
            ):
                raise ValueError("segment manifest integrity mismatch")
        if len(records) != manifest.total_record_count:
            raise ValueError("batch record count mismatch")
        self._records[manifest.batch_id] = tuple(records)
        consumer_receipt_digest = digest(
            {
                "domain": "strict-source-acknowledgement-v1",
                "batch_manifest_digest": receipt.batch_manifest_digest,
                "segment_record_set_digests": tuple(
                    segment.record_set_digest for segment in manifest.segment_manifests
                ),
            }
        )
        return AcquisitionAcknowledgement(
            acknowledgement_id=self._reference_factory("acknowledgement"),
            tenant_id=intent.tenant_id,
            consumer_ref=_CONSUMER_REF,
            contract_digest=intent.contract_digest,
            source_binding_ref=intent.source_binding_ref,
            batch_id=receipt.batch_id,
            batch_manifest_digest=receipt.batch_manifest_digest,
            prior_checkpoint_revision=intent.prior_checkpoint_revision,
            candidate_checkpoint_digest=receipt.candidate_checkpoint_digest,
            consumer_receipt_digest=consumer_receipt_digest,
            acknowledged_at=self._clock(),
        )

    def records_for(self, batch_id: str) -> tuple[AcquisitionRecord, ...]:
        return self._records[batch_id]


@dataclass(slots=True)
class _MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True, slots=True)
class _ContractActivation:
    """What contract-service asserts about this contract, in its own shape.

    The composition takes the lifecycle structurally, so the harness states the three
    fields rather than standing up a contract-service instance for a value it already
    knows.
    """

    tenant_id: str
    contract_digest: str
    lifecycle_state: str = "activated"


@dataclass(frozen=True, slots=True)
class _TenantAuthority:
    database: _FakePostgreSQLDatabase
    provider: PostgreSQLAcquisitionProvider
    observation: AcquisitionSourceObservation
    binding: SourceConnectionBinding
    contract: ActivatedAcquisitionContract


class _ForbiddenDestinationBoundary:
    def __init__(self) -> None:
        self.provider_resolutions = 0
        self.writes = 0


class OfflineSourceAcquisitionHarness:
    def __init__(self, work_dir: Path, *, check_same_thread: bool = True) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.work_dir, 0o700)
        self.clock = _MutableClock(_START)
        # The product's own allocator and evidence store, rather than a counter and a
        # list: this journey is the only place the acquisition runtime is driven end
        # to end offline, so a double here leaves the durable path unexercised.
        self.references = opaque_reference_factory()
        self.artifact_root = self.work_dir / "artifacts"
        self.artifact_store = LocalAcquisitionArtifactStore(self.artifact_root)
        self.state_path = self.work_dir / "acquisition-state.sqlite"
        self.state = SQLiteAcquisitionStateRepository(
            str(self.state_path),
            cipher=_CursorCipher(),
            reference_factory=self.references,
            connection_factory=lambda path: sqlite3.connect(
                path,
                check_same_thread=check_same_thread,
            ),
        )
        self.evidence_path = self.work_dir / "acquisition-evidence.sqlite"
        self.evidence_store = SQLiteStore.open(self.evidence_path)
        self.evidence = SQLiteAcquisitionEvidenceWriter(self.evidence_store)
        self.consumer = _StrictAcknowledgementConsumer(
            self.artifact_store,
            clock=self.clock,
            reference_factory=self.references,
        )
        self.destination = _ForbiddenDestinationBoundary()
        self.authorities: dict[str, _TenantAuthority] = {}
        self.observations: dict[tuple[str, str], AcquisitionSourceObservation] = {}
        self.source_provider_resolutions = 0
        self.private_boundaries: dict[tuple[str, str], bytes] = {}
        self.runner = AcquisitionRunner(
            binding_resolver=self.load_binding,
            contract_resolver=self._resolve_contract,
            observation_resolver=self.load_observation,
            provider_resolver=self.resolve_provider,
            state_store=self.state,
            artifact_store=self.artifact_store,
            evidence_writer=self.evidence,
            reference_factory=self.references,
            clock=self.clock,
        )

    def register_tenant(
        self, tenant_id: str, *, product_intent_ref: ArtifactReference | None = None
    ) -> _TenantAuthority:
        """Compose this tenant's managed source authority, bound to an approved intent if given."""
        database = _FakePostgreSQLDatabase()
        provider = PostgreSQLAcquisitionProvider(
            _provider_settings(database),
            connect=database.connect,
            clock=self.clock,
            private_boundary_reference_factory=lambda tenant, object_ref: (
                f"private-boundary:{tenant}:{object_ref}"
            ),
            private_boundary_writer=lambda tenant, reference, payload: (
                self.private_boundaries.__setitem__((tenant, reference), payload)
            ),
        )
        observation = provider.observe_source(
            SourceObservationRequest(
                tenant_id=tenant_id,
                source_binding_ref=_BINDING_REF,
                object_refs=_OBJECT_REFS,
            )
        )
        schemas = _schemas(database)
        binding = SourceConnectionBinding(
            binding_id=_BINDING_REF,
            tenant_id=tenant_id,
            provider_kind="postgresql",
            connection_handle="opaque-postgresql-source-capability",
            account_mode="not_applicable",
            lifecycle_state=SourceConnectionBindingState.READY,
            approved_object_refs=_OBJECT_REFS,
            capability_profile_digest=_CAPABILITY_DIGEST,
            source_observation_ref=_OBSERVATION_REF,
            credential_revision=1,
            revision=1,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        # Composed from what the owning services assert rather than hand-built, so the
        # journey exercises the composition the product uses. The declared half is the
        # five fields no service publishes.
        contract = compose_activated_acquisition_contract(
            lifecycle=_ContractActivation(tenant_id=tenant_id, contract_digest=_CONTRACT_DIGEST),
            binding=binding,
            observation=observation,
            declared=AcquisitionDeclaredActivation(
                contract_ref=_CONTRACT_REF,
                process_package_ref=ArtifactReference(
                    artifact_id="process:managed-business-data",
                    version=1,
                    digest="1" * 64,
                ),
                product_intent_ref=product_intent_ref
                or ArtifactReference(
                    artifact_id="intent:managed-business-data",
                    version=1,
                    digest="2" * 64,
                ),
                destination_product_ref="product:managed-business-data",
                acknowledgement_consumer_ref=_CONSUMER_REF,
                object_schemas=schemas,
                record_ceiling=100,
                encoded_byte_ceiling=1_000_000,
                activated_by="acceptance-architect",
                activated_at=self.clock(),
            ),
        )
        authority = _TenantAuthority(database, provider, observation, binding, contract)
        self.authorities[tenant_id] = authority
        self.observations[(tenant_id, _OBSERVATION_REF)] = observation
        self.state.activate_contract_authority(tenant_id, _CONTRACT_DIGEST)
        return authority

    def intent(
        self,
        tenant_id: str,
        *,
        mode: Literal["snapshot", "incremental", "reconciliation"],
        run_label: str,
        record_ceiling: int = 100,
        encoded_byte_ceiling: int = 1_000_000,
    ) -> AcquisitionIntent:
        authority = self.authorities[tenant_id]
        if mode == "snapshot":
            revision = 0
            checkpoint_digest = None
        else:
            checkpoint = self.state.load_checkpoint(tenant_id, _CONTRACT_DIGEST, _BINDING_REF)
            revision = checkpoint.revision
            checkpoint_digest = checkpoint.cursor_digest
        run_intent_ref = digest(
            {
                "domain": "manual-source-acquisition-run-v1",
                "tenant_id": tenant_id,
                "label": run_label,
            }
        )
        return AcquisitionIntent(
            intent_key=acquisition_intent_key(
                tenant_id=tenant_id,
                run_intent_ref=run_intent_ref,
                contract_digest=_CONTRACT_DIGEST,
                source_binding_ref=_BINDING_REF,
                acquisition_mode=mode,
                object_refs=_OBJECT_REFS,
                prior_checkpoint_revision=revision,
            ),
            tenant_id=tenant_id,
            run_intent_ref=run_intent_ref,
            contract_ref=_CONTRACT_REF,
            contract_digest=_CONTRACT_DIGEST,
            source_binding_ref=_BINDING_REF,
            source_observation_digest=digest(authority.observation),
            acquisition_mode=mode,
            object_refs=_OBJECT_REFS,
            prior_checkpoint_revision=revision,
            prior_checkpoint_digest=checkpoint_digest,
            record_ceiling=record_ceiling,
            encoded_byte_ceiling=encoded_byte_ceiling,
            admitted_at=self.clock(),
        )

    def prepare(self, intent: AcquisitionIntent) -> AcquisitionPreparationResult:
        return self.runner.prepare(intent)

    def consume_and_acknowledge(
        self,
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> tuple[AcquisitionAcknowledgement, AcquisitionCheckpointReceipt]:
        acknowledgement = self.consumer.acknowledge(intent, preparation)
        return acknowledgement, self.runner.acknowledge(intent, acknowledgement)

    def artifact_count(self) -> int:
        return sum(path.is_file() for path in self.artifact_root.rglob("*"))

    def load_binding(self, tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
        authority = self.authorities[tenant_id]
        if binding_ref != authority.binding.binding_id:
            raise KeyError("binding not found")
        return authority.binding

    def load(self, tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
        """Expose the connection-broker reader shape used by runtime composition."""
        return self.load_binding(tenant_id, binding_ref)

    def _resolve_contract(self, tenant_id: str, contract_ref: str) -> ActivatedAcquisitionContract:
        authority = self.authorities[tenant_id]
        if contract_ref != authority.contract.contract_ref:
            raise KeyError("contract not found")
        return authority.contract

    def load_observation(
        self, tenant_id: str, observation_ref: str
    ) -> AcquisitionSourceObservation:
        return self.observations[(tenant_id, observation_ref)]

    def resolve_provider(self, binding: SourceConnectionBinding) -> PostgreSQLAcquisitionProvider:
        self.source_provider_resolutions += 1
        return self.authorities[binding.tenant_id].provider

    def close(self) -> None:
        self.state.close()
        self.evidence_store.close()


def execute_source_acquisition_journey(work_dir: Path) -> SourceAcquisitionJourney:
    harness = OfflineSourceAcquisitionHarness(work_dir)
    tenant_a = harness.register_tenant("tenant-a")
    harness.register_tenant("tenant-b")
    denial_reason_codes: dict[str, str] = {}

    try:
        tenant_a.database.attempt_write()
    except psycopg.errors.InsufficientPrivilege:
        write_was_denied = True
    else:
        write_was_denied = False
    tenant_a.database.write_capability = True
    try:
        tenant_a.provider.observe_source(
            SourceObservationRequest(
                tenant_id="tenant-a",
                source_binding_ref=_BINDING_REF,
                object_refs=_OBJECT_REFS,
            )
        )
    except AcquisitionProviderError as error:
        denial_reason_codes["write_capability"] = error.reason_code
    finally:
        tenant_a.database.write_capability = False

    tenant_a_initial_intent = harness.intent(
        "tenant-a", mode="snapshot", run_label="initial-snapshot"
    )
    tenant_a_initial = harness.prepare(tenant_a_initial_intent)
    try:
        harness.state.load_checkpoint("tenant-a", _CONTRACT_DIGEST, _BINDING_REF)
    except AcquisitionStateNotFoundError:
        unacknowledged_advanced_nothing = True
    else:
        unacknowledged_advanced_nothing = False
    provider_resolutions_before_replay = harness.source_provider_resolutions
    tenant_a_replay = harness.prepare(tenant_a_initial_intent)
    provider_resolutions_at_replay = harness.source_provider_resolutions
    tenant_a_acknowledgement, initial_checkpoint = harness.consume_and_acknowledge(
        tenant_a_initial_intent, tenant_a_initial
    )
    acknowledgement_replay = harness.runner.acknowledge(
        tenant_a_initial_intent, tenant_a_acknowledgement
    )

    tenant_b_initial_intent = harness.intent(
        "tenant-b", mode="snapshot", run_label="initial-snapshot"
    )
    tenant_b_initial = harness.prepare(tenant_b_initial_intent)
    _tenant_b_acknowledgement, tenant_b_checkpoint = harness.consume_and_acknowledge(
        tenant_b_initial_intent, tenant_b_initial
    )

    cross_tenant_acknowledgement = tenant_a_acknowledgement.model_copy(
        update={"tenant_id": "tenant-b", "acknowledgement_id": "acknowledgement:cross-tenant"}
    )
    try:
        harness.runner.acknowledge(tenant_a_initial_intent, cross_tenant_acknowledgement)
    except AcquisitionOwnershipError as error:
        denial_reason_codes["cross_tenant_acknowledgement"] = error.reason_code

    stale_intent = harness.intent("tenant-a", mode="incremental", run_label="stale-acknowledgement")
    stale_acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:stale",
        tenant_id="tenant-a",
        consumer_ref=_CONSUMER_REF,
        contract_digest=_CONTRACT_DIGEST,
        source_binding_ref=_BINDING_REF,
        batch_id="b" * 64,
        batch_manifest_digest="d" * 64,
        prior_checkpoint_revision=stale_intent.prior_checkpoint_revision,
        candidate_checkpoint_digest="e" * 64,
        consumer_receipt_digest="f" * 64,
        acknowledged_at=harness.clock(),
    )
    try:
        harness.runner.acknowledge(stale_intent, stale_acknowledgement)
    except AcquisitionStaleRevision as error:
        denial_reason_codes["stale_acknowledgement"] = error.reason_code

    harness.clock.value = datetime(2026, 9, 1, 12, 10, tzinfo=UTC)
    tenant_a.database.now = harness.clock()
    tenant_a.database.insert(
        "account_segments",
        (2, "provider-account-0002", "enterprise", datetime(2026, 9, 1, 12, 0, tzinfo=UTC)),
    )
    tenant_a.database.insert(
        "orders",
        (
            1003,
            "provider-account-0002",
            Decimal("25.00"),
            "first-incremental",
            datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        ),
    )
    tenant_a.database.insert(
        "subscriptions",
        (
            2002,
            "provider-account-0002",
            "canceled",
            datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        ),
    )
    tenant_a.database.open_writer(
        "orders",
        (
            1004,
            "provider-account-0003",
            Decimal("31.00"),
            "late-commit",
            datetime(2026, 9, 1, 12, 2, tzinfo=UTC),
        ),
        xact_start=datetime(2026, 9, 1, 12, 3, tzinfo=UTC),
    )
    first_incremental_intent = harness.intent(
        "tenant-a", mode="incremental", run_label="first-incremental"
    )
    first_incremental = harness.prepare(first_incremental_intent)
    _first_acknowledgement, first_incremental_checkpoint = harness.consume_and_acknowledge(
        first_incremental_intent, first_incremental
    )
    tenant_a.database.commit_writer()

    harness.clock.value = datetime(2026, 9, 1, 12, 20, tzinfo=UTC)
    tenant_a.database.now = harness.clock()
    late_incremental_intent = harness.intent(
        "tenant-a", mode="incremental", run_label="late-incremental"
    )
    late_incremental = harness.prepare(late_incremental_intent)
    _late_acknowledgement, late_checkpoint = harness.consume_and_acknowledge(
        late_incremental_intent, late_incremental
    )

    original_order_fields = tenant_a.database.tables["orders"].fields
    tenant_a.database.tables["orders"].fields = tuple(
        AcquisitionField(name=field.name, value_type="string", nullable=field.nullable)
        if field.name == "amount"
        else field
        for field in original_order_fields
    )
    drift_observation = tenant_a.provider.observe_source(
        SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref=_BINDING_REF,
            object_refs=_OBJECT_REFS,
        )
    )
    harness.observations[("tenant-a", _OBSERVATION_REF)] = drift_observation
    try:
        harness.prepare(harness.intent("tenant-a", mode="incremental", run_label="drift"))
    except AcquisitionDriftError as error:
        denial_reason_codes["source_drift"] = error.reason_code
    finally:
        tenant_a.database.tables["orders"].fields = original_order_fields
        harness.observations[("tenant-a", _OBSERVATION_REF)] = tenant_a.observation

    harness.clock.value = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    tenant_a.database.now = harness.clock()
    tenant_a.database.insert(
        "account_segments",
        (3, "provider-account-0003", "at-risk", datetime(2026, 9, 1, 12, 16, tzinfo=UTC)),
    )
    artifact_count_before_ceiling = harness.artifact_count()
    checkpoint_revision_before_ceiling = harness.state.load_checkpoint(
        "tenant-a", _CONTRACT_DIGEST, _BINDING_REF
    ).revision
    try:
        harness.prepare(
            harness.intent(
                "tenant-a",
                mode="incremental",
                run_label="encoded-byte-ceiling",
                encoded_byte_ceiling=1,
            )
        )
    except AcquisitionCeilingExceeded as error:
        denial_reason_codes["ceiling"] = error.reason_code
    artifact_count_after_ceiling = harness.artifact_count()
    checkpoint_revision_after_ceiling = harness.state.load_checkpoint(
        "tenant-a", _CONTRACT_DIGEST, _BINDING_REF
    ).revision

    provider_object_identities = {
        tenant_id: tuple(
            item.provider_observation.object_identity
            for item in authority.observation.object_observations
        )
        for tenant_id, authority in harness.authorities.items()
    }
    initial_a_manifest = _required_manifest(tenant_a_initial)
    initial_b_manifest = _required_manifest(tenant_b_initial)
    first_manifest = _required_manifest(first_incremental)
    late_manifest = _required_manifest(late_incremental)
    first_records = harness.consumer.records_for(first_manifest.batch_id)
    late_records = harness.consumer.records_for(late_manifest.batch_id)
    late_commit_proven = not _has_label(first_records, "late-commit") and _has_label(
        late_records, "late-commit"
    )

    private_canaries = (
        tenant_a.database.dsn.encode(),
        tenant_a.database.database_name.encode(),
        tenant_a.database.snapshot_identity.encode(),
        b"provider-account-0001",
        str(harness.artifact_root).encode(),
    )
    state_payload = harness.state_path.read_bytes()
    _checkpoint, plaintext_cursor = harness.state.load_checkpoint_with_cursor(
        "tenant-a", _CONTRACT_DIGEST, _BINDING_REF
    )
    cursor_private_at_rest = plaintext_cursor not in state_payload
    # Read back from the store rather than from what was written, so the payload
    # proves what a reader would actually get.
    public_evidence_payload = canonical_bytes(
        tuple(
            receipt
            for tenant_id in sorted(harness.authorities)
            for receipt in harness.evidence_store.list_acquisition_receipts(tenant_id)
        )
    )
    evidence_is_private = all(canary not in public_evidence_payload for canary in private_canaries)

    report = SourceAcquisitionAcceptanceReport(
        object_refs=_OBJECT_REFS,
        tenant_isolation_proven=(
            provider_object_identities["tenant-a"] == provider_object_identities["tenant-b"]
            and initial_a_manifest.batch_id != initial_b_manifest.batch_id
            and tenant_b_checkpoint.committed_revision == 1
        ),
        replay_proven=(
            tenant_a_replay.prepared_receipt == tenant_a_initial.prepared_receipt
            and tenant_a_replay.batch_manifest == tenant_a_initial.batch_manifest
            and provider_resolutions_before_replay == provider_resolutions_at_replay
        ),
        exact_acknowledgement_proven=(
            unacknowledged_advanced_nothing
            and initial_checkpoint.committed_revision == 1
            and acknowledgement_replay == initial_checkpoint
            and first_incremental_checkpoint.committed_revision == 2
            and late_checkpoint.committed_revision == 3
        ),
        stale_and_cross_tenant_denial_proven=(
            "stale_acknowledgement" in denial_reason_codes
            and "cross_tenant_acknowledgement" in denial_reason_codes
        ),
        late_commit_proven=late_commit_proven,
        drift_denial_proven="source_drift" in denial_reason_codes,
        least_privilege_proven=(
            write_was_denied
            and denial_reason_codes.get("write_capability") == "authorization_denied"
        ),
        ceiling_refusal_proven=(
            "ceiling" in denial_reason_codes
            and artifact_count_before_ceiling == artifact_count_after_ceiling
            and checkpoint_revision_before_ceiling == checkpoint_revision_after_ceiling
        ),
        privacy_proven=evidence_is_private and cursor_private_at_rest,
        destination_provider_resolutions=harness.destination.provider_resolutions,
        destination_writes=harness.destination.writes + harness.consumer.destination_writes,
    )
    public_payload = canonical_bytes(report)
    if any(canary in public_payload for canary in private_canaries):
        raise RuntimeError("Plan 4A report leaked a private source value")
    harness.state.close()
    return SourceAcquisitionJourney(
        report=report,
        tenant_a_initial=tenant_a_initial,
        tenant_a_replay=tenant_a_replay,
        tenant_b_initial=tenant_b_initial,
        initial_checkpoint=initial_checkpoint,
        acknowledgement_replay=acknowledgement_replay,
        late_checkpoint=late_checkpoint,
        first_incremental_batch_id=first_manifest.batch_id,
        late_incremental_batch_id=late_manifest.batch_id,
        provider_object_identities=provider_object_identities,
        denial_reason_codes=denial_reason_codes,
        artifact_store=harness.artifact_store,
        consumer=harness.consumer,
        provider_resolutions_before_replay=provider_resolutions_before_replay,
        provider_resolutions_at_replay=provider_resolutions_at_replay,
        artifact_count_before_ceiling=artifact_count_before_ceiling,
        artifact_count_after_ceiling=artifact_count_after_ceiling,
        checkpoint_revision_before_ceiling=checkpoint_revision_before_ceiling,
        checkpoint_revision_after_ceiling=checkpoint_revision_after_ceiling,
        destination_provider_resolutions=harness.destination.provider_resolutions,
        destination_writes=harness.destination.writes + harness.consumer.destination_writes,
        private_canaries=private_canaries,
        public_payload=public_payload,
    )


def run_source_acquisition(work_dir: Path) -> SourceAcquisitionAcceptanceReport:
    return execute_source_acquisition_journey(work_dir).report


def _initial_tables() -> dict[str, _Table]:
    account_fields = (
        AcquisitionField(name="segment_id", value_type="integer", nullable=False),
        AcquisitionField(name="account_id", value_type="string", nullable=False),
        AcquisitionField(name="segment_name", value_type="string", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    order_fields = (
        AcquisitionField(name="order_id", value_type="integer", nullable=False),
        AcquisitionField(name="account_id", value_type="string", nullable=False),
        AcquisitionField(name="amount", value_type="decimal", nullable=False),
        AcquisitionField(name="label", value_type="string", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    subscription_fields = (
        AcquisitionField(name="subscription_id", value_type="integer", nullable=False),
        AcquisitionField(name="account_id", value_type="string", nullable=False),
        AcquisitionField(name="lifecycle_status", value_type="string", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    return {
        "account_segments": _Table(
            "account_segments",
            "provider-object-id-account-segments",
            account_fields,
            [
                (
                    1,
                    "provider-account-0001",
                    "growth",
                    datetime(2026, 9, 1, 11, 48, tzinfo=UTC),
                )
            ],
        ),
        "orders": _Table(
            "orders",
            "provider-object-id-orders",
            order_fields,
            [
                (
                    1001,
                    "provider-account-0001",
                    Decimal("10.50"),
                    "initial",
                    datetime(2026, 9, 1, 11, 45, tzinfo=UTC),
                ),
                (
                    1002,
                    "provider-account-0001",
                    Decimal("15.75"),
                    "renewal",
                    datetime(2026, 9, 1, 11, 50, tzinfo=UTC),
                ),
            ],
        ),
        "subscriptions": _Table(
            "subscriptions",
            "provider-object-id-subscriptions",
            subscription_fields,
            [
                (
                    2001,
                    "provider-account-0001",
                    "trialing",
                    datetime(2026, 9, 1, 11, 46, tzinfo=UTC),
                ),
                (
                    2002,
                    "provider-account-0002",
                    "active",
                    datetime(2026, 9, 1, 11, 49, tzinfo=UTC),
                ),
            ],
        ),
    }


def _provider_settings(database: _FakePostgreSQLDatabase) -> PostgreSQLAcquisitionSettings:
    return PostgreSQLAcquisitionSettings(
        dsn=SecretStr(database.dsn),
        connection_handle="opaque-postgresql-source-capability",
        objects=tuple(
            PostgreSQLSourceObjectDeclaration(
                logical_object_ref=object_ref,
                schema_name="source",
                table_name=object_ref,
                field_names=tuple(field.name for field in table.fields),
                key_name=table.key_name,
                source_updated_at_field=table.updated_at_name,
            )
            for object_ref, table in database.tables.items()
        ),
        unrelated_schema_name="private_unrelated",
        max_write_transaction_duration=_CURSOR_LAG,
    )


def _schemas(database: _FakePostgreSQLDatabase) -> tuple[AcquisitionObjectSchema, ...]:
    return tuple(
        AcquisitionObjectSchema(
            logical_object_ref=object_ref,
            schema_digest=digest(table.fields),
            fields=table.fields,
            record_key_fields=(table.key_name,),
            source_updated_at_field=table.updated_at_name,
        )
        for object_ref, table in database.tables.items()
    )


def _required_manifest(preparation: AcquisitionPreparationResult) -> AcquisitionBatchManifest:
    if preparation.batch_manifest is None:
        raise RuntimeError("Plan 4A journey expected a prepared batch")
    return preparation.batch_manifest


def _has_label(records: tuple[AcquisitionRecord, ...], expected: str) -> bool:
    return any(
        field.name == "label" and field.value == expected
        for record in records
        for field in record.fields
    )


def _require_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError("fake driver expected a timestamp")
    return value


def _require_integer(value: object) -> int:
    if type(value) is not int:
        raise RuntimeError("fake driver expected an integer key")
    return value


def _row_key(row: tuple[object, ...]) -> int:
    return _require_integer(row[0])


def _row_updated_at(row: tuple[object, ...]) -> datetime:
    return _require_datetime(row[-1])


def _row_compound_key(row: tuple[object, ...]) -> tuple[datetime, int]:
    return _row_updated_at(row), _row_key(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run offline Plan 4A PostgreSQL acceptance")
    parser.add_argument("--work-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = run_source_acquisition(arguments.work_dir)
    print(json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
