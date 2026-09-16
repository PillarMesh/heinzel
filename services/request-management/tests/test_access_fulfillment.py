from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AccessGrantAdmissionBinding,
    AccessGrantDeliveryObservation,
    AccessGrantEffectTarget,
    AccessScopePreview,
    FulfillmentAuthorityError,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    FulfillmentPolicySnapshot,
    FulfillmentReadService,
    FulfillmentService,
    InboxRequest,
    RequestManagementService,
    RequestState,
    ResolutionFailure,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
DIGEST = "0" * 64


def reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=DIGEST)


def snapshots(
    *, permitted: bool = True
) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]:
    grounding = FulfillmentGroundingSnapshot(
        snapshot_id="grounding-1",
        tenant_id="tenant-a",
        catalog_publication_id="publication-1",
        catalog_publication_intent_digest=DIGEST,
        catalog_round_trip_observation_digest=DIGEST,
        semantic_version_ref=reference("semantic-1"),
        integration_contract_ref=reference("contract-1"),
        process_package_ref=reference("process-1"),
        governed_dataset_refs=(reference("product-revenue"),),
        metric_refs=(),
        classification_refs=(reference("finance"),),
        lineage_refs=(),
        freshness_observation_ref=None,
        quality_observation_refs=(),
        authorization_policy_ref=reference("policy-1"),
        data_observation_refs=(),
        as_of=NOW,
        created_at=NOW,
    )
    policy = FulfillmentPolicySnapshot(
        snapshot_id="policy-1",
        tenant_id="tenant-a",
        requester_id="requester-a",
        requester_principal_ref="principal:requester-a",
        purpose_digest=digest("finance analysis"),
        approved_policy_refs=(reference("policy-1"),),
        entitlement_observation_refs=(reference("entitlement-1"),),
        classification_rule_refs=(reference("rule-finance"),),
        permitted_data_product_refs=(reference("product-revenue"),) if permitted else (),
        permitted_access_modes=("query",) if permitted else (),
        maximum_expiry=NOW + timedelta(days=2),
        policy_authority_classifications=("finance",),
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )
    return grounding, policy


class SnapshotResolver:
    def __init__(self, value: tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]):
        self.value = value

    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]:
        if tenant_id != "tenant-a" or request.tenant_id != tenant_id:
            raise ValueError("snapshot resolution authority mismatch")
        return self.value


class PreviewProvider:
    def propose(self, *, request: object, grounding: object, policy: object) -> AccessScopePreview:
        return AccessScopePreview(
            requester_principal_ref="principal:requester-a",
            data_product_ref=reference("product-revenue"),
            access_mode="query",
            requested_fields=("invoice_id", "refund_amount"),
            effective_object_refs=(reference("product-revenue"),),
            effective_fields=("invoice_id",),
            excluded_scopes=("refund_amount",),
            classifications=(reference("finance"),),
            expires_at=NOW + timedelta(days=1),
        )


class OwnerResolver:
    def resolve(self, *, tenant_id: str, data_product_ref: ArtifactReference) -> str:
        if tenant_id != "tenant-a" or data_product_ref != reference("product-revenue"):
            raise ValueError("owner resolution authority mismatch")
        return "owner:product-revenue"


class RoleResolver:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == "tenant-a" and (actor_id, authority_ref) in {
            ("requester-a", "principal:requester-a"),
            ("architect-a", "role:data_engineering_architect"),
            ("policy-a", "role:policy_authority"),
            ("owner-a", "owner:product-revenue"),
        }


