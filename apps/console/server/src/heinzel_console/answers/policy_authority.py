"""A signed entitlement authority a local console can stand up for itself.

The governed answer resolves the requester's entitlement from a connected policy authority,
over HTTPS, with a bearer credential, and verifies the Ed25519 signature on what comes back.
A deployment points `SignedHttpConnectedPolicyAuthority` at the enterprise's own authority. A
local demonstration has none, so this is one it can run: the signing half of that exchange,
and a loopback HTTPS server in front of it.

What this stands in for is the authority, not the protocol. The reader is the shipped
`SignedHttpConnectedPolicyAuthority`, the transport is real TLS on loopback, the bearer
credential is checked, and the signature is verified against the published key -- so an
entitlement this does not sign cannot be resolved. What it does not stand in for is a source
of truth about who is entitled to what: that is published into it, by the caller.

`LocalDevelopmentPolicyAuthority` signs entitlement envelopes, which is access-control's own
domain rather than the console's. It lives here because the console is its one consumer; a
second would make the case for promoting it beside its verifier in
`heinzel_access_control`. It lived under `tests/acceptance/` until the quickstart image needed
it, and the image ships `apps`, `packages`, `providers` and `services` but not `tests`.
"""

from __future__ import annotations

import ipaddress
import secrets
import sqlite3
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from heinzel_access_control import (
    EntitlementLookupRequest,
    SignedEntitlementBody,
    SignedEntitlementEnvelope,
    SignedHttpConnectedPolicyAuthority,
    SignedHttpPolicyAuthoritySettings,
)
from heinzel_contract_model import canonical_bytes
from pydantic import SecretStr, ValidationError

__all__ = [
    "LOCAL_CONNECTED_AUTHORITY_REF",
    "LOCAL_SIGNING_KEY_REF",
    "LocalDevelopmentPolicyAuthority",
    "LocalSignedPolicyAuthority",
    "create_local_policy_server",
    "local_signed_policy_authority",
]

# The authority the resolver records as the source of what it resolved. Exported because the
# runtime configuration must name the same one: an entitlement resolved from this authority and
# attributed to another would be evidence pointing at something that never answered.
LOCAL_CONNECTED_AUTHORITY_REF = "local-policy"
LOCAL_SIGNING_KEY_REF = "local-policy-key"

_SIGNATURE_DOMAIN = "heinzel-enterprise-entitlement-v1"
_MAXIMUM_REQUEST_BYTES = 64 * 1024
_CONNECTION_BINDING_REF = "local-policy-binding"
_ADAPTER_REF = "signed-http-local"
_TIMEOUT_SECONDS = 2.0
# Long enough that a demonstration left running for a day does not meet an expired
# certificate, short enough that this is plainly not a deployment credential.
_CERTIFICATE_VALIDITY = timedelta(days=2)


class LocalDevelopmentPolicyAuthority:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        key_ref: str,
        signing_key: Ed25519PrivateKey,
    ) -> None:
        if not key_ref:
            raise ValueError("policy signing key reference must not be empty")
        self._connection = connection
        self._key_ref = key_ref
        self._signing_key = signing_key
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS local_policy_entitlements ("
            "tenant_id TEXT NOT NULL, principal_ref TEXT NOT NULL, "
            "purpose_digest TEXT NOT NULL, source_revision INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, principal_ref, purpose_digest))"
        )

    def publish(self, body: SignedEntitlementBody) -> None:
        body = SignedEntitlementBody.model_validate(body.model_dump(mode="python"), strict=True)
        identity = (body.tenant_id, body.principal_ref, body.purpose_digest)
        payload = canonical_bytes(body)
        current = self._connection.execute(
            "SELECT source_revision, payload FROM local_policy_entitlements "
            "WHERE tenant_id = ? AND principal_ref = ? AND purpose_digest = ?",
            identity,
        ).fetchone()
        if current is not None:
            if current == (body.source_revision, payload):
                return
            if not isinstance(current[0], int) or body.source_revision <= current[0]:
                raise ValueError("policy authority revision must advance monotonically")
        with self._connection:
            self._connection.execute(
                "INSERT INTO local_policy_entitlements "
                "(tenant_id, principal_ref, purpose_digest, source_revision, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id, principal_ref, purpose_digest) "
                "DO UPDATE SET source_revision = excluded.source_revision, "
                "payload = excluded.payload",
                (*identity, body.source_revision, payload),
            )

    def read(self, request: EntitlementLookupRequest) -> SignedEntitlementEnvelope | None:
        row = self._connection.execute(
            "SELECT tenant_id, principal_ref, purpose_digest, source_revision, payload "
            "FROM local_policy_entitlements WHERE tenant_id = ? AND principal_ref = ? "
            "AND purpose_digest = ?",
            (request.tenant_id, request.principal_ref, request.purpose_digest),
        ).fetchone()
        if row is None:
            return None
        body = SignedEntitlementBody.model_validate_json(row[4], strict=True)
        if (
            row[0] != body.tenant_id
            or row[1] != body.principal_ref
            or row[2] != body.purpose_digest
            or row[3] != body.source_revision
        ):
            raise ValueError("policy authority index does not match its payload")
        signature = self._signing_key.sign(
            canonical_bytes({"domain": _SIGNATURE_DOMAIN, "body": body})
        ).hex()
        return SignedEntitlementEnvelope(key_ref=self._key_ref, body=body, signature=signature)


