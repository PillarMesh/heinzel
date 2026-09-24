from __future__ import annotations

from typing import Literal

from heinzel_provider_sdk.errors import AcquisitionProviderKind
from pydantic import BaseModel, ConfigDict, Field

from .models import SourceAccountMode

_ENDPOINT_REFERENCE_PATTERN = r"^endpoint-ref:[0-9a-f]{64}$"
_CREDENTIAL_REFERENCE_PATTERN = r"^credential-ref:[0-9a-f]{64}$"


class PrivateSourceCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    connection_handle: str = Field(min_length=1)
    account_mode: SourceAccountMode
    credential_revision: int = Field(ge=1)
    endpoint_reference: str = Field(pattern=_ENDPOINT_REFERENCE_PATTERN, repr=False)
    credential_reference: str = Field(pattern=_CREDENTIAL_REFERENCE_PATTERN, repr=False)