class GrantAdmissionResolver:
    def bind(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        proposal: object,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessGrantAdmissionBinding:
        assert tenant_id == request.tenant_id == policy.tenant_id == "tenant-a"
        return AccessGrantAdmissionBinding(
            grant_id="grant-request-1",
            proposal_digest=digest(proposal),
            entitlement_snapshot_digest="e" * 64,
            policy_revision=4,
            effective_at=NOW,
            permissions=("query", "view"),
            targets=(
                AccessGrantEffectTarget(
                    surface="result", provider_resource_ref="result:product-revenue"
                ),
                AccessGrantEffectTarget(
                    surface="warehouse", provider_resource_ref="relation:product-revenue"
                ),
            ),
        )


class GrantActivationReader:
    def __init__(self, observation: AccessGrantDeliveryObservation | None = None) -> None:
        self.observation = observation

    def read_active(
        self, *, tenant_id: str, request_id: str, grant_id: str
    ) -> AccessGrantDeliveryObservation | None:
        del tenant_id, request_id, grant_id
        return self.observation


class UnusedAnswerProvider:
    def propose(self, *, request: object, grounding: object) -> StakeholderAnswerDraft:
        raise AssertionError("answer provider must not be used for access")


class CurrentFreshness:
    def derive(self, grounding: FulfillmentGroundingSnapshot) -> str:
        return "unknown"


def setup(
    *,
    permitted: bool = True,
    configure_grant_resolver: bool = True,
    grant_activation_reader: GrantActivationReader | None = None,
):
    request_repository = SQLiteRequestRepository.open(":memory:")
    requests = RequestManagementService(request_repository, clock=lambda: NOW)
    repository = SQLiteFulfillmentRepository(request_repository)
    fulfillment = FulfillmentService(
        request_service=requests,
        repository=repository,
        snapshot_resolver=SnapshotResolver(snapshots(permitted=permitted)),
        answer_candidate_provider=UnusedAnswerProvider(),
        access_candidate_provider=PreviewProvider(),
        data_product_owner_resolver=OwnerResolver(),
        authority_role_resolver=RoleResolver(),
        access_grant_admission_resolver=(
            GrantAdmissionResolver() if configure_grant_resolver else None
        ),
        access_grant_activation_reader=grant_activation_reader,
        policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=CurrentFreshness()),
        clock=lambda: NOW,
    )
    submitted = requests.submit_access_request(
        tenant_id="tenant-a",
        requester_id="requester-a",
        purpose="finance analysis",
        data_product_id="product-revenue",
        requested_fields=("invoice_id", "refund_amount"),
        access_mode="query",
        expires_at=NOW + timedelta(days=7),
    )
    fulfillment.clarify_outcome(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        restated_request="Provide query access to approved invoice fields.",
        in_scope_summary="Invoice identifiers.",
        out_of_scope_summary="Refund amount.",
        expected_revision=submitted.revision,
    )
    return fulfillment, requests, repository, requests.get("tenant-a", submitted.request_id)


def _admit_access(fulfillment: FulfillmentService, investigating: InboxRequest):
    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "owner:product-revenue": "owner-a",
        "principal:requester-a": "requester-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    return proposal, fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )


def test_active_access_grant_delivers_the_request_with_requester_safe_terms() -> None:
    activation = GrantActivationReader()
    fulfillment, requests, repository, investigating = setup(grant_activation_reader=activation)
    proposal, admission = _admit_access(fulfillment, investigating)
    activation.observation = AccessGrantDeliveryObservation(
        grant_id="grant-request-1",
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        proposal_digest=digest(proposal),
        entitlement_snapshot_digest="e" * 64,
        policy_revision=4,
        effective_at=NOW,
        expires_at=NOW + timedelta(days=1),
        permissions=("query", "view"),
        targets=(
            AccessGrantEffectTarget(
                surface="result", provider_resource_ref="result:product-revenue"
            ),
            AccessGrantEffectTarget(
                surface="warehouse", provider_resource_ref="relation:product-revenue"
            ),
        ),
        effect_receipt_refs=(reference("effect-result"), reference("effect-warehouse")),
    )

    delivery = fulfillment.execute_access(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="pillarmesh-access-control",
        expected_revision=admission.resulting_request_revision,
    )

    assert delivery.access_mode == "query"
    assert delivery.fields == ("invoice_id",)
    assert delivery.expires_at == NOW + timedelta(days=1)
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.DELIVERED
    assert repository.list_access_deliveries("tenant-a", proposal.request_id) == (delivery,)
    requester_view = FulfillmentReadService(
        request_service=requests,
        repository=repository,
        authority_role_resolver=RoleResolver(),
    ).requester_view(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="requester-a",
    )
    assert requester_view.delivered_access is not None
    assert requester_view.delivered_access.access_mode == "query"
    assert requester_view.delivered_access.fields == ("invoice_id",)
    assert "grant_id" not in requester_view.delivered_access.model_dump()


