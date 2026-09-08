from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pillarmesh_compiler import AdmittedPlan, PhysicalPlan
from pillarmesh_contract_model import ArtifactModel, IntegrationContract, canonical_bytes, digest
from pillarmesh_execution_graph import GraphVerifier, SignedExecutionGraph
from pillarmesh_iir import IntentIR
from pillarmesh_provider_sdk import (
    CommitReceipt,
    ProviderObservation,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from .models import EvidenceEvent
from .package_models import (
    ArtifactEdge,
    ArtifactEntry,
    CheckDisposition,
    PackageIndex,
    PackageMetadata,
    PackageResult,
    ResourceDisposition,
    ScanInput,
    VerificationResult,
)
from .scanner import scan_bytes
from .store import SQLiteStore

_CHECKS = (
    CheckDisposition(name="package_structure"),
    CheckDisposition(name="payload_hashes"),
    CheckDisposition(name="artifact_digests"),
    CheckDisposition(name="artifact_graph"),
    CheckDisposition(name="graph_signature"),
    CheckDisposition(name="event_chain"),
    CheckDisposition(name="terminal_visibility"),
    CheckDisposition(name="sensitive_value_scan"),
)
_EXPECTED_COUNTS = {
    "integration_contract": 1,
    "provider_observation": 2,
    "intent_ir": 1,
    "physical_plan": 1,
    "legality_decision": 1,
    "signed_execution_graph": 1,
    "activation_summary": 1,
    "source_boundary": 1,
    "segment_manifest": 1,
    "commit_receipt": 1,
    "visibility_proof": 1,
}


class PackageError(RuntimeError):
    pass


class _ActivationSummary(ArtifactModel):
    schema_version: Literal["1"]
    contract_id: str
    contract_version: int
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    iir_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    legality_decision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_graph_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_id: str
    verified_at: datetime
    expires_at: datetime
    limitations: tuple[str, ...]
    source_effect: str
    destination_effect: str
    signed_graph: SignedExecutionGraph

    @model_validator(mode="after")
    def bound_graph(self) -> _ActivationSummary:
        graph = self.signed_graph.graph
        links = (
            (self.contract_digest, graph.contract_digest),
            (self.source_observation_digest, graph.source_observation_digest),
            (self.destination_observation_digest, graph.destination_observation_digest),
            (self.iir_digest, graph.iir_digest),
            (self.physical_plan_digest, graph.physical_plan_digest),
            (self.legality_decision_digest, graph.legality_decision_digest),
            (self.graph_digest, self.signed_graph.graph_digest),
        )
        if any(expected != actual for expected, actual in links):
            raise ValueError("activation summary parent mismatch")
        if self.graph_digest != digest(graph):
            raise ValueError("activation summary graph digest mismatch")
        if self.signed_graph_artifact_digest != digest(self.signed_graph):
            raise ValueError("activation summary signed graph digest mismatch")
        return self


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_model[TArtifact: BaseModel](model: type[TArtifact], payload: bytes) -> TArtifact:
    try:
        parsed = model.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise PackageError("artifact validation failed") from error
    if canonical_bytes(parsed) != payload:
        raise PackageError("artifact is not canonical JSON")
    return parsed


def _only(
    artifacts: Mapping[str, tuple[tuple[str, BaseModel], ...]], kind: str
) -> tuple[str, BaseModel]:
    values = artifacts.get(kind, ())
    if len(values) != 1:
        raise PackageError("artifact inventory is incomplete")
    return values[0]


def _validate_artifact_graph(
    entries: tuple[ArtifactEntry, ...],
    payloads: Mapping[str, bytes],
    verifier: GraphVerifier,
) -> tuple[ArtifactEdge, ...]:
    counts: dict[str, int] = {}
    parsed: dict[str, list[tuple[str, BaseModel]]] = {}
    models: dict[str, type[BaseModel]] = {
        "integration_contract": IntegrationContract,
        "provider_observation": ProviderObservation,
        "intent_ir": IntentIR,
        "physical_plan": PhysicalPlan,
        "legality_decision": AdmittedPlan,
        "signed_execution_graph": SignedExecutionGraph,
        "activation_summary": _ActivationSummary,
        "source_boundary": SourceBoundary,
        "segment_manifest": SegmentManifest,
        "commit_receipt": CommitReceipt,
        "visibility_proof": VisibilityProof,
    }
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
        payload = payloads.get(entry.relative_path)
        if payload is None or _sha256(payload) != entry.sha256 or _sha256(payload) != entry.digest:
            raise PackageError("artifact digest validation failed")
        model = _parse_model(models[entry.kind], payload)
        parsed.setdefault(entry.kind, []).append((entry.digest, model))
    if counts != _EXPECTED_COUNTS:
        raise PackageError("artifact inventory is incomplete")
    artifacts = {kind: tuple(values) for kind, values in parsed.items()}

    contract_digest, contract_model = _only(artifacts, "integration_contract")
    iir_digest, iir_model = _only(artifacts, "intent_ir")
    plan_digest, _plan_model = _only(artifacts, "physical_plan")
    decision_digest, decision_model = _only(artifacts, "legality_decision")
    signed_digest, signed_model = _only(artifacts, "signed_execution_graph")
    summary_digest, summary_model = _only(artifacts, "activation_summary")
    boundary_digest, _boundary_model = _only(artifacts, "source_boundary")
    manifest_digest, manifest_model = _only(artifacts, "segment_manifest")
    receipt_digest, receipt_model = _only(artifacts, "commit_receipt")
    visibility_digest, visibility_model = _only(artifacts, "visibility_proof")
    if not isinstance(contract_model, IntegrationContract):
        raise PackageError("contract artifact type mismatch")
    if not isinstance(iir_model, IntentIR):
        raise PackageError("IIR artifact type mismatch")
    if not isinstance(decision_model, AdmittedPlan):
        raise PackageError("legality artifact type mismatch")
    if not isinstance(signed_model, SignedExecutionGraph):
        raise PackageError("signed graph artifact type mismatch")
    if not isinstance(summary_model, _ActivationSummary):
        raise PackageError("activation artifact type mismatch")
    if not isinstance(manifest_model, SegmentManifest):
        raise PackageError("manifest artifact type mismatch")
    if not isinstance(receipt_model, CommitReceipt):
        raise PackageError("receipt artifact type mismatch")
    if not isinstance(visibility_model, VisibilityProof):
        raise PackageError("visibility artifact type mismatch")
    if manifest_model.schema_version != "2" or visibility_model.schema_version != "2":
        raise PackageError("privacy-incompatible artifact schema")

    observations: list[tuple[str, ProviderObservation]] = []
    for observation_digest, observation_model in artifacts["provider_observation"]:
        if not isinstance(observation_model, ProviderObservation):
            raise PackageError("provider observation artifact type mismatch")
        observations.append((observation_digest, observation_model))
    source = next(
        (
            (item_digest, item)
            for item_digest, item in observations
            if item.provider == "postgresql"
        ),
        None,
    )
    destination = next(
        ((item_digest, item) for item_digest, item in observations if item.provider == "snowflake"),
        None,
    )
    if source is None or destination is None:
        raise PackageError("provider observations are incomplete")
    source_digest, source_model = source
    destination_digest, destination_model = destination
    graph = signed_model.graph
    expected_links = (
        (contract_digest, graph.contract_digest),
        (iir_digest, graph.iir_digest),
        (plan_digest, graph.physical_plan_digest),
        (decision_digest, graph.legality_decision_digest),
        (source_digest, graph.source_observation_digest),
        (destination_digest, graph.destination_observation_digest),
        (plan_digest, decision_model.physical_plan_digest),
        (signed_digest, summary_model.signed_graph_artifact_digest),
        (boundary_digest, manifest_model.source_boundary_digest),
        (manifest_digest, receipt_model.manifest_digest),
    )
    if any(expected != actual for expected, actual in expected_links):
        raise PackageError("artifact parent linkage failed")
    if (
        contract_model.contract_id != iir_model.contract_id
        or contract_model.version != iir_model.contract_version
        or contract_model.source.connection_handle != source_model.connection_handle
        or contract_model.destination.connection_handle != destination_model.connection_handle
        or graph.source_connection_handle != source_model.connection_handle
        or graph.destination_connection_handle != destination_model.connection_handle
        or graph.source_object_identity != source_model.object_identity
        or manifest_model.batch_id != receipt_model.batch_id
        or manifest_model.batch_id != visibility_model.batch_id
        or manifest_model.acceptance_value_digest != visibility_model.value_digest
    ):
        raise PackageError("artifact semantic linkage failed")
    try:
        verifier.verify(signed_model, graph.issued_at)
    except Exception as error:
        raise PackageError("graph signature validation failed") from error

    edges = (
        ArtifactEdge(
            parent_digest=contract_digest,
            child_digest=iir_digest,
            relationship="lowered_to",
        ),
        ArtifactEdge(
            parent_digest=contract_digest,
            child_digest=source_digest,
            relationship="source_observed_as",
        ),
        ArtifactEdge(
            parent_digest=contract_digest,
            child_digest=destination_digest,
            relationship="destination_observed_as",
        ),
        ArtifactEdge(
            parent_digest=plan_digest,
            child_digest=decision_digest,
            relationship="admitted_by",
        ),
        ArtifactEdge(
            parent_digest=contract_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=iir_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=plan_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=decision_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=source_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=destination_digest,
            child_digest=signed_digest,
            relationship="graph_parent",
        ),
        ArtifactEdge(
            parent_digest=signed_digest,
            child_digest=summary_digest,
            relationship="published_as",
        ),
        ArtifactEdge(
            parent_digest=summary_digest,
            child_digest=boundary_digest,
            relationship="run_produced_boundary",
        ),
        ArtifactEdge(
            parent_digest=boundary_digest,
            child_digest=manifest_digest,
            relationship="describes_segment",
        ),
        ArtifactEdge(
            parent_digest=manifest_digest,
            child_digest=receipt_digest,
            relationship="committed_as",
        ),
        ArtifactEdge(
            parent_digest=manifest_digest,
            child_digest=visibility_digest,
            relationship="verified_by",
        ),
    )
    return tuple(sorted(edges, key=lambda item: (item.parent_digest, item.child_digest)))


def _validate_trace(
    payload: bytes, run_id: str, trace_head_digest: str
) -> tuple[EvidenceEvent, ...]:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, list):
            raise TypeError
        events = tuple(EvidenceEvent.model_validate(item) for item in raw)
    except (TypeError, ValidationError, json.JSONDecodeError) as error:
        raise PackageError("evidence trace validation failed") from error
    if canonical_bytes(events) != payload or not events:
        raise PackageError("evidence trace is not canonical")
    previous: str | None = None
    for sequence, event in enumerate(events, start=1):
        body = {
            "run_id": event.run_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "producer": event.producer,
            "attributes": event.attributes,
            "previous_digest": event.previous_digest,
        }
        if (
            event.run_id != run_id
            or event.sequence != sequence
            or event.previous_digest != previous
            or _sha256(canonical_bytes(body)) != event.event_digest
        ):
            raise PackageError("evidence event chain validation failed")
        previous = event.event_digest
    if previous != trace_head_digest:
        raise PackageError("trace head validation failed")
    return events


