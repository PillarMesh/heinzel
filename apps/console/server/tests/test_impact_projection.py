from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from pillarmesh_console.contracts import ActorRole
from pillarmesh_console.impact_projection import (
    ImpactProjectionError,
    ImpactProjectionRecord,
    ImpactViewProjector,
    RequestImpactProjectionReader,
)
from pillarmesh_contract_model import (
    ArtifactReference,
    ImpactAdmissionBinding,
    ImpactApprovalRequirement,
    ImpactAuthoritySnapshot,
    ImpactSubject,
)
from pillarmesh_knowledge_graph import (
    ApprovalRequirement,
    ContextNode,
    ImpactAnalysis,
    ImpactReference,
)
from pillarmesh_request_management import ImpactAdmissionResolutionError

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
DIGEST = "a" * 64
GRAPH_DIGEST = "b" * 64


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def _impact(node_id: str, owner_ref: str) -> ImpactReference:
    return ImpactReference(
        node_id=node_id,
        node_kind="dashboard",
        owner_ref=owner_ref,
        via_edge_ids=(f"edge:{node_id}",),
        source_record_ref=_reference(f"record:{node_id}"),
        validity="valid",
    )


def _record(
    *, visible_node_ids: tuple[str, ...] = ("dashboard:revenue",)
) -> ImpactProjectionRecord:
    requirements = tuple(
        sorted(
            (
                ApprovalRequirement(
                    authority_ref="principal:revenue-owner",
                    reason_code="validated_context_graph_dependency",
                    subject_ref="dashboard:revenue",
                ),
                ApprovalRequirement(
                    authority_ref="principal:private-owner",
                    reason_code="validated_context_graph_dependency",
                    subject_ref="dashboard:private",
                ),
            ),
            key=lambda item: (item.authority_ref, item.reason_code, item.subject_ref),
        )
    )
    all_impacts = {
        "dashboard:revenue": _impact("dashboard:revenue", "principal:revenue-owner"),
        "dashboard:private": _impact("dashboard:private", "principal:private-owner"),
    }
    subject = ImpactSubject(
        subject_kind="metric_version_change",
        subject_ref="metric:net-revenue:v2",
        change_subject_digest="c" * 64,
    )
    authority = ImpactAuthoritySnapshot(
        tenant_id="tenant-primary",
        subject=subject,
        source_record_refs=(_reference("metric-version:net-revenue:v2"),),
        derived_approval_requirements=tuple(
            ImpactApprovalRequirement(
                authority_ref=requirement.authority_ref,
                reason_code=requirement.reason_code,
                affected_subject_ref=requirement.subject_ref,
            )
            for requirement in requirements
        ),
    )
    return ImpactProjectionRecord(
        analysis=ImpactAnalysis(
            analysis_id="analysis-1",
            tenant_id="tenant-primary",
            subject_kind="metric_version_change",
            subject_ref="metric:net-revenue:v2",
            graph_snapshot_digest=GRAPH_DIGEST,
            validated_impacts=tuple(all_impacts[node_id] for node_id in visible_node_ids),
            inferred_impacts=(
                ImpactReference(
                    node_id="report:forecast",
                    node_kind="report",
                    owner_ref="principal:finance-owner",
                    via_edge_ids=("edge:forecast",),
                    source_record_ref=_reference("record:forecast"),
                    validity="valid",
                ),
            ),
            affected_owner_refs=tuple(
                sorted(
                    {all_impacts[node_id].owner_ref for node_id in visible_node_ids}
                    | {"principal:finance-owner"}
                )
            ),
            derived_approval_requirements=requirements,
            created_at=NOW,
        ),
        binding=ImpactAdmissionBinding.create(
            graph_snapshot_digest=GRAPH_DIGEST,
            authority_snapshot=authority,
        ),
    )


class Labels:
    _labels: ClassVar[dict[str, str]] = {
        "metric:net-revenue:v2": "Net revenue v2",
        "dashboard:revenue": "Revenue overview",
        "dashboard:private": "Executive margin",
        "report:forecast": "Quarterly forecast",
    }
    _owners: ClassVar[dict[str, str]] = {
        "principal:revenue-owner": "Revenue data owner",
        "principal:private-owner": "Executive data owner",
        "principal:finance-owner": "Finance data owner",
    }

    def subject_label(self, tenant_id: str, subject_ref: str) -> str | None:
        return self._labels.get(subject_ref)

    def impact_label(self, tenant_id: str, node_id: str) -> str | None:
        return self._labels.get(node_id)

    def owner_label(self, tenant_id: str, owner_ref: str) -> str | None:
        return self._owners.get(owner_ref)

    def authority_label(self, tenant_id: str, authority_ref: str) -> str | None:
        return self._owners.get(authority_ref)


class Bindings:
    def __init__(self, binding: ImpactAdmissionBinding | None) -> None:
        self.binding = binding

    def current_binding(self, *, tenant_id: str, request_id: str) -> ImpactAdmissionBinding | None:
        assert tenant_id == "tenant-primary"
        assert request_id == "request-answer"
        return self.binding


