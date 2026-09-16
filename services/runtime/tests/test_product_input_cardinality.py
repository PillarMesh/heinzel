from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    LandReceipt,
    RawGenerationTarget,
    raw_generation_key,
)
from pillarmesh_runtime.generation_ledger import GenerationLedger
from pillarmesh_runtime.product_input_cardinality import (
    ProductInputCardinalityAuthorityError,
    ProductInputCardinalityEvidence,
    ProductInputCardinalityEvidenceCorruptError,
    ProductInputCardinalityEvidenceSigner,
    ProductInputCardinalityEvidenceUnavailableError,
    ProductInputCardinalityEvidenceVerifier,
    ProductInputCardinalityResolver,
    ProductInputGenerationExpectation,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from pydantic import ValidationError

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_CONTRACT_DIGEST = "c" * 64
_PLAN_DIGEST = "d" * 64


def _record_generation(
    ledger: GenerationLedger,
    *,
    tenant_id: str = "tenant-a",
    contract_ref: str = "contract-a",
    contract_revision: int = 7,
    contract_digest: str = _CONTRACT_DIGEST,
    relation_ref: str = "raw_revenue",
    record_count: int = 2,
    segment_digest: str = "a" * 64,
    acknowledgement_receipt_digest: str | None = None,
    acknowledgement_tenant_id: str | None = None,
) -> ProductInputGenerationExpectation:
    target = RawGenerationTarget(
        tenant_id=tenant_id,
        contract_ref=contract_ref,
        contract_revision=contract_revision,
        trigger_window=f"window-{segment_digest[0:8]}",
        destination_binding_ref="warehouse-a",
        logical_object_ref="revenue",
        table_ref=relation_ref,
        schema_digest="b" * 64,
    )
    generation_id = raw_generation_key(target=target, segment_digest=segment_digest)
    receipt = LandReceipt(
        receipt_id=f"receipt-{segment_digest[0:8]}",
        idempotency_key="e" * 64,
        tenant_id=tenant_id,
        contract_ref=contract_ref,
        contract_revision=contract_revision,
        trigger_window=target.trigger_window,
        destination_binding_ref=target.destination_binding_ref,
        logical_object_ref=target.logical_object_ref,
        target_table_ref=target.table_ref,
        generation_id=generation_id,
        segment_digest=segment_digest,
        schema_digest=target.schema_digest,
        record_count=record_count,
        provider_commit_ref=f"commit-{segment_digest[0:8]}",
        committed_at=_NOW,
    )
    receipt_digest = digest(receipt)
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id=f"ack-{segment_digest[0:8]}",
        tenant_id=acknowledgement_tenant_id or tenant_id,
        consumer_ref="runtime-land",
        contract_digest=contract_digest,
        source_binding_ref="source-a",
        batch_id="f" * 64,
        batch_manifest_digest="1" * 64,
        prior_checkpoint_revision=6,
        candidate_checkpoint_digest="2" * 64,
        consumer_receipt_digest=acknowledgement_receipt_digest or receipt_digest,
        acknowledged_at=_NOW,
    )
    ledger.record(
        generation_key=generation_id,
        receipt=receipt,
        acknowledgement=acknowledgement,
    )
    return ProductInputGenerationExpectation(
        generation_id=generation_id,
        receipt_digest=receipt_digest,
    )


def _resolve(
    ledger: GenerationLedger,
    generations: tuple[ProductInputGenerationExpectation, ...],
    *,
    tenant_id: str = "tenant-a",
    contract_ref: str = "contract-a",
    contract_revision: int = 7,
    contract_digest: str = _CONTRACT_DIGEST,
    relation_ref: str = "raw_revenue",
    policy_maximum_contributing_rows: int = 10,
) -> ProductInputCardinalityEvidence:
    return ProductInputCardinalityResolver(
        ledger=ledger,
        clock=lambda: _NOW,
        authority_ref="runtime-generation-ledger-v1",
    ).resolve(
        tenant_id=tenant_id,
        contract_ref=contract_ref,
        contract_revision=contract_revision,
        contract_digest=contract_digest,
        product_plan_digest=_PLAN_DIGEST,
        relation_ref=relation_ref,
        generations=generations,
        policy_maximum_contributing_rows=policy_maximum_contributing_rows,
    )