def _terminal_links(events: tuple[EvidenceEvent, ...], entries: tuple[ArtifactEntry, ...]) -> None:
    if events[-1].event_type != "terminal_success":
        raise PackageError("terminal success evidence is missing")
    terminal = events[-1].attributes
    by_kind: dict[str, str] = {
        entry.kind: entry.digest for entry in entries if entry.kind != "provider_observation"
    }
    if (
        terminal.get("manifest_digest") != by_kind["segment_manifest"]
        or terminal.get("receipt_digest") != by_kind["commit_receipt"]
        or terminal.get("visibility_digest") != by_kind["visibility_proof"]
    ):
        raise PackageError("terminal visibility linkage failed")
    required_types = (
        "draft_created",
        "verification_completed",
        "legality_admitted",
        "graph_signed",
        "activation",
        "extraction_completed",
        "manifest_created",
        "commit_attempted",
        "commit_resolved",
        "visibility_verified",
        "terminal_success",
    )
    lifecycle: dict[str, EvidenceEvent] = {}
    positions: list[int] = []
    for event_type in required_types:
        matching = [
            (position, event)
            for position, event in enumerate(events)
            if event.event_type == event_type
        ]
        if len(matching) != 1:
            raise PackageError("required lifecycle evidence is missing or ambiguous")
        position, event = matching[0]
        positions.append(position)
        lifecycle[event_type] = event
    if positions != sorted(positions):
        raise PackageError("required lifecycle evidence is out of order")
    verification = lifecycle["verification_completed"]
    graph_signed = lifecycle["graph_signed"]
    if verification is None or graph_signed is None:
        raise PackageError("compiler lifecycle evidence is missing")
    for kind, attribute in (
        ("activation_summary", "summary_digest"),
        ("intent_ir", "iir_digest"),
        ("physical_plan", "physical_plan_digest"),
        ("legality_decision", "legality_decision_digest"),
        ("signed_execution_graph", "signed_graph_artifact_digest"),
    ):
        if verification.attributes.get(attribute) != by_kind[kind]:
            raise PackageError("compiler lifecycle linkage failed")
    if (
        graph_signed.attributes.get("signed_graph_artifact_digest")
        != by_kind["signed_execution_graph"]
    ):
        raise PackageError("signed graph lifecycle linkage failed")
    observations = {entry.digest for entry in entries if entry.kind == "provider_observation"}
    if {
        verification.attributes.get("source_observation_digest"),
        verification.attributes.get("destination_observation_digest"),
    } != observations:
        raise PackageError("provider observation lifecycle linkage failed")
    expected_attributes = (
        ("draft_created", "contract_digest", "integration_contract"),
        ("activation", "contract_digest", "integration_contract"),
        ("activation", "summary_digest", "activation_summary"),
        ("legality_admitted", "legality_decision_digest", "legality_decision"),
        ("extraction_completed", "source_boundary_digest", "source_boundary"),
        ("extraction_completed", "manifest_digest", "segment_manifest"),
        ("manifest_created", "manifest_digest", "segment_manifest"),
        ("commit_attempted", "manifest_digest", "segment_manifest"),
        ("commit_resolved", "receipt_digest", "commit_receipt"),
        ("visibility_verified", "visibility_digest", "visibility_proof"),
    )
    if any(
        lifecycle[event_type].attributes.get(attribute) != by_kind[kind]
        for event_type, attribute, kind in expected_attributes
    ):
        raise PackageError("runtime lifecycle linkage failed")


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _read_package_files(path: Path) -> dict[str, bytes]:
    if not path.is_dir() or path.is_symlink():
        raise PackageError("package directory is invalid")
    result: dict[str, bytes] = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise PackageError("package contains a symbolic link")
        if item.is_file():
            result[item.relative_to(path).as_posix()] = item.read_bytes()
    return result


