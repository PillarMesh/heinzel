from __future__ import annotations

import runpy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from heinzel_contract_model import ArtifactReference, ImpactSubject, digest
from heinzel_knowledge_graph import (
    ContextEdge,
    ContextGraphProjector,
    ContextGraphRepository,
    ContextNode,
    ImpactAnalyzer,
    SourceRecordObservation,
)
from heinzel_request_management import (
    FulfillmentImpactBindingReader,
    FulfillmentProposal,
    FulfillmentService,
    GraphImpactAdmissionResolver,
    ImpactAdmissionResolutionError,
    RequestManagementService,
)

_NOW = datetime(2026, 9, 12, 22, tzinfo=UTC)
_SUPPORT = runpy.run_path(str(Path(__file__).with_name("test_answer_fulfillment.py")))
service = _SUPPORT["service"]
submit_and_clarify = _SUPPORT["submit_and_clarify"]


def _reference(artifact_id: str, version: int, character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest=character * 64)


def _observation(reference: ArtifactReference) -> SourceRecordObservation:
    return SourceRecordObservation(
        tenant_id="tenant-a",
        source_record_ref=reference,
        producer="semantic-registry",
        observed_at=_NOW,
        validity="valid",
    )


def _proposal() -> FulfillmentProposal:
    # `service` is loaded through `runpy`, so its own annotations do not survive.
    fulfillment: FulfillmentService
    requests: RequestManagementService
    fulfillment, requests, _repository = service()
    investigating, _ = submit_and_clarify(fulfillment, requests)
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    if not isinstance(proposal, FulfillmentProposal):
        raise AssertionError("the seeded answer request did not produce a proposal")
    return proposal


class MetricSubjectResolver:
    def resolve(self, *, tenant_id: str, proposal: FulfillmentProposal) -> ImpactSubject | None:
        assert tenant_id == proposal.tenant_id
        return ImpactSubject(
            subject_kind="metric_version_change",
            subject_ref="metric:net-revenue",
            change_subject_digest=digest(proposal.subject),
        )


class NoImpactSubjectResolver:
    def resolve(self, *, tenant_id: str, proposal: FulfillmentProposal) -> ImpactSubject | None:
        return None


class InvalidSubjectResolver:
    def resolve(self, *, tenant_id: str, proposal: FulfillmentProposal) -> ImpactSubject | None:
        return ImpactSubject(
            subject_kind="metric_version_change",
            subject_ref="metric:net-revenue",
            change_subject_digest="f" * 64,
        )


def _store_graph(repository: ContextGraphRepository, *, metric_version: int) -> None:
    metric_reference = _reference("metric-record:net-revenue", metric_version, "a")
    dashboard_reference = _reference("dashboard-record:monthly-close", 1, "b")
    edge_reference = _reference("lineage-record:metric-dashboard", metric_version, "c")
    snapshot = ContextGraphProjector().rebuild(
        tenant_id="tenant-a",
        nodes=(
            ContextNode(
                tenant_id="tenant-a",
                node_id="metric:net-revenue",
                node_kind="metric_version",
                owner_ref="role:finance_data_owner",
                provenance=_observation(metric_reference),
            ),
            ContextNode(
                tenant_id="tenant-a",
                node_id="dashboard:monthly-close",
                node_kind="dashboard",
                owner_ref="role:analytics_owner",
                provenance=_observation(dashboard_reference),
            ),
        ),
        edges=(
            ContextEdge(
                tenant_id="tenant-a",
                edge_id="metric-to-dashboard",
                source_node_id="metric:net-revenue",
                target_node_id="dashboard:monthly-close",
                relationship="consumed_by",
                evidence_kind="validated",
                confidence=Decimal("1"),
                evidence_refs=(edge_reference,),
                provenance=_observation(edge_reference),
            ),
        ),
    )
    repository.replace(snapshot)


def test_graph_resolver_binds_current_sources_and_validated_owner(tmp_path: Path) -> None:
    repository = ContextGraphRepository(tmp_path / "graph.sqlite3")
    _store_graph(repository, metric_version=1)
    proposal = _proposal()
    resolver = GraphImpactAdmissionResolver(
        repository=repository,
        analyzer=ImpactAnalyzer(),
        subject_resolver=MetricSubjectResolver(),
        clock=lambda: _NOW,
    )

    binding = resolver.bind(tenant_id="tenant-a", proposal=proposal)

    assert binding is not None
    assert binding.subject.change_subject_digest == digest(proposal.subject)
    assert binding.authority_snapshot.source_record_refs == (
        _reference("dashboard-record:monthly-close", 1, "b"),
        _reference("metric-record:net-revenue", 1, "a"),
    )
    assert tuple(
        requirement.authority_ref
        for requirement in binding.authority_snapshot.derived_approval_requirements
    ) == ("role:analytics_owner",)


