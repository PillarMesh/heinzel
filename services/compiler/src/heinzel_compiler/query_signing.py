from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from heinzel_contract_model import canonical_bytes


def _payload(plan_digest: str) -> bytes:
    if re.fullmatch(r"[a-f0-9]{64}", plan_digest) is None:
        raise ValueError("query plan digest must be a lowercase SHA-256 digest")
    return canonical_bytes({"domain": "heinzel-governed-query-plan-v1", "plan_digest": plan_digest})


class QueryPlanSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", key_id) is None:
            raise ValueError("query signing key identifier is invalid")
        self._key_id = key_id
        self._private_key = private_key

    def sign(self, plan_digest: str) -> str:
        signature = self._private_key.sign(_payload(plan_digest))
        return f"{self._key_id}:{base64.b64encode(signature).decode('ascii')}"


class QueryPlanVerifier:
    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        self._keys = dict(keys)

    def verify(self, plan_digest: str, signature: str) -> bool:
        try:
            key_id, encoded = signature.split(":")
            key = self._keys.get(key_id)
            if key is None:
                return False
            key.verify(base64.b64decode(encoded, validate=True), _payload(plan_digest))
        except (InvalidSignature, ValueError, binascii.Error):
            return False
        return True
