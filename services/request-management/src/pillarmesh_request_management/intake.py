from __future__ import annotations

from typing import Annotated

from pillarmesh_contract_model import ArtifactModel, digest
from pydantic import Field

from .models import DataAccessRequest, StakeholderQuestion


class RequestDigestMismatch(ValueError):
    pass


class RequestIntakeContent(ArtifactModel):
    """Content checksum input; identity and replay authority are deliberately separate."""

    title: str | None = Field(default=None, min_length=1, max_length=16_000)
    payload: Annotated[StakeholderQuestion | DataAccessRequest, Field(discriminator="request_type")]

    def verify_digest(self, declared_digest: str) -> None:
        if declared_digest != digest(self):
            raise RequestDigestMismatch("request digest does not match submitted content")
