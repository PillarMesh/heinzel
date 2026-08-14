from datetime import datetime
from typing import Literal

from pillarmesh_contract_model import ArtifactModel, JsonValue
from pydantic import Field

type RunState = Literal["created", "running", "succeeded", "failed", "non_conforming"]


class RunRecord(ArtifactModel):
    run_id: str
    activation_key: str
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_graph_json: str
    state: RunState
    checkpoint: str
    batch_id: str | None
    created_at: datetime
    updated_at: datetime


class EvidenceEvent(ArtifactModel):
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    occurred_at: datetime
    producer: str
    attributes: dict[str, JsonValue]
    previous_digest: str | None
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
