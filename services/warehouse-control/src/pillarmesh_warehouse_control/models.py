from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pillarmesh_contract_model import ArtifactModel
from pydantic import Field, field_validator


class EngineKind(StrEnum):
    POSTGRESQL = "postgresql"
    CLICKHOUSE = "clickhouse"


class WarehouseBindingState(StrEnum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    SUSPENDED = "suspended"
    RETIRING = "retiring"
    RETIRED = "retired"


class WarehouseBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str
    tenant_id: str
    engine_kind: EngineKind
    deployment_mode: Literal["pillarmesh_cloud"] = "pillarmesh_cloud"
    region: str
    capacity_profile: Literal["mvp-fixed"] = "mvp-fixed"
    capability_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: WarehouseBindingState
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    provisioned_at: datetime | None = None

    @field_validator("created_at", "updated_at", "provisioned_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)
