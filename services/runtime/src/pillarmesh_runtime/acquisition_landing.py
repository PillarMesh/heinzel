from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from contextlib import suppress
from typing import BinaryIO, Protocol

from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionCheckpointReceipt,
    AcquisitionIntent,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    IdempotencyKey,
    RawGenerationTarget,
    StagedSegment,
    encode_canonical_jsonl,
    staged_segment_digest,
)

from .acquisition import AcquisitionPreparationResult
from .acquisition_errors import AcquisitionIntegrityError
from .landing import LandingResult

type AcquisitionTargetResolver = Callable[[AcquisitionSegmentManifest], RawGenerationTarget]


class AcquisitionSegmentArtifactStore(Protocol):
    def open_verified(self, *, tenant_id: str, artifact_digest: str) -> BinaryIO: ...


class AcquisitionLanding(Protocol):
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
    ) -> LandingResult: ...


class AcquisitionAcknowledger(Protocol):
    def acknowledge(
        self,
        intent: AcquisitionIntent,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt: ...


class AcquisitionLandingResult(ArtifactModel):
    landings: tuple[LandingResult, ...]
    consumer_receipt_digest: str
    checkpoint_receipt: AcquisitionCheckpointReceipt


class AcquisitionLandingCoordinator:
    def __init__(
        self,
        *,
        artifact_store: AcquisitionSegmentArtifactStore,
        landing: AcquisitionLanding,
        target_resolver: AcquisitionTargetResolver,
        acknowledger: AcquisitionAcknowledger,
        consumer_ref: str,
    ) -> None:
        if not consumer_ref:
            raise ValueError("consumer_ref must be non-empty")
        self._artifact_store = artifact_store
        self._landing = landing
        self._target_resolver = target_resolver
        self._acknowledger = acknowledger
        self._consumer_ref = consumer_ref

    async def land_and_acknowledge(
        self,
        *,
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> AcquisitionLandingResult:
        receipt, manifest = self._require_exact_preparation(intent, preparation)
        self._require_exact_manifest_artifact(
            intent.tenant_id,
            receipt.batch_manifest_digest,
            manifest,
        )

        landings: list[LandingResult] = []
        segment_receipts: list[dict[str, str | None]] = []
        for segment_manifest in manifest.segment_manifests:
            staged_segment = self._load_staged_segment(intent.tenant_id, segment_manifest)
            if staged_segment.record_count == 0:
                segment_receipts.append(
                    {
                        "logical_object_ref": segment_manifest.logical_object_ref,
                        "content_digest": segment_manifest.content_digest,
                        "land_receipt_digest": None,
                    }
                )
                continue
            target = self._resolve_exact_target(intent, segment_manifest)
            idempotency_key = self._idempotency_key(
                batch_manifest_digest=receipt.batch_manifest_digest,
                segment=staged_segment,
                target=target,
            )
            landing = await self._landing.land(
                segment=staged_segment,
                target=target,
                idempotency_key=idempotency_key,
                batch_id=receipt.batch_id,
                batch_manifest_digest=receipt.batch_manifest_digest,
                candidate_checkpoint_digest=receipt.candidate_checkpoint_digest,
                prior_checkpoint_revision=receipt.prior_checkpoint_revision,
                contract_digest=intent.contract_digest,
                source_binding_ref=intent.source_binding_ref,
                consumer_ref=self._consumer_ref,
            )
            self._require_exact_landing(
                landing=landing,
                segment=staged_segment,
                target=target,
                idempotency_key=idempotency_key,
                receipt=receipt,
                intent=intent,
            )
            landings.append(landing)
            segment_receipts.append(
                {
                    "logical_object_ref": segment_manifest.logical_object_ref,
                    "content_digest": segment_manifest.content_digest,
                    "land_receipt_digest": digest(landing.receipt),
                }
            )

        consumer_receipt_digest = digest(
            {
                "domain": "pillarmesh-landed-acquisition-batch-v1",
                "batch_manifest_digest": receipt.batch_manifest_digest,
                "segment_receipts": tuple(segment_receipts),
            }
        )
        acknowledged_at = max(
            (landing.receipt.committed_at for landing in landings),
            default=receipt.prepared_at,
        )
        acknowledgement = AcquisitionAcknowledgement(
            acknowledgement_id=digest(
                {
                    "domain": "pillarmesh-landed-acquisition-acknowledgement-v1",
                    "consumer_ref": self._consumer_ref,
                    "batch_id": receipt.batch_id,
                    "consumer_receipt_digest": consumer_receipt_digest,
                }
            ),
            tenant_id=intent.tenant_id,
            consumer_ref=self._consumer_ref,
            contract_digest=intent.contract_digest,
            source_binding_ref=intent.source_binding_ref,
            batch_id=receipt.batch_id,
            batch_manifest_digest=receipt.batch_manifest_digest,
            prior_checkpoint_revision=receipt.prior_checkpoint_revision,
            candidate_checkpoint_digest=receipt.candidate_checkpoint_digest,
            consumer_receipt_digest=consumer_receipt_digest,
            acknowledged_at=acknowledged_at,
        )
        checkpoint_receipt = self._acknowledger.acknowledge(intent, acknowledgement)
        self._require_exact_checkpoint(checkpoint_receipt, acknowledgement)
        return AcquisitionLandingResult(
            landings=tuple(landings),
            consumer_receipt_digest=consumer_receipt_digest,
            checkpoint_receipt=checkpoint_receipt,
        )

    @staticmethod
    def _require_exact_preparation(
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> tuple[AcquisitionPreparedReceipt, AcquisitionBatchManifest]:
        receipt = preparation.prepared_receipt
        manifest = preparation.batch_manifest
        if receipt is None or manifest is None or preparation.governed_outcome is not None:
            raise AcquisitionIntegrityError("acquisition_is_not_prepared_for_land")
        if (
            receipt.tenant_id != intent.tenant_id
            or receipt.intent_key != intent.intent_key
            or receipt.batch_id != manifest.batch_id
            or receipt.batch_manifest_digest != digest(manifest)
            or receipt.prior_checkpoint_revision != intent.prior_checkpoint_revision
            or receipt.candidate_checkpoint_digest != manifest.candidate_checkpoint_digest
            or manifest.intent_key != intent.intent_key
            or manifest.tenant_id != intent.tenant_id
            or manifest.contract_ref != intent.contract_ref
            or manifest.contract_digest != intent.contract_digest
            or manifest.source_binding_ref != intent.source_binding_ref
            or manifest.source_observation_digest != intent.source_observation_digest
            or manifest.acquisition_mode != intent.acquisition_mode
            or manifest.prior_checkpoint_revision != intent.prior_checkpoint_revision
            or manifest.prior_checkpoint_digest != intent.prior_checkpoint_digest
            or tuple(item.logical_object_ref for item in manifest.segment_manifests)
            != intent.object_refs
        ):
            raise AcquisitionIntegrityError("prepared_acquisition_authority_mismatch")
        return receipt, manifest

    def _require_exact_manifest_artifact(
        self,
        tenant_id: str,
        manifest_digest: str,
        manifest: AcquisitionBatchManifest,
    ) -> None:
        payload = self._read_artifact(tenant_id, manifest_digest)
        if (
            payload != canonical_bytes(manifest)
            or hashlib.sha256(payload).hexdigest() != manifest_digest
        ):
            raise AcquisitionIntegrityError("batch_manifest_artifact_mismatch")

    def _load_staged_segment(
        self,
        tenant_id: str,
        manifest: AcquisitionSegmentManifest,
    ) -> StagedSegment:
        payload = self._read_artifact(tenant_id, manifest.content_digest)
        lines = tuple(payload.splitlines())
        try:
            records = tuple(
                AcquisitionRecord.model_validate_json(line, strict=True) for line in lines
            )
            encoded_writer = io.BytesIO()
            encoded = encode_canonical_jsonl(
                records,
                encoded_writer,
                record_ceiling=max(1, manifest.record_count),
                encoded_byte_ceiling=max(1, manifest.encoded_bytes),
            )
        except Exception:
            raise AcquisitionIntegrityError("segment_artifact_invalid") from None
        if (
            hashlib.sha256(payload).hexdigest() != manifest.content_digest
            or len(payload) != manifest.encoded_bytes
            or len(lines) != manifest.record_count
            or encoded_writer.getvalue() != payload
            or encoded.content_digest != manifest.content_digest
            or encoded.record_set_digest != manifest.record_set_digest
            or any(record.logical_object_ref != manifest.logical_object_ref for record in records)
        ):
            raise AcquisitionIntegrityError("segment_artifact_mismatch")
        rows = tuple(_landing_row(line) for line in lines)
        return StagedSegment(
            segment_digest=staged_segment_digest(rows),
            schema_digest=manifest.record_schema_digest,
            record_count=len(rows),
            rows=rows,
        )

    def _read_artifact(self, tenant_id: str, artifact_digest: str) -> bytes:
        try:
            reader = self._artifact_store.open_verified(
                tenant_id=tenant_id,
                artifact_digest=artifact_digest,
            )
            try:
                payload = reader.read()
            finally:
                with suppress(Exception):
                    reader.close()
        except Exception:
            raise AcquisitionIntegrityError("acquisition_artifact_read_failed") from None
        if not isinstance(payload, bytes):
            raise AcquisitionIntegrityError("acquisition_artifact_read_failed")
        return payload

    def _resolve_exact_target(
        self,
        intent: AcquisitionIntent,
        manifest: AcquisitionSegmentManifest,
    ) -> RawGenerationTarget:
        try:
            target = self._target_resolver(manifest)
            target = RawGenerationTarget.model_validate(
                target.model_dump(mode="python"),
                strict=True,
            )
        except Exception:
            raise AcquisitionIntegrityError("destination_target_resolution_failed") from None
        if (
            target.tenant_id != intent.tenant_id
            or target.contract_ref != intent.contract_ref
            or target.logical_object_ref != manifest.logical_object_ref
            or target.schema_digest != manifest.record_schema_digest
        ):
            raise AcquisitionIntegrityError("destination_target_authority_mismatch")
        return target

    def _require_exact_landing(
        self,
        *,
        landing: LandingResult,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
        receipt: AcquisitionPreparedReceipt,
        intent: AcquisitionIntent,
    ) -> None:
        try:
            admitted = LandingResult.model_validate(landing.model_dump(mode="python"), strict=True)
        except Exception:
            raise AcquisitionIntegrityError("landing_receipt_authority_mismatch") from None
        land_receipt = admitted.receipt
        acknowledgement = admitted.acknowledgement
        if (
            admitted.generation_key != land_receipt.generation_id
            or land_receipt.idempotency_key != idempotency_key
            or land_receipt.tenant_id != target.tenant_id
            or land_receipt.contract_ref != target.contract_ref
            or land_receipt.contract_revision != target.contract_revision
            or land_receipt.trigger_window != target.trigger_window
            or land_receipt.destination_binding_ref != target.destination_binding_ref
            or land_receipt.logical_object_ref != target.logical_object_ref
            or land_receipt.target_table_ref != target.table_ref
            or land_receipt.segment_digest != segment.segment_digest
            or land_receipt.schema_digest != segment.schema_digest
            or land_receipt.record_count != segment.record_count
            or acknowledgement.tenant_id != intent.tenant_id
            or acknowledgement.consumer_ref != self._consumer_ref
            or acknowledgement.contract_digest != intent.contract_digest
            or acknowledgement.source_binding_ref != intent.source_binding_ref
            or acknowledgement.batch_id != receipt.batch_id
            or acknowledgement.batch_manifest_digest != receipt.batch_manifest_digest
            or acknowledgement.prior_checkpoint_revision != receipt.prior_checkpoint_revision
            or acknowledgement.candidate_checkpoint_digest != receipt.candidate_checkpoint_digest
            or acknowledgement.consumer_receipt_digest != digest(land_receipt)
        ):
            raise AcquisitionIntegrityError("landing_receipt_authority_mismatch")

    @staticmethod
    def _idempotency_key(
        *,
        batch_manifest_digest: str,
        segment: StagedSegment,
        target: RawGenerationTarget,
    ) -> str:
        return digest(
            {
                "domain": "pillarmesh-acquisition-land-v1",
                "batch_manifest_digest": batch_manifest_digest,
                "segment_digest": segment.segment_digest,
                "target": target,
            }
        )

    @staticmethod
    def _require_exact_checkpoint(
        checkpoint: AcquisitionCheckpointReceipt,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> None:
        try:
            admitted = AcquisitionCheckpointReceipt.model_validate(
                checkpoint.model_dump(mode="python"), strict=True
            )
        except Exception:
            raise AcquisitionIntegrityError("checkpoint_receipt_invalid") from None
        if (
            admitted.tenant_id != acknowledgement.tenant_id
            or admitted.contract_digest != acknowledgement.contract_digest
            or admitted.source_binding_ref != acknowledgement.source_binding_ref
            or admitted.previous_revision != acknowledgement.prior_checkpoint_revision
            or admitted.cursor_digest != acknowledgement.candidate_checkpoint_digest
            or admitted.batch_id != acknowledgement.batch_id
            or admitted.acknowledgement_id != acknowledgement.acknowledgement_id
        ):
            raise AcquisitionIntegrityError("checkpoint_receipt_authority_mismatch")


def _landing_row(record_line: bytes) -> bytes:
    """Project one verified canonical record line onto the flat object LAND stores.

    Generation-scoped product SQL decodes landing fields as top-level JSON keys, so the raw row is
    the record's fields keyed by name. Values are taken from the verified line as already encoded,
    so timestamps and decimals keep their acquisition encoding. The full record, including its key
    and source timestamps, stays in the content-addressed segment artifact the consumer receipt
    binds.
    """
    fields = json.loads(record_line)["fields"]
    return canonical_bytes({field["name"]: field["value"] for field in fields})
