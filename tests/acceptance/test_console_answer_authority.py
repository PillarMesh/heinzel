from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_access_control import (
    ConnectedAuthorityProvenance,
    ConnectedPolicyAuthorityUnavailable,
    CurrentEntitlementResolver,
    EnterpriseEntitlementAssertion,
    EntitlementFilterDomain,
    SQLiteEntitlementRepository,
)
from pillarmesh_compiler import GovernedQueryPlan, ProductGenerationReference
from pillarmesh_compiler.query_repository import SQLiteQueryPlanRepository
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_request_management import (
    AnswerExecutionEvidence,
    AnswerIntentValidation,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    AnswerQuestionIntent,
    AnswerQuestionValidationResult,
    AnswerResultEvidence,
    AnswerScopePolicy,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    BoundFilter,
    FilterDomain,
    GovernedAnswer,
    GovernedAnswerDeliveryAuthorizationUnavailable,
    GovernedAnswerExecutionAuthorizationDenied,
    GovernedAnswerExecutionAuthorizationUnavailable,
    GovernedAnswerNotVisible,
    GovernedAnswerService,
    InboxRequest,
    PolicyAdmissionReceipt,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteAnswerScopePolicyRepository,
    SQLiteAnswerValidationRepository,
    SQLiteGovernedAnswerRepository,
    SQLitePolicyAdmissionEvidenceReader,
    SQLiteRequestRepository,
    StakeholderQuestion,
)
from pillarmesh_request_management.answer_admission import PolicyScanReservation

from tests.acceptance.console_answer_authority import CurrentAnswerAuthority, ProductAnswerAuthority

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
TENANT = "tenant-a"
REQUEST = "request-1"
PURPOSE = "Monthly revenue decision support"


def _reference(identifier: str, character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=1, digest=character * 64)


PRODUCT = _reference("product:revenue", "a")
SEMANTIC = _reference("semantic:revenue", "b")
METRIC = _reference("metric:revenue", "c")
DIMENSION = _reference("dimension:region", "d")
LINEAGE = _reference("lineage:revenue", "e")


class _ConnectedAuthority:
    def __init__(self, assertion: EnterpriseEntitlementAssertion) -> None:
        self.assertion: EnterpriseEntitlementAssertion | Exception = assertion
        self.calls: list[tuple[str, str, str]] = []

    def read_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> EnterpriseEntitlementAssertion:
        self.calls.append((tenant_id, principal_ref, purpose_digest))
        if isinstance(self.assertion, Exception):
            raise self.assertion
        return self.assertion


class _PrincipalDirectory:
    def resolve_principal(self, *, tenant_id: str, actor_id: str) -> str | None:
        if tenant_id == TENANT and actor_id == "requester-1":
            return "principal:requester-a"
        return None


class _ProductReader:
    def __init__(
        self,
        authority: ProductAnswerAuthority | None,
        *,
        enforce_requested_generations: bool = True,
    ) -> None:
        self.authority = authority
        self.enforce_requested_generations = enforce_requested_generations

    def read_current(
        self,
        *,
        tenant_id: str,
        product_generation_refs: tuple[ProductGenerationReference, ...],
    ) -> ProductAnswerAuthority | None:
        if self.authority is None or self.authority.tenant_id != tenant_id:
            return None
        if (
            self.enforce_requested_generations
            and self.authority.product_generation_refs != product_generation_refs
        ):
            return None
        return self.authority


