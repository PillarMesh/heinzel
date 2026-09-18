from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from heinzel_access_control import (
    AccessGrantApplicationService,
    AccessGrantDenied,
    CurrentEntitlementSnapshot,
    RequestManagementAccessDeliveryReader,
    RequestManagementAdmittedAccessProposalReader,
    SQLiteAccessGrantRepository,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk import AccessEffectCommand, AccessEffectResult
from heinzel_request_management import (
    AccessGrantAdmissionBinding,
    AccessGrantEffectTarget,
    AccessScopePreview,
    FulfillmentAdmissionReceipt,
    FulfillmentGroundingSnapshot,
    FulfillmentPolicyCompiler,
    FulfillmentPolicySnapshot,
    FulfillmentProposal,
    FulfillmentReadService,
    FulfillmentService,
    InboxRequest,
    RequestManagementService,
    RequestState,
    SQLiteFulfillmentRepository,
    SQLiteRequestRepository,
    StakeholderAnswerDraft,
)
from heinzel_runtime import AnswerResultAccessEffectProvider, AnswerResultAccessTarget

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(minutes=30)
PURPOSE = "Review governed regional revenue"
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=7, digest="a" * 64)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _entitlement() -> CurrentEntitlementSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "entitlement-7",
        "snapshot_digest": "pending",
        "tenant_id": "tenant-a",
        "principal_ref": "principal:requester-a",
        "purpose_digest": digest(PURPOSE),
        "connected_authority_ref": "authority:policy-a",
        "source_revision": 7,
        "source_payload_digest": "b" * 64,
        "observation_id": "entitlement-observation-7",
        "product_version_refs": (PRODUCT,),
        "semantic_refs": (PRODUCT,),
        "filter_domains": (),
        "permissions": ("query", "view"),
        "effective_at": NOW - timedelta(minutes=5),
        "valid_until": EXPIRES_AT,
        "resolved_at": NOW,
    }
    values["snapshot_digest"] = digest(
        {
            "schema_version": "1",
            "tenant_id": values["tenant_id"],
            "principal_ref": values["principal_ref"],
            "purpose_digest": values["purpose_digest"],
            "connected_authority_ref": values["connected_authority_ref"],
            "source_revision": values["source_revision"],
            "source_payload_digest": values["source_payload_digest"],
            "product_version_refs": values["product_version_refs"],
            "semantic_refs": values["semantic_refs"],
            "filter_domains": values["filter_domains"],
            "permissions": values["permissions"],
            "effective_at": values["effective_at"],
            "valid_until": values["valid_until"],
        }
    )
    return CurrentEntitlementSnapshot.model_validate(values)


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest="c" * 64)


class _SnapshotResolver:
    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot]:
        assert tenant_id == request.tenant_id == "tenant-a"
        return (
            FulfillmentGroundingSnapshot(
                snapshot_id="grounding-1",
                tenant_id="tenant-a",
                catalog_publication_id="publication-1",
                catalog_publication_intent_digest="d" * 64,
                catalog_round_trip_observation_digest="e" * 64,
                semantic_version_ref=_reference("semantic-1"),
                integration_contract_ref=_reference("contract-1"),
                process_package_ref=_reference("process-1"),
                governed_dataset_refs=(PRODUCT,),
                metric_refs=(),
                classification_refs=(_reference("classification-finance"),),
                lineage_refs=(),
                freshness_observation_ref=None,
                quality_observation_refs=(),
                authorization_policy_ref=_reference("policy-1"),
                data_observation_refs=(),
                as_of=NOW,
                created_at=NOW,
            ),
            FulfillmentPolicySnapshot(
                snapshot_id="policy-1",
                tenant_id="tenant-a",
                requester_id="requester-a",
                requester_principal_ref="principal:requester-a",
                purpose_digest=digest(PURPOSE),
                approved_policy_refs=(_reference("policy-1"),),
                entitlement_observation_refs=(_reference("entitlement-7"),),
                classification_rule_refs=(_reference("rule-finance"),),
                permitted_data_product_refs=(PRODUCT,),
                permitted_access_modes=("query",),
                maximum_expiry=EXPIRES_AT,
                policy_authority_classifications=("finance",),
                observed_at=NOW,
                valid_until=EXPIRES_AT,
            ),
        )