class Analyses:
    def __init__(self, analysis: ImpactAnalysis | None) -> None:
        self.analysis = analysis

    def analyze_binding(
        self,
        binding: ImpactAdmissionBinding,
        *,
        can_view: Callable[[ContextNode], bool] | None = None,
    ) -> ImpactAnalysis:
        assert can_view is not None
        if self.analysis is None:
            raise ImpactAdmissionResolutionError("private graph detail")
        return self.analysis


class Visibility:
    def can_view(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        active_role: ActorRole,
        node: ContextNode,
    ) -> bool:
        return (
            tenant_id == "tenant-primary"
            and actor_id == "architect-a"
            and active_role == "data_architect"
        )


def test_projector_shows_authorized_validated_and_possible_impacts_without_internal_refs() -> None:
    view = ImpactViewProjector(Labels()).project(
        request_id="request-answer",
        tenant_id="tenant-primary",
        record=_record(),
    )

    assert view.subject_label == "Net revenue v2"
    assert [item.label for item in view.validated_impacts] == ["Revenue overview"]
    assert [item.label for item in view.possible_impacts] == ["Quarterly forecast"]
    assert view.affected_owners == ("Finance data owner", "Revenue data owner")
    assert len(view.added_approvers) == 2
    serialized = view.model_dump_json()
    for private_value in (
        "metric:net-revenue:v2",
        "dashboard:revenue",
        "principal:revenue-owner",
        GRAPH_DIGEST,
        DIGEST,
        "record:dashboard",
    ):
        assert private_value not in serialized


def test_reader_visibility_changes_assets_but_never_weakens_authority_requirements() -> None:
    projector = ImpactViewProjector(Labels())

    complete = projector.project(
        request_id="request-answer",
        tenant_id="tenant-primary",
        record=_record(visible_node_ids=("dashboard:private", "dashboard:revenue")),
    )
    restricted = projector.project(
        request_id="request-answer",
        tenant_id="tenant-primary",
        record=_record(visible_node_ids=("dashboard:revenue",)),
    )

    assert len(restricted.validated_impacts) < len(complete.validated_impacts)
    assert restricted.added_approvers == complete.added_approvers
    assert "Executive margin" not in restricted.model_dump_json()


def test_projector_rejects_a_binding_from_another_snapshot_before_presenting_it() -> None:
    record = _record()
    mismatched = replace(
        record,
        analysis=record.analysis.model_copy(update={"graph_snapshot_digest": "d" * 64}),
    )

    with pytest.raises(ImpactProjectionError, match="snapshot"):
        ImpactViewProjector(Labels()).project(
            request_id="request-answer",
            tenant_id="tenant-primary",
            record=mismatched,
        )


def test_projector_rejects_requirements_that_do_not_match_owning_authority_snapshot() -> None:
    record = _record()
    mismatched = replace(
        record,
        analysis=record.analysis.model_copy(update={"derived_approval_requirements": ()}),
    )

    with pytest.raises(ImpactProjectionError, match="authority requirements"):
        ImpactViewProjector(Labels()).project(
            request_id="request-answer",
            tenant_id="tenant-primary",
            record=mismatched,
        )


def test_request_reader_projects_the_current_owner_binding() -> None:
    record = _record()
    reader = RequestImpactProjectionReader(
        bindings=Bindings(record.binding),
        analyzer=Analyses(record.analysis),
        visibility=Visibility(),
        projector=ImpactViewProjector(Labels()),
    )

    view = reader.get_request_impact(
        tenant_id="tenant-primary",
        actor_id="architect-a",
        active_role="data_architect",
        request_id="request-answer",
    )

    assert view is not None
    assert view.subject_label == "Net revenue v2"
    assert view.validated_impacts[0].label == "Revenue overview"


def test_request_reader_returns_absent_without_invoking_analysis() -> None:
    reader = RequestImpactProjectionReader(
        bindings=Bindings(None),
        analyzer=Analyses(None),
        visibility=Visibility(),
        projector=ImpactViewProjector(Labels()),
    )

    assert (
        reader.get_request_impact(
            tenant_id="tenant-primary",
            actor_id="architect-a",
            active_role="data_architect",
            request_id="request-answer",
        )
        is None
    )


def test_request_reader_hides_internal_analysis_failure() -> None:
    record = _record()
    reader = RequestImpactProjectionReader(
        bindings=Bindings(record.binding),
        analyzer=Analyses(None),
        visibility=Visibility(),
        projector=ImpactViewProjector(Labels()),
    )

    with pytest.raises(ImpactProjectionError, match="authority is unavailable") as captured:
        reader.get_request_impact(
            tenant_id="tenant-primary",
            actor_id="architect-a",
            active_role="data_architect",
            request_id="request-answer",
        )

    assert captured.value.__cause__ is None
