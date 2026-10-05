from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_access_control import (
    ConnectedAuthorityProvenance,
    EnterpriseEntitlementAssertion,
)
from heinzel_compiler import GovernedQueryPlan
from heinzel_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier
from heinzel_console.answers import GovernedAnswerRuntimeConfiguration
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_request_management import (
    AnswerIntentCandidate,
    AnswerQuestion,
    AnswerScopePolicy,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    AnswerScopePolicyLifecycle,
    AnswerValidationContext,
    BoundSemanticReference,
    ProductOwnerAuthority,
    RequestState,
)
from heinzel_request_management import (
    AnswerProductGenerationReference as RequestProductGenerationReference,
)
from heinzel_runtime import (
    AnswerProductGenerationReference,
    AnswerQueryColumn,
    AnswerQueryCursor,
    ProductMaterializationReceipt,
    QueryGenerationState,
    ReadOnlyAnswerQuery,
)
from heinzel_runtime.answer_models import AnswerQueryValue
from heinzel_semantic_registry import ApprovedProductVersionMetadata
from heinzel_state import IncidentRecord
from starlette.testclient import TestClient

from tests.acceptance.run_console_governed import (
    ARCHITECT,
    REQUESTER,
    REQUESTER_PRINCIPAL,
    TENANT,
    GovernedConsoleDeployment,
)

NOW = datetime(2026, 9, 12, 18, tzinfo=UTC)
PURPOSE = "Monthly revenue decision support"
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=1, digest="a" * 64)
SEMANTIC = ArtifactReference(artifact_id="semantic:revenue", version=1, digest="b" * 64)
METRIC = ArtifactReference(artifact_id="metric:revenue", version=1, digest="c" * 64)
CONTRACT = ArtifactReference(artifact_id="contract:revenue", version=1, digest="d" * 64)
INPUT_GENERATION_DIGEST = "e" * 64
INPUT_CARDINALITY_EVIDENCE_DIGEST = "7" * 64
LINEAGE_DIGEST = "f" * 64


class _ConnectedAuthority:
    def read_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> EnterpriseEntitlementAssertion | None:
        if (
            tenant_id != TENANT
            or principal_ref != REQUESTER_PRINCIPAL
            or purpose_digest != digest(PURPOSE)
        ):
            return None
        return EnterpriseEntitlementAssertion(
            tenant_id=TENANT,
            principal_ref=REQUESTER_PRINCIPAL,
            purpose_digest=digest(PURPOSE),
            decision="active",
            product_version_refs=(PRODUCT,),
            semantic_refs=(SEMANTIC, METRIC),
            filter_domains=(),
            permissions=("download", "query", "view"),
            effective_at=NOW - timedelta(hours=1),
            valid_until=NOW + timedelta(hours=1),
            provenance=ConnectedAuthorityProvenance(
                connected_authority_ref="policy-authority:tenant-a",
                connection_binding_ref="connection:policy-authority-a",
                source_revision=1,
                source_payload_digest="1" * 64,
                authentication_method="signed_response",
                authentication_key_ref="key:policy-authority-a",
                authentication_evidence_digest="2" * 64,
                adapter_ref="provider:connected-policy:v1",
            ),
        )


class _Interpreter:
    def interpret(self, question: AnswerQuestion) -> AnswerIntentCandidate:
        assert question.request_revision > 0
        return AnswerIntentCandidate(
            intent_kind="metric_value",
            metric_refs=("revenue",),
            dimension_refs=(),
            filters=(),
            time_window=None,
            ordering=(),
            row_limit=10,
        )


class _Generations:
    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState:
        if (
            reference.product_ref.model_dump(mode="python") != PRODUCT.model_dump(mode="python")
            or reference.generation != 7
        ):
            return QueryGenerationState(addressable=False, current_generation=7)
        return QueryGenerationState(addressable=True, current_generation=7)


