from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DailyTriggerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["1"] = "1"
    policy_version: str = Field(min_length=1)
    hour_utc: int = Field(ge=0, le=23)
    minute_utc: int = Field(ge=0, le=59)
    maximum_backfill_windows: int = Field(default=31, ge=1, le=366)
    overlap_policy: Literal["forbid"] = "forbid"
    misfire_policy: Literal["run_immediately"] = "run_immediately"


class RunNowPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["1"] = "1"
    policy_version: str = Field(min_length=1)
