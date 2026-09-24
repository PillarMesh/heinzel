from __future__ import annotations

import secrets
import sqlite3
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_access_control import (
    EntitlementLookupRequest,
    SignedEntitlementBody,
    SignedEntitlementEnvelope,
)
from heinzel_contract_model import canonical_bytes
from pydantic import SecretStr, ValidationError

_SIGNATURE_DOMAIN = "heinzel-enterprise-entitlement-v1"
_MAXIMUM_REQUEST_BYTES = 64 * 1024


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
