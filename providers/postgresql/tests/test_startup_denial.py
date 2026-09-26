from __future__ import annotations

import socket
import struct

import psycopg
import pytest
from heinzel_provider_postgresql import warehouse_protocol
from heinzel_provider_postgresql.startup_denial import recover_startup_denial

_SSL_REQUEST = struct.pack("!II", 8, 80877103)
_DSN = (
    "host=db.internal port=6543 dbname=source user=acquisition_runtime "
    "password=private-credential sslmode=disable gssencmode=disable connect_timeout=4"
)


class _ProbeRecorder:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def __call__(self, **parameters: object) -> _ProbeRecorder:
        self.calls.append(parameters)
        if self.outcome is not None:
            raise self.outcome
        return self

    def close(self) -> None:
        self.closed = True


def _startup_rejection(sqlstate: str) -> bytes:
    payload = f"SFATAL\0VFATAL\0C{sqlstate}\0Mlocalized detail\0\0".encode("ascii")
    return b"E" + struct.pack("!I", len(payload) + 4) + payload


@pytest.fixture(autouse=True)
def _without_libpq_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGSSLMODE",
        "PGGSSENCMODE",
        "PGSERVICE",
        "PGREQUIREAUTH",
        "PGCHANNELBINDING",
        "PGSSLNEGOTIATION",
        "PGCONNECT_TIMEOUT",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.parametrize(
    "denial",
    (psycopg.errors.InvalidPassword(), psycopg.errors.InvalidAuthorizationSpecification()),
)
def test_sqlstate_less_startup_failure_recovers_the_structured_authorization_denial(
    denial: psycopg.Error,
) -> None:
    probe = _ProbeRecorder(denial)

    recovered = recover_startup_denial(psycopg.OperationalError(), _DSN, probe=probe)

    assert recovered is denial
    assert probe.calls == [
        {
            "dbname": "source",
            "user": "acquisition_runtime",
            "password": "private-credential",
            "host": "db.internal",
            "port": 6543,
            "sslmode": "disable",
            "connect_timeout": 4.0,
            "sslrootcert": None,
            "sslcert": None,
            "sslkey": None,
        }
    ]


@pytest.mark.parametrize(
    "probe_failure",
    (
        psycopg.OperationalError("PostgreSQL denial probe transport failed"),
        psycopg.errors.TooManyConnections(),
        psycopg.errors.CannotConnectNow(),
        psycopg.errors.InvalidCatalogName(),
        ValueError("PostgreSQL denial probe TLS material is incomplete"),
    ),
)
def test_anything_but_an_authorization_rejection_keeps_the_original_failure(
    probe_failure: Exception,
) -> None:
    original = psycopg.OperationalError()

    recovered = recover_startup_denial(original, _DSN, probe=_ProbeRecorder(probe_failure))

    assert recovered is original


def test_a_probe_that_is_admitted_is_closed_and_keeps_the_original_failure() -> None:
    original = psycopg.OperationalError()
    probe = _ProbeRecorder()

    recovered = recover_startup_denial(original, _DSN, probe=probe)

    assert recovered is original
    assert probe.closed


def test_a_failure_that_already_carries_a_sqlstate_is_never_probed() -> None:
    original = psycopg.errors.AdminShutdown()
    probe = _ProbeRecorder(psycopg.errors.InvalidPassword())

    recovered = recover_startup_denial(original, _DSN, probe=probe)

    assert recovered is original
    assert probe.calls == []


def test_dbname_and_timeout_follow_libpq_defaults_when_omitted() -> None:
    probe = _ProbeRecorder(psycopg.errors.InvalidPassword())

    recover_startup_denial(
        psycopg.OperationalError(),
        "postgresql://reader:secret@127.0.0.1/?sslmode=prefer&gssencmode=disable",
        probe=probe,
    )

    assert probe.calls[0]["dbname"] == "reader"
    assert probe.calls[0]["port"] == 5432
    assert probe.calls[0]["sslmode"] == "prefer"
    assert probe.calls[0]["connect_timeout"] == 10.0


