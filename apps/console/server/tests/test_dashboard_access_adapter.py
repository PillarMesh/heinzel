from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Never

import pytest
from heinzel_access_control import AccessGrant, AccessGrantDenied
from heinzel_bi_control import DashboardAccessAuthorityError
from heinzel_console.governed_adapters import DashboardAccessControlAuthority
from heinzel_contract_model import ArtifactReference, digest

NOW = datetime(2026, 9, 15, 7, tzinfo=UTC)
PRODUCT = ArtifactReference(artifact_id="product-revenue", version=2, digest="a" * 64)


def _grant(**updates: object) -> AccessGrant:
    values: dict[str, object] = {
        "grant_id": "grant-dashboard-1",
        "tenant_id": "tenant-a",
        "request_id": "access-request-1",
        "revision": 4,
        "state": "active",
        "principal_ref": "principal:requester-a",
        "purpose": "Review regional revenue",
        "purpose_digest": digest("Review regional revenue"),
        "data_product_version_ref": PRODUCT,
        "fields": ("region", "total_revenue"),
        "classification_refs": (),
        "access_mode": "dashboard",
        "permissions": ("dashboard", "view"),
        "effective_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
        "policy_revision": 2,
        "admission_receipt_ref": ArtifactReference(
            artifact_id="admission-access-1", version=1, digest="b" * 64
        ),
        "entitlement_snapshot_digest": "c" * 64,
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW,
    }
    values.update(updates)
    return AccessGrant.model_validate(values)


class _AccessCommands:
    def __init__(self, result: AccessGrant | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def apply(self, *, tenant_id: str, request_id: str, grant_id: str) -> Never:
        raise AssertionError("dashboard authorization must not apply a grant")

    def authorize(self, **values: object) -> AccessGrant:
        self.calls.append(values)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_dashboard_authority_projects_the_current_access_grant() -> None:
    grant = _grant()
    commands = _AccessCommands(grant)
    authority = DashboardAccessControlAuthority(commands=commands, clock=lambda: NOW)

    authorization = authority.authorize(
        tenant_id=grant.tenant_id,
        authorization_id=grant.grant_id,
        principal_ref=grant.principal_ref,
        purpose=grant.purpose,
        dashboard_id="dashboard-revenue",
        dashboard_version=3,
        data_product_version_ref=PRODUCT,
    )

    assert authorization is not None
    assert authorization.access_request_id == grant.request_id
    assert authorization.authorization_id == grant.grant_id
    assert authorization.dashboard_id == "dashboard-revenue"
    assert authorization.dashboard_version == 3
    assert authorization.verified_at == NOW
    assert commands.calls == [
        {
            "tenant_id": grant.tenant_id,
            "grant_id": grant.grant_id,
            "principal_ref": grant.principal_ref,
            "purpose": grant.purpose,
            "permission": "dashboard",
            "product_version_ref": PRODUCT,
        }
    ]


def test_dashboard_authority_denies_without_disclosing_the_grant() -> None:
    commands = _AccessCommands(AccessGrantDenied("private denial detail"))
    authority = DashboardAccessControlAuthority(commands=commands, clock=lambda: NOW)

    assert (
        authority.authorize(
            tenant_id="tenant-a",
            authorization_id="grant-dashboard-1",
            principal_ref="principal:requester-a",
            purpose="Review regional revenue",
            dashboard_id="dashboard-revenue",
            dashboard_version=3,
            data_product_version_ref=PRODUCT,
        )
        is None
    )


def test_dashboard_authority_classifies_an_unavailable_access_service() -> None:
    commands = _AccessCommands(RuntimeError("private service detail"))
    authority = DashboardAccessControlAuthority(commands=commands, clock=lambda: NOW)

    with pytest.raises(DashboardAccessAuthorityError, match="access-control unavailable"):
        authority.authorize(
            tenant_id="tenant-a",
            authorization_id="grant-dashboard-1",
            principal_ref="principal:requester-a",
            purpose="Review regional revenue",
            dashboard_id="dashboard-revenue",
            dashboard_version=3,
            data_product_version_ref=PRODUCT,
        )
