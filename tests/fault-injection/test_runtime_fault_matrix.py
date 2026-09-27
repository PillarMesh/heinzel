from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import FIXED_PROJECTION, digest
from heinzel_evidence import SQLiteStore
from heinzel_execution_graph import (
    ExecutionGraph,
    GraphSigner,
    GraphVerifier,
    SignedExecutionGraph,
)
from heinzel_provider_sdk import (
    CommitReceipt,
    DriftProbe,
    OrderRow,
    ProviderError,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from heinzel_provider_snowflake import encode_segment
from heinzel_runtime import FaultHook, Runtime, SegmentEncoder

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ACCEPTANCE_KEY = 984201


class InjectedProcessStop(BaseException):
    pass


class FaultMatrixSource:
    def __init__(self) -> None:
        self.reads = 0
        self.deny_read = False

    def drift_probe(self) -> DriftProbe:
        return DriftProbe(object_identity="pg:fixture:42", schema_digest="1" * 64)

    def read_snapshot(self) -> tuple[SourceBoundary, tuple[OrderRow, ...]]:
        self.reads += 1
        if self.deny_read:
            raise ProviderError("PostgreSQL snapshot read denied", "authorization")
        row = OrderRow(
            order_id=ACCEPTANCE_KEY,
            customer_ref="customer-fault-matrix",
            amount=Decimal("10.50"),
            currency="USD",
            status="paid",
            updated_at=NOW,
        )
        boundary = SourceBoundary(
            object_identity="pg:fixture:42",
            schema_digest="1" * 64,
            snapshot_identity=f"snapshot-{self.reads}",
            key_range_digest=digest({"key_min": ACCEPTANCE_KEY, "key_max": ACCEPTANCE_KEY}),
            row_count=1,
            query_shape_digest="2" * 64,
            opened_at=NOW,
            closed_at=NOW,
        )
        return boundary, (row,)


class FaultMatrixDestination:
    def __init__(self) -> None:
        self.deny_stage = False
        self.deny_merge = False
        self.lose_commit_response = False
        self.response_was_lost = False
        self.ledger: dict[str, str] = {}
        self.staged_batch_ids: list[str] = []
        self.commit_batch_ids: list[str] = []
        self.visibility_batch_ids: list[str] = []
        self.mutations = 0

    def stage(self, segment: Path, manifest: SegmentManifest) -> None:
        if self.deny_stage:
            raise ProviderError("Snowflake stage write denied", "authorization")
        if hashlib.sha256(segment.read_bytes()).hexdigest() != manifest.segment_digest:
            raise ProviderError("segment digest does not match manifest", "permanent")
        self.staged_batch_ids.append(manifest.batch_id)

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        self.commit_batch_ids.append(manifest.batch_id)
        if self.deny_merge:
            raise ProviderError("Snowflake merge denied", "authorization")
        manifest_digest = digest(manifest)
        existing = self.ledger.get(manifest.batch_id)
        if existing is not None and existing != manifest_digest:
            raise ProviderError(
                "batch identity already exists with a conflicting manifest", "permanent"
            )
        if existing is None:
            self.ledger[manifest.batch_id] = manifest_digest
            self.mutations += 1
        if self.lose_commit_response and not self.response_was_lost:
            self.response_was_lost = True
            raise InjectedProcessStop("commit response lost")
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=manifest_digest,
            query_ids=("ledger-resolution-query",) if existing is not None else ("merge-query",),
            affected_rows=0 if existing is not None else 1,
            ledger_identity=digest({"provider": "snowflake", "ledger": "sf:fixture:commit-ledger"}),
            committed_at=NOW,
            replayed=existing is not None,
        )

    def verify_visibility(self, manifest: SegmentManifest, _acceptance_key: int) -> VisibilityProof:
        self.visibility_batch_ids.append(manifest.batch_id)
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=manifest.acceptance_value_digest,
            query_id=f"visibility-{len(self.visibility_batch_ids)}",
            verified_at=NOW,
        )


class StopAt:
    def __init__(self, checkpoint: str) -> None:
        self.checkpoint = checkpoint
        self.seen: list[str] = []
        self.stopped = False

    def __call__(self, checkpoint: str) -> None:
        self.seen.append(checkpoint)
        if checkpoint == self.checkpoint and not self.stopped:
            self.stopped = True
            raise InjectedProcessStop(checkpoint)


