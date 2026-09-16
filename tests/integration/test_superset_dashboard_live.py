from __future__ import annotations

import ssl
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import pytest
from pillarmesh_bi_control import (
    DashboardAnswerAuthority,
    DashboardControlService,
    DashboardDesiredState,
    DashboardProductGenerationReference,
    SQLiteDashboardRepository,
)
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_provider_sdk import AccessEffectCommand, ProviderError
from pillarmesh_provider_superset import (
    CredentialScopedSupersetAccessEffectProvider,
    CredentialScopedSupersetProvider,
    HttpSupersetClient,
    HttpxSupersetTransport,
    SupersetAccessTarget,
    SupersetCredentials,
    SupersetHttpResponse,
)

from tests.emulators.superset.local_superset import fresh_superset_stack

NOW = datetime(2026, 9, 14, 20, tzinfo=UTC)


class _Resolver:
    def __init__(self, credentials: SupersetCredentials) -> None:
        self._credentials = credentials

    def resolve(self, *, secret_reference: str) -> SupersetCredentials:
        assert secret_reference == "secret://tenant-live/superset-database"
        return self._credentials


class _RecordingTransport:
    def __init__(self, transport: HttpxSupersetTransport) -> None:
        self._transport = transport
        self.calls: list[tuple[str, str, int, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: object | None = None,
        params: Mapping[str, str] | None = None,
    ) -> SupersetHttpResponse:
        response = self._transport.request(
            method=method,
            url=url,
            headers=headers,
            json=json,
            params=params,
        )
        shape: object
        if isinstance(response.payload, dict):
            result = response.payload.get("result")
            shape = {
                "keys": tuple(sorted(response.payload)),
                "count": response.payload.get("count"),
                "result_length": len(result) if isinstance(result, list) else None,
            }
        else:
            shape = type(response.payload).__name__
        self.calls.append((method, urlsplit(url).path, response.status_code, shape))
        return response


def _reference(artifact_id: str, marker: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest=marker * 64)


def _admitted_desired(
    *,
    tenant_id: str = "tenant-live",
    dashboard_id: str = "dashboard:revenue",
    title: str = "Revenue by region",
    relation_name: str = "orders_current",
) -> DashboardDesiredState:
    product = _reference("product:orders", "a")
    metric = _reference("metric:revenue", "b")
    consumption = _reference("consumption:orders", "c")
    answer = DashboardAnswerAuthority(
        tenant_id=tenant_id,
        request_id="request-live",
        request_revision=6,
        answer_id="answer-live",
        title=title,
        execution_receipt_ref="execution-live",
        result_ref="result-live",
        result_digest="d" * 64,
        product_generation_refs=(
            DashboardProductGenerationReference(product_ref=product, generation=1),
        ),
        metric_version_refs=(metric,),
        as_of=NOW - timedelta(minutes=5),
        freshness_disposition="current",
        delivered_at=NOW - timedelta(minutes=1),
    )
    return DashboardDesiredState(
        tenant_id=tenant_id,
        dashboard_id=dashboard_id,
        version=1,
        revision=1,
        title=title,
        contract_digest="e" * 64,
        contract_key_id="dashboard-live-key",
        contract_signature="test-signature",
        source_answer=answer,
        dataset_product_ref=product,
        dataset_generation=1,
        consumption_object_ref=consumption,
        materialization_receipt_ref=ArtifactReference(
            artifact_id="materialization-live", version=1, digest="f" * 64
        ),
        product_publication_ref=ArtifactReference(
            artifact_id="publication-live", version=1, digest="1" * 64
        ),
        dataset_namespace="analytics",
        dataset_relation_name=relation_name,
        warehouse_binding_id="warehouse-live",
        warehouse_binding_revision=1,
        warehouse_binding_digest="2" * 64,
        connection_secret_ref="secret://tenant-live/superset-database",
        metric_refs=(metric,),
        dimension_refs=(_reference("dimension:region", "3"),),
        filter_refs=(),
        visual_intents=("bar", "number"),
        lifecycle_state="active",
    )


class _AccessTargetAuthority:
    def __init__(self, target: SupersetAccessTarget) -> None:
        self._target = target

    def resolve(self, command: AccessEffectCommand) -> SupersetAccessTarget | None:
        if (
            command.tenant_id,
            command.grant_id,
            command.grant_revision,
        ) != (
            self._target.tenant_id,
            self._target.grant_id,
            self._target.grant_revision,
        ):
            return None
        return self._target


def _access_command(
    target: SupersetAccessTarget, *, action: Literal["apply", "revoke"]
) -> AccessEffectCommand:
    return AccessEffectCommand(
        tenant_id=target.tenant_id,
        grant_id=target.grant_id,
        grant_revision=target.grant_revision,
        surface="superset",
        action=action,
        idempotency_key=f"pm-live-{action}-{target.grant_id}",
        principal_ref=target.principal_ref,
        provider_resource_ref=target.provider_resource_ref,
        fields=target.fields,
        permissions=target.permissions,
        effective_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        scope_digest=target.scope_digest,
    )


@pytest.mark.live
def test_cold_superset_publishes_and_reads_back_governed_postgresql_dashboard(
    tmp_path: Path,
) -> None:
    with fresh_superset_stack() as stack:
        credentials = SupersetCredentials(
            base_url=stack.base_url,
            username="admin",
            password=stack.admin_password,
            database_uri=stack.database_uri,
        )
        transport = _RecordingTransport(
            HttpxSupersetTransport(
                httpx.Client(
                    verify=ssl.create_default_context(cafile=str(stack.ca_certificate_path)),
                    timeout=30.0,
                    trust_env=False,
                )
            )
        )
        provider = CredentialScopedSupersetProvider(
            resolver=_Resolver(credentials), transport=transport
        )
        repository = SQLiteDashboardRepository(str(tmp_path / "dashboard.sqlite3"))
        service = DashboardControlService(repository, provider, clock=lambda: NOW)
        desired = _admitted_desired()

        try:
            receipt = service.apply(desired)
        except ProviderError as error:
            raise AssertionError(f"Superset API trace: {transport.calls!r}") from error
        read_client = HttpSupersetClient(credentials=credentials, transport=transport)

        assert receipt.stable_external_key == desired.stable_external_key
        assert receipt.desired_digest == desired.desired_digest
        assert read_client.get_dataset(stable_key=desired.dataset_stable_key) is not None
        observed = read_client.get_dashboard(stable_key=desired.stable_external_key)
        assert observed is not None
        assert observed.managed_digest == desired.desired_digest
        assert observed.lifecycle_state == "active"
        managed_chart_ids = read_client.get_dashboard_chart_ids(
            stable_key=desired.stable_external_key
        )
        assert len(managed_chart_ids) == 2
        assert stack.resource_counts() == {
            "database": 1,
            "dataset": 1,
            "chart": 2,
            "dashboard": 1,
        }
        assert stack.warehouse_rows() == (("east", "99.00"), ("west", "30.00"))
        assert stack.reader_is_least_privilege()

        replayed_receipt = service.apply(desired)

        assert replayed_receipt == receipt
        assert (
            read_client.get_dashboard_chart_ids(stable_key=desired.stable_external_key)
            == managed_chart_ids
        )

        archived = desired.model_copy(
            update={
                "revision": 2,
                "prior_desired_digest": desired.desired_digest,
                "lifecycle_state": "archived",
            }
        )
        archived_receipt = service.apply(archived)

        assert archived_receipt.lifecycle_state == "archived"
        assert (
            read_client.get_dashboard_chart_ids(stable_key=desired.stable_external_key)
            == managed_chart_ids
        )
        assert service.apply(archived) == archived_receipt
        assert (
            read_client.get_dashboard_chart_ids(stable_key=desired.stable_external_key)
            == managed_chart_ids
        )
        assert service.get_publication("tenant-live", "dashboard:revenue", 1) is not None

    stack.assert_removed()


@pytest.mark.live
def test_cold_superset_applies_and_revokes_authority_bound_dashboard_access(
    tmp_path: Path,
) -> None:
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
        resolver = _Resolver(credentials)
        dashboard_provider = CredentialScopedSupersetProvider(
            resolver=resolver,
            transport=transport,
        )
        service = DashboardControlService(
            SQLiteDashboardRepository(str(tmp_path / "dashboard-access.sqlite3")),
            dashboard_provider,
            clock=lambda: NOW,
        )
        tenant_dashboard = _admitted_desired()
        isolated_dashboard = _admitted_desired(
            tenant_id="tenant-isolated",
            dashboard_id="dashboard:isolated",
            title="Isolated revenue",
            relation_name="orders_isolated",
        )
        service.apply(tenant_dashboard)
        service.apply(isolated_dashboard)
        read_client = HttpSupersetClient(credentials=credentials, transport=transport)
        tenant_observed = read_client.get_dashboard(stable_key=tenant_dashboard.stable_external_key)
        isolated_observed = read_client.get_dashboard(
            stable_key=isolated_dashboard.stable_external_key
        )
        assert tenant_observed is not None
        assert isolated_observed is not None
        tenant_dashboard_id = int(tenant_observed.external_id)
        isolated_dashboard_id = int(isolated_observed.external_id)

        principal = stack.provision_access_principal()
        read_client.reconcile_dashboard_roles(
            dashboard_id=tenant_dashboard_id,
            expected_role_ids=(),
            desired_role_ids=(principal.base_role_id,),
        )
        read_client.reconcile_dashboard_roles(
            dashboard_id=isolated_dashboard_id,
            expected_role_ids=(),
            desired_role_ids=(principal.isolated_base_role_id,),
        )
        requester_token = stack.access_token(
            username=principal.username,
            password=principal.password,
        )
        assert stack.visible_dashboard_ids(access_token=requester_token) == ()

        target = SupersetAccessTarget(
            tenant_id="tenant-live",
            grant_id="grant-live-dashboard",
            grant_revision=1,
            principal_ref="principal:requester-a",
            provider_resource_ref=tenant_dashboard.stable_external_key,
            dashboard_id=tenant_dashboard_id,
            grant_scoped_role_id=principal.grant_role_id,
            base_role_ids=(principal.base_role_id,),
            credential_secret_ref="secret://tenant-live/superset-database",
            fields=("region", "revenue"),
            scope_digest=digest(
                {
                    "tenant_id": "tenant-live",
                    "grant_id": "grant-live-dashboard",
                    "dashboard": tenant_dashboard.stable_external_key,
                }
            ),
        )
        access_provider = CredentialScopedSupersetAccessEffectProvider(
            targets=_AccessTargetAuthority(target),
            credentials=resolver,
            transport=transport,
        )
        apply_command = _access_command(target, action="apply")

        applied = access_provider.enact(apply_command)
        replayed = access_provider.enact(apply_command)

        assert replayed == applied
        assert applied.provider_receipt_digest == digest(
            {
                "domain": "pillarmesh.superset-access-effect.v1",
                "provider_version": access_provider.provider_version,
                "command": apply_command,
                "target": target,
            }
        )
        assert read_client.get_dashboard_role_ids(dashboard_id=tenant_dashboard_id) == (
            principal.base_role_id,
            principal.grant_role_id,
        )
        assert stack.visible_dashboard_ids(access_token=requester_token) == (tenant_dashboard_id,)
        assert isolated_dashboard_id not in stack.visible_dashboard_ids(
            access_token=requester_token
        )

        revoke_command = _access_command(target, action="revoke")
        revoked = access_provider.enact(revoke_command)
        revoked_replay = access_provider.enact(revoke_command)

        assert revoked_replay == revoked
        assert revoked.provider_receipt_digest != applied.provider_receipt_digest
        assert read_client.get_dashboard_role_ids(dashboard_id=tenant_dashboard_id) == (
            principal.base_role_id,
        )
        assert stack.visible_dashboard_ids(access_token=requester_token) == ()

    stack.assert_removed()
