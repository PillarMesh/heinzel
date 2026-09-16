from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import CurrentEntitlementSnapshot, EnterpriseEntitlementAssertion
from .repository import SQLiteEntitlementRepository


class ConnectedPolicyAuthorityUnavailable(RuntimeError):
    pass


class ConnectedPolicyAuthorityIntegrityError(RuntimeError):
    pass


class EntitlementResolutionDenied(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AuthenticatedConnectedPolicyAuthority(Protocol):
    def read_current(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> EnterpriseEntitlementAssertion | None: ...


class CurrentEntitlementResolver:
    def __init__(
        self,
        *,
        repository: SQLiteEntitlementRepository,
        connected_authority: AuthenticatedConnectedPolicyAuthority,
        connected_authority_ref: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._connected_authority = connected_authority
        self._connected_authority_ref = connected_authority_ref
        self._clock = clock

    def resolve_current(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> CurrentEntitlementSnapshot:
        now = self._now()
        try:
            assertion = self._connected_authority.read_current(
                tenant_id=tenant_id,
                principal_ref=principal_ref,
                purpose_digest=purpose_digest,
            )
        except ConnectedPolicyAuthorityUnavailable as error:
            raise EntitlementResolutionDenied("authority_unavailable") from error
        except ConnectedPolicyAuthorityIntegrityError as error:
            raise EntitlementResolutionDenied("authority_invalid") from error
        if assertion is None:
            raise EntitlementResolutionDenied("authority_missing")
        if (
            assertion.tenant_id != tenant_id
            or assertion.principal_ref != principal_ref
            or assertion.purpose_digest != purpose_digest
            or assertion.provenance.connected_authority_ref != self._connected_authority_ref
        ):
            raise EntitlementResolutionDenied("authority_scope_mismatch")

        observation = self._repository.record_observation(assertion, recorded_at=now)
        if observation.decision == "revoked":
            raise EntitlementResolutionDenied("entitlement_revoked")
        if observation.effective_at > now:
            raise EntitlementResolutionDenied("entitlement_not_yet_effective")
        if observation.valid_until <= now:
            raise EntitlementResolutionDenied("entitlement_expired")
        return self._repository.record_snapshot(observation, resolved_at=now)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return a timezone-aware UTC timestamp")
        return now.astimezone(UTC)
