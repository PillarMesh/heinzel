import os
from collections.abc import Callable

import pytest
from heinzel_provider_postgresql import PostgresProvider, PostgresSettings


@pytest.mark.live
def test_dedicated_postgres_account_supports_observe_and_snapshot(
    record_property: Callable[[str, object], None],
) -> None:
    dsn = os.getenv("HEINZEL_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("HEINZEL_TEST_POSTGRES_DSN is not configured")
    provider = PostgresProvider(
        PostgresSettings(
            dsn=dsn,
            connection_handle="live-postgresql",
            schema_name=os.getenv("HEINZEL_TEST_POSTGRES_SCHEMA", "heinzel_m0"),
            table_name=os.getenv("HEINZEL_TEST_POSTGRES_TABLE", "orders"),
        )
    )

    observation = provider.observe()
    boundary, rows = provider.read_snapshot()
    snapshot_rows = tuple(rows)

    assert observation.object_kind == "base_table"
    assert boundary.object_identity == observation.object_identity
    assert boundary.schema_digest == observation.schema_digest
    assert len(snapshot_rows) == boundary.row_count
    record_property("heinzel_created_resource_count", 0)
