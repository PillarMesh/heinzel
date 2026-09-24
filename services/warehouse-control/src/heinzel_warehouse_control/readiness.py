from __future__ import annotations

from typing import Protocol

from .errors import WarehouseAdmissionError
from .evidence import WarehouseValidationEvidence
from .models import EncryptionAtRestDisposition, WarehouseValidationProfile


class WarehouseReadinessPolicy(Protocol):
    def admit(self, evidence: WarehouseValidationEvidence) -> None: ...


class ProductionWarehouseReadinessPolicy:
    def admit(self, evidence: WarehouseValidationEvidence) -> None:
        if (
            evidence.validation_profile is not WarehouseValidationProfile.PRODUCTION
            or evidence.encryption_at_rest_disposition is not EncryptionAtRestDisposition.PROVEN
        ):
            raise WarehouseAdmissionError(
                "production warehouse validation evidence is insufficient"
            )


class LocalAcceptanceWarehouseReadinessPolicy:
    def admit(self, evidence: WarehouseValidationEvidence) -> None:
        if (
            evidence.validation_profile is not WarehouseValidationProfile.LOCAL_ACCEPTANCE
            or evidence.encryption_at_rest_disposition
            is not EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE
        ):
            raise WarehouseAdmissionError(
                "local acceptance warehouse validation evidence is insufficient"
            )
