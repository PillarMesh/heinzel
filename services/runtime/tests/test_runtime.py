from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import FIXED_PROJECTION, digest
from pillarmesh_evidence import SQLiteStore
from pillarmesh_execution_graph import (
    EvidenceRequirement,
    ExecutionGraph,
    GraphSigner,
    GraphVerifier,
    InvalidGraph,
    SignedExecutionGraph,
)
from pillarmesh_provider_sdk import (
    CommitReceipt,
    DriftProbe,
    OrderRow,
    ProviderError,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from pillarmesh_runtime import FaultHook, Runtime, noop_fault_hook

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_noop_fault_hook_is_a_public_no_op() -> None:
    hook: FaultHook = noop_fault_hook

    assert hook("before_extraction") is None


class Source:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def drift_probe(self) -> DriftProbe:
        self.calls.append("drift")
        return DriftProbe(object_identity="pg:fixture:42", schema_digest="1" * 64)

    def read_snapshot(self):  # type: ignore[no-untyped-def]
        self.calls.append("snapshot")
        boundary = SourceBoundary(
            object_identity="pg:fixture:42",
            schema_digest="1" * 64,
            snapshot_identity="10:20:",
            key_range_digest=digest({"key_min": 7, "key_max": 7}),
            row_count=1,
            query_shape_digest="2" * 64,
            opened_at=NOW,
            closed_at=NOW,
        )
        return boundary, (
            OrderRow(
                order_id=7,
                customer_ref="customer-7",
                amount=Decimal("10.50"),
                currency="USD",
                status="paid",
                updated_at=NOW,
            ),
        )


class Destination:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.staged_paths: list[Path] = []
        self.visibility_keys: list[int] = []

    def stage(self, path: Path, _manifest: SegmentManifest) -> None:
        self.calls.append("stage")
        self.staged_paths.append(path)

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        self.calls.append("commit")
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=digest(manifest),
            query_ids=("merge-query", "ledger-query"),
            affected_rows=1,
            ledger_identity=digest({"provider": "snowflake", "ledger": "sf:fixture:commit-ledger"}),
            committed_at=NOW,
        )

    def verify_visibility(self, manifest: SegmentManifest, acceptance_key: int) -> VisibilityProof:
        self.calls.append("visibility")
        self.visibility_keys.append(acceptance_key)
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=manifest.acceptance_value_digest,
            query_id="visibility-query",
            verified_at=NOW,
        )


def graph(signer: GraphSigner) -> SignedExecutionGraph:
    unsigned = ExecutionGraph(
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
        required_evidence=(
            EvidenceRequirement(event_type="terminal_success", redaction_class="metadata"),
        ),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )
    return signer.sign(unsigned)


def create_run(store: SQLiteStore, signed: SignedExecutionGraph) -> None:
    store.create_run(
        "run-1",
        "activation-1",
        signed.graph.contract_digest,
        "9" * 64,
        signed.model_dump_json(),
        NOW,
    )


def encoder(calls: list[str]):  # type: ignore[no-untyped-def]
    def encode(rows, boundary, acceptance_key, batch_id, output):  # type: ignore[no-untyped-def]
        calls.append("encode")
        values = tuple(rows)
        output.write_bytes(b"segment")
        return SegmentManifest(
            batch_id=batch_id,
            segment_name="segment.csv",
            segment_digest="4" * 64,
            row_set_digest="5" * 64,
            row_count=len(values),
            encoded_bytes=7,
            schema_digest="6" * 64,
            source_boundary_digest=digest(boundary),
            acceptance_value_digest=digest(values[0]),
        )

    return encode


def test_graph_verification_precedes_provider_resolution(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    wrong = GraphSigner("key-2", Ed25519PrivateKey.generate())
    signed = graph(wrong)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    resolutions: list[str] = []
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda handle: resolutions.append(handle),
        destination_resolver=lambda handle: resolutions.append(handle),
        segment_encoder=encoder([]),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidGraph):
        runtime.execute("run-1", signed, 7)

    assert resolutions == []
    assert store.get_run("run-1").state == "failed"


def _assert_graph_rejected_before_resolvers(
    tmp_path: Path,
    signed: SignedExecutionGraph,
    verifier: GraphVerifier,
    expected_message: str,
) -> None:
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    source_resolutions = 0
    destination_resolutions = 0

    def resolve_source(_handle: str) -> Source:
        nonlocal source_resolutions
        source_resolutions += 1
        return Source([])

    def resolve_destination(_handle: str) -> Destination:
        nonlocal destination_resolutions
        destination_resolutions += 1
        return Destination([])

    runtime = Runtime(
        store=store,
        verifier=verifier,
        source_resolver=resolve_source,
        destination_resolver=resolve_destination,
        segment_encoder=encoder([]),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidGraph, match=expected_message):
        runtime.execute("run-1", signed, 7)

    assert source_resolutions == 0
    assert destination_resolutions == 0
    assert store.get_private_state("run-1").acceptance_key is None


