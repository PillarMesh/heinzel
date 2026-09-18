from __future__ import annotations

import base64
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from heinzel_contract_model import canonical_bytes, digest

from .models import ExecutionGraph, SignedExecutionGraph


class InvalidGraph(RuntimeError):
    pass


class GraphSigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def generate(cls, key_id: str) -> GraphSigner:
        return cls(key_id, Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def sign(self, graph: ExecutionGraph) -> SignedExecutionGraph:
        payload = canonical_bytes(graph)
        signature = self._private_key.sign(payload)
        return SignedExecutionGraph(
            graph=graph,
            graph_digest=digest(graph),
            key_id=self.key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )


class GraphVerifier:
    def __init__(self, keys: dict[str, Ed25519PublicKey]) -> None:
        self._keys = keys

    def verify(self, signed: SignedExecutionGraph, now: datetime) -> ExecutionGraph:
        if digest(signed.graph) != signed.graph_digest:
            raise InvalidGraph("graph digest mismatch")
        key = self._keys.get(signed.key_id)
        if key is None:
            raise InvalidGraph("unknown signing key")
        try:
            key.verify(base64.b64decode(signed.signature), canonical_bytes(signed.graph))
        except (InvalidSignature, ValueError) as exc:
            raise InvalidGraph("graph signature is invalid") from exc
        if signed.graph.schema_version != "1":
            raise InvalidGraph("unsupported graph schema version")
        if now >= signed.graph.expires_at:
            raise InvalidGraph("graph is expired")
        return signed.graph
