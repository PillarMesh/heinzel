from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_access_control import (
    AccessEffectReceipt,
    AccessEffectSurface,
    AccessEffectTarget,
    AccessGrant,
    AccessGrantAdmissionAuthorityInvalid,
    RequestManagementAccessDeliveryReader,
    RequestManagementAdmittedAccessProposalReader,
    SQLiteAccessGrantRepository,
)
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AccessGrantAdmissionBinding,
    AccessGrantEffectTarget,
    AccessScopePreview,
    DataAccessRequest,
    FulfillmentAdmissionReceipt,
    FulfillmentProposal,
    InboxRequest,
    RequestState,
)

NOW = datetime(2026, 9, 12, 22, tzinfo=UTC)
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=3, digest="a" * 64)


def _request(**changes: object) -> InboxRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "tenant_id": "tenant-a",
        "requester_id": "requester-a",
        "payload": DataAccessRequest(
            purpose="Review regional revenue",
            data_product_id="product:revenue",
            requested_fields=("region", "revenue"),
            access_mode="query",
            expires_at=NOW + timedelta(hours=1),
        ),
        "state": RequestState.EXECUTING,
        "revision": 5,
        "submitted_at": NOW - timedelta(hours=1),
        "updated_at": NOW,
    }
    values.update(changes)
    return InboxRequest.model_validate(values)


def _proposal(**changes: object) -> FulfillmentProposal:
    subject = AccessScopePreview(
        requester_principal_ref="principal:requester-a",
        data_product_ref=PRODUCT,
        access_mode="query",
        requested_fields=("region", "revenue"),
        effective_object_refs=(PRODUCT,),
        effective_fields=("region", "revenue"),
        excluded_scopes=(),
        classifications=(),
        expires_at=NOW + timedelta(hours=1),
    )
    values: dict[str, object] = {
        "proposal_id": "proposal-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "request_revision": 3,
        "revision": 1,
        "clarified_outcome_digest": "b" * 64,
        "grounding_snapshot_digest": "c" * 64,
        "policy_snapshot_digest": "d" * 64,
        "subject": subject,
        "required_approvals": (
            {
                "authority_ref": "principal:requester-a",
                "reason_code": "requester_consent",
                "subject_digest": digest(subject),
            },
        ),
        "created_at": NOW - timedelta(minutes=10),
    }
    values.update(changes)
    return FulfillmentProposal.model_validate(values)


def _admission(proposal: FulfillmentProposal, **changes: object) -> FulfillmentAdmissionReceipt:
    values: dict[str, object] = {
        "admission_id": "admission-1",
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "source_request_revision": 4,
        "resulting_request_revision": 5,
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.revision,
        "proposal_digest": digest(proposal),
        "grounding_snapshot_digest": proposal.grounding_snapshot_digest,
        "policy_snapshot_digest": proposal.policy_snapshot_digest,
        "approval_ids": ("approval-1",),
        "access_grant_binding": AccessGrantAdmissionBinding(
            grant_id="grant-request-1",
            proposal_digest=digest(proposal),
            entitlement_snapshot_digest="e" * 64,
            policy_revision=7,
            effective_at=NOW,
            permissions=("query", "view"),
            targets=(
                AccessGrantEffectTarget(surface="result", provider_resource_ref="result:revenue"),
                AccessGrantEffectTarget(
                    surface="warehouse", provider_resource_ref="relation:revenue"
                ),
            ),
        ),
        "admitted_at": NOW,
    }
    values.update(changes)
    return FulfillmentAdmissionReceipt.model_validate(values)


class _Requests:
    def __init__(self, request: InboxRequest) -> None:
        self.request = request

    def get(self, tenant_id: str, request_id: str) -> InboxRequest:
        assert (tenant_id, request_id) == ("tenant-a", "request-1")
        return self.request


class _Fulfillment:
    def __init__(
        self, proposal: FulfillmentProposal, admission: FulfillmentAdmissionReceipt
    ) -> None:
        self.proposal = proposal
        self.admission = admission

    def list_proposals(self, tenant_id: str, request_id: str) -> tuple[FulfillmentProposal, ...]:
        return (self.proposal,)

    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[FulfillmentAdmissionReceipt, ...]:
        return (self.admission,)


def test_reader_projects_exact_request_management_admission_into_grant_terms() -> None:
    proposal = _proposal()
    reader = RequestManagementAdmittedAccessProposalReader(
        requests=_Requests(_request()),
        fulfillment=_Fulfillment(proposal, _admission(proposal)),
    )

    result = reader.read_admitted(tenant_id="tenant-a", request_id="request-1")

    assert result is not None
    assert result.admission_receipt_ref == ArtifactReference(
        artifact_id="admission-1", version=1, digest=digest(_admission(proposal))
    )
    assert result.data_product_version_ref == PRODUCT
    assert result.fields == ("region", "revenue")
    assert tuple(target.surface for target in result.targets) == ("result", "warehouse")


