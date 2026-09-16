from __future__ import annotations

from collections.abc import Callable

from pillarmesh_contract_model import canonical_bytes, digest

from .destination import (
    DestinationProvider,
    RawGenerationTarget,
    StagedSegment,
    staged_segment_digest,
)
from .errors import ProviderError


def destination_target() -> RawGenerationTarget:
    return RawGenerationTarget(
        tenant_id="tenant-a",
        contract_ref="contract-a",
        contract_revision=7,
        trigger_window="2026-09-11T12:00:00Z/PT1H",
        destination_binding_ref="warehouse-a",
        logical_object_ref="orders",
        table_ref="raw_orders",
        schema_digest="a" * 64,
    )


def destination_segment(*, value: str = "100.00") -> StagedSegment:
    rows = (b'{"amount":"' + value.encode() + b'","order_id":"order-7"}',)
    return StagedSegment(
        segment_digest=staged_segment_digest(rows),
        schema_digest="a" * 64,
        record_count=1,
        rows=rows,
    )


async def run_destination_conformance(
    provider_factory: Callable[[], DestinationProvider],
) -> None:
    provider = provider_factory()
    target = destination_target()
    segment = destination_segment()

    receipt = await provider.land(
        segment=segment,
        target=target,
        idempotency_key="b" * 64,
    )
    replay = await provider.land(
        segment=segment,
        target=target,
        idempotency_key="b" * 64,
    )
    observation = await provider.inspect_commit(receipt=receipt)

    assert canonical_bytes(replay) == canonical_bytes(receipt)
    assert receipt.segment_digest == segment.segment_digest
    assert receipt.record_count == 1
    assert observation.outcome == "committed"
    assert observation.receipt_digest == digest(receipt)

    try:
        await provider.land(
            segment=destination_segment(value="101.00"),
            target=target,
            idempotency_key="b" * 64,
        )
    except ProviderError as conflict_error:
        assert conflict_error.classification == "integrity_failure"
    else:
        raise AssertionError("destination provider accepted a conflicting replay")

    empty = StagedSegment(
        segment_digest=staged_segment_digest(()),
        schema_digest="a" * 64,
        record_count=0,
        rows=(),
    )
    try:
        await provider.land(segment=empty, target=target, idempotency_key="c" * 64)
    except ProviderError as empty_error:
        assert empty_error.classification == "statement_rejected"
    else:
        raise AssertionError("destination provider accepted an empty segment")

    mismatched = segment.model_copy(update={"schema_digest": "d" * 64})
    try:
        await provider.land(segment=mismatched, target=target, idempotency_key="d" * 64)
    except ProviderError as schema_error:
        assert schema_error.classification == "statement_rejected"
    else:
        raise AssertionError("destination provider accepted a schema mismatch")