# A payload path inside a package under verification is caller-controlled: `_safe_relative_path`
# only rejects traversal, so a crafted package can carry a filename that itself contains a canary.
# Diagnostics may therefore name a path only when it matches the inventory the exporter builds.
_DIAGNOSTIC_PATH = re.compile(
    r"^(?:package\.json"
    r"|trace/events\.json"
    r"|verification/result\.json"
    r"|operations/(?:resources|limitations)\.json"
    r"|artifacts/[a-z_]{1,64}/[0-9a-f]{64}\.json)$"
)


def _scan_payloads(payloads: Mapping[str, bytes], scan_input: ScanInput) -> None:
    # Reporting the rule, offset and payload turns a fail-closed scan into a diagnosable one; the
    # matched bytes stay out of the message because they are the sensitive value being hunted.
    for relative_path in sorted(payloads):
        findings = scan_bytes(relative_path, payloads[relative_path], scan_input)
        if not findings:
            continue
        finding = findings[0]
        safe_name = _DIAGNOSTIC_PATH.fullmatch(relative_path) and not scan_bytes(
            relative_path, relative_path.encode("utf-8"), scan_input
        )
        named = relative_path if safe_name else "an unnamed payload"
        raise PackageError(
            "sensitive material scan failed: "
            f"rule {finding.rule_id} at byte {finding.byte_offset} of {named}"
        )