def test_access_delivery_rejects_activation_that_does_not_match_admission() -> None:
    activation = GrantActivationReader()
    fulfillment, requests, repository, investigating = setup(grant_activation_reader=activation)
    proposal, admission = _admit_access(fulfillment, investigating)
    activation.observation = AccessGrantDeliveryObservation(
        grant_id="grant-request-1",
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        proposal_digest=digest(proposal),
        entitlement_snapshot_digest="f" * 64,
        policy_revision=4,
        effective_at=NOW,
        expires_at=NOW + timedelta(days=1),
        permissions=("query", "view"),
        targets=(
            AccessGrantEffectTarget(
                surface="result", provider_resource_ref="result:product-revenue"
            ),
            AccessGrantEffectTarget(
                surface="warehouse", provider_resource_ref="relation:product-revenue"
            ),
        ),
        effect_receipt_refs=(reference("effect-result"), reference("effect-warehouse")),
    )

    with pytest.raises(FulfillmentAuthorityError, match="active access authority does not match"):
        fulfillment.execute_access(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="pillarmesh-access-control",
            expected_revision=admission.resulting_request_revision,
        )

    assert requests.get("tenant-a", proposal.request_id).state is RequestState.EXECUTING
    assert repository.list_access_deliveries("tenant-a", proposal.request_id) == ()


def test_access_proposal_is_narrowed_and_requires_exact_owner_and_policy() -> None:
    fulfillment, requests, _, investigating = setup()

    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert proposal.subject.effective_fields == ("invoice_id",)
    assert proposal.subject.excluded_scopes == ("refund_amount",)
    assert tuple(item.authority_ref for item in proposal.required_approvals) == (
        "owner:product-revenue",
        "principal:requester-a",
        "role:policy_authority",
    )
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.PROPOSED


def test_access_admission_persists_current_grant_authority_with_the_approved_proposal() -> None:
    fulfillment, _, repository, investigating = setup()
    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "owner:product-revenue": "owner-a",
        "principal:requester-a": "requester-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )

    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    assert admission.access_grant_binding is not None
    assert admission.access_grant_binding.proposal_digest == digest(proposal)
    assert admission.access_grant_binding.permissions == ("query", "view")
    assert tuple(target.surface for target in admission.access_grant_binding.targets) == (
        "result",
        "warehouse",
    )
    assert repository.list_admissions("tenant-a", proposal.request_id) == (admission,)


def test_access_admission_fails_closed_without_current_grant_authority() -> None:
    fulfillment, _, repository, investigating = setup(configure_grant_resolver=False)
    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "owner:product-revenue": "owner-a",
        "principal:requester-a": "requester-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )

    with pytest.raises(FulfillmentAuthorityError, match="resolver is not configured"):
        fulfillment.admit(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id="architect-a",
            expected_revision=awaiting.revision,
        )

    assert repository.list_admissions("tenant-a", proposal.request_id) == ()


def test_unentitled_access_creates_denial_without_dependency() -> None:
    fulfillment, _, repository, investigating = setup(permitted=False)

    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert proposal.subject.subject_kind == "disclosure_denial"
    assert repository.list_dependencies("tenant-a", investigating.request_id) == ()


def test_access_snapshot_failure_preserves_requester_safe_explanation() -> None:
    fulfillment, requests, repository, investigating = setup()
    safe_explanation = "The authoritative access context is unavailable."
    fulfillment._snapshot_resolver.value = ResolutionFailure(
        reason_codes=("authority_unavailable",),
        constraint_refs=(),
        smallest_changes=("Restore the approved authority observation.",),
        requester_safe_explanation=safe_explanation,
    )

    result = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )

    assert result.requester_safe_explanation == safe_explanation
    assert repository.list_no_valid_plans("tenant-a", investigating.request_id) == (result,)
    assert requests.get("tenant-a", investigating.request_id).state is RequestState.NO_VALID_PLAN


def test_approved_denial_records_requester_safe_disposition() -> None:
    fulfillment, requests, repository, investigating = setup(permitted=False)
    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=investigating.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    actors = {
        "principal:requester-a": "requester-a",
        "role:data_engineering_architect": "architect-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=proposal.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )

    disposition = fulfillment.dispose_denial(
        tenant_id="tenant-a",
        request_id=proposal.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )

    assert disposition.requester_safe_explanation == (
        "The requested disclosure is not available for this purpose."
    )
    assert requests.get("tenant-a", proposal.request_id).state is RequestState.REJECTED
    assert repository.list_denials("tenant-a", proposal.request_id) == (disposition,)
