from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from heinzel_contract_model import ArtifactModel, ArtifactReference, canonical_bytes, digest
from pydantic import ConfigDict, Field, field_validator, model_validator

type ContextNodeKind = Literal[
    "source_object",
    "source_contract",
    "acquisition",
    "raw_generation",
    "canonical_entity",
    "transformation_model",
    "data_product",
    "metric_version",
    "consumption_object",
    "dashboard",
    "report",
    "answer_scope_policy",
    "scout",
    "governed_answer",
    "requester",
    "delegated_agent_session",
    "access_grant",
    "principal",
    "open_request",
    "dependency",
]
type ContextRelationship = Literal[
    "governs",
    "produces",
    "materializes",
    "defines",
    "consumed_by",
    "delivers",
    "grants",
    "assigned_to",
    "depends_on",
    "supersedes",
]
type EvidenceKind = Literal["validated", "inferred"]
type SourceValidity = Literal["valid", "invalid", "expired", "superseded", "retired"]


class _GraphModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GraphProjectionError(RuntimeError):
    pass


class SourceRecordObservation(_GraphModel):
    # The owning-service adapter supplies this observation time. The projector never samples a
    # clock, so rebuilding from the same authoritative records remains byte-identical.
    tenant_id: str = Field(min_length=1)
    source_record_ref: ArtifactReference
    producer: str = Field(min_length=1)
    observed_at: datetime
    validity: SourceValidity

    @field_validator("observed_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class ContextNode(_GraphModel):
    tenant_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    node_kind: ContextNodeKind
    owner_ref: str = Field(min_length=1)
    provenance: SourceRecordObservation

    @model_validator(mode="after")
    def provenance_belongs_to_node_tenant(self) -> Self:
        if self.provenance.tenant_id != self.tenant_id:
            raise ValueError("node provenance tenant mismatch")
        return self


class ContextEdge(_GraphModel):
    tenant_id: str = Field(min_length=1)
    edge_id: str = Field(min_length=1)
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    relationship: ContextRelationship
    evidence_kind: EvidenceKind
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    evidence_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    provenance: SourceRecordObservation

    @model_validator(mode="after")
    def provenance_belongs_to_edge_tenant(self) -> Self:
        if self.provenance.tenant_id != self.tenant_id:
            raise ValueError("edge provenance tenant mismatch")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("edge evidence references must not contain duplicates")
        return self


class ContextGraphSnapshot(_GraphModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    nodes: tuple[ContextNode, ...]
    edges: tuple[ContextEdge, ...]

    @model_validator(mode="after")
    def contains_one_canonical_tenant_graph(self) -> Self:
        node_ids = tuple(node.node_id for node in self.nodes)
        edge_ids = tuple(edge.edge_id for edge in self.edges)
        if node_ids != tuple(sorted(node_ids)) or len(node_ids) != len(set(node_ids)):
            raise ValueError("snapshot nodes must have unique canonical identities")
        if edge_ids != tuple(sorted(edge_ids)) or len(edge_ids) != len(set(edge_ids)):
            raise ValueError("snapshot edges must have unique canonical identities")
        if any(node.tenant_id != self.tenant_id for node in self.nodes) or any(
            edge.tenant_id != self.tenant_id for edge in self.edges
        ):
            raise ValueError("snapshot content tenant mismatch")
        node_id_set = set(node_ids)
        if any(
            edge.source_node_id not in node_id_set or edge.target_node_id not in node_id_set
            for edge in self.edges
        ):
            raise ValueError("snapshot contains a dangling edge")
        for edge in self.edges:
            canonical_evidence = tuple(
                sorted(
                    edge.evidence_refs,
                    key=lambda item: (item.artifact_id, item.version, item.digest),
                )
            )
            if edge.evidence_refs != canonical_evidence:
                raise ValueError("snapshot edge evidence must use canonical order")
        return self

    @property
    def graph_snapshot_digest(self) -> str:
        return digest(self)


class ContextGraphProjector:
    def rebuild(
        self,
        *,
        tenant_id: str,
        nodes: Iterable[ContextNode],
        edges: Iterable[ContextEdge],
    ) -> ContextGraphSnapshot:
        projected_nodes = tuple(nodes)
        projected_edges = tuple(edges)
        if any(node.tenant_id != tenant_id for node in projected_nodes) or any(
            edge.tenant_id != tenant_id for edge in projected_edges
        ):
            raise GraphProjectionError("tenant mismatch in graph projection input")

        nodes_by_id: dict[str, ContextNode] = {}
        for node in projected_nodes:
            if node.node_id in nodes_by_id:
                raise GraphProjectionError(f"duplicate node identity: {node.node_id}")
            nodes_by_id[node.node_id] = node

        edges_by_id: dict[str, ContextEdge] = {}
        for edge in projected_edges:
            if edge.edge_id in edges_by_id:
                raise GraphProjectionError(f"duplicate edge identity: {edge.edge_id}")
            if edge.source_node_id not in nodes_by_id or edge.target_node_id not in nodes_by_id:
                raise GraphProjectionError(f"dangling edge: {edge.edge_id}")
            edges_by_id[edge.edge_id] = edge.model_copy(
                update={
                    "evidence_refs": tuple(
                        sorted(
                            edge.evidence_refs,
                            key=lambda item: (item.artifact_id, item.version, item.digest),
                        )
                    )
                }
            )

        return ContextGraphSnapshot(
            tenant_id=tenant_id,
            nodes=tuple(nodes_by_id[node_id] for node_id in sorted(nodes_by_id)),
            edges=tuple(edges_by_id[edge_id] for edge_id in sorted(edges_by_id)),
        )


class ContextGraphRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS context_graph_snapshots ("
                "tenant_id TEXT PRIMARY KEY, snapshot_digest TEXT NOT NULL, payload BLOB NOT NULL)"
            )

    def replace(self, snapshot: ContextGraphSnapshot) -> None:
        payload = canonical_bytes(snapshot)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO context_graph_snapshots (tenant_id, snapshot_digest, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "snapshot_digest = excluded.snapshot_digest, payload = excluded.payload",
                (snapshot.tenant_id, snapshot.graph_snapshot_digest, payload),
            )

    def load(self, tenant_id: str) -> ContextGraphSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_digest, payload FROM context_graph_snapshots WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = ContextGraphSnapshot.model_validate_json(bytes(row[1]), strict=True)
        except ValueError as error:
            raise GraphProjectionError("stored graph snapshot is malformed") from error
        if snapshot.tenant_id != tenant_id or snapshot.graph_snapshot_digest != row[0]:
            raise GraphProjectionError("stored graph snapshot failed integrity validation")
        return snapshot

    def delete(self, tenant_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM context_graph_snapshots WHERE tenant_id = ?",
                (tenant_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path)
