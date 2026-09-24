from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_evidence import package_fulfillment_receipts
from heinzel_request_management import FulfillmentEvidenceReceipt, RequestState
from pydantic import ValidationError

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def receipt(*, evidence_id: str = "evidence-1") -> FulfillmentEvidenceReceipt:
    return FulfillmentEvidenceReceipt(
        evidence_id=evidence_id,
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=4,
        outcome="execution_ready",
        proposal_id="proposal-1",
        proposal_revision=1,
        authority_refs=("role:data_engineering_architect",),
        approval_ids=("approval-1",),
        reason_codes=(),
        resulting_state=RequestState.EXECUTING,
        created_at=NOW,
    )


def test_fulfillment_receipts_are_canonical_and_exclude_private_canaries() -> None:
    private_canaries = (
        "PRIVATE ANSWER VALUE",
        "private purpose",
        "customer.bank_account",
        "classification-secret",
        "f" * 64,
        "private-provider-object-123",
    )

    first = package_fulfillment_receipts((receipt(),))
    second = package_fulfillment_receipts((receipt(),))

    assert first == second
    assert all(canary.encode() not in first for canary in private_canaries)
    assert b'"schema_version":"1"' in first
    assert b'"execution_status"' not in first


def test_duplicate_evidence_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        package_fulfillment_receipts((receipt(), receipt()))


def test_malformed_constructed_receipt_is_strictly_revalidated() -> None:
    malformed = FulfillmentEvidenceReceipt.model_construct(
        evidence_id="evidence-malformed",
        tenant_id="tenant-a",
    )

    with pytest.raises(ValidationError):
        package_fulfillment_receipts((malformed,))


def test_privacy_scanner_rejects_connection_string_in_allowlisted_text() -> None:
    unsafe = receipt().model_copy(
        update={"reason_codes": ("postgresql://private-user:secret@warehouse/db",)}
    )

    with pytest.raises(ValueError, match="sensitive"):
        package_fulfillment_receipts((unsafe,))
