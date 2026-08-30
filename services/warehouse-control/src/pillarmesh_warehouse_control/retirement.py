from __future__ import annotations

from dataclasses import dataclass

from pillarmesh_contract_model import digest

from .evidence import WarehouseRetirementEvidence
from .private_state import PrivateWarehouseResource, WarehouseResourceCleanupStatus


@dataclass(frozen=True, slots=True)
class WarehouseRetirementResourceSnapshot:
    resource_inventory_digest: str
    cleanup_disposition_digest: str
    retention_policy_digest: str
    completed_resource_count: int
    retained_resource_count: int
    cleanup_failed_resource_count: int
    pending_resource_count: int


def canonical_retirement_resource_snapshot(
    resources: tuple[PrivateWarehouseResource, ...],
) -> WarehouseRetirementResourceSnapshot:
    """Digest private resource state without copying private values into public evidence."""
    ordered_resources = sorted(
        resources,
        key=lambda resource: (
            resource.binding_revision,
            resource.operation_id,
            resource.resource_id,
        ),
    )
    return WarehouseRetirementResourceSnapshot(
        resource_inventory_digest=digest(
            {
                "domain": "warehouse_retirement_resource_inventory_v1",
                "resources": [
                    {
                        "binding_revision": resource.binding_revision,
                        "operation_id": resource.operation_id,
                        "resource_id": resource.resource_id,
                        "resource_kind": resource.resource_kind,
                        "provider_resource_handle": resource.provider_resource_handle,
                        "parent_resource_handle": resource.parent_resource_handle,
                        "creation_state": resource.creation_state,
                        "created_at": resource.created_at,
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        cleanup_disposition_digest=digest(
            {
                "domain": "warehouse_retirement_cleanup_disposition_v1",
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "cleanup_status": resource.cleanup_status,
                        "cleanup_failure_classification": (resource.cleanup_failure_classification),
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        retention_policy_digest=digest(
            {
                "domain": "warehouse_retirement_retention_policy_v1",
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "retention_deadline": resource.retention_deadline,
                    }
                    for resource in ordered_resources
                ],
            }
        ),
        completed_resource_count=sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.COMPLETE
            for resource in resources
        ),
        retained_resource_count=sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.RETAINED
            for resource in resources
        ),
        cleanup_failed_resource_count=sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.FAILED
            for resource in resources
        ),
        pending_resource_count=sum(
            resource.cleanup_status is WarehouseResourceCleanupStatus.PENDING
            for resource in resources
        ),
    )


def retirement_evidence_mismatch(
    evidence: WarehouseRetirementEvidence,
    snapshot: WarehouseRetirementResourceSnapshot,
) -> str | None:
    comparisons = (
        (
            evidence.resource_inventory_digest,
            snapshot.resource_inventory_digest,
            "resource inventory digest",
        ),
        (
            evidence.cleanup_disposition_digest,
            snapshot.cleanup_disposition_digest,
            "cleanup disposition digest",
        ),
        (
            evidence.retention_policy_digest,
            snapshot.retention_policy_digest,
            "retention policy digest",
        ),
        (
            evidence.completed_resource_count,
            snapshot.completed_resource_count,
            "completed resource count",
        ),
        (
            evidence.retained_resource_count,
            snapshot.retained_resource_count,
            "retained resource count",
        ),
        (
            evidence.cleanup_failed_resource_count,
            snapshot.cleanup_failed_resource_count,
            "cleanup failed resource count",
        ),
    )
    for observed, expected, field in comparisons:
        if observed != expected:
            return field
    return None
