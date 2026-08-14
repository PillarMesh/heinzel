from datetime import datetime
from typing import Literal

from pillarmesh_contract_model import ArtifactModel, ProjectionField
from pydantic import Field


class EvidenceRequirement(ArtifactModel):
    event_type: str
    redaction_class: Literal["metadata", "digest"]


class ExecutionGraph(ArtifactModel):
    schema_version: Literal["1"] = "1"
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    iir_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    legality_decision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_connection_handle: str
    destination_connection_handle: str
    source_object_identity: str
    source_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection: tuple[ProjectionField, ...]
    operators: tuple[str, ...]
    required_evidence: tuple[EvidenceRequirement, ...]
    issued_at: datetime
    expires_at: datetime
    max_rows: Literal[10_000] = 10_000
    max_encoded_bytes: Literal[67_108_864] = 67_108_864
    max_segments: Literal[1] = 1
    max_runtime_seconds: Literal[900] = 900


class SignedExecutionGraph(ArtifactModel):
    schema_version: Literal["1"] = "1"
    graph: ExecutionGraph
    graph_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str
    signature: str
