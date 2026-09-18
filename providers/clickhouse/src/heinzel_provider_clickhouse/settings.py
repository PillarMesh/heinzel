from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLICKHOUSE_WAREHOUSE_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
CLICKHOUSE_SERVER_VERSION = "25.8.32.4"


class ClickHouseWarehouseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    private_operation_directory: Path
    retention_period: timedelta = timedelta(hours=1)
    backup_chunk_bytes: int = Field(default=64 * 1024, ge=4 * 1024, le=1024 * 1024)
    capacity_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    readiness_timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    @field_validator("private_operation_directory")
    @classmethod
    def requires_absolute_nonsymlink_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or value.is_symlink():
            raise ValueError("private operation directory must be an absolute non-symlink path")
        return value

    @field_validator("retention_period")
    @classmethod
    def requires_positive_retention_period(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("warehouse retention period must be positive")
        return value


__all__ = [
    "CLICKHOUSE_SERVER_VERSION",
    "CLICKHOUSE_WAREHOUSE_IMAGE",
    "ClickHouseWarehouseSettings",
]