def test_resolver_derives_cardinality_only_from_exact_authoritative_receipts() -> None:
    ledger = GenerationLedger.in_memory()
    first = _record_generation(ledger, record_count=2, segment_digest="a" * 64)
    second = _record_generation(ledger, record_count=3, segment_digest="3" * 64)

    evidence = _resolve(ledger, (first, second))

    assert evidence.generation_ids == (first.generation_id, second.generation_id)
    assert tuple(item.receipt_digest for item in evidence.receipts) == (
        first.receipt_digest,
        second.receipt_digest,
    )
    assert tuple(item.record_count for item in evidence.receipts) == (2, 3)
    assert evidence.total_contributing_row_ceiling == 5
    assert evidence.policy_maximum_contributing_rows == 10
    assert evidence.decimal_input_precision == 38
    assert evidence.decimal_input_scale == 9
    assert evidence.maximum_scaled_sum == 5 * (10**38 - 1)
    assert evidence.rule_id == "PRODUCT-SQL-INPUT-CARDINALITY"
    assert evidence.rule_version == "1"


def test_resolver_accepts_the_exact_policy_boundary_and_replays_identically() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger, record_count=7)

    first = _resolve(ledger, (generation,), policy_maximum_contributing_rows=7)
    replay = _resolve(ledger, (generation,), policy_maximum_contributing_rows=7)

    assert replay == first
    assert first.total_contributing_row_ceiling == 7


def test_resolver_rejects_policy_cap_plus_one_before_returning_evidence() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger, record_count=8)

    with pytest.raises(ProductInputCardinalityAuthorityError, match="policy maximum"):
        _resolve(ledger, (generation,), policy_maximum_contributing_rows=7)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("missing", "missing generation receipt"),
        ("substituted", "receipt digest mismatch"),
        ("cross_tenant", "generation authority mismatch"),
        ("stale_revision", "generation authority mismatch"),
        ("stale_digest", "contract digest mismatch"),
        ("wrong_relation", "generation authority mismatch"),
    ],
)
def test_resolver_rejects_missing_substituted_cross_tenant_and_stale_authority(
    change: str,
    message: str,
) -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    tenant_id = "tenant-a"
    contract_revision = 7
    contract_digest = _CONTRACT_DIGEST
    relation_ref = "raw_revenue"
    if change == "missing":
        generation = generation.model_copy(update={"generation_id": "9" * 64})
    elif change == "substituted":
        generation = generation.model_copy(update={"receipt_digest": "9" * 64})
    elif change == "cross_tenant":
        tenant_id = "tenant-b"
    elif change == "stale_revision":
        contract_revision = 8
    elif change == "stale_digest":
        contract_digest = "9" * 64
    elif change == "wrong_relation":
        relation_ref = "raw_other"

    with pytest.raises(ProductInputCardinalityAuthorityError, match=message):
        _resolve(
            ledger,
            (generation,),
            tenant_id=tenant_id,
            contract_revision=contract_revision,
            contract_digest=contract_digest,
            relation_ref=relation_ref,
        )


def test_resolver_rejects_duplicate_generation_inputs() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)

    with pytest.raises(ProductInputCardinalityAuthorityError, match="unique"):
        _resolve(ledger, (generation, generation))


def test_resolver_rejects_acknowledgement_bound_to_another_receipt() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(
        ledger,
        acknowledgement_receipt_digest="9" * 64,
    )

    with pytest.raises(
        ProductInputCardinalityAuthorityError,
        match="acknowledgement receipt digest mismatch",
    ):
        _resolve(ledger, (generation,))


def test_resolver_rejects_acknowledgement_bound_to_another_tenant() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(
        ledger,
        acknowledgement_tenant_id="tenant-b",
    )

    with pytest.raises(ProductInputCardinalityAuthorityError, match="authority mismatch"):
        _resolve(ledger, (generation,))


def test_resolver_rejects_policy_above_signed_64_bit_range_before_loading() -> None:
    ledger = GenerationLedger.in_memory()
    missing = ProductInputGenerationExpectation(
        generation_id="9" * 64,
        receipt_digest="8" * 64,
    )

    with pytest.raises(ProductInputCardinalityAuthorityError, match="signed 64-bit"):
        _resolve(ledger, (missing,), policy_maximum_contributing_rows=2**63)