def _verify_core(
    path: Path,
    verifier: GraphVerifier,
    scan_input: ScanInput,
    *,
    require_result: bool,
) -> tuple[str, tuple[CheckDisposition, ...], str]:
    files = _read_package_files(path)
    index_payload = files.get("package.json")
    if index_payload is None:
        raise PackageError("package index is missing")
    index = _parse_model(PackageIndex, index_payload)
    if len({entry.relative_path for entry in index.artifacts}) != len(index.artifacts):
        raise PackageError("artifact paths are not unique")
    for entry in index.artifacts:
        expected_path = f"artifacts/{entry.kind}/{entry.digest}.json"
        if entry.relative_path != expected_path or not _safe_relative_path(entry.relative_path):
            raise PackageError("artifact path validation failed")
    declared = set(index.payload_sha256)
    expected_files = declared | {"package.json"}
    if require_result:
        expected_files.add("verification/result.json")
    if set(files) != expected_files:
        raise PackageError("package file inventory validation failed")
    if any(not _safe_relative_path(relative_path) for relative_path in declared):
        raise PackageError("payload path validation failed")
    for relative_path, expected_digest in index.payload_sha256.items():
        payload = files.get(relative_path)
        if payload is None or _sha256(payload) != expected_digest:
            raise PackageError("package payload hash validation failed")
    _scan_payloads(
        {
            relative_path: payload
            for relative_path, payload in files.items()
            if require_result or relative_path != "verification/result.json"
        },
        scan_input,
    )
    resources_payload = files.get("operations/resources.json")
    limitations_payload = files.get("operations/limitations.json")
    if resources_payload is None or limitations_payload is None:
        raise PackageError("operations metadata is missing")
    try:
        resources = TypeAdapter(tuple[ResourceDisposition, ...]).validate_json(resources_payload)
        limitations = TypeAdapter(tuple[str, ...]).validate_json(limitations_payload)
    except ValidationError as error:
        raise PackageError("operations metadata validation failed") from error
    if (
        canonical_bytes(resources) != resources_payload
        or canonical_bytes(limitations) != limitations_payload
    ):
        raise PackageError("operations metadata is not canonical")
    artifact_payloads = {
        entry.relative_path: files[entry.relative_path] for entry in index.artifacts
    }
    expected_edges = _validate_artifact_graph(index.artifacts, artifact_payloads, verifier)
    if index.edges != expected_edges:
        raise PackageError("artifact edge inventory validation failed")
    trace_payload = files.get("trace/events.json")
    if trace_payload is None:
        raise PackageError("evidence trace is missing")
    events = _validate_trace(trace_payload, index.run_id, index.trace_head_digest)
    _terminal_links(events, index.artifacts)
    package_index_digest = _sha256(index_payload)
    result_digest = "0" * 64
    if require_result:
        result_payload = files.get("verification/result.json")
        if result_payload is None:
            raise PackageError("verification result is missing")
        result = _parse_model(VerificationResult, result_payload)
        if result.package_index_digest != package_index_digest or result.checks != _CHECKS:
            raise PackageError("verification result validation failed")
        result_digest = _sha256(result_payload)
    return package_index_digest, _CHECKS, result_digest


