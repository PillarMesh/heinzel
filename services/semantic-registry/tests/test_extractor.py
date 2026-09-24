from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from heinzel_contract_model import digest
from heinzel_contract_service import BusinessProcessManifest, ProcessPackageReceipt
from heinzel_semantic_registry import (
    CandidateKind,
    CandidateProvenance,
    DeterministicManifestExtractor,
    SemanticCandidateSet,
    SQLiteSemanticRepository,
)
from heinzel_semantic_registry.repository import (
    SemanticArtifactConflictError,
    SemanticPersistenceError,
    _CandidateDraft,
)

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "revenue_to_cash.md"


def manifest(**updates: object) -> BusinessProcessManifest:
    values: dict[str, object] = {
        "process_name": "revenue-to-cash",
        "owner": "finance-data-owner",
        "participants": ("customer", "finance"),
        "outcomes": ("recognized-revenue",),
        "entities": ("Customer", "Invoice", "Payment", "Refund"),
        "events": ("invoice-issued", "payment-settled", "refund-issued"),
        "states": ("invoice-open", "invoice-paid", "invoice-refunded"),
        "rules": ("refund does not exceed settled payment",),
        "source_references": ("postgresql.billing", "stripe"),
        "unresolved_questions": ("refund exception owner",),
    }
    values.update(updates)
    return BusinessProcessManifest.model_validate(values)


def receipt_for(
    original: bytes,
    process_manifest: BusinessProcessManifest,
    *,
    tenant_id: str = "tenant-a",
) -> ProcessPackageReceipt:
    return ProcessPackageReceipt(
        package_id="bpp-revenue-to-cash",
        tenant_id=tenant_id,
        version=1,
        media_type="text/markdown; charset=utf-8",
        original_digest=sha256(original).hexdigest(),
        manifest_digest=digest(process_manifest),
        manifest_source_digest="f" * 64,
        uploader_id="architect-a",
        received_at=NOW,
    )


def extractor(
    repository: SQLiteSemanticRepository,
    *,
    extractor_version: str = "1.0.0",
    recorded_at: datetime = NOW,
) -> DeterministicManifestExtractor:
    return DeterministicManifestExtractor(
        repository,
        extractor_id="heinzel-bounded-markdown",
        extractor_version=extractor_version,
        clock=lambda: recorded_at,
    )


def extract(
    repository: SQLiteSemanticRepository,
    original: bytes,
    *,
    process_manifest: BusinessProcessManifest | None = None,
    extractor_version: str = "1.0.0",
    tenant_id: str = "tenant-a",
    recorded_at: datetime = NOW,
) -> SemanticCandidateSet:
    selected_manifest = process_manifest or manifest()
    return extractor(
        repository,
        extractor_version=extractor_version,
        recorded_at=recorded_at,
    ).extract(
        tenant_id=tenant_id,
        receipt=receipt_for(original, selected_manifest, tenant_id=tenant_id),
        manifest=selected_manifest,
        original=original,
    )


class FaultingRevisionConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO semantic_candidate_set_revisions"):
            raise sqlite3.OperationalError("semantic revision write failed")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def candidate_draft(
    proposed_definition: str,
    *,
    source_digest: str,
) -> _CandidateDraft:
    return _CandidateDraft(
        kind=CandidateKind.ENTITY,
        name="Refund",
        proposed_definition=proposed_definition,
        related_refs=(),
        provenance=CandidateProvenance(
            source_kind="manifest",
            source_digest=source_digest,
            source_path="manifest.entities[0]",
        ),
        confidence=Decimal("1"),
    )


def materialize_drafts(
    repository: SQLiteSemanticRepository,
    receipt: ProcessPackageReceipt,
    candidates: tuple[_CandidateDraft, ...],
) -> SemanticCandidateSet:
    return repository._materialize(
        tenant_id=receipt.tenant_id,
        package_id=receipt.package_id,
        package_version=receipt.version,
        original_digest=receipt.original_digest,
        manifest_digest=receipt.manifest_digest,
        extractor_id="heinzel-bounded-markdown",
        extractor_version="1.0.0",
        candidates=candidates,
        unresolved_questions=(),
        created_at=NOW,
    )


def test_refund_definition_comes_from_attributable_narrative_marker() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = FIXTURE.read_bytes()

    candidate_set = extract(repository, original)

    refund = next(item for item in candidate_set.candidates if item.name == "Refund")
    assert refund.kind is CandidateKind.ENTITY
    assert refund.proposed_definition == "A repayment with its own lifecycle."
    assert refund.provenance.narrative_line_start == 14
    assert refund.provenance.source_digest == receipt_for(original, manifest()).original_digest
    assert any(item.kind is CandidateKind.RELATIONSHIP for item in candidate_set.candidates)
    assert any(item.kind is CandidateKind.METRIC for item in candidate_set.candidates)


