from __future__ import annotations

import io
import tempfile
import threading
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

import pytest
from heinzel_connection_broker import SourceConnectionBindingState
from heinzel_evidence import AcquisitionEvidenceReceipt
from heinzel_provider_sdk import AcquisitionAcknowledgement, AcquisitionIntent
from heinzel_runtime import (
    AcquisitionAuthorizationError,
    AcquisitionIntegrityError,
    AcquisitionPreparationResult,
    AcquisitionRunner,
    AcquisitionStaleRevision,
)
from heinzel_state import (
    AcquisitionArtifactStoreError,
    AcquisitionStateNotFoundError,
    SQLiteAcquisitionStateRepository,
)

from services.runtime.tests.test_acquisition import NOW, _intent, _record, _runner, _schema
from services.state.tests.test_repository import Cipher, concurrent_repository


class _FailOnceEvidenceWriter:
    def __init__(
        self,
        append: Callable[[AcquisitionEvidenceReceipt], None],
    ) -> None:
        self._append = append
        self._failed = False

    def append(self, receipt: AcquisitionEvidenceReceipt) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("private-evidence-fault")
        self._append(receipt)


class _ArtifactBoundaryFault:
    def __init__(self, fail_at: str) -> None:
        self.fail_at: str | None = fail_at

    def __call__(self, checkpoint: str) -> None:
        if checkpoint == self.fail_at:
            raise RuntimeError("private-artifact-boundary-fault")


class _CloseFailingBytesIO(io.BytesIO):
    def close(self) -> None:
        super().close()
        raise OSError("private-cleanup-fault")


def _state() -> SQLiteAcquisitionStateRepository:
    return SQLiteAcquisitionStateRepository(
        ":memory:",
        cipher=Cipher(),
        reference_factory=lambda kind: f"opaque-{kind}-receipt",
    )


def _acknowledgement(
    preparation: AcquisitionPreparationResult,
    intent: AcquisitionIntent,
) -> AcquisitionAcknowledgement:
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    return AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:recovery-1",
        tenant_id=intent.tenant_id,
        consumer_ref="strict-consumer",
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        batch_id=prepared_receipt.batch_id,
        batch_manifest_digest=prepared_receipt.batch_manifest_digest,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest=prepared_receipt.candidate_checkpoint_digest,
        consumer_receipt_digest="9" * 64,
        acknowledged_at=NOW,
    )


def _activate_contract(
    state: SQLiteAcquisitionStateRepository,
    intent: AcquisitionIntent,
) -> None:
    state.activate_contract_authority(intent.tenant_id, intent.contract_digest)


def test_prepared_evidence_failure_replays_durable_batch_without_reacquiring() -> None:
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        _fake_state,
        _artifacts,
        evidence,
        _events,
    ) = _runner()
    state = _state()
    runner._state_store = state
    runner._evidence_writer = _FailOnceEvidenceWriter(evidence.append)
    intent = _intent(observation)
    _activate_contract(state, intent)

    with pytest.raises(AcquisitionIntegrityError, match="evidence_write_failed"):
        runner.prepare(intent)

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint(intent.tenant_id, intent.contract_digest, intent.source_binding_ref)

    replay = runner.prepare(intent)

    assert replay.prepared_receipt is not None
    assert provider.calls == 1
    assert evidence.receipts[-1].outcome == "prepared"


def test_failure_after_artifact_write_leaves_safe_orphan_and_retry_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        state,
        artifacts,
        _evidence,
        _events,
    ) = _runner()
    intent = _intent(observation)
    original_put = artifacts.put_if_absent
    write_count = 0

    def fail_after_first_write(*, tenant_id: str, artifact_digest: str, reader: BinaryIO) -> None:
        nonlocal write_count
        original_put(tenant_id=tenant_id, artifact_digest=artifact_digest, reader=reader)
        write_count += 1
        if write_count == 1:
            raise AcquisitionArtifactStoreError(operation="put", detail="private-artifact-fault")

    monkeypatch.setattr(artifacts, "put_if_absent", fail_after_first_write)

    with pytest.raises(AcquisitionIntegrityError, match="artifact_publication_failed"):
        runner.prepare(intent)

    assert len(artifacts.payloads) == 1
    assert state.prepared_states == {}

    monkeypatch.setattr(artifacts, "put_if_absent", original_put)
    replay = runner.prepare(intent)

    assert replay.prepared_receipt is not None
    assert provider.calls == 2


