from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_execution_graph import (
    InvalidProductInputCardinalityEvidence,
    ProductInputCardinalityEvidence,
    ProductInputCardinalityEvidenceSigner,
    ProductInputCardinalityEvidenceVerifier,
    ProductInputReceiptCardinality,
)

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _evidence() -> ProductInputCardinalityEvidence:
    receipt = ProductInputReceiptCardinality(
        generation_id="1" * 64,
        receipt_digest="2" * 64,
        record_count=3,
    )
    return ProductInputCardinalityEvidence(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=4,
        contract_digest="3" * 64,
        product_plan_digest="4" * 64,
        relation_ref="raw_revenue",
        generation_ids=(receipt.generation_id,),
        receipts=(receipt,),
        total_contributing_row_ceiling=3,
        policy_maximum_contributing_rows=10,
        maximum_scaled_sum=3 * (10**38 - 1),
        authority_ref="runtime-generation-ledger-v1",
        created_at=NOW,
    )


def test_cardinality_evidence_signature_authenticates_the_exact_artifact() -> None:
    signer = ProductInputCardinalityEvidenceSigner.generate("cardinality-authority-1")
    signed = signer.sign(_evidence())

    verified = ProductInputCardinalityEvidenceVerifier(
        {"cardinality-authority-1": signer.public_key}
    ).verify(signed, evaluated_at=NOW)

    assert verified == _evidence()
    assert signed.evidence_digest == digest(_evidence())


def test_cardinality_evidence_signature_has_stable_domain_and_key_binding() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = ProductInputCardinalityEvidenceSigner("cardinality-authority-1", private_key).sign(
        _evidence()
    )

    private_key.public_key().verify(
        base64.b64decode(signed.signature, validate=True),
        canonical_bytes(
            {
                "domain": "pillarmesh-product-input-cardinality-evidence-v1",
                "evidence_digest": digest(_evidence()),
                "key_id": "cardinality-authority-1",
            }
        ),
    )


@pytest.mark.parametrize(
    "case_kind",
    (
        "tampered",
        "malformed_signature",
        "unknown_key",
        "future",
        "hidden_field",
        "coerced_count",
        "reordered",
    ),
)
def test_cardinality_evidence_verification_fails_closed(case_kind: str) -> None:
    signer = ProductInputCardinalityEvidenceSigner.generate("cardinality-authority-1")
    signed = signer.sign(_evidence())
    evaluated_at = NOW
    keys = {"cardinality-authority-1": signer.public_key}
    if case_kind == "tampered":
        signed = signed.model_copy(
            update={"evidence": signed.evidence.model_copy(update={"contract_digest": "9" * 64})}
        )
    elif case_kind == "malformed_signature":
        signed = signed.model_copy(update={"signature": "not-base64!"})
    elif case_kind == "unknown_key":
        keys = {}
    elif case_kind == "future":
        evaluated_at = NOW - timedelta(microseconds=1)
    elif case_kind == "hidden_field":
        object.__setattr__(signed.evidence, "hidden", "authority-bypass")
    elif case_kind == "coerced_count":
        receipt = signed.evidence.receipts[0].model_copy(update={"record_count": "3"})
        signed = signed.model_copy(
            update={"evidence": signed.evidence.model_copy(update={"receipts": (receipt,)})}
        )
    elif case_kind == "reordered":
        first = signed.evidence.receipts[0]
        other = first.model_copy(update={"generation_id": "5" * 64, "receipt_digest": "6" * 64})
        signed = signed.model_copy(
            update={
                "evidence": signed.evidence.model_copy(
                    update={
                        "generation_ids": (first.generation_id, other.generation_id),
                        "receipts": (other, first),
                        "total_contributing_row_ceiling": 6,
                        "maximum_scaled_sum": 6 * (10**38 - 1),
                    }
                )
            }
        )

    with pytest.raises(InvalidProductInputCardinalityEvidence):
        ProductInputCardinalityEvidenceVerifier(keys).verify(
            signed,
            evaluated_at=evaluated_at,
        )


def test_cardinality_evidence_verifier_rejects_a_non_utc_evaluation_time() -> None:
    signer = ProductInputCardinalityEvidenceSigner.generate("cardinality-authority-1")

    with pytest.raises(InvalidProductInputCardinalityEvidence, match="must be UTC"):
        ProductInputCardinalityEvidenceVerifier(
            {"cardinality-authority-1": signer.public_key}
        ).verify(signer.sign(_evidence()), evaluated_at=datetime(2026, 9, 15, 12))


def test_cardinality_evidence_signer_rejects_bypassed_invalid_artifacts() -> None:
    invalid = _evidence().model_copy(update={"total_contributing_row_ceiling": 4})

    with pytest.raises(InvalidProductInputCardinalityEvidence):
        ProductInputCardinalityEvidenceSigner.generate("cardinality-authority-1").sign(invalid)
