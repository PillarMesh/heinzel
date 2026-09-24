from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_access_control import (
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
    EntitlementLookupRequest,
    SignedEntitlementBody,
    SignedEntitlementEnvelope,
    SignedHttpConnectedPolicyAuthority,
    SignedHttpPolicyAuthoritySettings,
)
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from pydantic import ValidationError

NOW = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)
DOMAIN = "heinzel-enterprise-entitlement-v1"


def _reference(identifier: str, character: str) -> dict[str, object]:
    return {"artifact_id": identifier, "version": 1, "digest": character * 64}


def _body_payload(*, decision: str = "active") -> dict[str, object]:
    active = decision == "active"
    payload: dict[str, object] = {
        "schema_version": "1",
        "tenant_id": "tenant-a",
        "principal_ref": "principal:requester-a",
        "purpose_digest": "1" * 64,
        "decision": decision,
        "product_version_refs": [_reference("product:orders", "2")] if active else [],
        "semantic_refs": [_reference("metric:revenue", "3")] if active else [],
        "filter_domains": (
            [{"dimension_ref": _reference("dimension:region", "4"), "values": ["ca", "us"]}]
            if active
            else []
        ),
        "permissions": ["query", "view"] if active else [],
        "effective_at": NOW.isoformat().replace("+00:00", "Z"),
        "valid_until": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "source_revision": 7,
    }
    payload["source_payload_digest"] = SignedEntitlementBody.compute_source_payload_digest(payload)
    return payload


def _signed_response(
    private_key: Ed25519PrivateKey,
    *,
    body_payload: dict[str, object] | None = None,
    key_ref: str = "key:policy-a:1",
) -> dict[str, object]:
    body = SignedEntitlementBody.model_validate(body_payload or _body_payload())
    signature = private_key.sign(canonical_bytes({"domain": DOMAIN, "body": body})).hex()
    return {
        "schema_version": "1",
        "key_ref": key_ref,
        "body": body.model_dump(mode="json"),
        "signature": signature,
    }


def _settings(private_key: Ed25519PrivateKey) -> SignedHttpPolicyAuthoritySettings:
    encoded_key = base64.b64encode(
        b"0*0\x05\x06\x03+ep\x03!\x00" + private_key.public_key().public_bytes_raw()
    ).decode("ascii")
    public_key_pem = f"-----BEGIN PUBLIC KEY-----\n{encoded_key}\n-----END PUBLIC KEY-----\n"
    return SignedHttpPolicyAuthoritySettings.model_validate(
        {
            "endpoint": "https://policy.example.test/entitlements/current",
            "tls_ca_bundle_path": Path("/etc/heinzel/policy-ca.pem"),
            "bearer_credential": "read-only-canary",
            "signing_key_ref": "key:policy-a:1",
            "signing_public_key_pem": public_key_pem,
            "connected_authority_ref": "policy-authority:tenant-a",
            "connection_binding_ref": "connection:policy-a",
            "adapter_ref": "signed-http-policy:v1",
            "timeout_seconds": 3.0,
        }
    )


def _adapter(
    handler: httpx.MockTransport,
    private_key: Ed25519PrivateKey,
) -> SignedHttpConnectedPolicyAuthority:
    return SignedHttpConnectedPolicyAuthority(
        settings=_settings(private_key),
        http_client=httpx.Client(transport=handler),
    )


def test_signed_http_authority_posts_exact_lookup_and_derives_provenance() -> None:
    private_key = Ed25519PrivateKey.generate()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://policy.example.test/entitlements/current"
        assert request.headers["Authorization"] == "Bearer read-only-canary"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Content-Type"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 3.0,
            "read": 3.0,
            "write": 3.0,
            "pool": 3.0,
        }
        assert EntitlementLookupRequest.model_validate_json(request.content, strict=True) == (
            EntitlementLookupRequest(
                tenant_id="tenant-a",
                principal_ref="principal:requester-a",
                purpose_digest="1" * 64,
            )
        )
        return httpx.Response(200, json=_signed_response(private_key))

    assertion = _adapter(httpx.MockTransport(respond), private_key).read_current(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        purpose_digest="1" * 64,
    )

    assert assertion is not None
    assert assertion.decision == "active"
    assert assertion.semantic_refs == (
        ArtifactReference(artifact_id="metric:revenue", version=1, digest="3" * 64),
    )
    assert assertion.provenance.connected_authority_ref == "policy-authority:tenant-a"
    assert assertion.provenance.connection_binding_ref == "connection:policy-a"
    assert assertion.provenance.source_revision == 7
    assert assertion.provenance.source_payload_digest == _body_payload()["source_payload_digest"]
    assert assertion.provenance.authentication_method == "signed_response"
    assert assertion.provenance.authentication_key_ref == "key:policy-a:1"
    assert assertion.provenance.authentication_evidence_digest == digest(
        {
            "domain": DOMAIN,
            "key_ref": "key:policy-a:1",
            "signature": _signed_response(private_key)["signature"],
        }
    )


