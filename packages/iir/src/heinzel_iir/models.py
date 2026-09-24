from typing import Literal

from heinzel_contract_model import ArtifactModel, ProjectionField, digest
from pydantic import ConfigDict


class IntentIR(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    contract_id: str
    contract_version: int
    source_relation: str
    source_primary_key: Literal["order_id"]
    projection: tuple[ProjectionField, ...]
    materialization_mode: Literal["snapshot"] = "snapshot"
    deletion_behavior: Literal["not_observed"] = "not_observed"

    @property
    def semantic_digest(self) -> str:
        return digest(self)