def _require_graph_verifier(verifier: object) -> GraphVerifier:
    if not isinstance(verifier, GraphVerifier):
        raise PackageError("graph verifier is required")
    return verifier


def verify_package(path: Path, verifier: GraphVerifier, scan_input: ScanInput) -> PackageResult:
    checked_verifier = _require_graph_verifier(verifier)
    package_index_digest, checks, result_digest = _verify_core(
        path, checked_verifier, scan_input, require_result=True
    )
    return PackageResult(
        path=path,
        package_index_digest=package_index_digest,
        verification_result_digest=result_digest,
        checks=checks,
    )


def _write_payload(root: Path, relative_path: str, payload: bytes) -> None:
    target = root.joinpath(*PurePosixPath(relative_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def export_package(
    store: SQLiteStore,
    run_id: str,
    destination: Path,
    metadata: PackageMetadata,
    scan_input: ScanInput,
    verifier: GraphVerifier,
) -> PackageResult:
    checked_verifier = _require_graph_verifier(verifier)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        run = store.get_run(run_id)
        if run.state != "succeeded":
            raise PackageError("only a terminal successful run can be exported")
        stored = store.list_run_artifacts(run_id)
        entries = tuple(
            ArtifactEntry.model_validate(
                {
                    "kind": kind,
                    "digest": artifact_digest,
                    "relative_path": f"artifacts/{kind}/{artifact_digest}.json",
                    "sha256": _sha256(payload),
                }
            )
            for kind, artifact_digest, payload in stored
        )
        payloads = {
            entry.relative_path: payload
            for entry, (_kind, _digest, payload) in zip(entries, stored, strict=True)
        }
        trace = store.trace(run_id)
        if not trace:
            raise PackageError("evidence trace is empty")
        payloads["trace/events.json"] = canonical_bytes(trace)
        payloads["operations/resources.json"] = canonical_bytes(metadata.resources)
        payloads["operations/limitations.json"] = canonical_bytes(metadata.limitations)
        _scan_payloads(payloads, scan_input)
        edges = _validate_artifact_graph(entries, payloads, checked_verifier)
        for relative_path, payload in payloads.items():
            _write_payload(temporary, relative_path, payload)
        payload_hashes = {
            relative_path: _sha256(payload) for relative_path, payload in sorted(payloads.items())
        }
        index = PackageIndex(
            run_id=run_id,
            commit_sha=metadata.commit_sha,
            uv_lock_digest=metadata.uv_lock_digest,
            python_version=metadata.python_version,
            mcp_protocol_version=metadata.mcp_protocol_version,
            operator_pseudonym=metadata.operator_pseudonym,
            host_pseudonym=metadata.host_pseudonym,
            transport_decision=metadata.transport_decision,
            artifacts=entries,
            edges=edges,
            trace_head_digest=trace[-1].event_digest,
            payload_sha256=payload_hashes,
        )
        index_payload = canonical_bytes(index)
        _write_payload(temporary, "package.json", index_payload)
        _verify_core(temporary, checked_verifier, scan_input, require_result=False)
        result = VerificationResult(
            checks=_CHECKS,
            package_index_digest=_sha256(index_payload),
        )
        result_payload = canonical_bytes(result)
        _write_payload(temporary, "verification/result.json", result_payload)
        _verify_core(temporary, checked_verifier, scan_input, require_result=True)
        _scan_payloads(_read_package_files(temporary), scan_input)
        if destination.exists():
            raise PackageError("package destination already exists")
        package_result = PackageResult(
            path=destination,
            package_index_digest=_sha256(index_payload),
            verification_result_digest=_sha256(result_payload),
            checks=_CHECKS,
        )
        temporary.rename(destination)
        return package_result
    except PackageError:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    except Exception as error:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise PackageError("evidence package export failed") from error
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
