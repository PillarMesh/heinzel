from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import socket
import ssl
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import psycopg

_PROTOCOL_VERSION = 196608
_SSL_REQUEST_CODE = 80877103
_MAXIMUM_MESSAGE_BYTES = 1024 * 1024
_MAXIMUM_SCRAM_ITERATIONS = 1_000_000
_INTEGER = struct.Struct("!I")
_SSL_REQUEST = struct.Struct("!II")


class _StartupProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class _StartupConnection:
    stream: socket.socket | ssl.SSLSocket

    def close(self) -> None:
        with suppress(OSError):
            self.stream.close()


@dataclass(slots=True)
class _ScramExchange:
    client_first_bare: str
    client_nonce: str
    password: bytes
    expected_server_signature: bytes | None = None
    server_final_verified: bool = False


def connect_denial_probe(
    *,
    dbname: str,
    user: str,
    password: str,
    host: str,
    port: int,
    sslmode: str,
    connect_timeout: float,
    sslrootcert: str | None = None,
    sslcert: str | None = None,
    sslkey: str | None = None,
    entropy: Callable[[int], bytes] = os.urandom,
) -> _StartupConnection:
    """Open only enough PostgreSQL protocol to retain a structured startup SQLSTATE."""
    if sslmode not in {"disable", "verify-full"}:
        raise ValueError("PostgreSQL denial probe received an unsupported TLS mode")
    if not dbname or not user or connect_timeout <= 0:
        raise ValueError("PostgreSQL denial probe connection parameters are invalid")

    stream: socket.socket | ssl.SSLSocket | None = None
    try:
        stream = socket.create_connection((host, port), timeout=connect_timeout)
        stream.settimeout(connect_timeout)
        if sslmode == "verify-full":
            if sslrootcert is None or sslcert is None or sslkey is None:
                raise ValueError("PostgreSQL denial probe TLS material is incomplete")
            stream = _upgrade_to_tls(
                stream,
                host=host,
                root_certificate=sslrootcert,
                client_certificate=sslcert,
                client_private_key=sslkey,
            )
            stream.settimeout(connect_timeout)
        _send_startup(stream, database_name=dbname, user=user)
        _complete_startup(
            stream,
            user=user,
            password=password,
            entropy=entropy,
        )
        return _StartupConnection(stream)
    except psycopg.Error:
        if stream is not None:
            with suppress(OSError):
                stream.close()
        raise
    except (OSError, UnicodeError, binascii.Error, struct.error, _StartupProtocolError):
        if stream is not None:
            with suppress(OSError):
                stream.close()
        raise psycopg.OperationalError("PostgreSQL denial probe transport failed") from None


def _upgrade_to_tls(
    stream: socket.socket,
    *,
    host: str,
    root_certificate: str,
    client_certificate: str,
    client_private_key: str,
) -> ssl.SSLSocket:
    stream.sendall(_SSL_REQUEST.pack(8, _SSL_REQUEST_CODE))
    if _read_exact(stream, 1) != b"S":
        raise _StartupProtocolError("PostgreSQL server refused TLS negotiation")
    context = ssl.create_default_context(cafile=root_certificate)
    # Match libpq verify-full semantics while retaining CA and hostname verification. Python's
    # extra X509 strict flag rejects otherwise valid private CAs that libpq accepts.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.load_cert_chain(certfile=client_certificate, keyfile=client_private_key)
    return context.wrap_socket(stream, server_hostname=host)


def _send_startup(
    stream: socket.socket | ssl.SSLSocket,
    *,
    database_name: str,
    user: str,
) -> None:
    parameters = (
        b"user\0"
        + user.encode("utf-8")
        + b"\0database\0"
        + database_name.encode("utf-8")
        + b"\0client_encoding\0UTF8\0\0"
    )
    payload = _INTEGER.pack(_PROTOCOL_VERSION) + parameters
    stream.sendall(_INTEGER.pack(len(payload) + _INTEGER.size) + payload)


