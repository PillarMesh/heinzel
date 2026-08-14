from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import OrderRow, SourceBoundary
from pillarmesh_provider_snowflake import EncodingLimits, ResourceLimitExceeded, encode_segment


def boundary() -> SourceBoundary:
    return SourceBoundary(
        object_identity="pg:fixture:orders:42",
        schema_digest="1" * 64,
        snapshot_identity="10:20:",
        key_range_digest=digest({"key_min": 7, "key_max": 7}),
        row_count=1,
        query_shape_digest="2" * 64,
        opened_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC),
    )


def row(customer: str = 'customer,"seven"', *, order_id: int = 7) -> OrderRow:
    return OrderRow(
        order_id=order_id,
        customer_ref=customer,
        amount=Decimal("10.50"),
        currency="USD",
        status="paid",
        updated_at=datetime(2026, 8, 13, 12, 34, 56, 123456, tzinfo=UTC),
    )


def test_segment_has_exact_deterministic_bytes(tmp_path: Path) -> None:
    output = tmp_path / "segment.csv"

    manifest = encode_segment([row()], boundary(), 7, "batch-1", output)

    assert output.read_bytes() == (
        b"order_id,customer_ref,amount,currency,order_status,updated_at\n"
        b'7,"customer,""seven""",10.50,USD,paid,2026-08-13T12:34:56.123456Z\n'
    )
    assert manifest.row_count == 1
    assert manifest.encoded_bytes == len(output.read_bytes())
    assert len(manifest.acceptance_value_digest) == 64


def test_manifest_excludes_local_path_and_raw_acceptance_key(tmp_path: Path) -> None:
    output_dir = tmp_path / "private-runtime-materialization"
    output = output_dir / "segment.csv"
    acceptance_key = 984201

    manifest = encode_segment(
        [row(order_id=acceptance_key)], boundary(), acceptance_key, "batch-1", output
    )

    payload = canonical_bytes(manifest)
    assert str(output_dir).encode() not in payload
    assert str(acceptance_key).encode() not in payload
    assert manifest.segment_name == "segment.csv"


def test_segment_rejects_row_and_byte_limits_before_returning_manifest(tmp_path: Path) -> None:
    with pytest.raises(ResourceLimitExceeded, match="row ceiling"):
        encode_segment(
            [row(), row("customer-8")],
            boundary(),
            7,
            "batch-1",
            tmp_path / "rows.csv",
            limits=EncodingLimits(max_rows=1, max_bytes=1024),
        )

    with pytest.raises(ResourceLimitExceeded, match="byte ceiling"):
        encode_segment(
            [row()],
            boundary(),
            7,
            "batch-2",
            tmp_path / "bytes.csv",
            limits=EncodingLimits(max_rows=10, max_bytes=10),
        )

    assert not (tmp_path / "rows.csv").exists()
    assert not (tmp_path / "bytes.csv").exists()


def test_segment_requires_acceptance_key_in_snapshot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="acceptance key"):
        encode_segment([row()], boundary(), 99, "batch-1", tmp_path / "segment.csv")
