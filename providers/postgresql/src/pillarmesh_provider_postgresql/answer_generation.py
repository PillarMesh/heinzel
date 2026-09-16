from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_dbt_adapter import (
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_runtime import AnswerQueryReference, ProductMaterializationReceipt
from pillarmesh_semantic_registry import ApprovedProductQueryBinding
from pydantic import ValidationError

from .answer_query import PostgreSQLAnswerGenerationBinding


class ApprovedProductQueryBindingReader(Protocol):
    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductQueryBinding | None: ...


class ProductMaterializationReceiptReader(Protocol):
    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None: ...


class PostgreSQLAnswerGenerationAuthority:
    def __init__(
        self,
        *,
        query_bindings: ApprovedProductQueryBindingReader,
        materializations: ProductMaterializationReceiptReader,
        signed_model: SignedCompiledDbtModel | None = None,
        trusted_compiler_keys: Mapping[str, Ed25519PublicKey] | None = None,
    ) -> None:
        self._query_bindings = query_bindings
        self._materializations = materializations
        self._signed_model = signed_model
        self._trusted_compiler_keys = dict(trusted_compiler_keys or {})

    def resolve(
        self,
        *,
        tenant_id: str,
        product_ref: AnswerQueryReference,
        generation: int,
        consumption_object_ref: AnswerQueryReference,
    ) -> PostgreSQLAnswerGenerationBinding:
        authority_product_ref = ArtifactReference.model_validate(
            product_ref.model_dump(mode="python"), strict=True
        )
        try:
            binding = self._query_bindings.read_current(
                tenant_id=tenant_id,
                product_ref=authority_product_ref,
                generation=generation,
            )
            receipt = self._materializations.read_receipt(
                tenant_id=tenant_id,
                product_id=product_ref.artifact_id,
                product_revision=product_ref.version,
                product_generation=generation,
            )
            if binding is None or receipt is None:
                raise _unavailable()
            binding = ApprovedProductQueryBinding.model_validate(
                binding.model_dump(mode="python"), strict=True
            )
            receipt = ProductMaterializationReceipt.model_validate(
                receipt.model_dump(mode="python"), strict=True
            )
            _require_matching_authority(
                tenant_id=tenant_id,
                product_ref=authority_product_ref,
                generation=generation,
                consumption_object_ref=consumption_object_ref,
                binding=binding,
                receipt=receipt,
            )
            output_magnitude_checks = _verified_output_magnitude_checks(
                signed_model=self._signed_model,
                trusted_compiler_keys=self._trusted_compiler_keys,
                binding=binding,
                receipt=receipt,
            )
            return PostgreSQLAnswerGenerationBinding(
                tenant_id=tenant_id,
                product_ref=product_ref,
                generation=generation,
                consumption_object_ref=consumption_object_ref,
                materialization_receipt_ref=AnswerQueryReference.model_validate(
                    binding.materialization_receipt_ref.model_dump(mode="python"), strict=True
                ),
                receipt_plan_digest=receipt.compiled_model_digest,
                provider_commit_reference=receipt.provider_commit_reference,
                namespace=binding.namespace,
                relation_name=binding.relation_name,
                retained_until=receipt.retained_until,
                output_magnitude_checks=output_magnitude_checks,
            )
        except ProviderError:
            raise
        except (ValidationError, ValueError, TypeError):
            raise _invalid() from None
        except Exception:
            raise _failed() from None


def _require_matching_authority(
    *,
    tenant_id: str,
    product_ref: ArtifactReference,
    generation: int,
    consumption_object_ref: AnswerQueryReference,
    binding: ApprovedProductQueryBinding,
    receipt: ProductMaterializationReceipt,
) -> None:
    expected_consumption_ref = ArtifactReference.model_validate(
        consumption_object_ref.model_dump(mode="python"), strict=True
    )
    receipt_ref = binding.materialization_receipt_ref
    if binding.consumption_object_ref != expected_consumption_ref:
        raise _unavailable()
    if (
        binding.engine_kind != "postgresql"
        or binding.tenant_id != tenant_id
        or binding.product_ref != product_ref
        or binding.generation != generation
        or receipt.tenant_id != tenant_id
        or receipt.product_id != product_ref.artifact_id
        or receipt.product_revision != product_ref.version
        or receipt.product_generation != generation
        or receipt_ref.artifact_id != receipt.run_id
        or receipt_ref.version != receipt.product_generation
        or receipt_ref.digest != digest(receipt)
        or binding.contract_ref.digest != receipt.contract_digest
        or binding.lineage_digest != receipt.lineage_digest
    ):
        raise _invalid()


def _verified_output_magnitude_checks(
    *,
    signed_model: SignedCompiledDbtModel | None,
    trusted_compiler_keys: Mapping[str, Ed25519PublicKey],
    binding: ApprovedProductQueryBinding,
    receipt: ProductMaterializationReceipt,
) -> tuple[DbtDecimalMagnitudeCheck, ...]:
    if signed_model is None:
        return ()
    try:
        if not set(vars(signed_model)).issubset(type(signed_model).model_fields):
            raise ValueError("signed model contains undeclared fields")
        signing_bytes = compiled_dbt_model_signing_bytes(signed_model.model)
        validated = SignedCompiledDbtModel.model_validate(
            signed_model.model_dump(mode="python"), strict=True
        )
        public_key = trusted_compiler_keys.get(validated.key_id)
        if public_key is None:
            raise InvalidSignature
        public_key.verify(
            base64.b64decode(validated.signature, validate=True),
            signing_bytes,
        )
    except (
        AttributeError,
        binascii.Error,
        InvalidSignature,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise _invalid() from None
    if (
        digest(validated.model) != validated.model_digest
        or validated.model_digest != receipt.compiled_model_digest
        or validated.model.contract_digest != receipt.contract_digest
        or validated.model.provider != "postgresql"
        or validated.model.input_generation_digests != receipt.input_generation_digests
        or validated.model.target_schema != binding.namespace
        or validated.model.model_name != binding.relation_name
    ):
        raise _invalid()
    return validated.model.output_magnitude_checks


def _unavailable() -> ProviderError:
    return ProviderError(
        "PostgreSQL answer query generation authority is unavailable",
        "authorization_denied",
    )


def _invalid() -> ProviderError:
    return ProviderError(
        "PostgreSQL answer query generation authority is invalid",
        "integrity_failure",
    )


def _failed() -> ProviderError:
    return ProviderError(
        "PostgreSQL answer query generation authority failed",
        "transient_unavailable",
    )
