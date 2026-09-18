from __future__ import annotations

from typing import Literal

from heinzel_contract_model import ArtifactModel, canonical_bytes
from heinzel_request_management import FulfillmentEvidenceReceipt

from .package_models import ScanInput
from .scanner import scan_bytes


class FulfillmentEvidencePackage(ArtifactModel):
    schema_version: Literal["1"] = "1"
    receipts: tuple[FulfillmentEvidenceReceipt, ...]


def package_fulfillment_receipts(
    receipts: tuple[FulfillmentEvidenceReceipt, ...],
) -> bytes:
    validated = tuple(
        FulfillmentEvidenceReceipt.model_validate(receipt.model_dump(mode="python"))
        for receipt in receipts
    )
    identities = tuple((receipt.tenant_id, receipt.evidence_id) for receipt in validated)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate fulfillment evidence identity")
    ordered = tuple(
        sorted(
            validated,
            key=lambda item: (
                item.tenant_id,
                item.request_id,
                item.request_revision,
                item.evidence_id,
            ),
        )
    )
    payload = canonical_bytes(FulfillmentEvidencePackage(receipts=ordered))
    if scan_bytes("fulfillment/receipts.json", payload, ScanInput()):
        raise ValueError("sensitive material scan failed")
    return payload
