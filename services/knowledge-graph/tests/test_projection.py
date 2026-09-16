from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pillarmesh_contract_model import ArtifactReference, canonical_bytes
from pillarmesh_knowledge_graph import (
    ContextEdge,
    ContextGraphProjector,
    ContextGraphRepository,
    ContextNode,
    GraphProjectionError,
    SourceRecordObservation,
)
from pydantic import ValidationError

_OBSERVED_AT = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _reference(artifact_id: str, digest_character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=digest_character * 64)


def _provenance(
    tenant_id: str,
    record_id: str,
    *,
    digest_character: str,
    validity: str = "valid",
) -> SourceRecordObservation:
    return SourceRecordObservation.model_validate(
        {
            "tenant_id": tenant_id,
            "source_record_ref": _reference(record_id, digest_character),
            "producer": "semantic-registry",
            "observed_at": _OBSERVED_AT,
            "validity": validity,
        }
    )


def _nodes(tenant_id: str = "tenant-a") -> tuple[ContextNode, ContextNode]:
    return (
        ContextNode(
            tenant_id=tenant_id,
            node_id="metric:revenue:v1",
            node_kind="metric_version",
            owner_ref="principal:finance",
            provenance=_provenance(
                tenant_id,
                "semantic-version:revenue:v1",
                digest_character="a",
            ),
        ),
        ContextNode(
            tenant_id=tenant_id,
            node_id="dashboard:revenue:v1",
            node_kind="dashboard",
            owner_ref="principal:analytics",
            provenance=_provenance(
                tenant_id,
                "dashboard-contract:revenue:v1",
                digest_character="b",
            ),
        ),
    )


def _edge(tenant_id: str = "tenant-a") -> ContextEdge:
    return ContextEdge(
        tenant_id=tenant_id,
        edge_id="metric:revenue:v1->dashboard:revenue:v1",
        source_node_id="metric:revenue:v1",
        target_node_id="dashboard:revenue:v1",
        relationship="consumed_by",
        evidence_kind="validated",
        confidence=Decimal("1"),
        evidence_refs=(_reference("dashboard-contract:revenue:v1", "b"),),
        provenance=_provenance(
            tenant_id,
            "dashboard-contract:revenue:v1",
            digest_character="b",
        ),
    )


def test_projection_rebuilds_byte_identically_after_store_deletion(tmp_path: Path) -> None:
    projector = ContextGraphProjector()
    nodes = _nodes()
    edge = _edge()
    repository = ContextGraphRepository(tmp_path / "context-graph.sqlite")

    first = projector.rebuild(tenant_id="tenant-a", nodes=nodes, edges=(edge,))
    repository.replace(first)
    repository.delete("tenant-a")
    second = projector.rebuild(
        tenant_id="tenant-a",
        nodes=tuple(reversed(nodes)),
        edges=(edge,),
    )
    repository.replace(second)

    assert canonical_bytes(first) == canonical_bytes(second)
    assert first.graph_snapshot_digest == second.graph_snapshot_digest
    assert repository.load("tenant-a") == first
    assert first.nodes[0].node_id == "dashboard:revenue:v1"
    assert first.edges[0].provenance.source_record_ref.digest == "b" * 64


@pytest.mark.parametrize(
    ("nodes", "edges", "message"),
    (
        (
            (*_nodes(), _nodes()[0]),
            (_edge(),),
            "duplicate node identity",
        ),
        (
            _nodes(),
            (_edge().model_copy(update={"target_node_id": "dashboard:missing:v1"}),),
            "dangling edge",
        ),
        (
            _nodes("tenant-b"),
            (_edge(),),
            "tenant mismatch",
        ),
        (
            _nodes(),
            (_edge(), _edge().model_copy(update={"relationship": "depends_on"})),
            "duplicate edge identity",
        ),
    ),
)
def test_projection_rejects_invalid_or_conflicting_inputs(
    nodes: tuple[ContextNode, ...],
    edges: tuple[ContextEdge, ...],
    message: str,
) -> None:
    with pytest.raises(GraphProjectionError, match=message):
        ContextGraphProjector().rebuild(tenant_id="tenant-a", nodes=nodes, edges=edges)


def test_projection_contracts_are_strict_immutable_and_require_utc_provenance() -> None:
    provenance = _provenance("tenant-a", "semantic-version:revenue:v1", digest_character="a")

    with pytest.raises(ValidationError):
        provenance.validity = "superseded"
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        SourceRecordObservation.model_validate(
            {
                **provenance.model_dump(mode="python"),
                "observed_at": datetime(2026, 9, 12, 12),
            }
        )
    with pytest.raises(ValidationError):
        ContextEdge.model_validate(
            {
                **_edge().model_dump(mode="python"),
                "confidence": "1",
                "unexpected": True,
            }
        )
