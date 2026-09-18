from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import FIXED_PROJECTION, IntegrationContract, canonical_bytes, digest
from heinzel_contract_service import ActivationSummary, ContractService
from heinzel_evidence import (
    PackageError,
    PackageMetadata,
    ResourceDisposition,
    ScanInput,
    SQLiteStore,
    export_package,
    verify_package,
)
from heinzel_execution_graph import GraphSigner, GraphVerifier
from heinzel_provider_sdk import (
    ColumnObservation,
    CommitReceipt,
    ProviderObservation,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
RUN_ID = "run-package-001"
# A generated signing key re-rolls the signature and every digest derived from it, so the exported
# bytes differ on every run. The export scans those bytes for sensitive material, and a scanned
# needle that is also a hexadecimal string -- an acceptance key, say -- then matches inside a
# digest on a random run. Seeding the key keeps the package byte-identical, so any such collision
# is a permanent, reproducible failure rather than a rare one.
SIGNING_SEED = bytes(range(32))
UNTRUSTED_SIGNING_SEED = bytes(range(32, 64))


class Observable:
    def __init__(self, value: ProviderObservation) -> None:
        self.value = value

    def observe(self) -> ProviderObservation:
        return self.value


def _contract() -> IntegrationContract:
    return IntegrationContract.model_validate(
        {
            "contract_id": "contract-package-001",
            "version": 1,
            "source": {
                "connection_handle": "pg-m0",
                "schema": "m0_source",
                "table": "orders",
                "primary_key": "order_id",
            },
            "destination": {
                "connection_handle": "sf-m0",
                "database": "HEINZEL_M0",
                "schema": "PUBLIC",
                "table": "ORDERS",
                "key": "order_id",
            },
            "projection": [item.model_dump() for item in FIXED_PROJECTION],
            "freshness_seconds": 300,
        }
    )


def _columns(destination: bool = False) -> tuple[ColumnObservation, ...]:
    return (
        ColumnObservation(
            name="order_id", type_name="NUMBER(19,0)" if destination else "BIGINT", nullable=False
        ),
        ColumnObservation(
            name="customer_ref",
            type_name="VARCHAR(65535)",
            nullable=False,
        ),
        ColumnObservation(
            name="amount",
            type_name="NUMBER(18,2)" if destination else "NUMERIC(18,2)",
            nullable=False,
        ),
        ColumnObservation(name="currency", type_name="VARCHAR(3)", nullable=False),
        ColumnObservation(
            name="order_status" if destination else "status",
            type_name="VARCHAR(65535)",
            nullable=False,
        ),
        ColumnObservation(
            name="updated_at",
            type_name="TIMESTAMP_TZ(6)" if destination else "TIMESTAMPTZ",
            nullable=False,
        ),
    )


def _ledger_columns() -> tuple[ColumnObservation, ...]:
    return (
        ColumnObservation(name="batch_id", type_name="VARCHAR(16777216)", nullable=False),
        ColumnObservation(name="manifest_digest", type_name="VARCHAR(64)", nullable=False),
        ColumnObservation(name="committed_at", type_name="TIMESTAMP_TZ(9)", nullable=False),
    )


def _observations() -> tuple[ProviderObservation, ProviderObservation]:
    return (
        ProviderObservation(
            provider="postgresql",
            connection_handle="pg-m0",
            object_identity="pg:opaque-source",
            object_kind="base_table",
            schema_digest="1" * 64,
            columns=_columns(),
            key_name="order_id",
            key_type="BIGINT",
            key_nullable=False,
            key_constraint="primary_key",
            stable_key_order=True,
            read_only=True,
            capabilities=("snapshot_read", "stable_primary_key_order", "drift_probe"),
            observed_at=NOW,
            snapshot_semantics="snapshot",
            commit_ledger_object_kind=None,
            commit_ledger_columns=None,
            commit_ledger_key_name=None,
            commit_ledger_key_constraint=None,
            evidence_safe=True,
        ),
        ProviderObservation(
            provider="snowflake",
            connection_handle="sf-m0",
            object_identity="sf:opaque-destination",
            object_kind="base_table",
            schema_digest="2" * 64,
            columns=_columns(destination=True),
            key_name="order_id",
            key_type="NUMBER(19,0)",
            key_nullable=False,
            key_constraint="primary_key",
            stable_key_order=None,
            read_only=None,
            capabilities=("stage_write", "idempotent_merge", "commit_ledger", "visibility_query"),
            observed_at=NOW,
            snapshot_semantics="unknown",
            commit_ledger_object_kind="base_table",
            commit_ledger_columns=_ledger_columns(),
            commit_ledger_key_name="batch_id",
            commit_ledger_key_constraint="primary_key",
            evidence_safe=True,
        ),
    )


def _metadata() -> PackageMetadata:
    return PackageMetadata(
        commit_sha="a" * 40,
        uv_lock_digest="b" * 64,
        python_version="3.13.7",
        mcp_protocol_version="2025-06-18",
        operator_pseudonym="operator-one",
        host_pseudonym="host-one",
        transport_decision="cli-fallback",
        resources=(
            ResourceDisposition(
                resource_digest="c" * 64,
                creation_state="created",
                retention_deadline=NOW + timedelta(days=30),
                cleanup_status="scheduled",
                cleanup_operation="delete_synthetic_rows",
            ),
        ),
        limitations=("local operator can replace a complete package",),
    )


def _save_artifact(store: SQLiteStore, kind: str, value: object) -> str:
    artifact_digest = digest(value)
    store.save_artifact(kind, artifact_digest, canonical_bytes(value))
    return artifact_digest


def complete_store(tmp_path: Path) -> tuple[SQLiteStore, GraphVerifier, ScanInput]:
    source, destination = _observations()
    signer = GraphSigner("package-key", Ed25519PrivateKey.from_private_bytes(SIGNING_SEED))
    store = SQLiteStore.open(tmp_path / "state.sqlite3")
    service = ContractService(
        store=store,
        signer=signer,
        source_resolver=lambda _handle: Observable(source),
        destination_resolver=lambda _handle: Observable(destination),
        clock=lambda: NOW,
    )
    contract = service.create_draft(_contract())
    summary = service.verify(contract.contract_id, contract.version)
    assert isinstance(summary, ActivationSummary)
    run = store.create_run(
        RUN_ID,
        "activation-package-001",
        digest(contract),
        digest(summary),
        summary.signed_graph.model_dump_json(),
        NOW,
    )
    store.set_acceptance_key(run.run_id, 984201)
    lifecycle = (
        ("draft_created", {"contract_digest": digest(contract)}),
        (
            "verification_completed",
            {
                "summary_digest": digest(summary),
                "source_observation_digest": summary.source_observation_digest,
                "destination_observation_digest": summary.destination_observation_digest,
                "iir_digest": summary.iir_digest,
                "physical_plan_digest": summary.physical_plan_digest,
                "legality_decision_digest": summary.legality_decision_digest,
                "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
            },
        ),
        ("legality_admitted", {"legality_decision_digest": summary.legality_decision_digest}),
        (
            "graph_signed",
            {
                "graph_digest": summary.graph_digest,
                "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
                "key_id": summary.signing_key_id,
            },
        ),
        (
            "activation",
            {
                "contract_digest": digest(contract),
                "summary_digest": digest(summary),
                "graph_digest": summary.graph_digest,
            },
        ),
    )
    for event_type, attributes in lifecycle:
        store.append_event(RUN_ID, event_type, NOW, "heinzel-contract-service", attributes)
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="created",
        checkpoint="graph_verified",
        state="running",
        event_type="graph_verified",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"graph_digest": summary.graph_digest, "key_id": "package-key"},
    )
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="graph_verified",
        checkpoint="drift_revalidated",
        state="running",
        event_type="drift_revalidated",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"drift_probe_digest": "3" * 64},
    )
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="drift_revalidated",
        checkpoint="snapshot_opened",
        state="running",
        event_type="snapshot_opened",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={
            "object_identity": "pg:opaque-source",
            "schema_digest": source.schema_digest,
            "snapshot_identity_digest": "5" * 64,
        },
    )
    boundary = SourceBoundary(
        object_identity="pg:opaque-source",
        schema_digest=source.schema_digest,
        snapshot_identity="snapshot-opaque-001",
        key_range_digest="6" * 64,
        row_count=1,
        query_shape_digest="7" * 64,
        opened_at=NOW,
        closed_at=NOW,
    )
    boundary_digest = digest(boundary)
    manifest = SegmentManifest(
        batch_id="batch-package-001",
        segment_digest="8" * 64,
        row_set_digest="9" * 64,
        row_count=1,
        encoded_bytes=128,
        schema_digest=source.schema_digest,
        source_boundary_digest=boundary_digest,
        acceptance_value_digest="d" * 64,
    )
    store.record_extraction(
        RUN_ID,
        source_boundary_digest=boundary_digest,
        source_boundary_payload=canonical_bytes(boundary),
        manifest_digest=digest(manifest),
        manifest_payload=canonical_bytes(manifest),
        segment_path=tmp_path / "private" / "segment.csv",
        batch_id=manifest.batch_id,
        row_count=1,
        encoded_bytes=128,
        occurred_at=NOW,
        producer="heinzel-runtime",
    )
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="extraction_completed",
        checkpoint="manifest_created",
        state="running",
        event_type="manifest_created",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"manifest_digest": digest(manifest)},
    )
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="manifest_created",
        checkpoint="commit_attempted",
        state="running",
        event_type="commit_attempted",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"batch_id": manifest.batch_id, "manifest_digest": digest(manifest)},
    )
    receipt = CommitReceipt(
        batch_id=manifest.batch_id,
        manifest_digest=digest(manifest),
        query_ids=("opaque-query-one",),
        affected_rows=1,
        ledger_identity="e" * 64,
        committed_at=NOW,
    )
    receipt_digest = _save_artifact(store, "commit_receipt", receipt)
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="commit_attempted",
        checkpoint="commit_resolved",
        state="running",
        event_type="commit_resolved",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"receipt_digest": receipt_digest, "replayed": False},
    )
    proof = VisibilityProof(
        batch_id=manifest.batch_id,
        value_digest=manifest.acceptance_value_digest,
        query_id="opaque-visibility-query",
        verified_at=NOW,
    )
    proof_digest = _save_artifact(store, "visibility_proof", proof)
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="commit_resolved",
        checkpoint="visibility_verified",
        state="running",
        event_type="visibility_verified",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={"visibility_digest": proof_digest},
    )
    store.advance_checkpoint_with_event(
        RUN_ID,
        expected_checkpoint="visibility_verified",
        checkpoint="terminal_success",
        state="succeeded",
        event_type="terminal_success",
        occurred_at=NOW,
        producer="heinzel-runtime",
        attributes={
            "manifest_digest": digest(manifest),
            "receipt_digest": receipt_digest,
            "visibility_digest": proof_digest,
        },
    )
    return (
        store,
        GraphVerifier({"package-key": signer.public_key}),
        ScanInput(
            credential_canaries=("credential-never-export",),
            row_value_canaries=("customer-sensitive-17",),
            acceptance_keys=(984201,),
            local_path_prefixes=(str(tmp_path),),
        ),
    )


