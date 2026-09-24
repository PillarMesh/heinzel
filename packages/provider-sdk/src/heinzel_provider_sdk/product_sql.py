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
from heinzel_contract_model import canonical_bytes, digest
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
_KEY_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class _ProductSqlObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProductSqlColumnObservation(_ProductSqlObservationModel):
    name: str = Field(pattern=_IDENTIFIER_PATTERN)
    # "json" and "other" let an observation describe a whole landing relation, including columns
    # a product statement never reads. Only "decimal" and "string" carry type semantics.
    logical_type: Literal["decimal", "string", "json", "other"]
    physical_type: str = Field(min_length=1, max_length=128)
    nullable: bool
    decimal_precision: int | None = Field(default=None, ge=1, le=76)
    decimal_scale: int | None = Field(default=None, ge=0, le=76)
    collation: str | None = Field(default=None, min_length=1, max_length=128)
    encoding: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def type_semantics_are_complete(self) -> Self:
        if self.logical_type == "decimal":
            if self.decimal_precision is None or self.decimal_scale is None:
                raise ValueError("decimal columns require precision and scale")
            if self.decimal_scale > self.decimal_precision:
                raise ValueError("decimal scale cannot exceed precision")
            if self.collation is not None or self.encoding is not None:
                raise ValueError("decimal columns cannot declare collation or encoding")
        elif self.logical_type == "string":
            if self.collation is None or self.encoding is None:
                raise ValueError("string columns require collation and encoding")
            if self.decimal_precision is not None or self.decimal_scale is not None:
                raise ValueError("string columns cannot declare decimal precision or scale")
        elif any(
            value is not None
            for value in (
                self.decimal_precision,
                self.decimal_scale,
                self.collation,
                self.encoding,
            )
        ):
            raise ValueError(f"{self.logical_type} columns cannot declare type semantics")
        return self


class ProductSqlSumSemantics(_ProductSqlObservationModel):
    input_physical_type: str = Field(min_length=1, max_length=128)
    accumulator_physical_type: str = Field(min_length=1, max_length=128)
    result_physical_type: str = Field(min_length=1, max_length=128)
    overflow_behavior: Literal["error", "promote", "wrap"]
    null_input_behavior: Literal["exclude"]
    empty_group_behavior: Literal["no_row"]


class ProductSqlProviderObservation(_ProductSqlObservationModel):
    schema_version: Literal["1"] = "1"
    observation_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_id: str = Field(min_length=1, max_length=255)
    warehouse_binding_revision: int = Field(ge=1)
    relation_ref: str = Field(min_length=1, max_length=255)
    relation_namespace: str = Field(pattern=_IDENTIFIER_PATTERN)
    relation_name: str = Field(pattern=_IDENTIFIER_PATTERN)
    engine: Literal["postgresql", "clickhouse"]
    engine_version: str = Field(min_length=1, max_length=64)
    engine_image_digest: str = Field(pattern=_DIGEST_PATTERN)
    engine_build_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime
    columns: tuple[ProductSqlColumnObservation, ...] = Field(min_length=1)
    sum_semantics: ProductSqlSumSemantics

    @field_validator("observed_at")
    @classmethod
    def observation_time_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @field_validator("columns")
    @classmethod
    def column_names_are_unique(
        cls, value: tuple[ProductSqlColumnObservation, ...]
    ) -> tuple[ProductSqlColumnObservation, ...]:
        names = tuple(column.name for column in value)
        if len(names) != len(set(names)):
            raise ValueError("product SQL observation column names must be unique")
        return value


class SignedProductSqlProviderObservation(_ProductSqlObservationModel):
    schema_version: Literal["1"] = "1"
    observation: ProductSqlProviderObservation
    observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    key_id: str = Field(pattern=_KEY_ID_PATTERN)
    signature: str = Field(min_length=1, max_length=256)


class InvalidProductSqlProviderObservation(RuntimeError):
    pass


