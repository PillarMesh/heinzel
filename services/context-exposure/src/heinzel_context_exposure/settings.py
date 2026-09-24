from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEINZEL_AGENT_",
        extra="forbid",
        frozen=True,
        env_file=None,
    )

    tenant_id: str = Field(min_length=1, max_length=512)
    delegation_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    agent_client_ref: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=512)
    rate_invocation_ceiling: int = Field(default=60, gt=0, le=100_000)
    rate_window_seconds: int = Field(default=60, gt=0, le=86_400)
    maximum_rate_principals: int = Field(default=10_000, gt=0, le=1_000_000)
