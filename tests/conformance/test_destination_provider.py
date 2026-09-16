from __future__ import annotations

import pytest
from pillarmesh_provider_sdk import StagedSegment, staged_segment_digest
from pydantic import ValidationError


def test_staged_segment_rejects_a_forged_digest() -> None:
    with pytest.raises(ValidationError, match="segment_digest"):
        StagedSegment(
            segment_digest="f" * 64,
            schema_digest="a" * 64,
            record_count=1,
            rows=(b'{"order_id":"order-7"}',),
        )


def test_staged_segment_rejects_a_forged_record_count() -> None:
    rows = (b'{"order_id":"order-7"}',)

    with pytest.raises(ValidationError, match="record_count"):
        StagedSegment(
            segment_digest=staged_segment_digest(rows),
            schema_digest="a" * 64,
            record_count=2,
            rows=rows,
        )
