from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pillarmesh_contract_model import ImpactAdmissionBinding
from pillarmesh_knowledge_graph import ContextNode, ImpactAnalysis, ImpactReference
from pillarmesh_request_management import ImpactAdmissionResolutionError

from .contracts import ActorRole, ImpactApproverView, ImpactItemView, ImpactView


class ImpactProjectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImpactProjectionRecord:
    analysis: ImpactAnalysis
    binding: ImpactAdmissionBinding


class ImpactSafeLabelReader(Protocol):
    """Resolve presentation labels already authorized for the current reader."""

    def subject_label(self, tenant_id: str, subject_ref: str) -> str | None: ...

    def impact_label(self, tenant_id: str, node_id: str) -> str | None: ...

    def owner_label(self, tenant_id: str, owner_ref: str) -> str | None: ...

    def authority_label(self, tenant_id: str, authority_ref: str) -> str | None: ...


class ImpactBindingReader(Protocol):
    def current_binding(
        self,
        *,
        tenant_id: str,
        request_id: str,
    ) -> ImpactAdmissionBinding | None: ...


class BoundImpactAnalyzer(Protocol):
    def analyze_binding(
        self,
        binding: ImpactAdmissionBinding,
        *,
        can_view: Callable[[ContextNode], bool] | None = None,
    ) -> ImpactAnalysis: ...


class ImpactVisibilityReader(Protocol):
    def can_view(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        active_role: ActorRole,
        node: ContextNode,
    ) -> bool: ...


_ASSET_TYPE_LABELS = {
    "source_object": "Source object",
    "source_contract": "Source contract",
    "acquisition": "Acquisition",
    "raw_generation": "Data generation",
    "canonical_entity": "Canonical entity",
    "transformation_model": "Transformation",
    "data_product": "Data product",
    "metric_version": "Metric version",
    "consumption_object": "Consumption object",
    "dashboard": "Dashboard",
    "report": "Report",
    "answer_scope_policy": "Answer policy",
    "scout": "Scout",
    "governed_answer": "Governed answer",
    "requester": "Requester",
    "delegated_agent_session": "Delegated session",
    "access_grant": "Access grant",
    "principal": "Principal",
    "open_request": "Open request",
    "dependency": "Dependency",
}
_APPROVAL_REASON_LABELS = {
    "validated_context_graph_dependency": "Approval required for a validated dependency.",
}


class ImpactViewProjector:
    """Validate owner-issued authority, then translate authorized graph nodes to safe labels."""

    def __init__(self, labels: ImpactSafeLabelReader) -> None:
        self._labels = labels

    def project(
        self,
        *,
        request_id: str,
        tenant_id: str,
        record: ImpactProjectionRecord,
    ) -> ImpactView:
        analysis = record.analysis
        binding = record.binding
        authority = binding.authority_snapshot
        if analysis.tenant_id != tenant_id or binding.tenant_id != tenant_id:
            raise ImpactProjectionError("impact tenant does not match the requested tenant")
        if (
            analysis.subject_kind != binding.subject.subject_kind
            or analysis.subject_ref != binding.subject.subject_ref
        ):
            raise ImpactProjectionError("impact subject does not match the authority binding")
        if analysis.graph_snapshot_digest != binding.graph_snapshot_digest:
            raise ImpactProjectionError("impact analysis does not match the bound graph snapshot")

        analysis_requirements = tuple(
            (item.authority_ref, item.reason_code, item.subject_ref)
            for item in analysis.derived_approval_requirements
        )
        authority_requirements = tuple(
            (item.authority_ref, item.reason_code, item.affected_subject_ref)
            for item in authority.derived_approval_requirements
        )
        if analysis_requirements != authority_requirements:
            raise ImpactProjectionError(
                "impact analysis authority requirements do not match the owning snapshot"
            )

        subject_label = self._labels.subject_label(tenant_id, analysis.subject_ref)
        if subject_label is None:
            raise ImpactProjectionError("impact subject has no authorized presentation label")
        validated = self._items(tenant_id, request_id, analysis.validated_impacts)
        possible = self._items(tenant_id, request_id, analysis.inferred_impacts)
        affected_owners = tuple(sorted({item.owner_label for item in (*validated, *possible)}))
        added_approvers = tuple(
            ImpactApproverView(
                authority_label=(
                    self._labels.authority_label(tenant_id, requirement.authority_ref)
                    or "Governed approver"
                ),
                reason=_APPROVAL_REASON_LABELS.get(
                    requirement.reason_code,
                    "Approval required by the owning service.",
                ),
            )
            for requirement in authority.derived_approval_requirements
        )
        return ImpactView(
            request_id=request_id,
            change_type=analysis.subject_kind,
            subject_label=subject_label,
            analyzed_at=analysis.created_at,
            validated_impacts=validated,
            possible_impacts=possible,
            affected_owners=affected_owners,
            added_approvers=added_approvers,
        )

    def _items(
        self,
        tenant_id: str,
        request_id: str,
        impacts: tuple[ImpactReference, ...],
    ) -> tuple[ImpactItemView, ...]:
        items: list[ImpactItemView] = []
        for impact in impacts:
            label = self._labels.impact_label(tenant_id, impact.node_id)
            owner_label = self._labels.owner_label(tenant_id, impact.owner_ref)
            if label is None or owner_label is None:
                continue
            handle_digest = hashlib.sha256(
                f"pillarmesh.console-impact.v1\0{request_id}\0{impact.node_id}".encode()
            ).hexdigest()[:20]
            items.append(
                ImpactItemView(
                    impact_handle=f"impact-{handle_digest}",
                    label=label,
                    asset_type=_ASSET_TYPE_LABELS[impact.node_kind],
                    owner_label=owner_label,
                )
            )
        return tuple(items)


class RequestImpactProjectionReader:
    def __init__(
        self,
        *,
        bindings: ImpactBindingReader,
        analyzer: BoundImpactAnalyzer,
        visibility: ImpactVisibilityReader,
        projector: ImpactViewProjector,
    ) -> None:
        self._bindings = bindings
        self._analyzer = analyzer
        self._visibility = visibility
        self._projector = projector

    def get_request_impact(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        active_role: ActorRole,
        request_id: str,
    ) -> ImpactView | None:
        binding = self._bindings.current_binding(tenant_id=tenant_id, request_id=request_id)
        if binding is None:
            return None
        try:
            analysis = self._analyzer.analyze_binding(
                binding,
                can_view=lambda node: self._visibility.can_view(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    active_role=active_role,
                    node=node,
                ),
            )
        except ImpactAdmissionResolutionError:
            raise ImpactProjectionError("impact authority is unavailable") from None
        return self._projector.project(
            request_id=request_id,
            tenant_id=tenant_id,
            record=ImpactProjectionRecord(analysis=analysis, binding=binding),
        )
