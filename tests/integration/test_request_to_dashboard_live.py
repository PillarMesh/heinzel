from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from heinzel_bi_control import (
    DashboardAnswerAuthority,
    DashboardControlService,
    DashboardDesiredState,
    DashboardProductGenerationReference,
    SQLiteDashboardRepository,
)
from heinzel_console.contracts import (
    AccessRevocationCommand,
    AdmissionCommand,
    CreateRequestCommand,
)
from heinzel_console.request_intake import request_intake_content
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_superset import (
    CredentialScopedSupersetProvider,
    HttpSupersetClient,
    HttpxSupersetTransport,
    SupersetCredentials,
)
from heinzel_request_management import FulfillmentProposal
from starlette.testclient import TestClient

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
)
from tests.emulators.superset.local_superset import fresh_superset_stack


class _Credentials:
    def __init__(self, credentials: SupersetCredentials) -> None:
        self._credentials = credentials

    def resolve(self, *, secret_reference: str) -> SupersetCredentials:
        assert secret_reference == "secret://tenant-a/superset-database"
        return self._credentials


def _reference(artifact_id: str, marker: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=marker * 64)


@pytest.mark.live
def test_fresh_request_reaches_a_live_dashboard_and_revocation_hides_it_immediately(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    dashboard_repository = SQLiteDashboardRepository(str(tmp_path / "dashboards.sqlite3"))
    try:
        with fresh_superset_stack() as stack:
            credentials = SupersetCredentials(
                base_url=stack.base_url,
                username="admin",
                password=stack.admin_password,
                database_uri=stack.database_uri,
            )
            transport = HttpxSupersetTransport(
                httpx.Client(
                    verify=ssl.create_default_context(cafile=str(stack.ca_certificate_path)),
                    timeout=30.0,
                    trust_env=False,
                )
            )
            dashboard_control = DashboardControlService(
                dashboard_repository,
                CredentialScopedSupersetProvider(
                    resolver=_Credentials(credentials),
                    transport=transport,
                ),
                clock=lambda: now,
            )
            deployment = GovernedConsoleDeployment(tmp_path, dashboards=dashboard_control)
            try:
                publication_intent, _, _ = deployment.publication_repository.load_publication(
                    tenant_id=TENANT,
                    publication_id=deployment.active_publication_id,
                )
                _, contract = deployment.publication_repository.load_inputs(
                    tenant_id=TENANT,
                    operation_id=publication_intent.operation_id,
                )
                field = publication_intent.semantic_objects[0].object_id
                create_command = CreateRequestCommand.model_validate(
                    {
                        "expected_revision": 1,
                        "request_digest": "0" * 64,
                        "active_role": "requester",
                        "title": "Current revenue by region",
                        "request": {
                            "kind": "data_access",
                            "purpose": "Review current governed revenue by region.",
                            "data_product_ref": contract.destination_product.product_name,
                            "requested_fields": [field],
                            "access_mode": "dashboard",
                            "expires_at": (now + timedelta(days=1)).isoformat(),
                        },
                    }
                )
                create_command = create_command.model_copy(
                    update={"request_digest": digest(request_intake_content(create_command))}
                )
                created = deployment.backend.create_request(_context(REQUESTER), create_command)
                request = deployment.requests.get(TENANT, created.request_id)
                deployment.fulfillment.clarify_outcome(
                    tenant_id=TENANT,
                    request_id=request.request_id,
                    actor_id=ARCHITECT,
                    restated_request="Provide dashboard access to current governed revenue.",
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
                assert isinstance(proposal, FulfillmentProposal)
                awaiting = deployment.fulfillment.submit_proposal(
                    tenant_id=TENANT,
                    request_id=request.request_id,
                    actor_id=ARCHITECT,
                    expected_revision=proposal.request_revision,
                )
                approvers = {
                    REQUESTER_PRINCIPAL: REQUESTER,
                    ARCHITECT_PRINCIPAL: ARCHITECT,
                    IMPACT_OWNER: DATA_OWNER,
                    "role:policy_authority": POLICY_APPROVER,
                }
                for requirement in proposal.required_approvals:
                    deployment.fulfillment.record_approval(
                        tenant_id=TENANT,
                        request_id=request.request_id,
                        actor_id=approvers[requirement.authority_ref],
                        authority_ref=requirement.authority_ref,
                        subject_digest=requirement.subject_digest,
                        decision="approve",
                        expected_revision=awaiting.revision,
                    )
                request_detail = deployment.backend.get_request_detail(
                    _context(ARCHITECT), request.request_id
                )
                assert request_detail.proposal_digest is not None
                deployment.backend.admit_request(
                    _context(ARCHITECT),
                    request.request_id,
                    AdmissionCommand(
                        expected_revision=awaiting.revision,
                        reviewed_digest=request_detail.proposal_digest,
                        active_role="data_architect",
                    ),
                )
                grant = deployment.access_grants.load_current_for_request(
                    TENANT, request.request_id
                )
                assert grant is not None
                assert grant.state == "active"
                assert grant.access_mode == "dashboard"

                metric_ref = _reference(field, "3")
                desired = DashboardDesiredState(
                    tenant_id=TENANT,
                    dashboard_id="internal:current-revenue-by-region",
                    version=1,
                    revision=1,
                    title="Current revenue by region",
                    contract_digest="4" * 64,
                    contract_key_id="live-integration-key",
                    contract_signature="live-integration-signature",
                    source_answer=DashboardAnswerAuthority(
                        tenant_id=TENANT,
                        request_id="answer-request-current-revenue",
                        request_revision=1,
                        answer_id="answer-current-revenue",
                        title="Current revenue by region",
                        execution_receipt_ref="execution-current-revenue",
                        result_ref="result-current-revenue",
                        result_digest="5" * 64,
                        product_generation_refs=(
                            DashboardProductGenerationReference(
                                product_ref=grant.data_product_version_ref,
                                generation=1,
                            ),
                        ),
                        metric_version_refs=(metric_ref,),
                        as_of=now - timedelta(minutes=5),
                        freshness_disposition="current",
                        delivered_at=now - timedelta(minutes=1),
                    ),
                    dataset_product_ref=grant.data_product_version_ref,
                    dataset_generation=1,
                    consumption_object_ref=_reference("consumption:current-revenue", "6"),
                    materialization_receipt_ref=_reference("materialization:current-revenue", "7"),
                    product_publication_ref=_reference("publication:current-revenue", "8"),
                    dataset_namespace="analytics",
                    dataset_relation_name="orders_current",
                    warehouse_binding_id="warehouse-live",
                    warehouse_binding_revision=1,
                    warehouse_binding_digest="9" * 64,
                    connection_secret_ref="secret://tenant-a/superset-database",
                    metric_refs=(metric_ref,),
                    dimension_refs=(_reference("dimension:region", "a"),),
                    filter_refs=(),
                    visual_intents=("bar", "number"),
                    lifecycle_state="active",
                )
                receipt = dashboard_control.apply(desired)
                read_client = HttpSupersetClient(
                    credentials=credentials,
                    transport=transport,
                )
                provider_dashboard = read_client.get_dashboard(
                    stable_key=desired.stable_external_key
                )
                assert provider_dashboard is not None
                assert provider_dashboard.managed_digest == desired.desired_digest
                assert receipt.desired_digest == desired.desired_digest

                with TestClient(deployment.build_app(actor=REQUESTER)) as client:
                    active_response = client.get("/api/v1/dashboards")
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
                            reason="The governed dashboard review is complete.",
                        ),
                    )
                    revoked_response = client.get("/api/v1/dashboards")

                assert active_response.status_code == 200
                active_payload = active_response.json()
                assert [item["display_name"] for item in active_payload["data"]["dashboards"]] == [
                    "Current revenue by region"
                ]
                dashboard = active_payload["data"]["dashboards"][0]
                assert dashboard["dashboard_ref"].startswith("dashboard-")
                assert dashboard["freshness"] == "current"
                assert dashboard["access_state"] == "active"
                serialized = active_response.text
                for private_value in (
                    request.request_id,
                    grant.grant_id,
                    desired.dashboard_id,
                    desired.stable_external_key,
                    credentials.base_url,
                    "external_id",
                    "superset",
                ):
                    assert private_value not in serialized.lower()

                assert revoked_response.status_code == 200
                assert revoked_response.json()["data"]["dashboards"] == []
            finally:
                deployment.close()
    finally:
        dashboard_repository.close()

    stack.assert_removed()
