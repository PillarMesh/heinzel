from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import pytest
from pillarmesh_contract_model import canonical_bytes
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionCheckpointReceipt,
    AcquisitionNoValidPlan,
    AcquisitionPreparedReceipt,
)
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind
from pillarmesh_state import (
    AcquisitionStateConflictError,
    AcquisitionStateNotFoundError,
    AcquisitionStatePersistenceError,
    PreparedAcquisitionState,
    PreparedAcquisitionStateStatus,
    SourceCheckpointState,
    SQLiteAcquisitionStateRepository,
    StaleAcquisitionRevisionError,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
LATER = datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)
CURSOR = b'{"updated_at":"2026-09-01T12:00:00Z","id":7}'


class Cipher:
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        return b"cipher:" + tenant_id.encode() + b":" + plaintext[::-1]

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        prefix = b"cipher:" + tenant_id.encode() + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("wrong tenant")
        return ciphertext.removeprefix(prefix)[::-1]


class NonceCipher:
    def __init__(self) -> None:
        self._nonce = 0

    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        self._nonce += 1
        return (
            b"cipher:"
            + tenant_id.encode()
            + b":"
            + str(self._nonce).encode()
            + b":"
            + plaintext[::-1]
        )

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        prefix = b"cipher:" + tenant_id.encode() + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("wrong tenant")
        _nonce, separator, reversed_plaintext = ciphertext.removeprefix(prefix).partition(b":")
        if not separator:
            raise ValueError("missing nonce")
        return reversed_plaintext[::-1]


class FaultHook:
    def __init__(self) -> None:
        self.fail_at: str | None = None

    def __call__(self, checkpoint: str) -> None:
        if checkpoint == self.fail_at:
            raise InjectedFailure("private-cursor-canary")


class InjectedFailure(RuntimeError):
    pass


class BlockingFaultHook:
    def __init__(self, checkpoint: str) -> None:
        self._checkpoint = checkpoint
        self.entered = threading.Barrier(2)
        self.release = threading.Barrier(2)

    def __call__(self, checkpoint: str) -> None:
        if checkpoint != self._checkpoint:
            return
        self.entered.wait(timeout=5)
        self.release.wait(timeout=5)


class ReferenceFactory:
    def __init__(self) -> None:
        self._sequence = 0

    def __call__(self, kind: str) -> str:
        self._sequence += 1
        return f"{kind}-ref-{self._sequence}"


def candidate_digest(cursor: bytes = CURSOR) -> str:
    return sha256(cursor).hexdigest()


def prepared_receipt(
    *,
    prior_revision: int = 0,
    cursor: bytes = CURSOR,
    update: dict[str, object] | None = None,
) -> AcquisitionPreparedReceipt:
    receipt = AcquisitionPreparedReceipt(
        prepared_receipt_id="prepared-ref-a",
        tenant_id="tenant-a",
        intent_key="1" * 64,
        batch_id="2" * 64,
        batch_manifest_digest="3" * 64,
        prior_checkpoint_revision=prior_revision,
        candidate_checkpoint_digest=candidate_digest(cursor),
        cursor_version="postgresql-compound-v1",
        prepared_at=NOW,
    )
    return receipt.model_copy(update=update or {})


def acknowledgement(
    *,
    prior_revision: int = 0,
    cursor: bytes = CURSOR,
    update: dict[str, object] | None = None,
) -> AcquisitionAcknowledgement:
    receipt = prepared_receipt(prior_revision=prior_revision, cursor=cursor)
    value = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement-a",
        tenant_id=receipt.tenant_id,
        consumer_ref="warehouse-consumer-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        batch_id=receipt.batch_id,
        batch_manifest_digest=receipt.batch_manifest_digest,
        prior_checkpoint_revision=receipt.prior_checkpoint_revision,
        candidate_checkpoint_digest=receipt.candidate_checkpoint_digest,
        consumer_receipt_digest="5" * 64,
        acknowledged_at=LATER,
    )
    return value.model_copy(update=update or {})


def repository(
    database_path: str = ":memory:",
    *,
    fault_hook: Callable[[str], None] | None = None,
    connection_factory: Callable[[str], sqlite3.Connection] | None = None,
) -> SQLiteAcquisitionStateRepository:
    return SQLiteAcquisitionStateRepository(
        database_path,
        cipher=Cipher(),
        reference_factory=ReferenceFactory(),
        fault_hook=fault_hook,
        connection_factory=connection_factory,
    )


def concurrent_repository(
    database_path: str,
    *,
    fault_hook: BlockingFaultHook | None = None,
) -> SQLiteAcquisitionStateRepository:
    return repository(
        database_path,
        fault_hook=fault_hook,
        connection_factory=lambda path: sqlite3.connect(
            path,
            check_same_thread=False,
            timeout=5,
        ),
    )


def prepare(
    state: SQLiteAcquisitionStateRepository,
    *,
    receipt: AcquisitionPreparedReceipt | None = None,
    cursor: bytes = CURSOR,
) -> PreparedAcquisitionState:
    state.activate_contract_authority("tenant-a", "4" * 64)
    binding_epoch, contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
    )
    return state.prepare_exact(
        receipt or prepared_receipt(cursor=cursor),
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
        credential_revision=1,
        binding_authority_epoch=binding_epoch,
        contract_authority_epoch=contract_epoch,
        acknowledgement_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        candidate_cursor_plaintext=cursor,
    )


def test_authority_revision_one_is_valid_and_zero_is_rejected() -> None:
    state = repository()
    state.activate_contract_authority("tenant-a", "4" * 64)

    assert state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=1,
    ) == (0, 0)
    with pytest.raises(AcquisitionStateConflictError, match="revision is invalid"):
        state.admit_authority(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=0,
        )


def test_binding_invalidation_admits_the_immediate_next_revision() -> None:
    state = repository()
    state.activate_contract_authority("tenant-a", "4" * 64)
    state.invalidate_source_binding_authority(
        "tenant-a",
        "source-binding-a",
        invalidated_revision=1,
    )

    assert state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=2,
    ) == (1, 0)


def test_unactivated_contract_authority_cannot_be_admitted_or_prepared() -> None:
    state = repository()

    with pytest.raises(AcquisitionStateConflictError, match="contract authority is not activated"):
        state.admit_authority(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=3,
        )

    with pytest.raises(StaleAcquisitionRevisionError):
        state.prepare_exact(
            prepared_receipt(),
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=3,
            credential_revision=1,
            binding_authority_epoch=0,
            contract_authority_epoch=0,
            acknowledgement_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            candidate_cursor_plaintext=CURSOR,
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)

    state.activate_contract_authority("tenant-a", "4" * 64)
    assert state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
    ) == (0, 0)


