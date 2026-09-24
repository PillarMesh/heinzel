from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from heinzel_contract_model import canonical_bytes
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def _cursor_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Stripe event cursor contains a duplicate field")
        result[key] = value
    return result


class StripeEventCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    last_event_created: datetime
    last_event_id: str = Field(min_length=1, pattern=r"^evt_[A-Za-z0-9_]+$")
    api_version_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("last_event_created")
    @classmethod
    def requires_utc_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("last_event_created must be timezone-aware UTC")
        return value.astimezone(UTC)

    def to_payload(self) -> bytes:
        return canonical_bytes(self)

    @classmethod
    def from_payload(cls, payload: bytes) -> StripeEventCursor:
        try:
            raw = json.loads(payload, object_pairs_hook=_cursor_object)
            if not isinstance(raw, dict):
                raise ValueError("Stripe event cursor must be an object")
            timestamp = raw.get("last_event_created")
            if not isinstance(timestamp, str):
                raise ValueError("Stripe event cursor timestamp must be text")
            raw["last_event_created"] = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            cursor = cls.model_validate(raw, strict=True)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
            raise ValueError("Stripe event cursor failed integrity validation") from None
        if cursor.to_payload() != payload:
            raise ValueError("Stripe event cursor is not canonical")
        return cursor


@dataclass(frozen=True, slots=True)
class _StripeListPage:
    data: tuple[Mapping[str, object], ...]
    has_more: bool
    next_cursor: str | None