class _AccessPreviewProvider:
    def propose(
        self,
        *,
        request: InboxRequest,
        grounding: FulfillmentGroundingSnapshot,
        policy: FulfillmentPolicySnapshot,
    ) -> AccessScopePreview:
        assert request.payload.request_type == "data_access"
        assert PRODUCT in grounding.governed_dataset_refs
        assert PRODUCT in policy.permitted_data_product_refs
        return AccessScopePreview(
            requester_principal_ref="principal:requester-a",
            data_product_ref=PRODUCT,
            access_mode="query",
            requested_fields=("region", "revenue", "customer_email"),
            effective_object_refs=(PRODUCT,),
            effective_fields=("region", "revenue"),
            excluded_scopes=("customer_email",),
            classifications=(_reference("classification-finance"),),
            expires_at=EXPIRES_AT,
        )


class _OwnerResolver:
    def resolve(self, *, tenant_id: str, data_product_ref: ArtifactReference) -> str:
        assert (tenant_id, data_product_ref) == ("tenant-a", PRODUCT)
        return "owner:revenue"


class _RoleResolver:
    def has_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        return tenant_id == "tenant-a" and (actor_id, authority_ref) in {
            ("architect-a", "role:data_engineering_architect"),
            ("owner-a", "owner:revenue"),
            ("policy-a", "role:policy_authority"),
            ("requester-a", "principal:requester-a"),
        }


class _AdmissionResolver:
    def __init__(self, entitlement: CurrentEntitlementSnapshot) -> None:
        self._entitlement = entitlement

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
            grant_id=f"grant:{request.request_id}",
            proposal_digest=digest(proposal),
            entitlement_snapshot_digest=self._entitlement.snapshot_digest,
            policy_revision=self._entitlement.source_revision,
            effective_at=NOW,
            permissions=("query", "view"),
            targets=(
                AccessGrantEffectTarget(
                    surface="result", provider_resource_ref="result:regional-revenue"
                ),
                AccessGrantEffectTarget(
                    surface="warehouse", provider_resource_ref="relation:regional-revenue"
                ),
            ),
        )


class _EntitlementResolver:
    def __init__(self, entitlement: CurrentEntitlementSnapshot) -> None:
        self._entitlement = entitlement

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot:
        assert (tenant_id, principal_ref, purpose_digest) == (
            "tenant-a",
            "principal:requester-a",
            digest(PURPOSE),
        )
        return self._entitlement


class _ResultAuthority:
    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget:
        assert command.surface == "result"
        assert command.provider_resource_ref == "result:regional-revenue"
        assert command.fields == ("region", "revenue")
        assert command.permissions == ("view",)
        return AnswerResultAccessTarget(
            tenant_id="tenant-a",
            grant_id=command.grant_id,
            grant_revision=command.grant_revision,
            principal_ref="principal:requester-a",
            result_ref="result:regional-revenue",
            fields=("region", "revenue"),
            permissions=("view",),
            effective_at=NOW,
            expires_at=EXPIRES_AT,
            scope_digest=command.scope_digest,
        )


class _StrictWarehouseProvider:
    surface: Literal["warehouse"] = "warehouse"

    def __init__(self) -> None:
        self.actions: list[Literal["apply", "revoke"]] = []
        self.active = False

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        command = AccessEffectCommand.model_validate(command.model_dump(mode="python"), strict=True)
        assert command.surface == "warehouse"
        assert command.tenant_id == "tenant-a"
        assert command.principal_ref == "principal:requester-a"
        assert command.provider_resource_ref == "relation:regional-revenue"
        assert command.fields == ("region", "revenue")
        assert command.permissions == ("query", "view")
        assert command.effective_at == NOW
        assert command.expires_at == EXPIRES_AT
        self.actions.append(command.action)
        self.active = command.action == "apply"
        return AccessEffectResult(
            surface="warehouse",
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest(
                {"surface": "warehouse", "action": command.action, "scope": command.scope_digest}
            ),
        )


class _UnusedAnswerProvider:
    def propose(self, *, request: object, grounding: object) -> StakeholderAnswerDraft:
        raise AssertionError("answer generation must not run for an access request")


class _CurrentFreshness:
    def derive(
        self, grounding: FulfillmentGroundingSnapshot
    ) -> Literal["current", "stale", "unknown", "not_applicable"]:
        return "unknown"