def test_initial_prepare_replays_byte_identically_and_contains_no_plain_cursor(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "state.sqlite")
    state = repository(database_path)

    first = prepare(state)
    replay = prepare(state)

    assert first == replay
    assert first.prior_checkpoint_revision == 0
    assert first.state is PreparedAcquisitionStateStatus.PREPARED
    assert first.model_dump_json() == replay.model_dump_json()
    assert CURSOR not in Path(database_path).read_bytes()


def test_prepare_replay_rejects_a_newer_binding_authority_snapshot() -> None:
    state = repository()
    prepare(state)
    binding_epoch, contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=4,
    )

    with pytest.raises(AcquisitionStateConflictError, match="contradictory preparation"):
        state.prepare_exact(
            prepared_receipt(),
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=4,
            credential_revision=1,
            binding_authority_epoch=binding_epoch,
            contract_authority_epoch=contract_epoch,
            acknowledgement_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            candidate_cursor_plaintext=CURSOR,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_binding_revision", 4),
        ("credential_revision", 2),
        ("binding_authority_epoch", 1),
        ("contract_authority_epoch", 1),
        ("acknowledgement_consumer_ref", "warehouse-consumer-b"),
        ("provider_kind", "stripe"),
    ),
)
def test_prepare_replay_rejects_each_changed_durable_authority_dimension(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database_path = str(tmp_path / f"prepared-{field}.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT state_payload FROM acquisition_prepared_states WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prepared = PreparedAcquisitionState.model_validate_json(row[0]).model_copy(
        update={field: value}
    )
    connection.execute(
        "UPDATE acquisition_prepared_states SET state_payload = ? WHERE tenant_id = ?",
        (prepared.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStateConflictError, match="contradictory preparation"):
        prepare(state)


def test_pending_preparation_replay_rejects_invalidated_binding_authority() -> None:
    state = repository()
    prepare(state)
    state.invalidate_source_binding_authority(
        "tenant-a",
        "source-binding-a",
        invalidated_revision=3,
    )

    with pytest.raises(StaleAcquisitionRevisionError):
        state.load_preparation_for_replay("tenant-a", "4" * 64, "source-binding-a", 0)


@pytest.mark.parametrize(
    "authority_mutation",
    (
        "DELETE FROM acquisition_binding_authorities",
        "UPDATE acquisition_binding_authorities SET epoch = epoch + 1",
        "UPDATE acquisition_binding_authorities SET minimum_binding_revision = 4",
        "DELETE FROM acquisition_contract_authorities",
        "UPDATE acquisition_contract_authorities SET epoch = epoch + 1",
        "UPDATE acquisition_contract_authorities SET active = 0",
    ),
)
def test_acknowledgement_rejects_each_stale_authority_dimension(
    tmp_path: Path,
    authority_mutation: str,
) -> None:
    database_path = str(tmp_path / "authority-dimension.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    connection.execute(authority_mutation)
    connection.commit()
    connection.close()

    with pytest.raises(StaleAcquisitionRevisionError):
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize("corruption", ("missing_evidence", "updated_at"))
def test_prepare_replay_requires_complete_prepared_lifecycle(
    tmp_path: Path,
    corruption: Literal["missing_evidence", "updated_at"],
) -> None:
    database_path = str(tmp_path / "prepared-replay-lifecycle.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    if corruption == "missing_evidence":
        connection.execute("DELETE FROM acquisition_state_evidence WHERE event_type = 'prepared'")
    else:
        row = connection.execute(
            "SELECT state_payload FROM acquisition_prepared_states WHERE tenant_id = ?",
            ("tenant-a",),
        ).fetchone()
        assert row is not None
        prepared = PreparedAcquisitionState.model_validate_json(row[0]).model_copy(
            update={"updated_at": LATER}
        )
        connection.execute(
            "UPDATE acquisition_prepared_states SET state_payload = ? WHERE tenant_id = ?",
            (prepared.model_dump_json().encode(), "tenant-a"),
        )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        prepare(state)


def test_prepare_rejects_contradictory_replay_under_the_same_authority_key() -> None:
    state = repository()
    prepare(state)
    competing_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    competing = prepared_receipt(cursor=competing_cursor).model_copy(update={"batch_id": "6" * 64})

    with pytest.raises(AcquisitionStateConflictError, match="contradictory preparation"):
        prepare(state, receipt=competing, cursor=competing_cursor)


def test_prepare_rejects_receipt_only_contradiction_under_the_same_authority_key() -> None:
    state = repository()
    prepare(state)

    with pytest.raises(AcquisitionStateConflictError, match="contradictory preparation"):
        prepare(
            state,
            receipt=prepared_receipt(update={"batch_id": "6" * 64}),
        )


def test_prepare_rejects_a_candidate_digest_that_does_not_cover_plain_cursor() -> None:
    state = repository()

    with pytest.raises(AcquisitionStateConflictError, match="candidate checkpoint"):
        prepare(
            state,
            receipt=prepared_receipt(update={"candidate_checkpoint_digest": "f" * 64}),
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)


def test_acknowledgement_advances_zero_record_batch_exactly_once_and_replays() -> None:
    state = repository()
    prepared = prepare(state)
    accepted = acknowledgement()

    first = state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    replay = state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    assert first == replay
    assert first.previous_revision == 0
    assert first.committed_revision == 1
    checkpoint, cursor = state.load_checkpoint_with_cursor("tenant-a", "4" * 64, "source-binding-a")
    assert checkpoint.revision == 1
    assert checkpoint.cursor_digest == prepared.candidate_checkpoint_digest
    assert checkpoint.last_batch_id == accepted.batch_id
    assert cursor == CURSOR


def test_acknowledgement_replay_loader_distinguishes_pending_exact_and_contradictory() -> None:
    state = repository()
    prepare(state)
    accepted = acknowledgement()

    assert state.load_acknowledgement_replay(accepted) is None

    committed = state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    state.invalidate_contract_authority("tenant-a", "4" * 64)

    assert state.load_acknowledgement_replay(accepted) == committed
    with pytest.raises(AcquisitionStateConflictError, match="contradictory acknowledgement"):
        state.load_acknowledgement_replay(
            accepted.model_copy(update={"consumer_receipt_digest": "8" * 64})
        )


def test_acknowledgement_persists_the_exact_canonical_payload(tmp_path: Path) -> None:
    database_path = str(tmp_path / "acknowledgement.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()

    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_acknowledgements "
        "WHERE tenant_id = ? AND acknowledgement_id = ?",
        (accepted.tenant_id, accepted.acknowledgement_id),
    ).fetchone()
    connection.close()

    assert row == (canonical_bytes(accepted),)


def test_acknowledgement_rejects_corrupted_prepared_cursor_without_advancing(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "prepared-cursor-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT state_payload FROM acquisition_prepared_states WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prepared = PreparedAcquisitionState.model_validate_json(row[0])
    corrupted = prepared.model_copy(
        update={"candidate_cursor_ciphertext": b"cipher:tenant-a:corrupted"}
    )
    connection.execute(
        "UPDATE acquisition_prepared_states SET state_payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="cursor integrity"):
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


@pytest.mark.parametrize(
    ("provider_kind", "cursor_version"),
    (
        ("stripe", "postgresql-compound-v1"),
        ("postgresql", "postgresql-compound-v2"),
    ),
)
def test_acknowledgement_replay_rejects_changed_cursor_semantics(
    provider_kind: Literal["postgresql", "stripe"],
    cursor_version: str,
) -> None:
    state = repository()
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    with pytest.raises(AcquisitionStateConflictError, match="cursor semantics"):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind=provider_kind,
            cursor_version=cursor_version,
        )


@pytest.mark.parametrize(
    ("update", "expected_error"),
    (
        ({"tenant_id": "tenant-b"}, AcquisitionStateNotFoundError),
        ({"consumer_ref": "warehouse-consumer-b"}, AcquisitionStateConflictError),
        ({"contract_digest": "a" * 64}, AcquisitionStateNotFoundError),
        ({"source_binding_ref": "source-binding-b"}, AcquisitionStateNotFoundError),
        ({"batch_id": "b" * 64}, AcquisitionStateConflictError),
        ({"batch_manifest_digest": "c" * 64}, AcquisitionStateConflictError),
        ({"prior_checkpoint_revision": 1}, AcquisitionStateNotFoundError),
        ({"candidate_checkpoint_digest": "d" * 64}, AcquisitionStateConflictError),
    ),
)
def test_acknowledgement_rejects_each_wrong_authority_dimension(
    update: dict[str, object],
    expected_error: type[Exception],
) -> None:
    state = repository()
    prepare(state)

    with pytest.raises(expected_error):
        state.acknowledge_exact(
            acknowledgement(update=update),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_caller_cannot_authorize_a_consumer_not_bound_to_prepared_state() -> None:
    state = repository()
    prepare(state)

    with pytest.raises(AcquisitionStateConflictError, match="consumer"):
        state.acknowledge_exact(
            acknowledgement(update={"consumer_ref": "warehouse-consumer-b"}),
            expected_consumer_ref="warehouse-consumer-b",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_binding_authority_invalidation_wins_before_acknowledgement_commit() -> None:
    state = repository()
    prepare(state)
    state.invalidate_source_binding_authority(
        "tenant-a",
        "source-binding-a",
        invalidated_revision=3,
    )

    with pytest.raises(StaleAcquisitionRevisionError):
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")
    with pytest.raises(AcquisitionStateConflictError, match="invalidated"):
        state.admit_authority(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=3,
        )


def test_binding_authority_invalidation_is_idempotent_without_revoking_new_revision() -> None:
    state = repository()
    state.activate_contract_authority("tenant-a", "4" * 64)
    first_epoch, _contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
    )
    state.invalidate_source_binding_authority(
        "tenant-a",
        "source-binding-a",
        invalidated_revision=3,
    )
    next_epoch, _contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=4,
    )

    state.invalidate_source_binding_authority(
        "tenant-a",
        "source-binding-a",
        invalidated_revision=3,
    )

    replay_epoch, _contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=4,
    )
    assert first_epoch == 0
    assert next_epoch == replay_epoch == 1


def test_delayed_losing_binding_invalidation_cannot_revoke_newer_ready_revision() -> None:
    state = repository()
    state.activate_contract_authority("tenant-a", "4" * 64)
    state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=4,
    )

    with pytest.raises(AcquisitionStateConflictError, match="revision is stale"):
        state.invalidate_source_binding_authority(
            "tenant-a",
            "source-binding-a",
            invalidated_revision=3,
        )

    binding_epoch, _contract_epoch = state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=4,
    )
    assert binding_epoch == 0


def test_contract_authority_invalidation_wins_before_acknowledgement_commit() -> None:
    state = repository()
    prepare(state)
    state.invalidate_contract_authority("tenant-a", "4" * 64)

    with pytest.raises(StaleAcquisitionRevisionError):
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_contract_authority_invalidation_is_idempotent(tmp_path: Path) -> None:
    database_path = str(tmp_path / "contract-authority-idempotency.sqlite")
    state = repository(database_path)
    state.activate_contract_authority("tenant-a", "4" * 64)
    state.admit_authority(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
    )

    state.invalidate_contract_authority("tenant-a", "4" * 64)
    state.invalidate_contract_authority("tenant-a", "4" * 64)

    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT epoch, active FROM acquisition_contract_authorities "
        "WHERE tenant_id = ? AND contract_digest = ?",
        ("tenant-a", "4" * 64),
    ).fetchone()
    connection.close()
    assert row == (1, 0)

    with pytest.raises(AcquisitionStateConflictError, match="invalidated"):
        state.admit_authority(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            source_binding_ref="source-binding-a",
            source_binding_revision=3,
        )


def test_unknown_contract_invalidation_has_no_effect_and_retired_digest_cannot_reactivate() -> None:
    state = repository()

    with pytest.raises(AcquisitionStateConflictError, match="not activated"):
        state.invalidate_contract_authority("tenant-a", "4" * 64)

    assert state.activate_contract_authority("tenant-a", "4" * 64) == 0
    state.invalidate_contract_authority("tenant-a", "4" * 64)
    with pytest.raises(AcquisitionStateConflictError, match="cannot reactivate"):
        state.activate_contract_authority("tenant-a", "4" * 64)


@pytest.mark.parametrize(
    ("authority", "failure_point"),
    (
        ("binding", "binding_authority_invalidation_before"),
        ("binding", "binding_authority_invalidation_after"),
        ("contract", "contract_authority_invalidation_before"),
        ("contract", "contract_authority_invalidation_after"),
    ),
)
def test_authority_invalidation_fault_rolls_back_without_revoking_prepared_work(
    authority: str,
    failure_point: str,
) -> None:
    faults = FaultHook()
    state = repository(fault_hook=faults)
    prepare(state)
    faults.fail_at = failure_point

    with pytest.raises(AcquisitionStatePersistenceError):
        if authority == "binding":
            state.invalidate_source_binding_authority(
                "tenant-a",
                "source-binding-a",
                invalidated_revision=3,
            )
        else:
            state.invalidate_contract_authority("tenant-a", "4" * 64)

    faults.fail_at = None
    receipt = state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    assert receipt.committed_revision == 1


@pytest.mark.parametrize(
    ("authority", "failure_point"),
    (
        ("binding", "binding_authority_invalidation_after"),
        ("contract", "contract_authority_invalidation_after"),
    ),
)
def test_authority_invalidation_that_linearizes_first_defeats_concurrent_acknowledgement(
    tmp_path: Path,
    authority: str,
    failure_point: str,
) -> None:
    database_path = str(tmp_path / f"{authority}-invalidation-first.sqlite")
    blocker = BlockingFaultHook(failure_point)
    invalidating_state = concurrent_repository(database_path, fault_hook=blocker)
    acknowledging_state = concurrent_repository(database_path)
    prepare(invalidating_state)
    invalidation_errors: list[BaseException] = []
    acknowledgement_errors: list[BaseException] = []
    acknowledgement_started = threading.Event()

    def invalidate() -> None:
        try:
            if authority == "binding":
                invalidating_state.invalidate_source_binding_authority(
                    "tenant-a",
                    "source-binding-a",
                    invalidated_revision=3,
                )
            else:
                invalidating_state.invalidate_contract_authority("tenant-a", "4" * 64)
        except BaseException as error:
            invalidation_errors.append(error)

    def acknowledge() -> None:
        acknowledgement_started.set()
        try:
            acknowledging_state.acknowledge_exact(
                acknowledgement(),
                expected_consumer_ref="warehouse-consumer-a",
                provider_kind="postgresql",
                cursor_version="postgresql-compound-v1",
            )
        except BaseException as error:
            acknowledgement_errors.append(error)

    invalidation_thread = threading.Thread(target=invalidate)
    invalidation_thread.start()
    blocker.entered.wait(timeout=5)
    acknowledgement_thread = threading.Thread(target=acknowledge)
    acknowledgement_thread.start()
    assert acknowledgement_started.wait(timeout=5)
    blocker.release.wait(timeout=5)
    invalidation_thread.join(timeout=5)
    acknowledgement_thread.join(timeout=5)

    assert not invalidation_thread.is_alive()
    assert not acknowledgement_thread.is_alive()
    assert invalidation_errors == []
    assert len(acknowledgement_errors) == 1
    assert isinstance(acknowledgement_errors[0], StaleAcquisitionRevisionError)
    with pytest.raises(AcquisitionStateNotFoundError):
        acknowledging_state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


@pytest.mark.parametrize(
    ("authority", "failure_point"),
    (
        ("binding", "binding_authority_invalidation_after"),
        ("contract", "contract_authority_invalidation_after"),
    ),
)
def test_acknowledgement_that_linearizes_first_commits_before_concurrent_invalidation(
    tmp_path: Path,
    authority: str,
    failure_point: str,
) -> None:
    database_path = str(tmp_path / f"{authority}-acknowledgement-first.sqlite")
    blocker = BlockingFaultHook("acknowledgement_after")
    acknowledging_state = concurrent_repository(database_path, fault_hook=blocker)
    invalidating_state = concurrent_repository(database_path)
    prepare(acknowledging_state)
    acknowledgement_errors: list[BaseException] = []
    invalidation_errors: list[BaseException] = []
    receipts: list[AcquisitionCheckpointReceipt] = []
    invalidation_started = threading.Event()

    def acknowledge() -> None:
        try:
            receipts.append(
                acknowledging_state.acknowledge_exact(
                    acknowledgement(),
                    expected_consumer_ref="warehouse-consumer-a",
                    provider_kind="postgresql",
                    cursor_version="postgresql-compound-v1",
                )
            )
        except BaseException as error:
            acknowledgement_errors.append(error)

    def invalidate() -> None:
        invalidation_started.set()
        try:
            if authority == "binding":
                invalidating_state.invalidate_source_binding_authority(
                    "tenant-a",
                    "source-binding-a",
                    invalidated_revision=3,
                )
            else:
                invalidating_state.invalidate_contract_authority("tenant-a", "4" * 64)
        except BaseException as error:
            invalidation_errors.append(error)

    acknowledgement_thread = threading.Thread(target=acknowledge)
    acknowledgement_thread.start()
    blocker.entered.wait(timeout=5)
    invalidation_thread = threading.Thread(target=invalidate)
    invalidation_thread.start()
    assert invalidation_started.wait(timeout=5)
    blocker.release.wait(timeout=5)
    acknowledgement_thread.join(timeout=5)
    invalidation_thread.join(timeout=5)

    assert not acknowledgement_thread.is_alive()
    assert not invalidation_thread.is_alive()
    assert acknowledgement_errors == []
    assert invalidation_errors == []
    assert len(receipts) == 1
    assert invalidating_state.load_acknowledgement_replay(acknowledgement()) == receipts[0]


@pytest.mark.parametrize("exact_replay", (True, False))
def test_concurrent_preparation_transactions_converge_only_for_exact_output(
    tmp_path: Path,
    exact_replay: bool,
) -> None:
    database_path = str(tmp_path / f"preparation-{exact_replay}.sqlite")
    blocker = BlockingFaultHook("prepared_state_after")
    first_state = concurrent_repository(database_path, fault_hook=blocker)
    competing_state = concurrent_repository(database_path)
    winner_receipt = prepared_receipt()
    competing_receipt = (
        winner_receipt
        if exact_replay
        else winner_receipt.model_copy(update={"prepared_receipt_id": "prepared-ref-competing"})
    )
    results: list[PreparedAcquisitionState] = []
    errors: list[BaseException] = []
    competing_started = threading.Event()

    def prepare_first() -> None:
        try:
            results.append(prepare(first_state, receipt=winner_receipt))
        except BaseException as error:
            errors.append(error)

    def prepare_competing() -> None:
        competing_started.set()
        try:
            results.append(prepare(competing_state, receipt=competing_receipt))
        except BaseException as error:
            errors.append(error)

    first_thread = threading.Thread(target=prepare_first)
    first_thread.start()
    blocker.entered.wait(timeout=5)
    competing_thread = threading.Thread(target=prepare_competing)
    competing_thread.start()
    assert competing_started.wait(timeout=5)
    blocker.release.wait(timeout=5)
    first_thread.join(timeout=5)
    competing_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not competing_thread.is_alive()
    if exact_replay:
        assert errors == []
        assert len(results) == 2
        assert results[0] == results[1]
    else:
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AcquisitionStateConflictError)


