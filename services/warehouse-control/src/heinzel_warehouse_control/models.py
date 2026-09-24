from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from heinzel_contract_model import ArtifactModel
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


class WarehousePrincipalClass(StrEnum):
    ADMINISTRATION = "administration"
    INGESTION_RUNTIME = "ingestion_runtime"
    TRANSFORMATION_RUNTIME = "transformation_runtime"
    ANSWER_RUNTIME = "answer_runtime"
    BACKUP_RESTORE = "backup_restore"
    CUSTOMER_SQL = "customer_sql"
    CATALOG = "catalog"
    BI = "bi"


class WarehouseValidationProfile(StrEnum):
    LOCAL_ACCEPTANCE = "local_acceptance"
    PRODUCTION = "production"


class EncryptionAtRestDisposition(StrEnum):
    PROVEN = "proven"
    DEFERRED_LOCAL_ACCEPTANCE = "deferred_local_acceptance"


class WarehouseFailureClassification(StrEnum):
    TRANSIENT_TRANSPORT = "transient_transport"
    TRANSIENT_UNAVAILABLE = "transient_unavailable"
    THROTTLED = "throttled"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"
    AUTHORIZATION_DENIED = "authorization_denied"
    STATEMENT_REJECTED = "statement_rejected"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    INTEGRITY_FAILURE = "integrity_failure"
    PERMANENT_CONFIGURATION = "permanent_configuration"


class WarehouseBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str
    tenant_id: str
    engine_kind: EngineKind
    deployment_mode: Literal["heinzel_cloud"] = "heinzel_cloud"
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
