from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_KEY_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
_MAXIMUM_POLICY_ROWS = 2**63 - 1
_MAXIMUM_DECIMAL_38_SCALED_VALUE = 10**38 - 1


class InvalidProductInputCardinalityEvidence(RuntimeError):
    pass


class _ProductCardinalityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProductInputGenerationExpectation(_ProductCardinalityModel):
    generation_id: str = Field(pattern=_DIGEST_PATTERN)
    receipt_digest: str = Field(pattern=_DIGEST_PATTERN)


class ProductInputReceiptCardinality(_ProductCardinalityModel):
    generation_id: str = Field(pattern=_DIGEST_PATTERN)
    receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    record_count: int = Field(gt=0)


class ProductInputCardinalityEvidence(_ProductCardinalityModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_revision: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    product_plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    relation_ref: str = Field(min_length=1)
    generation_ids: tuple[str, ...] = Field(min_length=1)
    receipts: tuple[ProductInputReceiptCardinality, ...] = Field(min_length=1)
    total_contributing_row_ceiling: int = Field(gt=0, le=_MAXIMUM_POLICY_ROWS)
    policy_maximum_contributing_rows: int = Field(gt=0, le=_MAXIMUM_POLICY_ROWS)
    decimal_input_precision: Literal[38] = 38
    decimal_input_scale: Literal[9] = 9
    maximum_scaled_sum: int = Field(gt=0)
    rule_id: Literal["PRODUCT-SQL-INPUT-CARDINALITY"] = "PRODUCT-SQL-INPUT-CARDINALITY"
    rule_version: Literal["1"] = "1"
    authority_ref: str = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @field_validator("generation_ids")
    @classmethod
    def generation_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("generation identifiers must be unique")
        return value

    @model_validator(mode="after")
    def derived_cardinality_is_consistent(self) -> Self:
        receipt_generation_ids = tuple(item.generation_id for item in self.receipts)
        if receipt_generation_ids != self.generation_ids:
            raise ValueError("receipt cardinalities must follow the exact generation order")
        total = sum(item.record_count for item in self.receipts)
        if total != self.total_contributing_row_ceiling:
            raise ValueError("total contributing row ceiling does not match receipts")
        if total > self.policy_maximum_contributing_rows:
            raise ValueError("total contributing row ceiling exceeds policy maximum")
        if self.maximum_scaled_sum != total * _MAXIMUM_DECIMAL_38_SCALED_VALUE:
            raise ValueError("maximum scaled sum does not match the row ceiling")
        return self


class SignedProductInputCardinalityEvidence(_ProductCardinalityModel):
    schema_version: Literal["1"] = "1"
    evidence: ProductInputCardinalityEvidence
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    key_id: str = Field(pattern=_KEY_ID_PATTERN)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def digest_matches_evidence(self) -> Self:
        if self.evidence_digest != digest(self.evidence):
            raise ValueError("product input cardinality evidence digest does not match")
        return self


def _signature_payload(*, evidence_digest: str, key_id: str) -> bytes:
    return canonical_bytes(
        {
            "domain": "pillarmesh-product-input-cardinality-evidence-v1",
            "evidence_digest": evidence_digest,
            "key_id": key_id,
        }
    )


def _require_only_declared_fields(value: object, model_type: type[BaseModel]) -> None:
    if type(value) is not model_type or set(vars(value)) != set(model_type.model_fields):
        raise ValueError("product input cardinality artifact has undeclared fields")


def revalidate_product_input_cardinality_evidence(
    evidence: ProductInputCardinalityEvidence,
) -> ProductInputCardinalityEvidence:
    try:
        _require_only_declared_fields(evidence, ProductInputCardinalityEvidence)
        if type(evidence.generation_ids) is not tuple or type(evidence.receipts) is not tuple:
            raise TypeError
        receipt_payloads: list[dict[str, object]] = []
        for receipt in evidence.receipts:
            _require_only_declared_fields(receipt, ProductInputReceiptCardinality)
            receipt_payloads.append(dict(vars(receipt)))
        payload: dict[str, object] = dict(vars(evidence))
        payload["generation_ids"] = tuple(evidence.generation_ids)
        payload["receipts"] = tuple(receipt_payloads)
        return ProductInputCardinalityEvidence.model_validate(payload, strict=True)
    except (AttributeError, TypeError, ValueError, ValidationError) as error:
        raise InvalidProductInputCardinalityEvidence(
            "product input cardinality evidence is invalid"
        ) from error


def _revalidate_signed_evidence(
    signed: SignedProductInputCardinalityEvidence,
) -> SignedProductInputCardinalityEvidence:
    try:
        _require_only_declared_fields(signed, SignedProductInputCardinalityEvidence)
        evidence = revalidate_product_input_cardinality_evidence(signed.evidence)
        payload: dict[str, object] = dict(vars(signed))
        payload["evidence"] = evidence.model_dump(mode="python")
        return SignedProductInputCardinalityEvidence.model_validate(payload, strict=True)
    except (
        AttributeError,
        InvalidProductInputCardinalityEvidence,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise InvalidProductInputCardinalityEvidence(
            "signed product input cardinality evidence is invalid"
        ) from error


class ProductInputCardinalityEvidenceSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if re.fullmatch(_KEY_ID_PATTERN, key_id) is None:
            raise ValueError("product input cardinality signing key identifier is invalid")
        self._key_id = key_id
        self._private_key = private_key

    @classmethod
    def generate(cls, key_id: str) -> ProductInputCardinalityEvidenceSigner:
        return cls(key_id, Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def sign(
        self, evidence: ProductInputCardinalityEvidence
    ) -> SignedProductInputCardinalityEvidence:
        evidence = revalidate_product_input_cardinality_evidence(evidence)
        evidence_digest = digest(evidence)
        signature = self._private_key.sign(
            _signature_payload(evidence_digest=evidence_digest, key_id=self._key_id)
        )
        return SignedProductInputCardinalityEvidence(
            evidence=evidence,
            evidence_digest=evidence_digest,
            key_id=self._key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )


class ProductInputCardinalityEvidenceVerifier:
    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(keys)

    def verify(
        self,
        signed: SignedProductInputCardinalityEvidence,
        *,
        evaluated_at: datetime,
    ) -> ProductInputCardinalityEvidence:
        envelope = _revalidate_signed_evidence(signed)
        if (
            not isinstance(evaluated_at, datetime)
            or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() != timedelta(0)
        ):
            raise InvalidProductInputCardinalityEvidence(
                "product input cardinality evidence verification time must be UTC"
            )
        if envelope.evidence.created_at > evaluated_at.astimezone(UTC):
            raise InvalidProductInputCardinalityEvidence(
                "product input cardinality evidence is from the future"
            )
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise InvalidProductInputCardinalityEvidence(
                "unknown product input cardinality evidence signing key"
            )
        try:
            signature = base64.b64decode(envelope.signature, validate=True)
            key.verify(
                signature,
                _signature_payload(
                    evidence_digest=envelope.evidence_digest,
                    key_id=envelope.key_id,
                ),
            )
        except (AttributeError, InvalidSignature, TypeError, ValueError, binascii.Error) as error:
            raise InvalidProductInputCardinalityEvidence(
                "product input cardinality evidence signature is invalid"
            ) from error
        return envelope.evidence
