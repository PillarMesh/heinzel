from datetime import datetime
from typing import Literal, Self

from pillarmesh_contract_model import ArtifactModel, digest
from pillarmesh_execution_graph import SignedExecutionGraph
from pydantic import Field, model_validator


class ActivationSummary(ArtifactModel):
    schema_version: Literal["1"] = "1"
    contract_id: str
    contract_version: int
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    iir_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    legality_decision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_graph_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_id: str
    verified_at: datetime
    expires_at: datetime
    limitations: tuple[str, ...]
    source_effect: str
    destination_effect: str
    signed_graph: SignedExecutionGraph

    @model_validator(mode="after")
    def artifact_parents_are_bound(self) -> Self:
        graph = self.signed_graph.graph
        expected_graph_parents = (
            ("contract", self.contract_digest, graph.contract_digest),
            ("source observation", self.source_observation_digest, graph.source_observation_digest),
            (
                "destination observation",
                self.destination_observation_digest,
                graph.destination_observation_digest,
            ),
            ("IIR", self.iir_digest, graph.iir_digest),
            ("physical plan", self.physical_plan_digest, graph.physical_plan_digest),
            (
                "legality decision",
                self.legality_decision_digest,
                graph.legality_decision_digest,
            ),
            ("graph", self.graph_digest, self.signed_graph.graph_digest),
        )
        for parent, expected, actual in expected_graph_parents:
            if expected != actual:
                raise ValueError(f"{parent} digest does not match signed graph")
        if self.graph_digest != digest(graph):
            raise ValueError("graph digest does not match embedded graph")
        if self.signed_graph_artifact_digest != digest(self.signed_graph):
            raise ValueError("signed graph artifact digest does not match signed envelope")
        return self