def _signature_payload(*, observation_digest: str, key_id: str) -> bytes:
    return canonical_bytes(
        {
            "domain": "heinzel-product-sql-provider-observation-v1",
            "observation_digest": observation_digest,
            "key_id": key_id,
        }
    )


def _require_declared_fields(model: BaseModel) -> None:
    if not set(vars(model)).issubset(type(model).model_fields):
        raise ValueError("model contains undeclared fields")


def _validated_observation(
    observation: ProductSqlProviderObservation,
) -> ProductSqlProviderObservation:
    try:
        _require_declared_fields(observation)
        for column in observation.columns:
            _require_declared_fields(column)
        _require_declared_fields(observation.sum_semantics)
        payload: dict[str, object] = dict(vars(observation))
        payload["columns"] = tuple(dict(vars(column)) for column in observation.columns)
        payload["sum_semantics"] = dict(vars(observation.sum_semantics))
        return ProductSqlProviderObservation.model_validate(payload, strict=True)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("provider observation payload is invalid") from None


class ProductSqlProviderObservationSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if re.fullmatch(_KEY_ID_PATTERN, key_id) is None:
            raise ValueError("provider observation signing key identifier is invalid")
        self._key_id = key_id
        self._private_key = private_key

    def sign(
        self, observation: ProductSqlProviderObservation
    ) -> SignedProductSqlProviderObservation:
        validated_observation = _validated_observation(observation)
        observation_digest = digest(validated_observation)
        signature = self._private_key.sign(
            _signature_payload(
                observation_digest=observation_digest,
                key_id=self._key_id,
            )
        )
        return SignedProductSqlProviderObservation(
            observation=validated_observation,
            observation_digest=observation_digest,
            key_id=self._key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )


class ProductSqlProviderObservationVerifier:
    def __init__(
        self,
        keys: Mapping[str, Ed25519PublicKey],
        *,
        maximum_observation_age: timedelta,
    ) -> None:
        if maximum_observation_age <= timedelta(0):
            raise ValueError("maximum observation age must be positive")
        self._keys = dict(keys)
        self._maximum_observation_age = maximum_observation_age

    def verify(
        self,
        signed: SignedProductSqlProviderObservation,
        *,
        evaluated_at: datetime,
    ) -> ProductSqlProviderObservation:
        evaluated_at = self._validated_evaluation_time(evaluated_at)
        envelope = self._validated_envelope(signed)
        if digest(envelope.observation) != envelope.observation_digest:
            raise InvalidProductSqlProviderObservation("provider observation digest mismatch")
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise InvalidProductSqlProviderObservation("unknown provider observation signing key")
        try:
            signature = base64.b64decode(envelope.signature, validate=True)
            key.verify(
                signature,
                _signature_payload(
                    observation_digest=envelope.observation_digest,
                    key_id=envelope.key_id,
                ),
            )
        except (InvalidSignature, ValueError, binascii.Error):
            raise InvalidProductSqlProviderObservation(
                "provider observation signature is invalid"
            ) from None
        observation_age = evaluated_at - envelope.observation.observed_at
        if observation_age < timedelta(0):
            raise InvalidProductSqlProviderObservation("provider observation is from the future")
        if observation_age > self._maximum_observation_age:
            raise InvalidProductSqlProviderObservation("provider observation is stale")
        return envelope.observation

    @staticmethod
    def _validated_evaluation_time(evaluated_at: datetime) -> datetime:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() != timedelta(0):
            raise ValueError("evaluation time must be timezone-aware UTC")
        return evaluated_at.astimezone(UTC)

    @staticmethod
    def _validated_envelope(
        signed: SignedProductSqlProviderObservation,
    ) -> SignedProductSqlProviderObservation:
        try:
            _require_declared_fields(signed)
            validated_observation = _validated_observation(signed.observation)
            payload: dict[str, object] = dict(vars(signed))
            payload["observation"] = validated_observation.model_dump(mode="python")
            return SignedProductSqlProviderObservation.model_validate(payload, strict=True)
        except (AttributeError, TypeError, ValueError):
            raise InvalidProductSqlProviderObservation(
                "provider observation envelope is invalid"
            ) from None