def test_admitted_access_is_delivered_only_after_effects_and_is_revoked_at_expiry() -> None:
    clock = _Clock()
    entitlement = _entitlement()
    request_repository = SQLiteRequestRepository.open(":memory:")
    requests = RequestManagementService(request_repository, clock=clock)
    fulfillment_repository = SQLiteFulfillmentRepository(request_repository)
    grant_repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    delivery_reader = RequestManagementAccessDeliveryReader(
        grants=grant_repository,
        fulfillment=fulfillment_repository,
    )
    fulfillment = FulfillmentService(
        request_service=requests,
        repository=fulfillment_repository,
        snapshot_resolver=_SnapshotResolver(),
        answer_candidate_provider=_UnusedAnswerProvider(),
        access_candidate_provider=_AccessPreviewProvider(),
        data_product_owner_resolver=_OwnerResolver(),
        authority_role_resolver=_RoleResolver(),
        access_grant_admission_resolver=_AdmissionResolver(entitlement),
        access_grant_activation_reader=delivery_reader,
        policy_compiler=FulfillmentPolicyCompiler(freshness_evaluator=_CurrentFreshness()),
        clock=clock,
    )
    result_provider = AnswerResultAccessEffectProvider.in_memory(
        targets=_ResultAuthority(), clock=clock
    )
    warehouse_provider = _StrictWarehouseProvider()
    access = AccessGrantApplicationService(
        grants=grant_repository,
        admitted_proposals=RequestManagementAdmittedAccessProposalReader(
            requests=requests,
            fulfillment=fulfillment_repository,
        ),
        entitlements=_EntitlementResolver(entitlement),
        providers=(result_provider, warehouse_provider),
        clock=clock,
    )

    submitted = requests.submit_access_request(
        tenant_id="tenant-a",
        requester_id="requester-a",
        title="Regional revenue access",
        purpose=PURPOSE,
        data_product_id=PRODUCT.artifact_id,
        requested_fields=("region", "revenue", "customer_email"),
        access_mode="query",
        expires_at=EXPIRES_AT,
    )
    fulfillment.clarify_outcome(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        restated_request="Provide query access to approved regional revenue fields.",
        in_scope_summary="Region and revenue.",
        out_of_scope_summary="Customer email.",
        expected_revision=submitted.revision,
    )
    investigating = requests.get("tenant-a", submitted.request_id)
    proposal = fulfillment.propose_access(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        expected_revision=investigating.revision,
    )
    assert isinstance(proposal, FulfillmentProposal)
    awaiting = fulfillment.submit_proposal(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        expected_revision=proposal.request_revision,
    )
    approvers = {
        "owner:revenue": "owner-a",
        "principal:requester-a": "requester-a",
        "role:policy_authority": "policy-a",
    }
    for requirement in proposal.required_approvals:
        fulfillment.record_approval(
            tenant_id="tenant-a",
            request_id=submitted.request_id,
            actor_id=approvers[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    admission = fulfillment.admit(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="architect-a",
        expected_revision=awaiting.revision,
    )
    assert isinstance(admission, FulfillmentAdmissionReceipt)
    assert admission.access_grant_binding is not None
    grant_id = admission.access_grant_binding.grant_id

    active = access.apply(tenant_id="tenant-a", request_id=submitted.request_id, grant_id=grant_id)
    delivery = fulfillment.execute_access(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="heinzel-access-control",
        expected_revision=admission.resulting_request_revision,
    )
    requester_view = FulfillmentReadService(
        request_service=requests,
        repository=fulfillment_repository,
        authority_role_resolver=_RoleResolver(),
    ).requester_view(
        tenant_id="tenant-a",
        request_id=submitted.request_id,
        actor_id="requester-a",
    )

    assert active.state == "active"
    assert warehouse_provider.active
    assert result_provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="result:regional-revenue",
        permission="view",
    )
    assert delivery.fields == ("region", "revenue")
    assert requester_view.state is RequestState.DELIVERED
    assert requester_view.delivered_access is not None
    assert requester_view.delivered_access.model_dump() == {
        "access_mode": "query",
        "fields": ("region", "revenue"),
        "effective_at": NOW,
        "expires_at": EXPIRES_AT,
        "permissions": ("query", "view"),
    }

    clock.now = EXPIRES_AT
    with pytest.raises(AccessGrantDenied, match="expired"):
        access.authorize(
            tenant_id="tenant-a",
            grant_id=grant_id,
            principal_ref="principal:requester-a",
            purpose=PURPOSE,
            permission="view",
            product_version_ref=PRODUCT,
        )

    revoked = access.reconcile(tenant_id="tenant-a", grant_id=grant_id)

    assert revoked.state == "revoked"
    assert warehouse_provider.actions == ["apply", "revoke"]
    assert not warehouse_provider.active
    assert not result_provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="result:regional-revenue",
        permission="view",
    )
