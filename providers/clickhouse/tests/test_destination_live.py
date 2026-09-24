from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from heinzel_provider_clickhouse.destination import (
    ClickHouseDestinationProvider,
    ClickHouseLandStore,
    ClickHouseLandStoreSettings,
)
from heinzel_provider_sdk.destination_conformance import destination_segment, destination_target
from pydantic import SecretStr

_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("HEINZEL_RUN_DESTINATION_EMULATORS") != "1",
        reason="set HEINZEL_RUN_DESTINATION_EMULATORS=1",
    ),
]


@pytest.fixture
def clickhouse_settings() -> Iterator[ClickHouseLandStoreSettings]:
    name = f"heinzel-land-ch-{uuid.uuid4().hex[:12]}"
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
            "CLICKHOUSE_USER=ingestion",
            "--env",
            f"CLICKHOUSE_PASSWORD={password}",
            "--env",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--publish",
            f"127.0.0.1:{port}:8123",
            _IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    try:
        endpoint = f"http://127.0.0.1:{port}"
        auth = ("ingestion", password)
        deadline = time.monotonic() + 60
        while True:
            try:
                response = httpx.post(endpoint, auth=auth, content="SELECT 1", timeout=2)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickHouse emulator did not become ready")
            time.sleep(0.2)
        setup = (
            "CREATE DATABASE raw",
            "CREATE DATABASE control",
            "CREATE TABLE raw.raw_orders (generation_id String, row_ordinal UInt64, "
            "segment_digest String, insert_token String, payload String) "
            "ENGINE = MergeTree ORDER BY (generation_id, row_ordinal)",
            "CREATE TABLE control.land_receipts (idempotency_key String, generation_id String, "
            "receipt_payload String) ENGINE = MergeTree ORDER BY idempotency_key",
        )
        for statement in setup:
            response = httpx.post(endpoint, auth=auth, content=statement, timeout=10)
            response.raise_for_status()
        yield ClickHouseLandStoreSettings(
            endpoint=endpoint,
            username="ingestion",
            password=SecretStr(password),
            raw_database_name="raw",
            ledger_database_name="control",
            ledger_table_name="land_receipts",
            verify_tls=False,
        )
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def test_live_clickhouse_land_replay_and_post_insert_reconciliation(
    clickhouse_settings: ClickHouseLandStoreSettings,
) -> None:
    store = ClickHouseLandStore(clickhouse_settings)
    provider = ClickHouseDestinationProvider(store=store)
    segment = destination_segment()
    target = destination_target()

    first = asyncio.run(provider.land(segment=segment, target=target, idempotency_key="7" * 64))
    replay = asyncio.run(provider.land(segment=segment, target=target, idempotency_key="7" * 64))

    fired = False

    def lose_response() -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise TimeoutError("injected post-insert timeout")

    timeout_store = ClickHouseLandStore(clickhouse_settings, after_insert_hook=lose_response)
    timeout_target = target.model_copy(update={"trigger_window": "2026-09-11T13:00:00Z/PT1H"})
    reconciled = asyncio.run(
        ClickHouseDestinationProvider(store=timeout_store).land(
            segment=segment,
            target=timeout_target,
            idempotency_key="8" * 64,
        )
    )

    auth = (clickhouse_settings.username, clickhouse_settings.password.get_secret_value())
    row_count = httpx.post(
        clickhouse_settings.endpoint,
        auth=auth,
        content="SELECT count() FROM raw.raw_orders",
        timeout=10,
    )
    receipt_count = httpx.post(
        clickhouse_settings.endpoint,
        auth=auth,
        content="SELECT count() FROM control.land_receipts",
        timeout=10,
    )
    store.close()
    timeout_store.close()

    assert replay == first
    assert reconciled.idempotency_key == "8" * 64
    assert row_count.text.strip() == "2"
    assert receipt_count.text.strip() == "2"