@pytest.mark.parametrize(
    ("failure_point", "expected_orphan_count"),
    (
        ("segment_artifact_0_before", 0),
        ("segment_artifact_0_after", 1),
        ("manifest_artifact_before", 1),
        ("manifest_artifact_after", 2),
    ),
)
def test_every_artifact_write_boundary_leaves_only_safe_orphans_and_retry_converges(
    failure_point: str,
    expected_orphan_count: int,
) -> None:
    fault = _ArtifactBoundaryFault(failure_point)
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        state,
        artifacts,
        _evidence,
        _events,
    ) = _runner(fault_hook=fault)
    intent = _intent(observation)

    with pytest.raises(AcquisitionIntegrityError, match="artifact_publication_failed") as error:
        runner.prepare(intent)

    assert "private-artifact-boundary-fault" not in str(error.value)
    assert len(artifacts.payloads) == expected_orphan_count
    assert state.prepared_states == {}

    fault.fail_at = None
    preparation = runner.prepare(intent)

    assert preparation.prepared_receipt is not None
    assert provider.calls == 2


@pytest.mark.parametrize(
    ("failure_point", "expected_orphan_count"),
    (("segment_artifact_1_before", 1), ("segment_artifact_1_after", 2)),
)
def test_each_segment_position_has_independent_recoverable_write_boundaries(
    failure_point: str,
    expected_orphan_count: int,
) -> None:
    fault = _ArtifactBoundaryFault(failure_point)
    orders = _schema()
    customers = orders.model_copy(update={"logical_object_ref": "customers"})
    schemas = (customers, orders)
    records = (
        _record("1").model_copy(update={"logical_object_ref": "customers"}),
        _record("2"),
    )
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        state,
        artifacts,
        _evidence,
        _events,
    ) = _runner(
        records=records,
        schema=schemas,
        binding_changes={"approved_object_refs": ("customers", "orders")},
        fault_hook=fault,
    )
    intent = _intent(observation, object_refs=("customers", "orders"))

    with pytest.raises(AcquisitionIntegrityError, match="artifact_publication_failed"):
        runner.prepare(intent)

    assert len(artifacts.payloads) == expected_orphan_count
    assert state.prepared_states == {}
    fault.fail_at = None

    replay = runner.prepare(intent)

    assert replay.prepared_receipt is not None
    assert provider.calls == 2


def test_temporary_file_cleanup_failure_never_replaces_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        lambda *, mode: _CloseFailingBytesIO(),
    )
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner(
        artifact_failure=True
    )

    with pytest.raises(AcquisitionIntegrityError, match="artifact_publication_failed") as error:
        runner.prepare(_intent(observation))

    assert "private-cleanup-fault" not in str(error.value)
    assert state.prepared_states == {}


@pytest.mark.parametrize("different_batch", (False, True))
def test_file_backed_runners_converge_only_for_identical_batch_output(
    tmp_path: Path,
    different_batch: bool,
) -> None:
    database_path = str(tmp_path / f"runtime-preparation-{different_batch}.sqlite")
    publication_barrier = threading.Barrier(2)

    def rendezvous(checkpoint: str) -> None:
        if checkpoint == "manifest_artifact_after":
            publication_barrier.wait(timeout=5)

    (
        first_runner,
        first_observation,
        _first_session,
        first_provider,
        _first_resolutions,
        _first_fake_state,
        shared_artifacts,
        _first_evidence,
        _first_events,
    ) = _runner(fault_hook=rendezvous, reference_prefix="first:")
    competing_records = (_record(amount=Decimal("99.99")),) if different_batch else (_record(),)
    (
        competing_runner,
        competing_observation,
        _competing_session,
        competing_provider,
        _competing_resolutions,
        _competing_fake_state,
        _competing_artifacts,
        _competing_evidence,
        _competing_events,
    ) = _runner(
        records=competing_records,
        fault_hook=rendezvous,
        reference_prefix="competing:",
    )
    first_state = concurrent_repository(database_path)
    competing_state = concurrent_repository(database_path)
    first_runner._state_store = first_state
    competing_runner._state_store = competing_state
    competing_runner._artifact_store = shared_artifacts
    first_intent = _intent(first_observation)
    competing_intent = _intent(competing_observation)
    assert first_intent == competing_intent
    _activate_contract(first_state, first_intent)
    results: list[AcquisitionPreparationResult] = []
    errors: list[BaseException] = []

    def prepare(runner: AcquisitionRunner, intent: AcquisitionIntent) -> None:
        try:
            results.append(runner.prepare(intent))
        except BaseException as error:
            errors.append(error)

    first_thread = threading.Thread(target=prepare, args=(first_runner, first_intent))
    competing_thread = threading.Thread(
        target=prepare,
        args=(competing_runner, competing_intent),
    )
    first_thread.start()
    competing_thread.start()
    first_thread.join(timeout=5)
    competing_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not competing_thread.is_alive()
    assert first_provider.calls == competing_provider.calls == 1
    if different_batch:
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AcquisitionIntegrityError)
    else:
        assert errors == []
        assert len(results) == 2
        assert results[0].prepared_receipt == results[1].prepared_receipt


