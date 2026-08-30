from __future__ import annotations

import argparse
import os
import ssl
import time
from pathlib import Path

import httpx


def wait_ready(
    *,
    endpoint: str,
    username: str,
    password: str,
    root_certificate: Path,
    client_certificate: Path,
    client_private_key: Path,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: httpx.HTTPError | None = None
    context = ssl.create_default_context(cafile=str(root_certificate))
    context.load_cert_chain(
        certfile=str(client_certificate),
        keyfile=str(client_private_key),
    )
    while time.monotonic() < deadline:
        try:
            with httpx.Client(
                verify=context,
                auth=(username, password),
                timeout=2,
                trust_env=False,
            ) as client:
                response = client.post(endpoint, content=b"SELECT 1")
                response.raise_for_status()
                if response.content.strip() == b"1":
                    return
        except httpx.HTTPError as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError("ClickHouse HTTPS readiness deadline expired") from last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--root-certificate", required=True, type=Path)
    parser.add_argument("--client-certificate", required=True, type=Path)
    parser.add_argument("--client-private-key", required=True, type=Path)
    parser.add_argument("--timeout-seconds", default=120.0, type=float)
    arguments = parser.parse_args()
    password = os.environ.get("PILLARMESH_CLICKHOUSE_PASSWORD")
    if not password:
        raise ValueError("PILLARMESH_CLICKHOUSE_PASSWORD is required")
    wait_ready(
        endpoint=arguments.endpoint,
        username=arguments.username,
        password=password,
        root_certificate=arguments.root_certificate,
        client_certificate=arguments.client_certificate,
        client_private_key=arguments.client_private_key,
        timeout_seconds=arguments.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
