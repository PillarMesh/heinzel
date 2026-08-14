from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import OrderRow, SourceBoundary
from pillarmesh_provider_snowflake import encode_segment

from tests.emulators.localstack_support import (
    assert_expected_observation,
    localstack_provider,
)


@pytest.mark.live
@pytest.mark.emulator
def test_provider_stages_commits_verifies_and_replays(tmp_path: Path) -> None:
    if os.getenv("PILLARMESH_LOCALSTACK_SNOWFLAKE") != "1":
        pytest.skip("LocalStack Snowflake must be started by the emulator launcher")

    provider = localstack_provider()
    now = datetime.now(UTC)
    acceptance_key = secrets.randbits(62)
    row = OrderRow(
        order_id=acceptance_key,
        customer_ref=f"localstack-{secrets.token_hex(12)}",
        amount=Decimal("10.50"),
        currency="USD",
        status="acceptance",
        updated_at=now,
    )
    boundary = SourceBoundary(
        object_identity="pg:local-emulator:fixture",
        schema_digest="1" * 64,
        snapshot_identity=f"localstack-{secrets.token_hex(12)}",
        key_range_digest=digest({"key_min": acceptance_key, "key_max": acceptance_key}),
        row_count=1,
        query_shape_digest="2" * 64,
        opened_at=now,
        closed_at=now,
    )
    segment = tmp_path / "segment.csv"
    manifest = encode_segment(
        (row,),
        boundary,
        acceptance_key,
        f"batch-{secrets.token_hex(12)}",
        segment,
    )

    observation = provider.observe()
    provider.stage(segment, manifest)
    receipt = provider.commit_or_resolve(manifest)
    proof = provider.verify_visibility(manifest, acceptance_key)
    replay = provider.commit_or_resolve(manifest)

    assert_expected_observation(observation)
    assert receipt.replayed is False
    assert receipt.manifest_digest == digest(manifest)
    assert proof.value_digest == manifest.acceptance_value_digest
    assert replay.replayed is True
    assert replay.manifest_digest == receipt.manifest_digest
    assert replay.affected_rows == 0