def test_acknowledged_evidence_failure_replays_same_atomic_checkpoint_receipt() -> None:
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        _fake_state,
        _artifacts,
        evidence,
        _events,
    ) = _runner()
    state = _state()
    runner._state_store = state
    intent = _intent(observation)
    _activate_contract(state, intent)
    preparation = runner.prepare(intent)
    acknowledgement = _acknowledgement(preparation, intent)
    runner._evidence_writer = _FailOnceEvidenceWriter(evidence.append)

    with pytest.raises(AcquisitionIntegrityError, match="evidence_write_failed"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    committed = state.load_checkpoint(
        intent.tenant_id,
        intent.contract_digest,
        intent.source_binding_ref,
    )
    assert committed.revision == 1
    ready_binding = runner._binding_resolver(intent.tenant_id, intent.source_binding_ref)
    runner._binding_resolver = lambda tenant_id, binding_ref: ready_binding.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.RETIRED,
            "revision": ready_binding.revision + 1,
            "capability_profile_digest": None,
            "source_observation_ref": None,
        }
    )

    replay = runner.acknowledge(
        intent,
        acknowledgement,
    )

    assert replay.committed_revision == 1
    assert replay.checkpoint_receipt_id == "opaque-checkpoint-receipt"
    assert provider.calls == 1
    assert evidence.receipts[-1].outcome == "acknowledged"


def test_missing_referenced_segment_fails_closed_without_reacquiring() -> None:
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        _state_store,
        artifacts,
        _evidence,
        _events,
    ) = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    assert preparation.batch_manifest is not None
    segment_digest = preparation.batch_manifest.segment_manifests[0].content_digest
    del artifacts.payloads[segment_digest]

    with pytest.raises(AcquisitionIntegrityError, match="replay_artifact"):
        runner.prepare(intent)

    assert provider.calls == 1


@pytest.mark.parametrize(
    "unavailable_state",
    (SourceConnectionBindingState.SUSPENDED, SourceConnectionBindingState.RETIRED),
)
def test_unavailable_binding_cannot_acknowledge_prepared_batch(
    unavailable_state: SourceConnectionBindingState,
) -> None:
    (
        runner,
        observation,
        _session,
        _provider,
        _resolutions,
        _fake_state,
        _artifacts,
        _evidence,
        _events,
    ) = _runner()
    state = _state()
    runner._state_store = state
    intent = _intent(observation)
    _activate_contract(state, intent)
    preparation = runner.prepare(intent)
    acknowledgement = _acknowledgement(preparation, intent)
    ready_binding = runner._binding_resolver(intent.tenant_id, intent.source_binding_ref)
    runner._binding_resolver = lambda tenant_id, binding_ref: ready_binding.model_copy(
        update={
            "lifecycle_state": unavailable_state,
            "capability_profile_digest": None,
            "source_observation_ref": None,
        }
    )

    with pytest.raises(AcquisitionAuthorizationError, match="not_ready"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    with pytest.raises(AcquisitionStateNotFoundError):
        state.load_checkpoint(intent.tenant_id, intent.contract_digest, intent.source_binding_ref)


def test_old_intent_cannot_prepare_again_after_acknowledgement_moves_checkpoint() -> None:
    (
        runner,
        observation,
        _session,
        provider,
        _resolutions,
        _fake_state,
        _artifacts,
        _evidence,
        _events,
    ) = _runner()
    state = _state()
    runner._state_store = state
    intent = _intent(observation)
    _activate_contract(state, intent)
    preparation = runner.prepare(intent)
    runner.acknowledge(
        intent,
        _acknowledgement(preparation, intent),
    )

    with pytest.raises(AcquisitionStaleRevision):
        runner.prepare(intent)

    assert provider.calls == 1
