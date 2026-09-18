from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import (
    OrderRow,
    ProviderObservation,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)
from pydantic import ValidationError


def test_order_row_requires_utc_observation_time() -> None:
    row = OrderRow(
        order_id=7,
        customer_ref="customer-7",
        amount=Decimal("10.50"),
        currency="USD",
        status="paid",
        updated_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )

    assert row.amount == Decimal("10.50")

    invalid = row.model_dump()
    invalid["updated_at"] = datetime(2026, 8, 13, 12, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        OrderRow.model_validate(invalid)


@pytest.mark.parametrize("field", ["customer_ref", "status"])
def test_order_row_rejects_text_outside_the_fixed_snapshot_domain(field: str) -> None:
    payload = {
        "order_id": 7,
        "customer_ref": "customer-7",
        "amount": Decimal("10.50"),
        "currency": "USD",
        "status": "paid",
        "updated_at": datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    }
    payload[field] = "x" * 65_535
    assert len(getattr(OrderRow.model_validate(payload), field)) == 65_535

    payload[field] = "x" * 65_536
    with pytest.raises(ValidationError, match="at most 65535"):
        OrderRow.model_validate(payload)


def test_provider_observation_requires_access_and_ledger_kind_facts() -> None:
    assert ProviderObservation.model_fields["read_only"].is_required()
    assert ProviderObservation.model_fields["commit_ledger_object_kind"].is_required()


def test_provider_observation_rejects_driver_objects() -> None:
    with pytest.raises(ValidationError):
        ProviderObservation.model_validate(
            {
                "provider": "postgresql",
                "connection_handle": "pg-snapshot",
                "object_identity": "pg:db:42",
                "object_kind": "base_table",
                "schema_digest": "a" * 64,
                "columns": [],
                "key_name": "order_id",
                "key_type": "BIGINT",
                "capabilities": ["snapshot_read"],
                "observed_at": datetime.now(UTC),
                "driver_cursor": object(),
            }
        )


@pytest.mark.parametrize(
    "required_fact",
    [
        "key_nullable",
        "key_constraint",
        "stable_key_order",
        "read_only",
        "capabilities",
        "observed_at",
        "snapshot_semantics",
        "commit_ledger_object_kind",
        "commit_ledger_columns",
        "commit_ledger_key_name",
        "commit_ledger_key_constraint",
        "evidence_safe",
    ],
)
def test_provider_observation_has_no_unsafe_required_fact_defaults(required_fact: str) -> None:
    payload = {
        "provider": "postgresql",
        "connection_handle": "pg-snapshot",
        "object_identity": "pg:db:42",
        "object_kind": "base_table",
        "schema_digest": "a" * 64,
        "columns": [],
        "key_name": "order_id",
        "key_type": "BIGINT",
        "key_nullable": False,
        "key_constraint": "primary_key",
        "stable_key_order": True,
        "read_only": True,
        "capabilities": ["snapshot_read"],
        "observed_at": datetime.now(UTC),
        "snapshot_semantics": "snapshot",
        "commit_ledger_object_kind": None,
        "commit_ledger_columns": None,
        "commit_ledger_key_name": None,
        "commit_ledger_key_constraint": None,
        "evidence_safe": True,
    }
    payload.pop(required_fact)

    with pytest.raises(ValidationError, match=required_fact):
        ProviderObservation.model_validate(payload)


def test_segment_and_visibility_artifacts_exclude_private_path_and_acceptance_key(
    tmp_path: Path,
) -> None:
    acceptance_key = 984201
    manifest = SegmentManifest(
        batch_id="batch-1",
        segment_name="segment.csv",
        segment_digest="1" * 64,
        row_set_digest="2" * 64,
        row_count=1,
        encoded_bytes=128,
        schema_digest="3" * 64,
        source_boundary_digest="4" * 64,
        acceptance_value_digest="5" * 64,
    )
    proof = VisibilityProof(
        batch_id="batch-1",
        value_digest=manifest.acceptance_value_digest,
        query_id="query-1",
        verified_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )

    for artifact in (manifest, proof):
        payload = canonical_bytes(artifact)
        assert str(tmp_path).encode() not in payload
        assert str(acceptance_key).encode() not in payload
    assert manifest.schema_version == "2"
    assert proof.schema_version == "2"


def test_source_boundary_v2_replaces_raw_bounds_with_ordered_pair_digest() -> None:
    raw_key_min = 984200
    raw_key_max = 984201
    boundary = SourceBoundary(
        object_identity="pg:opaque-source",
        schema_digest="1" * 64,
        snapshot_identity="snapshot-opaque-001",
        key_range_digest=digest({"key_min": raw_key_min, "key_max": raw_key_max}),
        row_count=2,
        query_shape_digest="2" * 64,
        opened_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC),
    )

    payload = canonical_bytes(boundary)

    assert boundary.schema_version == "2"
    assert str(raw_key_min).encode() not in payload
    assert str(raw_key_max).encode() not in payload
    with pytest.raises(ValidationError):
        SourceBoundary.model_validate(
            {
                **boundary.model_dump(exclude={"key_range_digest"}),
                "schema_version": "1",
                "key_min": raw_key_min,
                "key_max": raw_key_max,
            }
        )
