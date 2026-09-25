from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    CommitObservation,
    DestinationProviderKind,
    IdempotencyKey,
    LandReceipt,
    ProviderError,
    RawGenerationTarget,
    StagedSegment,
    raw_generation_key,
)
from heinzel_provider_sdk.destination_conformance import destination_segment, destination_target
from heinzel_runtime import GenerationLedger, LandingRunner

_NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Provider:
    provider_kind: DestinationProviderKind = "postgresql"

    def __init__(self, *, observation: Literal["committed", "not_found"] = "committed") -> None:
        self.observation = observation
        self.land_count = 0

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
    ) -> LandReceipt:
        self.land_count += 1
        assert hasattr(segment, "segment_digest")
        assert hasattr(target, "tenant_id")
        typed_segment = destination_segment().model_validate(segment)
        typed_target = destination_target().model_validate(target)
        return LandReceipt(
            receipt_id="receipt-a",
            idempotency_key=idempotency_key,
            tenant_id="tenant-a",
            contract_ref="contract-a",
            contract_revision=7,
            trigger_window="2026-09-11T12:00:00Z/PT1H",
            destination_binding_ref="warehouse-a",
            logical_object_ref="orders",
            target_table_ref="raw_orders",
            generation_id=raw_generation_key(
                target=typed_target,
                segment_digest=typed_segment.segment_digest,
            ),
            segment_digest=typed_segment.segment_digest,
            schema_digest="a" * 64,
            record_count=1,
            provider_commit_ref="provider-commit-a",
            committed_at=_NOW,
        )

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation:
        return CommitObservation(
            outcome=self.observation,
            receipt_digest=digest(receipt) if self.observation == "committed" else None,
            observed_at=_NOW,
        )


class _LandArguments(TypedDict):
    """Exactly `LandingRunner.land`'s keywords, so `**arguments` stays checked.

    Several tests call `land` twice with one argument set to prove replay. As a
    plain dict literal the set collapsed to `dict[str, object]` and every splat
    went unchecked, so a renamed or added parameter would have type checked here
    and failed only when the test ran.
    """

    segment: StagedSegment
    target: RawGenerationTarget
    idempotency_key: IdempotencyKey
    batch_id: str
    batch_manifest_digest: str
    candidate_checkpoint_digest: str
    prior_checkpoint_revision: int
    contract_digest: str
    source_binding_ref: str
    consumer_ref: str


def test_landing_acknowledges_only_after_exact_commit_proof() -> None:
    ledger = GenerationLedger.in_memory()
    runner = LandingRunner(provider=_Provider(), ledger=ledger, clock=lambda: _NOW)

    result = asyncio.run(
        runner.land(
            segment=destination_segment(),
            target=destination_target(),
            idempotency_key="b" * 64,
            batch_id="c" * 64,
            batch_manifest_digest="d" * 64,
            candidate_checkpoint_digest="e" * 64,
            prior_checkpoint_revision=0,
            contract_digest="f" * 64,
            source_binding_ref="source-a",
            consumer_ref="runtime-land",
        )
    )

    assert result.acknowledgement.consumer_receipt_digest == digest(result.receipt)
    assert ledger.load(result.generation_key) == result.receipt


def test_landing_refuses_checkpoint_acknowledgement_without_commit_proof() -> None:
    segment = destination_segment()
    target = destination_target()
    ledger = GenerationLedger.in_memory()
    runner = LandingRunner(
        provider=_Provider(observation="not_found"),
        ledger=ledger,
        clock=lambda: _NOW,
    )

    async def exercise() -> None:
        with pytest.raises(ProviderError, match="exact destination commit"):
            await runner.land(
                segment=segment,
                target=target,
                idempotency_key="b" * 64,
                batch_id="c" * 64,
                batch_manifest_digest="d" * 64,
                candidate_checkpoint_digest="e" * 64,
                prior_checkpoint_revision=0,
                contract_digest="f" * 64,
                source_binding_ref="source-a",
                consumer_ref="runtime-land",
            )

    asyncio.run(exercise())

    generation_key = raw_generation_key(
        target=target,
        segment_digest=segment.segment_digest,
    )
    assert ledger.load_record(generation_key) is None


