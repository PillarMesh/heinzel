from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_provider_sdk import (
    AccessEffectAction,
    AccessEffectCommand,
    AccessEffectProvider,
    AccessEffectProviderError,
    AccessEffectResult,
)

from .grant_repository import AccessGrantStaleRevision, SQLiteAccessGrantRepository
from .grant_service import AccessGrantAuthorizationService, AccessGrantDenied
from .models import (
    AccessEffectOutcome,
    AccessEffectReceipt,
    AccessEffectSurface,
    AccessGrant,
    AccessGrantState,
    AdmittedAccessProposal,
    CurrentEntitlementSnapshot,
    EntitlementPermission,
    ManualAccessRevocation,
)
from .request_proposal import (
    AccessGrantAdmissionAuthorityInvalid,
    AccessGrantAdmissionAuthorityUnavailable,
)
from .resolver import (
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
    EntitlementResolutionDenied,
)


class CurrentAdmittedAccessProposalReader(Protocol):
    def read_admitted(
        self, *, tenant_id: str, request_id: str
    ) -> AdmittedAccessProposal | None: ...


class AccessApplicationEntitlementResolver(Protocol):
    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot: ...


class AccessRevocationAuthority(Protocol):
    def may_revoke(self, *, tenant_id: str, request_id: str, actor_id: str) -> bool: ...


