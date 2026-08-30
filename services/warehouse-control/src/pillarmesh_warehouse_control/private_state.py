from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

from pillarmesh_contract_model import ArtifactModel, canonical_bytes
from pydantic import Field, field_validator, model_validator

from .models import EngineKind, WarehouseFailureClassification


class WarehouseOperationKind(StrEnum):
    PROVISION = "provision"
    SUSPEND = "suspend"
    RESUME = "resume"
    RETIRE = "retire"


class WarehouseOperationStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WarehouseOperationPhase(StrEnum):
    CLAIMED = "claimed"
    RESOURCES_PLANNED = "resources_planned"
    PROVIDER_CREATED = "provider_created"
    VALIDATING = "validating"
    BACKUP_PLANNED = "backup_planned"
    BACKUP_CREATED = "backup_created"
    RESTORE_PLANNED = "restore_planned"
    RESTORE_CREATED = "restore_created"
    RESTORE_VERIFIED = "restore_verified"
    RESTORE_CLEANED = "restore_cleaned"
    VALIDATED = "validated"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    RETIREMENT_DISPOSITION_RECORDED = "retirement_disposition_recorded"
    RETIRED = "retired"


class WarehouseResourceKind(StrEnum):
    COMPOSE_PROJECT = "compose_project"
    WAREHOUSE_CONTAINER = "warehouse_container"
    PRIVATE_NETWORK = "private_network"
    WAREHOUSE_DATA_VOLUME = "warehouse_data_volume"
    PRIVATE_DIRECTORY = "private_directory"
    CREDENTIAL_FILE = "credential_file"
    HOST_PORT_FILE = "host_port_file"
    TLS_PRIVATE_KEY = "tls_private_key"
    TLS_CERTIFICATE = "tls_certificate"
    BACKUP_ARTIFACT = "backup_artifact"
    BACKUP_STAGING_FILE = "backup_staging_file"
    BACKUP_ENCRYPTION_KEY = "backup_encryption_key"
    BACKUP_RETIREMENT_JOURNAL = "backup_retirement_journal"
    RESTORE_COMPOSE_PROJECT = "restore_compose_project"
    RESTORE_CONTAINER = "restore_container"
    RESTORE_PRIVATE_NETWORK = "restore_private_network"
    RESTORE_DATA_VOLUME = "restore_data_volume"
    RESTORE_VERIFICATION_RECEIPT = "restore_verification_receipt"


class WarehouseResourceCreationState(StrEnum):
    PLANNED = "planned"
    CREATED = "created"
    AMBIGUOUS = "ambiguous"
    ABSENT = "absent"


class WarehouseResourceCleanupStatus(StrEnum):
    PENDING = "pending"
    RETAINED = "retained"
    COMPLETE = "complete"
    FAILED = "failed"


def _require_nonempty(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be empty")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


class PrivateWarehouseOperation(ArtifactModel):
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    operation_kind: WarehouseOperationKind
    engine_kind: EngineKind
    status: WarehouseOperationStatus
    phase: WarehouseOperationPhase
    provider_resource_handle: str | None = None
    failure_classification: WarehouseFailureClassification | None = None
    started_at: datetime
    updated_at: datetime

    @field_validator("tenant_id", "binding_id", "operation_id", "provider_resource_handle")
    @classmethod
    def requires_nonempty_identifiers(cls, value: str | None) -> str | None:
        return _require_nonempty(value) if value is not None else None

    @field_validator("started_at", "updated_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_consistent_failure_and_timestamps(self) -> Self:
        if self.updated_at < self.started_at:
            raise ValueError("updated_at must not precede started_at")
        if self.status is WarehouseOperationStatus.FAILED and self.failure_classification is None:
            raise ValueError("failed operation requires a failure classification")
        if (
            self.status is not WarehouseOperationStatus.FAILED
            and self.failure_classification is not None
        ):
            raise ValueError("non-failed operation must not carry a failure classification")
        return self


class PrivateWarehouseResource(ArtifactModel):
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    binding_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    state_revision: int = Field(default=0, ge=0)
    resource_kind: WarehouseResourceKind
    provider_resource_handle: str = Field(min_length=1)
    parent_resource_handle: str | None = None
    creation_state: WarehouseResourceCreationState
    retention_deadline: datetime
    cleanup_status: WarehouseResourceCleanupStatus
    cleanup_failure_classification: WarehouseFailureClassification | None = None
    created_at: datetime
    updated_at: datetime

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PrivateWarehouseResource):
            return NotImplemented
        return self._semantic_bytes() == other._semantic_bytes()

    def __hash__(self) -> int:
        return hash(self._semantic_bytes())

    def _semantic_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(exclude={"state_revision"}))

    @field_validator(
        "tenant_id",
        "binding_id",
        "operation_id",
        "resource_id",
        "provider_resource_handle",
        "parent_resource_handle",
    )
    @classmethod
    def requires_nonempty_identifiers(cls, value: str | None) -> str | None:
        return _require_nonempty(value) if value is not None else None

    @field_validator("retention_deadline", "created_at", "updated_at")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def requires_consistent_cleanup_and_timestamps(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.retention_deadline < self.created_at:
            raise ValueError("retention_deadline must not precede created_at")
        if (
            self.cleanup_status is WarehouseResourceCleanupStatus.FAILED
            and self.cleanup_failure_classification is None
        ):
            raise ValueError("failed cleanup requires a failure classification")
        if (
            self.cleanup_status is not WarehouseResourceCleanupStatus.FAILED
            and self.cleanup_failure_classification is not None
        ):
            raise ValueError("non-failed cleanup must not carry a failure classification")
        return self