def test_reader_rejects_an_admission_that_does_not_bind_the_current_request_revision() -> None:
    proposal = _proposal()
    reader = RequestManagementAdmittedAccessProposalReader(
        requests=_Requests(_request()),
        fulfillment=_Fulfillment(
            proposal,
            _admission(proposal, resulting_request_revision=6),
        ),
    )

    with pytest.raises(AccessGrantAdmissionAuthorityInvalid):
        reader.read_admitted(tenant_id="tenant-a", request_id="request-1")


def test_reader_accepts_policy_narrowed_expiry_within_the_original_request() -> None:
    subject = _proposal().subject.model_copy(update={"expires_at": NOW + timedelta(minutes=30)})
    proposal = _proposal(
        subject=subject,
        required_approvals=(
            {
                "authority_ref": "principal:requester-a",
                "reason_code": "requester_consent",
                "subject_digest": digest(subject),
            },
        ),
    )
    reader = RequestManagementAdmittedAccessProposalReader(
        requests=_Requests(_request()),
        fulfillment=_Fulfillment(proposal, _admission(proposal)),
    )

    result = reader.read_admitted(tenant_id="tenant-a", request_id="request-1")

    assert result is not None
    assert result.expires_at == NOW + timedelta(minutes=30)


def _active_grant(
    repository: SQLiteAccessGrantRepository,
    proposal: FulfillmentProposal,
    admission: FulfillmentAdmissionReceipt,
    *,
    surfaces: tuple[AccessEffectSurface, ...] = ("result", "warehouse"),
) -> None:
    binding = admission.access_grant_binding
    assert binding is not None
    pending = AccessGrant(
        grant_id=binding.grant_id,
        tenant_id="tenant-a",
        request_id="request-1",
        revision=1,
        state="pending",
        principal_ref="principal:requester-a",
        purpose="Review regional revenue",
        purpose_digest=digest("Review regional revenue"),
        data_product_version_ref=PRODUCT,
        fields=("region", "revenue"),
        classification_refs=(),
        access_mode="query",
        permissions=binding.permissions,
        effective_at=binding.effective_at,
        expires_at=NOW + timedelta(hours=1),
        policy_revision=binding.policy_revision,
        admission_receipt_ref=ArtifactReference(
            artifact_id=admission.admission_id,
            version=1,
            digest=digest(admission),
        ),
        entitlement_snapshot_digest=binding.entitlement_snapshot_digest,
        effect_targets=tuple(
            AccessEffectTarget(
                surface=target.surface,
                provider_resource_ref=target.provider_resource_ref,
            )
            for target in binding.targets
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    repository.append(pending, expected_current_revision=0)
    for surface in surfaces:
        repository.record_effect(
            AccessEffectReceipt(
                effect_id=f"effect-{surface}",
                tenant_id="tenant-a",
                grant_id=binding.grant_id,
                grant_revision=1,
                surface=surface,
                action="apply",
                attempt=1,
                outcome="succeeded",
                provider_receipt_digest=digest(surface),
                recorded_at=NOW,
            )
        )
    repository.append(
        pending.model_copy(update={"revision": 2, "state": "active"}),
        expected_current_revision=1,
    )


def test_active_grant_reader_requires_every_provider_effect() -> None:
    proposal = _proposal()
    admission = _admission(proposal)
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    _active_grant(repository, proposal, admission)
    reader = RequestManagementAccessDeliveryReader(
        grants=repository,
        fulfillment=_Fulfillment(proposal, admission),
    )

    observation = reader.read_active(
        tenant_id="tenant-a", request_id="request-1", grant_id="grant-request-1"
    )

    assert observation is not None
    assert observation.proposal_digest == digest(proposal)
    assert tuple(reference.artifact_id for reference in observation.effect_receipt_refs) == (
        "effect-result",
        "effect-warehouse",
    )


def test_active_grant_reader_withholds_partial_provider_application() -> None:
    proposal = _proposal()
    admission = _admission(proposal)
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    _active_grant(repository, proposal, admission, surfaces=("result",))
    reader = RequestManagementAccessDeliveryReader(
        grants=repository,
        fulfillment=_Fulfillment(proposal, admission),
    )

    observation = reader.read_active(
        tenant_id="tenant-a", request_id="request-1", grant_id="grant-request-1"
    )

    assert observation is None