def _package_files(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _canonical_write(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value))


def _rehash_package(path: Path) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    index["payload_sha256"] = {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
        and item.relative_to(path).as_posix() not in {"package.json", "verification/result.json"}
    }
    _canonical_write(index_path, index)
    result_path = path / "verification/result.json"
    result = json.loads(result_path.read_bytes())
    result["package_index_digest"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    _canonical_write(result_path, result)


def _rechain_trace(path: Path, mutate: Callable[[list[dict[str, object]]], None]) -> None:
    trace_path = path / "trace/events.json"
    events = json.loads(trace_path.read_bytes())
    mutate(events)
    previous: str | None = None
    for event in events:
        event["previous_digest"] = previous
        body = {key: value for key, value in event.items() if key != "event_digest"}
        event["event_digest"] = digest(body)
        previous = event["event_digest"]
    _canonical_write(trace_path, events)
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    index["trace_head_digest"] = previous
    _canonical_write(index_path, index)
    _rehash_package(path)


def test_export_is_byte_deterministic_and_independently_verifiable(tmp_path: Path) -> None:
    store, verifier, scan_input = complete_store(tmp_path)

    first = export_package(store, RUN_ID, tmp_path / "first", _metadata(), scan_input, verifier)
    second = export_package(store, RUN_ID, tmp_path / "second", _metadata(), scan_input, verifier)

    assert first.package_index_digest == second.package_index_digest
    assert _package_files(first.path) == _package_files(second.path)
    assert set(_package_files(first.path)) == {
        "package.json",
        "trace/events.json",
        "verification/result.json",
        "operations/resources.json",
        "operations/limitations.json",
        *{
            f"artifacts/{kind}/{artifact_digest}.json"
            for kind, artifact_digest, _payload in store.list_run_artifacts(RUN_ID)
        },
    }
    index = json.loads((first.path / "package.json").read_bytes())
    relationships = [edge["relationship"] for edge in index["edges"]]
    assert relationships.count("graph_parent") == 6
    assert set(relationships) == {
        "lowered_to",
        "source_observed_as",
        "destination_observed_as",
        "admitted_by",
        "graph_parent",
        "published_as",
        "run_produced_boundary",
        "describes_segment",
        "committed_as",
        "verified_by",
    }
    assert verify_package(first.path, verifier, scan_input) == first


def test_export_carries_no_run_specific_entropy_into_scanned_payloads(tmp_path: Path) -> None:
    first_root = tmp_path / "first-run"
    second_root = tmp_path / "second-run"
    first_root.mkdir()
    second_root.mkdir()
    first_store, first_verifier, first_scan_input = complete_store(first_root)
    second_store, second_verifier, second_scan_input = complete_store(second_root)

    first = export_package(
        first_store, RUN_ID, first_root / "package", _metadata(), first_scan_input, first_verifier
    )
    second = export_package(
        second_store,
        RUN_ID,
        second_root / "package",
        _metadata(),
        second_scan_input,
        second_verifier,
    )

    assert _package_files(first.path) == _package_files(second.path)


def test_scan_failure_names_the_rule_offset_and_payload_that_fired(tmp_path: Path) -> None:
    store, verifier, _scan_input = complete_store(tmp_path)
    canary = "local operator can replace"
    offset = canonical_bytes(_metadata().limitations).index(canary.encode("utf-8"))

    with pytest.raises(PackageError) as failure:
        export_package(
            store,
            RUN_ID,
            tmp_path / "package",
            _metadata(),
            ScanInput(credential_canaries=(canary,)),
            verifier,
        )

    assert str(failure.value) == (
        "sensitive material scan failed: rule credential_canary_exact "
        f"at byte {offset} of operations/limitations.json"
    )


@pytest.mark.parametrize("artifact_shaped", (False, True))
def test_scan_failure_never_echoes_a_canary_carried_by_a_package_payload_name(
    tmp_path: Path,
    artifact_shaped: bool,
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    canary = "secret_canary" if artifact_shaped else scan_input.credential_canaries[0]
    scan_input = ScanInput(credential_canaries=(canary,))
    relative_path = f"artifacts/{canary}/{'a' * 64}.json" if artifact_shaped else f"{canary}.json"
    payload_path = package.path / relative_path
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(f'["{canary}"]'.encode())
    _rehash_package(package.path)

    with pytest.raises(PackageError) as failure:
        verify_package(package.path, verifier, scan_input)

    assert "credential_canary_exact" in str(failure.value)
    assert canary not in str(failure.value)


def _remove_artifact(path: Path) -> None:
    next((path / "artifacts").rglob("*.json")).unlink()


def _modify_artifact(path: Path) -> None:
    artifact = next((path / "artifacts").rglob("*.json"))
    artifact.write_bytes(artifact.read_bytes() + b" ")
    _rehash_package(path)


def _replace_governed_artifact(path: Path, artifact: str) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    entries = [item for item in index["artifacts"] if item["kind"] == artifact]
    entry = entries[0]
    artifact_path = path / entry["relative_path"]
    value = json.loads(artifact_path.read_bytes())
    if artifact == "integration_contract":
        value["contract_id"] = "contract-package-tampered"
    elif artifact == "signed_execution_graph":
        value["graph"]["max_rows"] = 9_999
    else:  # pragma: no cover - the parametrized caller owns the vocabulary
        raise AssertionError(artifact)
    _replace_indexed_artifact(path, index, entry, artifact_path, value)


def _replace_provider_observation(path: Path, provider: str) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    entries = [item for item in index["artifacts"] if item["kind"] == "provider_observation"]
    entry = next(
        item
        for item in entries
        if json.loads((path / item["relative_path"]).read_bytes())["provider"] == provider
    )
    artifact_path = path / entry["relative_path"]
    value = json.loads(artifact_path.read_bytes())
    value["object_identity"] = f"{provider}:tampered-object"
    _replace_indexed_artifact(path, index, entry, artifact_path, value)


def _replace_indexed_artifact(
    path: Path,
    index: dict[str, Any],
    entry: dict[str, Any],
    artifact_path: Path,
    value: object,
) -> None:
    payload = canonical_bytes(value)
    replacement_digest = hashlib.sha256(payload).hexdigest()
    kind = entry["kind"]
    replacement_path = f"artifacts/{kind}/{replacement_digest}.json"
    original_digest = entry["digest"]
    artifact_path.unlink()
    _canonical_write(path / replacement_path, value)
    entry["digest"] = replacement_digest
    entry["relative_path"] = replacement_path
    entry["sha256"] = replacement_digest
    edges = index["edges"]
    assert isinstance(edges, list)
    for edge in edges:
        assert isinstance(edge, dict)
        if edge["parent_digest"] == original_digest:
            edge["parent_digest"] = replacement_digest
        if edge["child_digest"] == original_digest:
            edge["child_digest"] = replacement_digest
    _canonical_write(path / "package.json", index)
    _rehash_package(path)


def _swap_artifacts(path: Path) -> None:
    first, second = list((path / "artifacts").rglob("*.json"))[:2]
    first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_bytes)
    second.write_bytes(first_bytes)
    _rehash_package(path)


def _orphan_artifact(path: Path) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    duplicate = dict(index["artifacts"][0])
    duplicate["digest"] = "f" * 64
    duplicate["relative_path"] = f"artifacts/{duplicate['kind']}/{'f' * 64}.json"
    orphan_path = path / duplicate["relative_path"]
    orphan_path.write_bytes(b"{}")
    duplicate["sha256"] = hashlib.sha256(b"{}").hexdigest()
    index["artifacts"].append(duplicate)
    _canonical_write(index_path, index)
    _rehash_package(path)


def _unknown_kind(path: Path) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    index["artifacts"][0]["kind"] = "private_state"
    _canonical_write(index_path, index)
    _rehash_package(path)


def _v1_manifest(path: Path) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    entry = next(item for item in index["artifacts"] if item["kind"] == "segment_manifest")
    artifact = path / entry["relative_path"]
    value = json.loads(artifact.read_bytes())
    value["schema_version"] = "1"
    payload = canonical_bytes(value)
    replacement_digest = hashlib.sha256(payload).hexdigest()
    replacement_path = f"artifacts/segment_manifest/{replacement_digest}.json"
    artifact.unlink()
    _canonical_write(path / replacement_path, value)
    original_digest = entry["digest"]
    entry["digest"] = replacement_digest
    entry["relative_path"] = replacement_path
    entry["sha256"] = replacement_digest
    for edge in index["edges"]:
        if edge["parent_digest"] == original_digest:
            edge["parent_digest"] = replacement_digest
        if edge["child_digest"] == original_digest:
            edge["child_digest"] = replacement_digest
    _canonical_write(index_path, index)
    _rehash_package(path)


def _v1_boundary(path: Path) -> None:
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    entry = next(item for item in index["artifacts"] if item["kind"] == "source_boundary")
    artifact = path / entry["relative_path"]
    value = json.loads(artifact.read_bytes())
    value["schema_version"] = "1"
    value["key_min"] = 1
    value["key_max"] = 2
    value.pop("key_range_digest")
    payload = canonical_bytes(value)
    replacement_digest = hashlib.sha256(payload).hexdigest()
    replacement_path = f"artifacts/source_boundary/{replacement_digest}.json"
    artifact.unlink()
    _canonical_write(path / replacement_path, value)
    original_digest = entry["digest"]
    entry["digest"] = replacement_digest
    entry["relative_path"] = replacement_path
    entry["sha256"] = replacement_digest
    for edge in index["edges"]:
        if edge["parent_digest"] == original_digest:
            edge["parent_digest"] = replacement_digest
        if edge["child_digest"] == original_digest:
            edge["child_digest"] = replacement_digest
    _canonical_write(index_path, index)
    _rehash_package(path)


def _broken_event_chain(path: Path) -> None:
    trace = path / "trace/events.json"
    events = json.loads(trace.read_bytes())
    events[1]["previous_digest"] = "f" * 64
    _canonical_write(trace, events)
    _rehash_package(path)


def _missing_terminal_proof(path: Path) -> None:
    trace = path / "trace/events.json"
    events = json.loads(trace.read_bytes())
    events.pop()
    _canonical_write(trace, events)
    index_path = path / "package.json"
    index = json.loads(index_path.read_bytes())
    index["trace_head_digest"] = events[-1]["event_digest"]
    _canonical_write(index_path, index)
    _rehash_package(path)


def _inject_payload(path: Path, payload: bytes) -> None:
    limitations = path / "operations/limitations.json"
    limitations.write_bytes(payload)
    _rehash_package(path)


@pytest.mark.parametrize(
    "mutation",
    [
        _remove_artifact,
        _modify_artifact,
        _swap_artifacts,
        _orphan_artifact,
        _unknown_kind,
        _v1_manifest,
        _v1_boundary,
        _broken_event_chain,
        _missing_terminal_proof,
        lambda path: _inject_payload(path, b'["/Users/operator/private/segment.csv"]'),
        lambda path: _inject_payload(path, b'["984201"]'),
        lambda path: _inject_payload(path, b'["credential-never-export"]'),
        lambda path: _inject_payload(path, b'["Y3JlZGVudGlhbC1uZXZlci1leHBvcnQ="]'),
        lambda path: _inject_payload(path, b'["postgresql://user:pass@db.invalid/m0"]'),
        lambda path: _inject_payload(path, b'["-----BEGIN PRIVATE KEY-----"]'),
    ],
    ids=[
        "missing",
        "modified",
        "swapped",
        "orphaned",
        "unknown-kind",
        "v1-manifest",
        "v1-boundary",
        "broken-event-chain",
        "missing-terminal-proof",
        "local-path",
        "raw-key",
        "exact-canary",
        "encoded-canary",
        "connection-string",
        "private-key-marker",
    ],
)
def test_verifier_fails_closed_for_package_mutations(
    tmp_path: Path, mutation: Callable[[Path], None]
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    mutation(package.path)

    with pytest.raises(PackageError):
        verify_package(package.path, verifier, scan_input)


@pytest.mark.parametrize(
    ("artifact", "mutation"),
    [
        (
            "integration_contract",
            lambda path: _replace_governed_artifact(path, "integration_contract"),
        ),
        (
            "source_provider_observation",
            lambda path: _replace_provider_observation(path, "postgresql"),
        ),
        (
            "destination_provider_observation",
            lambda path: _replace_provider_observation(path, "snowflake"),
        ),
        (
            "signed_execution_graph",
            lambda path: _replace_governed_artifact(path, "signed_execution_graph"),
        ),
    ],
)
def test_verifier_rejects_rehashed_governed_artifact_tampering(
    tmp_path: Path, artifact: str, mutation: Callable[[Path], None]
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    mutation(package.path)

    with pytest.raises(PackageError, match=r"artifact|graph"):
        verify_package(package.path, verifier, scan_input)


def test_export_failure_does_not_touch_existing_destination_or_leave_temporary_sibling(
    tmp_path: Path,
) -> None:
    store, _verifier, scan_input = complete_store(tmp_path)
    destination = tmp_path / "completed"
    destination.mkdir()
    marker = destination / "existing.txt"
    marker.write_text("keep", encoding="utf-8")
    store.append_event(
        RUN_ID,
        "diagnostic",
        NOW,
        "test",
        {"unsafe": "credential-never-export"},
    )

    with pytest.raises(PackageError):
        export_package(store, RUN_ID, destination, _metadata(), scan_input, _verifier)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".completed.tmp-*"))


def test_export_rejects_privacy_incompatible_source_boundary_v1(tmp_path: Path) -> None:
    store, _verifier, scan_input = complete_store(tmp_path)
    boundary = next(
        item for item in store.list_run_artifacts(RUN_ID) if item[0] == "source_boundary"
    )
    v1_payload = canonical_bytes(
        {
            "schema_version": "1",
            "object_identity": "pg:opaque-source",
            "schema_digest": "1" * 64,
            "snapshot_identity": "snapshot-opaque-001",
            "key_min": 1,
            "key_max": 984201,
            "row_count": 1,
            "query_shape_digest": "7" * 64,
            "opened_at": NOW,
            "closed_at": NOW,
        }
    )
    store._connection.execute(
        "UPDATE artifacts SET payload = ? WHERE kind = ? AND digest = ?",
        (v1_payload, boundary[0], boundary[1]),
    )

    with pytest.raises(PackageError):
        export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, _verifier)