def test_contradictory_acknowledgement_replay_never_moves_checkpoint_twice() -> None:
    state = repository()
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    with pytest.raises(AcquisitionStateConflictError, match="contradictory acknowledgement"):
        state.acknowledge_exact(
            accepted.model_copy(update={"consumer_receipt_digest": "e" * 64}),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a").revision == 1


@pytest.mark.parametrize(
    "failure_point",
    (
        "prepared_state_before",
        "prepared_state_after",
        "prepare_evidence",
        "prepare_evidence_after",
        "acknowledged_state_before",
        "acknowledged_state_after",
        "acknowledgement_before",
        "acknowledgement_after",
        "checkpoint_before",
        "checkpoint_after",
        "checkpoint_receipt_before",
        "checkpoint_receipt_after",
        "acknowledgement_evidence",
        "acknowledgement_evidence_after",
    ),
)
def test_evidence_write_failure_rolls_back_the_complete_state_transaction(
    failure_point: str,
) -> None:
    faults = FaultHook()
    state = repository(fault_hook=faults)
    if failure_point.startswith("prepare") or failure_point.startswith("prepared"):
        faults.fail_at = failure_point

        with pytest.raises(AcquisitionStatePersistenceError) as captured:
            prepare(state)

        assert "private-cursor-canary" not in str(captured.value)
        assert captured.value.__cause__ is None

        with pytest.raises(AcquisitionStateNotFoundError):
            state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)
        return

    prepare(state)
    faults.fail_at = failure_point

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert "private-cursor-canary" not in str(captured.value)
    assert captured.value.__cause__ is None

    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_rollback_failure_never_replaces_the_primary_failure() -> None:
    class RollbackFailingConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise sqlite3.OperationalError("rollback-secret-canary")

    def connection_factory(database_path: str) -> sqlite3.Connection:
        return sqlite3.connect(database_path, factory=RollbackFailingConnection)

    faults = FaultHook()
    faults.fail_at = "prepare_evidence"
    state = repository(fault_hook=faults, connection_factory=connection_factory)

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        prepare(state)

    assert "rollback-secret-canary" not in str(captured.value)
    assert "private-cursor-canary" not in str(captured.value)


