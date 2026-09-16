from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator

from .models import (
    ConnectedAuthorityProvenance,
    EnterpriseEntitlementAssertion,
    _EnterpriseEntitlementScope,
)
from .resolver import (
    ConnectedPolicyAuthorityIntegrityError,
    ConnectedPolicyAuthorityUnavailable,
)
from .signature import _verify_ed25519_signature

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_SIGNATURE_DOMAIN = "pillarmesh-enterprise-entitlement-v1"


class EntitlementLookupRequest(ArtifactModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    principal_ref: str = Field(min_length=1)
    purpose_digest: str = Field(pattern=_DIGEST_PATTERN)


class _SignedEntitlementClaims(_EnterpriseEntitlementScope):
    source_revision: int = Field(gt=0)


class SignedEntitlementBody(_SignedEntitlementClaims):
    source_payload_digest: str = Field(pattern=_DIGEST_PATTERN)

    @classmethod
    def compute_source_payload_digest(cls, claims: object) -> str:
        if isinstance(claims, SignedEntitlementBody):
            claims = claims.model_dump(exclude={"source_payload_digest"})
        elif isinstance(claims, Mapping):
            claims = {key: value for key, value in claims.items() if key != "source_payload_digest"}
        return digest(_SignedEntitlementClaims.model_validate(claims))

    @model_validator(mode="after")
    def payload_digest_is_valid(self) -> Self:
        expected = self.compute_source_payload_digest(self)
        if self.source_payload_digest != expected:
            raise ValueError("source_payload_digest does not match canonical body")
        return self


class SignedEntitlementEnvelope(ArtifactModel):
    schema_version: Literal["1"] = "1"
    key_ref: str = Field(min_length=1)
    body: SignedEntitlementBody
    signature: str = Field(pattern=_SIGNATURE_PATTERN)


class SignedHttpPolicyAuthoritySettings(ArtifactModel):
    endpoint: AnyHttpUrl
    tls_ca_bundle_path: Path
    bearer_credential: SecretStr
    signing_key_ref: str = Field(min_length=1)
    signing_public_key_pem: str = Field(min_length=1)
    connected_authority_ref: str = Field(min_length=1)
    connection_binding_ref: str = Field(min_length=1)
    adapter_ref: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def endpoint_is_exact_https_lookup(self) -> Self:
        if self.endpoint.scheme != "https":
            raise ValueError("connected policy endpoint must use HTTPS")
        if self.endpoint.path != "/entitlements/current":
            raise ValueError("connected policy endpoint path must be /entitlements/current")
        if self.endpoint.query is not None or self.endpoint.fragment is not None:
            raise ValueError("connected policy endpoint must not contain query or fragment")
        if not self.bearer_credential.get_secret_value():
            raise ValueError("bearer credential must not be empty")
        return self


class SignedHttpConnectedPolicyAuthority:
    def __init__(
        self,
        *,
        settings: SignedHttpPolicyAuthoritySettings,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._public_key = _load_public_key(settings.signing_public_key_pem)
        self._http_client = http_client or httpx.Client(
            verify=str(settings.tls_ca_bundle_path),
            timeout=settings.timeout_seconds,
        )

    def read_current(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        purpose_digest: str,
    ) -> EnterpriseEntitlementAssertion | None:
        request = EntitlementLookupRequest(
            tenant_id=tenant_id,
            principal_ref=principal_ref,
            purpose_digest=purpose_digest,
        )
        try:
            response = self._http_client.post(
                str(self._settings.endpoint),
                content=canonical_bytes(request),
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer "
                    + self._settings.bearer_credential.get_secret_value(),
                    "Content-Type": "application/json",
                },
                timeout=self._settings.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise ConnectedPolicyAuthorityUnavailable(
                "connected policy authority request failed"
            ) from error
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ConnectedPolicyAuthorityUnavailable(
                f"connected policy authority returned HTTP {response.status_code}"
            )
        envelope = self._validated_envelope(response.content)
        body = envelope.body
        if envelope.key_ref != self._settings.signing_key_ref:
            raise ConnectedPolicyAuthorityIntegrityError(
                "unrecognized connected policy signing key"
            )
        if not _verify_ed25519_signature(
            self._public_key,
            signature_hex=envelope.signature,
            payload=canonical_bytes({"domain": _SIGNATURE_DOMAIN, "body": body}),
        ):
            raise ConnectedPolicyAuthorityIntegrityError(
                "connected policy signature verification failed"
            )
        if (
            body.tenant_id != tenant_id
            or body.principal_ref != principal_ref
            or body.purpose_digest != purpose_digest
        ):
            raise ConnectedPolicyAuthorityIntegrityError(
                "connected policy response scope does not match lookup"
            )
        return EnterpriseEntitlementAssertion(
            tenant_id=body.tenant_id,
            principal_ref=body.principal_ref,
            purpose_digest=body.purpose_digest,
            decision=body.decision,
            product_version_refs=body.product_version_refs,
            semantic_refs=body.semantic_refs,
            filter_domains=body.filter_domains,
            permissions=body.permissions,
            effective_at=body.effective_at,
            valid_until=body.valid_until,
            provenance=ConnectedAuthorityProvenance(
                connected_authority_ref=self._settings.connected_authority_ref,
                connection_binding_ref=self._settings.connection_binding_ref,
                source_revision=body.source_revision,
                source_payload_digest=body.source_payload_digest,
                authentication_method="signed_response",
                authentication_key_ref=envelope.key_ref,
                authentication_evidence_digest=digest(
                    {
                        "domain": _SIGNATURE_DOMAIN,
                        "key_ref": envelope.key_ref,
                        "signature": envelope.signature,
                    }
                ),
                adapter_ref=self._settings.adapter_ref,
            ),
        )

    @staticmethod
    def _validated_envelope(payload: bytes) -> SignedEntitlementEnvelope:
        try:
            return SignedEntitlementEnvelope.model_validate_json(payload, strict=True)
        except ValueError as error:
            raise ConnectedPolicyAuthorityIntegrityError(
                "connected policy response is malformed"
            ) from error


def _load_public_key(pem: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as error:
        raise ValueError("signing_public_key_pem must contain an Ed25519 public key") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("signing_public_key_pem must contain an Ed25519 public key")
    return key