def test_manifest_and_every_bounded_section_map_to_exact_candidates() -> None:
    repository = SQLiteSemanticRepository(":memory:")

    candidate_set = extract(repository, FIXTURE.read_bytes())

    invoice = next(item for item in candidate_set.candidates if item.name == "Invoice")
    relationship = next(
        item for item in candidate_set.candidates if item.kind is CandidateKind.RELATIONSHIP
    )
    metric = next(item for item in candidate_set.candidates if item.kind is CandidateKind.METRIC)
    integrity_constraint = next(
        item for item in candidate_set.candidates if item.kind is CandidateKind.INTEGRITY_CONSTRAINT
    )
    assert invoice.proposed_definition is None
    assert invoice.provenance.source_kind == "manifest"
    assert invoice.provenance.source_path == "manifest.entities[1]"
    assert relationship.name == "refunds"
    assert relationship.related_refs == ("Refund", "Payment")
    assert relationship.provenance.narrative_line_start == 17
    assert metric.name == "Net Revenue"
    assert metric.proposed_definition == "recognized revenue less approved refunds."
    assert metric.provenance.narrative_line_start == 20
    assert integrity_constraint.name == "refund does not exceed settled payment"
    assert integrity_constraint.provenance.source_path == "manifest.rules[0]"
    assert "Missing definition for Entity `Invoice`." in candidate_set.unresolved_questions


def test_extractor_version_change_appends_candidate_set_revision() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = FIXTURE.read_bytes()

    first = extract(repository, original, extractor_version="1.0.0")
    replay = repository.store(first)
    second = extract(repository, original, extractor_version="2.0.0")

    assert replay == first
    assert second.set_id == first.set_id
    assert second.revision == first.revision + 1
    assert repository.load_revision("tenant-a", first.set_id, 1) == first
    assert repository.store(second) == second


def test_identical_extraction_replays_the_exact_persisted_artifact() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = FIXTURE.read_bytes()

    first = extract(repository, original)
    replay = extract(repository, original)

    assert replay == first


def test_package_version_rejects_changed_source_digests() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = b"## Definitions\n- Entity `Refund`: A repayment.\n"
    changed_original = b"## Definitions\n- Entity `Refund`: A governed repayment.\n"
    process_manifest = manifest(entities=("Refund",), events=(), states=(), rules=())
    changed_manifest = process_manifest.model_copy(update={"owner": "revenue-data-owner"})

    first = extract(repository, original, process_manifest=process_manifest)

    with pytest.raises(SemanticArtifactConflictError, match="different source digests"):
        extract(repository, changed_original, process_manifest=process_manifest)
    with pytest.raises(SemanticArtifactConflictError, match="different source digests"):
        extract(repository, original, process_manifest=changed_manifest)

    assert repository.load_revision("tenant-a", first.set_id, 1) == first


def test_same_source_candidate_material_appends_and_replays_revision() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = b"immutable process package"
    process_manifest = manifest(entities=("Refund",), events=(), states=(), rules=())
    receipt = receipt_for(original, process_manifest)
    first_draft = candidate_draft("A repayment.", source_digest=receipt.manifest_digest)
    changed_draft = candidate_draft(
        "A governed repayment.",
        source_digest=receipt.manifest_digest,
    )

    first = materialize_drafts(repository, receipt, (first_draft,))
    first_replay = materialize_drafts(repository, receipt, (first_draft,))
    changed = materialize_drafts(repository, receipt, (changed_draft,))
    changed_replay = materialize_drafts(repository, receipt, (changed_draft,))

    assert first_replay == first
    assert changed_replay == changed
    assert changed.set_id == first.set_id
    assert changed.revision == first.revision + 1
    assert repository.load_revision("tenant-a", first.set_id, 1) == first