def test_checkpoint_compare_and_set_advances_each_revision_exactly_once() -> None:
    state = repository()
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    second_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    second_prepared = prepared_receipt(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "prepared_receipt_id": "prepared-ref-b",
            "intent_key": "7" * 64,
            "batch_id": "8" * 64,
            "prepared_at": LATER,
        },
    )
    prepare(state, receipt=second_prepared, cursor=second_cursor)
    second_acknowledgement = acknowledgement(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "acknowledgement_id": "acknowledgement-b",
            "batch_id": second_prepared.batch_id,
            "batch_manifest_digest": second_prepared.batch_manifest_digest,
            "consumer_receipt_digest": "9" * 64,
            "acknowledged_at": datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC),
        },
    )

    receipt = state.acknowledge_exact(
        second_acknowledgement,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    replay = state.acknowledge_exact(
        second_acknowledgement,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    assert replay == receipt
    assert receipt.previous_revision == 1
    assert receipt.committed_revision == 2
    checkpoint, cursor = state.load_checkpoint_with_cursor("tenant-a", "4" * 64, "source-binding-a")
    assert checkpoint.revision == 2
    assert checkpoint.created_at == LATER
    assert checkpoint.updated_at == second_acknowledgement.acknowledged_at
    assert cursor == second_cursor


def test_pending_second_revision_preparation_replays_against_its_exact_checkpoint() -> None:
    state = repository()
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    second_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    second_receipt = prepared_receipt(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "prepared_receipt_id": "prepared-ref-b",
            "intent_key": "7" * 64,
            "batch_id": "8" * 64,
            "prepared_at": LATER,
        },
    )
    pending = prepare(state, receipt=second_receipt, cursor=second_cursor)

    replayed_state, replayed_receipt = state.load_preparation_for_replay(
        "tenant-a",
        "4" * 64,
        "source-binding-a",
        1,
    )

    assert replayed_state == pending
    assert replayed_receipt == second_receipt


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    (
        ("provider_kind", "stripe", AcquisitionStateConflictError),
        ("cursor_version", "postgresql-compound-v2", AcquisitionStateConflictError),
        (
            "updated_at",
            datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC),
            AcquisitionStatePersistenceError,
        ),
    ),
)
def test_second_revision_replay_revalidates_prior_checkpoint_admission(
    tmp_path: Path,
    field: str,
    value: object,
    expected_error: type[Exception],
) -> None:
    database_path = str(tmp_path / "prior-checkpoint-admission.sqlite")
    state = repository(database_path)
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    second_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    second_prepared = prepared_receipt(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "prepared_receipt_id": "prepared-ref-b",
            "intent_key": "7" * 64,
            "batch_id": "8" * 64,
            "prepared_at": LATER,
        },
    )
    prepare(state, receipt=second_prepared, cursor=second_cursor)
    second_acknowledgement = acknowledgement(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "acknowledgement_id": "acknowledgement-b",
            "batch_id": second_prepared.batch_id,
            "batch_manifest_digest": second_prepared.batch_manifest_digest,
            "consumer_receipt_digest": "9" * 64,
            "acknowledged_at": datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC),
        },
    )
    state.acknowledge_exact(
        second_acknowledgement,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoints WHERE tenant_id = ? AND revision = 1",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prior_checkpoint = SourceCheckpointState.model_validate_json(row[0]).model_copy(
        update={field: value}
    )
    connection.execute(
        "UPDATE acquisition_checkpoints SET payload = ? WHERE tenant_id = ? AND revision = 1",
        (prior_checkpoint.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(expected_error):
        state.acknowledge_exact(
            second_acknowledgement,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize(
    ("provider_kind", "cursor_version"),
    (
        ("stripe", "postgresql-compound-v1"),
        ("postgresql", "postgresql-compound-v2"),
    ),
)
def test_next_revision_cannot_change_cursor_semantics(
    provider_kind: Literal["postgresql", "stripe"],
    cursor_version: str,
) -> None:
    state = repository()
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    second_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    second_prepared = prepared_receipt(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "prepared_receipt_id": "prepared-ref-b",
            "intent_key": "7" * 64,
            "batch_id": "8" * 64,
            "prepared_at": LATER,
        },
    )
    prepare(state, receipt=second_prepared, cursor=second_cursor)
    second_acknowledgement = acknowledgement(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "acknowledgement_id": "acknowledgement-b",
            "batch_id": second_prepared.batch_id,
            "batch_manifest_digest": second_prepared.batch_manifest_digest,
        },
    )

    with pytest.raises(AcquisitionStateConflictError, match="cursor semantics"):
        state.acknowledge_exact(
            second_acknowledgement,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind=provider_kind,
            cursor_version=cursor_version,
        )

    assert state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a").revision == 1
    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 1).state
        is PreparedAcquisitionStateStatus.PREPARED
    )


