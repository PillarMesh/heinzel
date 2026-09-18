from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from heinzel_bi_control import DashboardContract, DashboardPublication
from heinzel_contract_model import ArtifactReference, FreshnessRequirement
from pydantic import ValidationError


def _reference(artifact_id: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=1, digest="a" * 64)


def _contract(**updates: object) -> DashboardContract:
    dimension = _reference("dimension:region")
    values: dict[str, object] = {
        "dashboard_id": "dashboard:revenue",
        "version": 1,
        "owner": "principal:finance-owner",
        "audience": ("group:finance",),
        "data_product_versions": (_reference("product:orders"),),
        "metric_versions": (_reference("metric:revenue"),),
        "dimensions": (dimension,),
        "filters": (_reference("filter:completed-orders"),),
        "visual_intents": ("bar", "number"),
        "drill_paths": ((dimension,),),
        "freshness_requirement": FreshnessRequirement(maximum_age_seconds=3_600),
        "access_policy": _reference("access-policy:finance"),
        "report_delivery_policy": _reference("report-policy:daily"),
        "acceptance_tests": (_reference("acceptance:revenue-total"),),
        "lifecycle_state": "certified",
    }
    values.update(updates)
    return DashboardContract.model_validate(values)


def test_dashboard_contract_preserves_canonical_authority_references() -> None:
    contract = _contract()

    assert contract.dashboard_id == "dashboard:revenue"
    assert contract.data_product_versions[0].artifact_id == "product:orders"
    assert contract.freshness_requirement.maximum_age_seconds == 3_600
    assert contract.lifecycle_state == "certified"


@pytest.mark.parametrize(
    "updates",
    (
        {"dashboard_id": ""},
        {"version": 0},
        {"version": "1"},
        {"lifecycle_state": "active"},
        {"unexpected_provider_id": 42},
    ),
)
def test_dashboard_contract_rejects_invalid_boundary_values(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _contract(**updates)


def test_dashboard_contract_is_immutable() -> None:
    contract = _contract()

    with pytest.raises(ValidationError):
        contract.version = 2


@pytest.mark.parametrize("timestamp_field", ("as_of", "published_at"))
@pytest.mark.parametrize(
    "invalid_timestamp",
    (
        datetime(2026, 9, 11, 12),
        datetime(2026, 9, 11, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_dashboard_publication_requires_timezone_aware_utc(
    timestamp_field: str, invalid_timestamp: datetime
) -> None:
    values: dict[str, object] = {
        "dashboard_id": "dashboard:revenue",
        "version": 1,
        "title": "Revenue overview",
        "source_request_id": "request-1",
        "data_product_version_ref": _reference("product:orders"),
        "lifecycle_state": "active",
        "as_of": datetime(2026, 9, 11, 12, tzinfo=UTC),
        "freshness_disposition": "current",
        "published_at": datetime(2026, 9, 11, 12, tzinfo=UTC),
    }
    values[timestamp_field] = invalid_timestamp

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        DashboardPublication.model_validate(values)


def test_dashboard_contract_fields_are_exact_and_ordered() -> None:
    assert tuple(DashboardContract.model_fields) == (
        "dashboard_id",
        "version",
        "owner",
        "audience",
        "data_product_versions",
        "metric_versions",
        "dimensions",
        "filters",
        "visual_intents",
        "drill_paths",
        "freshness_requirement",
        "access_policy",
        "report_delivery_policy",
        "acceptance_tests",
        "lifecycle_state",
    )
