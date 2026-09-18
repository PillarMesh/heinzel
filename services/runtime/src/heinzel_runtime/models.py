from typing import Literal

from pydantic import BaseModel, ConfigDict


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    state: Literal["succeeded", "failed", "non_conforming"]
    batch_id: str | None = None
    manifest_digest: str | None = None
    receipt_digest: str | None = None
    visibility_digest: str | None = None