def test_stale_preparation_cannot_skip_the_current_checkpoint_revision() -> None:
    state = repository()
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    with pytest.raises(StaleAcquisitionRevisionError, match="revision is stale"):
        prepare(
            state,
            receipt=prepared_receipt(
                prior_revision=2,
                update={
                    "prepared_receipt_id": "prepared-ref-stale",
                    "intent_key": "a" * 64,
                    "batch_id": "b" * 64,
                },
            ),
        )


def test_repository_reopens_committed_checkpoint_verbatim(tmp_path: Path) -> None:
    database_path = str(tmp_path / "reopen.sqlite")
    state = repository(database_path)
    prepare(state)
    expected = state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    state.close()

    reopened = repository(database_path)

    assert reopened.load_checkpoint("tenant-a", "4" * 64, "source-binding-a").revision == 1
    assert (
        reopened.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )
        == expected
    )


@pytest.mark.parametrize(
    "delete_statement",
    (
        "DELETE FROM acquisition_acknowledgements",
        "DELETE FROM acquisition_checkpoints",
        "DELETE FROM acquisition_checkpoint_receipts",
        "DELETE FROM acquisition_state_evidence WHERE event_type = 'prepared'",
        "DELETE FROM acquisition_state_evidence WHERE event_type = 'acknowledged'",
    ),
)
def test_acknowledgement_replay_requires_the_complete_atomic_fact_set(
    tmp_path: Path,
    delete_statement: str,
) -> None:
    database_path = str(tmp_path / "incomplete-replay.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    connection.execute(delete_statement)
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


def test_acknowledgement_replay_rejects_corrupted_receipt_linkage(tmp_path: Path) -> None:
    database_path = str(tmp_path / "receipt-linkage.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoint_receipts WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    payload = row[0].replace(b'"batch_id":"' + b"2" * 64, b'"batch_id":"' + b"f" * 64)
    connection.execute(
        "UPDATE acquisition_checkpoint_receipts SET payload = ? WHERE tenant_id = ?",
        (payload, "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="authority"):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize("corruption", ("prepared_at", "acknowledged_state_updated_at"))
def test_acknowledgement_replay_rejects_each_corrupted_lifecycle_timestamp(
    tmp_path: Path,
    corruption: Literal["prepared_at", "acknowledged_state_updated_at"],
) -> None:
    database_path = str(tmp_path / "lifecycle-timestamp.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT state_payload, receipt_payload FROM acquisition_prepared_states "
        "WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prepared = PreparedAcquisitionState.model_validate_json(row[0])
    receipt = AcquisitionPreparedReceipt.model_validate_json(row[1])
    if corruption == "prepared_at":
        receipt = receipt.model_copy(
            update={"prepared_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)}
        )
    else:
        prepared = prepared.model_copy(
            update={"updated_at": datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC)}
        )
    connection.execute(
        "UPDATE acquisition_prepared_states SET state_payload = ?, receipt_payload = ? "
        "WHERE tenant_id = ?",
        (
            prepared.model_dump_json().encode(),
            receipt.model_dump_json().encode(),
            "tenant-a",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-b"),
        ("contract_digest", "a" * 64),
        ("source_binding_ref", "source-binding-b"),
        ("revision", 2),
        ("encrypted_cursor_payload", b"cipher:tenant-a:corrupted"),
        ("cursor_digest", "f" * 64),
        ("last_batch_id", "f" * 64),
        ("created_at", NOW),
        ("updated_at", datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC)),
    ),
)
def test_acknowledgement_replay_rejects_each_corrupted_checkpoint_link(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database_path = str(tmp_path / "checkpoint-linkage.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoints WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    checkpoint = SourceCheckpointState.model_validate_json(row[0])
    corrupted = checkpoint.model_copy(update={field: value})
    connection.execute(
        "UPDATE acquisition_checkpoints SET payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-b"),
        ("contract_digest", "a" * 64),
        ("source_binding_ref", "source-binding-b"),
        ("previous_revision", 1),
        ("committed_revision", 2),
        ("cursor_digest", "f" * 64),
        ("batch_id", "f" * 64),
        ("acknowledgement_id", "acknowledgement-b"),
        ("committed_at", datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC)),
    ),
)
def test_acknowledgement_replay_rejects_each_corrupted_receipt_authority(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database_path = str(tmp_path / "receipt-authority.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoint_receipts WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    receipt = AcquisitionCheckpointReceipt.model_validate_json(row[0])
    corrupted = receipt.model_copy(update={field: value})
    connection.execute(
        "UPDATE acquisition_checkpoint_receipts SET payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


def test_acknowledgement_replay_requires_the_exact_prepared_cursor_ciphertext(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "prepared-cursor-linkage.sqlite")
    cipher = NonceCipher()
    state = SQLiteAcquisitionStateRepository(
        database_path,
        cipher=cipher,
        reference_factory=ReferenceFactory(),
    )
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT state_payload FROM acquisition_prepared_states WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prepared = PreparedAcquisitionState.model_validate_json(row[0])
    alternate_ciphertext = cipher.encrypt(tenant_id="tenant-a", plaintext=CURSOR)
    assert alternate_ciphertext != prepared.candidate_cursor_ciphertext
    corrupted = prepared.model_copy(update={"candidate_cursor_ciphertext": alternate_ciphertext})
    connection.execute(
        "UPDATE acquisition_prepared_states SET state_payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="checkpoint authority"):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


def test_acknowledgement_replay_rejects_coupled_receipt_and_evidence_tenant_corruption(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "receipt-evidence-tenant.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoint_receipts WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    receipt = AcquisitionCheckpointReceipt.model_validate_json(row[0])
    corrupted = receipt.model_copy(update={"tenant_id": "tenant-b"})
    connection.execute(
        "UPDATE acquisition_checkpoint_receipts SET payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.execute(
        "UPDATE acquisition_state_evidence SET tenant_id = ? WHERE event_type = ?",
        ("tenant-b", "acknowledged"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


def test_acknowledgement_replay_rejects_corrupted_acknowledgement_index(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "acknowledgement-index.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE acquisition_acknowledgements SET acknowledgement_digest = ?",
        ("f" * 64,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


@pytest.mark.parametrize("corruption", ("event_type", "payload"))
def test_acknowledgement_replay_rejects_each_corrupted_evidence_dimension(
    tmp_path: Path,
    corruption: Literal["event_type", "payload"],
) -> None:
    database_path = str(tmp_path / "evidence-authority.sqlite")
    state = repository(database_path)
    prepare(state)
    accepted = acknowledgement()
    state.acknowledge_exact(
        accepted,
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    if corruption == "event_type":
        connection.execute(
            "UPDATE acquisition_state_evidence SET event_type = ? WHERE event_type = ?",
            ("corrupted", "acknowledged"),
        )
    else:
        connection.execute(
            "UPDATE acquisition_state_evidence SET payload = ? WHERE event_type = ?",
            (canonical_bytes({"event_type": "acknowledged"}), "acknowledged"),
        )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError):
        state.acknowledge_exact(
            accepted,
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )


def test_tenant_qualified_checkpoint_decryption_verifies_cursor_integrity(
    tmp_path: Path,
) -> None:
    database_path = str(tmp_path / "checkpoint-cursor-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoints WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    checkpoint = SourceCheckpointState.model_validate_json(row[0])
    corrupted = checkpoint.model_copy(
        update={"encrypted_cursor_payload": b"cipher:tenant-a:corrupted"}
    )
    connection.execute(
        "UPDATE acquisition_checkpoints SET payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="cursor integrity"):
        state.load_checkpoint_with_cursor("tenant-a", "4" * 64, "source-binding-a")
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint_with_cursor("tenant-b", "4" * 64, "source-binding-a")


def test_acknowledgement_cannot_predate_preparation() -> None:
    state = repository()
    prepare(state)

    with pytest.raises(AcquisitionStateConflictError, match="timestamp"):
        state.acknowledge_exact(
            acknowledgement(
                update={"acknowledged_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)}
            ),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )


def test_next_preparation_cannot_predate_the_current_checkpoint() -> None:
    state = repository()
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    with pytest.raises(AcquisitionStateConflictError, match="timestamp"):
        prepare(
            state,
            receipt=prepared_receipt(
                prior_revision=1,
                update={
                    "prepared_at": NOW,
                    "prepared_receipt_id": "prepared-ref-b",
                },
            ),
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 1)


@pytest.mark.parametrize("phase", ("prepare", "acknowledge"))
def test_current_checkpoint_cursor_is_verified_before_the_next_revision(
    tmp_path: Path,
    phase: Literal["prepare", "acknowledge"],
) -> None:
    database_path = str(tmp_path / "current-cursor-integrity.sqlite")
    state = repository(database_path)
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    second_cursor = b'{"updated_at":"2026-09-01T12:00:01Z","id":8}'
    second_prepared = prepared_receipt(
        prior_revision=1,
        cursor=second_cursor,
        update={
            "prepared_receipt_id": "prepared-ref-b",
            "intent_key": "7" * 64,
            "batch_id": "8" * 64,
            "prepared_at": LATER,
        },
    )
    if phase == "acknowledge":
        prepare(state, receipt=second_prepared, cursor=second_cursor)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoints WHERE tenant_id = ? AND revision = 1",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    checkpoint = SourceCheckpointState.model_validate_json(row[0]).model_copy(
        update={"encrypted_cursor_payload": b"cipher:tenant-a:corrupted"}
    )
    connection.execute(
        "UPDATE acquisition_checkpoints SET payload = ? WHERE tenant_id = ? AND revision = 1",
        (checkpoint.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="cursor integrity"):
        if phase == "prepare":
            prepare(state, receipt=second_prepared, cursor=second_cursor)
        else:
            state.acknowledge_exact(
                acknowledgement(
                    prior_revision=1,
                    cursor=second_cursor,
                    update={
                        "acknowledgement_id": "acknowledgement-b",
                        "batch_id": second_prepared.batch_id,
                        "batch_manifest_digest": second_prepared.batch_manifest_digest,
                        "consumer_receipt_digest": "9" * 64,
                    },
                ),
                expected_consumer_ref="warehouse-consumer-a",
                provider_kind="postgresql",
                cursor_version="postgresql-compound-v1",
            )


def test_receipt_reference_failure_rolls_back_acknowledgement() -> None:
    def failing_reference_factory(_kind: str) -> str:
        raise RuntimeError("reference-secret-canary")

    state = SQLiteAcquisitionStateRepository(
        ":memory:",
        cipher=Cipher(),
        reference_factory=failing_reference_factory,
    )
    prepare(state)

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert "reference-secret-canary" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_acknowledgement_detects_checkpoint_moved_after_preparation(tmp_path: Path) -> None:
    database_path = str(tmp_path / "checkpoint-race.sqlite")
    state = repository(database_path)
    pending = prepare(state)
    competing_checkpoint = SourceCheckpointState(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        source_binding_ref="source-binding-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
        revision=1,
        encrypted_cursor_payload=pending.candidate_cursor_ciphertext,
        cursor_digest=pending.candidate_checkpoint_digest,
        last_batch_id="f" * 64,
        created_at=NOW,
        updated_at=NOW,
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO acquisition_checkpoints "
        "(tenant_id, contract_digest, source_binding_ref, revision, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            competing_checkpoint.tenant_id,
            competing_checkpoint.contract_digest,
            competing_checkpoint.source_binding_ref,
            competing_checkpoint.revision,
            competing_checkpoint.model_dump_json().encode(),
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(StaleAcquisitionRevisionError):
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind="postgresql",
            cursor_version="postgresql-compound-v1",
        )

    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )


def test_acknowledgement_requests_an_independent_checkpoint_reference() -> None:
    requested_kinds: list[str] = []

    def recording_reference_factory(kind: str) -> str:
        requested_kinds.append(kind)
        return "opaque-checkpoint-reference"

    state = SQLiteAcquisitionStateRepository(
        ":memory:",
        cipher=Cipher(),
        reference_factory=recording_reference_factory,
    )
    prepare(state)

    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )

    assert requested_kinds == ["checkpoint"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-b"),
        ("contract_digest", "a" * 64),
        ("source_binding_ref", "source-binding-b"),
        ("revision", 2),
    ),
)
def test_repository_rejects_valid_payload_with_corrupted_checkpoint_authority(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database_path = str(tmp_path / "checkpoint-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT payload FROM acquisition_checkpoints WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    checkpoint = SourceCheckpointState.model_validate_json(row[0])
    corrupted = checkpoint.model_copy(update={field: value})
    connection.execute(
        "UPDATE acquisition_checkpoints SET payload = ? WHERE tenant_id = ?",
        (corrupted.model_dump_json().encode(), "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="authority"):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_repository_rejects_corrupted_checkpoint_index_revision(tmp_path: Path) -> None:
    database_path = str(tmp_path / "checkpoint-index-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE acquisition_checkpoints SET revision = 2 WHERE tenant_id = ? AND revision = 1",
        ("tenant-a",),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="authority"):
        state.load_checkpoint("tenant-a", "4" * 64, "source-binding-a")


def test_repository_rejects_corrupted_prepared_receipt_linkage(tmp_path: Path) -> None:
    database_path = str(tmp_path / "prepared-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT receipt_payload FROM acquisition_prepared_states WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    payload = row[0].replace(b'"batch_id":"' + b"2" * 64, b'"batch_id":"' + b"f" * 64)
    connection.execute(
        "UPDATE acquisition_prepared_states SET receipt_payload = ? WHERE tenant_id = ?",
        (payload, "tenant-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="authority"):
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)


@pytest.mark.parametrize(
    ("state_update", "receipt_update"),
    (
        ({"tenant_id": "tenant-b"}, {"tenant_id": "tenant-b"}),
        ({"contract_digest": "a" * 64}, {}),
        ({"source_binding_ref": "source-binding-b"}, {}),
        ({"prior_checkpoint_revision": 1}, {"prior_checkpoint_revision": 1}),
        ({}, {"tenant_id": "tenant-b"}),
        ({}, {"intent_key": "a" * 64}),
        ({}, {"batch_id": "b" * 64}),
        ({}, {"batch_manifest_digest": "c" * 64}),
        ({}, {"prior_checkpoint_revision": 1}),
        ({}, {"candidate_checkpoint_digest": "d" * 64}),
    ),
)
def test_repository_rejects_each_corrupted_prepared_authority_dimension(
    tmp_path: Path,
    state_update: dict[str, object],
    receipt_update: dict[str, object],
) -> None:
    database_path = str(tmp_path / "prepared-authority-corruption.sqlite")
    state = repository(database_path)
    prepare(state)
    connection = sqlite3.connect(database_path)
    row = connection.execute(
        "SELECT state_payload, receipt_payload FROM acquisition_prepared_states "
        "WHERE tenant_id = ?",
        ("tenant-a",),
    ).fetchone()
    assert row is not None
    prepared = PreparedAcquisitionState.model_validate_json(row[0]).model_copy(update=state_update)
    receipt = AcquisitionPreparedReceipt.model_validate_json(row[1]).model_copy(
        update=receipt_update
    )
    connection.execute(
        "UPDATE acquisition_prepared_states SET state_payload = ?, receipt_payload = ? "
        "WHERE tenant_id = ?",
        (
            prepared.model_dump_json().encode(),
            receipt.model_dump_json().encode(),
            "tenant-a",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="authority"):
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)


def test_state_evidence_contains_only_the_allowlisted_receipt_projection(tmp_path: Path) -> None:
    database_path = str(tmp_path / "evidence.sqlite")
    state = repository(database_path)
    prepare(state)
    receipt = state.acknowledge_exact(
        acknowledgement(),
        expected_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
    )
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        "SELECT event_type, payload FROM acquisition_state_evidence ORDER BY event_type"
    ).fetchall()
    connection.close()

    assert [(event_type, json.loads(payload)) for event_type, payload in rows] == [
        (
            "acknowledged",
            {
                "checkpoint_receipt_ref": receipt.checkpoint_receipt_id,
                "committed_revision": 1,
                "event_type": "acknowledged",
            },
        ),
        (
            "prepared",
            {
                "event_type": "prepared",
                "prepared_receipt_ref": "prepared-ref-a",
                "prior_checkpoint_revision": 0,
            },
        ),
    ]


def test_repository_rejects_live_schema_definition_drift(tmp_path: Path) -> None:
    database_path = str(tmp_path / "schema-drift.sqlite")
    state = repository(database_path)
    state.close()
    connection = sqlite3.connect(database_path)
    connection.execute("DROP TABLE acquisition_acknowledgements")
    connection.execute(
        "CREATE TABLE acquisition_acknowledgements ("
        "tenant_id TEXT NOT NULL, acknowledgement_id TEXT NOT NULL, "
        "acknowledgement_digest TEXT NOT NULL, payload BLOB NOT NULL)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(AcquisitionStatePersistenceError, match="schema definition"):
        repository(database_path)


def test_revalidation_sanitizes_an_untrusted_serializer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = prepared_receipt()

    def leaking_serializer(_self: AcquisitionPreparedReceipt) -> str:
        raise RuntimeError("serializer-secret-canary")

    monkeypatch.setattr(AcquisitionPreparedReceipt, "model_dump_json", leaking_serializer)
    state = repository()

    with pytest.raises(AcquisitionStateConflictError) as captured:
        prepare(state, receipt=receipt)

    assert "serializer-secret-canary" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_model_construction_sanitizes_invalid_boundary_values() -> None:
    state = repository()

    with pytest.raises(AcquisitionStateConflictError) as invalid_contract:
        state.admit_authority(
            tenant_id="tenant-a",
            contract_digest="contract-secret-canary",
            source_binding_ref="source-binding-a",
            source_binding_revision=3,
        )

    assert "contract-secret-canary" not in str(invalid_contract.value)
    assert invalid_contract.value.__cause__ is None

    prepare(state)
    with pytest.raises(AcquisitionStateConflictError) as invalid_provider:
        state.acknowledge_exact(
            acknowledgement(),
            expected_consumer_ref="warehouse-consumer-a",
            provider_kind=cast(AcquisitionProviderKind, "provider-secret-canary"),
            cursor_version="postgresql-compound-v1",
        )

    assert "provider-secret-canary" not in str(invalid_provider.value)
    assert invalid_provider.value.__cause__ is None
    assert (
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0).state
        is PreparedAcquisitionStateStatus.PREPARED
    )


def test_persisted_payload_revalidation_sanitizes_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repository()
    prepare(state)

    def leaking_parser(
        _model: type[PreparedAcquisitionState],
        _payload: object,
    ) -> PreparedAcquisitionState:
        raise RuntimeError("parser-secret-canary")

    monkeypatch.setattr(
        PreparedAcquisitionState,
        "model_validate_json",
        classmethod(leaking_parser),
    )

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        state.load_prepared("tenant-a", "4" * 64, "source-binding-a", 0)

    assert "parser-secret-canary" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_close_failure_is_typed_without_driver_details() -> None:
    class CloseFailingConnection(sqlite3.Connection):
        def close(self) -> None:
            raise sqlite3.OperationalError("close-secret-canary")

    def connection_factory(database_path: str) -> sqlite3.Connection:
        return sqlite3.connect(database_path, factory=CloseFailingConnection)

    state = repository(connection_factory=connection_factory)

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        state.close()

    assert "close-secret-canary" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_sqlite_transaction_failure_does_not_retain_driver_cause() -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def commit(self) -> None:
            if self.fail_commit:
                raise sqlite3.OperationalError("commit-secret-canary")
            super().commit()

    connection: CommitFailingConnection | None = None

    def connection_factory(database_path: str) -> sqlite3.Connection:
        nonlocal connection
        connection = sqlite3.connect(database_path, factory=CommitFailingConnection)
        return connection

    state = repository(connection_factory=connection_factory)
    assert connection is not None
    connection.fail_commit = True

    with pytest.raises(AcquisitionStatePersistenceError) as captured:
        prepare(state)

    assert "commit-secret-canary" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_governed_outcome_persistence_is_tenant_qualified_and_exact() -> None:
    state = repository()
    outcome = AcquisitionNoValidPlan(
        reason_codes=("physical_delete_capture_unsupported",),
        failed_constraints=("contract:delete-guarantee",),
    )

    first = state.record_governed_outcome(
        tenant_id="tenant-a",
        outcome_id="outcome-a",
        outcome=outcome,
        created_at=NOW,
    )
    replay = state.record_governed_outcome(
        tenant_id="tenant-a",
        outcome_id="outcome-a",
        outcome=outcome,
        created_at=NOW,
    )

    assert first == replay
    assert state.load_governed_outcome("tenant-a", "outcome-a") == first
    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_governed_outcome("tenant-b", "outcome-a")
    with pytest.raises(AcquisitionStateConflictError):
        state.record_governed_outcome(
            tenant_id="tenant-a",
            outcome_id="outcome-a",
            outcome=AcquisitionNoValidPlan(
                reason_codes=("contract_not_activated",),
                failed_constraints=("different",),
            ),
            created_at=NOW,
        )