def test_verifier_rejects_rechained_trace_with_wrong_artifact_link(tmp_path: Path) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)

    def break_boundary_link(events: list[dict[str, object]]) -> None:
        extraction = next(
            event for event in events if event["event_type"] == "extraction_completed"
        )
        attributes = extraction["attributes"]
        assert isinstance(attributes, dict)
        attributes["source_boundary_digest"] = "f" * 64

    _rechain_trace(package.path, break_boundary_link)

    with pytest.raises(PackageError):
        verify_package(package.path, verifier, scan_input)


def test_verifier_rejects_unknown_resource_metadata_fields(tmp_path: Path) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    resources = package.path / "operations/resources.json"
    _canonical_write(resources, [{"unexpected": "safe-but-unstructured"}])
    _rehash_package(package.path)

    with pytest.raises(PackageError):
        verify_package(package.path, verifier, scan_input)


def test_verifier_rejects_qualified_commit_ledger_identity(tmp_path: Path) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    index_path = package.path / "package.json"
    index = json.loads(index_path.read_bytes())
    entry = next(item for item in index["artifacts"] if item["kind"] == "commit_receipt")
    artifact = package.path / entry["relative_path"]
    value = json.loads(artifact.read_bytes())
    value["ledger_identity"] = "HEINZEL_M0.PUBLIC.COMMIT_LEDGER"
    payload = canonical_bytes(value)
    replacement_digest = hashlib.sha256(payload).hexdigest()
    replacement_path = f"artifacts/commit_receipt/{replacement_digest}.json"
    artifact.unlink()
    _canonical_write(package.path / replacement_path, value)
    original_digest = entry["digest"]
    entry["digest"] = replacement_digest
    entry["relative_path"] = replacement_path
    entry["sha256"] = replacement_digest
    for edge in index["edges"]:
        if edge["parent_digest"] == original_digest:
            edge["parent_digest"] = replacement_digest
        if edge["child_digest"] == original_digest:
            edge["child_digest"] = replacement_digest
    _canonical_write(index_path, index)

    def update_receipt_links(events: list[dict[str, object]]) -> None:
        for event in events:
            if event["event_type"] not in {"commit_resolved", "terminal_success"}:
                continue
            attributes = event["attributes"]
            assert isinstance(attributes, dict)
            attributes["receipt_digest"] = replacement_digest

    _rechain_trace(package.path, update_receipt_links)

    with pytest.raises(PackageError):
        verify_package(package.path, verifier, scan_input)


