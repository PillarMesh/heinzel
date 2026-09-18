from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from hashlib import sha256
from typing import Literal, Protocol

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionCheckpointReceipt,
    AcquisitionNoValidPlan,
    AcquisitionPreparedReceipt,
    ResynchronizationRequired,
)
from heinzel_provider_sdk.errors import AcquisitionProviderKind
from pydantic import BaseModel

from .crypto import CursorCipher, decrypt_cursor, encrypt_cursor
from .models import (
    GovernedAcquisitionOutcomeState,
    PreparedAcquisitionState,
    PreparedAcquisitionStateStatus,
    SourceCheckpointState,
)

type GovernedAcquisitionOutcome = AcquisitionNoValidPlan | ResynchronizationRequired
type ConnectionFactory = Callable[[str], sqlite3.Connection]
type FaultHook = Callable[[str], None]
type ReferenceFactory = Callable[[str], str]


class AcquisitionStateNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__("acquisition state not found")


class AcquisitionStateConflictError(RuntimeError):
    pass


class StaleAcquisitionRevisionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("acquisition checkpoint revision is stale")


class AcquisitionStatePersistenceError(RuntimeError):
    def __init__(self, *, operation: str, detail: str | None = None) -> None:
        self.operation = operation
        message = f"acquisition state persistence failed during {operation}"
        super().__init__(f"{message}: {detail}" if detail else message)


