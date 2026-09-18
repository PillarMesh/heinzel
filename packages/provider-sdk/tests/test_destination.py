from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_provider_sdk import LandReceipt, RawGenerationTarget, raw_generation_key
from pydantic import ValidationError

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _target() -> RawGenerationTarget:
    return RawGenerationTarget(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=7,
        trigger_window="2026-09-14T12:00:00Z/PT1H",
        destination_binding_ref="warehouse-a",
        logical_object_ref="orders",
        table_ref="raw_orders",
        schema_digest="a" * 64,
    )


def _receipt_payload() -> dict[str, object]:
    target = _target()
    segment_digest = "b" * 64
    return {
        "schema_version": "1",
        "receipt_id": "receipt-a",
        "idempotency_key": "c" * 64,
        "tenant_id": target.tenant_id,
        "contract_ref": target.contract_ref,
        "contract_revision": target.contract_revision,
        "trigger_window": target.trigger_window,
        "destination_binding_ref": target.destination_binding_ref,
        "logical_object_ref": target.logical_object_ref,
        "target_table_ref": target.table_ref,
        "generation_id": raw_generation_key(target=target, segment_digest=segment_digest),
        "segment_digest": segment_digest,
        "schema_digest": target.schema_digest,
        "record_count": 3,
        "provider_commit_ref": "provider-commit-a",
        "committed_at": _NOW,
    }


def test_land_receipt_has_the_exact_successful_provider_neutral_shape() -> None:
    receipt = LandReceipt.model_validate(_receipt_payload())

    assert tuple(LandReceipt.model_fields) == (
        "schema_version",
        "receipt_id",
        "idempotency_key",
        "tenant_id",
        "contract_ref",
        "contract_revision",
        "trigger_window",
        "destination_binding_ref",
        "logical_object_ref",
        "target_table_ref",
        "generation_id",
        "segment_digest",
        "schema_digest",
        "record_count",
        "provider_commit_ref",
        "committed_at",
    )
    assert receipt.generation_id == raw_generation_key(
        target=_target(),
        segment_digest=receipt.segment_digest,
    )


@pytest.mark.parametrize(
    ("update", "message"),
    (
        ({"schema_version": "2"}, "schema_version"),
        ({"schema_digest": "not-a-digest"}, "schema_digest"),
        ({"segment_digest": "not-a-digest"}, "segment_digest"),
        ({"generation_id": "d" * 64}, "generation_id does not match"),
        ({"private_connection_string": "postgresql://private"}, "extra_forbidden"),
    ),
)
def test_land_receipt_rejects_invalid_schema_digests_and_unknown_input(
    update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        LandReceipt.model_validate({**_receipt_payload(), **update})


def test_land_receipt_is_immutable() -> None:
    receipt = LandReceipt.model_validate(_receipt_payload())

    with pytest.raises(ValidationError, match="frozen"):
        receipt.record_count = 4
