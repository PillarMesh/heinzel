from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionAcknowledgement,
    DestinationProvider,
    IdempotencyKey,
    LandReceipt,
    ProviderError,
    RawGenerationTarget,
    StagedSegment,
    raw_generation_key,
)
from pydantic import BaseModel, ConfigDict

from .generation_ledger import GenerationLedger

type LandingFaultHook = Callable[[str], None]


class LandingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generation_key: str
    receipt: LandReceipt
    acknowledgement: AcquisitionAcknowledgement


def _clock() -> datetime:
    return datetime.now(UTC)


def _noop_fault_hook(checkpoint: str) -> None:
    del checkpoint


class LandingRunner:
    def __init__(
        self,
        *,
        provider: DestinationProvider,
        ledger: GenerationLedger,
        clock: Callable[[], datetime] = _clock,
        fault_hook: LandingFaultHook = _noop_fault_hook,
    ) -> None:
        self._provider = provider
        self._ledger = ledger
        self._clock = clock
        self._fault_hook = fault_hook

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
        batch_id: str,
        batch_manifest_digest: str,
        candidate_checkpoint_digest: str,
        prior_checkpoint_revision: int,
        contract_digest: str,
        source_binding_ref: str,
        consumer_ref: str,
    ) -> LandingResult:
        """Land one segment and return the exact checkpoint acknowledgement handoff.

        The caller passes the returned acknowledgement to ``AcquisitionRunner.acknowledge``.
        This boundary never advances source state; the acquisition runner retains checkpoint
        compare-and-set, checkpoint receipt, and evidence authority.
        """
        generation_key = raw_generation_key(
            target=target,
            segment_digest=segment.segment_digest,
        )
        existing = self._ledger.load_record(generation_key)
        if existing is not None:
            self._require_exact_receipt(
                receipt=existing.receipt,
                segment=segment,
                target=target,
                idempotency_key=idempotency_key,
            )
            expected_acknowledgement = self._acknowledgement(
                receipt=existing.receipt,
                batch_id=batch_id,
                batch_manifest_digest=batch_manifest_digest,
                candidate_checkpoint_digest=candidate_checkpoint_digest,
                prior_checkpoint_revision=prior_checkpoint_revision,
                contract_digest=contract_digest,
                source_binding_ref=source_binding_ref,
                consumer_ref=consumer_ref,
                acknowledged_at=existing.acknowledgement.acknowledged_at,
            )
            if existing.acknowledgement != expected_acknowledgement:
                raise ProviderError(
                    "generation replay checkpoint authority mismatch",
                    "integrity_failure",
                )
            return LandingResult(
                generation_key=generation_key,
                receipt=existing.receipt,
                acknowledgement=existing.acknowledgement,
            )
        receipt = await self._provider.land(
            segment=segment,
            target=target,
            idempotency_key=idempotency_key,
        )
        self._fault_hook("after_provider_land")
        self._require_exact_receipt(
            receipt=receipt,
            segment=segment,
            target=target,
            idempotency_key=idempotency_key,
        )
        observation = await self._provider.inspect_commit(receipt=receipt)
        if observation.outcome != "committed" or observation.receipt_digest != digest(receipt):
            raise ProviderError(
                "checkpoint requires exact destination commit proof",
                "ambiguous_outcome",
            )
        self._fault_hook("after_commit_inspection")
        acknowledgement = self._acknowledgement(
            receipt=receipt,
            batch_id=batch_id,
            batch_manifest_digest=batch_manifest_digest,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            prior_checkpoint_revision=prior_checkpoint_revision,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            consumer_ref=consumer_ref,
            acknowledged_at=self._clock(),
        )
        self._ledger.record(
            generation_key=generation_key,
            receipt=receipt,
            acknowledgement=acknowledgement,
        )
        self._fault_hook("after_generation_ledger")
        return LandingResult(
            generation_key=generation_key,
            receipt=receipt,
            acknowledgement=acknowledgement,
        )

    @staticmethod
    def _require_exact_receipt(
        *,
        receipt: LandReceipt,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: str,
    ) -> None:
        expected = (
            idempotency_key,
            target.tenant_id,
            target.contract_ref,
            target.contract_revision,
            target.trigger_window,
            target.destination_binding_ref,
            target.logical_object_ref,
            target.table_ref,
            raw_generation_key(target=target, segment_digest=segment.segment_digest),
            segment.segment_digest,
            segment.schema_digest,
            segment.record_count,
        )
        actual = (
            receipt.idempotency_key,
            receipt.tenant_id,
            receipt.contract_ref,
            receipt.contract_revision,
            receipt.trigger_window,
            receipt.destination_binding_ref,
            receipt.logical_object_ref,
            receipt.target_table_ref,
            receipt.generation_id,
            receipt.segment_digest,
            receipt.schema_digest,
            receipt.record_count,
        )
        if actual != expected:
            raise ProviderError("destination receipt authority mismatch", "integrity_failure")

    @staticmethod
    def _acknowledgement(
        *,
        receipt: LandReceipt,
        batch_id: str,
        batch_manifest_digest: str,
        candidate_checkpoint_digest: str,
        prior_checkpoint_revision: int,
        contract_digest: str,
        source_binding_ref: str,
        consumer_ref: str,
        acknowledged_at: datetime,
    ) -> AcquisitionAcknowledgement:
        consumer_receipt_digest = digest(receipt)
        acknowledgement_id = digest(
            {
                "domain": "heinzel-land-acknowledgement-v1",
                "consumer_ref": consumer_ref,
                "batch_id": batch_id,
                "consumer_receipt_digest": consumer_receipt_digest,
            }
        )
        return AcquisitionAcknowledgement(
            acknowledgement_id=acknowledgement_id,
            tenant_id=receipt.tenant_id,
            consumer_ref=consumer_ref,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            batch_id=batch_id,
            batch_manifest_digest=batch_manifest_digest,
            prior_checkpoint_revision=prior_checkpoint_revision,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            consumer_receipt_digest=consumer_receipt_digest,
            acknowledged_at=acknowledged_at,
        )
