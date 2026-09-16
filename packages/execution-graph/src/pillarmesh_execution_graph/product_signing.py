from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import ValidationError

from .product_plan import (
    ProductExecutionAuthorization,
    ProductPhysicalPlan,
    SignedProductExecutionAuthorization,
    _require_only_declared_fields,
    revalidate_product_physical_plan,
)


class InvalidProductExecutionAuthorization(RuntimeError):
    pass


def _signing_payload(*, authorization_digest: str, key_id: str) -> bytes:
    return canonical_bytes(
        {
            "domain": "pillarmesh-product-execution-authorization-v1",
            "authorization_digest": authorization_digest,
            "key_id": key_id,
        }
    )


def _revalidate_signed_authorization(
    signed: SignedProductExecutionAuthorization,
) -> SignedProductExecutionAuthorization:
    try:
        _require_only_declared_fields(signed, SignedProductExecutionAuthorization)
        _require_only_declared_fields(
            signed.authorization,
            ProductExecutionAuthorization,
        )
        return SignedProductExecutionAuthorization.model_validate(
            signed.model_dump(mode="python"), strict=True
        )
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise InvalidProductExecutionAuthorization(
            "product execution authorization is invalid"
        ) from error


class ProductExecutionAuthorizationSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", key_id) is None:
            raise ValueError("product execution authorization key identifier is invalid")
        self._key_id = key_id
        self._private_key = private_key

    @classmethod
    def generate(cls, key_id: str) -> ProductExecutionAuthorizationSigner:
        return cls(key_id, Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def sign(
        self,
        *,
        physical_plan: ProductPhysicalPlan,
        legality_decision_digest: str,
        cardinality_evidence_digest: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> SignedProductExecutionAuthorization:
        plan = revalidate_product_physical_plan(physical_plan)
        authorization = ProductExecutionAuthorization(
            physical_plan_digest=digest(plan),
            legality_decision_digest=legality_decision_digest,
            cardinality_evidence_digest=cardinality_evidence_digest,
            contract_digest=plan.contract_digest,
            provider_observation_digest=plan.provider_observation_digest,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        authorization_digest = digest(authorization)
        signature = self._private_key.sign(
            _signing_payload(
                authorization_digest=authorization_digest,
                key_id=self._key_id,
            )
        )
        return SignedProductExecutionAuthorization(
            authorization=authorization,
            authorization_digest=authorization_digest,
            key_id=self._key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )


class ProductExecutionAuthorizationVerifier:
    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(keys)

    def verify(
        self,
        signed: SignedProductExecutionAuthorization,
        *,
        physical_plan: ProductPhysicalPlan,
        now: datetime,
    ) -> ProductExecutionAuthorization:
        envelope = _revalidate_signed_authorization(signed)
        plan = revalidate_product_physical_plan(physical_plan)
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise InvalidProductExecutionAuthorization(
                "unknown product execution authorization signing key"
            )
        try:
            key.verify(
                base64.b64decode(envelope.signature, validate=True),
                _signing_payload(
                    authorization_digest=envelope.authorization_digest,
                    key_id=envelope.key_id,
                ),
            )
        except (InvalidSignature, ValueError, binascii.Error) as error:
            raise InvalidProductExecutionAuthorization(
                "product execution authorization signature is invalid"
            ) from error

        authorization = envelope.authorization
        if (
            authorization.physical_plan_digest != digest(plan)
            or authorization.contract_digest != plan.contract_digest
            or authorization.provider_observation_digest != plan.provider_observation_digest
        ):
            raise InvalidProductExecutionAuthorization(
                "product execution authorization does not match the physical plan"
            )
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise InvalidProductExecutionAuthorization(
                "product execution authorization verification time must be UTC"
            )
        normalized_now = now.astimezone(UTC)
        if normalized_now < authorization.issued_at:
            raise InvalidProductExecutionAuthorization(
                "product execution authorization is not yet valid"
            )
        if normalized_now >= authorization.expires_at:
            raise InvalidProductExecutionAuthorization("product execution authorization is expired")
        return authorization
