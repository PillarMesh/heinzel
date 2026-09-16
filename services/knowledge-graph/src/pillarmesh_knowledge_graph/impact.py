from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel, ArtifactReference
from pydantic import ConfigDict, Field, field_validator, model_validator

from .projection import (
    ContextEdge,
    ContextGraphSnapshot,
    ContextNode,
    ContextNodeKind,
    SourceValidity,
)

type ImpactSubjectKind = Literal[
    "source_drift",
    "metric_version_change",
    "contract_supersession",
    "generation_failure",
    "policy_change",
    "grant_change",
    "retirement",
]

_RETIRABLE_NODE_KINDS: frozenset[ContextNodeKind] = frozenset(
    (
        "source_object",
        "source_contract",
        "canonical_entity",
        "transformation_model",
        "data_product",
        "metric_version",
        "consumption_object",
        "dashboard",
        "report",
        "answer_scope_policy",
        "scout",
        "access_grant",
        "open_request",
    )
)
IMPACT_SUBJECT_NODE_KINDS: Mapping[ImpactSubjectKind, frozenset[ContextNodeKind]] = (
    MappingProxyType(
        {
            "source_drift": frozenset(("source_object",)),
            "metric_version_change": frozenset(("metric_version",)),
            "contract_supersession": frozenset(("source_contract",)),
            "generation_failure": frozenset(("raw_generation",)),
            "policy_change": frozenset(("answer_scope_policy",)),
            "grant_change": frozenset(("access_grant",)),
            # Retirement applies to governed assets with explicit lifecycle authority. Identity,
            # session, delivered-answer, generation-history, and projection-helper nodes are
            # deliberately excluded because the context graph cannot retire those records.
            "retirement": _RETIRABLE_NODE_KINDS,
        }
    )
)


class _ImpactModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ImpactAnalysisError(RuntimeError):
    pass


class ImpactReference(_ImpactModel):
    node_id: str = Field(min_length=1)
    node_kind: ContextNodeKind
    owner_ref: str = Field(min_length=1)
    via_edge_ids: tuple[str, ...] = Field(min_length=1)
    source_record_ref: ArtifactReference
    validity: SourceValidity


class ApprovalRequirement(_ImpactModel):
    authority_ref: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)


class ImpactAnalysis(_ImpactModel):
    schema_version: Literal["1"] = "1"
    analysis_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    subject_kind: ImpactSubjectKind
    subject_ref: str = Field(min_length=1)
    graph_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validated_impacts: tuple[ImpactReference, ...]
    inferred_impacts: tuple[ImpactReference, ...]
    affected_owner_refs: tuple[str, ...]
    derived_approval_requirements: tuple[ApprovalRequirement, ...]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def impact_collections_are_canonical(self) -> Self:
        validated_ids = tuple(item.node_id for item in self.validated_impacts)
        inferred_ids = tuple(item.node_id for item in self.inferred_impacts)
        if validated_ids != tuple(sorted(validated_ids)) or len(validated_ids) != len(
            set(validated_ids)
        ):
            raise ValueError("validated impacts must use unique canonical order")
        if inferred_ids != tuple(sorted(inferred_ids)) or len(inferred_ids) != len(
            set(inferred_ids)
        ):
            raise ValueError("inferred impacts must use unique canonical order")
        if set(validated_ids) & set(inferred_ids):
            raise ValueError("an impact cannot be both validated and inferred")
        if self.affected_owner_refs != tuple(sorted(set(self.affected_owner_refs))):
            raise ValueError("affected owners must use unique canonical order")
        if self.derived_approval_requirements != _canonical_requirements(
            self.derived_approval_requirements
        ):
            raise ValueError("approval requirements must use unique canonical order")
        return self


def add_approval_requirements(
    owning_requirements: Iterable[ApprovalRequirement],
    derived_requirements: Iterable[ApprovalRequirement],
) -> tuple[ApprovalRequirement, ...]:
    return _canonical_requirements((*owning_requirements, *derived_requirements))


