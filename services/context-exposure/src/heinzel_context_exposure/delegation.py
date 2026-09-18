from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal, Protocol, Self

from heinzel_access_control import CurrentEntitlementSnapshot, EntitlementResolutionDenied
from heinzel_contract_model import digest
from heinzel_request_management import AnswerScopePolicy
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

type DelegationDecision = Literal["active", "revoked"]
type AgentAccessDenialReason = Literal[
    "authority_unavailable",
    "authority_invalid",
    "delegation_missing",
    "delegation_revoked",
    "delegation_not_yet_effective",
    "delegation_expired",
    "delegation_scope_mismatch",
    "entitlement_not_yet_effective",
    "entitlement_expired",
    "entitlement_not_current",
    "entitlement_scope_mismatch",
    "policy_missing",
    "policy_not_yet_effective",
    "policy_expired",
    "policy_scope_mismatch",
    "agent_access_denied",
    "model_disclosure_denied",
    "rate_ceiling_exceeded",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class DelegatedAgentContext(_StrictModel):
    tenant_id: str = Field(min_length=1, max_length=512)
    delegation_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    agent_client_ref: str = Field(min_length=1, max_length=512)
    purpose: str = Field(min_length=1, max_length=512)


class CurrentDelegationAssertion(_StrictModel):
    schema_version: Literal["1"] = "1"
    delegation_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=512)
    principal_ref: str = Field(min_length=1, max_length=512)
    agent_client_ref: str = Field(min_length=1, max_length=512)
    decision: DelegationDecision
    authority_ref: str = Field(min_length=1, max_length=512)
    authority_revision: int = Field(gt=0)
    effective_at: datetime
    valid_until: datetime

    @field_validator("effective_at", "valid_until")
    @classmethod
    def timestamps_are_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> Self:
        if self.valid_until <= self.effective_at:
            raise ValueError("valid_until must be after effective_at")
        return self


class AuthorizedAgentContext(_StrictModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    delegation_id: str = Field(min_length=1)
    principal_ref: str = Field(min_length=1)
    agent_client_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=512)
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    delegation: CurrentDelegationAssertion
    entitlement: CurrentEntitlementSnapshot
    policy: AnswerScopePolicy
    entitlement_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def authorized_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "authorized_at")

    @model_validator(mode="after")
    def authority_is_bound_to_context(self) -> Self:
        identity = (self.tenant_id, self.principal_ref)
        if (
            (self.delegation.tenant_id, self.delegation.principal_ref) != identity
            or (self.entitlement.tenant_id, self.entitlement.principal_ref) != identity
            or (self.policy.tenant_id, self.principal_ref) != identity
            or self.delegation.delegation_id != self.delegation_id
            or self.delegation.agent_client_ref != self.agent_client_ref
            or self.entitlement.purpose_digest != self.purpose_digest
            or self.purpose not in self.policy.purposes
            or self.principal_ref not in self.policy.principal_scope
            or self.entitlement.snapshot_digest != self.entitlement_snapshot_digest
            or self.policy.canonical_digest() != self.policy_digest
            or self.delegation.decision != "active"
            or self.policy.agent_access != "allowed"
        ):
            raise ValueError("authorized agent context authority mismatch")
        return self


class AgentAuthorityUnavailable(RuntimeError):
    pass


class AgentAccessDenied(RuntimeError):
    def __init__(self, reason: AgentAccessDenialReason) -> None:
        self.reason = reason
        super().__init__(reason)


class CurrentDelegationAuthorizer(Protocol):
    def resolve_current(
        self,
        *,
        tenant_id: str,
        delegation_id: str,
        principal_ref: str,
        agent_client_ref: str,
    ) -> CurrentDelegationAssertion | None: ...


