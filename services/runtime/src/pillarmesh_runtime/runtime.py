from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_evidence import MigrationError, SQLiteStore
from pillarmesh_execution_graph import GraphVerifier, InvalidGraph, SignedExecutionGraph
from pillarmesh_provider_sdk import (
    CommitReceipt,
    DriftProbe,
    OrderRow,
    ProviderError,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from pydantic import ValidationError

from .faults import FaultHook, noop_fault_hook
from .models import RunResult
from .retry import retry_bounded

_EVIDENCE_WRITE_MARKER = "_pillarmesh_evidence_write_failure"
_RESOLVABLE_CHECKPOINTS = {"commit_attempted", "commit_resolved", "visibility_verified"}
_NON_CONFORMING_CLASSIFICATIONS = {"permanent", "authorization"}


class RuntimeSource(Protocol):
    def drift_probe(self) -> DriftProbe: ...

    def read_snapshot(self) -> tuple[SourceBoundary, Iterable[OrderRow]]: ...


class RuntimeDestination(Protocol):
    def stage(self, segment: Path, manifest: SegmentManifest) -> None: ...

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt: ...

    def verify_visibility(
        self, manifest: SegmentManifest, acceptance_key: int
    ) -> VisibilityProof: ...


class SegmentEncoder(Protocol):
    def __call__(
        self,
        rows: Iterable[OrderRow],
        boundary: SourceBoundary | Callable[[], SourceBoundary],
        acceptance_key: int,
        batch_id: str,
        output: Path,
    ) -> SegmentManifest: ...


class Runtime:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        verifier: GraphVerifier,
        source_resolver: Callable[[str], RuntimeSource],
        destination_resolver: Callable[[str], RuntimeDestination],
        segment_encoder: SegmentEncoder,
        output_dir: Path,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        fault_hook: FaultHook = noop_fault_hook,
    ) -> None:
        self._store = store
        self._verifier = verifier
        self._source_resolver = source_resolver
        self._destination_resolver = destination_resolver
        self._segment_encoder = segment_encoder
        self._output_dir = output_dir
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._fault_hook = fault_hook

    def _advance(
        self,
        run_id: str,
        expected: str,
        checkpoint: str,
        event_type: str,
        attributes: dict[str, object],
        *,
        batch_id: str | None = None,
        state: str = "running",
    ) -> None:
        try:
            self._store.advance_checkpoint_with_event(
                run_id,
                expected_checkpoint=expected,
                checkpoint=checkpoint,
                state=state,  # type: ignore[arg-type]
                event_type=event_type,
                occurred_at=self._clock(),
                producer="pillarmesh-runtime",
                attributes=attributes,
                batch_id=batch_id,
            )
        except Exception as error:
            setattr(error, _EVIDENCE_WRITE_MARKER, True)
            raise

    def _save(self, kind: str, artifact: object) -> str:
        artifact_digest = digest(artifact)
        try:
            self._store.save_artifact(kind, artifact_digest, canonical_bytes(artifact))
        except Exception as error:
            setattr(error, _EVIDENCE_WRITE_MARKER, True)
            raise
        return artifact_digest

    def _fail(self, run_id: str, error: BaseException) -> None:
        current = self._store.get_run(run_id)
        if current.state in {"succeeded", "failed", "non_conforming"}:
            return
        if current.checkpoint in _RESOLVABLE_CHECKPOINTS and getattr(
            error, _EVIDENCE_WRITE_MARKER, False
        ):
            return
        self._advance(
            run_id,
            current.checkpoint,
            "failed",
            "terminal_failure",
            {"error_type": type(error).__name__},
            state="failed",
        )

    def _non_conforming(self, run_id: str, error: ProviderError) -> None:
        current = self._store.get_run(run_id)
        self._advance(
            run_id,
            current.checkpoint,
            "non_conforming",
            "terminal_non_conforming",
            {"error_type": type(error).__name__, "classification": error.classification},
            state="non_conforming",
        )

    def _event_value(self, run_id: str, event_type: str, key: str) -> str:
        for event in reversed(self._store.trace(run_id)):
            if event.event_type == event_type:
                value = event.attributes.get(key)
                if isinstance(value, str):
                    return value
        raise RuntimeError(f"run evidence is missing {event_type}.{key}")

    def _assert_run_budget(self, run_id: str, max_seconds: int) -> None:
        record = self._store.get_run(run_id)
        elapsed = (self._clock() - record.created_at).total_seconds()
        if elapsed < 0 or elapsed > max_seconds:
            raise RuntimeError("run wall-clock budget exceeded")

    def _assert_evidence_ready(self, run_id: str, signed_graph: SignedExecutionGraph) -> None:
        present = {event.event_type for event in self._store.trace(run_id)}
        required = {
            requirement.event_type
            for requirement in signed_graph.graph.required_evidence
            if requirement.event_type != "terminal_success"
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError("mandatory evidence is missing: " + ", ".join(missing))

    def _load_manifest(self, run_id: str) -> tuple[SegmentManifest, str]:
        manifest_digest = self._event_value(run_id, "extraction_completed", "manifest_digest")
        payload = self._store.load_artifact("segment_manifest", manifest_digest)
        if payload is None:
            raise RuntimeError("run manifest artifact is missing")
        parsed = json.loads(payload)
        if isinstance(parsed, dict) and parsed.get("schema_version") == "1":
            raise MigrationError("schema-v1 manifest cannot resume under privacy-safe runtime")
        return SegmentManifest.model_validate_json(payload), manifest_digest

    @staticmethod
    def _assert_probe(probe: DriftProbe, object_identity: str, schema_digest: str) -> None:
        if probe.object_identity != object_identity or probe.schema_digest != schema_digest:
            raise RuntimeError("provider drift detected before snapshot")

    def execute(
        self, run_id: str, signed_graph: SignedExecutionGraph, acceptance_key: int
    ) -> RunResult:
        try:
            record = self._store.get_run(run_id)
            graph = self._verifier.verify(signed_graph, self._clock())
            try:
                stored_graph = SignedExecutionGraph.model_validate_json(record.signed_graph_json)
            except ValidationError:
                raise InvalidGraph("stored graph envelope is incompatible") from None
            if digest(stored_graph) != digest(signed_graph):
                raise InvalidGraph(
                    "GRAPH_ENVELOPE_MISMATCH: supplied graph differs from activated graph"
                )
            if graph.contract_digest != record.contract_digest:
                raise InvalidGraph(
                    "GRAPH_RUN_CONTRACT_MISMATCH: graph contract differs from admitted run"
                )
            self._assert_run_budget(run_id, graph.max_runtime_seconds)
            self._store.set_acceptance_key(run_id, acceptance_key)
            private_state = self._store.get_private_state(run_id)
            batch_id = digest(
                {"domain": "pillarmesh-m0-batch-v1", "run_id": run_id, "graph": graph}
            )[:32]
            checkpoint = record.checkpoint
            if checkpoint == "created":
                self._advance(
                    run_id,
                    "created",
                    "graph_verified",
                    "graph_verified",
                    {"graph_digest": signed_graph.graph_digest, "key_id": signed_graph.key_id},
                )
                checkpoint = "graph_verified"

            if checkpoint in {"graph_verified", "drift_revalidated", "snapshot_opened"}:
                source = self._source_resolver(graph.source_connection_handle)
                probe = source.drift_probe()
                self._assert_probe(probe, graph.source_object_identity, graph.source_schema_digest)
                if checkpoint == "graph_verified":
                    self._advance(
                        run_id,
                        checkpoint,
                        "drift_revalidated",
                        "drift_revalidated",
                        {"drift_probe_digest": digest(probe)},
                    )
                    checkpoint = "drift_revalidated"
                elif checkpoint == "snapshot_opened":
                    self._store.append_event(
                        run_id,
                        "snapshot_recovery_started",
                        self._clock(),
                        "pillarmesh-runtime",
                        {"drift_probe_digest": digest(probe)},
                    )

                boundary, rows = source.read_snapshot()
                if checkpoint == "drift_revalidated":
                    self._advance(
                        run_id,
                        checkpoint,
                        "snapshot_opened",
                        "snapshot_opened",
                        {
                            "object_identity": boundary.object_identity,
                            "schema_digest": boundary.schema_digest,
                            "snapshot_identity_digest": digest(boundary.snapshot_identity),
                        },
                    )
                    checkpoint = "snapshot_opened"
                else:
                    self._store.append_event(
                        run_id,
                        "snapshot_reopened",
                        self._clock(),
                        "pillarmesh-runtime",
                        {"snapshot_identity_digest": digest(boundary.snapshot_identity)},
                    )
                self._fault_hook("before_extraction")
                run_output = self._output_dir / run_id
                run_output.mkdir(parents=True, exist_ok=True)
                final_boundary = getattr(rows, "final_boundary", None)
                boundary_input = final_boundary if callable(final_boundary) else boundary
                try:
                    manifest = self._segment_encoder(
                        rows,
                        boundary_input,
                        acceptance_key,
                        batch_id,
                        run_output / "segment.csv",
                    )
                except BaseException:
                    abort = getattr(rows, "abort", None)
                    if callable(abort):
                        abort()
                    raise
                completed_boundary = final_boundary() if callable(final_boundary) else boundary
                boundary_payload = canonical_bytes(completed_boundary)
                boundary_digest = digest(completed_boundary)
                if manifest.source_boundary_digest != boundary_digest:
                    raise RuntimeError("manifest source boundary linkage is invalid")
                manifest_payload = canonical_bytes(manifest)
                manifest_digest = digest(manifest)
                self._store.record_extraction(
                    run_id,
                    source_boundary_digest=boundary_digest,
                    source_boundary_payload=boundary_payload,
                    manifest_digest=manifest_digest,
                    manifest_payload=manifest_payload,
                    segment_path=run_output / manifest.segment_name,
                    batch_id=batch_id,
                    row_count=manifest.row_count,
                    encoded_bytes=manifest.encoded_bytes,
                    occurred_at=self._clock(),
                    producer="pillarmesh-runtime",
                )
                self._fault_hook("after_extraction_persisted")
                private_state = self._store.get_private_state(run_id)
                checkpoint = "extraction_completed"
            else:
                manifest, manifest_digest = self._load_manifest(run_id)

            if checkpoint == "extraction_completed":
                self._advance(
                    run_id,
                    checkpoint,
                    "manifest_created",
                    "manifest_created",
                    {"manifest_digest": manifest_digest},
                )
                checkpoint = "manifest_created"

            destination = self._destination_resolver(graph.destination_connection_handle)
            if checkpoint == "manifest_created":
                segment_path = private_state.segment_path
                if segment_path is None:
                    raise RuntimeError("run has no durable segment path")
                retry_bounded(
                    lambda: destination.stage(segment_path, manifest),
                    sleeper=self._sleeper,
                    monotonic=self._monotonic,
                )
                self._advance(
                    run_id,
                    checkpoint,
                    "commit_attempted",
                    "commit_attempted",
                    {"batch_id": batch_id, "manifest_digest": manifest_digest},
                )
                checkpoint = "commit_attempted"

            if checkpoint == "commit_attempted":
                self._fault_hook("during_commit")
                receipt = retry_bounded(
                    lambda: destination.commit_or_resolve(manifest),
                    sleeper=self._sleeper,
                    monotonic=self._monotonic,
                )
                self._fault_hook("after_commit_before_receipt")
                receipt_digest = self._save("commit_receipt", receipt)
                self._advance(
                    run_id,
                    checkpoint,
                    "commit_resolved",
                    "commit_resolved",
                    {"receipt_digest": receipt_digest, "replayed": receipt.replayed},
                )
                checkpoint = "commit_resolved"
                self._fault_hook("after_receipt_before_visibility")
            else:
                receipt_digest = self._event_value(run_id, "commit_resolved", "receipt_digest")

            if checkpoint == "commit_resolved":
                acceptance_key_value = private_state.acceptance_key
                if acceptance_key_value is None:
                    raise RuntimeError("run has no durable acceptance key")
                try:
                    proof = retry_bounded(
                        lambda: destination.verify_visibility(manifest, acceptance_key_value),
                        sleeper=self._sleeper,
                        monotonic=self._monotonic,
                    )
                    if proof.value_digest != manifest.acceptance_value_digest:
                        raise ProviderError(
                            "visibility proof digest does not match the source manifest",
                            "permanent",
                        )
                except ProviderError as error:
                    # Only a verdict about the destination itself is non-conformance.
                    # A transient failure that merely outlived the bounded retry says
                    # nothing about conformance and must not be recorded as such.
                    if error.classification in _NON_CONFORMING_CLASSIFICATIONS:
                        self._non_conforming(run_id, error)
                    raise
                visibility_digest = self._save("visibility_proof", proof)
                self._advance(
                    run_id,
                    checkpoint,
                    "visibility_verified",
                    "visibility_verified",
                    {"visibility_digest": visibility_digest},
                )
                checkpoint = "visibility_verified"
            else:
                visibility_digest = self._event_value(
                    run_id, "visibility_verified", "visibility_digest"
                )

            if checkpoint == "visibility_verified":
                self._assert_run_budget(run_id, graph.max_runtime_seconds)
                self._assert_evidence_ready(run_id, signed_graph)
                self._advance(
                    run_id,
                    checkpoint,
                    "terminal_success",
                    "terminal_success",
                    {
                        "manifest_digest": manifest_digest,
                        "receipt_digest": receipt_digest,
                        "visibility_digest": visibility_digest,
                    },
                    state="succeeded",
                )
            return RunResult(
                run_id=run_id,
                state="succeeded",
                batch_id=batch_id,
                manifest_digest=manifest_digest,
                receipt_digest=receipt_digest,
                visibility_digest=visibility_digest,
            )
        except Exception as error:
            self._fail(run_id, error)
            raise

    def resume(self, run_id: str) -> RunResult:
        record = self._store.get_run(run_id)
        if record.state == "failed":
            raise RuntimeError("failed run cannot resume")
        if record.state == "non_conforming":
            raise RuntimeError("non-conforming run cannot resume")
        private_state = self._store.get_private_state(run_id)
        if private_state.acceptance_key is None:
            raise RuntimeError("run has no durable acceptance key")
        signed_graph = SignedExecutionGraph.model_validate_json(record.signed_graph_json)
        return self.execute(run_id, signed_graph, private_state.acceptance_key)