def test_source_payload_digest_excludes_itself_and_rejects_mismatch() -> None:
    payload = _body_payload()
    body = SignedEntitlementBody.model_validate(payload)

    assert body.source_payload_digest == SignedEntitlementBody.compute_source_payload_digest(body)

    payload["source_payload_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="source_payload_digest"):
        SignedEntitlementBody.model_validate(payload)


def test_wire_models_reject_unknown_fields() -> None:
    private_key = Ed25519PrivateKey.generate()
    response = _signed_response(private_key)
    response["authentication_method"] = "signed_response"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SignedEntitlementEnvelope.model_validate(response)


@pytest.mark.parametrize("failure", ["unknown_key", "bad_signature", "wrong_scope"])
def test_untrusted_signed_response_is_rejected_as_integrity_failure(failure: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    response = _signed_response(private_key)
    if failure == "unknown_key":
        response["key_ref"] = "key:policy-a:unknown"
    elif failure == "bad_signature":
        response["signature"] = "0" * 128
    else:
        payload = _body_payload()
        payload["tenant_id"] = "tenant-b"
        payload["source_payload_digest"] = SignedEntitlementBody.compute_source_payload_digest(
            payload
        )
        response = _signed_response(private_key, body_payload=payload)

    adapter = _adapter(
        httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        private_key,
    )

    with pytest.raises(ConnectedPolicyAuthorityIntegrityError):
        adapter.read_current(
            tenant_id="tenant-a",
            principal_ref="principal:requester-a",
            purpose_digest="1" * 64,
        )


def test_revoked_signed_decision_is_returned_for_resolver_to_record() -> None:
    private_key = Ed25519PrivateKey.generate()
    response = _signed_response(private_key, body_payload=_body_payload(decision="revoked"))
    adapter = _adapter(
        httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        private_key,
    )

    assertion = adapter.read_current(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        purpose_digest="1" * 64,
    )

    assert assertion is not None
    assert assertion.decision == "revoked"
    assert assertion.permissions == ()


@pytest.mark.parametrize("status_code", [300, 401, 429, 503])
def test_http_failure_is_classified_as_authority_unavailable(status_code: int) -> None:
    private_key = Ed25519PrivateKey.generate()
    adapter = _adapter(
        httpx.MockTransport(lambda _: httpx.Response(status_code, text="untrusted detail")),
        private_key,
    )

    with pytest.raises(ConnectedPolicyAuthorityUnavailable, match="connected policy authority"):
        adapter.read_current(
            tenant_id="tenant-a",
            principal_ref="principal:requester-a",
            purpose_digest="1" * 64,
        )


def test_transport_timeout_is_classified_as_authority_unavailable() -> None:
    private_key = Ed25519PrivateKey.generate()

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = _adapter(httpx.MockTransport(timeout), private_key)

    with pytest.raises(ConnectedPolicyAuthorityUnavailable):
        adapter.read_current(
            tenant_id="tenant-a",
            principal_ref="principal:requester-a",
            purpose_digest="1" * 64,
        )


def test_not_found_is_missing_authority() -> None:
    private_key = Ed25519PrivateKey.generate()
    adapter = _adapter(
        httpx.MockTransport(lambda _: httpx.Response(404)),
        private_key,
    )

    assert (
        adapter.read_current(
            tenant_id="tenant-a",
            principal_ref="principal:requester-a",
            purpose_digest="1" * 64,
        )
        is None
    )


@pytest.mark.parametrize(
    "settings_update",
    [
        {"endpoint": "http://policy.example.test/entitlements/current"},
        {"endpoint": "https://policy.example.test/a-different-path"},
    ],
)
def test_settings_reject_untrusted_or_wrong_endpoint(
    settings_update: dict[str, object],
) -> None:
    settings = _settings(Ed25519PrivateKey.generate())

    with pytest.raises(ValidationError):
        SignedHttpPolicyAuthoritySettings.model_validate(
            {**settings.model_dump(), **settings_update}
        )