def _complete_startup(
    stream: socket.socket | ssl.SSLSocket,
    *,
    user: str,
    password: str,
    entropy: Callable[[int], bytes],
) -> None:
    exchange: _ScramExchange | None = None
    authentication_complete = False
    while True:
        message_type, payload = _read_message(stream)
        if message_type == b"E":
            _raise_startup_rejection(payload)
        if message_type == b"R":
            if len(payload) < _INTEGER.size:
                raise _StartupProtocolError("PostgreSQL authentication message was truncated")
            authentication_type = _INTEGER.unpack(payload[: _INTEGER.size])[0]
            authentication_payload = payload[_INTEGER.size :]
            if authentication_type == 10:
                if exchange is not None:
                    raise _StartupProtocolError("PostgreSQL repeated SASL negotiation")
                exchange = _begin_scram(
                    stream,
                    authentication_payload,
                    user=user,
                    password=password,
                    entropy=entropy,
                )
            elif authentication_type == 11:
                if exchange is None or exchange.expected_server_signature is not None:
                    raise _StartupProtocolError("PostgreSQL sent an unexpected SASL challenge")
                _continue_scram(stream, exchange, authentication_payload)
            elif authentication_type == 12:
                if exchange is None or exchange.expected_server_signature is None:
                    raise _StartupProtocolError("PostgreSQL sent an unexpected SASL result")
                _finish_scram(exchange, authentication_payload)
            elif authentication_type == 0:
                if exchange is not None and not exchange.server_final_verified:
                    raise _StartupProtocolError("PostgreSQL omitted SCRAM server verification")
                authentication_complete = True
            else:
                raise _StartupProtocolError("PostgreSQL requested an unsupported authentication")
            continue
        if message_type == b"Z":
            if not authentication_complete:
                raise _StartupProtocolError("PostgreSQL became ready before authentication")
            return
        if message_type not in {b"K", b"N", b"S"}:
            raise _StartupProtocolError("PostgreSQL sent an unexpected startup message")


def _begin_scram(
    stream: socket.socket | ssl.SSLSocket,
    payload: bytes,
    *,
    user: str,
    password: str,
    entropy: Callable[[int], bytes],
) -> _ScramExchange:
    mechanisms = tuple(value for value in payload.rstrip(b"\0").split(b"\0") if value)
    if b"SCRAM-SHA-256" not in mechanisms:
        raise _StartupProtocolError("PostgreSQL did not offer SCRAM-SHA-256")
    client_nonce = base64.b64encode(entropy(18)).decode("ascii").rstrip("=")
    sasl_user = user.replace("=", "=3D").replace(",", "=2C")
    client_first_bare = f"n={sasl_user},r={client_nonce}"
    initial_response = f"n,,{client_first_bare}".encode()
    _send_message(
        stream,
        b"p",
        b"SCRAM-SHA-256\0" + _INTEGER.pack(len(initial_response)) + initial_response,
    )
    return _ScramExchange(
        client_first_bare=client_first_bare,
        client_nonce=client_nonce,
        password=password.encode("utf-8"),
    )


