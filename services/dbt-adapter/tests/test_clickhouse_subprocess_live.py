from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import digest
from heinzel_dbt_adapter import (
    CompiledDbtModel,
    DbtColumnTest,
    DbtInvocationAuthority,
    DbtInvoker,
    DbtSubprocessSettings,
    SignedCompiledDbtModel,
    SubprocessDbtRunner,
    compiled_dbt_model_signing_bytes,
)

_IMAGE = (
    "clickhouse/clickhouse-server:25.8.32.4@"
    "sha256:7c39abeb161d627fa3ca6a1e5f6241ecdc24501e8463486e61b80be3ab4471b0"
)
_CONTRACT_DIGEST = "1" * 64
_INPUT_GENERATION_DIGEST = "2" * 64
_TARGET_DATABASE = "contract_" + _CONTRACT_DIGEST[:54]

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
def clickhouse_endpoint() -> Iterator[tuple[str, str]]:
    container_name = f"heinzel-dbt-ch-{uuid.uuid4().hex[:12]}"
    password = f"dbt-{uuid.uuid4().hex}"
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
            "CLICKHOUSE_USER=dbt_runtime",
            "--env",
            f"CLICKHOUSE_PASSWORD={password}",
            "--publish",
            f"127.0.0.1:{port}:8123",
            _IMAGE,
        ),
        check=True,
        capture_output=True,
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                response = httpx.post(
                    endpoint,
                    auth=("dbt_runtime", password),
                    content="SELECT version()",
                    timeout=2,
                )
                if response.status_code == 200:
                    assert response.text.strip() == "25.8.32.4"
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickHouse dbt emulator did not become ready")
            time.sleep(0.2)
        yield endpoint, password
    finally:
        subprocess.run(("docker", "stop", container_name), check=False, capture_output=True)


def _write_profile(directory: Path, *, port: int) -> None:
    directory.mkdir(mode=0o700)
    (directory / "profiles.yml").write_text(
        "heinzel_materialization:\n"
        "  target: clickhouse\n"
        "  outputs:\n"
        "    clickhouse:\n"
        "      type: clickhouse\n"
        "      host: 127.0.0.1\n"
        f"      port: {port}\n"
        "      user: dbt_runtime\n"
        "      password: \"{{ env_var('HEINZEL_DBT_CLICKHOUSE_TEST_PASSWORD') }}\"\n"
        f"      schema: {_TARGET_DATABASE}\n"
        "      secure: false\n"
        "      threads: 1\n",
        encoding="utf-8",
    )


def _signed_model(private_key: Ed25519PrivateKey) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="product_revenue_v1_g1",
        contract_digest=_CONTRACT_DIGEST,
        provider="clickhouse",
        input_generation_digests=(_INPUT_GENERATION_DIGEST,),
        target_schema=_TARGET_DATABASE,
        output_columns=("region", "total_revenue"),
        quality_tests=(
            DbtColumnTest(column_name="region", kind="not_null"),
            DbtColumnTest(column_name="total_revenue", kind="not_null"),
        ),
        compiled_sql=(
            "SELECT CAST('east' AS String) AS region, "
            "toDecimal64('99.000000000', 9) AS total_revenue"
        ),
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-clickhouse-live-1",
        signature=base64.b64encode(
            private_key.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


def test_pinned_dbt_clickhouse_executes_a_signed_table_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clickhouse_endpoint: tuple[str, str],
) -> None:
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")
    endpoint, password = clickhouse_endpoint
    port = int(endpoint.rsplit(":", maxsplit=1)[1])
    profiles = tmp_path / "profiles"
    _write_profile(profiles, port=port)
    monkeypatch.setenv("HEINZEL_DBT_CLICKHOUSE_TEST_PASSWORD", password)
    private_key = Ed25519PrivateKey.generate()
    signed_model = _signed_model(private_key)
    invoker = DbtInvoker(
        trusted_compiler_keys={"compiler-clickhouse-live-1": private_key.public_key()},
        runner=SubprocessDbtRunner(
            DbtSubprocessSettings(
                executable=Path(dbt_executable),
                profiles_directory=profiles,
                workspace_directory=tmp_path,
                timeout_seconds=120,
                credential_environment_names=("HEINZEL_DBT_CLICKHOUSE_TEST_PASSWORD",),
            )
        ),
    )

    receipt = invoker.invoke(
        signed_model=signed_model,
        authority=DbtInvocationAuthority(
            contract_digest=_CONTRACT_DIGEST,
            provider="clickhouse",
            input_generation_digests=(_INPUT_GENERATION_DIGEST,),
        ),
    )

    response = httpx.post(
        endpoint,
        auth=("dbt_runtime", password),
        content=(
            "SELECT region, toDecimalString(total_revenue, 9), toTypeName(total_revenue) "
            f"FROM {_TARGET_DATABASE}.product_revenue_v1_g1 FORMAT TabSeparatedRaw"
        ),
        timeout=10,
    )
    response.raise_for_status()
    assert response.text.strip() == "east\t99.000000000\tDecimal(18, 9)"
    assert receipt.provider == "clickhouse"
    assert receipt.model_digest == signed_model.model_digest
    assert receipt.quality_assertion_count == 2
    assert receipt.quality_disposition == "passed"
