from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from heinzel_contract_model import digest
from heinzel_execution_graph import (
    InvalidProductInputCardinalityEvidence,
)
from heinzel_execution_graph import (
    ProductInputCardinalityEvidence as ProductInputCardinalityEvidence,
)
from heinzel_execution_graph import (
    ProductInputCardinalityEvidenceSigner as ProductInputCardinalityEvidenceSigner,
)
from heinzel_execution_graph import (
    ProductInputCardinalityEvidenceVerifier as ProductInputCardinalityEvidenceVerifier,
)
from heinzel_execution_graph import (
    ProductInputGenerationExpectation as ProductInputGenerationExpectation,
)
from heinzel_execution_graph import (
    ProductInputReceiptCardinality as ProductInputReceiptCardinality,
)
from heinzel_execution_graph import (
    SignedProductInputCardinalityEvidence as SignedProductInputCardinalityEvidence,
)
from pydantic import ValidationError

from .generation_ledger import GenerationLedger

_MAXIMUM_POLICY_ROWS = 2**63 - 1
_MAXIMUM_DECIMAL_38_SCALED_VALUE = 10**38 - 1
_EVIDENCE_TABLE = "product_input_cardinality_evidence_v1"
_SIGNED_EVIDENCE_TABLE = "signed_product_input_cardinality_evidence_v1"


class ProductInputCardinalityAuthorityError(ValueError):
    pass


class ProductInputCardinalityEvidenceUnavailableError(RuntimeError):
    pass


class ProductInputCardinalityEvidenceCorruptError(RuntimeError):
    pass


class ProductInputCardinalityEvidenceReader(Protocol):
    def read(
        self,
        *,
        tenant_id: str,
        evidence_digest: str,
    ) -> ProductInputCardinalityEvidence | None: ...


class SignedProductInputCardinalityEvidenceReader(Protocol):
    def read_signed(
        self,
        *,
        tenant_id: str,
        evidence_digest: str,
    ) -> SignedProductInputCardinalityEvidence | None: ...


class SQLiteProductInputCardinalityEvidenceRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        signed_evidence_verifier: ProductInputCardinalityEvidenceVerifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = connection
        self._signed_evidence_verifier = signed_evidence_verifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_EVIDENCE_TABLE} ("
            "evidence_digest TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS product_input_cardinality_evidence_no_update "
            f"BEFORE UPDATE ON {_EVIDENCE_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'product input cardinality evidence is append-only'); END"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS product_input_cardinality_evidence_no_delete "
            f"BEFORE DELETE ON {_EVIDENCE_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'product input cardinality evidence is append-only'); END"
        )
        self._connection.execute(
            f"CREATE TABLE IF NOT EXISTS {_SIGNED_EVIDENCE_TABLE} ("
            "tenant_id TEXT NOT NULL, evidence_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, evidence_digest))"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS signed_product_input_cardinality_evidence_no_update "
            f"BEFORE UPDATE ON {_SIGNED_EVIDENCE_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'signed product input cardinality evidence is append-only'); END"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS signed_product_input_cardinality_evidence_no_delete "
            f"BEFORE DELETE ON {_SIGNED_EVIDENCE_TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'signed product input cardinality evidence is append-only'); END"
        )

    @classmethod
    def in_memory(
        cls,
        *,
        signed_evidence_verifier: ProductInputCardinalityEvidenceVerifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> SQLiteProductInputCardinalityEvidenceRepository:
        return cls(
            sqlite3.connect(":memory:"),
            signed_evidence_verifier=signed_evidence_verifier,
            clock=clock,
        )

    def record(self, evidence: ProductInputCardinalityEvidence) -> str:
        try:
            validated = ProductInputCardinalityEvidence.model_validate(evidence.model_dump())
            evidence_digest = digest(validated)
            with self._connection:
                self._connection.execute(
                    f"INSERT OR IGNORE INTO {_EVIDENCE_TABLE} "
                    "(evidence_digest, tenant_id, payload) VALUES (?, ?, ?)",
                    (
                        evidence_digest,
                        validated.tenant_id,
                        validated.model_dump_json().encode(),
                    ),
                )
        except ValidationError as error:
            raise ProductInputCardinalityEvidenceCorruptError(
                "product input cardinality evidence is invalid"
            ) from error
        except sqlite3.Error as error:
            raise ProductInputCardinalityEvidenceUnavailableError(
                "product input cardinality evidence authority is unavailable"
            ) from error

        replay = self.read(
            tenant_id=validated.tenant_id,
            evidence_digest=evidence_digest,
        )
        if replay != validated:
            raise ProductInputCardinalityEvidenceCorruptError(
                "product input cardinality evidence digest conflicts with stored authority"
            )
        return evidence_digest

    def read(
        self,
        *,
        tenant_id: str,
        evidence_digest: str,
    ) -> ProductInputCardinalityEvidence | None:
        try:
            row = self._connection.execute(
                f"SELECT payload FROM {_EVIDENCE_TABLE} "
                "WHERE tenant_id = ? AND evidence_digest = ?",
                (tenant_id, evidence_digest),
            ).fetchone()
        except sqlite3.Error as error:
            raise ProductInputCardinalityEvidenceUnavailableError(
                "product input cardinality evidence authority is unavailable"
            ) from error
        if row is None:
            signed = self.read_signed(tenant_id=tenant_id, evidence_digest=evidence_digest)
            return None if signed is None else signed.evidence
        try:
            evidence = ProductInputCardinalityEvidence.model_validate_json(bytes(row[0]))
        except (TypeError, ValidationError) as error:
            raise ProductInputCardinalityEvidenceCorruptError(
                "product input cardinality evidence is invalid"
            ) from error
        if evidence.tenant_id != tenant_id or digest(evidence) != evidence_digest:
            raise ProductInputCardinalityEvidenceCorruptError(
                "product input cardinality evidence does not match its authority index"
            )
        return evidence

    def record_signed(self, signed: SignedProductInputCardinalityEvidence) -> str:
        validated = self._verify_signed(signed)
        try:
            with self._connection:
                self._connection.execute(
                    f"INSERT OR IGNORE INTO {_SIGNED_EVIDENCE_TABLE} "
                    "(tenant_id, evidence_digest, payload) VALUES (?, ?, ?)",
                    (
                        validated.evidence.tenant_id,
                        validated.evidence_digest,
                        validated.model_dump_json().encode(),
                    ),
                )
        except sqlite3.Error as error:
            raise ProductInputCardinalityEvidenceUnavailableError(
                "product input cardinality evidence authority is unavailable"
            ) from error
        replay = self.read_signed(
            tenant_id=validated.evidence.tenant_id,
            evidence_digest=validated.evidence_digest,
        )
        if replay != validated:
            raise ProductInputCardinalityEvidenceCorruptError(
                "signed product input cardinality evidence digest conflicts with stored authority"
            )
        return validated.evidence_digest

    def read_signed(
        self,
        *,
        tenant_id: str,
        evidence_digest: str,
    ) -> SignedProductInputCardinalityEvidence | None:
        try:
            row = self._connection.execute(
                f"SELECT payload FROM {_SIGNED_EVIDENCE_TABLE} "
                "WHERE tenant_id = ? AND evidence_digest = ?",
                (tenant_id, evidence_digest),
            ).fetchone()
        except sqlite3.Error as error:
            raise ProductInputCardinalityEvidenceUnavailableError(
                "product input cardinality evidence authority is unavailable"
            ) from error
        if row is None:
            return None
        try:
            signed = SignedProductInputCardinalityEvidence.model_validate_json(bytes(row[0]))
            validated = self._verify_signed(signed)
        except (TypeError, ValidationError) as error:
            raise ProductInputCardinalityEvidenceCorruptError(
                "signed product input cardinality evidence is invalid"
            ) from error
        if (
            validated.evidence.tenant_id != tenant_id
            or validated.evidence_digest != evidence_digest
        ):
            raise ProductInputCardinalityEvidenceCorruptError(
                "signed product input cardinality evidence does not match its authority index"
            )
        return validated

    def _verify_signed(
        self, signed: SignedProductInputCardinalityEvidence
    ) -> SignedProductInputCardinalityEvidence:
        if self._signed_evidence_verifier is None:
            raise ProductInputCardinalityEvidenceCorruptError(
                "signed product input cardinality evidence verifier is not configured"
            )
        try:
            evidence = self._signed_evidence_verifier.verify(
                signed,
                evaluated_at=self._clock(),
            )
        except (
            AttributeError,
            InvalidProductInputCardinalityEvidence,
            TypeError,
            ValueError,
        ) as error:
            raise ProductInputCardinalityEvidenceCorruptError(
                "signed product input cardinality evidence is invalid"
            ) from error
        return signed.model_copy(update={"evidence": evidence})


class ProductInputCardinalityResolver:
    def __init__(
        self,
        *,
        ledger: GenerationLedger,
        clock: Callable[[], datetime],
        authority_ref: str,
        signer: ProductInputCardinalityEvidenceSigner | None = None,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._authority_ref = authority_ref
        self._signer = signer

    def resolve(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        contract_revision: int,
        contract_digest: str,
        product_plan_digest: str,
        relation_ref: str,
        generations: tuple[ProductInputGenerationExpectation, ...],
        policy_maximum_contributing_rows: int,
    ) -> ProductInputCardinalityEvidence:
        if not generations:
            raise ProductInputCardinalityAuthorityError("generation inputs cannot be empty")
        generation_ids = tuple(item.generation_id for item in generations)
        if len(generation_ids) != len(set(generation_ids)):
            raise ProductInputCardinalityAuthorityError("generation identifiers must be unique")
        if not 1 <= policy_maximum_contributing_rows <= _MAXIMUM_POLICY_ROWS:
            raise ProductInputCardinalityAuthorityError(
                "policy maximum must be within the signed 64-bit positive range"
            )

        receipt_cardinalities: list[ProductInputReceiptCardinality] = []
        total_contributing_rows = 0
        for expectation in generations:
            record = self._ledger.load_record(expectation.generation_id)
            if record is None:
                raise ProductInputCardinalityAuthorityError("missing generation receipt")

            receipt = record.receipt
            acknowledgement = record.acknowledgement
            receipt_digest = digest(receipt)
            if receipt_digest != expectation.receipt_digest:
                raise ProductInputCardinalityAuthorityError("receipt digest mismatch")
            if acknowledgement.consumer_receipt_digest != receipt_digest:
                raise ProductInputCardinalityAuthorityError(
                    "acknowledgement receipt digest mismatch"
                )
            if acknowledgement.contract_digest != contract_digest:
                raise ProductInputCardinalityAuthorityError("contract digest mismatch")
            if (
                receipt.generation_id != expectation.generation_id
                or receipt.tenant_id != tenant_id
                or acknowledgement.tenant_id != tenant_id
                or receipt.contract_ref != contract_ref
                or receipt.contract_revision != contract_revision
                or receipt.target_table_ref != relation_ref
            ):
                raise ProductInputCardinalityAuthorityError("generation authority mismatch")

            record_count = receipt.record_count
            if record_count > policy_maximum_contributing_rows - total_contributing_rows:
                raise ProductInputCardinalityAuthorityError(
                    "total contributing rows exceed policy maximum"
                )
            total_contributing_rows += record_count
            receipt_cardinalities.append(
                ProductInputReceiptCardinality(
                    generation_id=receipt.generation_id,
                    receipt_digest=receipt_digest,
                    record_count=record_count,
                )
            )

        return ProductInputCardinalityEvidence(
            tenant_id=tenant_id,
            contract_ref=contract_ref,
            contract_revision=contract_revision,
            contract_digest=contract_digest,
            product_plan_digest=product_plan_digest,
            relation_ref=relation_ref,
            generation_ids=generation_ids,
            receipts=tuple(receipt_cardinalities),
            total_contributing_row_ceiling=total_contributing_rows,
            policy_maximum_contributing_rows=policy_maximum_contributing_rows,
            maximum_scaled_sum=(total_contributing_rows * _MAXIMUM_DECIMAL_38_SCALED_VALUE),
            authority_ref=self._authority_ref,
            created_at=self._clock(),
        )

    def resolve_signed(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        contract_revision: int,
        contract_digest: str,
        product_plan_digest: str,
        relation_ref: str,
        generations: tuple[ProductInputGenerationExpectation, ...],
        policy_maximum_contributing_rows: int,
    ) -> SignedProductInputCardinalityEvidence:
        if self._signer is None:
            raise ProductInputCardinalityAuthorityError(
                "product input cardinality signing authority is not configured"
            )
        evidence = self.resolve(
            tenant_id=tenant_id,
            contract_ref=contract_ref,
            contract_revision=contract_revision,
            contract_digest=contract_digest,
            product_plan_digest=product_plan_digest,
            relation_ref=relation_ref,
            generations=generations,
            policy_maximum_contributing_rows=policy_maximum_contributing_rows,
        )
        return self._signer.sign(evidence)