def test_evidence_rejects_policy_values_above_signed_64_bit_range() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))

    with pytest.raises(ValidationError, match="less than or equal"):
        ProductInputCardinalityEvidence.model_validate(
            {
                **evidence.model_dump(),
                "policy_maximum_contributing_rows": 2**63,
            }
        )


def test_evidence_rejects_a_non_utc_creation_time() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ProductInputCardinalityEvidence.model_validate(
            {
                **evidence.model_dump(),
                "created_at": datetime(2026, 9, 15, 12),
            }
        )


def test_evidence_repository_persists_and_replays_the_exact_private_artifact() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()

    evidence_digest = repository.record(evidence)
    replay_digest = repository.record(evidence)

    assert replay_digest == evidence_digest == digest(evidence)
    assert (
        repository.read(
            tenant_id="tenant-a",
            evidence_digest=evidence_digest,
        )
        == evidence
    )
    assert (
        repository.read(
            tenant_id="tenant-b",
            evidence_digest=evidence_digest,
        )
        is None
    )
    columns = {
        str(row[1])
        for row in repository._connection.execute(
            "PRAGMA table_info(product_input_cardinality_evidence_v1)"
        ).fetchall()
    }
    assert columns == {"evidence_digest", "tenant_id", "payload"}


def test_evidence_repository_rejects_corrupt_payload_instead_of_exposing_it() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    evidence_digest = repository.record(evidence)
    repository._connection.execute("DROP TRIGGER product_input_cardinality_evidence_no_update")
    repository._connection.execute(
        "UPDATE product_input_cardinality_evidence_v1 SET payload = ?",
        (b"not-json",),
    )

    with pytest.raises(ProductInputCardinalityEvidenceCorruptError, match="invalid"):
        repository.read(tenant_id="tenant-a", evidence_digest=evidence_digest)


def test_evidence_repository_is_append_only() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    repository.record(evidence)

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        repository._connection.execute(
            "UPDATE product_input_cardinality_evidence_v1 SET payload = payload"
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        repository._connection.execute("DELETE FROM product_input_cardinality_evidence_v1")


def test_evidence_repository_classifies_authority_unavailability() -> None:
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
    repository._connection.close()

    with pytest.raises(ProductInputCardinalityEvidenceUnavailableError, match="unavailable"):
        repository.read(tenant_id="tenant-a", evidence_digest="9" * 64)


def test_resolver_derives_and_signs_the_exact_ledger_evidence() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger, record_count=7)
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    signer = ProductInputCardinalityEvidenceSigner("runtime-cardinality-1", private_key)
    resolver = ProductInputCardinalityResolver(
        ledger=ledger,
        clock=lambda: _NOW,
        authority_ref="runtime-generation-ledger-v1",
        signer=signer,
    )

    signed = resolver.resolve_signed(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=7,
        contract_digest=_CONTRACT_DIGEST,
        product_plan_digest=_PLAN_DIGEST,
        relation_ref="raw_revenue",
        generations=(generation,),
        policy_maximum_contributing_rows=7,
    )

    verified = ProductInputCardinalityEvidenceVerifier(
        {"runtime-cardinality-1": signer.public_key}
    ).verify(signed, evaluated_at=_NOW)
    assert verified == _resolve(ledger, (generation,), policy_maximum_contributing_rows=7)
    assert signed.evidence_digest == digest(verified)


def test_signed_resolution_without_signer_fails_before_ledger_access() -> None:
    resolver = ProductInputCardinalityResolver(
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
        authority_ref="runtime-generation-ledger-v1",
    )
    missing = ProductInputGenerationExpectation(
        generation_id="9" * 64,
        receipt_digest="8" * 64,
    )

    with pytest.raises(ProductInputCardinalityAuthorityError, match="signing authority"):
        resolver.resolve_signed(
            tenant_id="tenant-a",
            contract_ref="contract-a",
            contract_revision=7,
            contract_digest=_CONTRACT_DIGEST,
            product_plan_digest=_PLAN_DIGEST,
            relation_ref="raw_revenue",
            generations=(missing,),
            policy_maximum_contributing_rows=7,
        )


