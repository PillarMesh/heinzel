from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from heinzel_contract_model import ArtifactReference, canonical_bytes
from heinzel_knowledge_graph import (
    ApprovalRequirement,
    ContextEdge,
    ContextGraphProjector,
    ContextGraphSnapshot,
    ContextNode,
    ImpactAnalysisError,
    ImpactAnalyzer,
    SourceRecordObservation,
    add_approval_requirements,
)
from heinzel_knowledge_graph.impact import ImpactSubjectKind

_NOW = datetime(2026, 9, 12, 18, tzinfo=UTC)


def _reference(artifact_id: str, character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=character * 64)


def _provenance(record_id: str, character: str) -> SourceRecordObservation:
    return SourceRecordObservation(
        tenant_id="tenant-a",
        source_record_ref=_reference(record_id, character),
        producer="fixture-authority",
        observed_at=_NOW,
        validity="valid",
    )


def _node(node_id: str, node_kind: str, owner_ref: str, character: str) -> ContextNode:
    return ContextNode.model_validate(
        {
            "tenant_id": "tenant-a",
            "node_id": node_id,
            "node_kind": node_kind,
            "owner_ref": owner_ref,
            "provenance": _provenance(f"record:{node_id}", character),
        }
    )


def _edge(
    edge_id: str,
    source_node_id: str,
    target_node_id: str,
    evidence_kind: str,
    character: str,
    relationship: str = "consumed_by",
) -> ContextEdge:
    return ContextEdge.model_validate(
        {
            "tenant_id": "tenant-a",
            "edge_id": edge_id,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "relationship": relationship,
            "evidence_kind": evidence_kind,
            "confidence": Decimal("1") if evidence_kind == "validated" else Decimal("0.6"),
            "evidence_refs": (_reference(f"evidence:{edge_id}", character),),
            "provenance": _provenance(f"edge-record:{edge_id}", character),
        }
    )


def _snapshot() -> ContextGraphSnapshot:
    nodes = (
        _node("metric:revenue:v2", "metric_version", "principal:finance", "a"),
        _node("dashboard:revenue", "dashboard", "principal:analytics", "b"),
        _node("report:revenue", "report", "principal:reporting", "c"),
        _node("answer-policy:finance", "answer_scope_policy", "principal:policy", "d"),
        _node("principal:requester", "principal", "principal:identity-owner", "5"),
    )
    edges = (
        _edge(
            "metric-to-dashboard",
            "metric:revenue:v2",
            "dashboard:revenue",
            "validated",
            "e",
        ),
        _edge(
            "dashboard-to-policy",
            "dashboard:revenue",
            "answer-policy:finance",
            "validated",
            "f",
        ),
        _edge(
            "metric-to-report",
            "metric:revenue:v2",
            "report:revenue",
            "inferred",
            "1",
        ),
    )
    return ContextGraphProjector().rebuild(tenant_id="tenant-a", nodes=nodes, edges=edges)


@pytest.mark.parametrize(
    ("subject_kind", "node_kind", "target_kind", "relationship"),
    (
        ("source_drift", "source_object", "source_contract", "governs"),
        ("metric_version_change", "metric_version", "dashboard", "consumed_by"),
        ("contract_supersession", "source_contract", "raw_generation", "produces"),
        ("generation_failure", "raw_generation", "data_product", "materializes"),
        ("policy_change", "answer_scope_policy", "governed_answer", "governs"),
        ("grant_change", "access_grant", "principal", "grants"),
        ("retirement", "dashboard", "report", "produces"),
    ),
)
def test_every_subject_kind_traverses_from_a_compatible_subject(
    subject_kind: ImpactSubjectKind,
    node_kind: str,
    target_kind: str,
    relationship: str,
) -> None:
    subject_ref = f"subject:{subject_kind}"
    target_ref = f"impact:{subject_kind}"
    snapshot = ContextGraphProjector().rebuild(
        tenant_id="tenant-a",
        nodes=(
            _node(subject_ref, node_kind, "principal:source-owner", "2"),
            _node(target_ref, target_kind, "principal:impact-owner", "3"),
        ),
        edges=(
            _edge(
                f"edge:{subject_kind}",
                subject_ref,
                target_ref,
                "validated",
                "4",
                relationship,
            ),
        ),
    )

    analysis = ImpactAnalyzer().analyze(
        snapshot=snapshot,
        analysis_id=f"analysis:{subject_kind}",
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        created_at=_NOW,
    )

    assert tuple(impact.node_id for impact in analysis.validated_impacts) == (target_ref,)
    assert analysis.inferred_impacts == ()
    assert analysis.affected_owner_refs == ("principal:impact-owner",)
    assert analysis.derived_approval_requirements[0].authority_ref == "principal:impact-owner"
    assert analysis.graph_snapshot_digest == snapshot.graph_snapshot_digest


