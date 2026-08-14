from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import FIXED_PROJECTION, canonical_bytes, digest
from pillarmesh_evidence import MigrationError, SQLiteStore
from pillarmesh_execution_graph import ExecutionGraph, GraphSigner, GraphVerifier
from pillarmesh_provider_sdk import CommitReceipt, SegmentManifest, VisibilityProof
from pillarmesh_runtime import Runtime

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class RecoveryDestination:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stage(self, _path: Path, _manifest: SegmentManifest) -> None:
        raise AssertionError("resume after commit attempt must not stage blindly")

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        self.calls.append("resolve")
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=digest(manifest),
            query_ids=("ledger-resolution-query",),
            affected_rows=0,
            ledger_identity=digest({"provider": "snowflake", "ledger": "sf:fixture:commit-ledger"}),
            committed_at=NOW,
            replayed=True,
        )

    def verify_visibility(self, manifest: SegmentManifest, _acceptance_key: int) -> VisibilityProof:
        self.calls.append("visibility")
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=manifest.acceptance_value_digest,
            query_id="fresh-visibility-query",
            verified_at=NOW,
        )


def test_restart_after_commit_attempt_resolves_same_batch_without_blind_write(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
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
    signed = signer.sign(graph)
    path = tmp_path / "state.db"
    store = SQLiteStore.open(path)
    store.create_run(
        "run-1", "activation-1", graph.contract_digest, "9" * 64, signed.model_dump_json(), NOW
    )
    store.set_acceptance_key("run-1", 7)
    batch_id = digest({"domain": "pillarmesh-m0-batch-v1", "run_id": "run-1", "graph": graph})[:32]
    segment = tmp_path / "segment.csv"
    segment.write_bytes(b"segment")
    manifest = SegmentManifest(
        batch_id=batch_id,
        segment_name="segment.csv",
        segment_digest="4" * 64,
        row_set_digest="5" * 64,
        row_count=1,
        encoded_bytes=7,
        schema_digest="6" * 64,
        source_boundary_digest="7" * 64,
        acceptance_value_digest="8" * 64,
    )
    manifest_digest = digest(manifest)
    store.save_artifact("segment_manifest", manifest_digest, canonical_bytes(manifest))
    checkpoint = "created"
    for next_checkpoint, event_type, attributes in (
        ("graph_verified", "graph_verified", {"graph_digest": signed.graph_digest}),
        ("drift_revalidated", "drift_revalidated", {"drift_probe_digest": "1" * 64}),
        ("snapshot_opened", "snapshot_opened", {"source_boundary_digest": "7" * 64}),
        (
            "extraction_completed",
            "extraction_completed",
            {"manifest_digest": manifest_digest, "row_count": 1},
        ),
        ("manifest_created", "manifest_created", {"manifest_digest": manifest_digest}),
        ("commit_attempted", "commit_attempted", {"batch_id": batch_id}),
    ):
        store.advance_checkpoint_with_event(
            "run-1",
            expected_checkpoint=checkpoint,
            checkpoint=next_checkpoint,
            state="running",
            event_type=event_type,
            occurred_at=NOW,
            producer="pillarmesh-runtime",
            attributes=attributes,
            batch_id=batch_id,
        )
        checkpoint = next_checkpoint
    store.close()

    resumed_store = SQLiteStore.open(path)
    destination = RecoveryDestination()
    fault_checkpoints: list[str] = []
    runtime = Runtime(
        store=resumed_store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: (_ for _ in ()).throw(
            AssertionError("source must not reopen after commit attempt")
        ),
        destination_resolver=lambda _handle: destination,
        segment_encoder=lambda *_args: (_ for _ in ()).throw(AssertionError("must not encode")),
        output_dir=tmp_path,
        clock=lambda: NOW,
        fault_hook=fault_checkpoints.append,
    )

    result = runtime.resume("run-1")

    assert result.state == "succeeded"
    assert result.batch_id == batch_id
    assert destination.calls == ["resolve", "visibility"]
    assert fault_checkpoints == [
        "during_commit",
        "after_commit_before_receipt",
        "after_receipt_before_visibility",
    ]
    assert resumed_store.verify_chain("run-1")


class ExtractionRecoveryDestination:
    def __init__(self) -> None:
        self.staged_paths: list[Path] = []
        self.visibility_keys: list[int] = []

    def stage(self, path: Path, _manifest: SegmentManifest) -> None:
        self.staged_paths.append(path)

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt:
        return CommitReceipt(
            batch_id=manifest.batch_id,
            manifest_digest=digest(manifest),
            query_ids=("merge-query", "ledger-query"),
            affected_rows=1,
            ledger_identity=digest({"provider": "snowflake", "ledger": "sf:fixture:commit-ledger"}),
            committed_at=NOW,
        )

    def verify_visibility(self, manifest: SegmentManifest, acceptance_key: int) -> VisibilityProof:
        self.visibility_keys.append(acceptance_key)
        return VisibilityProof(
            batch_id=manifest.batch_id,
            value_digest=manifest.acceptance_value_digest,
            query_id="fresh-visibility-query",
            verified_at=NOW,
        )


def test_restart_after_extraction_reuses_private_path_and_acceptance_key(
    tmp_path: Path,
) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
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
    signed = signer.sign(graph)
    database_path = tmp_path / "state.db"
    store = SQLiteStore.open(database_path)
    store.create_run(
        "run-1", "activation-1", graph.contract_digest, "9" * 64, signed.model_dump_json(), NOW
    )
    acceptance_key = 984201
    store.set_acceptance_key("run-1", acceptance_key)
    checkpoint = "created"
    for next_checkpoint, event_type in (
        ("graph_verified", "graph_verified"),
        ("drift_revalidated", "drift_revalidated"),
        ("snapshot_opened", "snapshot_opened"),
    ):
        store.advance_checkpoint_with_event(
            "run-1",
            expected_checkpoint=checkpoint,
            checkpoint=next_checkpoint,
            state="running",
            event_type=event_type,
            occurred_at=NOW,
            producer="pillarmesh-runtime",
            attributes={"digest": "1" * 64},
        )
        checkpoint = next_checkpoint
    batch_id = digest({"domain": "pillarmesh-m0-batch-v1", "run_id": "run-1", "graph": graph})[:32]
    segment_path = tmp_path / "private-materialization" / "segment.csv"
    segment_path.parent.mkdir()
    segment_path.write_bytes(b"segment")
    source_payload = canonical_bytes({"schema_version": "1", "snapshot": "snapshot-1"})
    source_digest = digest({"schema_version": "1", "snapshot": "snapshot-1"})
    manifest = SegmentManifest(
        batch_id=batch_id,
        segment_name="segment.csv",
        segment_digest="4" * 64,
        row_set_digest="5" * 64,
        row_count=1,
        encoded_bytes=7,
        schema_digest="6" * 64,
        source_boundary_digest=source_digest,
        acceptance_value_digest="8" * 64,
    )
    store.record_extraction(
        "run-1",
        source_boundary_digest=manifest.source_boundary_digest,
        source_boundary_payload=source_payload,
        manifest_digest=digest(manifest),
        manifest_payload=canonical_bytes(manifest),
        segment_path=segment_path,
        batch_id=batch_id,
        row_count=manifest.row_count,
        encoded_bytes=manifest.encoded_bytes,
        occurred_at=NOW,
        producer="pillarmesh-runtime",
    )
    store.close()

    resumed_store = SQLiteStore.open(database_path)
    destination = ExtractionRecoveryDestination()
    runtime = Runtime(
        store=resumed_store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: (_ for _ in ()).throw(
            AssertionError("source must not reopen after extraction")
        ),
        destination_resolver=lambda _handle: destination,
        segment_encoder=lambda *_args: (_ for _ in ()).throw(AssertionError("must not encode")),
        output_dir=tmp_path / "different-runtime-output",
        clock=lambda: NOW,
    )

    result = runtime.resume("run-1")

    assert result.state == "succeeded"
    assert destination.staged_paths == [segment_path]
    assert destination.visibility_keys == [acceptance_key]


def test_v1_manifest_cannot_resume_under_privacy_safe_runtime(tmp_path: Path) -> None:
    signer = GraphSigner("key-1", Ed25519PrivateKey.generate())
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
    signed = signer.sign(graph)
    store = SQLiteStore.open(tmp_path / "state.db")
    store.create_run(
        "run-1", "activation-1", graph.contract_digest, "9" * 64, signed.model_dump_json(), NOW
    )
    store.set_acceptance_key("run-1", 7)
    store.set_segment_path("run-1", tmp_path / "legacy" / "segment.csv")
    legacy_manifest = {
        "schema_version": "1",
        "batch_id": "batch-1",
        "segment_path": str(tmp_path / "legacy" / "segment.csv"),
        "segment_digest": "4" * 64,
        "row_set_digest": "5" * 64,
        "row_count": 1,
        "encoded_bytes": 7,
        "schema_digest": "6" * 64,
        "source_boundary_digest": "7" * 64,
        "acceptance_key": 7,
        "acceptance_value_digest": "8" * 64,
    }
    manifest_payload = canonical_bytes(legacy_manifest)
    manifest_digest = digest(legacy_manifest)
    store.save_artifact("segment_manifest", manifest_digest, manifest_payload)
    checkpoint = "created"
    for next_checkpoint, event_type, attributes in (
        ("graph_verified", "graph_verified", {"graph_digest": signed.graph_digest}),
        ("drift_revalidated", "drift_revalidated", {"drift_probe_digest": "1" * 64}),
        ("snapshot_opened", "snapshot_opened", {"source_boundary_digest": "7" * 64}),
        (
            "extraction_completed",
            "extraction_completed",
            {"manifest_digest": manifest_digest, "row_count": 1},
        ),
    ):
        store.advance_checkpoint_with_event(
            "run-1",
            expected_checkpoint=checkpoint,
            checkpoint=next_checkpoint,
            state="running",
            event_type=event_type,
            occurred_at=NOW,
            producer="pillarmesh-runtime",
            attributes=attributes,
            batch_id="batch-1",
        )
        checkpoint = next_checkpoint
    runtime = Runtime(
        store=store,
        verifier=GraphVerifier({"key-1": signer.public_key}),
        source_resolver=lambda _handle: (_ for _ in ()).throw(AssertionError("must not read")),
        destination_resolver=lambda _handle: (_ for _ in ()).throw(
            AssertionError("must not resolve destination")
        ),
        segment_encoder=lambda *_args: (_ for _ in ()).throw(AssertionError("must not encode")),
        output_dir=tmp_path,
        clock=lambda: NOW,
    )

    with pytest.raises(
        MigrationError, match="schema-v1 manifest cannot resume under privacy-safe runtime"
    ):
        runtime.resume("run-1")
