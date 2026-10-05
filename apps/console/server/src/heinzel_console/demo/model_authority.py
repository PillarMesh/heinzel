"""The compiler's signed model for one product generation, kept so an answer can verify it.

`PostgreSQLAnswerGenerationAuthority` re-verifies the compiled model before it answers: it checks
the compiler's signature, that the model's digest is the one the materialization receipt names,
and that its target is the relation the query binding points at. What it gets from that is the
model's declared decimal magnitude checks, which hold the engine to the magnitude the plan
promised.

That verification is optional in the provider -- with no signed model it returns no checks and
answers anyway. The demonstration signs its model inside `materialize_demo_generation` with a
key it generates there, so without somewhere to keep both, the demonstration would restart into
an answer path that enforces nothing about its own output magnitudes, and read as though it did.

So the signed model and the public half of the key that verifies it are stored here. The private
half is never stored: it signs once, during the materialization, and is discarded with the
process that made it.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from heinzel_dbt_adapter import SignedCompiledDbtModel
from pydantic import ValidationError

__all__ = ["DemoSignedModelStore", "SignedModelAuthority", "SignedModelAuthorityError"]


class SignedModelAuthorityError(RuntimeError):
    """The stored signed model is missing, unreadable, or not the one it is indexed under."""


@dataclass(frozen=True, slots=True)
class SignedModelAuthority:
    """One generation's signed model, and the key a verifier checks it with."""

    signed_model: SignedCompiledDbtModel
    public_key: Ed25519PublicKey


class DemoSignedModelStore:
    """The demonstration's signed models, one per product generation.

    Immutable per generation, like every other product authority the demonstration writes: a
    generation's compiled model is part of what it is, so a second model for the same generation
    is a conflict rather than an update.
    """

    def __init__(self, database_path: str, *, check_same_thread: bool = True) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS demo_signed_models ("
            "tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
            "generation INTEGER NOT NULL, model_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "public_key_pem BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, product_id, product_revision, generation))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        generation: int,
        signed_model: SignedCompiledDbtModel,
        public_key: Ed25519PublicKey,
    ) -> None:
        """Record this generation's signed model, or confirm the stored one is identical."""
        identity = (tenant_id, product_id, product_revision, generation)
        payload = signed_model.model_dump_json().encode("utf-8")
        public_key_pem = public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        try:
            self._connection.execute(
                "INSERT INTO demo_signed_models (tenant_id, product_id, product_revision, "
                "generation, model_digest, payload, public_key_pem) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (tenant_id, product_id, product_revision, generation) DO NOTHING",
                (*identity, signed_model.model_digest, payload, public_key_pem),
            )
            row = self._connection.execute(
                "SELECT model_digest, public_key_pem FROM demo_signed_models WHERE tenant_id = ? "
                "AND product_id = ? AND product_revision = ? AND generation = ?",
                identity,
            ).fetchone()
            if (
                row is None
                or row[0] != signed_model.model_digest
                or bytes(row[1]) != public_key_pem
            ):
                raise SignedModelAuthorityError(
                    "a different signed model is already recorded for this product generation"
                )
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def read(
        self, *, tenant_id: str, product_id: str, product_revision: int, generation: int
    ) -> SignedModelAuthority | None:
        """The signed model recorded for this generation, or `None` when there is none."""
        try:
            row = self._connection.execute(
                "SELECT model_digest, payload, public_key_pem FROM demo_signed_models "
                "WHERE tenant_id = ? AND product_id = ? AND product_revision = ? "
                "AND generation = ?",
                (tenant_id, product_id, product_revision, generation),
            ).fetchone()
        except sqlite3.Error as error:
            raise SignedModelAuthorityError("signed model authority is unavailable") from error
        if row is None:
            return None
        try:
            signed_model = SignedCompiledDbtModel.model_validate_json(row[1], strict=True)
            public_key = serialization.load_pem_public_key(bytes(row[2]))
        except (ValidationError, ValueError, TypeError) as error:
            raise SignedModelAuthorityError("stored signed model authority is invalid") from error
        if not isinstance(public_key, Ed25519PublicKey):
            raise SignedModelAuthorityError("stored compiler key is not an Ed25519 public key")
        if signed_model.model_digest != row[0]:
            raise SignedModelAuthorityError(
                "signed model authority index does not match its payload"
            )
        return SignedModelAuthority(signed_model=signed_model, public_key=public_key)
