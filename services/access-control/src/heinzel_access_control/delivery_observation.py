from __future__ import annotations

from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AccessGrantDeliveryObservation,
    AccessGrantEffectTarget,
)

from .grant_repository import SQLiteAccessGrantRepository
from .request_proposal import RequestManagementFulfillmentReader


class RequestManagementAccessDeliveryReader:
    def __init__(
        self,
        *,
        grants: SQLiteAccessGrantRepository,
        fulfillment: RequestManagementFulfillmentReader,
    ) -> None:
        self._grants = grants
        self._fulfillment = fulfillment

    def read_active(
        self, *, tenant_id: str, request_id: str, grant_id: str
    ) -> AccessGrantDeliveryObservation | None:
        grant = self._grants.load_current(tenant_id, grant_id)
        if grant is None or grant.state != "active" or grant.request_id != request_id:
            return None
        proposals = self._fulfillment.list_proposals(tenant_id, request_id)
        admissions = self._fulfillment.list_admissions(tenant_id, request_id)
        if not proposals or not admissions:
            return None
        proposal = proposals[-1]
        admission = admissions[-1]
        binding = admission.access_grant_binding
        receipts = tuple(
            receipt
            for receipt in self._grants.effect_receipts(
                tenant_id, grant_id, grant.revision - 1, action="apply"
            )
            if receipt.outcome == "succeeded"
        )
        successful_surfaces = tuple(sorted(receipt.surface for receipt in receipts))
        target_surfaces = tuple(sorted(target.surface for target in grant.effect_targets))
        if (
            binding is None
            or admission.proposal_id != proposal.proposal_id
            or admission.proposal_revision != proposal.revision
            or admission.proposal_digest != digest(proposal)
            or binding.grant_id != grant.grant_id
            or grant.admission_receipt_ref
            != ArtifactReference(
                artifact_id=admission.admission_id,
                version=1,
                digest=digest(admission),
            )
            or successful_surfaces != target_surfaces
        ):
            return None
        return AccessGrantDeliveryObservation(
            grant_id=grant.grant_id,
            tenant_id=grant.tenant_id,
            request_id=grant.request_id,
            proposal_digest=admission.proposal_digest,
            entitlement_snapshot_digest=grant.entitlement_snapshot_digest,
            policy_revision=grant.policy_revision,
            effective_at=grant.effective_at,
            expires_at=grant.expires_at,
            permissions=grant.permissions,
            targets=tuple(
                AccessGrantEffectTarget(
                    surface=target.surface,
                    provider_resource_ref=target.provider_resource_ref,
                )
                for target in grant.effect_targets
            ),
            effect_receipt_refs=tuple(
                ArtifactReference(
                    artifact_id=receipt.effect_id,
                    version=receipt.attempt,
                    digest=digest(receipt),
                )
                for receipt in receipts
            ),
        )
