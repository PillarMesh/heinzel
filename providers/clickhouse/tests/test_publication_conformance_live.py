from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping

import httpx
import pytest
from heinzel_provider_clickhouse import (
    CLICKHOUSE_SERVER_VERSION,
    ClickHousePublicationConformanceProbe,
    ClickHousePublicationConformanceRequest,
    ClickHousePublicationConformanceResponse,
    ClickHousePublicationConformanceSettings,
    ClickHousePublicationConformanceTransport,
    HttpxClickHousePublicationConformanceTransport,
)
from pydantic import SecretStr

_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)

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


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@pytest.fixture
def publication_settings() -> Iterator[ClickHousePublicationConformanceSettings]:
    container_name = f"heinzel-publication-ch-{uuid.uuid4().hex[:12]}"
    administration_password = f"admin-{uuid.uuid4().hex}"
    transformation_password = f"transform-{uuid.uuid4().hex}"
    port = _available_loopback_port()
    subprocess.run(
        (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container_name,
            "--env",
            "CLICKHOUSE_USER=administrator",
            "--env",
            f"CLICKHOUSE_PASSWORD={administration_password}",
            "--env",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            "--publish",
            f"127.0.0.1:{port}:8123",
            _IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    administration_auth = ("administrator", administration_password)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                response = httpx.post(
                    endpoint,
                    auth=administration_auth,
                    content="SELECT 1",
                    timeout=2,
                )
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickHouse publication emulator did not become ready")
            time.sleep(0.2)
        statements = (
            "CREATE DATABASE product",
            "CREATE DATABASE consumption",
            "CREATE TABLE product.revenue_v1_g1 (region String) ENGINE = MergeTree ORDER BY region",
            "CREATE TABLE product.revenue_v1_g2 (region String) ENGINE = MergeTree ORDER BY region",
            "INSERT INTO product.revenue_v1_g1 VALUES ('initial')",
            "INSERT INTO product.revenue_v1_g2 VALUES ('replacement')",
            "CREATE VIEW consumption.revenue_current AS SELECT * FROM product.revenue_v1_g1",
            "CREATE ROLE transformation_runtime",
            "CREATE USER transformer IDENTIFIED WITH sha256_password BY "
            + _sql_string(transformation_password),
            "GRANT transformation_runtime TO transformer",
            "GRANT SELECT, INSERT, ALTER, DROP TABLE ON product.* TO transformation_runtime",
        )
        for statement in statements:
            response = httpx.post(
                endpoint,
                auth=administration_auth,
                content=statement,
                timeout=10,
            )
            response.raise_for_status()
        yield ClickHousePublicationConformanceSettings(
            endpoint=endpoint,
            administration_username="administrator",
            administration_password=SecretStr(administration_password),
            transformation_username="transformer",
            transformation_password=SecretStr(transformation_password),
            verify_tls=False,
        )
    finally:
        subprocess.run(("docker", "stop", container_name), check=False, capture_output=True)


def _request() -> ClickHousePublicationConformanceRequest:
    return ClickHousePublicationConformanceRequest(
        product_database="product",
        consumption_database="consumption",
        initial_generation_table="revenue_v1_g1",
        replacement_generation_table="revenue_v1_g2",
        stable_view_name="revenue_current",
        transformation_role="transformation_runtime",
    )


class _LoseEffectResponseTransport:
    def __init__(
        self,
        delegate: ClickHousePublicationConformanceTransport,
        *,
        effect_prefix: str,
    ) -> None:
        self._delegate = delegate
        self._effect_prefix = effect_prefix
        self._lost = False

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> ClickHousePublicationConformanceResponse:
        response = self._delegate.execute(
            url=url,
            headers=headers,
            params=params,
            content=content,
            timeout_seconds=timeout_seconds,
        )
        if not self._lost and content.decode("utf-8").startswith(self._effect_prefix):
            self._lost = True
            raise httpx.ReadTimeout("injected response loss after committed effect")
        return response


@pytest.mark.parametrize("lost_effect", ["CREATE OR REPLACE VIEW", "REVOKE"])
def test_pinned_clickhouse_proves_publication_mechanism_and_reconciles_lost_response(
    publication_settings: ClickHousePublicationConformanceSettings,
    lost_effect: str,
) -> None:
    transport = _LoseEffectResponseTransport(
        HttpxClickHousePublicationConformanceTransport(verify_tls=False),
        effect_prefix=lost_effect,
    )
    evidence = ClickHousePublicationConformanceProbe(
        settings=publication_settings,
        transport=transport,
    ).run(_request())

    assert evidence.engine_version == CLICKHOUSE_SERVER_VERSION
    assert evidence.initial_generation_uuid != evidence.replacement_generation_uuid
    assert evidence.generation_engine == "MergeTree"
    assert evidence.observed_view_target == "product.revenue_v1_g2"
    assert evidence.sealed_write_privileges == ("ALTER", "DROP TABLE", "INSERT")
    assert evidence.view_ambiguous_outcome_reconciled is (lost_effect == "CREATE OR REPLACE VIEW")
    assert evidence.seal_ambiguous_outcome_reconciled is (lost_effect == "REVOKE")
