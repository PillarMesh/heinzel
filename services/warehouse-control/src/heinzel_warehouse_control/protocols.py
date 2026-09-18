from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol

from heinzel_contract_model import ArtifactModel
from pydantic import ConfigDict, Field, field_validator

from .errors import WarehouseProviderError
from .evidence import (
    WarehouseRestoreVerification,
    WarehouseResumeValidationEvidence,
    WarehouseRetirementEvidence,
    WarehouseValidationEvidence,
)
from .models import (
    EngineKind,
    WarehouseBinding,
    WarehouseFailureClassification,
)
from .private_state import (
    PrivateWarehouseOperation,
    PrivateWarehouseResource,
    WarehouseResourceCleanupStatus,
)


class WarehouseProvisionResult(ArtifactModel):
    model_config = ConfigDict(strict=True, revalidate_instances="always")

    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    engine_kind: EngineKind
    private_resource_handle: str = Field(min_length=1)
    provider_build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("tenant_id", "binding_id", "operation_id", "private_resource_handle")
    @classmethod
    def requires_nonempty_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("warehouse provision result value must not be empty")
        return value


class InitialWarehouseValidationResult(ArtifactModel):
    result_kind: Literal["initial"] = "initial"
    evidence: WarehouseValidationEvidence
    restore_verification: WarehouseRestoreVerification


class ResumeWarehouseValidationResult(ArtifactModel):
    result_kind: Literal["resume"] = "resume"
    evidence: WarehouseResumeValidationEvidence


type WarehouseValidationResult = Annotated[
    InitialWarehouseValidationResult | ResumeWarehouseValidationResult,
    Field(discriminator="result_kind"),
]


class WarehouseResourceRecorder(Protocol):
    def load_resources(
        self, tenant_id: str, binding_id: str
    ) -> tuple[PrivateWarehouseResource, ...]: ...

    def record_planned(self, resource: PrivateWarehouseResource) -> None: ...

    def reopen_resources_for_recreation(
        self,
        expected_resources: tuple[PrivateWarehouseResource, ...],
        *,
        reopened_at: datetime,
    ) -> tuple[PrivateWarehouseResource, ...]: ...

    def mark_created(
        self,
        tenant_id: str,
        resource_id: str,
        provider_resource_handle: str,
    ) -> None: ...

    def mark_ambiguous(self, tenant_id: str, resource_id: str) -> None: ...

    def record_cleanup(
        self,
        tenant_id: str,
        resource_id: str,
        status: WarehouseResourceCleanupStatus,
        classification: WarehouseFailureClassification | None,
    ) -> None: ...

    def record_cleanup_batch(self, resources: tuple[PrivateWarehouseResource, ...]) -> None: ...


class WarehouseProvider(Protocol):
    engine_kind: EngineKind

    def provision(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult: ...

    def reconcile(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseProvisionResult: ...

    def validate(
        self,
        binding: WarehouseBinding,
        operation: PrivateWarehouseOperation,
        *,
        resume: bool,
    ) -> WarehouseValidationResult: ...

    def suspend(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None: ...

    def resume(self, binding: WarehouseBinding, operation: PrivateWarehouseOperation) -> None: ...

    def retire(
        self, binding: WarehouseBinding, operation: PrivateWarehouseOperation
    ) -> WarehouseRetirementEvidence: ...


__all__ = [
    "InitialWarehouseValidationResult",
    "ResumeWarehouseValidationResult",
    "WarehouseProvider",
    "WarehouseProviderError",
    "WarehouseProvisionResult",
    "WarehouseResourceRecorder",
    "WarehouseValidationResult",
]