@pytest.mark.parametrize(
    ("subject_kind", "subject_ref"),
    (
        ("source_drift", "metric:revenue:v2"),
        ("metric_version_change", "dashboard:revenue"),
        ("contract_supersession", "report:revenue"),
        ("generation_failure", "answer-policy:finance"),
        ("policy_change", "metric:revenue:v2"),
        ("grant_change", "dashboard:revenue"),
        ("retirement", "principal:requester"),
    ),
)
def test_analysis_rejects_a_change_kind_incompatible_with_its_subject_node(
    subject_kind: ImpactSubjectKind,
    subject_ref: str,
) -> None:
    with pytest.raises(ImpactAnalysisError, match="incompatible"):
        ImpactAnalyzer().analyze(
            snapshot=_snapshot(),
            analysis_id=f"analysis:incompatible:{subject_kind}",
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            created_at=_NOW,
        )


def test_visibility_filters_only_presentation_and_never_authority_requirements() -> None:
    snapshot = _snapshot()
    analyzer = ImpactAnalyzer()

    complete = analyzer.analyze(
        snapshot=snapshot,
        analysis_id="analysis:complete",
        subject_kind="metric_version_change",
        subject_ref="metric:revenue:v2",
        created_at=_NOW,
    )
    restricted = analyzer.analyze(
        snapshot=snapshot,
        analysis_id="analysis:restricted",
        subject_kind="metric_version_change",
        subject_ref="metric:revenue:v2",
        created_at=_NOW,
        can_view=lambda node: node.node_id == "report:revenue",
    )

    assert len(restricted.validated_impacts) < len(complete.validated_impacts)
    assert restricted.inferred_impacts == complete.inferred_impacts
    assert restricted.affected_owner_refs == ("principal:reporting",)
    assert canonical_bytes(restricted.derived_approval_requirements) == canonical_bytes(
        complete.derived_approval_requirements
    )


def test_approval_requirements_are_additive_and_inferred_edges_never_add_one() -> None:
    analysis = ImpactAnalyzer().analyze(
        snapshot=_snapshot(),
        analysis_id="analysis:additive",
        subject_kind="metric_version_change",
        subject_ref="metric:revenue:v2",
        created_at=_NOW,
    )
    owning = (
        ApprovalRequirement(
            authority_ref="principal:data-owner",
            reason_code="owning_service_policy",
            subject_ref="metric:revenue:v2",
        ),
    )

    combined = add_approval_requirements(owning, analysis.derived_approval_requirements)

    assert owning[0] in combined
    assert all(item.authority_ref != "principal:reporting" for item in combined)
    assert tuple(item.authority_ref for item in combined) == (
        "principal:analytics",
        "principal:data-owner",
        "principal:policy",
    )


def test_analysis_rejects_a_subject_absent_from_the_bound_snapshot() -> None:
    with pytest.raises(ImpactAnalysisError, match="absent"):
        ImpactAnalyzer().analyze(
            snapshot=_snapshot(),
            analysis_id="analysis:missing",
            subject_kind="retirement",
            subject_ref="metric:missing",
            created_at=_NOW,
        )
