from __future__ import annotations

import hashlib
import json
import os
import runpy
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pillarmesh_bi_control import (
    DashboardAnswerAuthority,
    DashboardControlService,
    DashboardDesiredState,
    DashboardProductGenerationReference,
    SQLiteDashboardRepository,
)
from pillarmesh_console.contracts import (
    AccessRevocationCommand,
    AdmissionCommand,
    CreateRequestCommand,
    DecisionCommand,
    ProposalPreparationCommand,
    RequestClarificationCommand,
    WarehouseBindingCommand,
)
from pillarmesh_console.governed_backend import _catalog_classification_label
from pillarmesh_console.request_intake import request_intake_content
from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    SemanticObject,
    digest,
)
from pillarmesh_contract_service import SourceObservation, SQLiteSourceObservationRepository
from pillarmesh_provider_sdk.bi import BiApplyResult, BiDashboardDefinition
from pillarmesh_request_management import (
    DeliveryIntent,
    DimensionIntent,
    FreshnessObjective,
    Grain,
    MeasureIntent,
    ProductIntent,
    ProductIntentAuthorityRefs,
    ProductIntentConstraints,
    ProductIntentSourceCoverage,
    RequestState,
    SQLiteRequestRepository,
)
from pillarmesh_semantic_registry import SQLiteSemanticVersionRepository
from pillarmesh_warehouse_control import EngineKind
from starlette.testclient import TestClient

import tests.acceptance.console_postgresql_engine as postgresql_engine
from tests.acceptance.run_console_governed import (
    ARCHITECT,
    ARCHITECT_PRINCIPAL,
    DATA_OWNER,
    IMPACT_OWNER,
    POLICY_APPROVER,
    REQUESTER,
    REQUESTER_PRINCIPAL,
    TENANT,
    GovernedConsoleDeployment,
    _context,
    _PublishedAuthorityResolver,
    default_state_directory,
)
from tests.acceptance.run_plan3b import ScenarioAnswerProvider

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


class _AcceptanceDashboardProvider:
    provider_kind = "superset"

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        return BiApplyResult(
            stable_external_key=definition.stable_external_key,
            desired_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url=f"https://superset.invalid/dashboard/{definition.stable_external_key}",
            provider_version="acceptance-v1",
        )


class _RecordingAnswerProvider(ScenarioAnswerProvider):
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, **kwargs):
        self.calls += 1
        return super().propose(**kwargs)


def _prepare_question(
    deployment: GovernedConsoleDeployment,
    *,
    purpose: str,
    question: str,
):
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Test",
            "request": {
                "kind": "stakeholder_question",
                "purpose": purpose,
                "question": question,
            },
        }
    )
    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})
    created = deployment.backend.create_request(_context(REQUESTER), command)
    clarified = deployment.backend.clarify_request(
        _context(ARCHITECT),
        created.request_id,
        RequestClarificationCommand(
            expected_revision=created.revision,
            active_role="data_architect",
            restated_request="Report current monthly recurring revenue.",
            in_scope_summary="The current governed MRR metric only.",
            out_of_scope_summary="Customer-level subscription records.",
        ),
    )
    prepared = deployment.backend.prepare_request_proposal(
        _context(ARCHITECT),
        created.request_id,
        ProposalPreparationCommand(
            expected_revision=clarified.revision,
            active_role="data_architect",
        ),
    )
    return created.request_id, prepared


@pytest.fixture
def deployment(tmp_path: Path):
    running = GovernedConsoleDeployment(tmp_path)
    try:
        yield running
    finally:
        running.close()


def test_the_deployment_composes_the_supplied_answer_provider(tmp_path: Path) -> None:
    provider = _RecordingAnswerProvider()
    running = GovernedConsoleDeployment(tmp_path, answer_candidate_provider=provider)
    try:
        running.seed()
    finally:
        running.close()

    assert provider.calls == 1


