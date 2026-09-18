from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_evidence import (
    AcquisitionEvidenceReceipt,
    SQLiteAcquisitionEvidenceWriter,
    SQLiteStore,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _receipt(**changes: object) -> AcquisitionEvidenceReceipt:
    values: dict[str, object] = {
        "evidence_id": "evidence-ref:prepared-1",
        "tenant_id": "tenant-a",
        "run_intent_ref": "1" * 64,
        "contract_ref": "contract:orders:v1",
        "source_binding_ref": "source-binding:orders",
        "acquisition_mode": "snapshot",
        "logical_object_refs": ("orders",),
        "prepared_receipt_ref": "receipt-ref:prepared-1",
        "checkpoint_receipt_ref": None,
        "prior_checkpoint_revision": 0,
        "resulting_checkpoint_revision": None,
        "reason_codes": (),
        "outcome": "prepared",
        "created_at": NOW,
    }
    values.update(changes)
    return AcquisitionEvidenceReceipt.model_validate(values)


def test_prepared_receipt_is_frozen_canonical_and_strict() -> None:
    receipt = _receipt()

    assert receipt.logical_object_refs == ("orders",)
    assert receipt.outcome == "prepared"
    with pytest.raises(ValidationError):
        AcquisitionEvidenceReceipt.model_validate(
            {**receipt.model_dump(), "private_cursor": "forbidden"}
        )


def test_public_receipt_structurally_excludes_private_canaries() -> None:
    receipt = _receipt()
    public = receipt.model_dump_json()

    for forbidden in (
        "private-row-canary",
        "private-cursor-canary",
        "credential-canary",
        "endpoint-canary",
        "record_key",
        "cursor_digest",
        "manifest_digest",
        "batch_id",
        "segment",
    ):
        assert forbidden not in public


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"logical_object_refs": ("orders", "orders")}, "canonical"),
        ({"logical_object_refs": ("payments", "orders")}, "canonical"),
        ({"created_at": NOW.replace(tzinfo=None)}, "UTC"),
        ({"prepared_receipt_ref": "a" * 64}, "opaque"),
        ({"outcome": "prepared", "prepared_receipt_ref": None}, "prepared"),
        (
            {
                "outcome": "acknowledged",
                "checkpoint_receipt_ref": None,
                "resulting_checkpoint_revision": None,
            },
            "acknowledged",
        ),
        (
            {
                "outcome": "failed",
                "prepared_receipt_ref": None,
                "reason_codes": (),
            },
            "reason_codes",
        ),
    ),
)
def test_receipt_rejects_noncanonical_or_contradictory_state(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _receipt(**changes)


def test_acknowledged_receipt_advances_exactly_one_revision() -> None:
    receipt = _receipt(
        outcome="acknowledged",
        checkpoint_receipt_ref="receipt-ref:checkpoint-1",
        resulting_checkpoint_revision=1,
    )

    assert receipt.resulting_checkpoint_revision == 1

    with pytest.raises(ValidationError, match="exactly one"):
        _receipt(
            outcome="acknowledged",
            checkpoint_receipt_ref="receipt-ref:checkpoint-2",
            resulting_checkpoint_revision=2,
        )


def test_public_reason_codes_reject_arbitrary_dependency_text() -> None:
    with pytest.raises(ValidationError, match="reason_codes"):
        _receipt(
            outcome="failed",
            prepared_receipt_ref=None,
            reason_codes=("private-provider-response-canary",),
        )


def test_durable_writer_retains_what_it_appends(tmp_path: Path) -> None:
    """The writer is the seam the runtime holds; it must persist, not just accept.

    Every writer the estate had was a list in a test double, so a receipt the runtime
    built was discarded the moment the process ended. This is the smallest statement
    that the seam now leads somewhere durable.
    """
    store = SQLiteStore.open(tmp_path / "m0.sqlite3")
    writer = SQLiteAcquisitionEvidenceWriter(store)
    receipt = _receipt()

    writer.append(receipt)
    store.close()

    assert SQLiteStore.open(tmp_path / "m0.sqlite3").list_acquisition_receipts("tenant-a") == (
        receipt,
    )