class _UnusedExecutionReader:
    def read_receipt(
        self,
        tenant_id: str,
        request_id: str,
        receipt_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerExecutionEvidence | None:
        raise AssertionError("answer metadata reads must not read execution evidence")

    def read_result(
        self,
        tenant_id: str,
        request_id: str,
        result_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerResultEvidence | None:
        raise AssertionError("answer metadata reads must not read result evidence")


class _UnusedPlanReader:
    def read_plan(self, tenant_id: str, plan_digest: str) -> AnswerPlanEvidence | None:
        raise AssertionError("answer metadata reads must not read plan evidence")


def _assertion(
    *,
    permissions: tuple[str, ...] = ("query", "view"),
    filter_values: tuple[str, ...] = ("east", "west"),
) -> EnterpriseEntitlementAssertion:
    return EnterpriseEntitlementAssertion.model_validate(
        {
            "tenant_id": TENANT,
            "principal_ref": "principal:requester-a",
            "purpose_digest": digest(PURPOSE),
            "decision": "active",
            "product_version_refs": (PRODUCT,),
            "semantic_refs": (SEMANTIC, METRIC, DIMENSION),
            "filter_domains": (
                EntitlementFilterDomain(dimension_ref=DIMENSION, values=filter_values),
            ),
            "permissions": permissions,
            "effective_at": NOW - timedelta(hours=1),
            "valid_until": NOW + timedelta(hours=1),
            "provenance": ConnectedAuthorityProvenance(
                connected_authority_ref="policy-authority:tenant-a",
                connection_binding_ref="connection:policy-authority-a",
                source_revision=1,
                source_payload_digest="1" * 64,
                authentication_method="signed_response",
                authentication_key_ref="key:policy-authority-a",
                authentication_evidence_digest="2" * 64,
                adapter_ref="provider:connected-policy:v1",
            ),
        }
    )


def _revoked_assertion() -> EnterpriseEntitlementAssertion:
    active = _assertion()
    return active.model_copy(
        update={
            "decision": "revoked",
            "product_version_refs": (),
            "semantic_refs": (),
            "filter_domains": (),
            "permissions": (),
            "provenance": active.provenance.model_copy(
                update={"source_revision": 2, "source_payload_digest": "4" * 64}
            ),
        }
    )


def _policy(repository: SQLiteAnswerScopePolicyRepository) -> AnswerScopePolicy:
    draft = AnswerScopePolicyDraft(
        policy_id="answer-policy-1",
        tenant_id=TENANT,
        revision=1,
        principal_scope=("principal:requester-a",),
        purposes=(PURPOSE,),
        semantic_version_ref=SEMANTIC,
        data_product_version_refs=(PRODUCT,),
        metric_version_refs=(METRIC,),
        dimension_refs=(DIMENSION,),
        filter_domains=(FilterDomain(dimension_ref=DIMENSION.artifact_id, values=("east",)),),
        max_time_window=86_400,
        max_staleness=3600,
        quality_disposition="label",
        disclosure_classifications=(),
        disclosure_entity="customer",
        minimum_group_size=5,
        row_ceiling=10,
        byte_ceiling=10_000,
        scan_ceiling=100_000,
        period_scan_budget=1_000_000,
        statement_timeout=15,
        result_retention=3600,
        agent_access="denied",
        model_disclosure="metadata",
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(hours=1),
    )
    approval = AnswerScopePolicyApproval(
        approval_id="approval-1",
        tenant_id=TENANT,
        policy_id=draft.policy_id,
        policy_revision=draft.revision,
        policy_digest=digest(draft),
        authority_ref="role:data_engineering_architect",
        actor_id="architect-1",
        decision="approve",
        created_at=NOW - timedelta(minutes=30),
    )
    policy = AnswerScopePolicy(**draft.model_dump(), approval_ids=(approval.approval_id,))
    repository.save(policy, approvals=(approval,))
    return policy


def _plan(validation_digest: str, *, minimum_group_size: int = 5) -> GovernedQueryPlan:
    statement = (
        'SELECT "region", SUM("revenue") FROM "sales" GROUP BY "region" '
        'HAVING COUNT(DISTINCT "customer_id") >= %s LIMIT 10'
    )
    parameters = (
        {"name": "p0", "value_type": "integer", "value": 5},
        {"name": "p1", "value_type": "string", "value": "east"},
    )
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "plan-1",
        "tenant_id": TENANT,
        "validation_digest": validation_digest,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (
            {"artifact_id": "consumption:sales", "version": 1, "digest": "f" * 64},
        ),
        "product_generation_refs": (
            {"product_ref": PRODUCT.model_dump(mode="python"), "generation": 7},
        ),
        "minimum_group_size": minimum_group_size,
        "statement": statement,
        "parameters": parameters,
        "statement_digest": digest(statement),
        "parameter_digest": digest(parameters),
        "estimated_scan": {"rows": 100, "bytes": 1_000, "estimator_version": "test"},
        "ceilings": {
            "row_limit": 10,
            "scan": {"rows": 1_000, "bytes": 100_000},
            "period_scan": {"rows": 10_000, "bytes": 1_000_000},
        },
        "routing": "policy_admitted",
    }
    return GovernedQueryPlan.model_validate(
        {**body, "plan_digest": digest(body), "signature": "compiler-key.test"}
    )


def _composition(
    *,
    assertion: EnterpriseEntitlementAssertion | None = None,
    include_product: bool = True,
    include_bound_filter: bool = True,
    plan_minimum_group_size: int = 5,
    product_generation: int = 7,
    validation_generation: int = 7,
    enforce_product_reader_generations: bool = True,
) -> tuple[
    CurrentAnswerAuthority,
    SQLiteRequestRepository,
    _ConnectedAuthority,
    PolicyAdmissionReceipt,
    GovernedQueryPlan,
]:
    connection = sqlite3.connect(":memory:")
    requests = SQLiteRequestRepository(connection)
    requests.save(
        InboxRequest(
            request_id=REQUEST,
            tenant_id=TENANT,
            requester_id="requester-1",
            payload=StakeholderQuestion(purpose=PURPOSE, question="Revenue by region?"),
            state=RequestState.INVESTIGATING,
            revision=1,
            submitted_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(minutes=20),
        )
    )
    policies = SQLiteAnswerScopePolicyRepository(connection)
    policy = _policy(policies)
    connected = _ConnectedAuthority(assertion or _assertion())
    entitlements = CurrentEntitlementResolver(
        repository=SQLiteEntitlementRepository(sqlite3.connect(":memory:")),
        connected_authority=connected,
        connected_authority_ref="policy-authority:tenant-a",
        clock=lambda: NOW,
    )
    snapshot = entitlements.resolve_current(
        tenant_id=TENANT,
        principal_ref="principal:requester-a",
        purpose_digest=digest(PURPOSE),
    )
    intent = AnswerQuestionIntent(
        intent_id="intent-1",
        tenant_id=TENANT,
        request_id=REQUEST,
        request_revision=1,
        question_digest="3" * 64,
        intent_kind="metric_value",
        metric_refs=("revenue",),
        dimension_refs=("region",),
        filters=(),
        time_window=None,
        ordering=(),
        row_limit=10,
        interpreter="form",
        interpreter_ref="answer-form-v1",
        created_at=NOW - timedelta(minutes=15),
    )
    validation = AnswerIntentValidation(
        validation_id="validation-1",
        tenant_id=TENANT,
        request_id=REQUEST,
        request_revision=1,
        intent_digest=digest(intent),
        semantic_version_digest=SEMANTIC.digest,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        policy_digest=policy.canonical_digest(),
        entitlement_snapshot_digest=snapshot.snapshot_digest,
        bound_metric_versions=(METRIC,),
        bound_dimensions=(DIMENSION,),
        bound_filters=(
            (BoundFilter(dimension_ref=DIMENSION, operator="equals", values=("east",)),)
            if include_bound_filter
            else ()
        ),
        restatement="Metric revenue by region where region is east",
        product_generation_refs=(
            AnswerProductGenerationReference(
                product_ref=PRODUCT,
                generation=validation_generation,
            ),
        ),
        outcome="admitted",
        reason_codes=(),
        created_at=NOW - timedelta(minutes=15),
    )
    validations = SQLiteAnswerValidationRepository(connection)
    validations.save(
        AnswerQuestionValidationResult(
            intent=intent,
            validation=validation,
            restatement_confirmation_required=False,
        )
    )
    plan = _plan(digest(validation), minimum_group_size=plan_minimum_group_size)
    SQLiteQueryPlanRepository(connection).save(plan)
    admission = PolicyAdmissionReceipt(
        admission_id="admission-1",
        tenant_id=TENANT,
        request_id=REQUEST,
        request_revision=1,
        validation_digest=digest(validation),
        restatement_acceptance_ref="restatement-approval-1",
        plan_digest=plan.plan_digest,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        policy_digest=policy.canonical_digest(),
        entitlement_snapshot_digest=snapshot.snapshot_digest,
        period_scan_consumed=None,
        created_at=NOW - timedelta(minutes=10),
    )
    admissions = SQLiteAnswerAdmissionRepository(requests)
    recorded = admissions.record_and_transition(
        admission,
        actor_id="architect-1",
        reservation=PolicyScanReservation.for_calendar_month(
            reserved_scan=plan.ceilings.scan.bytes,
            period_budget=policy.period_scan_budget,
            at=admission.created_at,
        ),
    )
    assert isinstance(recorded, tuple)
    admission = recorded[0]
    product = (
        ProductAnswerAuthority(
            tenant_id=TENANT,
            product_generation_refs=tuple(
                reference.model_copy(update={"generation": product_generation})
                for reference in plan.product_generation_refs
            ),
            freshness_observation_ref="freshness-1",
            quality_observation_ref="quality-1",
            freshness_disposition="current",
            quality_blocked=False,
            material_quality_limitations=(),
            lineage_refs=(LINEAGE,),
            as_of=NOW - timedelta(minutes=5),
            approved_narrative_terms=("Revenue", "East"),
        )
        if include_product
        else None
    )
    authority = CurrentAnswerAuthority(
        requests=requests,
        admissions=admissions,
        plans=SQLiteQueryPlanRepository(connection),
        policies=policies,
        validations=validations,
        entitlements=entitlements,
        principals=_PrincipalDirectory(),
        products=_ProductReader(
            product, enforce_requested_generations=enforce_product_reader_generations
        ),
        clock=lambda: NOW,
    )
    return authority, requests, connected, admission, plan


def _delivered_answer_service(
    authority: CurrentAnswerAuthority,
    requests: SQLiteRequestRepository,
    admission: PolicyAdmissionReceipt,
    plan: GovernedQueryPlan,
) -> GovernedAnswerService:
    requests.transition(
        TENANT,
        REQUEST,
        expected_revision=2,
        actor_id="runtime-1",
        to_state=RequestState.VERIFYING,
        created_at=NOW,
    )
    repository = SQLiteGovernedAnswerRepository(requests)
    product_generations = tuple(
        AnswerProductGenerationReference.model_validate(
            reference.model_dump(mode="python"), strict=True
        )
        for reference in plan.product_generation_refs
    )
    repository.store(
        GovernedAnswer(
            answer_id="pending",
            tenant_id=TENANT,
            request_id=REQUEST,
            request_revision=3,
            restatement="Metric revenue by region where region is east",
            admission_ref=admission.admission_id,
            execution_receipt_ref="receipt-1",
            metric_version_refs=(METRIC,),
            product_generation_refs=product_generations,
            as_of=NOW - timedelta(minutes=5),
            freshness_disposition="current",
            material_quality_limitations=(),
            lineage_refs=(LINEAGE,),
            narrative="East revenue is available.",
            narrative_source="template",
            result_ref="result-1",
            result_digest="9" * 64,
            delivered_at=NOW,
        ),
        command_digest="8" * 64,
        actor_id="system:answer-verifier",
    )
    return GovernedAnswerService(
        repository=repository,
        admission_reader=SQLitePolicyAdmissionEvidenceReader(
            SQLiteAnswerAdmissionRepository(requests), requests
        ),
        execution_reader=_UnusedExecutionReader(),
        plan_reader=_UnusedPlanReader(),
        authorization_rechecker=authority.delivery_rechecker(),
        clock=lambda: NOW,
    )


def test_runtime_and_delivery_rechecks_derive_exact_owning_authority() -> None:
    authority, requests, connected, admission, plan = _composition()

    runtime = authority.runtime_rechecker().recheck(
        tenant_id=TENANT,
        request_id=REQUEST,
        validation_digest=admission.validation_digest,
        plan_digest=plan.plan_digest,
    )
    requests.transition(
        TENANT,
        REQUEST,
        expected_revision=2,
        actor_id="runtime-1",
        to_state=RequestState.VERIFYING,
        created_at=NOW,
    )
    delivery = authority.delivery_rechecker().recheck(
        tenant_id=TENANT,
        request_id=REQUEST,
        requester_id="requester-1",
        plan_digest=plan.plan_digest,
        required_permission="view",
    )

    assert runtime.entitlement_digest == admission.entitlement_snapshot_digest
    assert runtime.policy_revision == admission.policy_revision
    assert runtime.freshness_observation_ref == "freshness-1"
    assert delivery.entitlement_snapshot_digest == admission.entitlement_snapshot_digest
    assert delivery.policy_snapshot_digest == admission.policy_digest
    assert delivery.metric_version_refs == (METRIC,)
    assert delivery.lineage_refs == (LINEAGE,)
    assert connected.calls == [
        (TENANT, "principal:requester-a", digest(PURPOSE)),
        (TENANT, "principal:requester-a", digest(PURPOSE)),
        (TENANT, "principal:requester-a", digest(PURPOSE)),
    ]


def test_validation_filter_must_remain_inside_current_entitlement_domain() -> None:
    authority, _, _, admission, plan = _composition(assertion=_assertion(filter_values=("west",)))

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_current_entitlement_filter_restriction_cannot_be_omitted() -> None:
    authority, _, _, admission, plan = _composition(include_bound_filter=False)

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_plan_cannot_weaken_current_policy_suppression() -> None:
    authority, _, _, admission, plan = _composition(plan_minimum_group_size=1)

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_product_reader_cannot_return_evidence_for_another_generation() -> None:
    authority, _, _, admission, plan = _composition(
        product_generation=8,
        enforce_product_reader_generations=False,
    )

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_validation_generation_must_match_the_exact_compiled_generation() -> None:
    authority, _, _, admission, plan = _composition(validation_generation=8)

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_stored_policy_approval_does_not_substitute_for_revoked_entitlement() -> None:
    authority, _, connected, admission, plan = _composition()
    connected.assertion = _revoked_assertion()

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_connected_authority_outage_is_distinct_from_terminal_denial() -> None:
    authority, _, connected, admission, plan = _composition()
    connected.assertion = ConnectedPolicyAuthorityUnavailable("offline")

    with pytest.raises(GovernedAnswerExecutionAuthorizationUnavailable):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )


def test_each_rechecker_requires_its_current_entitlement_permission() -> None:
    authority, requests, _, admission, plan = _composition(
        assertion=_assertion(permissions=("query",))
    )
    authority.runtime_rechecker().recheck(
        tenant_id=TENANT,
        request_id=REQUEST,
        validation_digest=admission.validation_digest,
        plan_digest=plan.plan_digest,
    )
    requests.transition(
        TENANT,
        REQUEST,
        expected_revision=2,
        actor_id="runtime-1",
        to_state=RequestState.VERIFYING,
        created_at=NOW,
    )

    with pytest.raises(GovernedAnswerNotVisible, match="not visible"):
        authority.delivery_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            requester_id="requester-1",
            plan_digest=plan.plan_digest,
            required_permission="view",
        )


def test_composed_answer_read_maps_current_view_denial_to_not_visible() -> None:
    authority, requests, _, admission, plan = _composition(
        assertion=_assertion(permissions=("query",))
    )
    delivery = _delivered_answer_service(authority, requests, admission, plan)

    with pytest.raises(GovernedAnswerNotVisible):
        delivery.read_for_request(TENANT, "requester-1", REQUEST)


def test_composed_answer_download_maps_current_download_denial_to_not_visible() -> None:
    authority, requests, _, admission, plan = _composition()
    delivery = _delivered_answer_service(authority, requests, admission, plan)

    with pytest.raises(GovernedAnswerNotVisible):
        delivery.read_for_download(TENANT, "requester-1", REQUEST)


def test_composed_answer_read_exposes_connected_authority_outage_as_governed_error() -> None:
    authority, requests, connected, admission, plan = _composition()
    delivery = _delivered_answer_service(authority, requests, admission, plan)
    connected.assertion = ConnectedPolicyAuthorityUnavailable("offline")

    with pytest.raises(GovernedAnswerDeliveryAuthorizationUnavailable):
        delivery.read_for_request(TENANT, "requester-1", REQUEST)


def test_missing_product_evidence_and_cross_tenant_reads_fail_closed() -> None:
    authority, _, _, admission, plan = _composition(include_product=False)

    with pytest.raises(GovernedAnswerExecutionAuthorizationDenied):
        authority.runtime_rechecker().recheck(
            tenant_id=TENANT,
            request_id=REQUEST,
            validation_digest=admission.validation_digest,
            plan_digest=plan.plan_digest,
        )
    with pytest.raises(GovernedAnswerNotVisible, match="not visible"):
        authority.delivery_rechecker().recheck(
            tenant_id="tenant-b",
            request_id=REQUEST,
            requester_id="requester-1",
            plan_digest=plan.plan_digest,
            required_permission="view",
        )