def create_local_policy_server(
    *,
    database_path: Path,
    signing_key: Ed25519PrivateKey,
    key_ref: str,
    bearer_credential: SecretStr,
    tls_context: ssl.SSLContext,
) -> ThreadingHTTPServer:
    expected_bearer = "Bearer " + bearer_credential.get_secret_value()

    class _PolicyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/entitlements/current":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not secrets.compare_digest(self.headers.get("Authorization", ""), expected_bearer):
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if content_length < 1 or content_length > _MAXIMUM_REQUEST_BYTES:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                request = EntitlementLookupRequest.model_validate_json(
                    self.rfile.read(content_length), strict=True
                )
                with sqlite3.connect(database_path) as connection:
                    envelope = LocalDevelopmentPolicyAuthority(
                        connection,
                        key_ref=key_ref,
                        signing_key=signing_key,
                    ).read(request)
            except (sqlite3.Error, ValidationError, ValueError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if envelope is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = canonical_bytes(envelope)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PolicyHandler)
    server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    return server


@dataclass(frozen=True, slots=True)
class LocalSignedPolicyAuthority:
    """The running local authority: what resolves entitlements, and what publishes them."""

    reader: SignedHttpConnectedPolicyAuthority
    database_path: Path
    signing_key: Ed25519PrivateKey = field(repr=False)

    def publish(self, body: SignedEntitlementBody) -> None:
        """Record one entitlement, which this authority will then sign on request."""
        with sqlite3.connect(self.database_path) as connection:
            LocalDevelopmentPolicyAuthority(
                connection,
                key_ref=LOCAL_SIGNING_KEY_REF,
                signing_key=self.signing_key,
            ).publish(body)


def _loopback_tls_material(root: Path) -> tuple[Path, ssl.SSLContext]:
    """A self-signed certificate for 127.0.0.1, and a server context using it.

    Generated rather than shipped: a certificate committed to a repository is a private key
    committed to a repository. It is valid for loopback alone, so it cannot authenticate
    anything a browser or another host would reach.
    """
    tls_key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local policy authority")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + _CERTIFICATE_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(tls_key, algorithm=None)
    )
    certificate_path = root / "policy-tls.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = root / "policy-tls-key.pem"
    # Created empty with the mode first: writing then chmod would leave the key readable for
    # as long as it takes to get to the second call.
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate_path, key_path)
    return certificate_path, tls_context


@contextmanager
def local_signed_policy_authority(
    root: Path, *, body: SignedEntitlementBody
) -> Iterator[LocalSignedPolicyAuthority]:
    """Run a signed entitlement authority on loopback for the length of this context.

    The bearer credential is generated per run and never written down, so the authority
    answers this process and nothing else that finds the port.
    """
    root.mkdir(parents=True, exist_ok=True)
    certificate_path, tls_context = _loopback_tls_material(root)
    signing_key = Ed25519PrivateKey.generate()
    database_path = root / "policy-authority.sqlite3"
    # Started empty, like every other part of this authority. The signing key above is generated
    # per process, so an entitlement a previous run recorded is one nothing here can verify; and
    # the store refuses a body that does not supersede what it holds, which a second run's
    # otherwise identical entitlement does not, because its timestamps moved while its revision
    # did not. A console restarting over its state directory met exactly that and refused to
    # start. Nothing is lost: what this authority asserts is published into it on every run.
    database_path.unlink(missing_ok=True)
    with sqlite3.connect(database_path) as connection:
        LocalDevelopmentPolicyAuthority(
            connection,
            key_ref=LOCAL_SIGNING_KEY_REF,
            signing_key=signing_key,
        ).publish(body)
    bearer = SecretStr(secrets.token_urlsafe(32))
    server = create_local_policy_server(
        database_path=database_path,
        signing_key=signing_key,
        key_ref=LOCAL_SIGNING_KEY_REF,
        bearer_credential=bearer,
        tls_context=tls_context,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = SignedHttpPolicyAuthoritySettings.model_validate(
        {
            "endpoint": f"https://127.0.0.1:{server.server_port}/entitlements/current",
            "tls_ca_bundle_path": certificate_path,
            "bearer_credential": bearer,
            "signing_key_ref": LOCAL_SIGNING_KEY_REF,
            "signing_public_key_pem": signing_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "connected_authority_ref": LOCAL_CONNECTED_AUTHORITY_REF,
            "connection_binding_ref": _CONNECTION_BINDING_REF,
            "adapter_ref": _ADAPTER_REF,
            "timeout_seconds": _TIMEOUT_SECONDS,
        }
    )
    with httpx.Client(verify=ssl.create_default_context(cafile=str(certificate_path))) as client:
        try:
            yield LocalSignedPolicyAuthority(
                reader=SignedHttpConnectedPolicyAuthority(settings=settings, http_client=client),
                database_path=database_path,
                signing_key=signing_key,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("the local policy authority did not stop")