class AcquisitionStateRepository(Protocol):
    def activate_contract_authority(self, tenant_id: str, contract_digest: str) -> int: ...

    def admit_authority(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
    ) -> tuple[int, int]: ...

    def invalidate_contract_authority(self, tenant_id: str, contract_digest: str) -> None: ...

    def invalidate_source_binding_authority(
        self,
        tenant_id: str,
        source_binding_ref: str,
        *,
        invalidated_revision: int,
    ) -> None: ...

    def prepare_exact(
        self,
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState: ...

    def load_preparation(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]: ...

    def load_preparation_for_replay(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]: ...

    def acknowledge_exact(
        self,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        expected_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        cursor_version: str,
    ) -> AcquisitionCheckpointReceipt: ...

    def load_acknowledgement_replay(
        self,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt | None: ...

    def load_checkpoint_with_cursor(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> tuple[SourceCheckpointState, bytes]: ...


_SCHEMA_VERSION = 2
_SCHEMA_DEFINITIONS = (
    (
        "acquisition_state_schema_metadata",
        "CREATE TABLE acquisition_state_schema_metadata ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
        "version INTEGER NOT NULL, checksum TEXT NOT NULL)",
    ),
    (
        "acquisition_binding_authorities",
        "CREATE TABLE acquisition_binding_authorities ("
        "tenant_id TEXT NOT NULL, source_binding_ref TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "minimum_binding_revision INTEGER NOT NULL, last_invalidated_revision INTEGER NOT NULL, "
        "PRIMARY KEY (tenant_id, source_binding_ref))",
    ),
    (
        "acquisition_contract_authorities",
        "CREATE TABLE acquisition_contract_authorities ("
        "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "active INTEGER NOT NULL CHECK (active IN (0, 1)), "
        "PRIMARY KEY (tenant_id, contract_digest))",
    ),
    (
        "acquisition_prepared_states",
        "CREATE TABLE acquisition_prepared_states ("
        "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, source_binding_ref TEXT NOT NULL, "
        "prior_checkpoint_revision INTEGER NOT NULL, state_payload BLOB NOT NULL, "
        "receipt_payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, contract_digest, source_binding_ref, prior_checkpoint_revision))",
    ),
    (
        "acquisition_acknowledgements",
        "CREATE TABLE acquisition_acknowledgements ("
        "tenant_id TEXT NOT NULL, acknowledgement_id TEXT NOT NULL, "
        "acknowledgement_digest TEXT NOT NULL, "
        "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, acknowledgement_id))",
    ),
    (
        "acquisition_checkpoints",
        "CREATE TABLE acquisition_checkpoints ("
        "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, source_binding_ref TEXT NOT NULL, "
        "revision INTEGER NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, contract_digest, source_binding_ref, revision))",
    ),
    (
        "acquisition_checkpoint_receipts",
        "CREATE TABLE acquisition_checkpoint_receipts ("
        "tenant_id TEXT NOT NULL, contract_digest TEXT NOT NULL, source_binding_ref TEXT NOT NULL, "
        "previous_revision INTEGER NOT NULL, acknowledgement_digest TEXT NOT NULL, "
        "payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, contract_digest, source_binding_ref, previous_revision), "
        "UNIQUE (tenant_id, acknowledgement_digest))",
    ),
    (
        "acquisition_state_evidence",
        "CREATE TABLE acquisition_state_evidence ("
        "tenant_id TEXT NOT NULL, evidence_ref TEXT NOT NULL, event_type TEXT NOT NULL, "
        "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, evidence_ref))",
    ),
    (
        "acquisition_governed_outcomes",
        "CREATE TABLE acquisition_governed_outcomes ("
        "tenant_id TEXT NOT NULL, outcome_id TEXT NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, outcome_id))",
    ),
)
_SCHEMA_CHECKSUM = digest(
    {
        "component": "heinzel-state",
        "schema_version": _SCHEMA_VERSION,
        "definitions": _SCHEMA_DEFINITIONS,
    }
)


class SQLiteAcquisitionStateRepository:
    def __init__(
        self,
        database_path: str,
        *,
        cipher: CursorCipher,
        reference_factory: ReferenceFactory,
        fault_hook: FaultHook | None = None,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        factory = connection_factory or sqlite3.connect
        connection: sqlite3.Connection | None = None
        try:
            connection = factory(database_path)
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            _initialize_schema(self._connection)
        except AcquisitionStatePersistenceError:
            if connection is not None:
                with suppress(BaseException):
                    connection.close()
            raise
        except sqlite3.Error:
            if connection is not None:
                with suppress(BaseException):
                    connection.close()
            raise AcquisitionStatePersistenceError(
                operation="initialize acquisition state repository"
            ) from None
        self._cipher = cipher
        self._reference_factory = reference_factory
        self._fault_hook = fault_hook

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="close acquisition state repository"
            ) from None

    def admit_authority(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
    ) -> tuple[int, int]:
        if source_binding_revision < 1:
            raise AcquisitionStateConflictError("source binding revision is invalid")
        try:
            with _transaction(self._connection):
                binding_row = self._connection.execute(
                    "SELECT epoch, minimum_binding_revision "
                    "FROM acquisition_binding_authorities "
                    "WHERE tenant_id = ? AND source_binding_ref = ?",
                    (tenant_id, source_binding_ref),
                ).fetchone()
                if binding_row is None:
                    binding_epoch = 0
                    self._connection.execute(
                        "INSERT INTO acquisition_binding_authorities "
                        "(tenant_id, source_binding_ref, epoch, minimum_binding_revision, "
                        "last_invalidated_revision) VALUES (?, ?, ?, ?, 0)",
                        (tenant_id, source_binding_ref, binding_epoch, source_binding_revision),
                    )
                else:
                    binding_epoch = int(binding_row[0])
                    minimum_revision = int(binding_row[1])
                    if source_binding_revision < minimum_revision:
                        raise AcquisitionStateConflictError(
                            "source binding authority has been invalidated"
                        )
                    if source_binding_revision > minimum_revision:
                        self._connection.execute(
                            "UPDATE acquisition_binding_authorities "
                            "SET minimum_binding_revision = ? "
                            "WHERE tenant_id = ? AND source_binding_ref = ?",
                            (source_binding_revision, tenant_id, source_binding_ref),
                        )
                contract_row = self._connection.execute(
                    "SELECT epoch, active FROM acquisition_contract_authorities "
                    "WHERE tenant_id = ? AND contract_digest = ?",
                    (tenant_id, contract_digest),
                ).fetchone()
                if contract_row is None:
                    raise AcquisitionStateConflictError("contract authority is not activated")
                contract_epoch = int(contract_row[0])
                if int(contract_row[1]) != 1:
                    raise AcquisitionStateConflictError("contract authority has been invalidated")
                return binding_epoch, contract_epoch
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="admit acquisition authority"
            ) from None

    def activate_contract_authority(self, tenant_id: str, contract_digest: str) -> int:
        try:
            with _transaction(self._connection):
                row = self._connection.execute(
                    "SELECT epoch, active FROM acquisition_contract_authorities "
                    "WHERE tenant_id = ? AND contract_digest = ?",
                    (tenant_id, contract_digest),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        "INSERT INTO acquisition_contract_authorities "
                        "(tenant_id, contract_digest, epoch, active) VALUES (?, ?, 0, 1)",
                        (tenant_id, contract_digest),
                    )
                    return 0
                epoch, active = map(int, row)
                if active != 1:
                    raise AcquisitionStateConflictError(
                        "retired contract authority cannot reactivate"
                    )
                return epoch
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="activate contract acquisition authority"
            ) from None

    def invalidate_contract_authority(self, tenant_id: str, contract_digest: str) -> None:
        try:
            with _transaction(self._connection):
                row = self._connection.execute(
                    "SELECT epoch, active FROM acquisition_contract_authorities "
                    "WHERE tenant_id = ? AND contract_digest = ?",
                    (tenant_id, contract_digest),
                ).fetchone()
                if row is None:
                    raise AcquisitionStateConflictError("contract authority is not activated")
                epoch, active = map(int, row)
                if active == 0:
                    return
                self._invoke_fault("contract_authority_invalidation_before")
                result = self._connection.execute(
                    "UPDATE acquisition_contract_authorities SET epoch = ?, active = 0 "
                    "WHERE tenant_id = ? AND contract_digest = ? AND epoch = ? AND active = 1",
                    (epoch + 1, tenant_id, contract_digest, epoch),
                )
                if result.rowcount != 1:
                    raise AcquisitionStateConflictError("contract authority revision is stale")
                self._invoke_fault("contract_authority_invalidation_after")
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="invalidate contract acquisition authority"
            ) from None

    def invalidate_source_binding_authority(
        self,
        tenant_id: str,
        source_binding_ref: str,
        *,
        invalidated_revision: int,
    ) -> None:
        if invalidated_revision < 1:
            raise AcquisitionStateConflictError("source binding revision is invalid")
        try:
            with _transaction(self._connection):
                row = self._connection.execute(
                    "SELECT epoch, minimum_binding_revision, last_invalidated_revision "
                    "FROM acquisition_binding_authorities "
                    "WHERE tenant_id = ? AND source_binding_ref = ?",
                    (tenant_id, source_binding_ref),
                ).fetchone()
                if row is None:
                    self._invoke_fault("binding_authority_invalidation_before")
                    self._connection.execute(
                        "INSERT INTO acquisition_binding_authorities "
                        "(tenant_id, source_binding_ref, epoch, minimum_binding_revision, "
                        "last_invalidated_revision) VALUES (?, ?, 1, ?, ?)",
                        (
                            tenant_id,
                            source_binding_ref,
                            invalidated_revision + 1,
                            invalidated_revision,
                        ),
                    )
                    self._invoke_fault("binding_authority_invalidation_after")
                    return
                epoch, minimum_revision, last_invalidated_revision = map(int, row)
                if last_invalidated_revision == invalidated_revision:
                    return
                if minimum_revision != invalidated_revision:
                    raise AcquisitionStateConflictError(
                        "source binding authority revision is stale"
                    )
                self._invoke_fault("binding_authority_invalidation_before")
                result = self._connection.execute(
                    "UPDATE acquisition_binding_authorities "
                    "SET epoch = ?, minimum_binding_revision = ?, last_invalidated_revision = ? "
                    "WHERE tenant_id = ? AND source_binding_ref = ? AND epoch = ? "
                    "AND minimum_binding_revision = ? AND last_invalidated_revision = ?",
                    (
                        epoch + 1,
                        invalidated_revision + 1,
                        invalidated_revision,
                        tenant_id,
                        source_binding_ref,
                        epoch,
                        minimum_revision,
                        last_invalidated_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise AcquisitionStateConflictError(
                        "source binding authority revision is stale"
                    )
                self._invoke_fault("binding_authority_invalidation_after")
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="invalidate source binding acquisition authority"
            ) from None

    def prepare_exact(
        self,
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState:
        receipt = _revalidate(
            AcquisitionPreparedReceipt,
            receipt,
            operation="validate prepared receipt",
        )
        if sha256(candidate_cursor_plaintext).hexdigest() != receipt.candidate_checkpoint_digest:
            raise AcquisitionStateConflictError("candidate checkpoint digest does not match cursor")
        try:
            with _transaction(self._connection):
                self._require_authority(
                    tenant_id=receipt.tenant_id,
                    contract_digest=contract_digest,
                    source_binding_ref=source_binding_ref,
                    source_binding_revision=source_binding_revision,
                    binding_authority_epoch=binding_authority_epoch,
                    contract_authority_epoch=contract_authority_epoch,
                )
                existing = self._load_prepared_optional(
                    receipt.tenant_id,
                    contract_digest,
                    source_binding_ref,
                    receipt.prior_checkpoint_revision,
                )
                if existing is not None:
                    state, stored_receipt = existing
                    if (
                        canonical_bytes(stored_receipt) != canonical_bytes(receipt)
                        or state.source_binding_revision != source_binding_revision
                        or state.credential_revision != credential_revision
                        or state.binding_authority_epoch != binding_authority_epoch
                        or state.contract_authority_epoch != contract_authority_epoch
                        or state.acknowledgement_consumer_ref != acknowledgement_consumer_ref
                        or state.provider_kind != provider_kind
                        or decrypt_cursor(
                            self._cipher,
                            tenant_id=state.tenant_id,
                            ciphertext=state.candidate_cursor_ciphertext,
                        )
                        != candidate_cursor_plaintext
                    ):
                        raise AcquisitionStateConflictError("contradictory preparation")
                    return state
                current = self._require_current_revision(
                    receipt.tenant_id,
                    contract_digest,
                    source_binding_ref,
                    receipt.prior_checkpoint_revision,
                )
                if current is not None and receipt.prepared_at < current.updated_at:
                    raise AcquisitionStateConflictError(
                        "preparation timestamp predates current checkpoint"
                    )
                state = _construct_model(
                    PreparedAcquisitionState,
                    {
                        "tenant_id": receipt.tenant_id,
                        "intent_key": receipt.intent_key,
                        "contract_digest": contract_digest,
                        "source_binding_ref": source_binding_ref,
                        "source_binding_revision": source_binding_revision,
                        "credential_revision": credential_revision,
                        "binding_authority_epoch": binding_authority_epoch,
                        "contract_authority_epoch": contract_authority_epoch,
                        "acknowledgement_consumer_ref": acknowledgement_consumer_ref,
                        "provider_kind": provider_kind,
                        "prior_checkpoint_revision": receipt.prior_checkpoint_revision,
                        "batch_id": receipt.batch_id,
                        "batch_manifest_digest": receipt.batch_manifest_digest,
                        "candidate_cursor_ciphertext": encrypt_cursor(
                            self._cipher,
                            tenant_id=receipt.tenant_id,
                            plaintext=candidate_cursor_plaintext,
                        ),
                        "candidate_checkpoint_digest": receipt.candidate_checkpoint_digest,
                        "cursor_version": receipt.cursor_version,
                        "state": PreparedAcquisitionStateStatus.PREPARED,
                        "acknowledgement_digest": None,
                        "created_at": receipt.prepared_at,
                        "updated_at": receipt.prepared_at,
                    },
                    operation="construct prepared acquisition",
                )
                self._invoke_fault("prepared_state_before")
                self._connection.execute(
                    "INSERT INTO acquisition_prepared_states "
                    "(tenant_id, contract_digest, source_binding_ref, prior_checkpoint_revision, "
                    "state_payload, receipt_payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        state.tenant_id,
                        state.contract_digest,
                        state.source_binding_ref,
                        state.prior_checkpoint_revision,
                        _state_bytes(state),
                        canonical_bytes(receipt),
                    ),
                )
                self._invoke_fault("prepared_state_after")
                self._invoke_fault("prepare_evidence")
                self._append_evidence(
                    tenant_id=state.tenant_id,
                    evidence_ref=receipt.prepared_receipt_id,
                    event_type="prepared",
                    payload={
                        "event_type": "prepared",
                        "prepared_receipt_ref": receipt.prepared_receipt_id,
                        "prior_checkpoint_revision": state.prior_checkpoint_revision,
                    },
                )
                self._invoke_fault("prepare_evidence_after")
                return state
        except sqlite3.IntegrityError:
            raise AcquisitionStateConflictError("contradictory preparation") from None
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="prepare acquisition state") from None

    def acknowledge_exact(
        self,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        expected_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        cursor_version: str,
    ) -> AcquisitionCheckpointReceipt:
        acknowledgement = _revalidate(
            AcquisitionAcknowledgement,
            acknowledgement,
            operation="validate acquisition acknowledgement",
        )
        acknowledgement_digest = digest(acknowledgement)
        try:
            with _transaction(self._connection):
                state, prepared_receipt = self._load_prepared_required(
                    acknowledgement.tenant_id,
                    acknowledgement.contract_digest,
                    acknowledgement.source_binding_ref,
                    acknowledgement.prior_checkpoint_revision,
                )
                self._assert_acknowledgement_matches(
                    state,
                    acknowledgement,
                    expected_consumer_ref=expected_consumer_ref,
                )
                self._require_authority(
                    tenant_id=state.tenant_id,
                    contract_digest=state.contract_digest,
                    source_binding_ref=state.source_binding_ref,
                    source_binding_revision=state.source_binding_revision,
                    binding_authority_epoch=state.binding_authority_epoch,
                    contract_authority_epoch=state.contract_authority_epoch,
                )
                if cursor_version != state.cursor_version:
                    raise AcquisitionStateConflictError(
                        "acknowledgement cursor semantics do not match prepared acquisition"
                    )
                self._decrypt_and_verify_cursor(
                    tenant_id=state.tenant_id,
                    ciphertext=state.candidate_cursor_ciphertext,
                    expected_digest=state.candidate_checkpoint_digest,
                )
                if acknowledgement.acknowledged_at < state.created_at:
                    raise AcquisitionStateConflictError(
                        "acknowledgement timestamp predates preparation"
                    )
                if state.state is PreparedAcquisitionStateStatus.ACKNOWLEDGED:
                    if state.acknowledgement_digest != acknowledgement_digest:
                        raise AcquisitionStateConflictError("contradictory acknowledgement")
                    return self._load_acknowledged_replay(
                        state,
                        prepared_receipt,
                        acknowledgement,
                        acknowledgement_digest=acknowledgement_digest,
                        provider_kind=provider_kind,
                        cursor_version=cursor_version,
                    )
                current = self._require_current_revision(
                    state.tenant_id,
                    state.contract_digest,
                    state.source_binding_ref,
                    state.prior_checkpoint_revision,
                )
                if current is not None and (
                    current.provider_kind != provider_kind
                    or current.cursor_version != cursor_version
                ):
                    raise AcquisitionStateConflictError(
                        "acknowledgement cursor semantics do not match current checkpoint"
                    )
                acknowledged_state = state.model_copy(
                    update={
                        "state": PreparedAcquisitionStateStatus.ACKNOWLEDGED,
                        "acknowledgement_digest": acknowledgement_digest,
                        "updated_at": acknowledgement.acknowledged_at,
                    }
                )
                acknowledged_state = _construct_model(
                    PreparedAcquisitionState,
                    acknowledged_state,
                    operation="construct acknowledged acquisition",
                )
                self._invoke_fault("acknowledged_state_before")
                result = self._connection.execute(
                    "UPDATE acquisition_prepared_states SET state_payload = ? "
                    "WHERE tenant_id = ? AND contract_digest = ? AND source_binding_ref = ? "
                    "AND prior_checkpoint_revision = ?",
                    (
                        _state_bytes(acknowledged_state),
                        state.tenant_id,
                        state.contract_digest,
                        state.source_binding_ref,
                        state.prior_checkpoint_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise StaleAcquisitionRevisionError()
                self._invoke_fault("acknowledged_state_after")
                self._invoke_fault("acknowledgement_before")
                self._connection.execute(
                    "INSERT INTO acquisition_acknowledgements "
                    "(tenant_id, acknowledgement_id, acknowledgement_digest, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        acknowledgement.tenant_id,
                        acknowledgement.acknowledgement_id,
                        acknowledgement_digest,
                        canonical_bytes(acknowledgement),
                    ),
                )
                self._invoke_fault("acknowledgement_after")
                checkpoint = _construct_model(
                    SourceCheckpointState,
                    {
                        "tenant_id": state.tenant_id,
                        "contract_digest": state.contract_digest,
                        "source_binding_ref": state.source_binding_ref,
                        "provider_kind": provider_kind,
                        "cursor_version": state.cursor_version,
                        "revision": state.prior_checkpoint_revision + 1,
                        "encrypted_cursor_payload": state.candidate_cursor_ciphertext,
                        "cursor_digest": state.candidate_checkpoint_digest,
                        "last_batch_id": state.batch_id,
                        "created_at": (
                            current.created_at
                            if current is not None
                            else acknowledgement.acknowledged_at
                        ),
                        "updated_at": acknowledgement.acknowledged_at,
                    },
                    operation="construct acquisition checkpoint",
                )
                self._invoke_fault("checkpoint_before")
                self._insert_checkpoint(checkpoint)
                self._invoke_fault("checkpoint_after")
                receipt = _construct_model(
                    AcquisitionCheckpointReceipt,
                    {
                        "checkpoint_receipt_id": self._new_reference("checkpoint"),
                        "tenant_id": state.tenant_id,
                        "contract_digest": state.contract_digest,
                        "source_binding_ref": state.source_binding_ref,
                        "previous_revision": state.prior_checkpoint_revision,
                        "committed_revision": checkpoint.revision,
                        "cursor_digest": checkpoint.cursor_digest,
                        "batch_id": state.batch_id,
                        "acknowledgement_id": acknowledgement.acknowledgement_id,
                        "committed_at": acknowledgement.acknowledged_at,
                    },
                    operation="construct acquisition checkpoint receipt",
                )
                self._invoke_fault("checkpoint_receipt_before")
                self._connection.execute(
                    "INSERT INTO acquisition_checkpoint_receipts "
                    "(tenant_id, contract_digest, source_binding_ref, previous_revision, "
                    "acknowledgement_digest, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        receipt.tenant_id,
                        receipt.contract_digest,
                        receipt.source_binding_ref,
                        receipt.previous_revision,
                        acknowledgement_digest,
                        canonical_bytes(receipt),
                    ),
                )
                self._invoke_fault("checkpoint_receipt_after")
                self._invoke_fault("acknowledgement_evidence")
                self._append_evidence(
                    tenant_id=receipt.tenant_id,
                    evidence_ref=receipt.checkpoint_receipt_id,
                    event_type="acknowledged",
                    payload={
                        "checkpoint_receipt_ref": receipt.checkpoint_receipt_id,
                        "committed_revision": receipt.committed_revision,
                        "event_type": "acknowledged",
                    },
                )
                self._invoke_fault("acknowledgement_evidence_after")
                return receipt
        except sqlite3.IntegrityError:
            raise AcquisitionStateConflictError("contradictory acknowledgement") from None
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="acknowledge acquisition state"
            ) from None

    def load_acknowledgement_replay(
        self,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt | None:
        acknowledgement = _revalidate(
            AcquisitionAcknowledgement,
            acknowledgement,
            operation="validate acquisition acknowledgement replay",
        )
        acknowledgement_digest = digest(acknowledgement)
        try:
            with _transaction(self._connection):
                state, prepared_receipt = self._load_prepared_required(
                    acknowledgement.tenant_id,
                    acknowledgement.contract_digest,
                    acknowledgement.source_binding_ref,
                    acknowledgement.prior_checkpoint_revision,
                )
                self._assert_acknowledgement_matches(
                    state,
                    acknowledgement,
                    expected_consumer_ref=state.acknowledgement_consumer_ref,
                )
                if state.state is PreparedAcquisitionStateStatus.PREPARED:
                    return None
                if state.acknowledgement_digest != acknowledgement_digest:
                    raise AcquisitionStateConflictError("contradictory acknowledgement")
                return self._load_acknowledged_replay(
                    state,
                    prepared_receipt,
                    acknowledgement,
                    acknowledgement_digest=acknowledgement_digest,
                    provider_kind=state.provider_kind,
                    cursor_version=state.cursor_version,
                )
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="load acquisition acknowledgement replay"
            ) from None

    def load_checkpoint(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> SourceCheckpointState:
        try:
            checkpoint = self._load_checkpoint_optional(
                tenant_id,
                contract_digest,
                source_binding_ref,
            )
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(
                operation="load acquisition checkpoint"
            ) from None
        if checkpoint is None:
            raise AcquisitionStateNotFoundError()
        return checkpoint

    def load_checkpoint_with_cursor(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> tuple[SourceCheckpointState, bytes]:
        checkpoint = self.load_checkpoint(tenant_id, contract_digest, source_binding_ref)
        cursor = self._decrypt_and_verify_cursor(
            tenant_id=tenant_id,
            ciphertext=checkpoint.encrypted_cursor_payload,
            expected_digest=checkpoint.cursor_digest,
        )
        return checkpoint, cursor

    def load_prepared(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> PreparedAcquisitionState:
        try:
            state, _receipt = self._load_prepared_required(
                tenant_id,
                contract_digest,
                source_binding_ref,
                prior_checkpoint_revision,
            )
            return state
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="load prepared acquisition") from None

    def load_preparation(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        try:
            return self._load_prepared_required(
                tenant_id,
                contract_digest,
                source_binding_ref,
                prior_checkpoint_revision,
            )
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="load prepared acquisition") from None

    def load_preparation_for_replay(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        try:
            with _transaction(self._connection):
                state, receipt = self._load_prepared_required(
                    tenant_id,
                    contract_digest,
                    source_binding_ref,
                    prior_checkpoint_revision,
                )
                if state.state is not PreparedAcquisitionStateStatus.PREPARED:
                    raise AcquisitionStateConflictError(
                        "acknowledged acquisition cannot replay as pending"
                    )
                self._require_authority(
                    tenant_id=state.tenant_id,
                    contract_digest=state.contract_digest,
                    source_binding_ref=state.source_binding_ref,
                    source_binding_revision=state.source_binding_revision,
                    binding_authority_epoch=state.binding_authority_epoch,
                    contract_authority_epoch=state.contract_authority_epoch,
                )
                self._require_current_revision(
                    tenant_id,
                    contract_digest,
                    source_binding_ref,
                    prior_checkpoint_revision,
                )
                return state, receipt
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="load prepared replay") from None

    def record_governed_outcome(
        self,
        *,
        tenant_id: str,
        outcome_id: str,
        outcome: GovernedAcquisitionOutcome,
        created_at: datetime,
    ) -> GovernedAcquisitionOutcomeState:
        outcome_kind: Literal["no_valid_plan", "resynchronization_required"]
        if isinstance(outcome, AcquisitionNoValidPlan):
            payload = canonical_bytes(
                _revalidate(
                    AcquisitionNoValidPlan,
                    outcome,
                    operation="validate governed acquisition outcome",
                )
            )
            outcome_kind = "no_valid_plan"
        elif isinstance(outcome, ResynchronizationRequired):
            payload = canonical_bytes(
                _revalidate(
                    ResynchronizationRequired,
                    outcome,
                    operation="validate governed acquisition outcome",
                )
            )
            outcome_kind = "resynchronization_required"
        else:
            raise AcquisitionStateConflictError("unsupported governed acquisition outcome")
        state = _construct_model(
            GovernedAcquisitionOutcomeState,
            {
                "outcome_id": outcome_id,
                "tenant_id": tenant_id,
                "outcome_kind": outcome_kind,
                "payload": payload,
                "created_at": created_at,
            },
            operation="construct governed acquisition outcome",
        )
        try:
            with _transaction(self._connection):
                existing = self._load_governed_outcome_optional(tenant_id, outcome_id)
                if existing is not None:
                    if _state_bytes(existing) != _state_bytes(state):
                        raise AcquisitionStateConflictError("contradictory governed outcome")
                    return existing
                self._connection.execute(
                    "INSERT INTO acquisition_governed_outcomes (tenant_id, outcome_id, payload) "
                    "VALUES (?, ?, ?)",
                    (tenant_id, outcome_id, _state_bytes(state)),
                )
                return state
        except sqlite3.IntegrityError:
            raise AcquisitionStateConflictError("contradictory governed outcome") from None
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="record governed outcome") from None

    def load_governed_outcome(
        self,
        tenant_id: str,
        outcome_id: str,
    ) -> GovernedAcquisitionOutcomeState:
        try:
            outcome = self._load_governed_outcome_optional(tenant_id, outcome_id)
        except sqlite3.Error:
            raise AcquisitionStatePersistenceError(operation="load governed outcome") from None
        if outcome is None:
            raise AcquisitionStateNotFoundError()
        return outcome

    def _load_prepared_required(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        value = self._load_prepared_optional(
            tenant_id,
            contract_digest,
            source_binding_ref,
            prior_checkpoint_revision,
        )
        if value is None:
            raise AcquisitionStateNotFoundError()
        return value

    def _load_prepared_optional(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt] | None:
        row = self._connection.execute(
            "SELECT state_payload, receipt_payload FROM acquisition_prepared_states "
            "WHERE tenant_id = ? AND contract_digest = ? AND source_binding_ref = ? "
            "AND prior_checkpoint_revision = ?",
            (tenant_id, contract_digest, source_binding_ref, prior_checkpoint_revision),
        ).fetchone()
        if row is None:
            return None
        state = _revalidate_payload(
            PreparedAcquisitionState,
            row[0],
            operation="load prepared acquisition",
        )
        receipt = _revalidate_payload(
            AcquisitionPreparedReceipt,
            row[1],
            operation="load prepared receipt",
        )
        if (
            state.tenant_id != tenant_id
            or state.contract_digest != contract_digest
            or state.source_binding_ref != source_binding_ref
            or state.prior_checkpoint_revision != prior_checkpoint_revision
            or receipt.tenant_id != state.tenant_id
            or receipt.intent_key != state.intent_key
            or receipt.batch_id != state.batch_id
            or receipt.batch_manifest_digest != state.batch_manifest_digest
            or receipt.prior_checkpoint_revision != state.prior_checkpoint_revision
            or receipt.candidate_checkpoint_digest != state.candidate_checkpoint_digest
            or receipt.cursor_version != state.cursor_version
            or receipt.prepared_at != state.created_at
        ):
            raise AcquisitionStatePersistenceError(
                operation="load prepared acquisition",
                detail="stored authority mismatch",
            )
        if (
            state.state is PreparedAcquisitionStateStatus.PREPARED
            and state.updated_at != state.created_at
        ):
            raise AcquisitionStatePersistenceError(
                operation="load prepared acquisition",
                detail="stored lifecycle timestamp mismatch",
            )
        self._require_evidence(
            tenant_id=state.tenant_id,
            evidence_ref=receipt.prepared_receipt_id,
            event_type="prepared",
            payload={
                "event_type": "prepared",
                "prepared_receipt_ref": receipt.prepared_receipt_id,
                "prior_checkpoint_revision": state.prior_checkpoint_revision,
            },
        )
        return state, receipt

    def _load_checkpoint_optional(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
    ) -> SourceCheckpointState | None:
        row = self._connection.execute(
            "SELECT revision, payload FROM acquisition_checkpoints "
            "WHERE tenant_id = ? AND contract_digest = ? AND source_binding_ref = ? "
            "ORDER BY revision DESC LIMIT 1",
            (tenant_id, contract_digest, source_binding_ref),
        ).fetchone()
        if row is None:
            return None
        checkpoint = _revalidate_payload(
            SourceCheckpointState,
            row[1],
            operation="load acquisition checkpoint",
        )
        if (
            checkpoint.tenant_id != tenant_id
            or checkpoint.contract_digest != contract_digest
            or checkpoint.source_binding_ref != source_binding_ref
            or checkpoint.revision != int(row[0])
        ):
            raise AcquisitionStatePersistenceError(
                operation="load acquisition checkpoint",
                detail="stored authority mismatch",
            )
        return checkpoint

    def _require_current_revision(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        expected_revision: int,
    ) -> SourceCheckpointState | None:
        current = self._load_checkpoint_optional(tenant_id, contract_digest, source_binding_ref)
        actual_revision = current.revision if current is not None else 0
        if actual_revision != expected_revision or (current is not None and expected_revision == 0):
            raise StaleAcquisitionRevisionError()
        if current is not None:
            self._decrypt_and_verify_cursor(
                tenant_id=current.tenant_id,
                ciphertext=current.encrypted_cursor_payload,
                expected_digest=current.cursor_digest,
            )
        return current

    @staticmethod
    def _assert_acknowledgement_matches(
        state: PreparedAcquisitionState,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        expected_consumer_ref: str,
    ) -> None:
        if (
            expected_consumer_ref != state.acknowledgement_consumer_ref
            or acknowledgement.consumer_ref != state.acknowledgement_consumer_ref
        ):
            raise AcquisitionStateConflictError("acknowledgement consumer does not match authority")
        if (
            acknowledgement.batch_id != state.batch_id
            or acknowledgement.batch_manifest_digest != state.batch_manifest_digest
            or acknowledgement.prior_checkpoint_revision != state.prior_checkpoint_revision
            or acknowledgement.candidate_checkpoint_digest != state.candidate_checkpoint_digest
        ):
            raise AcquisitionStateConflictError("acknowledgement does not match prepared authority")

    def _insert_checkpoint(self, checkpoint: SourceCheckpointState) -> None:
        self._connection.execute(
            "INSERT INTO acquisition_checkpoints "
            "(tenant_id, contract_digest, source_binding_ref, revision, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                checkpoint.tenant_id,
                checkpoint.contract_digest,
                checkpoint.source_binding_ref,
                checkpoint.revision,
                _state_bytes(checkpoint),
            ),
        )

    def _require_authority(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
    ) -> None:
        binding_row = self._connection.execute(
            "SELECT epoch, minimum_binding_revision FROM acquisition_binding_authorities "
            "WHERE tenant_id = ? AND source_binding_ref = ?",
            (tenant_id, source_binding_ref),
        ).fetchone()
        contract_row = self._connection.execute(
            "SELECT epoch, active FROM acquisition_contract_authorities "
            "WHERE tenant_id = ? AND contract_digest = ?",
            (tenant_id, contract_digest),
        ).fetchone()
        if (
            binding_row is None
            or int(binding_row[0]) != binding_authority_epoch
            or source_binding_revision < int(binding_row[1])
            or contract_row is None
            or int(contract_row[0]) != contract_authority_epoch
            or int(contract_row[1]) != 1
        ):
            raise StaleAcquisitionRevisionError()

    def _load_acknowledged_replay(
        self,
        state: PreparedAcquisitionState,
        prepared_receipt: AcquisitionPreparedReceipt,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        acknowledgement_digest: str,
        provider_kind: AcquisitionProviderKind,
        cursor_version: str,
    ) -> AcquisitionCheckpointReceipt:
        if state.updated_at != acknowledgement.acknowledged_at:
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored lifecycle timestamp mismatch",
            )
        stored_acknowledgement = self._load_acknowledgement_required(
            acknowledgement.tenant_id,
            acknowledgement.acknowledgement_id,
            acknowledgement_digest=acknowledgement_digest,
        )
        if canonical_bytes(stored_acknowledgement) != canonical_bytes(acknowledgement):
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored acknowledgement authority mismatch",
            )
        checkpoint = self._load_checkpoint_revision_required(
            state.tenant_id,
            state.contract_digest,
            state.source_binding_ref,
            state.prior_checkpoint_revision + 1,
        )
        expected_checkpoint_created_at = acknowledgement.acknowledged_at
        if state.prior_checkpoint_revision > 0:
            previous_checkpoint = self._load_checkpoint_revision_required(
                state.tenant_id,
                state.contract_digest,
                state.source_binding_ref,
                state.prior_checkpoint_revision,
            )
            self._decrypt_and_verify_cursor(
                tenant_id=previous_checkpoint.tenant_id,
                ciphertext=previous_checkpoint.encrypted_cursor_payload,
                expected_digest=previous_checkpoint.cursor_digest,
            )
            if (
                previous_checkpoint.provider_kind != checkpoint.provider_kind
                or previous_checkpoint.cursor_version != checkpoint.cursor_version
            ):
                raise AcquisitionStateConflictError(
                    "acknowledgement cursor semantics do not match previous checkpoint"
                )
            if previous_checkpoint.updated_at > state.created_at:
                raise AcquisitionStatePersistenceError(
                    operation="load acknowledged acquisition",
                    detail="stored checkpoint timeline mismatch",
                )
            expected_checkpoint_created_at = previous_checkpoint.created_at
        if checkpoint.provider_kind != provider_kind or checkpoint.cursor_version != cursor_version:
            raise AcquisitionStateConflictError(
                "acknowledgement cursor semantics do not match committed checkpoint"
            )
        if (
            checkpoint.encrypted_cursor_payload != state.candidate_cursor_ciphertext
            or checkpoint.cursor_digest != state.candidate_checkpoint_digest
            or checkpoint.last_batch_id != state.batch_id
            or checkpoint.created_at != expected_checkpoint_created_at
            or checkpoint.updated_at != acknowledgement.acknowledged_at
        ):
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored checkpoint authority mismatch",
            )
        self._decrypt_and_verify_cursor(
            tenant_id=checkpoint.tenant_id,
            ciphertext=checkpoint.encrypted_cursor_payload,
            expected_digest=checkpoint.cursor_digest,
        )
        receipt = self._load_checkpoint_receipt_required(
            state.tenant_id,
            state.contract_digest,
            state.source_binding_ref,
            state.prior_checkpoint_revision,
            acknowledgement_digest=acknowledgement_digest,
        )
        if (
            receipt.committed_revision != checkpoint.revision
            or receipt.cursor_digest != checkpoint.cursor_digest
            or receipt.batch_id != state.batch_id
            or receipt.acknowledgement_id != acknowledgement.acknowledgement_id
            or receipt.committed_at != acknowledgement.acknowledged_at
        ):
            raise AcquisitionStatePersistenceError(
                operation="load checkpoint receipt",
                detail="stored authority mismatch",
            )
        self._require_evidence(
            tenant_id=state.tenant_id,
            evidence_ref=prepared_receipt.prepared_receipt_id,
            event_type="prepared",
            payload={
                "event_type": "prepared",
                "prepared_receipt_ref": prepared_receipt.prepared_receipt_id,
                "prior_checkpoint_revision": state.prior_checkpoint_revision,
            },
        )
        self._require_evidence(
            tenant_id=receipt.tenant_id,
            evidence_ref=receipt.checkpoint_receipt_id,
            event_type="acknowledged",
            payload={
                "checkpoint_receipt_ref": receipt.checkpoint_receipt_id,
                "committed_revision": receipt.committed_revision,
                "event_type": "acknowledged",
            },
        )
        return receipt

    def _load_acknowledgement_required(
        self,
        tenant_id: str,
        acknowledgement_id: str,
        *,
        acknowledgement_digest: str,
    ) -> AcquisitionAcknowledgement:
        row = self._connection.execute(
            "SELECT acknowledgement_digest, payload FROM acquisition_acknowledgements "
            "WHERE tenant_id = ? AND acknowledgement_id = ?",
            (tenant_id, acknowledgement_id),
        ).fetchone()
        if row is None:
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored acknowledgement is missing",
            )
        acknowledgement = _revalidate_payload(
            AcquisitionAcknowledgement,
            row[1],
            operation="load acknowledged acquisition",
        )
        if (
            str(row[0]) != acknowledgement_digest
            or digest(acknowledgement) != acknowledgement_digest
        ):
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored acknowledgement authority mismatch",
            )
        return acknowledgement

    def _load_checkpoint_revision_required(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        revision: int,
    ) -> SourceCheckpointState:
        row = self._connection.execute(
            "SELECT revision, payload FROM acquisition_checkpoints "
            "WHERE tenant_id = ? AND contract_digest = ? AND source_binding_ref = ? "
            "AND revision = ?",
            (tenant_id, contract_digest, source_binding_ref, revision),
        ).fetchone()
        if row is None:
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored checkpoint is missing",
            )
        checkpoint = _revalidate_payload(
            SourceCheckpointState,
            row[1],
            operation="load acknowledged acquisition",
        )
        if (
            checkpoint.tenant_id != tenant_id
            or checkpoint.contract_digest != contract_digest
            or checkpoint.source_binding_ref != source_binding_ref
            or checkpoint.revision != revision
            or int(row[0]) != revision
        ):
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored checkpoint authority mismatch",
            )
        return checkpoint

    def _decrypt_and_verify_cursor(
        self,
        *,
        tenant_id: str,
        ciphertext: bytes,
        expected_digest: str,
    ) -> bytes:
        plaintext = decrypt_cursor(
            self._cipher,
            tenant_id=tenant_id,
            ciphertext=ciphertext,
        )
        if sha256(plaintext).hexdigest() != expected_digest:
            raise AcquisitionStatePersistenceError(
                operation="verify acquisition cursor",
                detail="cursor integrity mismatch",
            )
        return plaintext

    def _load_checkpoint_receipt_required(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        previous_revision: int,
        *,
        acknowledgement_digest: str,
    ) -> AcquisitionCheckpointReceipt:
        row = self._connection.execute(
            "SELECT acknowledgement_digest, payload FROM acquisition_checkpoint_receipts "
            "WHERE tenant_id = ? AND contract_digest = ? AND source_binding_ref = ? "
            "AND previous_revision = ?",
            (tenant_id, contract_digest, source_binding_ref, previous_revision),
        ).fetchone()
        if row is None:
            raise AcquisitionStatePersistenceError(
                operation="load checkpoint receipt",
                detail="acknowledged state has no checkpoint receipt",
            )
        receipt = _revalidate_payload(
            AcquisitionCheckpointReceipt,
            row[1],
            operation="load checkpoint receipt",
        )
        if (
            str(row[0]) != acknowledgement_digest
            or receipt.tenant_id != tenant_id
            or receipt.contract_digest != contract_digest
            or receipt.source_binding_ref != source_binding_ref
            or receipt.previous_revision != previous_revision
        ):
            raise AcquisitionStatePersistenceError(
                operation="load checkpoint receipt",
                detail="stored authority mismatch",
            )
        return receipt

    def _append_evidence(
        self,
        *,
        tenant_id: str,
        evidence_ref: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self._connection.execute(
            "INSERT INTO acquisition_state_evidence "
            "(tenant_id, evidence_ref, event_type, payload) VALUES (?, ?, ?, ?)",
            (tenant_id, evidence_ref, event_type, canonical_bytes(payload)),
        )

    def _require_evidence(
        self,
        *,
        tenant_id: str,
        evidence_ref: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        row = self._connection.execute(
            "SELECT event_type, payload FROM acquisition_state_evidence "
            "WHERE tenant_id = ? AND evidence_ref = ?",
            (tenant_id, evidence_ref),
        ).fetchone()
        if row is None or str(row[0]) != event_type or bytes(row[1]) != canonical_bytes(payload):
            raise AcquisitionStatePersistenceError(
                operation="load acknowledged acquisition",
                detail="stored evidence authority mismatch",
            )

    def _load_governed_outcome_optional(
        self,
        tenant_id: str,
        outcome_id: str,
    ) -> GovernedAcquisitionOutcomeState | None:
        row = self._connection.execute(
            "SELECT payload FROM acquisition_governed_outcomes "
            "WHERE tenant_id = ? AND outcome_id = ?",
            (tenant_id, outcome_id),
        ).fetchone()
        if row is None:
            return None
        outcome = _revalidate_payload(
            GovernedAcquisitionOutcomeState,
            row[0],
            operation="load governed acquisition outcome",
        )
        if outcome.tenant_id != tenant_id or outcome.outcome_id != outcome_id:
            raise AcquisitionStatePersistenceError(
                operation="load governed acquisition outcome",
                detail="stored authority mismatch",
            )
        return outcome

    def _invoke_fault(self, checkpoint: str) -> None:
        if self._fault_hook is not None:
            try:
                self._fault_hook(checkpoint)
            except Exception:
                raise AcquisitionStatePersistenceError(operation=f"write {checkpoint}") from None

    def _new_reference(self, kind: str) -> str:
        try:
            reference = self._reference_factory(kind)
        except Exception:
            raise AcquisitionStatePersistenceError(operation="allocate receipt reference") from None
        if not reference:
            raise AcquisitionStatePersistenceError(operation="allocate receipt reference")
        return reference


def _revalidate[Model: BaseModel](
    model: type[Model],
    value: object,
    *,
    operation: str,
) -> Model:
    if not isinstance(value, model):
        raise AcquisitionStateConflictError(f"{operation} failed")
    try:
        return model.model_validate_json(value.model_dump_json())
    except Exception:
        raise AcquisitionStateConflictError(f"{operation} failed") from None


def _construct_model[Model: BaseModel](
    model: type[Model],
    value: object,
    *,
    operation: str,
) -> Model:
    try:
        return model.model_validate(value)
    except Exception:
        raise AcquisitionStateConflictError(f"{operation} failed") from None


def _state_bytes(model: BaseModel) -> bytes:
    return model.model_dump_json().encode("utf-8")


def _revalidate_payload[Model: BaseModel](
    model: type[Model],
    payload: str | bytes,
    *,
    operation: str,
) -> Model:
    try:
        return model.model_validate_json(payload)
    except Exception:
        raise AcquisitionStatePersistenceError(
            operation=operation,
            detail="stored payload is invalid",
        ) from None


def _initialize_schema(connection: sqlite3.Connection) -> None:
    # Schema discovery and creation are one write transaction. Two workers can open
    # a fresh database together; without taking the lock before discovery both see
    # an empty layout and the loser later fails while creating tables the winner
    # already committed.
    with _transaction(connection):
        _initialize_schema_locked(connection)


def _initialize_schema_locked(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    expected_tables = {name for name, _statement in _SCHEMA_DEFINITIONS}
    if "acquisition_state_schema_metadata" in tables:
        row = connection.execute(
            "SELECT version, checksum FROM acquisition_state_schema_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise AcquisitionStatePersistenceError(
                operation="initialize acquisition state repository",
                detail="schema metadata is missing",
            )
        version, checksum = int(row[0]), str(row[1])
        if version != _SCHEMA_VERSION or checksum != _SCHEMA_CHECKSUM:
            raise AcquisitionStatePersistenceError(
                operation="initialize acquisition state repository",
                detail="schema checksum mismatch",
            )
        if tables != expected_tables:
            raise AcquisitionStatePersistenceError(
                operation="initialize acquisition state repository",
                detail="schema table layout mismatch",
            )
        actual_definitions = {
            str(name): _normalize_sql(str(statement))
            for name, statement in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_definitions = {
            name: _normalize_sql(statement) for name, statement in _SCHEMA_DEFINITIONS
        }
        if actual_definitions != expected_definitions:
            raise AcquisitionStatePersistenceError(
                operation="initialize acquisition state repository",
                detail="schema definition mismatch",
            )
        return
    if tables:
        raise AcquisitionStatePersistenceError(
            operation="initialize acquisition state repository",
            detail="schema metadata is missing",
        )
    for _name, statement in _SCHEMA_DEFINITIONS:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO acquisition_state_schema_metadata (singleton, version, checksum) "
        "VALUES (1, ?, ?)",
        (_SCHEMA_VERSION, _SCHEMA_CHECKSUM),
    )


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.split())


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with suppress(BaseException):
            connection.rollback()
        raise
    else:
        try:
            connection.commit()
        except BaseException:
            with suppress(BaseException):
                connection.rollback()
            raise
