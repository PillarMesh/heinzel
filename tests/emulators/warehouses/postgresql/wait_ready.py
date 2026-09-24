from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import psycopg


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--database", default="heinzel_warehouse")
    parser.add_argument("--username", default="postgres")
    parser.add_argument("--tls-directory", required=True, type=Path)
    parser.add_argument("--timeout-seconds", default=120.0, type=float)
    parsed = parser.parse_args(arguments)
    password = os.environ.get("HEINZEL_POSTGRES_READY_PASSWORD")
    if not password:
        print("ERROR: PostgreSQL readiness credential is unavailable", file=sys.stderr)
        return 2
    deadline = time.monotonic() + parsed.timeout_seconds
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=parsed.host,
                port=parsed.port,
                dbname=parsed.database,
                user=parsed.username,
                password=password,
                sslmode="verify-full",
                sslrootcert=parsed.tls_directory / "ca.crt",
                sslcert=parsed.tls_directory / "client.crt",
                sslkey=parsed.tls_directory / "client.key",
                connect_timeout=3,
            ) as connection:
                row = connection.execute("SELECT 1").fetchone()
                if row == (1,):
                    return 0
        except psycopg.Error:
            pass
        time.sleep(0.5)
    print("ERROR: PostgreSQL did not reach TLS readiness", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