def test_export_rejects_wrong_signing_key_without_leaving_output(tmp_path: Path) -> None:
    store, _verifier, scan_input = complete_store(tmp_path)
    destination = tmp_path / "package"
    wrong_signer = GraphSigner(
        "package-key", Ed25519PrivateKey.from_private_bytes(UNTRUSTED_SIGNING_SEED)
    )
    wrong_verifier = GraphVerifier({"package-key": wrong_signer.public_key})

    with pytest.raises(PackageError):
        export_package(
            store,
            RUN_ID,
            destination,
            _metadata(),
            scan_input,
            wrong_verifier,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".package.tmp-*"))


def test_export_cleans_temporary_package_after_filesystem_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    destination = tmp_path / "package"
    original_write = Path.write_bytes

    def fail_resources_write(path: Path, payload: bytes) -> int:
        if path.name == "resources.json":
            raise OSError("injected package write failure")
        return original_write(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_resources_write)

    with pytest.raises(PackageError):
        export_package(store, RUN_ID, destination, _metadata(), scan_input, verifier)

    assert not destination.exists()
    assert not list(tmp_path.glob(".package.tmp-*"))


@pytest.mark.parametrize("invalid_verifier", [None, object()])
def test_export_rejects_non_graph_verifier_before_creating_output(
    tmp_path: Path, invalid_verifier: object
) -> None:
    store, _verifier, scan_input = complete_store(tmp_path)
    destination = tmp_path / "package"

    with pytest.raises(PackageError, match="graph verifier"):
        export_package(
            store,
            RUN_ID,
            destination,
            _metadata(),
            scan_input,
            invalid_verifier,  # type: ignore[arg-type]
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".package.tmp-*"))


