from datetime import UTC, datetime, timedelta
from typing import Literal

from pillarmesh_contract_model import ArtifactModel
from pydantic import Field, field_validator


class BusinessProcessManifest(ArtifactModel):
    schema_version: Literal["1"] = "1"
    process_name: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    participants: tuple[str, ...]
    outcomes: tuple[str, ...]
    entities: tuple[str, ...]
    events: tuple[str, ...]
    states: tuple[str, ...]
    rules: tuple[str, ...]
    source_references: tuple[str, ...]
    unresolved_questions: tuple[str, ...]


class ProcessPackageReceipt(ArtifactModel):
    package_id: str
    tenant_id: str
    version: int = Field(ge=1)
    media_type: Literal["text/markdown; charset=utf-8"]
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    uploader_id: str
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)
