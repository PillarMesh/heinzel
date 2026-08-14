from datetime import datetime
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel, ProjectionField, digest
from pillarmesh_execution_graph import EvidenceRequirement, SignedExecutionGraph
from pillarmesh_iir import IntentIR
from pydantic import Field, model_validator


class PreconditionResult(ArtifactModel):
    number: int = Field(ge=1, le=10)
    status: Literal["satisfied", "unsatisfied", "unknown"]
    reason: str
    evidence_ids: tuple[str, ...] = ()


class NoValidPlan(ArtifactModel):
    result: Literal["no_valid_plan"] = "no_valid_plan"
    rule_id: str
    preconditions: tuple[PreconditionResult, ...]
    smallest_changes: tuple[str, ...]
    execution_occurred: Literal[False] = False


class PhysicalPlan(ArtifactModel):
    schema_version: Literal["1"] = "1"
    rule_id: str
    operators: tuple[str, ...]
    projection: tuple[ProjectionField, ...]
    required_evidence: tuple[EvidenceRequirement, ...]


class AdmittedPlan(ArtifactModel):
    result: Literal["admitted"] = "admitted"
    rule_id: str
    preconditions: tuple[PreconditionResult, ...]
    physical_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_plan: PhysicalPlan

    @model_validator(mode="after")
    def physical_plan_is_bound(self) -> Self:
        if self.physical_plan_digest != digest(self.physical_plan):
            raise ValueError("physical plan digest does not match embedded physical plan")
        return self


class CompilationBundle(ArtifactModel):
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: datetime
    iir: IntentIR
    physical_plan: PhysicalPlan
    legality_decision: AdmittedPlan
    signed_graph: SignedExecutionGraph

    @property
    def iir_digest(self) -> str:
        return digest(self.iir)

    @property
    def physical_plan_digest(self) -> str:
        return digest(self.physical_plan)

    @property
    def legality_decision_digest(self) -> str:
        return digest(self.legality_decision)

    @property
    def signed_graph_artifact_digest(self) -> str:
        return digest(self.signed_graph)

    @model_validator(mode="after")
    def graph_parents_are_bound(self) -> Self:
        graph = self.signed_graph.graph
        if self.signed_graph.graph_digest != digest(graph):
            raise ValueError("signed graph digest does not match embedded graph")
        if graph.contract_digest != self.contract_digest:
            raise ValueError("contract digest does not match graph parent")
        if graph.iir_digest != self.iir_digest:
            raise ValueError("IIR digest does not match graph parent")
        if graph.physical_plan_digest != self.physical_plan_digest:
            raise ValueError("physical plan digest does not match graph parent")
        if graph.legality_decision_digest != self.legality_decision_digest:
            raise ValueError("legality decision digest does not match graph parent")
        if self.legality_decision.physical_plan_digest != self.physical_plan_digest:
            raise ValueError("physical plan digest does not match legality decision parent")
        return self
