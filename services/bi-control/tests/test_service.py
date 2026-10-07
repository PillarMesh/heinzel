from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from heinzel_bi_control import (
    DashboardAnswerAuthority,
    DashboardControlService,
    DashboardDesiredState,
    DashboardProductGenerationReference,
    SQLiteDashboardRepository,
)
from heinzel_contract_model import ArtifactReference
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import BiApplyResult, BiDashboardDefinition

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Provider:
    provider_kind: Literal["superset"] = "superset"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.definitions: list[BiDashboardDefinition] = []

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        self.calls += 1
        self.definitions.append(definition)
        if self.fail:
            raise ProviderError("BI provider unavailable", "transient_unavailable")
        return BiApplyResult(
            stable_external_key=definition.stable_external_key,
            desired_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url=f"https://superset.test/dashboard/{definition.stable_external_key}",
            provider_version="4.1.1",
        )


def _desired(**updates: object) -> DashboardDesiredState:
    product_ref = ArtifactReference(artifact_id="product:orders", version=2, digest="a" * 64)
    consumption_ref = ArtifactReference(
        artifact_id="consumption:orders", version=7, digest="b" * 64
    )
    materialization_ref = ArtifactReference(
        artifact_id="materialization:orders", version=7, digest="c" * 64
    )
    metric_ref = ArtifactReference(artifact_id="metric:revenue", version=2, digest="d" * 64)
    dimension_ref = ArtifactReference(artifact_id="dimension:region", version=1, digest="e" * 64)
    filter_ref = ArtifactReference(artifact_id="dimension:status", version=1, digest="f" * 64)
    tenant_id = str(updates.get("tenant_id", "tenant-a"))
    title = str(updates.get("title", "Revenue overview"))
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "dashboard_id": "revenue",
        "version": 3,
        "revision": 1,
        "prior_desired_digest": None,
        "title": "Revenue overview",
        "contract_digest": "e" * 64,
        "contract_key_id": "dashboard-key-1",
        "contract_signature": "test-signature",
        "source_answer": DashboardAnswerAuthority(
            tenant_id=tenant_id,
            request_id="request-1",
            request_revision=6,
            answer_id="answer-1",
            title=title,
            execution_receipt_ref="execution-1",
            result_ref="result-1",
            result_digest="1" * 64,
            product_generation_refs=(
                DashboardProductGenerationReference(product_ref=product_ref, generation=7),
            ),
            metric_version_refs=(metric_ref,),
            as_of=NOW,
            freshness_disposition="current",
            delivered_at=NOW,
            result_expires_at=NOW + timedelta(hours=1),
        ),
        "dataset_product_ref": product_ref,
        "dataset_generation": 7,
        "consumption_object_ref": consumption_ref,
        "materialization_receipt_ref": materialization_ref,
        "product_publication_ref": ArtifactReference(
            artifact_id="product-publication-1", version=7, digest="6" * 64
        ),
        "dataset_namespace": "analytics",
        "dataset_relation_name": "revenue_current",
        "warehouse_binding_id": "warehouse-1",
        "warehouse_binding_revision": 2,
        "warehouse_binding_digest": "2" * 64,
        "connection_secret_ref": "secret://tenant-a/superset-database",
        "metric_refs": (metric_ref,),
        "dimension_refs": (dimension_ref,),
        "filter_refs": (filter_ref,),
        "visual_intents": ("bar",),
        "lifecycle_state": "active",
    }
    values.update(updates)
    return DashboardDesiredState.model_validate(values)


def test_service_persists_desired_state_and_receipt_before_exposing_publication() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    provider = _Provider()
    service = DashboardControlService(repository, provider, clock=lambda: NOW)
    desired = _desired()

    receipt = service.apply(desired)

    assert repository.load_desired("tenant-a", "revenue", 3, 1) == desired
    assert repository.load_receipt("tenant-a", "revenue", 3, 1) == receipt
    assert provider.definitions == [
        BiDashboardDefinition(
            schema_version="2",
            tenant_id="tenant-a",
            dashboard_id="revenue",
            version=3,
            revision=1,
            stable_external_key=desired.stable_external_key,
            desired_digest=desired.desired_digest,
            prior_desired_digest=None,
            title="Revenue overview",
            contract_digest="e" * 64,
            contract_signature="test-signature",
            dataset_stable_key=desired.dataset_stable_key,
            dataset_generation=7,
            dataset_namespace="analytics",
            dataset_relation_name="revenue_current",
            connection_secret_ref="secret://tenant-a/superset-database",
            metric_refs=(f"metric:revenue@2#{'d' * 64}",),
            dimension_refs=(f"dimension:region@1#{'e' * 64}",),
            filter_refs=(f"dimension:status@1#{'f' * 64}",),
            visual_intents=("bar",),
            lifecycle_state="active",
        )
    ]
    publication = service.get_publication("tenant-a", "revenue", 3)
    assert publication is not None
    assert publication.model_dump(mode="json") == {
        "schema_version": "1",
        "dashboard_id": "revenue",
        "version": 3,
        "title": "Revenue overview",
        "source_request_id": "request-1",
        "data_product_version_ref": {
            "artifact_id": "product:orders",
            "version": 2,
            "digest": "a" * 64,
        },
        "lifecycle_state": "active",
        "as_of": "2026-09-11T12:00:00Z",
        "freshness_disposition": "current",
        "published_at": "2026-09-11T12:00:00Z",
    }
    publication_payload = publication.model_dump_json()
    assert all(
        private_value not in publication_payload
        for private_value in (
            "tenant-a",
            "pm-dashboard-",
            "https://superset.test",
            "superset",
            "4.1.1",
            "test-signature",
            "dataset-orders",
            "analytics",
            "revenue_current",
            "secret://",
            receipt.desired_digest,
        )
    )