class ImpactAnalyzer:
    def analyze(
        self,
        *,
        snapshot: ContextGraphSnapshot,
        analysis_id: str,
        subject_kind: ImpactSubjectKind,
        subject_ref: str,
        created_at: datetime,
        can_view: Callable[[ContextNode], bool] | None = None,
    ) -> ImpactAnalysis:
        nodes_by_id = {node.node_id: node for node in snapshot.nodes}
        if subject_ref not in nodes_by_id:
            raise ImpactAnalysisError("impact subject is absent from the graph snapshot")
        compatible_node_kinds = IMPACT_SUBJECT_NODE_KINDS.get(subject_kind)
        if (
            compatible_node_kinds is None
            or nodes_by_id[subject_ref].node_kind not in compatible_node_kinds
        ):
            raise ImpactAnalysisError("impact subject kind is incompatible with graph node kind")

        validated_paths = self._shortest_paths(
            snapshot=snapshot,
            subject_ref=subject_ref,
            allowed_evidence=frozenset({"validated"}),
        )
        all_paths = self._shortest_paths(
            snapshot=snapshot,
            subject_ref=subject_ref,
            allowed_evidence=frozenset({"validated", "inferred"}),
        )
        validated_impacts = self._references(validated_paths, nodes_by_id)
        inferred_paths = {
            node_id: path for node_id, path in all_paths.items() if node_id not in validated_paths
        }
        inferred_impacts = self._references(inferred_paths, nodes_by_id)
        requirements = _canonical_requirements(
            ApprovalRequirement(
                authority_ref=impact.owner_ref,
                reason_code="validated_context_graph_dependency",
                subject_ref=impact.node_id,
            )
            for impact in validated_impacts
        )

        # Approval derivation is complete before reader visibility is consulted. Filtering this
        # tuple would let a restricted view silently weaken the owning service's admission rules.
        visible_validated = self._visible(validated_impacts, nodes_by_id, can_view)
        visible_inferred = self._visible(inferred_impacts, nodes_by_id, can_view)
        visible_owners = tuple(
            sorted({impact.owner_ref for impact in (*visible_validated, *visible_inferred)})
        )
        return ImpactAnalysis(
            analysis_id=analysis_id,
            tenant_id=snapshot.tenant_id,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            graph_snapshot_digest=snapshot.graph_snapshot_digest,
            validated_impacts=visible_validated,
            inferred_impacts=visible_inferred,
            affected_owner_refs=visible_owners,
            derived_approval_requirements=requirements,
            created_at=created_at,
        )

    @staticmethod
    def _shortest_paths(
        *,
        snapshot: ContextGraphSnapshot,
        subject_ref: str,
        allowed_evidence: frozenset[str],
    ) -> dict[str, tuple[str, ...]]:
        outgoing: dict[str, list[ContextEdge]] = defaultdict(list)
        valid_node_ids = {
            node.node_id for node in snapshot.nodes if node.provenance.validity == "valid"
        }
        for edge in snapshot.edges:
            if (
                edge.evidence_kind in allowed_evidence
                and edge.provenance.validity == "valid"
                and edge.target_node_id in valid_node_ids
            ):
                outgoing[edge.source_node_id].append(edge)
        for edge_list in outgoing.values():
            edge_list.sort(key=lambda edge: edge.edge_id)

        paths: dict[str, tuple[str, ...]] = {}
        queue: deque[tuple[str, tuple[str, ...]]] = deque(((subject_ref, ()),))
        visited = {subject_ref}
        while queue:
            node_id, path = queue.popleft()
            for edge in outgoing[node_id]:
                if edge.target_node_id in visited:
                    continue
                next_path = (*path, edge.edge_id)
                visited.add(edge.target_node_id)
                paths[edge.target_node_id] = next_path
                queue.append((edge.target_node_id, next_path))
        return paths

    @staticmethod
    def _references(
        paths: dict[str, tuple[str, ...]],
        nodes_by_id: dict[str, ContextNode],
    ) -> tuple[ImpactReference, ...]:
        return tuple(
            ImpactReference(
                node_id=node_id,
                node_kind=nodes_by_id[node_id].node_kind,
                owner_ref=nodes_by_id[node_id].owner_ref,
                via_edge_ids=paths[node_id],
                source_record_ref=nodes_by_id[node_id].provenance.source_record_ref,
                validity=nodes_by_id[node_id].provenance.validity,
            )
            for node_id in sorted(paths)
        )

    @staticmethod
    def _visible(
        impacts: tuple[ImpactReference, ...],
        nodes_by_id: dict[str, ContextNode],
        can_view: Callable[[ContextNode], bool] | None,
    ) -> tuple[ImpactReference, ...]:
        if can_view is None:
            return impacts
        return tuple(impact for impact in impacts if can_view(nodes_by_id[impact.node_id]))


def _canonical_requirements(
    requirements: Iterable[ApprovalRequirement],
) -> tuple[ApprovalRequirement, ...]:
    unique = {
        (requirement.authority_ref, requirement.reason_code, requirement.subject_ref): requirement
        for requirement in requirements
    }
    return tuple(unique[key] for key in sorted(unique))