def test_deployment_projects_bound_impact_with_safe_labels(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()

    with TestClient(deployment.build_app(actor=ARCHITECT)) as client:
        response = client.get(f"/api/v1/inbox/{seeded.request_id}/impact")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["subject_label"] == "Net revenue"
    assert payload["validated_impacts"][0]["label"] == "Revenue data product"
    assert payload["validated_impacts"][0]["owner_label"] == "Finance data owner"
    assert payload["added_approvers"][0]["authority_label"] == "Finance data owner"
    serialized = response.text
    assert "context-graph" not in serialized
    assert "contract-revenue" not in serialized


def test_published_semantic_term_resolves_without_an_exact_scenario_string(
    deployment: GovernedConsoleDeployment,
) -> None:
    _, prepared = _prepare_question(
        deployment,
        purpose="Prepare the finance glossary review.",
        question="Please explain net revenue for the board.",
    )

    assert prepared.proposal is not None
    assert prepared.proposal.kind == "stakeholder_answer"
    assert prepared.proposal.candidate == "Net revenue is gross revenue less approved refunds."


def test_published_authority_uses_the_active_publication_contract(
    deployment: GovernedConsoleDeployment,
) -> None:
    request_id, _ = _prepare_question(
        deployment,
        purpose="Prepare the finance glossary review.",
        question="Please explain net revenue for the board.",
    )
    receipt = deployment.publication_repository.list_publications(tenant_id=TENANT)[0]
    intent, _, _ = deployment.publication_repository.load_publication(
        tenant_id=TENANT, publication_id=receipt.publication_id
    )
    semantic_version, integration_contract = deployment.publication_repository.load_inputs(
        tenant_id=TENANT, operation_id=intent.operation_id
    )
    resolver = _PublishedAuthorityResolver(
        publications=deployment.publication_repository,
        publication_id=receipt.publication_id,
        clock=lambda: _NOW,
    )

    authority = resolver.resolve(
        tenant_id=TENANT,
        request=deployment.requests.get(TENANT, request_id),
    )

    assert authority.classification_rule_refs == tuple(
        ArtifactReference(
            artifact_id=classification.object_id,
            version=semantic_version.version,
            digest=digest(classification),
        )
        for classification in semantic_version.classifications
    )
    assert authority.policy_authority_classifications == tuple(
        classification.object_id for classification in semantic_version.classifications
    )
    assert authority.permitted_data_product_refs == (
        ArtifactReference(
            artifact_id=integration_contract.destination_product.product_name,
            version=integration_contract.version,
            digest=digest(integration_contract.destination_product),
        ),
    )


def test_published_catalog_objects_are_listed_and_openable(
    deployment: GovernedConsoleDeployment,
) -> None:
    listing = deployment.backend.get_catalog_assets(_context(ARCHITECT))

    assert listing.assets
    selected = listing.assets[0]
    assert selected.display_name
    assert deployment.backend.get_catalog_asset(_context(ARCHITECT), selected.asset_ref) == selected

    workspace = deployment.backend.get_workspace(_context(ARCHITECT))
    preview = next(
        capability
        for capability in workspace.capabilities
        if capability.capability_id == "catalog-asset-preview"
    )
    assert preview.state == "ready"
    assert preview.dependency is None


def test_the_catalog_lists_only_the_publication_questions_are_answered_from(
    deployment: GovernedConsoleDeployment,
) -> None:
    from datetime import timedelta

    from tests.acceptance.run_plan3b import _SEMANTIC_SUPPORT as support

    # A newer publication recorded after the deployment chose its active one. Listing it would
    # advertise a term that preparation refuses, because answers come from the active publication.
    version = support["semantic_version"]().model_copy(
        update={
            "semantic_version_id": "semantic-revenue-superseding",
            "entities": (
                support["SemanticObject"](
                    object_id="invoice-line",
                    name="Invoice line",
                    definition="One line of an issued customer invoice.",
                    source_refs=("process-revenue",),
                ),
            ),
        }
    )
    integration_contract = support["contract"](version)
    intent = support["publication_intent"](
        binding=support["CatalogBinding"](
            binding_id="catalog-a",
            tenant_id=TENANT,
            capability_profile_digest=support["DIGEST"],
            lifecycle_state=support["CatalogBindingState"].READY,
            revision=1,
            created_at=support["NOW"],
            updated_at=support["NOW"],
            provisioned_at=support["NOW"],
        ),
        semantic_version=version,
        contract=integration_contract,
    )
    repository = deployment.publication_repository
    repository.store_intent(intent=intent, semantic_version=version, contract=integration_contract)
    observation = support["CatalogObjectSnapshot"](
        tenant_key=TENANT,
        stable_identity="private-provider-object-superseding",
        logical_identity="private-logical-object-superseding",
        object_kind="namespace",
        normalized_payload={},
        normalized_digest=digest({}),
    )
    repository.store_receipt(
        intent=intent,
        receipt=support["CatalogPublicationReceipt"](
            publication_id="publication-revenue-superseding",
            tenant_id=TENANT,
            intent_digest=digest(intent),
            provider_version="private-provider-version",
            published_refs=(support["reference"](version.semantic_version_id, payload=version),),
            round_trip_observation_digest=digest((observation,)),
            published_at=support["NOW"] + timedelta(days=1),
        ),
        references=(
            support["CatalogObjectRef"](
                tenant_key=TENANT,
                stable_identity=observation.stable_identity,
                normalized_digest=support["DIGEST"],
            ),
        ),
        observations=(observation,),
    )

    names = {
        asset.display_name
        for asset in deployment.backend.get_catalog_assets(_context(ARCHITECT)).assets
    }

    assert "Invoice" in names
    assert "Invoice line" not in names


def test_catalog_contract_digest_is_projected_as_a_user_facing_classification() -> None:
    contract_digest = "a" * 64

    assert (
        _catalog_classification_label(
            contract_digest,
            contract_digest=contract_digest,
            semantic_names={},
        )
        == "Governed by approved contract"
    )


def test_postgresql_tls_material_uses_the_host_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deterministic scenario clock must not expire live engine credentials."""
    observed_at: list[datetime] = []
    generate = postgresql_engine.run_operation_secrets

    def record_clock(*, clock):
        observed_at.append(clock())
        return generate(clock=clock)

    monkeypatch.setattr(postgresql_engine, "run_operation_secrets", record_clock)
    before = datetime.now(UTC)
    running = GovernedConsoleDeployment(tmp_path, engine="postgresql")
    after = datetime.now(UTC)
    running.close()

    assert len(observed_at) == 1
    assert before <= observed_at[0] <= after


def test_interactive_transactions_use_the_wall_clock(tmp_path: Path) -> None:
    before = datetime.now(UTC)
    running = GovernedConsoleDeployment(tmp_path)
    try:
        request = running.requests.submit_question(
            tenant_id=TENANT,
            requester_id=REQUESTER,
            purpose="semantic definition",
            question="What does net revenue mean?",
        )
    finally:
        running.close()
    after = datetime.now(UTC)

    assert before <= request.updated_at <= after


def test_governed_runtime_accepts_data_access_intake_for_owned_fulfillment(
    deployment: GovernedConsoleDeployment,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Revenue export",
            "request": {
                "kind": "data_access",
                "purpose": "Prepare the quarterly review.",
                "data_product_ref": "product-revenue",
                "requested_fields": ["net-revenue"],
                "access_mode": "query",
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        }
    )
    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})

    created = deployment.backend.create_request(_context(REQUESTER), command)

    assert created.kind == "data_access"
    assert created.state == "submitted"


def test_governed_runtime_applies_and_delivers_approved_access(
    deployment: GovernedConsoleDeployment,
) -> None:
    intent, _, _ = deployment.publication_repository.load_publication(
        tenant_id=TENANT,
        publication_id=deployment.active_publication_id,
    )
    _, contract = deployment.publication_repository.load_inputs(
        tenant_id=TENANT,
        operation_id=intent.operation_id,
    )
    field = intent.semantic_objects[0].object_id
    expires_at = datetime.now(UTC) + timedelta(days=1)
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Governed revenue access",
            "request": {
                "kind": "data_access",
                "purpose": "Review the governed revenue definition.",
                "data_product_ref": contract.destination_product.product_name,
                "requested_fields": [field, "unpublished-field"],
                "access_mode": "query",
                "expires_at": expires_at.isoformat(),
            },
        }
    )
    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})
    created = deployment.backend.create_request(_context(REQUESTER), command)
    request = deployment.requests.get(TENANT, created.request_id)
    deployment.fulfillment.clarify_outcome(
        tenant_id=TENANT,
        request_id=request.request_id,
        actor_id=ARCHITECT,
        restated_request="Provide query access to the published revenue field.",
        in_scope_summary="The published revenue field.",
        out_of_scope_summary="Unpublished fields.",
        expected_revision=request.revision,
    )
    investigating = deployment.requests.get(TENANT, request.request_id)
    proposal = deployment.fulfillment.propose_access(
        tenant_id=TENANT,
        request_id=request.request_id,
        actor_id=ARCHITECT,
        expected_revision=investigating.revision,
    )
    awaiting = deployment.fulfillment.submit_proposal(
        tenant_id=TENANT,
        request_id=request.request_id,
        actor_id=ARCHITECT,
        expected_revision=proposal.request_revision,
    )
    actors = {
        REQUESTER_PRINCIPAL: REQUESTER,
        IMPACT_OWNER: DATA_OWNER,
        "role:policy_authority": POLICY_APPROVER,
    }
    for requirement in proposal.required_approvals:
        deployment.fulfillment.record_approval(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    detail = deployment.backend.get_request_detail(_context(ARCHITECT), request.request_id)
    assert detail.proposal_digest is not None

    delivered = deployment.backend.admit_request(
        _context(ARCHITECT),
        request.request_id,
        AdmissionCommand(
            expected_revision=awaiting.revision,
            reviewed_digest=detail.proposal_digest,
            active_role="data_architect",
        ),
    )
    requester = next(
        item
        for item in deployment.backend.get_requester_requests(_context(REQUESTER))
        if item.request_id == request.request_id
    )

    assert delivered.state == "delivered"
    assert requester.delivered_access is not None
    assert requester.delivered_access.fields == (field,)
    assert "grant_id" not in requester.delivered_access.model_dump()
    assert requester.access_lifecycle is not None
    assert requester.access_lifecycle.state == "active"
    assert requester.access_lifecycle.title == "Access is active"
    assert "grant-" not in requester.model_dump_json()

    revoked = deployment.backend.revoke_access(
        _context(REQUESTER),
        request.request_id,
        AccessRevocationCommand(
            expected_revision=requester.access_lifecycle.revision,
            active_role="requester",
            reason="The governed review is complete.",
        ),
    )
    refreshed = next(
        item
        for item in deployment.backend.get_requester_requests(_context(REQUESTER))
        if item.request_id == request.request_id
    )
    architect_detail = deployment.backend.get_request_detail(
        _context(ARCHITECT), request.request_id
    )

    assert revoked.state == "revoked"
    assert not revoked.can_revoke
    assert refreshed.access_lifecycle == revoked
    assert architect_detail.access_lifecycle == revoked
    assert "grant-" not in revoked.model_dump_json()


def test_requester_dashboard_disappears_after_authoritative_access_revocation(
    tmp_path: Path,
) -> None:
    dashboard_repository = SQLiteDashboardRepository(str(tmp_path / "dashboards.sqlite3"))
    dashboard_control = DashboardControlService(
        dashboard_repository,
        _AcceptanceDashboardProvider(),
        clock=lambda: _NOW,
    )
    deployment = GovernedConsoleDeployment(tmp_path, dashboards=dashboard_control)
    try:
        intent, _, _ = deployment.publication_repository.load_publication(
            tenant_id=TENANT,
            publication_id=deployment.active_publication_id,
        )
        _, contract = deployment.publication_repository.load_inputs(
            tenant_id=TENANT,
            operation_id=intent.operation_id,
        )
        field = intent.semantic_objects[0].object_id
        command = CreateRequestCommand.model_validate(
            {
                "expected_revision": 1,
                "request_digest": "0" * 64,
                "active_role": "requester",
                "title": "Governed revenue dashboard",
                "request": {
                    "kind": "data_access",
                    "purpose": "Review the governed revenue dashboard.",
                    "data_product_ref": contract.destination_product.product_name,
                    "requested_fields": [field],
                    "access_mode": "dashboard",
                    "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                },
            }
        )
        command = command.model_copy(
            update={"request_digest": digest(request_intake_content(command))}
        )
        created = deployment.backend.create_request(_context(REQUESTER), command)
        request = deployment.requests.get(TENANT, created.request_id)
        deployment.fulfillment.clarify_outcome(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            restated_request="Provide dashboard access to the published revenue field.",
            in_scope_summary="The published revenue field.",
            out_of_scope_summary="Unpublished fields.",
            expected_revision=request.revision,
        )
        investigating = deployment.requests.get(TENANT, request.request_id)
        proposal = deployment.fulfillment.propose_access(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            expected_revision=investigating.revision,
        )
        awaiting = deployment.fulfillment.submit_proposal(
            tenant_id=TENANT,
            request_id=request.request_id,
            actor_id=ARCHITECT,
            expected_revision=proposal.request_revision,
        )
        actors = {
            REQUESTER_PRINCIPAL: REQUESTER,
            ARCHITECT_PRINCIPAL: ARCHITECT,
            IMPACT_OWNER: DATA_OWNER,
            "role:policy_authority": POLICY_APPROVER,
        }
        for requirement in proposal.required_approvals:
            deployment.fulfillment.record_approval(
                tenant_id=TENANT,
                request_id=request.request_id,
                actor_id=actors[requirement.authority_ref],
                authority_ref=requirement.authority_ref,
                subject_digest=requirement.subject_digest,
                decision="approve",
                expected_revision=awaiting.revision,
            )
        detail = deployment.backend.get_request_detail(_context(ARCHITECT), request.request_id)
        assert detail.proposal_digest is not None
        deployment.backend.admit_request(
            _context(ARCHITECT),
            request.request_id,
            AdmissionCommand(
                expected_revision=awaiting.revision,
                reviewed_digest=detail.proposal_digest,
                active_role="data_architect",
            ),
        )
        grant = deployment.access_grants.load_current_for_request(TENANT, request.request_id)
        assert grant is not None

        metric_ref = ArtifactReference(artifact_id=field, version=1, digest="3" * 64)
        dashboard_control.apply(
            DashboardDesiredState(
                tenant_id=TENANT,
                dashboard_id="governed-revenue",
                version=1,
                revision=1,
                prior_desired_digest=None,
                title="Governed revenue dashboard",
                contract_digest="4" * 64,
                contract_key_id="acceptance-key",
                contract_signature="acceptance-signature",
                source_answer=DashboardAnswerAuthority(
                    tenant_id=TENANT,
                    request_id=request.request_id,
                    request_revision=grant.revision,
                    answer_id="answer-dashboard-access",
                    title="Governed revenue dashboard",
                    execution_receipt_ref="execution-dashboard-access",
                    result_ref="result-dashboard-access",
                    result_digest="5" * 64,
                    product_generation_refs=(
                        DashboardProductGenerationReference(
                            product_ref=grant.data_product_version_ref,
                            generation=1,
                        ),
                    ),
                    metric_version_refs=(metric_ref,),
                    as_of=_NOW,
                    freshness_disposition="current",
                    delivered_at=_NOW,
                ),
                dataset_product_ref=grant.data_product_version_ref,
                dataset_generation=1,
                consumption_object_ref=ArtifactReference(
                    artifact_id="consumption:governed-revenue", version=1, digest="6" * 64
                ),
                materialization_receipt_ref=ArtifactReference(
                    artifact_id="materialization:governed-revenue", version=1, digest="7" * 64
                ),
                product_publication_ref=ArtifactReference(
                    artifact_id="publication:governed-revenue", version=1, digest="8" * 64
                ),
                dataset_namespace="analytics",
                dataset_relation_name="governed_revenue",
                warehouse_binding_id="warehouse-governed",
                warehouse_binding_revision=1,
                warehouse_binding_digest="9" * 64,
                connection_secret_ref="secret://governed/superset",
                metric_refs=(metric_ref,),
                dimension_refs=(),
                filter_refs=(),
                visual_intents=("table",),
                lifecycle_state="active",
            )
        )

        with TestClient(deployment.build_app(actor=REQUESTER)) as client:
            current = client.get("/api/v1/dashboards")
            lifecycle = next(
                item
                for item in deployment.backend.get_requester_requests(_context(REQUESTER))
                if item.request_id == request.request_id
            ).access_lifecycle
            assert lifecycle is not None
            deployment.backend.revoke_access(
                _context(REQUESTER),
                request.request_id,
                AccessRevocationCommand(
                    expected_revision=lifecycle.revision,
                    active_role="requester",
                    reason="The dashboard review is complete.",
                ),
            )
            revoked = client.get("/api/v1/dashboards")

        assert current.status_code == 200
        assert [item["display_name"] for item in current.json()["data"]["dashboards"]] == [
            "Governed revenue dashboard"
        ]
        assert revoked.status_code == 200
        assert revoked.json()["data"]["dashboards"] == []
    finally:
        deployment.close()
        dashboard_repository.close()


def test_interactive_actor_identifiers_are_projected_as_display_names(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        architect = client.get("/api/v1/session").json()["data"]
        requester = client.get("/api/v1/session", headers={"x-pillarmesh-actor": REQUESTER}).json()[
            "data"
        ]
        conversation = client.get(
            f"/api/v1/requests/{seeded.request_id}/conversation",
            headers={"x-pillarmesh-actor": REQUESTER},
        ).json()["data"]

    assert architect["actor"]["display_name"] == "Data architect"
    assert requester["actor"]["display_name"] == "Requester"
    assert conversation["messages"][0]["author_label"] == "Requester"


def test_unsupported_local_mrr_question_stops_before_candidate_generation(tmp_path: Path) -> None:
    provider = _RecordingAnswerProvider()
    running = GovernedConsoleDeployment(tmp_path, answer_candidate_provider=provider)
    try:
        request_id, prepared = _prepare_question(
            running,
            purpose="This is a test request",
            question="What is the current MRR",
        )

        assert running.requests.get(TENANT, request_id).state is RequestState.NO_VALID_PLAN
        assert running.fulfillment_repository.list_proposals(TENANT, request_id) == ()
        refusal = running.fulfillment_repository.list_no_valid_plans(TENANT, request_id)[-1]
        assert refusal.reason_codes == ("published_semantic_term_not_found",)
        assert refusal.requester_safe_explanation == (
            "The current governed catalog does not contain one unambiguous term for this question."
        )
        assert prepared.preparation_notes == (
            "No Valid Plan: published_semantic_term_not_found.",
            "Required change: Ask about one term in the workspace's current approved semantic "
            "publication.",
        )
        requester_view = running.backend.get_requester_requests(_context(REQUESTER))[0]
        serialized = requester_view.model_dump_json()
        assert requester_view.state == "no_valid_plan"
        assert requester_view.question == "What is the current MRR"
        assert requester_view.no_valid_plan_explanation == (
            "The current governed catalog does not contain one unambiguous term for this question."
        )
        assert "Ask about one term" not in serialized
        assert "published_semantic_term_not_found" not in serialized
        assert provider.calls == 0
    finally:
        running.close()


def test_question_without_a_published_semantic_term_is_refused(tmp_path: Path) -> None:
    provider = _RecordingAnswerProvider()
    running = GovernedConsoleDeployment(tmp_path, answer_candidate_provider=provider)
    try:
        request_id, _ = _prepare_question(
            running,
            purpose="semantic definition",
            question="What is the current MRR?",
        )

        assert running.requests.get(TENANT, request_id).state is RequestState.NO_VALID_PLAN
        assert running.fulfillment_repository.list_proposals(TENANT, request_id) == ()
        assert provider.calls == 0
    finally:
        running.close()


def test_a_term_embedded_inside_another_word_does_not_answer_the_question(
    deployment: GovernedConsoleDeployment,
) -> None:
    # "invoiced" contains the published term "Invoice" as a substring, but asks something else.
    request_id, _ = _prepare_question(
        deployment,
        purpose="Prepare the finance glossary review.",
        question="What is the invoiced total?",
    )

    assert deployment.requests.get(TENANT, request_id).state is RequestState.NO_VALID_PLAN
    assert deployment.fulfillment_repository.list_proposals(TENANT, request_id) == ()
    refusal = deployment.fulfillment_repository.list_no_valid_plans(TENANT, request_id)[-1]
    assert refusal.reason_codes == ("published_semantic_term_not_found",)


def test_a_question_naming_two_published_terms_is_refused_as_ambiguous(
    deployment: GovernedConsoleDeployment,
) -> None:
    request_id, prepared = _prepare_question(
        deployment,
        purpose="Prepare the finance glossary review.",
        question="How does net revenue relate to an invoice?",
    )

    refusal = deployment.fulfillment_repository.list_no_valid_plans(TENANT, request_id)[-1]
    assert refusal.reason_codes == ("published_semantic_term_ambiguous",)
    assert refusal.requester_safe_explanation == (
        "This question names more than one term in the current governed catalog. "
        "Ask about one term at a time."
    )
    assert prepared.preparation_notes == (
        "No Valid Plan: published_semantic_term_ambiguous.",
        "Required change: Ask about exactly one term in the workspace's current approved "
        "semantic publication.",
    )


def test_the_longest_published_term_wins_over_a_term_it_contains() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from pillarmesh_request_management import InboxRequest
    from pillarmesh_request_management.models import StakeholderQuestion

    from tests.acceptance.run_console_governed import _semantic_match

    customer = SimpleNamespace(object_id="customer", name="Customer")
    audited = SimpleNamespace(
        object_id="customer-audit-20260910081713", name="Customer audit 20260910081713"
    )
    intent = SimpleNamespace(semantic_objects=(customer, audited))
    publications = cast(Any, SimpleNamespace(load_publication=lambda **_: (intent, None, None)))

    def ask(question: str) -> object:
        return _semantic_match(
            publications=publications,
            tenant_id=TENANT,
            publication_id="publication-a",
            request=InboxRequest(
                request_id="request-a",
                tenant_id=TENANT,
                requester_id=REQUESTER,
                payload=StakeholderQuestion(purpose="glossary", question=question),
                state=RequestState.INVESTIGATING,
                revision=2,
                submitted_at=_NOW,
                updated_at=_NOW,
            ),
        )

    assert ask("What does Customer audit 20260910081713 mean?") is audited
    assert ask("What does Customer mean?") is customer
    assert ask("What does customer_audit_20260910081713 mean?") is audited


def test_a_term_is_not_found_inside_a_word_that_contains_non_ascii_letters() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from pillarmesh_request_management import InboxRequest
    from pillarmesh_request_management.models import StakeholderQuestion

    from tests.acceptance.run_console_governed import _semantic_match

    # "Umsätze" must read as one word; splitting at "ä" would make it name the term "Tze".
    tze = SimpleNamespace(object_id="tze", name="Tze")
    intent = SimpleNamespace(semantic_objects=(tze,))
    publications = cast(Any, SimpleNamespace(load_publication=lambda **_: (intent, None, None)))

    def ask(question: str) -> object:
        return _semantic_match(
            publications=publications,
            tenant_id=TENANT,
            publication_id="publication-a",
            request=InboxRequest(
                request_id="request-a",
                tenant_id=TENANT,
                requester_id=REQUESTER,
                payload=StakeholderQuestion(purpose="glossary", question=question),
                state=RequestState.INVESTIGATING,
                revision=2,
                submitted_at=_NOW,
                updated_at=_NOW,
            ),
        )

    assert ask("Was bedeuten die Umsätze?") is None
    assert ask("Was bedeutet Tze?") is tze


def test_the_deployment_serves_governed_reads_rather_than_fixtures(
    deployment: GovernedConsoleDeployment,
) -> None:
    with TestClient(deployment.build_app()) as client:
        session = client.get("/api/v1/session")
        workspace = client.get("/api/v1/workspace")

    assert session.status_code == 200
    assert session.headers["X-PillarMesh-Data-Provenance"] == "governed_local"
    assert workspace.json()["meta"]["data_provenance"] == "governed_local"


def test_the_seeded_decision_reaches_the_architect_inbox(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        inbox = client.get("/api/v1/inbox")
        detail = client.get(f"/api/v1/inbox/{seeded.request_id}")

    assert inbox.status_code == 200
    assert seeded.request_id in {item["request_id"] for item in inbox.json()["data"]["items"]}
    assert detail.status_code == 200
    assert detail.json()["data"]["state"] == "awaiting_approval"
    assert detail.json()["data"]["proposal_digest"] == seeded.proposal_digest
    approvals = detail.json()["data"]["proposal"]["required_approvals"]
    assert {approval["authority_ref"]: approval["authority_label"] for approval in approvals} == {
        REQUESTER_PRINCIPAL: "Requester",
        ARCHITECT_PRINCIPAL: "Data engineering architect",
        IMPACT_OWNER: "Finance data owner",
    }


def _withdraw(client: TestClient, request_id: str, expected_revision: int, key: str):
    requester = {"x-pillarmesh-actor": REQUESTER}
    token = client.get("/api/v1/session", headers=requester).json()["data"]["csrf_token"]
    return client.post(
        f"/api/v1/requests/{request_id}/withdrawal",
        json={"expected_revision": expected_revision, "active_role": "requester"},
        headers=requester
        | {"Origin": "http://127.0.0.1:8000", "X-CSRF-Token": token, "Idempotency-Key": key},
    )


def test_a_requester_withdraws_an_open_request_through_the_owning_service(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()
    revision = deployment.requests.get(TENANT, seeded.request_id).revision

    with TestClient(deployment.build_app()) as client:
        response = _withdraw(client, seeded.request_id, revision, "withdraw-open")

    assert response.status_code == 200
    assert response.json()["data"]["state"] == "cancelled"
    assert deployment.requests.get(TENANT, seeded.request_id).state is RequestState.CANCELLED


def test_a_request_that_reached_an_outcome_cannot_be_withdrawn(
    deployment: GovernedConsoleDeployment,
) -> None:
    request_id, prepared = _prepare_question(
        deployment,
        purpose="Prepare the finance glossary review.",
        question="What is the current MRR?",
    )

    with TestClient(deployment.build_app()) as client:
        response = _withdraw(client, request_id, prepared.revision, "withdraw-terminal")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "withdrawal_unavailable"
    assert deployment.requests.get(TENANT, request_id).state is RequestState.NO_VALID_PLAN


def test_a_retried_withdrawal_reports_the_request_it_already_withdrew(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()
    revision = deployment.requests.get(TENANT, seeded.request_id).revision

    with TestClient(deployment.build_app()) as client:
        first = _withdraw(client, seeded.request_id, revision, "withdraw-first")
        # A network retry resends the command the browser built, at the revision it read.
        retried = _withdraw(client, seeded.request_id, revision, "withdraw-retry")

    assert first.status_code == 200
    assert retried.status_code == 200
    assert retried.json()["data"]["state"] == "cancelled"
    assert retried.json()["data"]["revision"] == first.json()["data"]["revision"]


def test_a_withdrawal_against_a_stale_revision_changes_nothing(
    deployment: GovernedConsoleDeployment,
) -> None:
    seeded = deployment.seed()
    revision = deployment.requests.get(TENANT, seeded.request_id).revision

    with TestClient(deployment.build_app()) as client:
        response = _withdraw(client, seeded.request_id, revision - 1, "withdraw-stale")

    assert response.status_code == 409
    unchanged = deployment.requests.get(TENANT, seeded.request_id)
    assert unchanged.state is RequestState.AWAITING_APPROVAL


def test_admission_executes_and_delivers_the_admitted_answer(
    deployment: GovernedConsoleDeployment,
) -> None:
    setup = deployment.backend.get_setup(_context(ARCHITECT))
    deployment.backend.confirm_warehouse_binding(
        _context(ARCHITECT),
        WarehouseBindingCommand(
            expected_revision=setup.revision,
            reviewed_digest=setup.setup_digest,
            active_role="data_architect",
            engine="postgresql",
            region="us-west-2",
            capacity="mvp-fixed",
        ),
    )
    seeded = deployment.seed()
    proposal = deployment.fulfillment_repository.list_proposals(TENANT, seeded.request_id)[-1]
    request = deployment.requests.get(TENANT, seeded.request_id)
    actors = {
        REQUESTER_PRINCIPAL: REQUESTER,
        ARCHITECT_PRINCIPAL: ARCHITECT,
        IMPACT_OWNER: DATA_OWNER,
    }
    for requirement in proposal.required_approvals:
        deployment.fulfillment.record_approval(
            tenant_id=TENANT,
            request_id=seeded.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=request.revision,
        )
    detail = deployment.backend.admit_request(
        _context(ARCHITECT),
        seeded.request_id,
        AdmissionCommand(
            expected_revision=request.revision,
            reviewed_digest=seeded.proposal_digest,
            active_role="data_architect",
        ),
    )
    requester = deployment.backend.get_requester_requests(_context(REQUESTER))[0]

    assert detail.state == "delivered"
    assert requester.state == "delivered"
    assert requester.delivered_answer is not None
    assert requester.delivered_answer.answer_text == (
        "Net revenue is gross revenue less approved refunds."
    )
    assert requester.delivered_answer.delivery_ref.startswith("dlv-")


def test_a_delivery_that_fails_after_admission_can_be_retried(
    deployment: GovernedConsoleDeployment,
) -> None:
    from pillarmesh_console.errors import ConsoleConflict

    seeded = deployment.seed()
    proposal = deployment.fulfillment_repository.list_proposals(TENANT, seeded.request_id)[-1]
    request = deployment.requests.get(TENANT, seeded.request_id)
    actors = {
        REQUESTER_PRINCIPAL: REQUESTER,
        ARCHITECT_PRINCIPAL: ARCHITECT,
        IMPACT_OWNER: DATA_OWNER,
    }
    for requirement in proposal.required_approvals:
        deployment.fulfillment.record_approval(
            tenant_id=TENANT,
            request_id=seeded.request_id,
            actor_id=actors[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=request.revision,
        )

    # No warehouse binding is confirmed yet, so the answer cannot be checked for delivery.
    with pytest.raises(ConsoleConflict):
        deployment.backend.admit_request(
            _context(ARCHITECT),
            seeded.request_id,
            AdmissionCommand(
                expected_revision=request.revision,
                reviewed_digest=seeded.proposal_digest,
                active_role="data_architect",
            ),
        )
    stranded = deployment.backend.get_request_detail(_context(ARCHITECT), seeded.request_id)

    assert stranded.state == "execution_ready"
    assert stranded.admission is not None
    assert stranded.admission.available
    assert stranded.admission.pending_delivery

    setup = deployment.backend.get_setup(_context(ARCHITECT))
    deployment.backend.confirm_warehouse_binding(
        _context(ARCHITECT),
        WarehouseBindingCommand(
            expected_revision=setup.revision,
            reviewed_digest=setup.setup_digest,
            active_role="data_architect",
            engine="postgresql",
            region="us-west-2",
            capacity="mvp-fixed",
        ),
    )
    retried = deployment.backend.admit_request(
        _context(ARCHITECT),
        seeded.request_id,
        AdmissionCommand(
            expected_revision=stranded.revision,
            reviewed_digest=stranded.proposal_digest,
            active_role="data_architect",
        ),
    )

    assert retried.state == "delivered"
    assert deployment.requests.get(TENANT, seeded.request_id).state is RequestState.DELIVERED


def test_data_access_intake_availability_is_published_as_a_capability(
    deployment: GovernedConsoleDeployment,
) -> None:
    from pillarmesh_console.fixture_data import build_fixture_seed

    governed = {
        capability.capability_id: capability
        for capability in deployment.backend.get_workspace(_context(REQUESTER)).capabilities
    }
    fixture = {
        capability.capability_id: capability
        for capability in build_fixture_seed().workspace.capabilities
    }

    # The browser gates intake on this capability, so it must say what the server will accept.
    assert governed["data-access-intake"].state == "ready"
    assert "expires automatically" in governed["data-access-intake"].detail
    assert fixture["data-access-intake"].state == "ready"


def test_a_publication_store_from_another_catalog_binding_names_the_mismatch(
    tmp_path: Path,
) -> None:
    from tests.acceptance.run_plan3b import published_repository

    repository, _, _ = published_repository(check_same_thread=False)

    # The fixture publication was made through catalog binding "catalog-a"; a fresh state
    # directory mints its own binding, so the store cannot be served as this workspace's.
    with pytest.raises(ValueError, match="catalog-a") as refused:
        GovernedConsoleDeployment(tmp_path, publication_repository=repository)

    assert "bindings.json" in str(refused.value)


def test_the_seeded_request_is_owned_by_the_request_service_on_disk(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The console projection is never the evidence that a transaction committed."""
    seeded = deployment.seed()
    deployment.close()

    reopened = SQLiteRequestRepository.open(deployment.request_path)
    try:
        stored = reopened.load(TENANT, seeded.request_id)
    finally:
        reopened.close()

    assert stored is not None
    assert stored.requester_id == REQUESTER


def test_the_actor_header_selects_the_requester_surface(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The local harness switches actor so one browser can walk both sides."""
    deployment.seed()

    with TestClient(deployment.build_app()) as client:
        architect = client.get("/api/v1/session")
        requester = client.get("/api/v1/session", headers={"x-pillarmesh-actor": REQUESTER})
        unknown = client.get("/api/v1/session", headers={"x-pillarmesh-actor": "nobody"})

    assert architect.json()["data"]["actor"]["display_name"] == "Data architect"
    assert requester.json()["data"]["active_role"] == "requester"
    assert unknown.json()["data"]["active_role"] == "data_architect"


def test_access_approvers_can_open_the_request_their_authority_must_decide(
    deployment: GovernedConsoleDeployment,
) -> None:
    command = CreateRequestCommand.model_validate(
        {
            "expected_revision": 1,
            "request_digest": "0" * 64,
            "active_role": "requester",
            "title": "Governed revenue access",
            "request": {
                "kind": "data_access",
                "purpose": "Review the governed revenue definition.",
                "data_product_ref": "product-revenue",
                "requested_fields": ["net-revenue"],
                "access_mode": "query",
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        }
    )
    command = command.model_copy(update={"request_digest": digest(request_intake_content(command))})
    created = deployment.backend.create_request(_context(REQUESTER), command)
    clarified = deployment.backend.clarify_request(
        _context(ARCHITECT),
        created.request_id,
        RequestClarificationCommand(
            expected_revision=created.revision,
            active_role="data_architect",
            restated_request="Provide query access to the published revenue field.",
            in_scope_summary="The published revenue field.",
            out_of_scope_summary="Unpublished fields.",
        ),
    )
    prepared = deployment.backend.prepare_request_proposal(
        _context(ARCHITECT),
        created.request_id,
        ProposalPreparationCommand(
            expected_revision=clarified.revision,
            active_role="data_architect",
        ),
    )
    deployment.backend.submit_request_proposal(
        _context(ARCHITECT),
        created.request_id,
        ProposalPreparationCommand(
            expected_revision=prepared.revision,
            active_role="data_architect",
        ),
    )

    with TestClient(deployment.build_app()) as client:
        owner = client.get(
            f"/api/v1/inbox/{created.request_id}",
            headers={"x-pillarmesh-actor": DATA_OWNER},
        )
        policy = client.get(
            f"/api/v1/inbox/{created.request_id}",
            headers={"x-pillarmesh-actor": POLICY_APPROVER},
        )

    assert owner.status_code == 200
    assert owner.json()["data"]["proposal"]["required_approvals"][0]["authority_label"] == (
        "Finance data owner"
    )
    assert policy.status_code == 200
    assert policy.json()["data"]["proposal"]["required_approvals"][0]["authority_label"] == (
        "Policy authority"
    )

    owner_view = deployment.backend.get_request_detail(_context(DATA_OWNER), created.request_id)
    policy_view = deployment.backend.get_request_detail(
        _context(POLICY_APPROVER), created.request_id
    )
    assert owner_view.proposal_digest is not None
    assert policy_view.proposal_digest is not None
    deployment.backend.decide_request(
        _context(DATA_OWNER),
        created.request_id,
        DecisionCommand(
            expected_revision=owner_view.revision,
            reviewed_digest=owner_view.proposal_digest,
            active_role="data_owner",
            decision="approve",
        ),
    )
    decided_policy = deployment.backend.decide_request(
        _context(POLICY_APPROVER),
        created.request_id,
        DecisionCommand(
            expected_revision=policy_view.revision,
            reviewed_digest=policy_view.proposal_digest,
            active_role="policy_approver",
            decision="approve",
        ),
    )

    assert decided_policy.available_actions == ()


def test_the_catalog_capability_is_delivered_rather_than_reported_as_unwired(
    deployment: GovernedConsoleDeployment,
) -> None:
    """`CatalogControlBindingReader` was built and never composed.

    The console reported `catalog-binding: not_delivered - catalog-control read
    wiring` because the harness passed no reader, not because anything was missing.
    Composing catalog-control turns the capability live and is the cheapest real
    progress the console has left: the owning service already exists.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    catalog = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "catalog-binding"
    )
    assert catalog["state"] != "not_delivered"


def test_markdown_process_package_is_persisted_and_invalid_shapes_are_denied(
    deployment: GovernedConsoleDeployment,
) -> None:
    narrative = "# Revenue to cash\n\nInvoice settlement closes the process.\n"
    manifest = {
        "process_name": "Revenue to cash",
        "owner": "Finance operations",
        "participants": ["Billing", "Finance"],
        "outcomes": ["Settled invoice"],
        "entities": ["Invoice"],
        "events": ["Invoice settled"],
        "states": ["settled"],
        "rules": ["Only settled invoices close"],
        "source_references": ["billing-postgresql"],
        "unresolved_questions": [],
    }

    with TestClient(deployment.build_app()) as client:
        session = client.get("/api/v1/session").json()["data"]
        headers = {
            "Origin": "http://127.0.0.1:8000",
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "process-package-acceptance-1",
        }
        payload = {
            "expected_revision": 1,
            "package_digest": hashlib.sha256(narrative.encode()).hexdigest(),
            "active_role": "data_architect",
            "file_name": "revenue-to-cash.md",
            "media_type": "text/markdown; charset=utf-8",
            "narrative_markdown": narrative,
            "manifest": manifest,
        }
        accepted = client.post("/api/v1/setup/process-packages", headers=headers, json=payload)
        invalid_manifest = client.post(
            "/api/v1/setup/process-packages",
            headers=headers | {"Idempotency-Key": "process-package-invalid-manifest"},
            json=payload | {"manifest": manifest | {"invented_authority": True}},
        )
        unsupported_media = client.post(
            "/api/v1/setup/process-packages",
            headers=headers | {"Idempotency-Key": "process-package-unsupported-media"},
            json=payload | {"media_type": "application/pdf", "file_name": "process.pdf"},
        )

    assert accepted.status_code == 200
    operation = accepted.json()["data"]
    assert operation["state"] == "succeeded"
    assert operation["revision"] == 1
    latest = deployment.process_packages.latest(TENANT)
    assert latest is not None
    package_id = latest.receipt.package_id
    assert operation["summary"] == "Business process package saved."
    assert package_id not in operation["summary"]
    assert deployment.process_packages.get_original(TENANT, package_id, 1) == narrative.encode()
    stored_manifest = deployment.process_packages.get_manifest(TENANT, package_id, 1)
    assert json.loads(stored_manifest) == manifest | {"schema_version": "1"}
    with sqlite3.connect(deployment.process_package_path) as persisted:
        receipt = persisted.execute(
            "SELECT package_id, version, tenant_id, media_type, original_digest, uploader_id "
            "FROM process_packages WHERE package_id = ? AND version = ?",
            (package_id, 1),
        ).fetchone()
        receipt_count = persisted.execute("SELECT COUNT(*) FROM process_packages").fetchone()[0]
    assert receipt == (
        package_id,
        1,
        TENANT,
        "text/markdown; charset=utf-8",
        hashlib.sha256(narrative.encode()).hexdigest(),
        ARCHITECT,
    )
    assert receipt_count == 1
    assert invalid_manifest.status_code == 422
    assert invalid_manifest.json()["error"]["code"] == "invalid_request"
    assert invalid_manifest.json()["error"]["field"] == "manifest"
    assert unsupported_media.status_code == 422
    assert unsupported_media.json()["error"]["code"] == "invalid_request"
    assert unsupported_media.json()["error"]["field"] == "file_name"


def _seed_intent_authority(deployment: GovernedConsoleDeployment) -> ProductIntentAuthorityRefs:
    """Record the governed facts the approved intent relies on, as the owning services would."""
    now = datetime.now(UTC)
    semantic_versions = SQLiteSemanticVersionRepository(deployment.semantic_versions_path)
    observations = SQLiteSourceObservationRepository(deployment.source_observations_path)
    try:
        semantic_version = semantic_versions.store(
            ApprovedSemanticVersion(
                semantic_version_id="semantic-finance",
                tenant_id=TENANT,
                version=1,
                process_package_ref=ArtifactReference(
                    artifact_id="process-finance", version=1, digest="1" * 64
                ),
                candidate_set_digest="2" * 64,
                review_bundle_digest="3" * 64,
                entities=(
                    SemanticObject(
                        object_id="fiscal_quarter",
                        name="Fiscal quarter",
                        definition="The fiscal quarter a transaction belongs to.",
                        source_refs=("process-finance",),
                    ),
                ),
                events=(),
                states=(),
                relationships=(),
                identity_rules=(),
                constraints=(),
                metrics=(
                    SemanticObject(
                        object_id="net_revenue",
                        name="Net revenue",
                        definition="Revenue net of refunds.",
                        source_refs=("process-finance",),
                    ),
                ),
                classifications=(),
                authority_bindings=(),
                approval_ids=("semantic-approval-finance-1",),
                created_at=now - timedelta(days=1),
            )
        )
        observation = observations.store(
            SourceObservation(
                observation_id="source-observation-billing-catalog",
                tenant_id=TENANT,
                version=1,
                source_ref="billing-catalog",
                schema_digest="4" * 64,
                observed_at=now - timedelta(hours=1),
                valid_until=now + timedelta(days=1),
            )
        )
    finally:
        semantic_versions.close()
        observations.close()
    return ProductIntentAuthorityRefs(
        semantic_version=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        source_observations=(
            ArtifactReference(
                artifact_id=observation.observation_id,
                version=observation.version,
                digest=digest(observation),
            ),
        ),
    )


def test_external_interpreter_candidate_is_projected_and_approved_by_owning_service(
    deployment: GovernedConsoleDeployment,
) -> None:
    request = deployment.requests.submit_question(
        tenant_id=TENANT,
        requester_id=REQUESTER,
        purpose="Quarterly finance reporting",
        question="What is quarterly net revenue?",
    )
    candidates = deployment.product_intent_candidates
    authority_refs = _seed_intent_authority(deployment)
    intent = ProductIntent(
        request_id=request.request_id,
        title="Quarterly net revenue",
        business_outcome="Give finance one governed quarterly view.",
        source_refs=("billing-catalog",),
        grain=Grain(keys=("fiscal_quarter",)),
        measures=(MeasureIntent(metric_ref="net_revenue", aggregation="sum"),),
        dimensions=(DimensionIntent(dimension_ref="fiscal_quarter"),),
        filters=(),
        freshness=FreshnessObjective(maximum_age_seconds=86_400),
        delivery=DeliveryIntent(outputs=("table", "dashboard")),
    )
    constraints = ProductIntentConstraints(
        approved_source_refs=("billing-catalog",),
        approved_metric_refs=("net_revenue",),
        approved_dimension_refs=("fiscal_quarter",),
        minimum_source_interval_seconds=86_400,
    )

    with TestClient(deployment.build_app()) as client:
        assert (
            client.get(f"/api/v1/inbox/{request.request_id}").json()["data"]["product_intent"]
            is None
        )
        candidate = candidates.propose(
            tenant_id=TENANT,
            request_id=request.request_id,
            request_revision=request.revision,
            idempotency_key="external-interpreter-candidate-1",
            proposed_by="external-interpreter",
            intent=intent,
            constraints=constraints,
            source_coverage=(
                ProductIntentSourceCoverage(
                    source_ref="billing-catalog",
                    covered_fields=("fiscal_quarter", "net_revenue"),
                    authorized=True,
                ),
            ),
            unresolved_constraints=(),
            authority_refs=authority_refs,
        )
        projected = client.get(f"/api/v1/inbox/{request.request_id}")
        session = client.get("/api/v1/session").json()["data"]
        approved = client.post(
            f"/api/v1/inbox/{request.request_id}/product-intent/approval",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "X-CSRF-Token": session["csrf_token"],
                "Idempotency-Key": "approve-product-intent-1",
            },
            json={
                "expected_revision": request.revision,
                "reviewed_digest": intent.canonical_digest(),
                "active_role": "data_architect",
            },
        )

    assert projected.status_code == 200
    assert projected.json()["data"]["product_intent"]["reviewed_digest"] == digest(intent)
    assert projected.json()["data"]["product_intent"]["source_coverage"] == [
        {
            "source_ref": "billing-catalog",
            "covered_fields": ["fiscal_quarter", "net_revenue"],
            "authorized": True,
        }
    ]
    assert approved.status_code == 200
    assert approved.json()["data"]["intent_digest"] == candidate.intent.canonical_digest()