def test_desired_state_rejects_the_previous_schema_version() -> None:
    payload = _desired().model_dump(mode="python")
    payload["schema_version"] = "3"

    with pytest.raises(ValueError, match="schema_version"):
        DashboardDesiredState.model_validate(payload)


def test_exact_replay_returns_the_stored_receipt_without_a_provider_call() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    provider = _Provider()
    service = DashboardControlService(repository, provider, clock=lambda: NOW)
    desired = _desired()
    first = service.apply(desired)

    replay = service.apply(desired)

    assert replay == first
    assert provider.calls == 1


def test_desired_state_and_receipt_survive_repository_reopen(tmp_path: Path) -> None:
    database_path = str(tmp_path / "bi-control.db")
    repository = SQLiteDashboardRepository(database_path)
    service = DashboardControlService(repository, _Provider(), clock=lambda: NOW)
    service.apply(_desired())
    publication = service.get_publication("tenant-a", "revenue", 3)

    reopened = DashboardControlService(
        SQLiteDashboardRepository(database_path), _Provider(), clock=lambda: NOW
    )

    assert reopened.get_publication("tenant-a", "revenue", 3) == publication


def test_update_and_archive_advance_owned_desired_revisions() -> None:
    service = DashboardControlService(
        SQLiteDashboardRepository(":memory:"), _Provider(), clock=lambda: NOW
    )
    first = service.apply(_desired())
    updated = _desired(
        revision=2,
        prior_desired_digest=first.desired_digest,
        visual_intents=("line",),
    )
    second = service.apply(updated)
    archived = service.apply(
        _desired(
            revision=3,
            prior_desired_digest=second.desired_digest,
            visual_intents=("line",),
            lifecycle_state="archived",
        )
    )

    assert second.revision == 2
    assert archived.lifecycle_state == "archived"
    publication = service.get_publication("tenant-a", "revenue", 3)
    assert publication is not None
    assert publication.lifecycle_state == "archived"


def test_transient_failure_leaves_desired_state_without_exposing_a_link() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    provider = _Provider()
    provider.fail = True
    service = DashboardControlService(repository, provider, clock=lambda: NOW)

    with pytest.raises(ProviderError, match="unavailable"):
        service.apply(_desired())

    assert repository.load_desired("tenant-a", "revenue", 3, 1) == _desired()
    assert service.get_publication("tenant-a", "revenue", 3) is None


def test_publication_lookup_does_not_cross_tenant_boundary() -> None:
    service = DashboardControlService(
        SQLiteDashboardRepository(":memory:"), _Provider(), clock=lambda: NOW
    )
    service.apply(_desired())

    assert service.get_publication("tenant-b", "revenue", 3) is None


def test_publication_listing_returns_only_current_receipted_states_for_the_tenant() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    service = DashboardControlService(repository, _Provider(), clock=lambda: NOW)
    first = service.apply(_desired())
    service.apply(
        _desired(
            revision=2,
            prior_desired_digest=first.desired_digest,
            title="Current revenue overview",
        )
    )
    other_tenant = _desired(tenant_id="tenant-b", dashboard_id="margin", title="Margin")
    service.apply(other_tenant)
    repository.record_desired(_desired(dashboard_id="pending", title="Pending provider"))

    publications = service.list_publications("tenant-a")

    assert [publication.model_dump(mode="json") for publication in publications] == [
        {
            "schema_version": "1",
            "dashboard_id": "revenue",
            "version": 3,
            "title": "Current revenue overview",
            "source_request_id": "request-1",
            "data_product_version_ref": {
                "artifact_id": "product:orders",
                "version": 2,
                "digest": "a" * 64,
            },
            "lifecycle_state": "active",
            "as_of": "2026-09-11T12:00:00Z",
            "freshness_disposition": "current",
            "published_at": "2026-09-11T12:00:00Z",
        }
    ]


def test_publication_listing_fails_closed_for_a_corrupt_tenant_index() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    service = DashboardControlService(repository, _Provider(), clock=lambda: NOW)
    service.apply(_desired())
    repository._connection.execute(
        "UPDATE dashboard_desired_states SET payload = ?",
        (_desired(tenant_id="tenant-b").model_dump_json(),),
    )
    repository._connection.commit()

    with pytest.raises(ValueError, match="index does not match"):
        service.list_publications("tenant-a")


def test_conflicting_replay_is_rejected_before_provider_effect() -> None:
    repository = SQLiteDashboardRepository(":memory:")
    provider = _Provider()
    service = DashboardControlService(repository, provider, clock=lambda: NOW)
    service.apply(_desired())

    with pytest.raises(ValueError, match="conflicting dashboard desired state"):
        service.apply(
            _desired(connection_secret_ref="secret://tenant-a/changed-without-a-revision")
        )

    assert provider.calls == 1