def test_failed_material_revision_does_not_burn_candidate_sequences() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original_connection = repository._connection
    original = b"immutable process package"
    process_manifest = manifest(entities=("Refund",), events=(), states=(), rules=())
    receipt = receipt_for(original, process_manifest)
    first_draft = candidate_draft("A repayment.", source_digest=receipt.manifest_digest)
    changed_draft = candidate_draft(
        "A governed repayment.",
        source_digest=receipt.manifest_digest,
    )
    materialize_drafts(repository, receipt, (first_draft,))

    repository._connection = FaultingRevisionConnection(  # type: ignore[assignment]
        original_connection
    )
    with pytest.raises(SemanticPersistenceError, match="materialize semantic candidate set"):
        materialize_drafts(repository, receipt, (changed_draft,))

    repository._connection = original_connection
    recovered = materialize_drafts(repository, receipt, (changed_draft,))
    baseline_repository = SQLiteSemanticRepository(":memory:")
    materialize_drafts(baseline_repository, receipt, (first_draft,))
    baseline = materialize_drafts(baseline_repository, receipt, (changed_draft,))

    assert recovered.revision == 2
    assert tuple(item.candidate_id for item in recovered.candidates) == tuple(
        item.candidate_id for item in baseline.candidates
    )


def test_malformed_bounded_marker_is_rejected() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = b"## Definitions\n- Entity Refund: missing required backticks.\n"

    with pytest.raises(ValueError, match="malformed Definitions marker at line 2"):
        extract(repository, original, process_manifest=manifest(entities=("Refund",)))


@pytest.mark.parametrize(
    ("original", "message"),
    [
        (
            b"## Relationships\n- Refund --refunds--> `Payment`\n",
            "malformed Relationships marker at line 2",
        ),
        (
            b"## Metrics\n- Net Revenue: missing required backticks.\n",
            "malformed Metrics marker at line 2",
        ),
    ],
)
def test_every_bounded_section_rejects_malformed_markers(original: bytes, message: str) -> None:
    repository = SQLiteSemanticRepository(":memory:")

    with pytest.raises(ValueError, match=message):
        extract(repository, original)


def test_exact_duplicates_collapse_within_one_candidate_set() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = b"## Definitions\n- Entity `Refund`: A repayment.\n- Entity `Refund`: A repayment.\n"

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund", "Refund"), events=(), states=(), rules=()),
    )

    refunds = [item for item in candidate_set.candidates if item.name == "Refund"]
    assert len(refunds) == 1
    assert refunds[0].provenance.narrative_line_start == 2


def test_conflicting_definitions_remain_separate_review_items() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"## Definitions\n"
        b"- Entity `Refund`: A repayment.\n"
        b"- Entity `Refund`: A disputed reversal.\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    refunds = [item for item in candidate_set.candidates if item.name == "Refund"]
    assert {item.proposed_definition for item in refunds} == {
        "A repayment.",
        "A disputed reversal.",
    }
    assert "Conflicting definitions for Entity `Refund`." in candidate_set.unresolved_questions


def test_ambiguous_and_missing_relationship_references_are_questions() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"## Definitions\n"
        b"- Entity `Refund`: A repayment.\n"
        b"- Entity `Refund`: A disputed reversal.\n"
        b"## Relationships\n"
        b"- `Refund` --refunds--> `Unknown Payment`\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    assert "Ambiguous relationship reference `Refund` at line 5." in (
        candidate_set.unresolved_questions
    )
    assert "Unresolved relationship reference `Unknown Payment` at line 5." in (
        candidate_set.unresolved_questions
    )


def test_unknown_prose_is_not_interpreted_and_unknown_marker_is_a_question() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"Refund is unrestricted prose and must not become a definition.\n"
        b"## Definitions\n"
        b"- Attribute `Refund Code`: Internal code.\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    refund = next(item for item in candidate_set.candidates if item.name == "Refund")
    assert refund.proposed_definition is None
    assert "Unrecognized Definitions marker at line 3." in candidate_set.unresolved_questions


def test_another_markdown_heading_terminates_the_bounded_section() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"## Definitions\n"
        b"### Illustrative prose, not bounded markers\n"
        b"- Entity `Refund`: This bullet is outside the exact section.\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    refund = next(item for item in candidate_set.candidates if item.name == "Refund")
    assert refund.proposed_definition is None


def test_backtick_fence_before_a_section_cannot_create_candidates() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"```markdown\n"
        b"## Definitions\n"
        b"- Entity `Refund`: Fenced example only.\n"
        b"```\n"
        b"## Definitions\n"
        b"- Entity `Refund`: The attributable definition.\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    refunds = [item for item in candidate_set.candidates if item.name == "Refund"]
    assert len(refunds) == 1
    assert refunds[0].proposed_definition == "The attributable definition."
    assert refunds[0].provenance.narrative_line_start == 6


