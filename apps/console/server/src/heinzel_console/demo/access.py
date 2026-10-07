"""The applied access grants the demonstration's data access journey produces.

ADR-0007 keeps the applied `AccessGrant` in access-control: an admitted data access proposal
produces one, narrowed against a freshly resolved enterprise entitlement, and nothing else may
mint it. ADR-0010 is the other half -- publishing a dashboard confers no access -- so the
stakeholder who asked the question sees the dashboard built from their answer only once such a
grant exists. Until this
module was composed the demonstration wired no grant at all, which is why that stakeholder met a
404 and why its workspace reported data access as not delivered.

What is real here. The grant lifecycle, its append-only revisions, its state machine, its expiry
and revocation, and its provider-effect receipts are access-control's own
`AccessGrantApplicationService` over its own SQLite repository. The entitlement each grant is
narrowed against is resolved through the real `CurrentEntitlementResolver` from the
demonstration's local signed policy authority, so a grant is refused when that authority asserts
nothing, asserts less, or cannot be reached. The result surface's effect is the real
`AnswerResultAccessEffectProvider`, which durably records what the requester may read back.

What is not. `DemoAccessEffectProvider` stands in for the warehouse and Superset effect adapters
a deployment composes. It checks every command against the grant revision that issued it and
returns a classified receipt, and it enacts nothing: no database role is granted, no Superset
permission is set, because this demonstration hands out no warehouse roles and has no Superset to
reach. A grant it reports as applied is a real, durable Heinzel capability over provider state
that was never changed -- so the demonstration shows the governed lifecycle of access and not its
enforcement at an engine. That is the same posture `demo/collaborators.py` takes about the
identity provider and the policy decision point, and it is not a security boundary.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from heinzel_access_control import (
    AccessEffectSurface,
    AccessGrantApplicationService,
    AccessMode,
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
    CurrentEntitlementSnapshot,
    EntitlementPermission,
    EntitlementResolutionDenied,
    RequestManagementAccessDeliveryReader,
    RequestManagementAdmittedAccessProposalReader,
    SQLiteAccessGrantRepository,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    AccessEffectResult,
)
from heinzel_request_management import (
    AccessGrantAdmissionBinding,
    AccessGrantEffectTarget,
    AccessScopePreview,
    BoundSemanticReference,
    FulfillmentAuthorityError,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    InboxRequest,
    RequestManagementService,
    SQLiteFulfillmentRepository,
)
from heinzel_request_management.models import DataAccessRequest
from heinzel_runtime import AnswerResultAccessEffectProvider, AnswerResultAccessTarget

from .collaborators import (
    DEMO_ARCHITECT_ID,
    DEMO_ARCHITECT_PRINCIPAL_REF,
)
from .publication import DEMO_TENANT_ID

__all__ = [
    "DEMO_PRODUCT_OWNER_AUTHORITY_REF",
    "DemoAccessControl",
    "DemoAccessEffectProvider",
    "DemoAccessRevocationAuthority",
    "DemoAccessScopePreviewProvider",
    "DemoAccessScopeUnavailable",
    "DemoDataProductOwnerResolver",
    "DemoGrantAdmissionResolver",
    "build_demo_access_control",
    "demo_access_effect_targets",
    "demo_access_grant_id",
]

# Who approves a disclosure of the demonstration's product. The architect, because the
# demonstration has exactly two actors and the requester must not approve the product's release
# to themselves. A deployment resolves the product's own owner authority, and the owner and the
# architect are then different people whose approvals mean different things; here they are one
# actor, so the demonstration shows the approval requirement and not a separation of duties.
DEMO_PRODUCT_OWNER_AUTHORITY_REF = DEMO_ARCHITECT_PRINCIPAL_REF

# What a grant of each access mode must carry. These are exactly the sets
# `FulfillmentService._bind_access_grant_admission` requires, so a mode whose permissions were
# narrowed here would be refused at admission rather than silently granting less.
_GRANT_PERMISSIONS: dict[AccessMode, tuple[EntitlementPermission, ...]] = {
    "query": ("query", "view"),
    "dashboard": ("dashboard", "view"),
    "export": ("download", "view"),
}


class DemoAccessScopeUnavailable(ValueError):
    """The demonstration cannot ground a least-privilege access scope for this request.

    A value error rather than a refusal model because the seam it is raised from is declared to
    return one artifact: request-management turns it into `FulfillmentGroundingError` for the
    scope preview and `FulfillmentAuthorityError` for the owner, both of which the console reports
    to the architect. The message names what the demonstration lacks, because an operator who
    meets it has to act on the demonstration's own composition rather than on the request.
    """


class _EntitlementResolver(Protocol):
    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot: ...


def demo_access_grant_id(*, tenant_id: str, request_id: str, proposal: FulfillmentProposal) -> str:
    """The grant identifier one admitted proposal mints.

    Derived from the proposal rather than allocated, so a replayed admission of the same proposal
    names the same grant and access-control recognises it as an exact replay. A revised proposal
    is a different identity, which is what keeps a narrowed scope from being applied under the
    grant the wider one admitted.
    """
    return (
        "grant-demo-"
        + digest(
            {
                "domain": "heinzel.demonstration-access-grant.v1",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "proposal_digest": digest(proposal),
            }
        )[:24]
    )


def demo_access_effect_targets(
    preview: AccessScopePreview,
) -> tuple[AccessGrantEffectTarget, ...]:
    """The provider effects one admitted access scope requires.

    The result and warehouse surfaces always: the requester reads the answer's result back and
    queries the product behind it. The Superset surface only for dashboard access, because that
    is the only mode a dashboard is read in -- and request-management requires exactly this
    split, refusing a dashboard admission whose binding names no Superset target.
    """
    product = preview.data_product_ref.artifact_id
    targets = [
        AccessGrantEffectTarget(surface="result", provider_resource_ref=f"result:{product}"),
        AccessGrantEffectTarget(surface="warehouse", provider_resource_ref=f"relation:{product}"),
    ]
    if preview.access_mode == "dashboard":
        targets.append(
            AccessGrantEffectTarget(
                surface="superset", provider_resource_ref=f"dashboard:{product}"
            )
        )
    return tuple(targets)


class DemoAccessScopePreviewProvider:
    """The least-privilege scope the demonstration offers for one data access request.

    This is demonstration-grade. A real deployment derives the effective scope from the product's
    column-level policy, the requester's entitlement filter domains and the classification rules
    that apply to each field. Here the permitted fields are the approved terms the product's own
    query binding carries -- the same tuple the governed answer resolves a question against -- so
    the demonstration can never offer access to a field no approved term names.

    It carries no classifications, and the demonstration's product is classified `commercial`.
    Carrying it would require a `role:policy_authority` approval, which the demonstration has no
    actor for: it has an architect and a requester. That is a stated gap rather than an approval
    invented to fill it, and it is the same gap `activate_demo_answer_scope_policy` records about
    the answer's own disclosure control.
    """

    def __init__(
        self,
        *,
        product_ref: ArtifactReference,
        bindings: tuple[BoundSemanticReference, ...],
    ) -> None:
        self._product_ref = product_ref
        self._permitted_fields = tuple(sorted({binding.canonical_ref for binding in bindings}))
        if not self._permitted_fields:
            raise ValueError("the demonstration's access scope needs at least one approved term")

    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessScopePreview:
        payload = request.payload
        if not isinstance(payload, DataAccessRequest):
            raise DemoAccessScopeUnavailable(
                "the demonstration previews access scope for a data access request alone"
            )
        if (
            self._product_ref not in grounding.governed_dataset_refs
            or self._product_ref not in policy.permitted_data_product_refs
        ):
            raise DemoAccessScopeUnavailable(
                "the demonstration's product is not in the grounded, permitted scope"
            )
        requested = tuple(dict.fromkeys(payload.requested_fields))
        requested_set = set(requested)
        effective = tuple(name for name in self._permitted_fields if name in requested_set)
        if not effective:
            raise DemoAccessScopeUnavailable(
                "no requested field names an approved term of the demonstration's product; "
                f"it publishes {', '.join(self._permitted_fields)}"
            )
        return AccessScopePreview(
            # The principal the policy snapshot resolved, not one derived here: the grant is
            # narrowed against the entitlement resolved for this principal, and a second
            # derivation could name a principal the authority asserts nothing for.
            requester_principal_ref=policy.requester_principal_ref,
            data_product_ref=self._product_ref,
            access_mode=payload.access_mode,
            requested_fields=requested,
            effective_object_refs=(self._product_ref,),
            effective_fields=effective,
            excluded_scopes=tuple(sorted(requested_set - set(effective))),
            classifications=(),
            # The requester's own term, bounded by the policy's maximum where it declares one.
            # The grant's expiry is this value, and access-control refuses a grant that outlives
            # the entitlement it was narrowed against.
            expires_at=(
                payload.expires_at
                if policy.maximum_expiry is None
                else min(payload.expires_at, policy.maximum_expiry)
            ),
        )


class DemoDataProductOwnerResolver:
    """Who must approve a disclosure of the demonstration's product.

    This is demonstration-grade: the owner authority is a constant rather than a binding read
    from the product's governance record. It refuses any other product instead of naming an
    owner for it, because an owner answered for an unknown product would make every disclosure
    approvable by the one actor this demonstration has.
    """

    def __init__(self, *, product_ref: ArtifactReference) -> None:
        self._product_ref = product_ref

    def resolve(self, *, tenant_id: str, data_product_ref: ArtifactReference) -> str:
        if tenant_id != DEMO_TENANT_ID or data_product_ref != self._product_ref:
            raise DemoAccessScopeUnavailable(
                "the demonstration owns one data product and no owner for any other"
            )
        return DEMO_PRODUCT_OWNER_AUTHORITY_REF


class DemoGrantAdmissionResolver:
    """Bind the grant an admitted access proposal authorizes, against current entitlement.

    The entitlement is resolved here, at admission, through the demonstration's own signed
    policy authority -- the same authority the governed answer resolves through. Nothing about
    the grant is taken from the approval: ADR-0007 is explicit that approval evidence authorizes
    creating the proposed grant and never becomes entitlement evidence, so the snapshot digest
    and the source revision this binding carries come from the authority alone.

    The permission check here is not access-control's; access-control repeats it when the grant is
    applied. It exists so that an authority asserting less than the requested mode needs is
    reported as the authority failure it is, naming the missing permission, rather than as an
    opaque refusal one transaction later with nothing naming what was withheld.

    Both refusals are `FulfillmentAuthorityError`, which is what request-management raises for a
    binding that does not match. Anything else leaving this method is turned into
    `FulfillmentIntegrityError` by the service's own guard, and an unreachable or silent policy
    authority is not an integrity failure in the request -- it is the fail-closed denial ADR-0007
    requires, and the architect has to be able to read it as one.
    """

    def __init__(
        self, *, entitlements: _EntitlementResolver, clock: Callable[[], datetime]
    ) -> None:
        self._entitlements = entitlements
        self._clock = clock

    def bind(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessGrantAdmissionBinding:
        del policy
        payload = request.payload
        subject = proposal.subject
        if not isinstance(payload, DataAccessRequest) or not isinstance(
            subject, AccessScopePreview
        ):
            raise FulfillmentAuthorityError(
                "grant admission requires an admitted access scope proposal"
            )
        try:
            entitlement = self._entitlements.resolve_current(
                tenant_id=tenant_id,
                principal_ref=subject.requester_principal_ref,
                purpose_digest=digest(payload.purpose),
            )
        except EntitlementResolutionDenied as denial:
            raise FulfillmentAuthorityError(
                "the demonstration's entitlement authority resolved no current entitlement "
                f"for this requester and purpose: {denial.reason_code}"
            ) from denial
        except (
            ConnectedPolicyAuthorityIntegrityError,
            ConnectedPolicyAuthorityUnavailable,
        ) as failure:
            raise FulfillmentAuthorityError(
                "the demonstration's entitlement authority could not be read"
            ) from failure
        permissions = _GRANT_PERMISSIONS[subject.access_mode]
        missing = tuple(
            permission for permission in permissions if permission not in entitlement.permissions
        )
        if missing:
            raise FulfillmentAuthorityError(
                "the demonstration's entitlement authority does not assert "
                f"{', '.join(missing)} for {subject.access_mode} access"
            )
        return AccessGrantAdmissionBinding(
            grant_id=demo_access_grant_id(
                tenant_id=tenant_id, request_id=request.request_id, proposal=proposal
            ),
            proposal_digest=digest(proposal),
            entitlement_snapshot_digest=entitlement.snapshot_digest,
            policy_revision=entitlement.source_revision,
            effective_at=_utc_now(self._clock),
            permissions=permissions,
            targets=demo_access_effect_targets(subject),
        )


class DemoAccessRevocationAuthority:
    """Who may revoke the demonstration's grant by hand.

    This is demonstration-grade, and it is the pair that carries the authority rather than
    either half: the requester may give up their own access, and the architect may withdraw it.
    An actor who is neither may not revoke a grant belonging to someone else's request, which is
    the gate this stands for. A deployment resolves the revocation authority from the tenant's
    role assignments and the product's governance record.
    """

    def __init__(self, requests: RequestManagementService) -> None:
        self._requests = requests

    def may_revoke(self, *, tenant_id: str, request_id: str, actor_id: str) -> bool:
        if tenant_id != DEMO_TENANT_ID:
            return False
        try:
            request = self._requests.get(tenant_id, request_id)
        except KeyError:
            return False
        return actor_id in (request.requester_id, DEMO_ARCHITECT_ID)


class DemoAccessEffectProvider:
    """A warehouse or Superset access effect the demonstration records but does not enact.

    This stands in for `PostgreSQLAccessEffectProvider` and
    `CredentialScopedSupersetAccessEffectProvider`, the real adapters ADR-0007 assigns these
    surfaces. Both exist and neither is reachable from here: the warehouse one needs a requester
    principal warehouse-control has provisioned on the demonstration's database, and the Superset
    one needs a Superset instance, which this demonstration does not run. So what this returns is
    a classified receipt over provider state that was never changed: a grant reported as applied
    is a real Heinzel capability whose engine-side effect does not exist, and no reading of this
    demonstration may describe warehouse or dashboard access as enforced.

    It is strict about the one thing it can be strict about. Every command is re-validated against
    the grant revision that issued it -- state, revision, target, principal, fields, window and
    scope digest -- and anything that does not match is a permanent failure. A provider that
    accepted whatever it was handed would let a composition mistake in this module read as an
    applied effect, which is exactly the evidence this demonstration must not manufacture.
    """

    def __init__(
        self, *, surface: AccessEffectSurface, grants: SQLiteAccessGrantRepository
    ) -> None:
        if surface == "result":
            raise ValueError(
                "the result surface is enacted by the answer runtime's own durable provider"
            )
        self.surface: AccessEffectSurface = surface
        self._grants = grants

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        if command.surface != self.surface or not _command_matches_grant(command, self._grants):
            raise AccessEffectProviderError(
                outcome="permanent_failure", provider_receipt_digest=digest(command)
            )
        return AccessEffectResult(
            surface=command.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {
                    "domain": "heinzel.demonstration-access-effect.v1",
                    "surface": command.surface,
                    "command": command,
                }
            ),
        )


class _DemoResultAccessTargetAuthority:
    """What the result surface's real provider is allowed to record, read from the grant.

    The real `AnswerResultAccessEffectProvider` refuses a command no authority resolves a target
    for, so this is what binds its durable rows to the grant revision that asked for them rather
    than to the command it was handed.
    """

    def __init__(self, grants: SQLiteAccessGrantRepository) -> None:
        self._grants = grants

    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget | None:
        if command.surface != "result" or not _command_matches_grant(command, self._grants):
            return None
        readable: list[Literal["download", "view"]] = [
            permission
            for permission in command.permissions
            if permission == "download" or permission == "view"
        ]
        permissions = tuple(readable)
        if permissions != command.permissions:
            # The result surface records `download` and `view` and nothing else. A command
            # carrying another permission means access-control narrowed differently than this
            # target can express, which must refuse rather than record a narrower row.
            return None
        return AnswerResultAccessTarget(
            tenant_id=command.tenant_id,
            grant_id=command.grant_id,
            grant_revision=command.grant_revision,
            principal_ref=command.principal_ref,
            result_ref=command.provider_resource_ref,
            fields=command.fields,
            permissions=permissions,
            effective_at=command.effective_at,
            expires_at=command.expires_at,
            scope_digest=command.scope_digest,
        )


def _command_matches_grant(
    command: AccessEffectCommand, grants: SQLiteAccessGrantRepository
) -> bool:
    """Whether this command is the one the named grant revision is currently asking for.

    The grant is re-read rather than trusted from the command, and the scope digest is recomputed
    from what the grant holds, so a command whose scope was altered between composition and the
    provider cannot be enacted under the grant it names.
    """
    grant = grants.load_current(command.tenant_id, command.grant_id)
    expected_state = "pending" if command.action == "apply" else "revocation_pending"
    if grant is None or grant.state != expected_state or grant.revision != command.grant_revision:
        return False
    target = next((item for item in grant.effect_targets if item.surface == command.surface), None)
    scope = {
        "domain": "heinzel-access-effect-scope-v1",
        "tenant_id": grant.tenant_id,
        "grant_id": grant.grant_id,
        "principal_ref": grant.principal_ref,
        "provider_resource_ref": command.provider_resource_ref,
        "fields": grant.fields,
        "permissions": command.permissions,
        "effective_at": grant.effective_at,
        "expires_at": grant.expires_at,
    }
    return bool(
        target is not None
        and target.provider_resource_ref == command.provider_resource_ref
        and command.principal_ref == grant.principal_ref
        and command.fields == grant.fields
        and command.effective_at == grant.effective_at
        and command.expires_at == grant.expires_at
        and command.scope_digest == digest(scope)
    )


@dataclass(frozen=True, slots=True)
class DemoAccessControl:
    """Every access collaborator the demonstration's data access journey is composed from.

    The three command seams the console asks for are one service: `AccessGrantApplicationService`
    applies, reconciles and revokes, and separating them here would suggest the demonstration had
    composed two authorities over one grant.

    There is nothing to close. Both SQLite connections underneath belong to `DemoStores`, which
    opened them and closes them in the order it opened them.
    """

    grants: SQLiteAccessGrantRepository
    grant_commands: AccessGrantApplicationService
    revocation_commands: AccessGrantApplicationService
    scope_previews: DemoAccessScopePreviewProvider
    product_owners: DemoDataProductOwnerResolver
    grant_admission: DemoGrantAdmissionResolver
    activation: RequestManagementAccessDeliveryReader
    result_access: AnswerResultAccessEffectProvider


def build_demo_access_control(
    *,
    grants: SQLiteAccessGrantRepository,
    result_access_connection: sqlite3.Connection,
    requests: RequestManagementService,
    fulfillment_repository: SQLiteFulfillmentRepository,
    entitlements: _EntitlementResolver,
    product_ref: ArtifactReference,
    bindings: tuple[BoundSemanticReference, ...],
    clock: Callable[[], datetime],
) -> DemoAccessControl:
    """Compose access-control over the demonstration's stores, for one published product.

    `grants` and `result_access_connection` are `DemoStores.access_grants` and
    `DemoStores.result_access_connection`: the two durable handles this assembly writes through.
    They arrive separately rather than as the whole store, like every other collaborator builder
    in this package, because that is what makes the assembly testable over handles a test opens
    itself -- and because `DemoStores` owns their lifetime either way.

    `entitlements` is the resolver the governed answer already composes over the demonstration's
    local signed policy authority. It is a parameter rather than something built here because one
    tenant, principal and purpose has one authority: a second resolver over a second authority
    would let the answer and the grant be narrowed against different assertions of the same
    entitlement, and nothing downstream would say which one the requester actually holds. It
    follows that the demonstration composes no access at all without a governed answer, which is
    the honest posture -- there is nothing to grant access to.

    `bindings` and `product_ref` are the answer's own, for the same reason: the fields a grant
    carries are the approved terms the answer resolves, so what access is granted to is what was
    answered from.
    """
    result_access = AnswerResultAccessEffectProvider(
        result_access_connection,
        targets=_DemoResultAccessTargetAuthority(grants),
        clock=clock,
    )
    application = AccessGrantApplicationService(
        grants=grants,
        admitted_proposals=RequestManagementAdmittedAccessProposalReader(
            requests=requests, fulfillment=fulfillment_repository
        ),
        entitlements=entitlements,
        providers=(
            result_access,
            DemoAccessEffectProvider(surface="warehouse", grants=grants),
            DemoAccessEffectProvider(surface="superset", grants=grants),
        ),
        clock=clock,
        revocation_authority=DemoAccessRevocationAuthority(requests),
    )
    return DemoAccessControl(
        grants=grants,
        grant_commands=application,
        revocation_commands=application,
        scope_previews=DemoAccessScopePreviewProvider(product_ref=product_ref, bindings=bindings),
        product_owners=DemoDataProductOwnerResolver(product_ref=product_ref),
        grant_admission=DemoGrantAdmissionResolver(entitlements=entitlements, clock=clock),
        activation=RequestManagementAccessDeliveryReader(
            grants=grants, fulfillment=fulfillment_repository
        ),
        result_access=result_access,
    )


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("the demonstration clock must return timezone-aware UTC")
    return now.astimezone(UTC)
