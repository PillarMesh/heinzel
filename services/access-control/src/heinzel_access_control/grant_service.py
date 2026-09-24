from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from heinzel_contract_model import ArtifactReference, digest

from .grant_repository import SQLiteAccessGrantRepository
from .models import AccessGrant, CurrentEntitlementSnapshot, EntitlementPermission
from .resolver import (
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
    EntitlementResolutionDenied,
)


class AccessGrantDenied(RuntimeError):
    pass


class CurrentGrantEntitlementResolver(Protocol):
    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot: ...


class AccessGrantAuthorizationService:
    def __init__(
        self,
        *,
        grants: SQLiteAccessGrantRepository,
        entitlements: CurrentGrantEntitlementResolver,
        clock: Callable[[], datetime],
    ) -> None:
        self._grants = grants
        self._entitlements = entitlements
        self._clock = clock

    def authorize(
        self,
        *,
        tenant_id: str,
        grant_id: str,
        principal_ref: str,
        purpose: str,
        permission: EntitlementPermission,
        product_version_ref: ArtifactReference,
    ) -> AccessGrant:
        grant = self._grants.load_current(tenant_id, grant_id)
        if grant is None:
            raise AccessGrantDenied("access grant is unavailable")
        if grant.state != "active":
            raise AccessGrantDenied("access grant is not active")
        now = self._now()
        if now < grant.effective_at:
            raise AccessGrantDenied("access grant is not effective")
        if now >= grant.expires_at:
            raise AccessGrantDenied("access grant has expired")
        purpose_digest = digest(purpose)
        if (
            grant.principal_ref != principal_ref
            or grant.purpose_digest != purpose_digest
            or grant.data_product_version_ref != product_version_ref
            or permission not in grant.permissions
        ):
            raise AccessGrantDenied("access grant scope does not match")
        try:
            current = self._entitlements.resolve_current(
                tenant_id=tenant_id,
                principal_ref=principal_ref,
                purpose_digest=purpose_digest,
            )
        except (
            ConnectedPolicyAuthorityIntegrityError,
            ConnectedPolicyAuthorityUnavailable,
            EntitlementResolutionDenied,
        ) as error:
            raise AccessGrantDenied("current entitlement is unavailable") from error
        if current.snapshot_digest != grant.entitlement_snapshot_digest:
            raise AccessGrantDenied("current entitlement changed")
        if (
            product_version_ref not in current.product_version_refs
            or permission not in current.permissions
            or not (current.effective_at <= now < current.valid_until)
        ):
            raise AccessGrantDenied("current entitlement does not authorize access")
        return grant

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return timezone-aware UTC")
        return now.astimezone(UTC)