def test_tilde_fence_inside_an_active_section_cannot_create_candidates() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = (
        b"## Definitions\n"
        b"~~~markdown\n"
        b"- Entity `Refund`: Fenced example only.\n"
        b"~~~\n"
        b"- Entity `Refund`: The attributable definition.\n"
    )

    candidate_set = extract(
        repository,
        original,
        process_manifest=manifest(entities=("Refund",), events=(), states=(), rules=()),
    )

    refunds = [item for item in candidate_set.candidates if item.name == "Refund"]
    assert len(refunds) == 1
    assert refunds[0].proposed_definition == "The attributable definition."
    assert refunds[0].provenance.narrative_line_start == 5


def test_source_digests_are_verified_before_persistence() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = FIXTURE.read_bytes()
    process_manifest = manifest()
    invalid_receipt = receipt_for(original, process_manifest).model_copy(
        update={"original_digest": "0" * 64}
    )

    with pytest.raises(ValueError, match="original digest does not match"):
        extractor(repository).extract(
            tenant_id="tenant-a",
            receipt=invalid_receipt,
            manifest=process_manifest,
            original=original,
        )

    invalid_manifest_receipt = receipt_for(original, process_manifest).model_copy(
        update={"manifest_digest": "0" * 64}
    )
    with pytest.raises(ValueError, match="manifest digest does not match"):
        extractor(repository).extract(
            tenant_id="tenant-a",
            receipt=invalid_manifest_receipt,
            manifest=process_manifest,
            original=original,
        )


def test_receipt_tenant_must_match_the_extraction_tenant() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original = FIXTURE.read_bytes()
    process_manifest = manifest()

    with pytest.raises(ValueError, match="receipt tenant does not match"):
        extractor(repository).extract(
            tenant_id="tenant-b",
            receipt=receipt_for(original, process_manifest),
            manifest=process_manifest,
            original=original,
        )


def test_repository_rolls_back_sequences_when_revision_persistence_fails() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    original_connection = repository._connection

    repository._connection = FaultingRevisionConnection(  # type: ignore[assignment]
        original_connection
    )
    with pytest.raises(SemanticPersistenceError, match="materialize semantic candidate set"):
        extract(repository, FIXTURE.read_bytes())

    repository._connection = original_connection
    recovered = extract(repository, FIXTURE.read_bytes())
    baseline = extract(SQLiteSemanticRepository(":memory:"), FIXTURE.read_bytes())

    assert recovered.set_id == baseline.set_id
    assert recovered.candidates[0].candidate_id == baseline.candidates[0].candidate_id


def test_equivalent_sequence_state_has_clock_independent_candidate_ids() -> None:
    first = extract(
        SQLiteSemanticRepository(":memory:"),
        FIXTURE.read_bytes(),
        recorded_at=NOW,
    )
    later = extract(
        SQLiteSemanticRepository(":memory:"),
        FIXTURE.read_bytes(),
        recorded_at=NOW + timedelta(days=1),
    )

    assert later.created_at != first.created_at
    assert later.set_id == first.set_id
    assert tuple(item.candidate_id for item in later.candidates) == tuple(
        item.candidate_id for item in first.candidates
    )


def test_equivalent_tenant_sequences_produce_tenant_distinct_candidate_ids() -> None:
    repository = SQLiteSemanticRepository(":memory:")

    tenant_a = extract(repository, FIXTURE.read_bytes(), tenant_id="tenant-a")
    tenant_b = extract(repository, FIXTURE.read_bytes(), tenant_id="tenant-b")

    assert tenant_b.set_id != tenant_a.set_id
    assert {item.candidate_id for item in tenant_b.candidates}.isdisjoint(
        item.candidate_id for item in tenant_a.candidates
    )


def test_one_tenant_cannot_read_another_tenants_candidate_revision() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    candidate_set = extract(repository, FIXTURE.read_bytes())

    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.load_revision("tenant-b", candidate_set.set_id, candidate_set.revision)


def test_cross_tenant_denial_happens_before_payload_deserialization() -> None:
    repository = SQLiteSemanticRepository(":memory:")
    candidate_set = extract(repository, FIXTURE.read_bytes())
    repository._connection.execute(
        "UPDATE semantic_candidate_set_revisions SET payload = ? "
        "WHERE tenant_id = ? AND set_id = ? AND revision = ?",
        (b"not-json", "tenant-a", candidate_set.set_id, candidate_set.revision),
    )
    repository._connection.commit()

    with pytest.raises(KeyError, match="belongs to another tenant"):
        repository.load_revision("tenant-b", candidate_set.set_id, candidate_set.revision)