def test_generation_ledger_replay_does_not_call_provider_twice() -> None:
    provider = _Provider()
    runner = LandingRunner(
        provider=provider,
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )
    arguments: _LandArguments = {
        "segment": destination_segment(),
        "target": destination_target(),
        "idempotency_key": "b" * 64,
        "batch_id": "c" * 64,
        "batch_manifest_digest": "d" * 64,
        "candidate_checkpoint_digest": "e" * 64,
        "prior_checkpoint_revision": 0,
        "contract_digest": "f" * 64,
        "source_binding_ref": "source-a",
        "consumer_ref": "runtime-land",
    }

    first = asyncio.run(runner.land(**arguments))
    replay = asyncio.run(runner.land(**arguments))

    assert replay == first
    assert replay.acknowledgement == first.acknowledgement
    assert replay.acknowledgement.consumer_receipt_digest == digest(replay.receipt)
    assert provider.land_count == 1


def test_generation_ledger_replay_returns_the_exact_acknowledgement() -> None:
    provider = _Provider()
    runner = LandingRunner(
        provider=provider,
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW + timedelta(minutes=1),
    )
    arguments: _LandArguments = {
        "segment": destination_segment(),
        "target": destination_target(),
        "idempotency_key": "b" * 64,
        "batch_id": "c" * 64,
        "batch_manifest_digest": "d" * 64,
        "candidate_checkpoint_digest": "e" * 64,
        "prior_checkpoint_revision": 0,
        "contract_digest": "f" * 64,
        "source_binding_ref": "source-a",
        "consumer_ref": "runtime-land",
    }

    first = asyncio.run(runner.land(**arguments))
    replay = asyncio.run(runner.land(**arguments))

    assert replay == first


def test_generation_ledger_rejects_changed_checkpoint_authority_on_replay() -> None:
    runner = LandingRunner(
        provider=_Provider(),
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )
    arguments: _LandArguments = {
        "segment": destination_segment(),
        "target": destination_target(),
        "idempotency_key": "b" * 64,
        "batch_id": "c" * 64,
        "batch_manifest_digest": "d" * 64,
        "candidate_checkpoint_digest": "e" * 64,
        "prior_checkpoint_revision": 0,
        "contract_digest": "f" * 64,
        "source_binding_ref": "source-a",
        "consumer_ref": "runtime-land",
    }
    asyncio.run(runner.land(**arguments))

    with pytest.raises(ProviderError, match="checkpoint authority mismatch"):
        changed: _LandArguments = {**arguments, "batch_id": "9" * 64}
        asyncio.run(runner.land(**changed))


@pytest.mark.parametrize(
    "fault_checkpoint",
    ("after_provider_land", "after_commit_inspection", "after_generation_ledger"),
)
def test_landing_replay_recovers_from_every_durable_boundary(
    fault_checkpoint: str,
) -> None:
    provider = _Provider()
    ledger = GenerationLedger.in_memory()
    fired = False

    def fail_once(checkpoint: str) -> None:
        nonlocal fired
        if checkpoint == fault_checkpoint and not fired:
            fired = True
            raise RuntimeError("injected crash")

    runner = LandingRunner(
        provider=provider,
        ledger=ledger,
        clock=lambda: _NOW,
        fault_hook=fail_once,
    )
    arguments: _LandArguments = {
        "segment": destination_segment(),
        "target": destination_target(),
        "idempotency_key": "b" * 64,
        "batch_id": "c" * 64,
        "batch_manifest_digest": "d" * 64,
        "candidate_checkpoint_digest": "e" * 64,
        "prior_checkpoint_revision": 0,
        "contract_digest": "f" * 64,
        "source_binding_ref": "source-a",
        "consumer_ref": "runtime-land",
    }

    with pytest.raises(RuntimeError, match="injected crash"):
        asyncio.run(runner.land(**arguments))

    result = asyncio.run(runner.land(**arguments))

    assert ledger.load(result.generation_key) == result.receipt
    assert provider.land_count == (1 if fault_checkpoint == "after_generation_ledger" else 2)