class AccessGrantApplicationService:
    def __init__(
        self,
        *,
        grants: SQLiteAccessGrantRepository,
        admitted_proposals: CurrentAdmittedAccessProposalReader,
        entitlements: AccessApplicationEntitlementResolver,
        providers: tuple[AccessEffectProvider, ...],
        clock: Callable[[], datetime],
        revocation_authority: AccessRevocationAuthority | None = None,
    ) -> None:
        by_surface = {provider.surface: provider for provider in providers}
        if len(by_surface) != len(providers):
            raise ValueError("access effect providers must have unique surfaces")
        self._grants = grants
        self._admitted_proposals = admitted_proposals
        self._entitlements = entitlements
        self._providers = by_surface
        self._revocation_authority = revocation_authority
        self._clock = clock
        self._authorization = AccessGrantAuthorizationService(
            grants=grants,
            entitlements=entitlements,
            clock=clock,
        )

    def apply(self, *, tenant_id: str, request_id: str, grant_id: str) -> AccessGrant:
        try:
            proposal = self._admitted_proposals.read_admitted(
                tenant_id=tenant_id, request_id=request_id
            )
        except (
            AccessGrantAdmissionAuthorityInvalid,
            AccessGrantAdmissionAuthorityUnavailable,
        ) as error:
            raise AccessGrantDenied("admitted access proposal is unavailable") from error
        if proposal is None:
            raise AccessGrantDenied("admitted access proposal is unavailable")
        if (proposal.tenant_id, proposal.request_id) != (tenant_id, request_id):
            raise AccessGrantDenied("admitted access proposal is unavailable")
        entitlement = self._resolve_entitlement(proposal)
        self._require_proposal_entitlement(proposal, entitlement)
        current = self._grants.load_current(tenant_id, grant_id)
        if current is not None:
            self._require_exact_replay(current, proposal)
            self._require_provider_coverage(current)
            if current.state in ("active", "failed", "revoked"):
                return self._reconcile_grant(current)
            return self._reconcile_grant(current)

        now = self._now()
        pending = AccessGrant(
            grant_id=grant_id,
            tenant_id=tenant_id,
            request_id=request_id,
            revision=1,
            state="pending",
            principal_ref=proposal.principal_ref,
            purpose=proposal.purpose,
            purpose_digest=digest(proposal.purpose),
            data_product_version_ref=proposal.data_product_version_ref,
            fields=proposal.fields,
            classification_refs=proposal.classification_refs,
            access_mode=proposal.access_mode,
            permissions=proposal.permissions,
            effective_at=proposal.effective_at,
            expires_at=proposal.expires_at,
            policy_revision=proposal.policy_revision,
            admission_receipt_ref=proposal.admission_receipt_ref,
            entitlement_snapshot_digest=proposal.entitlement_snapshot_digest,
            effect_targets=proposal.targets,
            created_at=now,
            updated_at=now,
        )
        self._require_provider_coverage(pending)
        self._grants.append(pending, expected_current_revision=0)
        return self._apply_effects(pending, action="apply")

    def reconcile(self, *, tenant_id: str, grant_id: str) -> AccessGrant:
        grant = self._grants.load_current(tenant_id, grant_id)
        if grant is None:
            raise AccessGrantDenied("access grant is unavailable")
        self._require_provider_coverage(grant)
        return self._reconcile_grant(grant)

    def revoke(self, *, tenant_id: str, grant_id: str) -> AccessGrant:
        grant = self._grants.load_current(tenant_id, grant_id)
        if grant is None:
            raise AccessGrantDenied("access grant is unavailable")
        self._require_provider_coverage(grant)
        if grant.state == "revoked":
            return grant
        if grant.state == "failed":
            if grant.failed_action == "revoke":
                return grant
            return self._apply_effects(grant, action="revoke")
        if grant.state != "revocation_pending":
            grant = self._transition(grant, "revocation_pending")
        return self._apply_effects(grant, action="revoke")

    def revoke_for_request(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
        reason: str,
    ) -> AccessGrant:
        grant = self._grants.load_current_for_request(tenant_id, request_id)
        authority = self._revocation_authority
        if (
            grant is None
            or authority is None
            or not authority.may_revoke(
                tenant_id=tenant_id, request_id=request_id, actor_id=actor_id
            )
        ):
            raise AccessGrantDenied("access grant is unavailable")
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 512:
            raise AccessGrantDenied("manual revocation reason is invalid")
        prior = grant.manual_revocation
        if prior is not None:
            if (
                prior.actor_id != actor_id
                or prior.reason != normalized_reason
                or prior.base_revision != expected_revision
            ):
                raise AccessGrantStaleRevision("access grant revision is stale")
            return self._reconcile_grant(grant)
        if grant.revision != expected_revision:
            raise AccessGrantStaleRevision("access grant revision is stale")
        if grant.state != "active":
            raise AccessGrantDenied("access grant cannot be revoked in its current state")
        pending = self._record_manual_revocation(
            grant,
            ManualAccessRevocation(
                actor_id=actor_id,
                reason=normalized_reason,
                requested_at=self._now(),
                base_revision=expected_revision,
            ),
        )
        return self._apply_effects(pending, action="revoke")

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
        return self._authorization.authorize(
            tenant_id=tenant_id,
            grant_id=grant_id,
            principal_ref=principal_ref,
            purpose=purpose,
            permission=permission,
            product_version_ref=product_version_ref,
        )

    def _reconcile_grant(self, grant: AccessGrant) -> AccessGrant:
        if grant.state == "revoked":
            return grant
        if grant.state == "failed":
            if grant.failed_action == "revoke":
                return grant
            return self._apply_effects(grant, action="revoke")
        now = self._now()
        if grant.state == "active":
            if now >= grant.expires_at:
                expired = self._transition(grant, "expired")
                return self._apply_effects(expired, action="revoke")
            if self._entitlement_changed(grant):
                pending = self._transition(grant, "revocation_pending")
                return self._apply_effects(pending, action="revoke")
            return grant
        if grant.state in ("expired", "revocation_pending"):
            return self._apply_effects(grant, action="revoke")
        if now >= grant.expires_at or self._entitlement_changed(grant):
            pending = self._transition(grant, "revocation_pending")
            return self._apply_effects(pending, action="revoke")
        return self._apply_effects(grant, action="apply")

    def _apply_effects(self, grant: AccessGrant, *, action: AccessEffectAction) -> AccessGrant:
        receipts = self._grants.effect_receipts(
            grant.tenant_id, grant.grant_id, grant.revision, action=action
        )
        by_surface: dict[AccessEffectSurface, list[AccessEffectReceipt]] = {}
        for receipt in receipts:
            by_surface.setdefault(receipt.surface, []).append(receipt)
        ordered_targets = sorted(
            grant.effect_targets,
            key=lambda target: {"warehouse": 0, "result": 1, "superset": 2}[target.surface],
        )
        for target in ordered_targets:
            previous = by_surface.get(target.surface, [])
            if any(receipt.outcome == "succeeded" for receipt in previous):
                continue
            if any(receipt.outcome == "permanent_failure" for receipt in previous):
                continue
            attempt = max((receipt.attempt for receipt in previous), default=0) + 1
            command = self._effect_command(
                grant=grant,
                surface=target.surface,
                provider_resource_ref=target.provider_resource_ref,
                action=action,
            )
            try:
                result = self._providers[target.surface].enact(command)
                if self._result_matches(command, result):
                    outcome: AccessEffectOutcome = "succeeded"
                    receipt_digest = result.provider_receipt_digest
                else:
                    outcome = "permanent_failure"
                    receipt_digest = digest(
                        {
                            "domain": "pillarmesh-invalid-access-provider-result-v1",
                            "surface": command.surface,
                            "action": command.action,
                            "idempotency_key": command.idempotency_key,
                        }
                    )
            except AccessEffectProviderError as error:
                outcome = error.outcome
                receipt_digest = error.provider_receipt_digest
            receipt = AccessEffectReceipt(
                effect_id=self._effect_id(command=command, attempt=attempt),
                tenant_id=grant.tenant_id,
                grant_id=grant.grant_id,
                grant_revision=grant.revision,
                surface=target.surface,
                action=action,
                attempt=attempt,
                outcome=outcome,
                provider_receipt_digest=receipt_digest,
                recorded_at=self._now(),
            )
            self._grants.record_effect(receipt)

        current_receipts = self._grants.effect_receipts(
            grant.tenant_id, grant.grant_id, grant.revision, action=action
        )
        succeeded = {
            receipt.surface for receipt in current_receipts if receipt.outcome == "succeeded"
        }
        required = {target.surface for target in grant.effect_targets}
        if succeeded == required:
            if action == "revoke" and grant.state == "failed":
                return grant
            return self._transition(grant, "active" if action == "apply" else "revoked")
        if any(receipt.outcome == "permanent_failure" for receipt in current_receipts):
            failed = self._transition(grant, "failed", failed_action=action)
            if action == "apply":
                return self._apply_effects(failed, action="revoke")
            return failed
        return grant

    def _effect_command(
        self,
        *,
        grant: AccessGrant,
        surface: AccessEffectSurface,
        provider_resource_ref: str,
        action: AccessEffectAction,
    ) -> AccessEffectCommand:
        permissions = self._surface_permissions(grant, surface)
        scope = {
            "domain": "pillarmesh-access-effect-scope-v1",
            "tenant_id": grant.tenant_id,
            "grant_id": grant.grant_id,
            "principal_ref": grant.principal_ref,
            "provider_resource_ref": provider_resource_ref,
            "fields": grant.fields,
            "permissions": permissions,
            "effective_at": grant.effective_at,
            "expires_at": grant.expires_at,
        }
        idempotency_key = (
            "pm-access-"
            + digest(
                {
                    "domain": "pillarmesh-access-effect-idempotency-v1",
                    "tenant_id": grant.tenant_id,
                    "grant_id": grant.grant_id,
                    "surface": surface,
                    "action": action,
                    "provider_resource_ref": provider_resource_ref,
                }
            )[:24]
        )
        return AccessEffectCommand(
            tenant_id=grant.tenant_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            surface=surface,
            action=action,
            idempotency_key=idempotency_key,
            principal_ref=grant.principal_ref,
            provider_resource_ref=provider_resource_ref,
            fields=grant.fields,
            permissions=permissions,
            effective_at=grant.effective_at,
            expires_at=grant.expires_at,
            scope_digest=digest(scope),
        )

    @staticmethod
    def _surface_permissions(
        grant: AccessGrant, surface: AccessEffectSurface
    ) -> tuple[EntitlementPermission, ...]:
        if surface == "result":
            return tuple(
                permission for permission in grant.permissions if permission in {"download", "view"}
            )
        if surface == "superset":
            return tuple(
                permission
                for permission in grant.permissions
                if permission in {"dashboard", "view"}
            )
        return grant.permissions

    @staticmethod
    def _result_matches(command: AccessEffectCommand, result: object) -> bool:
        return isinstance(result, AccessEffectResult) and (
            result.surface,
            result.action,
            result.idempotency_key,
        ) == (
            command.surface,
            command.action,
            command.idempotency_key,
        )

    @staticmethod
    def _effect_id(*, command: AccessEffectCommand, attempt: int) -> str:
        return (
            "effect-"
            + digest(
                {
                    "domain": "pillarmesh-access-effect-receipt-v1",
                    "idempotency_key": command.idempotency_key,
                    "grant_revision": command.grant_revision,
                    "attempt": attempt,
                }
            )[:24]
        )

    def _transition(
        self,
        grant: AccessGrant,
        state: AccessGrantState,
        *,
        failed_action: AccessEffectAction | None = None,
    ) -> AccessGrant:
        values = grant.model_dump(mode="python")
        values.update(
            revision=grant.revision + 1,
            state=state,
            failed_action=failed_action,
            updated_at=self._now(),
        )
        changed = AccessGrant.model_validate(values)
        return self._grants.append(changed, expected_current_revision=grant.revision)

    def _record_manual_revocation(
        self, grant: AccessGrant, revocation: ManualAccessRevocation
    ) -> AccessGrant:
        values = grant.model_dump(mode="python")
        values.update(
            revision=grant.revision + 1,
            state="revocation_pending",
            manual_revocation=revocation,
            updated_at=self._now(),
        )
        changed = AccessGrant.model_validate(values)
        return self._grants.append(changed, expected_current_revision=grant.revision)

    def _resolve_entitlement(self, proposal: AdmittedAccessProposal) -> CurrentEntitlementSnapshot:
        try:
            return self._entitlements.resolve_current(
                tenant_id=proposal.tenant_id,
                principal_ref=proposal.principal_ref,
                purpose_digest=digest(proposal.purpose),
            )
        except (
            ConnectedPolicyAuthorityIntegrityError,
            ConnectedPolicyAuthorityUnavailable,
            EntitlementResolutionDenied,
        ) as error:
            raise AccessGrantDenied("current entitlement is unavailable") from error

    def _entitlement_changed(self, grant: AccessGrant) -> bool:
        try:
            entitlement = self._entitlements.resolve_current(
                tenant_id=grant.tenant_id,
                principal_ref=grant.principal_ref,
                purpose_digest=grant.purpose_digest,
            )
        except (
            ConnectedPolicyAuthorityIntegrityError,
            ConnectedPolicyAuthorityUnavailable,
            EntitlementResolutionDenied,
        ):
            return True
        return entitlement.snapshot_digest != grant.entitlement_snapshot_digest

    def _require_proposal_entitlement(
        self, proposal: AdmittedAccessProposal, entitlement: CurrentEntitlementSnapshot
    ) -> None:
        now = self._now()
        authorized = (
            entitlement.snapshot_digest == proposal.entitlement_snapshot_digest,
            proposal.data_product_version_ref in entitlement.product_version_refs,
            set(proposal.permissions).issubset(entitlement.permissions),
            proposal.effective_at >= entitlement.effective_at,
            proposal.expires_at <= entitlement.valid_until,
            entitlement.effective_at <= now < entitlement.valid_until,
            proposal.effective_at <= now < proposal.expires_at,
        )
        if not all(authorized):
            raise AccessGrantDenied("proposal is outside current entitlement")

    @staticmethod
    def _require_exact_replay(grant: AccessGrant, proposal: AdmittedAccessProposal) -> None:
        grant_identity = (
            grant.request_id,
            grant.principal_ref,
            grant.purpose,
            grant.data_product_version_ref,
            grant.fields,
            grant.classification_refs,
            grant.access_mode,
            grant.permissions,
            grant.effective_at,
            grant.expires_at,
            grant.policy_revision,
            grant.admission_receipt_ref,
            grant.entitlement_snapshot_digest,
            grant.effect_targets,
        )
        proposal_identity = (
            proposal.request_id,
            proposal.principal_ref,
            proposal.purpose,
            proposal.data_product_version_ref,
            proposal.fields,
            proposal.classification_refs,
            proposal.access_mode,
            proposal.permissions,
            proposal.effective_at,
            proposal.expires_at,
            proposal.policy_revision,
            proposal.admission_receipt_ref,
            proposal.entitlement_snapshot_digest,
            proposal.targets,
        )
        if grant_identity != proposal_identity:
            raise AccessGrantDenied("grant identity conflicts with admitted proposal")

    def _require_provider_coverage(self, grant: AccessGrant) -> None:
        if any(target.surface not in self._providers for target in grant.effect_targets):
            raise AccessGrantDenied("access effect provider is unavailable")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return timezone-aware UTC")
        return now.astimezone(UTC)