def _signed_graph() -> tuple[GraphSigner, SignedExecutionGraph]:
    signer = GraphSigner("fault-key", Ed25519PrivateKey.generate())
    graph = ExecutionGraph(
        contract_digest="a" * 64,
        iir_digest="b" * 64,
        physical_plan_digest="c" * 64,
        legality_decision_digest="d" * 64,
        source_observation_digest="e" * 64,
        destination_observation_digest="f" * 64,
        source_connection_handle="source",
        destination_connection_handle="destination",
        source_object_identity="pg:fixture:42",
        source_schema_digest="1" * 64,
        projection=FIXED_PROJECTION,
        operators=("read_snapshot", "encode_segment", "commit", "verify"),
        required_evidence=(),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )
    return signer, signer.sign(graph)


def _create_store(path: Path, signed_graph: SignedExecutionGraph) -> SQLiteStore:
    store = SQLiteStore.open(path)
    store.create_run(
        "run-fault-matrix",
        "activation-fault-matrix",
        signed_graph.graph.contract_digest,
        "9" * 64,
        signed_graph.model_dump_json(),
        NOW,
    )
    return store


def _runtime(
    store: SQLiteStore,
    signer: GraphSigner,
    source: FaultMatrixSource,
    destination: FaultMatrixDestination,
    output: Path,
    *,
    fault_hook: FaultHook | None = None,
    segment_encoder: SegmentEncoder = encode_segment,
) -> Runtime:
    arguments: dict[str, object] = {}
    if fault_hook is not None:
        arguments["fault_hook"] = fault_hook
    return Runtime(
        store=store,
        verifier=GraphVerifier({"fault-key": signer.public_key}),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: destination,
        segment_encoder=segment_encoder,
        output_dir=output,
        clock=lambda: NOW,
        **arguments,  # type: ignore[arg-type]
    )


def _expected_batch_id(signed_graph: SignedExecutionGraph) -> str:
    return digest(
        {
            "domain": "heinzel-batch-v1",
            "run_id": "run-fault-matrix",
            "graph": signed_graph.graph,
        }
    )[:32]


def _assert_success_after_resume(
    runtime: Runtime,
    store: SQLiteStore,
    signed_graph: SignedExecutionGraph,
    destination: FaultMatrixDestination,
) -> None:
    result = runtime.resume("run-fault-matrix")
    expected_batch_id = _expected_batch_id(signed_graph)

    assert result.state == "succeeded"
    assert result.batch_id == expected_batch_id
    assert store.get_run("run-fault-matrix").batch_id == expected_batch_id
    assert destination.mutations == 1
    assert set(
        destination.staged_batch_ids
        + destination.commit_batch_ids
        + destination.visibility_batch_ids
    ) == {expected_batch_id}
    assert store.verify_chain("run-fault-matrix")
    event_types = [event.event_type for event in store.trace("run-fault-matrix")]
    assert event_types.count("terminal_success") == 1
    assert "terminal_failure" not in event_types


@pytest.mark.parametrize(
    ("checkpoint", "durable_checkpoint", "mutations_before_resume"),
    [
        ("before_extraction", "snapshot_opened", 0),
        ("after_extraction_persisted", "extraction_completed", 0),
        ("during_commit", "commit_attempted", 0),
        ("after_commit_before_receipt", "commit_attempted", 1),
        ("after_receipt_before_visibility", "commit_resolved", 1),
    ],
)
def test_process_stop_at_stable_fault_hook_resumes_without_false_success(
    tmp_path: Path,
    checkpoint: str,
    durable_checkpoint: str,
    mutations_before_resume: int,
) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    stop = StopAt(checkpoint)
    runtime = _runtime(
        store,
        signer,
        source,
        destination,
        tmp_path / "output",
        fault_hook=stop,
    )

    with pytest.raises(InjectedProcessStop, match=checkpoint):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    interrupted = store.get_run("run-fault-matrix")
    assert interrupted.state == "running"
    assert interrupted.checkpoint == durable_checkpoint
    assert destination.mutations == mutations_before_resume
    assert "terminal_success" not in {event.event_type for event in store.trace("run-fault-matrix")}
    assert store.verify_chain("run-fault-matrix")

    resumed = _runtime(store, signer, source, destination, tmp_path / "output")
    _assert_success_after_resume(resumed, store, signed_graph, destination)


