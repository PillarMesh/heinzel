from __future__ import annotations

from typing import Literal

from .models import WarehouseFailureClassification

type WarehouseProviderOperation = Literal[
    "provision", "reconcile", "validate", "suspend", "resume", "retire"
]


class WarehousePersistenceError(RuntimeError):
    pass


class WarehouseOperationConflictError(WarehousePersistenceError):
    pass


class WarehouseValidationConflictError(WarehousePersistenceError):
    pass


class WarehouseAdmissionError(RuntimeError):
    pass


class WarehouseProviderError(RuntimeError):
    def __init__(
        self,
        *,
        operation: WarehouseProviderOperation,
        classification: WarehouseFailureClassification,
    ) -> None:
        super().__init__(f"warehouse provider {operation} failed: {classification.value}")
        self.operation = operation
        self.classification = classification


class WarehouseSecretStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("warehouse operation secret storage is unavailable")


class WarehouseSecretRetiredError(WarehouseSecretStorageError):
    pass
