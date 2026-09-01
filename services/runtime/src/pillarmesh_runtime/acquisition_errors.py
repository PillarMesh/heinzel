from __future__ import annotations

from typing import Literal

from pillarmesh_provider_sdk.acquisition_models import ResynchronizationReasonCode


class AcquisitionRuntimeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"acquisition failed: {reason_code}")


class AcquisitionStaleRevision(AcquisitionRuntimeError):
    pass


class AcquisitionOwnershipError(AcquisitionRuntimeError):
    pass


class AcquisitionContractError(AcquisitionRuntimeError):
    pass


class AcquisitionAuthorizationError(AcquisitionRuntimeError):
    pass


class AcquisitionThrottledError(AcquisitionRuntimeError):
    pass


class AcquisitionTransientError(AcquisitionRuntimeError):
    pass


class AcquisitionDriftError(AcquisitionRuntimeError):
    pass


class AcquisitionCursorExpiredError(AcquisitionRuntimeError):
    reason_code: ResynchronizationReasonCode

    def __init__(self, reason_code: ResynchronizationReasonCode) -> None:
        super().__init__(reason_code)


class AcquisitionCeilingExceeded(AcquisitionRuntimeError):
    def __init__(
        self,
        *,
        logical_object_ref: str,
        limit_kind: Literal["records", "encoded_bytes"],
        ceiling: int,
    ) -> None:
        self.logical_object_ref = logical_object_ref
        self.limit_kind = limit_kind
        self.ceiling = ceiling
        super().__init__(f"{limit_kind}_ceiling_exceeded:{logical_object_ref}")


class AcquisitionIntegrityError(AcquisitionRuntimeError):
    pass
