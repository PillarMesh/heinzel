from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class WarehouseLifecycleCheckpoint(StrEnum):
    AFTER_PROVISIONING_TRANSITION = "after_provisioning_transition"
    AFTER_OPERATION_CLAIM = "after_operation_claim"
    AFTER_RESOURCE_PLAN = "after_resource_plan"
    AFTER_PROVIDER_CREATE = "after_provider_create"
    AFTER_VALIDATING_TRANSITION = "after_validating_transition"
    AFTER_BACKUP_RECORDED = "after_backup_recorded"
    AFTER_BACKUP_CREATED = "after_backup_created"
    AFTER_RESTORE_PLAN = "after_restore_plan"
    AFTER_RESTORE_CREATED = "after_restore_created"
    AFTER_RESTORE_VERIFIED = "after_restore_verified"
    AFTER_RESTORE_CLEANUP = "after_restore_cleanup"
    BEFORE_VALIDATION_ADMISSION = "before_validation_admission"
    AFTER_SUSPEND_EFFECT = "after_suspend_effect"
    AFTER_RESUME_EFFECT = "after_resume_effect"
    BEFORE_RESUME_ADMISSION = "before_resume_admission"
    AFTER_RETIREMENT_DISPOSITION = "after_retirement_disposition"
    BEFORE_RETIREMENT_ADMISSION = "before_retirement_admission"


type WarehouseFaultHook = Callable[[WarehouseLifecycleCheckpoint], None]


def noop_warehouse_fault_hook(checkpoint: WarehouseLifecycleCheckpoint) -> None:
    del checkpoint


__all__ = [
    "WarehouseFaultHook",
    "WarehouseLifecycleCheckpoint",
    "noop_warehouse_fault_hook",
]