def _continue_scram(
    stream: socket.socket | ssl.SSLSocket,
    exchange: _ScramExchange,
    payload: bytes,
) -> None:
    server_first = payload.decode("ascii")
    attributes = _parse_scram_attributes(server_first)
    server_nonce = attributes.get("r")
    if server_nonce is None or not server_nonce.startswith(exchange.client_nonce):
        raise _StartupProtocolError("PostgreSQL SCRAM nonce was invalid")
    if "m" in attributes:
        raise _StartupProtocolError("PostgreSQL SCRAM extension was unsupported")
    try:
        salt = base64.b64decode(attributes["s"], validate=True)
        iterations = int(attributes["i"])
    except (KeyError, ValueError):
        raise _StartupProtocolError("PostgreSQL SCRAM challenge was invalid") from None
    if not salt or not 0 < iterations <= _MAXIMUM_SCRAM_ITERATIONS:
        raise _StartupProtocolError("PostgreSQL SCRAM challenge exceeded bounds")

    client_final_without_proof = f"c=biws,r={server_nonce}"
    authentication_message = (
        f"{exchange.client_first_bare},{server_first},{client_final_without_proof}"
    ).encode("ascii")
    salted_password = hashlib.pbkdf2_hmac("sha256", exchange.password, salt, iterations)
    client_key = hmac.digest(salted_password, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    client_signature = hmac.digest(stored_key, authentication_message, "sha256")
    client_proof = bytes(
        client_value ^ signature_value
        for client_value, signature_value in zip(client_key, client_signature, strict=True)
    )
    server_key = hmac.digest(salted_password, b"Server Key", "sha256")
    exchange.expected_server_signature = hmac.digest(
        server_key,
        authentication_message,
        "sha256",
    )
    final_response = (
        f"{client_final_without_proof},p={base64.b64encode(client_proof).decode('ascii')}"
    ).encode("ascii")
    _send_message(stream, b"p", final_response)


def _finish_scram(exchange: _ScramExchange, payload: bytes) -> None:
    attributes = _parse_scram_attributes(payload.decode("ascii"))
    if "e" in attributes or set(attributes) != {"v"}:
        raise _StartupProtocolError("PostgreSQL SCRAM authentication failed")
    try:
        server_signature = base64.b64decode(attributes["v"], validate=True)
    except binascii.Error:
        raise _StartupProtocolError("PostgreSQL SCRAM verifier was invalid") from None
    if exchange.expected_server_signature is None or not hmac.compare_digest(
        exchange.expected_server_signature,
        server_signature,
    ):
        raise _StartupProtocolError("PostgreSQL SCRAM server verification failed")
    exchange.server_final_verified = True


def _parse_scram_attributes(value: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in value.split(","):
        if len(item) < 2 or item[1] != "=" or item[0] in attributes:
            raise _StartupProtocolError("PostgreSQL SCRAM attributes were invalid")
        attributes[item[0]] = item[2:]
    return attributes


def _raise_startup_rejection(payload: bytes) -> None:
    fields: dict[bytes, bytes] = {}
    offset = 0
    while offset < len(payload):
        field_type = payload[offset : offset + 1]
        offset += 1
        if field_type == b"\0":
            if offset != len(payload):
                raise _StartupProtocolError("PostgreSQL error fields had trailing data")
            break
        terminator = payload.find(b"\0", offset)
        if terminator < 0 or field_type in fields:
            raise _StartupProtocolError("PostgreSQL error fields were invalid")
        fields[field_type] = payload[offset:terminator]
        offset = terminator + 1
    else:
        raise _StartupProtocolError("PostgreSQL error fields were unterminated")
    try:
        sqlstate = fields[b"C"].decode("ascii")
        error_type = psycopg.errors.lookup(sqlstate)
    except (KeyError, UnicodeError):
        raise _StartupProtocolError("PostgreSQL rejection omitted a valid SQLSTATE") from None
    raise error_type("PostgreSQL startup rejected the connection")


def _read_message(stream: socket.socket | ssl.SSLSocket) -> tuple[bytes, bytes]:
    message_type = _read_exact(stream, 1)
    message_size = _INTEGER.unpack(_read_exact(stream, _INTEGER.size))[0]
    if message_size < _INTEGER.size or message_size > _MAXIMUM_MESSAGE_BYTES:
        raise _StartupProtocolError("PostgreSQL startup message exceeded bounds")
    return message_type, _read_exact(stream, message_size - _INTEGER.size)


def _send_message(
    stream: socket.socket | ssl.SSLSocket,
    message_type: bytes,
    payload: bytes,
) -> None:
    stream.sendall(message_type + _INTEGER.pack(len(payload) + _INTEGER.size) + payload)


def _read_exact(stream: socket.socket | ssl.SSLSocket, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = stream.recv(size - len(value))
        if not chunk:
            raise _StartupProtocolError("PostgreSQL startup connection ended early")
        value.extend(chunk)
    return bytes(value)
