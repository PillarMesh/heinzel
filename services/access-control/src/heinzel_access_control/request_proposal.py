from __future__ import annotations

from typing import Protocol

from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import (
    AccessScopePreview,
    DataAccessRequest,
    FulfillmentAdmissionReceipt,
    FulfillmentProposal,
    InboxRequest,
    RequestState,
)

from .models import AccessEffectTarget, AdmittedAccessProposal


class AccessGrantAdmissionAuthorityInvalid(RuntimeError):
    pass


class AccessGrantAdmissionAuthorityUnavailable(RuntimeError):
    pass


class RequestManagementRequestReader(Protocol):
    def get(self, tenant_id: str, request_id: str) -> InboxRequest: ...


class RequestManagementFulfillmentReader(Protocol):
    def list_proposals(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentProposal, ...]: ...

    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentAdmissionReceipt, ...]: ...


class RequestManagementAdmittedAccessProposalReader:
    def __init__(
        self,
        *,
        requests: RequestManagementRequestReader,
        fulfillment: RequestManagementFulfillmentReader,
    ) -> None:
        self._requests = requests
        self._fulfillment = fulfillment

    def read_admitted(self, *, tenant_id: str, request_id: str) -> AdmittedAccessProposal | None:
        try:
            request = self._requests.get(tenant_id, request_id)
            proposals = self._fulfillment.list_proposals(tenant_id, request_id)
            admissions = self._fulfillment.list_admissions(tenant_id, request_id)
        except KeyError:
            return None
        except (OSError, TimeoutError) as error:
            raise AccessGrantAdmissionAuthorityUnavailable(
                "request admission authority is unavailable"
            ) from error
        except Exception as error:
            raise AccessGrantAdmissionAuthorityInvalid(
                "request admission authority is invalid"
            ) from error
        if not proposals or not admissions:
            return None
        proposal = proposals[-1]
        admission = admissions[-1]
        subject = proposal.subject
        binding = admission.access_grant_binding
        if not isinstance(request.payload, DataAccessRequest) or not isinstance(
            subject, AccessScopePreview
        ):
            return None
        if binding is None or not self._authority_matches(
            tenant_id=tenant_id,
            request_id=request_id,
            request=request,
            proposal=proposal,
            admission=admission,
        ):
            raise AccessGrantAdmissionAuthorityInvalid("request admission authority is invalid")
        try:
            return AdmittedAccessProposal(
                tenant_id=tenant_id,
                request_id=request_id,
                proposal_id=proposal.proposal_id,
                proposal_revision=proposal.revision,
                admission_receipt_ref=ArtifactReference(
                    artifact_id=admission.admission_id,
                    version=1,
                    digest=digest(admission),
                ),
                entitlement_snapshot_digest=binding.entitlement_snapshot_digest,
                principal_ref=subject.requester_principal_ref,
                purpose=request.payload.purpose,
                data_product_version_ref=subject.data_product_ref,
                fields=subject.effective_fields,
                classification_refs=subject.classifications,
                access_mode=subject.access_mode,
                permissions=binding.permissions,
                effective_at=binding.effective_at,
                expires_at=subject.expires_at,
                policy_revision=binding.policy_revision,
                targets=tuple(
                    AccessEffectTarget(
                        surface=target.surface,
                        provider_resource_ref=target.provider_resource_ref,
                    )
                    for target in binding.targets
                ),
            )
        except (TypeError, ValueError) as error:
            raise AccessGrantAdmissionAuthorityInvalid(
                "request admission authority is invalid"
            ) from error

    @staticmethod
    def _authority_matches(
        *,
        tenant_id: str,
        request_id: str,
        request: InboxRequest,
        proposal: FulfillmentProposal,
        admission: FulfillmentAdmissionReceipt,
    ) -> bool:
        subject = proposal.subject
        binding = admission.access_grant_binding
        return (
            isinstance(subject, AccessScopePreview)
            and isinstance(request.payload, DataAccessRequest)
            and binding is not None
            and request.tenant_id == proposal.tenant_id == admission.tenant_id == tenant_id
            and request.request_id == proposal.request_id == admission.request_id == request_id
            and request.state is RequestState.EXECUTING
            and proposal.request_revision + 1 == admission.source_request_revision
            and admission.resulting_request_revision == request.revision
            and admission.proposal_id == proposal.proposal_id
            and admission.proposal_revision == proposal.revision
            and admission.proposal_digest == binding.proposal_digest == digest(proposal)
            and admission.grounding_snapshot_digest == proposal.grounding_snapshot_digest
            and subject.requested_fields == request.payload.requested_fields
            and subject.data_product_ref.artifact_id == request.payload.data_product_id
            and subject.access_mode == request.payload.access_mode
            and subject.expires_at <= request.payload.expires_at
        )
