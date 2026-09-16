from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_provider_postgresql.answer_generation import (
    PostgreSQLAnswerGenerationAuthority,
)
from pillarmesh_provider_postgresql.answer_query import PostgreSQLAnswerGenerationBinding
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_runtime import AnswerQueryReference, ProductMaterializationReceipt
from pillarmesh_semantic_registry import (
    ApprovedProductQueryBinding,
    ProductQueryBindingApproval,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
_INPUT_CARDINALITY_EVIDENCE_DIGEST = "7" * 64


class _QueryBindingReader:
    def __init__(self, binding: ApprovedProductQueryBinding | None) -> None:
        self.binding = binding

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductQueryBinding | None:
        del tenant_id, product_ref, generation
        return self.binding


class _ReceiptReader:
    def __init__(self, receipt: ProductMaterializationReceipt | None) -> None:
        self.receipt = receipt

    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None:
        del tenant_id, product_id, product_revision, product_generation
        return self.receipt


def _receipt(**changes: object) -> ProductMaterializationReceipt:
    values: dict[str, object] = {
        "run_id": "materialization-7",
        "tenant_id": "tenant-a",
        "product_id": "product-revenue",
        "product_revision": 3,
        "product_generation": 7,
        "contract_digest": "a" * 64,
        "input_generation_digests": ("b" * 64,),
        "input_cardinality_evidence_digest": _INPUT_CARDINALITY_EVIDENCE_DIGEST,
        "execution_authorization_digest": "7" * 64,
        "legality_decision_digest": "8" * 64,
        "physical_plan_digest": "9" * 64,
        "compiled_model_digest": "c" * 64,
        "output_schema_digest": "d" * 64,
        "output_row_count": 2,
        "provider_commit_reference": "e" * 64,
        "dbt_manifest_digest": "f" * 64,
        "dbt_run_results_digest": "1" * 64,
        "lineage_digest": "2" * 64,
        "quality_assertion_count": 3,
        "quality_disposition": "passed",
        "committed_at": NOW,
        "retained_until": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return ProductMaterializationReceipt.model_validate(values)


def _binding(
    receipt: ProductMaterializationReceipt,
    **changes: object,
) -> ApprovedProductQueryBinding:
    product_ref = ArtifactReference(
        artifact_id=receipt.product_id,
        version=receipt.product_revision,
        digest="3" * 64,
    )
    receipt_ref = ArtifactReference(
        artifact_id=receipt.run_id,
        version=receipt.product_generation,
        digest=digest(receipt),
    )
    metric_ref = ArtifactReference(artifact_id="metric-revenue", version=2, digest="4" * 64)
    dimension_ref = ArtifactReference(artifact_id="entity-region", version=2, digest="5" * 64)
    declaration = ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="product",
        relation_name="revenue_v3_g7",
        metric_bindings=(
            ProductQueryMetricBinding(
                semantic_ref=metric_ref,
                aggregate="sum",
                column_name="revenue",
                output_name="total_revenue",
            ),
        ),
        dimension_bindings=(
            ProductQueryDimensionBinding(
                semantic_ref=dimension_ref,
                semantic_kind="entity",
                column_name="region",
                output_name="region",
            ),
        ),
        disclosure_entity_ref=dimension_ref,
        disclosure_entity_column="region",
    )
    declaration_digest = digest(declaration)
    approval = ProductQueryBindingApproval(
        approval_id="approval-1",
        tenant_id=receipt.tenant_id,
        product_ref=product_ref,
        generation=receipt.product_generation,
        declaration_digest=declaration_digest,
        authority_ref="owner:revenue",
        actor_id="architect-1",
        decision="approve",
        created_at=NOW,
    )
    consumption_ref = ArtifactReference(
        artifact_id=f"{product_ref.artifact_id}:consumption",
        version=receipt.product_generation,
        digest=digest(
            {
                "domain": "pillarmesh-product-query-consumption-v1",
                "tenant_id": receipt.tenant_id,
                "product_ref": product_ref,
                "generation": receipt.product_generation,
                "materialization_receipt_ref": receipt_ref,
                "engine_kind": declaration.engine_kind,
                "namespace": declaration.namespace,
                "relation_name": declaration.relation_name,
            }
        ),
    )
    values: dict[str, object] = {
        "tenant_id": receipt.tenant_id,
        "product_ref": product_ref,
        "generation": receipt.product_generation,
        "contract_ref": ArtifactReference(
            artifact_id="contract-revenue", version=3, digest=receipt.contract_digest
        ),
        "semantic_version_ref": ArtifactReference(
            artifact_id="semantic-revenue", version=2, digest="6" * 64
        ),
        "materialization_receipt_ref": receipt_ref,
        "lineage_digest": receipt.lineage_digest,
        "declaration_digest": declaration_digest,
        "approval_digest": digest((approval,)),
        "consumption_object_ref": consumption_ref,
        "engine_kind": declaration.engine_kind,
        "namespace": declaration.namespace,
        "relation_name": declaration.relation_name,
        "metric_bindings": declaration.metric_bindings,
        "dimension_bindings": declaration.dimension_bindings,
        "disclosure_entity_ref": declaration.disclosure_entity_ref,
        "disclosure_entity_column": declaration.disclosure_entity_column,
        "approvals": (approval,),
        "recorded_at": NOW,
    }
    values.update(changes)
    return ApprovedProductQueryBinding.model_validate(values)


def _resolve(
    binding: ApprovedProductQueryBinding | None,
    receipt: ProductMaterializationReceipt | None,
    *,
    tenant_id: str = "tenant-a",
    consumption_object_ref: AnswerQueryReference | None = None,
    signed_model: SignedCompiledDbtModel | None = None,
    trusted_compiler_keys: dict[str, Ed25519PublicKey] | None = None,
) -> PostgreSQLAnswerGenerationBinding:
    authority = PostgreSQLAnswerGenerationAuthority(
        query_bindings=_QueryBindingReader(binding),
        materializations=_ReceiptReader(receipt),
        signed_model=signed_model,
        trusted_compiler_keys=trusted_compiler_keys,
    )
    product_ref = AnswerQueryReference(artifact_id="product-revenue", version=3, digest="3" * 64)
    if consumption_object_ref is None and binding is not None:
        consumption_object_ref = AnswerQueryReference.model_validate(
            binding.consumption_object_ref.model_dump(mode="python"), strict=True
        )
    assert consumption_object_ref is not None
    return authority.resolve(
        tenant_id=tenant_id,
        product_ref=product_ref,
        generation=7,
        consumption_object_ref=consumption_object_ref,
    )


def _signed_magnitude_model(
    private_key: Ed25519PrivateKey,
    *,
    checks: tuple[DbtDecimalMagnitudeCheck, ...] = (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
    ),
) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="revenue_v3_g7",
        contract_digest="a" * 64,
        provider="postgresql",
        input_generation_digests=("b" * 64,),
        target_schema="product",
        output_columns=("region", "total_revenue", "tax"),
        output_magnitude_checks=checks,
        compiled_sql="SELECT region, total_revenue FROM source",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-1",
        signature=base64.b64encode(
            private_key.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


def test_generation_authority_builds_binding_from_service_owned_records() -> None:
    receipt = _receipt()
    query_binding = _binding(receipt)

    resolved = _resolve(query_binding, receipt)

    assert resolved.tenant_id == "tenant-a"
    assert resolved.product_ref.artifact_id == "product-revenue"
    assert resolved.generation == 7
    assert resolved.materialization_receipt_ref.digest == digest(receipt)
    assert resolved.receipt_plan_digest == receipt.compiled_model_digest
    assert resolved.provider_commit_reference == receipt.provider_commit_reference
    assert resolved.namespace == "product"
    assert resolved.relation_name == "revenue_v3_g7"
    assert resolved.retained_until == receipt.retained_until


def test_generation_authority_verifies_and_carries_ordered_magnitude_checks() -> None:
    private_key = Ed25519PrivateKey.generate()
    checks = (
        DbtDecimalMagnitudeCheck(column_name="total_revenue"),
        DbtDecimalMagnitudeCheck(column_name="tax"),
    )
    signed_model = _signed_magnitude_model(private_key, checks=checks)
    receipt = _receipt(compiled_model_digest=signed_model.model_digest)

    resolved = _resolve(
        _binding(receipt),
        receipt,
        signed_model=signed_model,
        trusted_compiler_keys={"compiler-1": private_key.public_key()},
    )

    assert resolved.output_magnitude_checks == checks


@pytest.mark.parametrize("failure", ("missing_key", "untrusted_key", "tampered"))
def test_generation_authority_rejects_unverified_magnitude_model(failure: str) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_model = _signed_magnitude_model(private_key)
    trusted_keys: dict[str, Ed25519PublicKey] = {"compiler-1": private_key.public_key()}
    if failure == "missing_key":
        trusted_keys = {}
    elif failure == "untrusted_key":
        trusted_keys = {"compiler-1": Ed25519PrivateKey.generate().public_key()}
    else:
        signed_model = signed_model.model_copy(
            update={
                "model": signed_model.model.model_copy(
                    update={
                        "output_magnitude_checks": (DbtDecimalMagnitudeCheck(column_name="tax"),)
                    }
                )
            }
        )
    receipt = _receipt(compiled_model_digest=signed_model.model_digest)

    with pytest.raises(ProviderError, match="generation authority is invalid") as error:
        _resolve(
            _binding(receipt),
            receipt,
            signed_model=signed_model,
            trusted_compiler_keys=trusted_keys,
        )

    assert error.value.classification == "integrity_failure"


@pytest.mark.parametrize("missing", ["query_binding", "receipt"])
def test_generation_authority_denies_absent_authority(missing: str) -> None:
    receipt = _receipt()
    query_binding = _binding(receipt)

    with pytest.raises(ProviderError, match="generation authority is unavailable") as error:
        _resolve(
            None if missing == "query_binding" else query_binding,
            None if missing == "receipt" else receipt,
            consumption_object_ref=AnswerQueryReference.model_validate(
                query_binding.consumption_object_ref.model_dump(mode="python"), strict=True
            ),
        )

    assert error.value.classification == "authorization_denied"


def test_generation_authority_denies_cross_tenant_record() -> None:
    receipt = _receipt(tenant_id="tenant-b")
    query_binding = _binding(receipt)

    with pytest.raises(ProviderError, match="generation authority is invalid") as error:
        _resolve(query_binding, receipt, tenant_id="tenant-a")

    assert error.value.classification == "integrity_failure"


def test_generation_authority_denies_non_postgresql_binding() -> None:
    receipt = _receipt()
    query_binding = _binding(receipt)
    query_binding = query_binding.model_copy(update={"engine_kind": "clickhouse"})

    with pytest.raises(ProviderError, match="generation authority is invalid") as error:
        _resolve(query_binding, receipt)

    assert error.value.classification == "integrity_failure"


def test_generation_authority_denies_consumption_reference_mismatch() -> None:
    receipt = _receipt()
    query_binding = _binding(receipt)

    with pytest.raises(ProviderError, match="generation authority is unavailable"):
        _resolve(
            query_binding,
            receipt,
            consumption_object_ref=AnswerQueryReference(
                artifact_id="other:consumption", version=7, digest="7" * 64
            ),
        )


@pytest.mark.parametrize(
    ("binding_change", "receipt_change"),
    [
        (
            {"contract_ref": ArtifactReference(artifact_id="contract", version=3, digest="8" * 64)},
            {},
        ),
        ({"lineage_digest": "8" * 64}, {}),
        (
            {
                "materialization_receipt_ref": ArtifactReference(
                    artifact_id="materialization-7", version=7, digest="8" * 64
                )
            },
            {},
        ),
        ({}, {"provider_commit_reference": "not-a-postgresql-commit"}),
    ],
)
def test_generation_authority_denies_receipt_integrity_mismatch(
    binding_change: dict[str, object], receipt_change: dict[str, object]
) -> None:
    receipt = _receipt(**receipt_change)
    query_binding = _binding(receipt).model_copy(update=binding_change)

    with pytest.raises(ProviderError, match="generation authority is invalid") as error:
        _resolve(query_binding, receipt)

    assert error.value.classification == "integrity_failure"


def test_generation_authority_classifies_repository_failure_as_transient() -> None:
    class FailingReader:
        def read_current(
            self,
            *,
            tenant_id: str,
            product_ref: ArtifactReference,
            generation: int,
        ) -> ApprovedProductQueryBinding | None:
            del tenant_id, product_ref, generation
            raise RuntimeError("storage unavailable")

    authority = PostgreSQLAnswerGenerationAuthority(
        query_bindings=FailingReader(), materializations=_ReceiptReader(_receipt())
    )

    with pytest.raises(ProviderError, match="generation authority failed") as error:
        authority.resolve(
            tenant_id="tenant-a",
            product_ref=AnswerQueryReference(
                artifact_id="product-revenue", version=3, digest="3" * 64
            ),
            generation=7,
            consumption_object_ref=AnswerQueryReference(
                artifact_id="product-revenue:consumption", version=7, digest="7" * 64
            ),
        )

    assert error.value.classification == "transient_unavailable"
