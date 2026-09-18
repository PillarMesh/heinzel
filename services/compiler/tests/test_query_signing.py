from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier


def test_query_signatures_are_deterministic_and_bound_to_digest() -> None:
    key = Ed25519PrivateKey.generate()
    signer = QueryPlanSigner("compiler-1", key)
    verifier = QueryPlanVerifier({"compiler-1": key.public_key()})
    signature = signer.sign("a" * 64)
    assert signature == signer.sign("a" * 64)
    assert verifier.verify("a" * 64, signature)
    assert not verifier.verify("b" * 64, signature)
    assert not QueryPlanVerifier({}).verify("a" * 64, signature)


@pytest.mark.parametrize(
    "signature", ["", "compiler-1:not-base64!", "unknown:AAAA", "compiler-1:AAAA:extra"]
)
def test_invalid_query_signatures_fail_closed(signature: str) -> None:
    key = Ed25519PrivateKey.generate()
    assert not QueryPlanVerifier({"compiler-1": key.public_key()}).verify("a" * 64, signature)


def test_query_verification_rejects_a_signature_from_another_artifact_domain() -> None:
    import base64

    from heinzel_contract_model import canonical_bytes

    key = Ed25519PrivateKey.generate()
    signature = key.sign(canonical_bytes({"plan_digest": "a" * 64}))
    encoded = base64.b64encode(signature).decode("ascii")

    assert not QueryPlanVerifier({"compiler-1": key.public_key()}).verify(
        "a" * 64, f"compiler-1:{encoded}"
    )


@pytest.mark.parametrize("plan_digest", ["", "a" * 63, "A" * 64, "g" * 64])
def test_invalid_digest_cannot_be_signed_or_verified(plan_digest: str) -> None:
    key = Ed25519PrivateKey.generate()
    signer = QueryPlanSigner("compiler-1", key)
    valid_signature = signer.sign("a" * 64)

    with pytest.raises(ValueError, match="digest"):
        signer.sign(plan_digest)
    assert not QueryPlanVerifier({"compiler-1": key.public_key()}).verify(
        plan_digest, valid_signature
    )


@pytest.mark.parametrize("key_id", ["", "compiler:1", "compiler 1"])
def test_ambiguous_key_identifiers_are_rejected(key_id: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        QueryPlanSigner(key_id, Ed25519PrivateKey.generate())


def test_query_signature_wire_format_uses_the_pinned_domain() -> None:
    import base64

    from heinzel_contract_model import canonical_bytes

    key = Ed25519PrivateKey.generate()
    expected = key.sign(
        canonical_bytes(
            {
                "domain": "heinzel-governed-query-plan-v1",
                "plan_digest": "a" * 64,
            }
        )
    )
    signature = f"compiler-1:{base64.b64encode(expected).decode('ascii')}"

    assert QueryPlanSigner("compiler-1", key).sign("a" * 64) == signature
    assert QueryPlanVerifier({"compiler-1": key.public_key()}).verify("a" * 64, signature)