@pytest.mark.parametrize(
    ("event_type", "method_name", "before_checkpoint", "after_checkpoint"),
    [
        ("graph_verified", "advance_checkpoint_with_event", "created", "graph_verified"),
        (
            "drift_revalidated",
            "advance_checkpoint_with_event",
            "graph_verified",
            "drift_revalidated",
        ),
        (
            "snapshot_opened",
            "advance_checkpoint_with_event",
            "drift_revalidated",
            "snapshot_opened",
        ),
        ("extraction_completed", "record_extraction", "snapshot_opened", "extraction_completed"),
        (
            "manifest_created",
            "advance_checkpoint_with_event",
            "extraction_completed",
            "manifest_created",
        ),
        (
            "commit_attempted",
            "advance_checkpoint_with_event",
            "manifest_created",
            "commit_attempted",
        ),
        (
            "commit_resolved",
            "advance_checkpoint_with_event",
            "commit_attempted",
            "commit_resolved",
        ),
        (
            "visibility_verified",
            "advance_checkpoint_with_event",
            "commit_resolved",
            "visibility_verified",
        ),
        (
            "terminal_success",
            "advance_checkpoint_with_event",
            "visibility_verified",
            "terminal_success",
        ),
    ],
)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_stop_around_evidence_transaction_resumes_from_durable_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    method_name: str,
    before_checkpoint: str,
    after_checkpoint: str,
    phase: str,
) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    original = getattr(store, method_name)
    injected = False

    def interrupted_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        current_event = (
            "extraction_completed" if method_name == "record_extraction" else kwargs["event_type"]
        )
        should_stop = current_event == event_type and not injected
        if should_stop and phase == "before":
            injected = True
            raise InjectedProcessStop(f"before:{event_type}")
        result = original(*args, **kwargs)
        if should_stop:
            injected = True
            raise InjectedProcessStop(f"after:{event_type}")
        return result

    monkeypatch.setattr(store, method_name, interrupted_write)
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(InjectedProcessStop, match=f"{phase}:{event_type}"):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    record = store.get_run("run-fault-matrix")
    expected_checkpoint = before_checkpoint if phase == "before" else after_checkpoint
    expected_state = (
        "succeeded"
        if expected_checkpoint == "terminal_success"
        else "created"
        if expected_checkpoint == "created"
        else "running"
    )
    assert record.checkpoint == expected_checkpoint
    assert record.state == expected_state
    assert store.verify_chain("run-fault-matrix")
    terminal_events = [
        event.event_type
        for event in store.trace("run-fault-matrix")
        if event.event_type.startswith("terminal_")
    ]
    assert terminal_events == (["terminal_success"] if expected_state == "succeeded" else [])

    monkeypatch.setattr(store, method_name, original)
    resumed = _runtime(store, signer, source, destination, tmp_path / "output")
    _assert_success_after_resume(resumed, store, signed_graph, destination)