def test_a_proposer_cannot_make_an_intent_approvable_by_asserting_constraints(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The console approval route refuses when the governed records do not exist.

    The candidate claims every source, metric and dimension is approved, marks its source
    authorized, and references authority records that were never recorded. Before approval
    resolved constraints itself, the console passed the candidate's own constraints to approval
    and this intent would have been approved.
    """
    request = deployment.requests.submit_question(
        tenant_id=TENANT,
        requester_id=REQUESTER,
        purpose="Quarterly finance reporting",
        question="What is quarterly net revenue?",
    )
    intent = ProductIntent(
        request_id=request.request_id,
        title="Quarterly net revenue",
        business_outcome="Give finance one governed quarterly view.",
        source_refs=("billing-catalog",),
        grain=Grain(keys=("fiscal_quarter",)),
        measures=(MeasureIntent(metric_ref="net_revenue", aggregation="sum"),),
        dimensions=(DimensionIntent(dimension_ref="fiscal_quarter"),),
        filters=(),
        freshness=FreshnessObjective(maximum_age_seconds=86_400),
        delivery=DeliveryIntent(outputs=("table",)),
    )
    unrecorded = ArtifactReference(artifact_id="never-recorded", version=1, digest="e" * 64)

    with TestClient(deployment.build_app()) as client:
        deployment.product_intent_candidates.propose(
            tenant_id=TENANT,
            request_id=request.request_id,
            request_revision=request.revision,
            idempotency_key="asserted-constraints-candidate",
            proposed_by="external-interpreter",
            intent=intent,
            constraints=ProductIntentConstraints(
                approved_source_refs=("billing-catalog",),
                approved_metric_refs=("net_revenue",),
                approved_dimension_refs=("fiscal_quarter",),
                minimum_source_interval_seconds=86_400,
            ),
            source_coverage=(
                ProductIntentSourceCoverage(
                    source_ref="billing-catalog",
                    covered_fields=("fiscal_quarter", "net_revenue"),
                    authorized=True,
                ),
            ),
            unresolved_constraints=(),
            authority_refs=ProductIntentAuthorityRefs(
                semantic_version=unrecorded, source_observations=(unrecorded,)
            ),
        )
        session = client.get("/api/v1/session").json()["data"]
        refused = client.post(
            f"/api/v1/inbox/{request.request_id}/product-intent/approval",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "X-CSRF-Token": session["csrf_token"],
                "Idempotency-Key": "approve-asserted-constraints-1",
            },
            json={
                "expected_revision": request.revision,
                "reviewed_digest": intent.canonical_digest(),
                "active_role": "data_architect",
            },
        )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "product_intent_no_valid_plan"
    assert deployment.product_intent_approvals.list_for_request(TENANT, request.request_id) == ()


def test_the_meaning_review_capability_is_delivered_rather_than_reported_as_unwired(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The console reads review bundles from the semantic registry itself.

    Both seams existed on the governed backend and the harness wired neither, so the
    console reported `semantic-review: not_delivered - semantic-registry review
    wiring` for a service that has been implemented since Plan 2.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    review = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "semantic-review"
    )
    assert review["state"] == "ready"
    assert review["dependency"] is None


def test_restarting_reuses_the_bindings_the_previous_run_created(tmp_path: Path) -> None:
    """The workspace binding directory is deployment configuration, so it persists.

    Holding it only in memory meant a restart forgot which binding this workspace
    used. The warehouse binding became unreachable even though warehouse-control
    still held it, and the catalog binding was worse: the harness minted a fresh
    draft on every construction, so restarting stacked orphaned bindings the
    directory then abandoned.
    """
    first = GovernedConsoleDeployment(tmp_path)
    catalog_binding = first.bindings.catalog_binding_id(TENANT)
    first.bindings.bind_warehouse(tenant_id=TENANT, binding_id="whb-recorded-by-a-command")
    first.close()

    second = GovernedConsoleDeployment(tmp_path)
    try:
        assert second.bindings.catalog_binding_id(TENANT) == catalog_binding
        assert second.bindings.warehouse_binding_id(TENANT) == "whb-recorded-by-a-command"
    finally:
        second.close()


def test_running_deployments_observe_bindings_recorded_by_each_other(tmp_path: Path) -> None:
    first = GovernedConsoleDeployment(tmp_path)
    second = GovernedConsoleDeployment(tmp_path)
    try:
        first.bindings.bind_warehouse(
            tenant_id=TENANT, binding_id="whb-recorded-by-another-process"
        )

        assert second.bindings.warehouse_binding_id(TENANT) == "whb-recorded-by-another-process"
    finally:
        second.close()
        first.close()


def test_seeding_twice_does_not_leave_two_indistinguishable_decisions(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The state directory persists, so restarting the server re-enters `seed`.

    Minting a second identical stakeholder question each time left a queue of
    copies no reviewer could tell apart, and the printed identifier named only the
    newest.
    """
    first = deployment.seed()

    second = deployment.seed()

    assert second.request_id == first.request_id
    with TestClient(deployment.build_app()) as client:
        items = client.get("/api/v1/inbox").json()["data"]["items"]
    assert [item["request_id"] for item in items] == [first.request_id]


def test_a_localhost_host_is_refused_because_a_browser_does_not_treat_it_as_loopback() -> None:
    """The allowed origin is built from the bound spelling; a browser sends its own."""
    with pytest.raises(ValueError, match="loopback"):
        GovernedConsoleDeployment.require_loopback("localhost")


def test_the_default_state_directory_is_named_for_the_current_user() -> None:
    """`gettempdir()` is the shared `/tmp` on Linux and in CI."""
    assert str(os.getuid()) in default_state_directory().name


def test_the_default_state_directory_stays_out_of_the_repository() -> None:
    """A new directory at the repository root fails the repository-structure gate."""
    repository_root = Path(__file__).resolve().parents[2]

    default = default_state_directory()

    assert not default.is_relative_to(repository_root)


def test_fresh_postgresql_workspaces_allocate_distinct_warehouse_bindings(
    tmp_path: Path,
) -> None:
    """Separate acceptance workspaces must never address the same Docker resources.

    A fresh SQLite repository starts every tenant's binding sequence at one. The
    PostgreSQL provider derives its container, networks, and volume from that binding,
    so two local workspaces with the fixed acceptance tenant otherwise collide even
    though neither workspace has authority over the other's retained resources.
    """
    first = GovernedConsoleDeployment(tmp_path / "first", engine="postgresql")
    second = GovernedConsoleDeployment(tmp_path / "second", engine="postgresql")
    try:
        first_binding = first.control.create_draft(
            tenant_id=TENANT,
            engine_kind=EngineKind.POSTGRESQL,
            region="us-west-2",
            capacity_profile="mvp-fixed",
        )
        second_binding = second.control.create_draft(
            tenant_id=TENANT,
            engine_kind=EngineKind.POSTGRESQL,
            region="us-west-2",
            capacity_profile="mvp-fixed",
        )

        assert first_binding.binding_id != second_binding.binding_id
    finally:
        first.close()
        second.close()


def test_a_non_loopback_host_is_refused() -> None:
    """This harness has no authentication; it may not leave the machine."""
    with pytest.raises(ValueError, match="loopback"):
        GovernedConsoleDeployment.require_loopback("0.0.0.0")


def test_the_run_capability_is_delivered_rather_than_reported_as_undelivered(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The reads exist now, so the harness must actually compose them.

    Publishing `list_activated` and `list_runs_for_contracts` in the owning
    services delivers nothing on its own. Leaving them uncomposed is exactly how
    `catalog-binding` and `semantic-review` came to be reported as undelivered for
    services that had been implemented for two plans.
    """
    with TestClient(deployment.build_app()) as client:
        workspace = client.get("/api/v1/workspace").json()["data"]

    runs = next(
        capability
        for capability in workspace["capabilities"]
        if capability["capability_id"] == "data-product-runs"
    )
    assert runs["state"] == "ready"
    assert runs["dependency"] is None


def test_a_tenant_with_no_activated_contracts_reads_an_empty_run_listing(
    deployment: GovernedConsoleDeployment,
) -> None:
    """Delivered-and-empty is the honest answer, and it must not be a failure."""
    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/runs")

    assert response.status_code == 200
    assert response.json()["data"]["runs"] == []


def test_a_run_recorded_by_the_owning_services_reaches_the_console(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The tenant is derived, never stored on the run.

    The evidence record carries no tenant. It is reachable only because the
    contract digest it was witnessed under is activated for this tenant, which is
    the whole of the derivation this capability rests on.
    """
    deployment.lifecycles.activate(tenant_id=TENANT, contract_digest="a" * 64, activated_at=_NOW)
    deployment.evidence.create_run(
        "run-000000000000000000000001", "activation-1", "a" * 64, "b" * 64, "{}", _NOW
    )

    with TestClient(deployment.build_app()) as client:
        listed = client.get("/api/v1/runs").json()["data"]["runs"]

    assert [run["run_id"] for run in listed] == ["run-000000000000000000000001"]
    assert listed[0]["contract_digest"] == "a" * 64


def test_a_run_under_another_tenant_s_contract_is_not_listed(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The derivation is the tenant boundary, so this is the test that matters."""
    deployment.lifecycles.activate(
        tenant_id="tenant-somebody-else", contract_digest="c" * 64, activated_at=_NOW
    )
    deployment.evidence.create_run(
        "run-000000000000000000000002", "activation-2", "c" * 64, "b" * 64, "{}", _NOW
    )

    with TestClient(deployment.build_app()) as client:
        listed = client.get("/api/v1/runs").json()["data"]["runs"]

    assert listed == []


def test_every_store_is_closed_even_when_one_close_raises(tmp_path: Path) -> None:
    """Teardown must not abandon file handles because an earlier close failed.

    The deployment now owns six databases. A chain of nested `finally` blocks grew
    one level per store and silently skipped the rest whenever an early close
    raised, which on this harness leaks the state directory between runs.
    """
    running = GovernedConsoleDeployment(tmp_path)

    def explode() -> None:
        raise RuntimeError("this store refuses to close")

    running.warehouse_repository.close = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="refuses to close"):
        running.close()

    # A closed connection is observed by using it, not by asking the store to
    # carry a flag that exists only for this assertion.
    with pytest.raises(sqlite3.ProgrammingError):
        running.evidence.list_runs_for_contracts(("a" * 64,))


def test_the_advertised_data_product_route_is_actually_exercised(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The capability badge claimed a route no test had ever called.

    `data-product-runs` reported `ready` because two readers were non-None, while
    every call to the data-product half raised `TypeError` and returned 500. A badge
    is only worth what an exercised route makes it worth, so this walks the route the
    badge advertises rather than the wiring behind it.
    """
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        permitted = client.get(f"/api/v1/data-products/{seeded.data_product_ref}")

    assert permitted.status_code == 200
    assert permitted.json()["data"]["data_product_id"] == seeded.data_product_ref


def test_a_data_product_no_policy_permits_is_not_found_rather_than_a_failure(
    deployment: GovernedConsoleDeployment,
) -> None:
    deployment.seed()

    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/data-products/product-nobody-permits")

    assert response.status_code == 404


def test_a_tenant_with_no_acquisitions_reads_an_empty_receipt_listing(
    deployment: GovernedConsoleDeployment,
) -> None:
    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/acquisition-receipts")

    assert response.status_code == 200
    assert response.json()["data"]["receipts"] == []


def test_run_now_performs_a_fresh_composed_source_acquisition(
    deployment: GovernedConsoleDeployment,
) -> None:
    command = {
        "active_role": "data_architect",
        "contract_ref": "contract:managed-business-data:v1",
        "trigger_window": "2026-09-14T12:00:00Z/2026-09-14T13:00:00Z",
        "acquisition_mode": "snapshot",
    }

    with TestClient(deployment.build_app()) as client:
        session = client.get("/api/v1/session").json()["data"]
        headers = {
            "Origin": "http://127.0.0.1:8000",
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "run-managed-source-2026-09-14-12",
        }
        first = client.post("/api/v1/acquisitions/run-now", headers=headers, json=command)
        replay = client.post("/api/v1/acquisitions/run-now", headers=headers, json=command)
        receipts = client.get("/api/v1/acquisition-receipts")

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["data"]["outcome"] == "prepared"
    assert replay.json()["data"]["outcome"] == "prepared"
    assert deployment.source_acquisition.source_provider_resolutions == 1
    assert deployment.source_acquisition.artifact_count() > 0
    assert first.json()["data"]["evidence_id"] in {
        receipt["evidence_id"] for receipt in receipts.json()["data"]["receipts"]
    }


def test_a_receipt_the_acquisition_runtime_recorded_reaches_the_console(
    deployment: GovernedConsoleDeployment,
) -> None:
    """A real acquisition, its own receipt, and the console's own HTTP read.

    Nothing here is a fixture or a hand-built row: the runtime prepares an
    acquisition, the durable writer retains what it produced, and the assertion is
    made against what the served route returns. That is the only evidence that the
    capability the workspace now reports as ready is one the product can serve.
    """
    from pillarmesh_evidence import SQLiteAcquisitionEvidenceWriter

    runtime_support = runpy.run_path(
        str(Path(__file__).parents[2] / "services/runtime/tests/test_acquisition.py")
    )
    acquisition_intent = runtime_support["_intent"]
    acquisition_runner = runtime_support["_runner"]

    runner, observation, *_rest = acquisition_runner(
        evidence_delegate=SQLiteAcquisitionEvidenceWriter(deployment.evidence)
    )
    result = runner.prepare(acquisition_intent(observation, tenant_id=TENANT))

    with TestClient(deployment.build_app()) as client:
        body = client.get("/api/v1/acquisition-receipts").json()["data"]["receipts"]

    assert [receipt["evidence_id"] for receipt in body] == [result.evidence.evidence_id]
    assert body[0]["outcome"] == "prepared"
    assert body[0]["contract_ref"] == result.evidence.contract_ref
    assert body[0]["logical_object_refs"] == list(result.evidence.logical_object_refs)


def test_a_receipt_belonging_to_another_tenant_is_not_listed(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The receipt's own tenant is the boundary, so this is the test that matters.

    The foreign receipt is written straight to the store rather than produced by the
    runner, because the runner refuses to acquire under a contract another tenant
    owns -- it raises `contract_authority_mismatch` before any receipt exists. That
    refusal is the runtime's boundary and is tested there; this asserts the
    console's, which has to hold even for a row the runtime would never write.
    """
    from pillarmesh_evidence import AcquisitionEvidenceReceipt

    deployment.evidence.append_acquisition_receipt(
        AcquisitionEvidenceReceipt(
            evidence_id="evidence-ref:somebody-elses",
            tenant_id="tenant-somebody-else",
            run_intent_ref="1" * 64,
            contract_ref="contract:theirs:v1",
            source_binding_ref="source-binding:theirs",
            acquisition_mode="snapshot",
            logical_object_refs=("orders",),
            prepared_receipt_ref="receipt-ref:theirs",
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=0,
            resulting_checkpoint_revision=None,
            reason_codes=(),
            outcome="prepared",
            created_at=_NOW,
        )
    )

    with TestClient(deployment.build_app()) as client:
        body = client.get("/api/v1/acquisition-receipts").json()["data"]["receipts"]

    assert body == []


def test_acquisition_evidence_and_execution_are_ready_in_the_governed_workspace(
    deployment: GovernedConsoleDeployment,
) -> None:
    with TestClient(deployment.build_app()) as client:
        capabilities = client.get("/api/v1/workspace").json()["data"]["capabilities"]

    by_id = {capability["capability_id"]: capability for capability in capabilities}

    assert by_id["acquisition-evidence"]["state"] == "ready"
    assert by_id["acquisition-evidence"]["dependency"] is None
    assert by_id["source-acquisition"]["state"] == "ready"
    assert by_id["source-acquisition"]["dependency"] is None


@pytest.mark.parametrize("path", ("/api/v1/acquisition-receipts", "/api/v1/runs"))
def test_a_requester_cannot_read_acquisition_or_run_evidence(
    deployment: GovernedConsoleDeployment, path: str
) -> None:
    """Governed mode must not be more permissive than the demo it stands in for.

    `FixtureConsoleBackend` restricts both reads to `("data_architect",
    "data_owner")`. The governed backend served them to any authenticated actor, so
    a requester could read the tenant's contract references, source binding
    references, object references and refusal reason codes -- material the demo
    refuses them. An unauthorized read answers exactly as an unknown resource does.
    """
    with TestClient(deployment.build_app()) as client:
        response = client.get(path, headers={"x-pillarmesh-actor": REQUESTER})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", ("/api/v1/setup", "/api/v1/inbox"))
def test_a_requester_cannot_read_the_architect_surface(
    deployment: GovernedConsoleDeployment, path: str
) -> None:
    """The architect's setup and decision queue are not a requester's to read.

    `FixtureConsoleBackend` restricts `get_setup` to `("data_architect",)` and
    `get_inbox` to the architect plus the three approving authorities. Both routes
    answered a requester with `200` in governed mode, which showed them the tenant's
    warehouse binding and every open decision the demo refuses them.
    """
    with TestClient(deployment.build_app()) as client:
        response = client.get(path, headers={"x-pillarmesh-actor": REQUESTER})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_architect_cannot_read_a_requesters_own_request_list(
    deployment: GovernedConsoleDeployment,
) -> None:
    """`GET /api/v1/requests/mine` is the requester's own surface, as in the demo.

    It answered the architect with an empty list rather than refusing the role, which
    reports "you have no requests" to an actor who may never have one.
    """
    with TestClient(deployment.build_app()) as client:
        response = client.get("/api/v1/requests/mine", headers={"x-pillarmesh-actor": ARCHITECT})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_data_owner_reads_the_queue_but_is_refused_an_item_from_it(
    deployment: GovernedConsoleDeployment,
) -> None:
    """The one asymmetry left, pinned so it stays deliberate rather than drifting.

    `get_request_detail` admits `data_architect` alone while the demo admits the three
    approving authorities too, so a `data_owner` can list the queue and is refused when
    opening an item. That is stricter than the demo rather than more permissive, so it
    fails closed; widening it decides who may read an architect's decision, which is not
    a consistency edit to make in passing.
    """
    seeded = deployment.seed()

    with TestClient(deployment.build_app()) as client:
        queue = client.get("/api/v1/inbox", headers={"x-pillarmesh-actor": ARCHITECT})
        detail = client.get(
            f"/api/v1/inbox/{seeded.request_id}", headers={"x-pillarmesh-actor": ARCHITECT}
        )

    assert queue.status_code == 200
    assert detail.status_code == 200


def test_a_fixed_requester_browser_session_cannot_be_overridden_by_a_header(
    tmp_path: Path,
) -> None:
    deployment = GovernedConsoleDeployment(tmp_path)
    try:
        with TestClient(deployment.build_app(actor=REQUESTER)) as client:
            session = client.get("/api/v1/session", headers={"x-pillarmesh-actor": ARCHITECT})
            inbox = client.get("/api/v1/inbox")

        assert session.json()["data"]["active_role"] == "requester"
        assert inbox.status_code == 404
    finally:
        deployment.close()
