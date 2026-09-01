from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pillarmesh_provider_sdk.errors import AcquisitionProviderKind

from .models import SourceAccountMode, SourceBindingValidationEvidence, SourceConnectionBinding
from .private_state import PrivateSourceCapability


class SourceSecretResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        provider_kind: AcquisitionProviderKind,
        connection_handle: str,
        account_mode: SourceAccountMode,
        credential_revision: int,
    ) -> PrivateSourceCapability: ...


class SourceCapabilityProbe(Protocol):
    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> SourceBindingValidationEvidence: ...