def test_evidence_write_failure_is_terminal_without_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    original = store.advance_checkpoint_with_event
    injected = False

    def failing_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        if kwargs["event_type"] == "manifest_created" and not injected:
            injected = True
            raise OSError("injected evidence write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "advance_checkpoint_with_event", failing_write)
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(OSError, match="injected evidence write failure"):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    assert store.get_run("run-fault-matrix").state == "failed"
    event_types = [event.event_type for event in store.trace("run-fault-matrix")]
    assert "manifest_created" not in event_types
    assert "terminal_success" not in event_types
    assert event_types[-1] == "terminal_failure"
    assert destination.mutations == 0
    assert store.verify_chain("run-fault-matrix")


@pytest.mark.parametrize(
    ("failure_point", "durable_checkpoint", "visibility_attempts_before_resume"),
    [
        ("commit_receipt_artifact", "commit_attempted", 0),
        ("commit_resolved_event", "commit_attempted", 0),
        ("visibility_proof_artifact", "commit_resolved", 1),
        ("visibility_verified_event", "commit_resolved", 1),
        ("terminal_success_event", "visibility_verified", 1),
    ],
)
def test_evidence_write_failure_after_commit_remains_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    durable_checkpoint: str,
    visibility_attempts_before_resume: int,
) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    original_save = store.save_artifact
    original_advance = store.advance_checkpoint_with_event
    injected = False

    def failing_save(kind: str, artifact_digest: str, payload: bytes) -> None:
        nonlocal injected
        if failure_point == f"{kind}_artifact" and not injected:
            injected = True
            raise OSError(f"injected evidence write failure at {failure_point}")
        original_save(kind, artifact_digest, payload)

    def failing_advance(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        if failure_point == f"{kwargs['event_type']}_event" and not injected:
            injected = True
            raise OSError(f"injected evidence write failure at {failure_point}")
        return original_advance(*args, **kwargs)

    monkeypatch.setattr(store, "save_artifact", failing_save)
    monkeypatch.setattr(store, "advance_checkpoint_with_event", failing_advance)
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(OSError, match=failure_point):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    interrupted = store.get_run("run-fault-matrix")
    assert interrupted.state == "running"
    assert interrupted.checkpoint == durable_checkpoint
    assert destination.mutations == 1
    assert len(destination.visibility_batch_ids) == visibility_attempts_before_resume
    assert not any(
        event.event_type.startswith("terminal_") for event in store.trace("run-fault-matrix")
    )
    assert store.verify_chain("run-fault-matrix")

    monkeypatch.setattr(store, "save_artifact", original_save)
    monkeypatch.setattr(store, "advance_checkpoint_with_event", original_advance)
    resumed = _runtime(store, signer, source, destination, tmp_path / "output")
    _assert_success_after_resume(resumed, store, signed_graph, destination)


@pytest.mark.parametrize(
    "denial",
    ["postgresql_read", "snowflake_stage", "snowflake_merge"],
)
def test_provider_permission_denial_is_terminal_without_false_success(
    tmp_path: Path, denial: str
) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    source.deny_read = denial == "postgresql_read"
    destination.deny_stage = denial == "snowflake_stage"
    destination.deny_merge = denial == "snowflake_merge"
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(ProviderError) as caught:
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    assert caught.value.classification == "authorization"
    assert store.get_run("run-fault-matrix").state == "failed"
    assert store.trace("run-fault-matrix")[-1].event_type == "terminal_failure"
    assert "terminal_success" not in {event.event_type for event in store.trace("run-fault-matrix")}
    assert destination.mutations == 0
    assert store.verify_chain("run-fault-matrix")


def test_corrupt_staged_bytes_are_terminal_without_destination_mutation(tmp_path: Path) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()

    def corrupting_encoder(*args, **kwargs):  # type: ignore[no-untyped-def]
        manifest = encode_segment(*args, **kwargs)
        output = args[-1]
        output.write_bytes(output.read_bytes() + b"tampered")
        return manifest

    runtime = _runtime(
        store,
        signer,
        source,
        destination,
        tmp_path / "output",
        segment_encoder=corrupting_encoder,
    )

    with pytest.raises(ProviderError, match="segment digest"):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    assert store.get_run("run-fault-matrix").state == "failed"
    assert destination.mutations == 0
    assert store.trace("run-fault-matrix")[-1].event_type == "terminal_failure"
    assert store.verify_chain("run-fault-matrix")


def test_conflicting_ledger_digest_is_terminal_without_second_mutation(tmp_path: Path) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    destination.ledger[_expected_batch_id(signed_graph)] = "0" * 64
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(ProviderError, match="conflicting manifest") as caught:
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    assert caught.value.classification == "permanent"
    assert store.get_run("run-fault-matrix").state == "failed"
    assert destination.mutations == 0
    assert store.trace("run-fault-matrix")[-1].event_type == "terminal_failure"
    assert store.verify_chain("run-fault-matrix")


def test_lost_commit_response_resolves_same_batch_without_second_mutation(tmp_path: Path) -> None:
    signer, signed_graph = _signed_graph()
    store = _create_store(tmp_path / "state.sqlite3", signed_graph)
    source = FaultMatrixSource()
    destination = FaultMatrixDestination()
    destination.lose_commit_response = True
    runtime = _runtime(store, signer, source, destination, tmp_path / "output")

    with pytest.raises(InjectedProcessStop, match="commit response lost"):
        runtime.execute("run-fault-matrix", signed_graph, ACCEPTANCE_KEY)

    interrupted = store.get_run("run-fault-matrix")
    assert interrupted.state == "running"
    assert interrupted.checkpoint == "commit_attempted"
    assert destination.mutations == 1
    assert "terminal_success" not in {event.event_type for event in store.trace("run-fault-matrix")}

    resumed = _runtime(store, signer, source, destination, tmp_path / "output")
    _assert_success_after_resume(resumed, store, signed_graph, destination)
    assert destination.commit_batch_ids == [
        _expected_batch_id(signed_graph),
        _expected_batch_id(signed_graph),
    ]
