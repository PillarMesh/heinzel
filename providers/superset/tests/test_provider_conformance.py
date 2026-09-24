from __future__ import annotations

from dataclasses import replace

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import (
    BiDashboardDefinition,
    BiDataset,
    dashboard_external_key,
    run_bi_provider_conformance,
)
from heinzel_provider_superset import (
    SupersetClientError,
    SupersetDashboard,
    SupersetProvider,
)


class _Client:
    def __init__(self) -> None:
        self.datasets = {
            "dataset-orders": BiDataset(
                stable_key="dataset-orders",
                generation=7,
                namespace="analytics",
                relation_name="revenue_current",
            )
        }
        self.dashboards: dict[str, SupersetDashboard] = {}
        self.fail_next = False
        self.lose_create_response = False
        self.create_count = 0
        self.update_count = 0
        self.archive_count = 0
        self.prepare_count = 0

    def prepare_dashboard(self, *, definition: BiDashboardDefinition) -> None:
        del definition
        self.prepare_count += 1

    def get_dataset(self, *, stable_key: str) -> BiDataset | None:
        return self.datasets.get(stable_key)

    def get_dashboard(self, *, stable_key: str) -> SupersetDashboard | None:
        return self.dashboards.get(stable_key)

    def create_dashboard(self, *, definition: BiDashboardDefinition) -> SupersetDashboard:
        self._maybe_fail()
        self.create_count += 1
        dashboard = SupersetDashboard(
            external_id=f"remote-{len(self.dashboards) + 1}",
            stable_key=definition.stable_external_key,
            managed_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url=f"https://superset.test/dashboard/{definition.stable_external_key}",
        )
        self.dashboards[definition.stable_external_key] = dashboard
        if self.lose_create_response:
            self.lose_create_response = False
            raise SupersetClientError(classification="ambiguous_outcome")
        return dashboard

    def update_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard:
        self._maybe_fail()
        self.update_count += 1
        current = self.dashboards[definition.stable_external_key]
        assert current.external_id == external_id
        updated = replace(
            current,
            managed_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
        )
        self.dashboards[definition.stable_external_key] = updated
        return updated

    def archive_dashboard(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> SupersetDashboard:
        self.archive_count += 1
        current = self.dashboards[definition.stable_external_key]
        assert current.external_id == external_id
        archived = replace(
            current,
            managed_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
        )
        self.dashboards[definition.stable_external_key] = archived
        return archived

    def reconcile_dashboard_charts(
        self, *, external_id: str, definition: BiDashboardDefinition
    ) -> None:
        del external_id, definition

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise SupersetClientError(classification="transient_unavailable")


def _definition(**updates: object) -> BiDashboardDefinition:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "dashboard_id": "revenue",
        "version": 3,
        "revision": 1,
        "stable_external_key": dashboard_external_key(
            tenant_id="tenant-a", dashboard_id="revenue", version=3
        ),
        "desired_digest": "b" * 64,
        "prior_desired_digest": None,
        "title": "Revenue overview",
        "contract_digest": "e" * 64,
        "contract_signature": "test-signature",
        "dataset_stable_key": "dataset-orders",
        "dataset_generation": 7,
        "dataset_namespace": "analytics",
        "dataset_relation_name": "revenue_current",
        "connection_secret_ref": "secret://tenant-a/superset-database",
        "metric_refs": ("metric:revenue:v2",),
        "dimension_refs": ("dimension:region:v1",),
        "filter_refs": ("dimension:status:v1",),
        "visual_intents": ("bar",),
        "lifecycle_state": "active",
    }
    values.update(updates)
    return BiDashboardDefinition.model_validate(values)


def test_superset_passes_shared_bi_conformance() -> None:
    client = _Client()

    run_bi_provider_conformance(lambda: SupersetProvider(client), _definition)

    assert len(client.dashboards) == 1
    assert client.create_count == 1
    assert client.update_count == 1
    assert client.archive_count == 1


def test_superset_classifies_transient_failure_without_leaking_driver_details() -> None:
    client = _Client()
    client.fail_next = True
    provider = SupersetProvider(client)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "transient_unavailable"
    assert captured.value.__cause__ is None
    assert "SupersetClientError" not in str(captured.value)


def test_superset_reconciles_a_response_lost_after_create_without_a_duplicate() -> None:
    client = _Client()
    client.lose_create_response = True
    provider = SupersetProvider(client)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "ambiguous_outcome"
    receipt = provider.apply(_definition())
    assert receipt.desired_digest == "b" * 64
    assert client.create_count == 1
    assert len(client.dashboards) == 1


def test_superset_refuses_unrecognized_external_mutation() -> None:
    client = _Client()
    provider = SupersetProvider(client)
    created = provider.apply(_definition())
    client.dashboards[created.stable_external_key] = replace(
        client.dashboards[created.stable_external_key], managed_digest=digest("manual edit")
    )
    prepare_count = client.prepare_count

    with pytest.raises(ProviderError) as captured:
        provider.apply(
            _definition(
                revision=2,
                desired_digest="c" * 64,
                prior_desired_digest="b" * 64,
            )
        )

    assert captured.value.classification == "integrity_failure"
    assert client.prepare_count == prepare_count


def test_superset_refuses_a_missing_dataset() -> None:
    provider = SupersetProvider(_Client())

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition(dataset_stable_key="missing"))

    assert captured.value.classification == "statement_rejected"


def test_superset_refuses_a_dataset_bound_to_another_warehouse_relation() -> None:
    client = _Client()
    client.datasets["dataset-orders"] = BiDataset(
        stable_key="dataset-orders",
        generation=7,
        namespace="analytics",
        relation_name="other_revenue",
    )
    provider = SupersetProvider(client)

    with pytest.raises(ProviderError) as captured:
        provider.apply(_definition())

    assert captured.value.classification == "integrity_failure"
    assert client.create_count == 0
