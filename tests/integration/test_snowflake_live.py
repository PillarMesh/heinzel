import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import OrderRow, SourceBoundary
from pillarmesh_provider_snowflake import SnowflakeProvider, SnowflakeSettings, encode_segment

from tests.acceptance.resource_ledger import (
    PrivateResourceLedger,
    live_diagnostic_ledger_path,
)

_REQUIRED = (
    "PILLARMESH_TEST_SNOWFLAKE_ACCOUNT",
    "PILLARMESH_TEST_SNOWFLAKE_USER",
    "PILLARMESH_TEST_SNOWFLAKE_PASSWORD",
    "PILLARMESH_TEST_SNOWFLAKE_ROLE",
    "PILLARMESH_TEST_SNOWFLAKE_WAREHOUSE",
    "PILLARMESH_TEST_SNOWFLAKE_DATABASE",
    "PILLARMESH_TEST_SNOWFLAKE_SCHEMA",
    "PILLARMESH_TEST_SNOWFLAKE_STAGE",
    "PILLARMESH_TEST_SNOWFLAKE_TARGET",
    "PILLARMESH_TEST_SNOWFLAKE_LEDGER",
    "PILLARMESH_LIVE_DIAGNOSTIC_LEDGER_DIR",
)


@pytest.mark.live
def test_dedicated_snowflake_account_commits_and_verifies_fresh_row(
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    missing = tuple(name for name in _REQUIRED if not os.getenv(name))
    if missing:
        pytest.skip("dedicated Snowflake settings are not configured: " + ", ".join(missing))
    settings = SnowflakeSettings(
        account=os.environ[_REQUIRED[0]],
        user=os.environ[_REQUIRED[1]],
        password=os.environ[_REQUIRED[2]],
        role=os.environ[_REQUIRED[3]],
        warehouse=os.environ[_REQUIRED[4]],
        database=os.environ[_REQUIRED[5]],
        schema_name=os.environ[_REQUIRED[6]],
        stage=os.environ[_REQUIRED[7]],
        target_table=os.environ[_REQUIRED[8]],
        ledger_table=os.environ[_REQUIRED[9]],
        connection_handle="live-snowflake",
    )
    provider = SnowflakeProvider(settings)
    now = datetime.now(UTC)
    acceptance_key = time.time_ns() // 1_000
    fixture_label = f"diagnostic-{secrets.token_hex(12)}"
    row = OrderRow(
        order_id=acceptance_key,
        customer_ref=fixture_label,
        amount=Decimal("10.50"),
        currency="USD",
        status="acceptance",
        updated_at=now,
    )
    boundary = SourceBoundary(
        object_identity="pg:live-test:fixture",
        schema_digest="1" * 64,
        snapshot_identity=fixture_label,
        key_range_digest=digest({"key_min": acceptance_key, "key_max": acceptance_key}),
        row_count=1,
        query_shape_digest="2" * 64,
        opened_at=now,
        closed_at=now,
    )
    resources = PrivateResourceLedger(
        live_diagnostic_ledger_path(os.environ, "snowflake"),
        clock=lambda: datetime.now(UTC),
    )
    segment = tmp_path / "segment.csv"
    local_resource = resources.register(
        kind="local_segment",
        exact_identifier=str(segment),
        retention_seconds=0,
        cleanup_operation="delete_local_state",
    )
    stage_resource: str | None = None
    commit_attempted = False
    committed = False
    test_state = "failed"
    resources.mark_attempted(local_resource)
    resources.persist(run_state="running")
    try:
        manifest = encode_segment(
            (row,), boundary, acceptance_key, f"batch-{secrets.token_hex(12)}", segment
        )
        resources.mark_created(local_resource)
        stage_prefix = (
            f"@{settings.connection_handle}:{settings.database}.{settings.schema_name}."
            f"{settings.stage}/runs/{manifest.batch_id}"
        )
        stage_resource = resources.register(
            kind="staged_segment",
            exact_identifier=stage_prefix,
            retention_seconds=24 * 60 * 60,
            cleanup_operation="delete_staged_segments",
        )
        target_resource = resources.register(
            kind="target_row",
            exact_identifier=(
                f"snowflake:{settings.connection_handle}:"
                f"{settings.database}.{settings.schema_name}.{settings.target_table}:"
                f"order_id={acceptance_key}"
            ),
            retention_seconds=30 * 24 * 60 * 60,
            cleanup_operation="delete_synthetic_rows",
        )
        ledger_resource = resources.register(
            kind="commit_ledger_entry",
            exact_identifier=(
                f"snowflake:{settings.connection_handle}:"
                f"{settings.database}.{settings.schema_name}.{settings.ledger_table}:"
                f"batch_id={manifest.batch_id}"
            ),
            retention_seconds=30 * 24 * 60 * 60,
            cleanup_operation="delete_synthetic_rows",
        )
        observation = provider.observe()
        resources.mark_attempted(stage_resource)
        resources.persist(run_state="running")
        try:
            provider.stage(segment, manifest)
        except BaseException:
            resources.mark_quarantined(stage_resource)
            raise
        else:
            resources.mark_created(stage_resource)
        resources.mark_attempted(target_resource)
        resources.mark_attempted(ledger_resource)
        resources.persist(run_state="running")
        commit_attempted = True
        receipt = provider.commit_or_resolve(manifest)
        committed = True
        resources.mark_created(target_resource)
        resources.mark_created(ledger_resource)
        proof = provider.verify_visibility(manifest, acceptance_key)

        assert observation.commit_ledger_columns is not None
        assert observation.commit_ledger_key_name == "batch_id"
        assert observation.commit_ledger_key_constraint == "primary_key"
        assert receipt.manifest_digest
        assert proof.value_digest == manifest.acceptance_value_digest
        test_state = "succeeded"
    finally:
        segment.unlink(missing_ok=True)
        resources.mark_completed(local_resource)
        if stage_resource is not None and test_state == "failed":
            resources.mark_quarantined(stage_resource)
        if commit_attempted and not committed:
            resources.mark_attempted(target_resource)
            resources.mark_attempted(ledger_resource)
        resources.persist(run_state=test_state)
        digests = ",".join(
            str(item["resource_digest"]) for item in resources.sanitized_dispositions()
        )
        record_property("pillarmesh_resource_digests", digests)
