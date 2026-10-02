"""Rejected credentials over TLS, on the pinned PostgreSQL engine.

The startup denial probe mirrors libpq's negotiation exactly and never sends a credential over a
channel it has not verified. These journeys prove both halves against a TLS-enabled, digest-pinned
PostgreSQL 18.6 server: `verify-full` recovers the structured rejection, while the modes the probe
cannot mirror without weakening transport security keep their transient classification.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from heinzel_provider_postgresql import PostgreSQLLandStore, PostgreSQLLandStoreSettings
from heinzel_provider_postgresql.destination import PostgreSQLLandStoreError
from pydantic import SecretStr

_IMAGE = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_CERTIFICATE_COMMAND = (
    'openssl req -new -x509 -days 1 -nodes -subj "/CN=localhost" '
    '-addext "subjectAltName=DNS:localhost,IP:127.0.0.1" '
    "-keyout /tmp/server.key -out /tmp/server.crt >/dev/null 2>&1 && "
    "chown postgres:postgres /tmp/server.key /tmp/server.crt && "
    "chmod 600 /tmp/server.key && "
    "exec docker-entrypoint.sh postgres -c ssl=on "
    "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
)

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") != "1",
        reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1",
    ),
]


@dataclass(frozen=True)
class _TlsCluster:
    port: int
    password: str
    root_certificate: Path


def _available_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _tls_postgresql(root: Path) -> Iterator[_TlsCluster]:
    name = f"heinzel-tls-pg-{uuid.uuid4().hex[:12]}"
    password = f"tls-{uuid.uuid4().hex}"
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
            "--tmpfs",
            "/var/lib/postgresql",
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--entrypoint",
            "bash",
            _IMAGE,
            "-c",
            _CERTIFICATE_COMMAND,
        ),
        check=True,
        capture_output=True,
    )
    root_certificate = root / "server.crt"
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(
                    _conninfo(port=port, password=password, sslmode="require"),
                ):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        subprocess.run(
            ("docker", "cp", f"{name}:/tmp/server.crt", str(root_certificate)),
            check=True,
            capture_output=True,
        )
        yield _TlsCluster(port=port, password=password, root_certificate=root_certificate)
    finally:
        subprocess.run(("docker", "stop", name), check=False, capture_output=True)


def _conninfo(
    *,
    port: int,
    password: str,
    sslmode: str,
    root_certificate: Path | None = None,
) -> str:
    # Every value is a string: a conninfo parameter is textual, and `make_conninfo` also
    # takes `conninfo` as its first positional parameter, so a wider value type makes the
    # `**values` expansion ambiguous against it.
    values: dict[str, str] = {
        "host": "localhost",
        "port": str(port),
        "dbname": "postgres",
        "user": "postgres",
        "password": password,
        "sslmode": sslmode,
        "connect_timeout": "5",
    }
    if root_certificate is not None:
        values["sslrootcert"] = str(root_certificate)
    return psycopg.conninfo.make_conninfo(**values)


def _land_store(dsn: str) -> PostgreSQLLandStore:
    return PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn=SecretStr(dsn),
            raw_schema_name="raw",
            ledger_schema_name="control",
            ledger_table_name="land_receipts",
        )
    )


def test_verify_full_rejected_credentials_are_authorization_denied(tmp_path: Path) -> None:
    with _tls_postgresql(tmp_path) as cluster:
        accepted = _conninfo(
            port=cluster.port,
            password=cluster.password,
            sslmode="verify-full",
            root_certificate=cluster.root_certificate,
        )
        with psycopg.connect(accepted) as connection:
            encrypted = connection.execute(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).fetchone()
        with pytest.raises(PostgreSQLLandStoreError) as denied:
            _land_store(
                _conninfo(
                    port=cluster.port,
                    password="not-the-password",
                    sslmode="verify-full",
                    root_certificate=cluster.root_certificate,
                )
            ).inspect_receipt(idempotency_key="4" * 64)

    assert encrypted == (True,)
    assert denied.value.classification == "authorization_denied"


@pytest.mark.parametrize("sslmode", ("prefer", "require"))
def test_tls_modes_the_probe_cannot_mirror_keep_the_transient_classification(
    sslmode: str, tmp_path: Path
) -> None:
    with (
        _tls_postgresql(tmp_path) as cluster,
        pytest.raises(PostgreSQLLandStoreError) as rejected,
    ):
        _land_store(
            _conninfo(
                port=cluster.port,
                password="not-the-password",
                sslmode=sslmode,
                root_certificate=cluster.root_certificate,
            )
        ).inspect_receipt(idempotency_key="4" * 64)

    # The probe declines a server that accepts TLS under these modes rather than sending the
    # credential over a channel it has not verified, so the rejection stays retryable.
    assert rejected.value.classification == "transient_transport"
