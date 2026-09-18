from __future__ import annotations

from heinzel_provider_sdk.bi import dashboard_external_key


def test_dashboard_external_key_is_deterministic_and_title_independent() -> None:
    first = dashboard_external_key(tenant_id="tenant-a", dashboard_id="revenue", version=3)
    replay = dashboard_external_key(tenant_id="tenant-a", dashboard_id="revenue", version=3)

    assert first == replay
    assert first.startswith("pm-dashboard-")
    assert first != dashboard_external_key(tenant_id="tenant-a", dashboard_id="revenue", version=4)
    assert first != dashboard_external_key(tenant_id="tenant-b", dashboard_id="revenue", version=3)
