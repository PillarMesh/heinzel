from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from heinzel_contract_model import (
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactApprovalRequirement,
    ImpactAuthoritySnapshot,
    ImpactSubject,
    digest,
)
from heinzel_knowledge_graph import (
    ContextGraphRepository,
    ContextGraphSnapshot,
    ContextNode,
    GraphProjectionError,
    ImpactAnalysis,
    ImpactAnalysisError,
    ImpactAnalyzer,
)

from .fulfillment_errors import ImpactAdmissionResolutionError
from .fulfillment_models import FulfillmentProposal
from .fulfillment_repository import FulfillmentRepository


class ImpactSubjectResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        proposal: FulfillmentProposal,
    ) -> ImpactSubject | None: ...


class FulfillmentImpactBindingReader:
    def __init__(self, repository: FulfillmentRepository) -> None:
        self._repository = repository

    def current_binding(
        self,
        *,
        tenant_id: str,
        request_id: str,
    ) -> ImpactAdmissionBinding | None:
        proposals = self._repository.list_proposals(tenant_id, request_id)
        if not proposals:
            return None
        return proposals[-1].impact_admission_binding


class GraphImpactAdmissionResolver:
    def __init__(
        self,
        *,
        repository: ContextGraphRepository,
        analyzer: ImpactAnalyzer,
        subject_resolver: ImpactSubjectResolver,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._analyzer = analyzer
        self._subject_resolver = subject_resolver
        self._clock = clock

    def bind(
        self,
        *,
        tenant_id: str,
        proposal: FulfillmentProposal,
    ) -> ImpactAdmissionBinding | None:
        subject = self._subject_resolver.resolve(tenant_id=tenant_id, proposal=proposal)
        if subject is None:
            return None
        if tenant_id != proposal.tenant_id or subject.change_subject_digest != digest(
            proposal.subject
        ):
            raise ImpactAdmissionResolutionError("impact subject authority is invalid")
        snapshot = self._load_snapshot(tenant_id)
        authority = self._derive(snapshot=snapshot, subject=subject)
        return ImpactAdmissionBinding.create(
            graph_snapshot_digest=snapshot.graph_snapshot_digest,
            authority_snapshot=authority,
        )

    def rederive(
        self,
        *,
        tenant_id: str,
        subject: ImpactSubject,
        source_record_refs: tuple[ArtifactReference, ...],
    ) -> ImpactAuthoritySnapshot:
        if not source_record_refs:
            raise ImpactAdmissionResolutionError("impact source authority is absent")
        return self._derive(snapshot=self._load_snapshot(tenant_id), subject=subject)

    def analyze_binding(
        self,
        binding: ImpactAdmissionBinding,
        *,
        can_view: Callable[[ContextNode], bool] | None = None,
    ) -> ImpactAnalysis:
        snapshot = self._load_snapshot(binding.tenant_id)
        if snapshot.graph_snapshot_digest != binding.graph_snapshot_digest:
            raise ImpactAdmissionResolutionError("bound impact graph is no longer current")
        complete_analysis = self._analyze(snapshot=snapshot, subject=binding.subject)
        authority = self._authority_snapshot(
            snapshot=snapshot,
            subject=binding.subject,
            analysis=complete_analysis,
        )
        if authority.authority_digest != binding.authority_snapshot_digest:
            raise ImpactAdmissionResolutionError("bound impact authority is no longer current")
        if can_view is None:
            return complete_analysis
        return self._analyze(snapshot=snapshot, subject=binding.subject, can_view=can_view)

    def _load_snapshot(self, tenant_id: str) -> ContextGraphSnapshot:
        try:
            snapshot = self._repository.load(tenant_id)
        except GraphProjectionError as error:
            raise ImpactAdmissionResolutionError("impact graph is invalid") from error
        if snapshot is None:
            raise ImpactAdmissionResolutionError("impact graph is unavailable")
        return snapshot

    def _derive(
        self,
        *,
        snapshot: ContextGraphSnapshot,
        subject: ImpactSubject,
    ) -> ImpactAuthoritySnapshot:
        analysis = self._analyze(snapshot=snapshot, subject=subject)
        return self._authority_snapshot(snapshot=snapshot, subject=subject, analysis=analysis)

    def _analyze(
        self,
        *,
        snapshot: ContextGraphSnapshot,
        subject: ImpactSubject,
        can_view: Callable[[ContextNode], bool] | None = None,
    ) -> ImpactAnalysis:
        try:
            return self._analyzer.analyze(
                snapshot=snapshot,
                analysis_id=self._analysis_id(snapshot, subject),
                subject_kind=subject.subject_kind,
                subject_ref=subject.subject_ref,
                created_at=self._clock(),
                can_view=can_view,
            )
        except ImpactAnalysisError as error:
            raise ImpactAdmissionResolutionError("impact subject cannot be analyzed") from error

    @staticmethod
    def _authority_snapshot(
        *,
        snapshot: ContextGraphSnapshot,
        subject: ImpactSubject,
        analysis: ImpactAnalysis,
    ) -> ImpactAuthoritySnapshot:
        return ImpactAuthoritySnapshot(
            tenant_id=snapshot.tenant_id,
            subject=subject,
            source_record_refs=GraphImpactAdmissionResolver._source_records(snapshot, analysis),
            derived_approval_requirements=tuple(
                ImpactApprovalRequirement(
                    authority_ref=requirement.authority_ref,
                    reason_code=requirement.reason_code,
                    affected_subject_ref=requirement.subject_ref,
                )
                for requirement in analysis.derived_approval_requirements
            ),
        )

    @staticmethod
    def _analysis_id(snapshot: ContextGraphSnapshot, subject: ImpactSubject) -> str:
        identity = {"snapshot": snapshot.graph_snapshot_digest, "subject": subject}
        return f"impact-analysis:{digest(identity)}"

    @staticmethod
    def _source_records(
        snapshot: ContextGraphSnapshot,
        analysis: ImpactAnalysis,
    ) -> tuple[ArtifactReference, ...]:
        nodes = {node.node_id: node for node in snapshot.nodes}
        references = {
            nodes[analysis.subject_ref].provenance.source_record_ref,
            *(impact.source_record_ref for impact in analysis.validated_impacts),
        }
        return tuple(
            sorted(
                references,
                key=lambda reference: (
                    reference.artifact_id,
                    reference.version,
                    reference.digest,
                ),
            )
        )
