from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from heinzel_contract_model import digest
from heinzel_provider_postgresql import (
    PostgreSQLProductSqlObservationRequest,
    PostgreSQLProductSqlObservationSettings,
    PostgreSQLProductSqlObserver,
)
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pydantic import SecretStr

_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_IMAGE_DIGEST = _IMAGE.rsplit("@sha256:", 1)[1]

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("HEINZEL_RUN_DESTINATION_EMULATORS") != "1",
        reason="set HEINZEL_RUN_DESTINATION_EMULATORS=1",
    ),
]


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def postgresql_dsn() -> Iterator[str]:
    name = f"heinzel-product-sql-pg-{uuid.uuid4().hex[:12]}"
    password = f"observation-{uuid.uuid4().hex}"
    port = _available_loopback_port()
    subprocess.run(
        (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--publish",
            f"127.0.0.1:{port}:5432",
            _IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    try:
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        with psycopg.connect(dsn) as connection:
            connection.execute("CREATE SCHEMA raw")
            # Laid out like a real managed landing relation: the statement reads only
            # generation_id and payload; the bookkeeping columns must be described, not refused.
            connection.execute(
                "CREATE TABLE raw.raw_sales (generation_id text NOT NULL, "
                "row_ordinal bigint NOT NULL, segment_digest text NOT NULL, "
                "payload jsonb NOT NULL)"
            )
            # A NOT NULL ... NOT VALID constraint over existing NULLs: PostgreSQL 18 still sets
            # attnotnull, and the observer must not report such a column as non-null.
            connection.execute("CREATE TABLE raw.unvalidated (generation_id text, payload jsonb)")
            connection.execute("INSERT INTO raw.unvalidated VALUES (NULL, '{}')")
            connection.execute(
                "ALTER TABLE raw.unvalidated "
                "ADD CONSTRAINT generation_present NOT NULL generation_id NOT VALID"
            )
        yield dsn
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def test_live_observer_reads_postgresql_catalog_and_sum_behavior(
    postgresql_dsn: str,
) -> None:
    with psycopg.connect(postgresql_dsn) as connection:
        version_row = connection.execute(
            "SELECT current_setting('server_version_num'), current_setting('server_version')"
        ).fetchone()
        assert version_row is not None
        server_version_number, server_version_text = version_row
    engine_version = f"{int(server_version_number) // 10_000}.{int(server_version_number) % 10_000}"
    engine_build_digest = digest(
        {
            "domain": "heinzel-postgresql-engine-build-v1",
            "version": {
                "server_version_num": str(server_version_number),
                "server_version": str(server_version_text),
            },
        }
    )
    validation = _validation_evidence(
        engine_version=engine_version,
        engine_build_digest=engine_build_digest,
    )
    observer = PostgreSQLProductSqlObserver(
        PostgreSQLProductSqlObservationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr(postgresql_dsn),
        )
    )

    observation = observer.observe(
        PostgreSQLProductSqlObservationRequest(
            tenant_id="tenant-a",
            warehouse_binding_id="warehouse-a",
            warehouse_binding_revision=4,
            relation_ref="relation-revenue-events-v1",
            relation_namespace="raw",
            relation_name="raw_sales",
        ),
        warehouse_validation=validation,
    )

    assert tuple(
        (column.name, column.logical_type, column.physical_type, column.nullable)
        for column in observation.columns
    ) == (
        ("generation_id", "string", "TEXT", False),
        ("row_ordinal", "other", "BIGINT", False),
        ("segment_digest", "string", "TEXT", False),
        ("payload", "json", "JSONB", False),
    )
    assert observation.columns[0].collation == "default"
    assert observation.columns[0].encoding == "UTF8"
    assert observation.sum_semantics.input_physical_type == "NUMERIC(38,9)"
    assert observation.sum_semantics.accumulator_physical_type == "INTERNAL"
    assert observation.sum_semantics.result_physical_type == "NUMERIC"
    assert observation.sum_semantics.overflow_behavior == "promote"


def _validation_evidence(
    *,
    engine_version: str,
    engine_build_digest: str,
) -> WarehouseValidationEvidence:
    return WarehouseValidationEvidence(
        evidence_id="warehouse-validation-live-1",
        tenant_id="tenant-a",
        binding_id="warehouse-a",
        binding_revision=4,
        validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        engine_kind=EngineKind.POSTGRESQL,
        engine_version=engine_version,
        engine_build_digest=engine_build_digest,
        engine_image_digest=_IMAGE_DIGEST,
        principal_profile_digest="1" * 64,
        namespace_grant_matrix_digest="2" * 64,
        tls_probe_digest="3" * 64,
        network_isolation_probe_digest="4" * 64,
        encryption_at_rest_evidence_digest="5" * 64,
        encryption_at_rest_disposition=EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE,
        positive_probe_digest="6" * 64,
        denial_probe_digest="7" * 64,
        ledger_probe_digest="8" * 64,
        monitoring_probe_digest="9" * 64,
        capacity_alert_probe_digest="a" * 64,
        backup_artifact_digest="b" * 64,
        restore_verification_digest="c" * 64,
        restore_cleanup_digest="d" * 64,
        observed_at=datetime.now(UTC),
    )


def test_live_observer_does_not_trust_an_unvalidated_not_null_constraint(
    postgresql_dsn: str,
) -> None:
    with psycopg.connect(postgresql_dsn) as connection:
        version_row = connection.execute(
            "SELECT current_setting('server_version_num'), current_setting('server_version')"
        ).fetchone()
        assert version_row is not None
        server_version_number, server_version_text = version_row
        attnotnull = connection.execute(
            "SELECT attnotnull FROM pg_catalog.pg_attribute "
            "WHERE attrelid = 'raw.unvalidated'::regclass AND attname = 'generation_id'"
        ).fetchone()
    assert attnotnull == (True,), "precondition of this test: PostgreSQL sets attnotnull"
    engine_version = f"{int(server_version_number) // 10_000}.{int(server_version_number) % 10_000}"
    validation = _validation_evidence(
        engine_version=engine_version,
        engine_build_digest=digest(
            {
                "domain": "heinzel-postgresql-engine-build-v1",
                "version": {
                    "server_version_num": str(server_version_number),
                    "server_version": str(server_version_text),
                },
            }
        ),
    )
    observer = PostgreSQLProductSqlObserver(
        PostgreSQLProductSqlObservationSettings(tenant_id="tenant-a", dsn=SecretStr(postgresql_dsn))
    )

    observation = observer.observe(
        PostgreSQLProductSqlObservationRequest(
            tenant_id="tenant-a",
            warehouse_binding_id="warehouse-a",
            warehouse_binding_revision=4,
            relation_ref="relation-unvalidated-v1",
            relation_namespace="raw",
            relation_name="unvalidated",
        ),
        warehouse_validation=validation,
    )

    assert observation.columns[0].name == "generation_id"
    assert observation.columns[0].nullable is True
