from datetime import UTC, datetime, timedelta, timezone

import pytest
from pillarmesh_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from pydantic import ValidationError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def binding(**updates: object) -> WarehouseBinding:
    values: dict[str, object] = {
        "binding_id": "whb-test",
        "tenant_id": "tenant-a",
        "engine_kind": EngineKind.POSTGRESQL,
        "region": "us-west",
        "capacity_profile": "mvp-fixed",
        "capability_profile_digest": "a" * 64,
        "lifecycle_state": WarehouseBindingState.DRAFT,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return WarehouseBinding.model_validate(values)


@pytest.mark.parametrize(
    ("field", "timestamp"),
    (
        ("created_at", datetime(2026, 8, 17, 12)),
        ("updated_at", datetime(2026, 8, 17, 12)),
        ("provisioned_at", datetime(2026, 8, 17, 12)),
        ("created_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("updated_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
        ("provisioned_at", datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1)))),
    ),
)
def test_binding_rejects_non_utc_durable_timestamps(field: str, timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        binding(**{field: timestamp})
