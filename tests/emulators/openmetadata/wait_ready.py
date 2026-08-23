from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

import httpx


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parsed = parser.parse_args(arguments)
    deadline = time.monotonic() + parsed.timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{parsed.url}/api/v1/system/health", timeout=5.0)
            if _is_healthy(response):
                return 0
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1.0)
    print("ERROR: OpenMetadata did not report terminal healthy status", file=sys.stderr)
    return 1


def _is_healthy(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return False
    if response.text.strip() == "OK":
        return True
    payload = response.json()
    return isinstance(payload, dict) and payload.get("status") in {"ok", "healthy"}


if __name__ == "__main__":
    raise SystemExit(main())