class CurrentEntitlementAuthorizer(Protocol):
    def resolve_current(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> CurrentEntitlementSnapshot: ...


class CurrentAnswerPolicyResolver(Protocol):
    def resolve_current(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        purpose: str,
    ) -> AnswerScopePolicy | None: ...


class PrincipalRateLimiter(Protocol):
    def consume(self, *, tenant_id: str, principal_ref: str, invoked_at: datetime) -> bool: ...


class FixedWindowPrincipalRateLimiter:
    """Bound memory without evicting a live principal and reopening its ceiling."""

    def __init__(
        self,
        *,
        invocation_ceiling: int,
        window: timedelta,
        maximum_principals: int,
    ) -> None:
        window_microseconds = window // timedelta(microseconds=1)
        if invocation_ceiling < 1:
            raise ValueError("invocation ceiling must be positive")
        if window_microseconds < 1:
            raise ValueError("rate window must be positive")
        if maximum_principals < 1:
            raise ValueError("maximum principals must be positive")
        self._invocation_ceiling = invocation_ceiling
        self._window = window
        self._window_microseconds = window_microseconds
        self._maximum_principals = maximum_principals
        self._entries: dict[tuple[str, str], tuple[datetime, int]] = {}
        self._lock = RLock()

    def consume(self, *, tenant_id: str, principal_ref: str, invoked_at: datetime) -> bool:
        invoked_at = _utc(invoked_at, "invoked_at")
        window_start = self._window_start(invoked_at)
        identity = (tenant_id, principal_ref)
        with self._lock:
            self._entries = {
                key: value
                for key, value in self._entries.items()
                if value[0] + self._window > invoked_at
            }
            existing = self._entries.get(identity)
            if existing is None:
                if len(self._entries) >= self._maximum_principals:
                    return False
                self._entries[identity] = (window_start, 1)
                return True
            existing_window, count = existing
            if existing_window != window_start:
                self._entries[identity] = (window_start, 1)
                return True
            if count >= self._invocation_ceiling:
                return False
            self._entries[identity] = (window_start, count + 1)
            return True

    def _window_start(self, invoked_at: datetime) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed_microseconds = (invoked_at - epoch) // timedelta(microseconds=1)
        start_microseconds = (
            elapsed_microseconds // self._window_microseconds * self._window_microseconds
        )
        return epoch + timedelta(microseconds=start_microseconds)


class AgentInvocationGuard:
    def __init__(
        self,
        *,
        delegations: CurrentDelegationAuthorizer,
        entitlements: CurrentEntitlementAuthorizer,
        policies: CurrentAnswerPolicyResolver,
        rate_limiter: PrincipalRateLimiter,
        clock: Callable[[], datetime],
    ) -> None:
        self._delegations = delegations
        self._entitlements = entitlements
        self._policies = policies
        self._rate_limiter = rate_limiter
        self._clock = clock

    def authorize(self, context: DelegatedAgentContext) -> AuthorizedAgentContext:
        context = DelegatedAgentContext.model_validate(
            context.model_dump(mode="python"), strict=True
        )
        now = _utc(self._clock(), "clock")
        purpose_digest = digest(context.purpose)
        delegation = self._delegation(context, now)
        entitlement = self._entitlement(context, purpose_digest, now)
        policy = self._policy(context, now)
        if not self._rate_limiter.consume(
            tenant_id=context.tenant_id,
            principal_ref=context.principal_ref,
            invoked_at=now,
        ):
            raise AgentAccessDenied("rate_ceiling_exceeded")
        return AuthorizedAgentContext(
            tenant_id=context.tenant_id,
            delegation_id=context.delegation_id,
            principal_ref=context.principal_ref,
            agent_client_ref=context.agent_client_ref,
            purpose=context.purpose,
            purpose_digest=purpose_digest,
            delegation=delegation,
            entitlement=entitlement,
            policy=policy,
            entitlement_snapshot_digest=entitlement.snapshot_digest,
            policy_digest=policy.canonical_digest(),
            authorized_at=now,
        )

    def _delegation(
        self, context: DelegatedAgentContext, now: datetime
    ) -> CurrentDelegationAssertion:
        try:
            raw = self._delegations.resolve_current(
                tenant_id=context.tenant_id,
                delegation_id=context.delegation_id,
                principal_ref=context.principal_ref,
                agent_client_ref=context.agent_client_ref,
            )
        except AgentAuthorityUnavailable as error:
            raise AgentAccessDenied("authority_unavailable") from error
        if raw is None:
            raise AgentAccessDenied("delegation_missing")
        try:
            delegation = CurrentDelegationAssertion.model_validate(
                _authority_payload(raw), strict=True
            )
        except (TypeError, ValidationError, ValueError) as error:
            raise AgentAccessDenied("authority_invalid") from error
        if (
            delegation.tenant_id != context.tenant_id
            or delegation.delegation_id != context.delegation_id
            or delegation.principal_ref != context.principal_ref
            or delegation.agent_client_ref != context.agent_client_ref
        ):
            raise AgentAccessDenied("delegation_scope_mismatch")
        if delegation.decision == "revoked":
            raise AgentAccessDenied("delegation_revoked")
        if delegation.effective_at > now:
            raise AgentAccessDenied("delegation_not_yet_effective")
        if delegation.valid_until <= now:
            raise AgentAccessDenied("delegation_expired")
        return delegation

    def _entitlement(
        self, context: DelegatedAgentContext, purpose_digest: str, now: datetime
    ) -> CurrentEntitlementSnapshot:
        try:
            raw = self._entitlements.resolve_current(
                tenant_id=context.tenant_id,
                principal_ref=context.principal_ref,
                purpose_digest=purpose_digest,
            )
        except AgentAuthorityUnavailable as error:
            raise AgentAccessDenied("authority_unavailable") from error
        except EntitlementResolutionDenied as error:
            if str(error) == "authority_unavailable":
                raise AgentAccessDenied("authority_unavailable") from error
            if str(error) == "authority_invalid":
                raise AgentAccessDenied("authority_invalid") from error
            raise AgentAccessDenied("entitlement_not_current") from error
        try:
            entitlement = CurrentEntitlementSnapshot.model_validate(
                _authority_payload(raw), strict=True
            )
        except (TypeError, ValidationError, ValueError) as error:
            raise AgentAccessDenied("authority_invalid") from error
        if (
            entitlement.tenant_id != context.tenant_id
            or entitlement.principal_ref != context.principal_ref
            or entitlement.purpose_digest != purpose_digest
        ):
            raise AgentAccessDenied("entitlement_scope_mismatch")
        if entitlement.effective_at > now:
            raise AgentAccessDenied("entitlement_not_yet_effective")
        if entitlement.valid_until <= now:
            raise AgentAccessDenied("entitlement_expired")
        return entitlement

    def _policy(self, context: DelegatedAgentContext, now: datetime) -> AnswerScopePolicy:
        try:
            raw = self._policies.resolve_current(
                tenant_id=context.tenant_id,
                principal_ref=context.principal_ref,
                purpose=context.purpose,
            )
        except AgentAuthorityUnavailable as error:
            raise AgentAccessDenied("authority_unavailable") from error
        if raw is None:
            raise AgentAccessDenied("policy_missing")
        try:
            policy = AnswerScopePolicy.model_validate(_authority_payload(raw), strict=True)
        except (TypeError, ValidationError, ValueError) as error:
            raise AgentAccessDenied("authority_invalid") from error
        if (
            policy.tenant_id != context.tenant_id
            or context.principal_ref not in policy.principal_scope
            or context.purpose not in policy.purposes
        ):
            raise AgentAccessDenied("policy_scope_mismatch")
        if policy.valid_from > now:
            raise AgentAccessDenied("policy_not_yet_effective")
        if policy.valid_until <= now:
            raise AgentAccessDenied("policy_expired")
        if policy.agent_access != "allowed":
            raise AgentAccessDenied("agent_access_denied")
        return policy


def _authority_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value
