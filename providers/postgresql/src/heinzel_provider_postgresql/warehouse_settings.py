from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

POSTGRESQL_WAREHOUSE_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
POSTGRESQL_SERVER_VERSION_NUM = "180006"


class PostgreSQLWarehouseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    private_operation_directory: Path
    retention_period: timedelta = timedelta(hours=1)
    backup_chunk_bytes: int = Field(default=64 * 1024, ge=4 * 1024, le=1024 * 1024)
    capacity_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)

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
    "POSTGRESQL_SERVER_VERSION_NUM",
    "POSTGRESQL_WAREHOUSE_IMAGE",
    "PostgreSQLWarehouseSettings",
]
