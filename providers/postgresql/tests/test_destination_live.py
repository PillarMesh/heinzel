from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from pillarmesh_provider_postgresql.destination import (
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
)
from pillarmesh_provider_sdk.destination_conformance import destination_segment, destination_target

_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("PILLARMESH_RUN_DESTINATION_EMULATORS") != "1",
        reason="set PILLARMESH_RUN_DESTINATION_EMULATORS=1",
    ),
]


@pytest.fixture
def postgresql_dsn() -> Iterator[str]:
    name = f"pillarmesh-land-pg-{uuid.uuid4().hex[:12]}"
    password = f"destination-{uuid.uuid4().hex}"
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
            connection.execute("CREATE SCHEMA control")
            connection.execute(
                "CREATE TABLE raw.raw_orders (generation_id text NOT NULL, "
                "row_ordinal bigint NOT NULL, segment_digest text NOT NULL, "
                "payload jsonb NOT NULL, PRIMARY KEY (generation_id, row_ordinal))"
            )
            connection.execute(
                "CREATE TABLE control.land_receipts (idempotency_key text PRIMARY KEY, "
                "generation_id text UNIQUE NOT NULL, receipt_payload jsonb NOT NULL)"
            )
        yield dsn
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def test_live_postgresql_land_replay_and_post_commit_reconciliation(
    postgresql_dsn: str,
) -> None:
    store = PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn=postgresql_dsn,
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        )
    )
    provider = PostgreSQLDestinationProvider(store=store)
    segment = destination_segment()
    target = destination_target()

    first = asyncio.run(provider.land(segment=segment, target=target, idempotency_key="7" * 64))
    replay = asyncio.run(provider.land(segment=segment, target=target, idempotency_key="7" * 64))

    fired = False

    def lose_response() -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise TimeoutError("injected post-commit timeout")

    timeout_store = PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn=postgresql_dsn,
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        ),
        after_commit_hook=lose_response,
    )
    timeout_target = target.model_copy(update={"trigger_window": "2026-09-11T13:00:00Z/PT1H"})
    reconciled = asyncio.run(
        PostgreSQLDestinationProvider(store=timeout_store).land(
            segment=segment,
            target=timeout_target,
            idempotency_key="8" * 64,
        )
    )

    with psycopg.connect(postgresql_dsn) as connection:
        row_count = connection.execute("SELECT count(*) FROM raw.raw_orders").fetchone()
        receipt_count = connection.execute("SELECT count(*) FROM control.land_receipts").fetchone()

    assert replay == first
    assert reconciled.idempotency_key == "8" * 64
    assert row_count == (2,)
    assert receipt_count == (2,)