def test_signed_repository_replays_exact_envelope_and_projects_unsigned_evidence() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    signer = ProductInputCardinalityEvidenceSigner("runtime-cardinality-1", private_key)
    signed = signer.sign(evidence)
    verifier = ProductInputCardinalityEvidenceVerifier({"runtime-cardinality-1": signer.public_key})
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory(
        signed_evidence_verifier=verifier
    )

    evidence_digest = repository.record_signed(signed)
    replay_digest = repository.record_signed(signed)

    assert replay_digest == evidence_digest == signed.evidence_digest
    assert repository.read_signed(tenant_id="tenant-a", evidence_digest=evidence_digest) == signed
    assert repository.read(tenant_id="tenant-a", evidence_digest=evidence_digest) == evidence
    assert repository.read_signed(tenant_id="tenant-b", evidence_digest=evidence_digest) is None
    columns = {
        str(row[1]): int(row[5])
        for row in repository._connection.execute(
            "PRAGMA table_info(signed_product_input_cardinality_evidence_v1)"
        ).fetchall()
    }
    assert columns == {"tenant_id": 1, "evidence_digest": 2, "payload": 0}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        repository._connection.execute(
            "UPDATE signed_product_input_cardinality_evidence_v1 SET payload = payload"
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        repository._connection.execute("DELETE FROM signed_product_input_cardinality_evidence_v1")


def test_signed_repository_rejects_conflicting_envelope_for_the_same_evidence_digest() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    first = ProductInputCardinalityEvidenceSigner(
        "runtime-cardinality-1", Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
    )
    second = ProductInputCardinalityEvidenceSigner(
        "runtime-cardinality-2", Ed25519PrivateKey.from_private_bytes(b"\x04" * 32)
    )
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory(
        signed_evidence_verifier=ProductInputCardinalityEvidenceVerifier(
            {
                "runtime-cardinality-1": first.public_key,
                "runtime-cardinality-2": second.public_key,
            }
        )
    )
    repository.record_signed(first.sign(evidence))

    with pytest.raises(ProductInputCardinalityEvidenceCorruptError, match="conflicts"):
        repository.record_signed(second.sign(evidence))


def test_signed_repository_rejects_future_dated_evidence_before_storage() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,)).model_copy(
        update={"created_at": datetime(2026, 9, 15, 12, 0, 1, tzinfo=UTC)}
    )
    signer = ProductInputCardinalityEvidenceSigner(
        "runtime-cardinality-1", Ed25519PrivateKey.from_private_bytes(b"\x05" * 32)
    )
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory(
        signed_evidence_verifier=ProductInputCardinalityEvidenceVerifier(
            {"runtime-cardinality-1": signer.public_key}
        ),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProductInputCardinalityEvidenceCorruptError, match="invalid"):
        repository.record_signed(signer.sign(evidence))

    assert repository.read_signed(tenant_id="tenant-a", evidence_digest=digest(evidence)) is None


def test_signed_repository_rejects_corrupt_envelope_payload() -> None:
    ledger = GenerationLedger.in_memory()
    generation = _record_generation(ledger)
    evidence = _resolve(ledger, (generation,))
    signer = ProductInputCardinalityEvidenceSigner(
        "runtime-cardinality-1", Ed25519PrivateKey.from_private_bytes(b"\x05" * 32)
    )
    repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory(
        signed_evidence_verifier=ProductInputCardinalityEvidenceVerifier(
            {"runtime-cardinality-1": signer.public_key}
        )
    )
    signed = signer.sign(evidence)
    repository.record_signed(signed)
    repository._connection.execute(
        "DROP TRIGGER signed_product_input_cardinality_evidence_no_update"
    )
    payload = json.loads(signed.model_dump_json())
    payload["signature"] = "not-base64!"
    repository._connection.execute(
        "UPDATE signed_product_input_cardinality_evidence_v1 SET payload = ?",
        (json.dumps(payload).encode(),),
    )

    with pytest.raises(ProductInputCardinalityEvidenceCorruptError, match="invalid"):
        repository.read_signed(tenant_id="tenant-a", evidence_digest=signed.evidence_digest)
