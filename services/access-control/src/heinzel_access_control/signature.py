from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _verify_ed25519_signature(
    public_key: Ed25519PublicKey,
    *,
    signature_hex: str,
    payload: bytes,
) -> bool:
    try:
        public_key.verify(bytes.fromhex(signature_hex), payload)
    except (InvalidSignature, ValueError):
        return False
    return True
