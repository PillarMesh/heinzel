from __future__ import annotations

import re

import pytest
from pillarmesh_evidence import AcquisitionEvidenceReceipt
from pillarmesh_runtime import opaque_reference_factory

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def test_a_reference_names_its_kind_and_carries_allocated_entropy() -> None:
    allocate = opaque_reference_factory()

    reference = allocate("evidence")

    kind, _, token = reference.partition(":")
    assert kind == "evidence-ref"
    assert len(token) >= 32


def test_two_references_of_the_same_kind_never_collide() -> None:
    """`evidence_id` is a primary key in the receipt store, so a collision is a loss.

    A colliding second append is refused as a contradictory replay, which would
    surface as an integrity failure on a run that did nothing wrong.
    """
    allocate = opaque_reference_factory()

    references = {allocate("evidence") for _ in range(2_000)}

    assert len(references) == 2_000


def test_a_reference_is_never_a_bare_digest() -> None:
    """`AcquisitionEvidenceReceipt` refuses a digest-shaped reference outright.

    A reference derived from content would let a reader recompute it and treat it
    as a claim about the payload, which is why the model requires independently
    allocated values.
    """
    allocate = opaque_reference_factory()

    for kind in ("evidence", "prepared_receipt", "governed_outcome"):
        assert not _DIGEST.fullmatch(allocate(kind))


def test_an_allocated_reference_is_accepted_by_the_receipt_it_is_allocated_for() -> None:
    """The model is the authority on what a reference may look like, so ask it."""
    from datetime import UTC, datetime

    allocate = opaque_reference_factory()

    receipt = AcquisitionEvidenceReceipt(
        evidence_id=allocate("evidence"),
        tenant_id="tenant-a",
        run_intent_ref="1" * 64,
        contract_ref="contract:orders:v1",
        source_binding_ref="source-binding:orders",
        acquisition_mode="snapshot",
        logical_object_refs=("orders",),
        prepared_receipt_ref=allocate("prepared_receipt"),
        checkpoint_receipt_ref=None,
        prior_checkpoint_revision=0,
        resulting_checkpoint_revision=None,
        reason_codes=(),
        outcome="prepared",
        created_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
    )

    assert receipt.evidence_id.startswith("evidence-ref:")


@pytest.mark.parametrize("kind", ("", " ", "not a kind", "evidence:extra"))
def test_a_kind_that_would_not_survive_the_reference_format_is_refused(kind: str) -> None:
    """The kind becomes part of the reference, so it cannot carry the separator.

    Accepting one would produce a reference whose kind cannot be read back, and the
    runtime asks for its kinds by literal name -- a caller passing something else
    has made a mistake worth reporting rather than encoding.
    """
    allocate = opaque_reference_factory()

    with pytest.raises(ValueError, match="kind"):
        allocate(kind)