class _ProductEvidence:
    def __init__(self) -> None:
        self.receipt = ProductMaterializationReceipt(
            run_id="materialization-1",
            tenant_id=TENANT,
            product_id=PRODUCT.artifact_id,
            product_revision=PRODUCT.version,
            product_generation=7,
            contract_digest=CONTRACT.digest,
            input_generation_digests=(INPUT_GENERATION_DIGEST,),
            input_cardinality_evidence_digest=INPUT_CARDINALITY_EVIDENCE_DIGEST,
            execution_authorization_digest="8" * 64,
            legality_decision_digest="9" * 64,
            physical_plan_digest="3" * 64,
            compiled_model_digest="7" * 64,
            output_schema_digest="4" * 64,
            output_row_count=1,
            provider_commit_reference="commit:materialization-1",
            dbt_manifest_digest="5" * 64,
            dbt_run_results_digest="6" * 64,
            lineage_digest=LINEAGE_DIGEST,
            quality_assertion_count=2,
            quality_disposition="passed",
            committed_at=NOW - timedelta(minutes=10),
            retained_until=NOW + timedelta(hours=1),
        )
        receipt_ref = ArtifactReference(
            artifact_id=self.receipt.run_id,
            version=7,
            digest=digest(self.receipt),
        )
        self.metadata = ApprovedProductVersionMetadata(
            tenant_id=TENANT,
            product_ref=PRODUCT,
            generation=7,
            contract_ref=CONTRACT,
            semantic_version_ref=SEMANTIC,
            materialization_receipt_ref=receipt_ref,
            lineage_digest=LINEAGE_DIGEST,
            approved_narrative_terms=("Revenue",),
            recorded_at=NOW - timedelta(minutes=9),
        )
        self.freshness = SourceFreshnessObservation(
            observation_id="freshness-1",
            tenant_id=TENANT,
            version=1,
            source_ref="source:billing",
            input_generation_digest=INPUT_GENERATION_DIGEST,
            data_observation_ref=ArtifactReference(
                artifact_id="land-generation-1", version=1, digest="7" * 64
            ),
            watermark_at=NOW - timedelta(minutes=5),
            observed_at=NOW - timedelta(minutes=4),
        )

    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None:
        if (tenant_id, product_id, product_revision, product_generation) != (
            TENANT,
            PRODUCT.artifact_id,
            PRODUCT.version,
            7,
        ):
            return None
        return self.receipt

    def read_for_generation(
        self, *, tenant_id: str, input_generation_digest: str
    ) -> SourceFreshnessObservation | None:
        if (tenant_id, input_generation_digest) != (TENANT, INPUT_GENERATION_DIGEST):
            return None
        return self.freshness

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductVersionMetadata | None:
        if (tenant_id, product_ref, generation) != (TENANT, PRODUCT, 7):
            return None
        return self.metadata


class _Cursor:
    columns: tuple[AnswerQueryColumn, ...] = (
        AnswerQueryColumn(name="revenue", value_type="decimal"),
    )
    suppressed_group_count = 0

    def __init__(self) -> None:
        self._rows = iter(((Decimal("12.50"),),))

    def fetchone(self) -> tuple[AnswerQueryValue, ...] | None:
        return next(self._rows, None)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def engine_kind(self) -> Literal["postgresql"]:
        return "postgresql"

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        assert request.read_only is True
        self.calls += 1
        return _Cursor()