@pytest.mark.parametrize("invalid_verifier", [None, object()])
def test_verify_rejects_non_graph_verifier_at_entry(
    tmp_path: Path, invalid_verifier: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "must-not-be-read"

    def reject_path_access(_path: Path) -> bool:
        raise AssertionError("package path accessed before verifier validation")

    monkeypatch.setattr(Path, "is_dir", reject_path_access)

    with pytest.raises(PackageError, match="graph verifier"):
        verify_package(
            path,
            invalid_verifier,  # type: ignore[arg-type]
            ScanInput(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit_sha", ""),
        ("uv_lock_digest", "not-a-digest"),
        ("python_version", "3.12.0"),
        ("mcp_protocol_version", ""),
        ("operator_pseudonym", ""),
        ("host_pseudonym", ""),
    ],
)
def test_package_index_reuses_strict_metadata_constraints(
    tmp_path: Path, field: str, value: str
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    package = export_package(store, RUN_ID, tmp_path / "package", _metadata(), scan_input, verifier)
    index_path = package.path / "package.json"
    index = json.loads(index_path.read_bytes())
    index[field] = value
    _canonical_write(index_path, index)
    _rehash_package(package.path)

    with pytest.raises(PackageError):
        verify_package(package.path, verifier, scan_input)


def test_atomic_rename_is_the_exporters_final_filesystem_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, verifier, scan_input = complete_store(tmp_path)
    destination = tmp_path / "package"
    original_rename = Path.rename
    original_rglob = Path.rglob
    renamed = False

    def observed_rename(path: Path, target: Path) -> Path:
        nonlocal renamed
        result = original_rename(path, target)
        renamed = True
        return result

    def reject_post_rename_read(path: Path, pattern: str):
        if renamed:
            raise AssertionError("filesystem accessed after atomic rename")
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rename", observed_rename)
    monkeypatch.setattr(Path, "rglob", reject_post_rename_read)

    result = export_package(store, RUN_ID, destination, _metadata(), scan_input, verifier)

    assert result.path == destination
    assert destination.is_dir()
