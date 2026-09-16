from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .delegation import (
    AgentInvocationGuard,
    CurrentAnswerPolicyResolver,
    CurrentDelegationAuthorizer,
    CurrentEntitlementAuthorizer,
    DelegatedAgentContext,
    FixedWindowPrincipalRateLimiter,
)
from .settings import AppSettings
from .tools import AgentCatalogBoundary, AgentImpactBoundary, AgentInterface, AgentRequestBoundary


class AgentInterfaceConfigurationError(RuntimeError):
    def __init__(self, *, owner: str, interface: str) -> None:
        self.owner = owner
        self.interface = interface
        super().__init__(f"{owner} does not provide required interface: {interface}")


@dataclass(frozen=True, slots=True)
class AgentInterfacePorts:
    delegations: CurrentDelegationAuthorizer
    entitlements: CurrentEntitlementAuthorizer
    policies: CurrentAnswerPolicyResolver
    requests: AgentRequestBoundary
    catalog: AgentCatalogBoundary
    impacts: AgentImpactBoundary


def build_application(
    settings: AppSettings,
    ports: AgentInterfacePorts,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentInterface:
    invocation_clock = clock or _utc_now
    guard = AgentInvocationGuard(
        delegations=ports.delegations,
        entitlements=ports.entitlements,
        policies=ports.policies,
        rate_limiter=FixedWindowPrincipalRateLimiter(
            invocation_ceiling=settings.rate_invocation_ceiling,
            window=timedelta(seconds=settings.rate_window_seconds),
            maximum_principals=settings.maximum_rate_principals,
        ),
        clock=invocation_clock,
    )
    return AgentInterface(
        context=DelegatedAgentContext(
            tenant_id=settings.tenant_id,
            delegation_id=settings.delegation_id,
            principal_ref=settings.principal_ref,
            agent_client_ref=settings.agent_client_ref,
            purpose=settings.purpose,
        ),
        guard=guard,
        requests=ports.requests,
        catalog=ports.catalog,
        impacts=ports.impacts,
    )


def build_production_application(_settings: AppSettings) -> AgentInterface:
    # Session settings do not confer authority. Deployment must inject durable current-authority
    # resolvers and every owning-service read/write port before the stdio server can start.
    raise AgentInterfaceConfigurationError(
        owner="context-exposure deployment",
        interface="configured current-authority and owning-service ports",
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