@pytest.mark.parametrize(
    "conninfo",
    (
        "host=/var/run/postgresql user=u password=p sslmode=disable gssencmode=disable",
        "host=a,b user=u password=p sslmode=disable gssencmode=disable",
        "host=h hostaddr=10.0.0.1 user=u password=p sslmode=disable gssencmode=disable",
        "user=u password=p sslmode=disable gssencmode=disable",
        "host=h user=u sslmode=disable gssencmode=disable",
        "host=h user=u password=p sslmode=require gssencmode=disable",
        "host=h user=u password=p sslmode=verify-ca gssencmode=disable",
        "host=h user=u password=p sslmode=verify-full gssencmode=disable",
        "host=h user=u password=p sslmode=disable gssencmode=prefer",
        "host=h user=u password=p sslmode=disable gssencmode=disable require_auth=md5",
        "host=h user=u password=p sslmode=disable gssencmode=disable channel_binding=require",
        "host=h user=u password=p sslmode=disable gssencmode=disable sslnegotiation=direct",
        "service=warehouse host=h user=u password=p sslmode=disable gssencmode=disable",
        "host=h port=5432,5433 user=u password=p sslmode=disable gssencmode=disable",
        "not a conninfo",
    ),
)
def test_connections_the_probe_cannot_mirror_exactly_are_never_probed(conninfo: str) -> None:
    original = psycopg.OperationalError()
    probe = _ProbeRecorder(psycopg.errors.InvalidPassword())

    recovered = recover_startup_denial(original, conninfo, probe=probe)

    assert recovered is original
    assert probe.calls == []


def test_verify_full_is_probed_with_the_configured_trust_material() -> None:
    probe = _ProbeRecorder(psycopg.errors.InvalidPassword())

    recover_startup_denial(
        psycopg.OperationalError(),
        "host=h user=u password=p sslmode=verify-full gssencmode=disable sslrootcert=/ca.crt",
        probe=probe,
    )

    assert probe.calls[0]["sslmode"] == "verify-full"
    assert probe.calls[0]["sslrootcert"] == "/ca.crt"
    assert probe.calls[0]["sslcert"] is None


def test_prefer_continues_in_plaintext_only_when_the_server_declines_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, server = socket.socketpair()
    server.sendall(b"N" + _startup_rejection("28P01"))
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: client,
    )

    try:
        with pytest.raises(psycopg.errors.InvalidPassword):
            warehouse_protocol.connect_denial_probe(
                dbname="source",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=5432,
                sslmode="prefer",
                connect_timeout=5,
            )
        assert server.recv(len(_SSL_REQUEST)) == _SSL_REQUEST
    finally:
        server.close()


def test_prefer_never_sends_the_credential_over_unverified_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, server = socket.socketpair()
    server.sendall(b"S")
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: client,
    )

    try:
        with pytest.raises(psycopg.OperationalError) as captured:
            warehouse_protocol.connect_denial_probe(
                dbname="source",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=5432,
                sslmode="prefer",
                connect_timeout=5,
            )
        server.settimeout(1)
        received = server.recv(4096)
    finally:
        server.close()

    assert type(captured.value) is psycopg.OperationalError
    assert received == _SSL_REQUEST


@pytest.mark.parametrize(
    ("client_certificate", "client_private_key"),
    (("/client.crt", None), (None, "/client.key")),
)
def test_verify_full_rejects_half_configured_client_certificate_material(
    client_certificate: str | None,
    client_private_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, server = socket.socketpair()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: client,
    )

    try:
        with pytest.raises(ValueError, match="TLS material is incomplete"):
            warehouse_protocol.connect_denial_probe(
                dbname="source",
                user="probe",
                password="credential",
                host="127.0.0.1",
                port=5432,
                sslmode="verify-full",
                sslrootcert="/ca.crt",
                sslcert=client_certificate,
                sslkey=client_private_key,
                connect_timeout=5,
            )
    finally:
        client.close()
        server.close()