def test_graph_resolver_rederives_changed_source_authority(tmp_path: Path) -> None:
    repository = ContextGraphRepository(tmp_path / "graph.sqlite3")
    _store_graph(repository, metric_version=1)
    resolver = GraphImpactAdmissionResolver(
        repository=repository,
        analyzer=ImpactAnalyzer(),
        subject_resolver=MetricSubjectResolver(),
        clock=lambda: _NOW,
    )
    binding = resolver.bind(tenant_id="tenant-a", proposal=_proposal())
    assert binding is not None
    _store_graph(repository, metric_version=2)

    current = resolver.rederive(
        tenant_id="tenant-a",
        subject=binding.subject,
        source_record_refs=binding.authority_snapshot.source_record_refs,
    )

    assert current.authority_digest != binding.authority_snapshot_digest
    assert _reference("metric-record:net-revenue", 2, "a") in current.source_record_refs


def test_graph_resolver_presents_only_from_the_exact_bound_snapshot(tmp_path: Path) -> None:
    repository = ContextGraphRepository(tmp_path / "graph.sqlite3")
    _store_graph(repository, metric_version=1)
    resolver = GraphImpactAdmissionResolver(
        repository=repository,
        analyzer=ImpactAnalyzer(),
        subject_resolver=MetricSubjectResolver(),
        clock=lambda: _NOW,
    )
    binding = resolver.bind(tenant_id="tenant-a", proposal=_proposal())
    assert binding is not None

    analysis = resolver.analyze_binding(
        binding, can_view=lambda node: node.node_kind == "dashboard"
    )

    assert tuple(item.node_id for item in analysis.validated_impacts) == (
        "dashboard:monthly-close",
    )
    assert analysis.derived_approval_requirements[0].authority_ref == "role:analytics_owner"
    _store_graph(repository, metric_version=2)
    with pytest.raises(ImpactAdmissionResolutionError, match="no longer current"):
        resolver.analyze_binding(binding)


def test_graph_resolver_returns_no_binding_when_subject_is_not_impactful(tmp_path: Path) -> None:
    resolver = GraphImpactAdmissionResolver(
        repository=ContextGraphRepository(tmp_path / "graph.sqlite3"),
        analyzer=ImpactAnalyzer(),
        subject_resolver=NoImpactSubjectResolver(),
        clock=lambda: _NOW,
    )

    assert resolver.bind(tenant_id="tenant-a", proposal=_proposal()) is None


def test_graph_resolver_fails_closed_when_selected_graph_is_absent(tmp_path: Path) -> None:
    resolver = GraphImpactAdmissionResolver(
        repository=ContextGraphRepository(tmp_path / "graph.sqlite3"),
        analyzer=ImpactAnalyzer(),
        subject_resolver=MetricSubjectResolver(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ImpactAdmissionResolutionError, match="unavailable"):
        resolver.bind(tenant_id="tenant-a", proposal=_proposal())


def test_graph_resolver_rejects_a_subject_for_different_proposal_content(tmp_path: Path) -> None:
    repository = ContextGraphRepository(tmp_path / "graph.sqlite3")
    _store_graph(repository, metric_version=1)
    resolver = GraphImpactAdmissionResolver(
        repository=repository,
        analyzer=ImpactAnalyzer(),
        subject_resolver=InvalidSubjectResolver(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ImpactAdmissionResolutionError, match="subject authority is invalid"):
        resolver.bind(tenant_id="tenant-a", proposal=_proposal())


def test_binding_reader_returns_only_the_latest_owner_persisted_binding(tmp_path: Path) -> None:
    graph_repository = ContextGraphRepository(tmp_path / "graph.sqlite3")
    _store_graph(graph_repository, metric_version=1)
    fulfillment, requests, repository = service()
    fulfillment._impact_admission_resolver = GraphImpactAdmissionResolver(
        repository=graph_repository,
        analyzer=ImpactAnalyzer(),
        subject_resolver=MetricSubjectResolver(),
        clock=lambda: _NOW,
    )
    investigating, _ = submit_and_clarify(fulfillment, requests)
    reader = FulfillmentImpactBindingReader(repository)

    assert (
        reader.current_binding(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
        )
        is None
    )
    proposal = fulfillment.propose_answer(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert (
        reader.current_binding(
            tenant_id="tenant-a",
            request_id=investigating.request_id,
        )
        == proposal.impact_admission_binding
    )
    with pytest.raises(KeyError):
        reader.current_binding(tenant_id="tenant-b", request_id=investigating.request_id)