def test_modified_graph_fails_before_either_provider_resolver(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    modified = signed.model_copy(
        update={"graph": signed.graph.model_copy(update={"source_connection_handle": "modified"})}
    )

    _assert_graph_rejected_before_resolvers(
        tmp_path, modified, GraphVerifier({"key-1": signer.public_key}), "digest mismatch"
    )


def test_expired_graph_fails_before_either_provider_resolver(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    current = graph(signer)
    expired = signer.sign(current.graph.model_copy(update={"expires_at": NOW}))

    _assert_graph_rejected_before_resolvers(
        tmp_path, expired, GraphVerifier({"key-1": signer.public_key}), "expired"
    )


def test_wrong_key_graph_fails_before_either_provider_resolver(tmp_path: Path) -> None:
    trusted = GraphSigner("key-1", Ed25519PrivateKey.generate())
    untrusted = GraphSigner("key-2", Ed25519PrivateKey.generate())

    _assert_graph_rejected_before_resolvers(
        tmp_path,
        graph(untrusted),
        GraphVerifier({"key-1": trusted.public_key}),
        "unknown signing key",
    )


def test_incompatible_graph_fails_before_either_provider_resolver(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    current = graph(signer)
    incompatible_graph = current.graph.model_copy(update={"schema_version": "2"})
    incompatible = signer.sign(incompatible_graph)

    _assert_graph_rejected_before_resolvers(
        tmp_path,
        incompatible,
        GraphVerifier({"key-1": signer.public_key}),
        "unsupported graph schema version",
    )


def _assert_linkage_rejected_before_resolvers(
    tmp_path: Path,
    supplied: SignedExecutionGraph,
    stored: SignedExecutionGraph,
    run_contract_digest: str,
    verifier: GraphVerifier,
    expected_diagnostic: str,
) -> None:
    store = SQLiteStore.open(tmp_path / "state.db")
    store.create_run(
        "run-1",
        "activation-1",
        run_contract_digest,
        "9" * 64,
        stored.model_dump_json(),
        NOW,
    )
    source_resolutions = 0
    destination_resolutions = 0

    def resolve_source(_handle: str) -> Source:
        nonlocal source_resolutions
        source_resolutions += 1
        return Source([])

    def resolve_destination(_handle: str) -> Destination:
        nonlocal destination_resolutions
        destination_resolutions += 1
        return Destination([])

    runtime = Runtime(
        store=store,
        verifier=verifier,
        source_resolver=resolve_source,
        destination_resolver=resolve_destination,
        segment_encoder=encoder([]),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidGraph) as caught:
        runtime.execute("run-1", supplied, 984201)

    assert str(caught.value) == expected_diagnostic
    assert source_resolutions == 0
    assert destination_resolutions == 0
    assert store.get_private_state("run-1").acceptance_key is None


def test_supplied_and_stored_graph_mismatch_is_typed_before_resolvers(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    stored = graph(signer)
    supplied = signer.sign(
        stored.graph.model_copy(update={"source_connection_handle": "other-source"})
    )

    _assert_linkage_rejected_before_resolvers(
        tmp_path,
        supplied,
        stored,
        stored.graph.contract_digest,
        GraphVerifier({"key-1": signer.public_key}),
        "GRAPH_ENVELOPE_MISMATCH: supplied graph differs from activated graph",
    )


def test_graph_and_run_contract_mismatch_is_typed_before_resolvers(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)

    _assert_linkage_rejected_before_resolvers(
        tmp_path,
        signed,
        signed,
        "0" * 64,
        GraphVerifier({"key-1": signer.public_key}),
        "GRAPH_RUN_CONTRACT_MISMATCH: graph contract differs from admitted run",
    )


def test_drift_failure_happens_before_snapshot_and_destination(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    source = Source(calls)
    source.drift_probe = lambda: DriftProbe(  # type: ignore[method-assign]
        object_identity="pg:fixture:changed", schema_digest="1" * 64
    )
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: source,
        destination_resolver=lambda _handle: Destination(calls),
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="drift"):
        runtime.execute("run-1", signed, 7)

    assert "snapshot" not in calls
    assert not {"stage", "commit", "visibility"}.intersection(calls)


def test_successful_run_reaches_visibility_with_continuous_evidence(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    fault_checkpoints: list[str] = []
    destination = Destination(calls)
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
        fault_hook=fault_checkpoints.append,
    )

    result = runtime.execute("run-1", signed, 7)

    assert result.state == "succeeded"
    assert calls == ["drift", "snapshot", "encode", "stage", "commit", "visibility"]
    assert fault_checkpoints == [
        "before_extraction",
        "after_extraction_persisted",
        "during_commit",
        "after_commit_before_receipt",
        "after_receipt_before_visibility",
    ]
    private_state = store.get_private_state("run-1")
    assert destination.staged_paths == [private_state.segment_path]
    assert destination.visibility_keys == [private_state.acceptance_key]
    assert store.verify_chain("run-1")
    assert [event.event_type for event in store.trace("run-1")] == [
        "graph_verified",
        "drift_revalidated",
        "snapshot_opened",
        "extraction_completed",
        "manifest_created",
        "commit_attempted",
        "commit_resolved",
        "visibility_verified",
        "terminal_success",
    ]


def test_retryable_stage_uses_one_and_five_second_delays(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    destination = Destination(calls)
    attempts = 0

    def flaky_stage(_path: Path, _manifest: SegmentManifest) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ProviderError("temporary staging failure", "retryable")
        calls.append("stage")

    destination.stage = flaky_stage  # type: ignore[method-assign]
    delays: list[float] = []
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
        sleeper=delays.append,
        monotonic=lambda: 0,
    )

    result = runtime.execute("run-1", signed, 7)

    assert result.state == "succeeded"
    assert attempts == 3
    assert delays == [1.0, 5.0]


def test_visibility_integrity_failure_is_non_conforming_not_success(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    destination = Destination(calls)
    destination.verify_visibility = (  # type: ignore[method-assign]
        lambda _manifest, _acceptance_key: (_ for _ in ()).throw(
            ProviderError("visibility digest mismatch", "permanent")
        )
    )
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(ProviderError):
        runtime.execute("run-1", signed, 7)

    assert store.get_run("run-1").state == "non_conforming"
    assert store.trace("run-1")[-1].event_type == "terminal_non_conforming"


def test_transient_visibility_failure_is_retried_and_is_not_non_conforming(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    destination = Destination(calls)
    original = destination.verify_visibility
    attempts = 0

    def flaky_visibility(manifest: SegmentManifest, acceptance_key: int) -> VisibilityProof:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ProviderError("visibility read timed out", "retryable")
        return original(manifest, acceptance_key)

    destination.verify_visibility = flaky_visibility  # type: ignore[method-assign]
    delays: list[float] = []
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
        sleeper=delays.append,
        monotonic=lambda: 0,
    )

    result = runtime.execute("run-1", signed, 7)

    assert result.state == "succeeded"
    assert attempts == 3
    assert delays == [1.0, 5.0]


def test_exhausted_transient_visibility_failure_is_not_recorded_as_non_conforming(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    destination = Destination(calls)
    destination.verify_visibility = (  # type: ignore[method-assign]
        lambda _manifest, _acceptance_key: (_ for _ in ()).throw(
            ProviderError("visibility read timed out", "retryable")
        )
    )
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0,
    )

    with pytest.raises(ProviderError):
        runtime.execute("run-1", signed, 7)

    # Non-conformance is a verdict about the destination; a transient read failure that
    # merely outlived the retry budget is not evidence of one.
    assert store.get_run("run-1").state == "failed"
    assert store.trace("run-1")[-1].event_type == "terminal_failure"


def test_visibility_retry_budget_exhaustion_is_not_recorded_as_non_conforming(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
    signed = graph(signer)
    store = SQLiteStore.open(tmp_path / "state.db")
    create_run(store, signed)
    calls: list[str] = []
    destination = Destination(calls)
    destination.verify_visibility = (  # type: ignore[method-assign]
        lambda _manifest, _acceptance_key: (_ for _ in ()).throw(
            ProviderError("visibility read timed out", "retryable")
        )
    )
    # Stage and commit each sample the clock once before visibility starts its own
    # retry budget and observes that the first failed read consumed it.
    monotonic_values = iter((0.0, 0.0, 0.0, 120.0))
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: Source(calls),
        destination_resolver=lambda _handle: destination,
        segment_encoder=encoder(calls),
        output_dir=tmp_path,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(monotonic_values),
    )

    with pytest.raises(ProviderError, match="retry wall-clock budget exhausted") as raised:
        runtime.execute("run-1", signed, 7)

    assert raised.value.classification == "retryable"
    assert store.get_run("run-1").state == "failed"
    assert store.trace("run-1")[-1].event_type == "terminal_failure"