def _policy(deployment: GovernedConsoleDeployment) -> AnswerScopePolicy:
    runtime = deployment.answer_runtime
    assert runtime is not None
    draft = AnswerScopePolicyDraft(
        policy_id="answer-policy-1",
        tenant_id=TENANT,
        revision=1,
        principal_scope=(REQUESTER_PRINCIPAL,),
        purposes=(PURPOSE,),
        semantic_version_ref=SEMANTIC,
        data_product_version_refs=(PRODUCT,),
        metric_version_refs=(METRIC,),
        dimension_refs=(),
        filter_domains=(),
        max_time_window=86_400,
        max_staleness=3600,
        quality_disposition="block",
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
    authority_refs = (
        "role:data_engineering_architect",
        "owner:product:revenue",
        "role:policy_authority",
    )
    approvals = tuple(
        AnswerScopePolicyApproval(
            approval_id=f"approval-{position}",
            tenant_id=TENANT,
            policy_id=draft.policy_id,
            policy_revision=draft.revision,
            policy_digest=digest(draft),
            authority_ref=authority_ref,
            actor_id=f"actor-{position}",
            decision="approve",
            created_at=NOW - timedelta(minutes=30),
        )
        for position, authority_ref in enumerate(authority_refs, start=1)
    )
    return AnswerScopePolicyLifecycle(runtime.policies, clock=lambda: NOW).activate(
        draft=draft,
        approvals=approvals,
        product_owner_bindings=(
            ProductOwnerAuthority(
                data_product_version_ref=PRODUCT,
                authority_ref="owner:product:revenue",
            ),
        ),
        period_scan_budget_threshold=2_000_000,
    )


def _signed_plan(validation_digest: str, key: Ed25519PrivateKey) -> GovernedQueryPlan:
    statement = (
        'SELECT SUM("source"."revenue") FROM "consumption"."sales" AS "source" '
        'HAVING COUNT(DISTINCT "source"."customer_id") >= %s LIMIT 10'
    )
    parameters = ({"name": "p0", "value_type": "integer", "value": 5},)
    body: dict[str, object] = {
        "schema_version": "1",
        "plan_id": "plan-1",
        "tenant_id": TENANT,
        "validation_digest": validation_digest,
        "engine_kind": "postgresql",
        "compiler_version": "1",
        "allowlist_version": "governed-query-v1",
        "consumption_object_refs": (
            {"artifact_id": "consumption:sales", "version": 1, "digest": "8" * 64},
        ),
        "product_generation_refs": (
            {"product_ref": PRODUCT.model_dump(mode="python"), "generation": 7},
        ),
        "minimum_group_size": 5,
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
    plan_digest = digest(body)
    return GovernedQueryPlan.model_validate(
        {
            **body,
            "plan_digest": plan_digest,
            "signature": QueryPlanSigner("compiler-1", key).sign(plan_digest),
        }
    )


def test_fresh_query_reaches_delivered_and_serves_persisted_http_result_and_csv(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    evidence = _ProductEvidence()
    generations = _Generations()
    provider = _Provider()
    deployment = GovernedConsoleDeployment(
        tmp_path,
        answer_runtime_configuration=GovernedAnswerRuntimeConfiguration(
            connected_authority=_ConnectedAuthority(),
            connected_authority_ref="policy-authority:tenant-a",
            interpreter=_Interpreter(),
            materializations=evidence,
            freshness=evidence,
            product_metadata=evidence,
            generations=generations,
            signature_verifier=QueryPlanVerifier({"compiler-1": key.public_key()}),
            provider_resolver=lambda engine_kind: provider,
            clock=lambda: NOW,
            sleeper=lambda delay: None,
            intent_identifier=lambda: "intent-1",
            validation_identifier=lambda: "validation-1",
            admission_identifier=lambda: "admission-1",
        ),
    )
    try:
        runtime = deployment.answer_runtime
        assert runtime is not None
        request = deployment.requests.submit_question(
            tenant_id=TENANT,
            requester_id=REQUESTER,
            purpose=PURPOSE,
            question="What is revenue?",
            title="Monthly revenue",
        )
        investigating = deployment.requests.transition(
            TENANT,
            request.request_id,
            RequestState.INVESTIGATING,
            actor_id="architect-a",
            expected_revision=request.revision,
        )
        policy = _policy(deployment)
        snapshot = runtime.entitlements.resolve_current(
            tenant_id=TENANT,
            principal_ref=REQUESTER_PRINCIPAL,
            purpose_digest=digest(PURPOSE),
        )
        question = AnswerQuestion(
            tenant_id=TENANT,
            request_id=request.request_id,
            request_revision=investigating.revision,
            question_digest=digest(request.payload),
            interpreter="form",
            interpreter_ref="answer-form-v1",
        )
        validated = runtime.questions.interpret_and_validate(
            question=question,
            policy=policy,
            context=AnswerValidationContext(
                semantic_version_digest=SEMANTIC.digest,
                entitlement_snapshot_digest=snapshot.snapshot_digest,
                bindings=(
                    BoundSemanticReference(
                        canonical_ref="revenue",
                        kind="metric",
                        aliases=(),
                        version_ref=METRIC,
                        product_version_ref=PRODUCT,
                    ),
                ),
                entitled_refs=("revenue",),
                answer_enabled_product_refs=(PRODUCT,),
                product_generation_refs=(
                    RequestProductGenerationReference(product_ref=PRODUCT, generation=7),
                ),
                product_staleness=300,
                quality_blocked=False,
                authority_conflict=False,
                requester_principal_ref=REQUESTER_PRINCIPAL,
                purpose=PURPOSE,
                acting_as_agent=False,
                latest_policy_revision=policy.revision,
            ),
        )
        assert validated.validation.outcome == "admitted"
        plan = runtime.plans.save(_signed_plan(digest(validated.validation), key))
        admission = runtime.policy_admissions.admit(
            intent=validated.intent,
            validation=validated.validation,
            policy=policy,
            plan=plan,
            restatement_acceptance_ref=None,
            current_entitlement_snapshot_digest=snapshot.snapshot_digest,
            latest_policy_revision=policy.revision,
            actor_id="system:answer-policy",
        )
        assert admission.receipt is not None

        answer = runtime.execute_answer(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id="heinzel-runtime",
            expected_revision=admission.request.revision,
        )
        replay = runtime.execute_answer(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id="heinzel-runtime",
            expected_revision=admission.request.revision,
        )
        runtime.incidents.append(
            IncidentRecord(
                incident_id="query-incident-private",
                tenant_id=TENANT,
                revision=1,
                kind="query_failure",
                classification="permanent",
                last_successful_stage="catalog_publication",
                failed_stage="governed_query",
                user_impact="A separate governed question could not be completed.",
                next_automatic_action=None,
                allowed_operator_actions=(),
                source_service="runtime",
                source_record_ref="execution-receipt-private",
                run_id=None,
                run_attempt_number=None,
                evidence_refs=("evidence-private",),
                opened_at=NOW,
                updated_at=NOW,
            ),
            expected_current_revision=0,
        )

        with TestClient(deployment.build_app(actor=REQUESTER)) as client:
            result = client.get(f"/api/v1/requests/{request.request_id}/result")
            csv = client.get(f"/api/v1/requests/{request.request_id}/result.csv")
        with TestClient(deployment.build_app(actor=ARCHITECT)) as client:
            incident_response = client.get("/api/v1/incidents")

        assert replay == answer
        assert deployment.requests.get(TENANT, request.request_id).state is RequestState.DELIVERED
        assert provider.calls == 1
        assert result.status_code == 200
        assert result.json()["data"]["rows"] == [["12.50"]]
        assert csv.status_code == 200
        assert csv.content == b"revenue\r\n12.50\r\n"
        assert incident_response.status_code == 200
        incident_payload = incident_response.json()["data"]["incidents"][0]
        assert incident_payload["kind"] == "query_failure"
        assert incident_payload["user_impact"] == (
            "A separate governed question could not be completed."
        )
        assert "query-incident-private" not in incident_response.text
        assert "execution-receipt-private" not in incident_response.text
        assert "evidence-private" not in incident_response.text
        assert len(runtime.downloads.list_for_request(TENANT, request.request_id)) == 1
    finally:
        deployment.close()


@pytest.mark.live
def test_native_postgresql_and_https_authority_deliver_fresh_http_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.acceptance.console_native_answer_fixture import (
        CONTRACT as NATIVE_CONTRACT,
    )
    from tests.acceptance.console_native_answer_fixture import (
        PRODUCT as NATIVE_PRODUCT,
    )
    from tests.acceptance.console_native_answer_fixture import (
        fresh_native_answer_deployment,
    )

    with fresh_native_answer_deployment(tmp_path / "native", monkeypatch=monkeypatch) as native:
        runtime = native.deployment.answer_runtime
        assert runtime is not None
        with TestClient(native.deployment.build_app(actor=REQUESTER)) as client:
            result = client.get(f"/api/v1/requests/{native.request_id}/result")
            csv = client.get(f"/api/v1/requests/{native.request_id}/result.csv")
            products = client.get("/api/v1/data-products")
            product = client.get(f"/api/v1/data-products/{NATIVE_PRODUCT.artifact_id}")
            requester_dashboards = client.get("/api/v1/dashboards")
            workspace = client.get("/api/v1/workspace")

            assert len(runtime.downloads.list_for_request(TENANT, native.request_id)) == 1
            native.revoke_access()
            revoked_result = client.get(f"/api/v1/requests/{native.request_id}/result")
            revoked_csv = client.get(f"/api/v1/requests/{native.request_id}/result.csv")
        with TestClient(native.deployment.build_app(actor="architect-a")) as architect_client:
            dashboards = architect_client.get("/api/v1/dashboards")
            catalog = architect_client.get("/api/v1/catalog")

        assert result.status_code == 200
        assert result.json()["data"]["rows"] == [
            ["east", "99.000000000"],
            ["west", "30.000000000"],
        ]
        assert csv.status_code == 200
        assert csv.content == (
            b"region,total_revenue\r\neast,99.000000000\r\nwest,30.000000000\r\n"
        )
        assert products.status_code == 200
        assert product.status_code == 200
        assert requester_dashboards.status_code == 200
        assert requester_dashboards.json()["data"]["dashboards"] == []
        assert dashboards.status_code == 200
        assert catalog.status_code == 200
        assert all(asset["owner"] is None for asset in catalog.json()["data"]["assets"])
        assert NATIVE_CONTRACT.contract_id not in catalog.text
        assert workspace.status_code == 200
        capability_states = {
            capability["capability_id"]: capability["state"]
            for capability in workspace.json()["data"]["capabilities"]
        }
        assert capability_states["warehouse-binding"] == "ready"
        assert capability_states["catalog-binding"] == "ready"
        assert capability_states["analyst-dashboard"] == "ready"
        dashboard = dashboards.json()["data"]["dashboards"][0]
        assert dashboard["display_name"] == "Current revenue by region"
        assert dashboard["state"] == "ready"
        assert dashboard["lifecycle_state"] == "active"
        assert dashboard["freshness"] == "current"
        assert dashboard["as_of"] == result.json()["data"]["as_of"]
        assert dashboard["access_state"] == "workspace_role"
        assert dashboard["dashboard_ref"].startswith("dashboard-")
        assert "superset" not in dashboards.text.lower()
        assert products.json()["data"]["products"] == [product.json()["data"]]
        assert product.json()["data"] == {
            "data_product_id": NATIVE_PRODUCT.artifact_id,
            "version": NATIVE_PRODUCT.version,
            "publication_status": "published",
            "name": "Current revenue by region",
            "description": "Approved revenue grouped by reporting region.",
            "product_revision": NATIVE_PRODUCT.version,
            "generation": 1,
            "catalog_revision": 1,
            "namespace": "contract_" + digest(NATIVE_CONTRACT)[:54],
            "relation_name": "product_revenue_v1_g1",
            "column_count": 2,
            "source_count": 1,
            "freshness_observed_at": product.json()["data"]["freshness_observed_at"],
        }
        assert revoked_result.status_code == 404
        assert revoked_csv.status_code == 404
        assert len(runtime.downloads.list_for_request(TENANT, native.request_id)) == 1


@pytest.mark.live
def test_native_result_view_does_not_imply_download_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.acceptance.console_native_answer_fixture import (
        fresh_native_answer_deployment,
    )

    with fresh_native_answer_deployment(
        tmp_path / "native-view-only",
        monkeypatch=monkeypatch,
        permissions=("query", "view"),
    ) as native:
        runtime = native.deployment.answer_runtime
        assert runtime is not None
        with TestClient(native.deployment.build_app(actor=REQUESTER)) as client:
            result = client.get(f"/api/v1/requests/{native.request_id}/result")
            csv = client.get(f"/api/v1/requests/{native.request_id}/result.csv")

        assert result.status_code == 200
        assert csv.status_code == 404
        assert runtime.downloads.list_for_request(TENANT, native.request_id) == ()
