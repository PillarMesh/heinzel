from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime

import pillarmesh_runtime as runtime
import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_evidence import AcquisitionEvidenceReceipt
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionCheckpointReceipt,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    LandReceipt,
    ProviderError,
    RawGenerationTarget,
    StagedSegment,
    acquisition_batch_id,
    acquisition_intent_key,
    encode_canonical_jsonl,
    raw_generation_key,
    staged_segment_digest,
)
from pillarmesh_runtime import AcquisitionPreparationResult

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_TENANT = "tenant-a"
_CONTRACT_REF = "contract:business-data:v1"
_CONTRACT_DIGEST = "c" * 64
_SOURCE_BINDING_REF = "source-a"
_DESTINATION_BINDING_REF = "warehouse-a"
_TRIGGER_WINDOW = "2026-09-14T12:00:00Z/PT1H"


def _intent() -> AcquisitionIntent:
    object_refs = ("accounts", "orders")
    run_intent_ref = "1" * 64
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id=_TENANT,
            run_intent_ref=run_intent_ref,
            contract_digest=_CONTRACT_DIGEST,
            source_binding_ref=_SOURCE_BINDING_REF,
            acquisition_mode="snapshot",
            object_refs=object_refs,
            prior_checkpoint_revision=0,
        ),
        tenant_id=_TENANT,
        run_intent_ref=run_intent_ref,
        contract_ref=_CONTRACT_REF,
        contract_digest=_CONTRACT_DIGEST,
        source_binding_ref=_SOURCE_BINDING_REF,
        source_observation_digest="2" * 64,
        acquisition_mode="snapshot",
        object_refs=object_refs,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=10,
        encoded_byte_ceiling=100_000,
        admitted_at=_NOW,
    )


def _preparation(
    intent: AcquisitionIntent,
    *,
    empty_object_refs: frozenset[str] = frozenset(),
    fields: tuple[AcquisitionFieldValue, ...] | None = None,
    records_by_object: dict[str, tuple[AcquisitionRecord, ...]] | None = None,
) -> tuple[AcquisitionPreparationResult, dict[str, bytes]]:
    artifacts: dict[str, bytes] = {}
    segment_manifests: list[AcquisitionSegmentManifest] = []
    for ordinal, object_ref in enumerate(intent.object_refs):
        records = (
            records_by_object[object_ref]
            if records_by_object is not None and object_ref in records_by_object
            else ()
            if object_ref in empty_object_refs
            else (
                AcquisitionRecord(
                    logical_object_ref=object_ref,
                    record_key=f"{object_ref}-1",
                    source_created_at=_NOW,
                    source_updated_at=_NOW,
                    fields=fields
                    or (AcquisitionFieldValue(name="identifier", value=f"{object_ref}-1"),),
                ),
            )
        )
        writer = io.BytesIO()
        encoded = encode_canonical_jsonl(
            records,
            writer,
            record_ceiling=10,
            encoded_byte_ceiling=100_000,
        )
        artifacts[encoded.content_digest] = writer.getvalue()
        segment_manifests.append(
            AcquisitionSegmentManifest(
                segment_name=f"{ordinal:04d}-{digest(object_ref)}.jsonl",
                logical_object_ref=object_ref,
                record_schema_digest=digest(("identifier",)),
                boundary_digest=str(ordinal + 3) * 64,
                content_digest=encoded.content_digest,
                record_set_digest=encoded.record_set_digest,
                record_count=encoded.record_count,
                encoded_bytes=encoded.encoded_bytes,
            )
        )
    manifests = tuple(segment_manifests)
    candidate_checkpoint_digest = "6" * 64
    batch_id = acquisition_batch_id(
        intent_key=intent.intent_key,
        prior_checkpoint_revision=0,
        candidate_checkpoint_digest=candidate_checkpoint_digest,
        segment_manifests=manifests,
    )
    manifest = AcquisitionBatchManifest(
        batch_id=batch_id,
        intent_key=intent.intent_key,
        tenant_id=intent.tenant_id,
        contract_ref=intent.contract_ref,
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        source_observation_digest=intent.source_observation_digest,
        acquisition_mode=intent.acquisition_mode,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        candidate_checkpoint_digest=candidate_checkpoint_digest,
        segment_manifests=manifests,
        total_record_count=sum(item.record_count for item in manifests),
        total_encoded_bytes=sum(item.encoded_bytes for item in manifests),
        prepared_at=_NOW,
    )
    manifest_payload = canonical_bytes(manifest)
    manifest_digest = digest(manifest)
    artifacts[manifest_digest] = manifest_payload
    receipt = AcquisitionPreparedReceipt(
        prepared_receipt_id="prepared-a",
        tenant_id=intent.tenant_id,
        intent_key=intent.intent_key,
        batch_id=batch_id,
        batch_manifest_digest=manifest_digest,
        prior_checkpoint_revision=0,
        candidate_checkpoint_digest=candidate_checkpoint_digest,
        cursor_version="postgresql-snapshot-v1",
        prepared_at=_NOW,
    )
    return (
        AcquisitionPreparationResult(
            evidence=AcquisitionEvidenceReceipt(
                evidence_id="evidence-a",
                tenant_id=intent.tenant_id,
                run_intent_ref=intent.run_intent_ref,
                contract_ref=intent.contract_ref,
                source_binding_ref=intent.source_binding_ref,
                acquisition_mode=intent.acquisition_mode,
                logical_object_refs=intent.object_refs,
                prepared_receipt_ref=receipt.prepared_receipt_id,
                checkpoint_receipt_ref=None,
                prior_checkpoint_revision=0,
                resulting_checkpoint_revision=None,
                reason_codes=(),
                outcome="prepared",
                created_at=_NOW,
            ),
            prepared_receipt=receipt,
            batch_manifest=manifest,
            governed_outcome=None,
        ),
        artifacts,
    )


class _ArtifactStore:
    def __init__(self, artifacts: dict[str, bytes]) -> None:
        self._artifacts = artifacts

    def open_verified(self, *, tenant_id: str, artifact_digest: str) -> io.BytesIO:
        assert tenant_id == _TENANT
        return io.BytesIO(self._artifacts[artifact_digest])


class _Landing:
    def __init__(
        self,
        *,
        fail_object_ref: str | None = None,
        forge_first_receipt: bool = False,
    ) -> None:
        self._fail_object_ref = fail_object_ref
        self._forge_first_receipt = forge_first_receipt
        self.landed_objects: list[str] = []
        self.segments: list[StagedSegment] = []

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: str,
        batch_id: str,
        batch_manifest_digest: str,
        candidate_checkpoint_digest: str,
        prior_checkpoint_revision: int,
        contract_digest: str,
        source_binding_ref: str,
        consumer_ref: str,
    ) -> runtime.LandingResult:
        if target.logical_object_ref == self._fail_object_ref:
            raise ProviderError("destination unavailable", "transient_unavailable")
        self.landed_objects.append(target.logical_object_ref)
        self.segments.append(segment)
        receipt = LandReceipt(
            receipt_id="receipt-accounts",
            idempotency_key=idempotency_key,
            tenant_id=target.tenant_id,
            contract_ref=target.contract_ref,
            contract_revision=target.contract_revision,
            trigger_window=target.trigger_window,
            destination_binding_ref=target.destination_binding_ref,
            logical_object_ref=target.logical_object_ref,
            target_table_ref=target.table_ref,
            generation_id=raw_generation_key(
                target=target,
                segment_digest=segment.segment_digest,
            ),
            segment_digest=segment.segment_digest,
            schema_digest=segment.schema_digest,
            record_count=segment.record_count,
            provider_commit_ref="commit-accounts",
            committed_at=_NOW,
        )
        acknowledgement = AcquisitionAcknowledgement(
            acknowledgement_id=digest(
                {
                    "domain": "pillarmesh-land-acknowledgement-v1",
                    "consumer_ref": consumer_ref,
                    "batch_id": batch_id,
                    "consumer_receipt_digest": digest(receipt),
                }
            ),
            tenant_id=target.tenant_id,
            consumer_ref=consumer_ref,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            batch_id=batch_id,
            batch_manifest_digest=batch_manifest_digest,
            prior_checkpoint_revision=prior_checkpoint_revision,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            consumer_receipt_digest=digest(receipt),
            acknowledged_at=_NOW,
        )
        result = runtime.LandingResult(
            generation_key=receipt.generation_id,
            receipt=receipt,
            acknowledgement=acknowledgement,
        )
        if self._forge_first_receipt and target.logical_object_ref == "accounts":
            return result.model_copy(
                update={
                    "receipt": receipt.model_copy(
                        update={"logical_object_ref": "some-other-object"}
                    )
                }
            )
        return result


class _RecordingAcknowledger:
    def __init__(self) -> None:
        self.acknowledgements: list[AcquisitionAcknowledgement] = []

    def acknowledge(
        self,
        intent: AcquisitionIntent,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt:
        self.acknowledgements.append(acknowledgement)
        return AcquisitionCheckpointReceipt(
            checkpoint_receipt_id="checkpoint-a",
            tenant_id=intent.tenant_id,
            contract_digest=intent.contract_digest,
            source_binding_ref=intent.source_binding_ref,
            previous_revision=intent.prior_checkpoint_revision,
            committed_revision=intent.prior_checkpoint_revision + 1,
            cursor_digest=acknowledgement.candidate_checkpoint_digest,
            batch_id=acknowledgement.batch_id,
            acknowledgement_id=acknowledgement.acknowledgement_id,
            committed_at=_NOW,
        )


def _target(segment: AcquisitionSegmentManifest) -> RawGenerationTarget:
    return RawGenerationTarget(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        contract_revision=1,
        trigger_window=_TRIGGER_WINDOW,
        destination_binding_ref=_DESTINATION_BINDING_REF,
        logical_object_ref=segment.logical_object_ref,
        table_ref=f"raw_{segment.logical_object_ref}",
        schema_digest=segment.record_schema_digest,
    )


def test_checkpoint_is_not_acknowledged_when_any_batch_segment_fails_land() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    acknowledger = _RecordingAcknowledger()
    coordinator_type = getattr(runtime, "AcquisitionLandingCoordinator", None)
    assert coordinator_type is not None, "runtime must provide the acquisition LAND coordinator"
    coordinator = coordinator_type(
        artifact_store=_ArtifactStore(artifacts),
        landing=_Landing(fail_object_ref="orders"),
        target_resolver=_target,
        acknowledger=acknowledger,
        consumer_ref="land:business-data",
    )

    with pytest.raises(ProviderError, match="destination unavailable"):
        asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert acknowledger.acknowledgements == []


def test_checkpoint_is_not_acknowledged_for_a_landing_receipt_from_another_target() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    acknowledger = _RecordingAcknowledger()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=_Landing(forge_first_receipt=True),
        target_resolver=_target,
        acknowledger=acknowledger,
        consumer_ref="land:business-data",
    )

    with pytest.raises(runtime.AcquisitionIntegrityError, match="landing_receipt_authority"):
        asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert acknowledger.acknowledgements == []


def test_all_batch_segments_land_before_one_exact_checkpoint_acknowledgement() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()
    acknowledger = _RecordingAcknowledger()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=landing,
        target_resolver=_target,
        acknowledger=acknowledger,
        consumer_ref="land:business-data",
    )

    result = asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert landing.landed_objects == ["accounts", "orders"]
    assert len(result.landings) == 2
    assert len(acknowledger.acknowledgements) == 1
    assert acknowledger.acknowledgements[0].consumer_receipt_digest == (
        result.consumer_receipt_digest
    )
    assert result.consumer_receipt_digest not in {
        digest(landing_result.receipt) for landing_result in result.landings
    }
    assert result.checkpoint_receipt.committed_revision == 1


def test_empty_segment_is_bound_into_batch_acknowledgement_without_destination_write() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(
        intent,
        empty_object_refs=frozenset({"accounts"}),
    )
    landing = _Landing()
    acknowledger = _RecordingAcknowledger()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=landing,
        target_resolver=_target,
        acknowledger=acknowledger,
        consumer_ref="land:business-data",
    )

    result = asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert landing.landed_objects == ["orders"]
    assert len(result.landings) == 1
    assert len(acknowledger.acknowledgements) == 1
    assert result.checkpoint_receipt.cursor_digest == "6" * 64


def test_landed_rows_are_flat_field_objects_that_generation_scoped_sql_decodes() -> None:
    # The compiler's generation-scoped decode reads `payload -> '<field>'` (PostgreSQL) and
    # `JSONExtractString(payload, '<field>')` (ClickHouse). LAND must therefore store each record's
    # fields as top-level keys, not the canonical acquisition record with its `fields` array.
    intent = _intent()
    fields = (
        AcquisitionFieldValue(name="identifier", value="orders-1"),
        AcquisitionFieldValue(name="region", value="west"),
        AcquisitionFieldValue(name="revenue", value="10.00"),
        AcquisitionFieldValue(name="quantity", value=3),
        AcquisitionFieldValue(name="refunded", value=False),
        AcquisitionFieldValue(name="note", value=None),
        AcquisitionFieldValue(name="updated_at", value=_NOW),
    )
    preparation, artifacts = _preparation(intent, fields=fields)
    landing = _Landing()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=landing,
        target_resolver=_target,
        acknowledger=_RecordingAcknowledger(),
        consumer_ref="land:business-data",
    )

    asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    segment = landing.segments[0]
    assert preparation.batch_manifest is not None
    record_line = artifacts[preparation.batch_manifest.segment_manifests[0].content_digest]
    record_fields = json.loads(record_line)["fields"]
    record = json.loads(record_line)
    assert json.loads(segment.rows[0]) == {
        "pillarmesh:record": {
            "record_key": record["record_key"],
            "source_created_at": record["source_created_at"],
            "source_updated_at": record["source_updated_at"],
        },
        "identifier": "orders-1",
        "region": "west",
        "revenue": "10.00",
        "quantity": 3,
        "refunded": False,
        "note": None,
        "updated_at": record_fields[6]["value"],
    }
    assert isinstance(record_fields[6]["value"], str)
    assert segment.segment_digest == staged_segment_digest(segment.rows)
    assert segment.record_count == 1


def test_flattened_rows_still_bind_to_the_verified_record_artifact() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    assert preparation.batch_manifest is not None
    content_digest = preparation.batch_manifest.segment_manifests[0].content_digest
    record = json.loads(artifacts[content_digest])
    record["fields"][0]["value"] = "tampered"
    artifacts[content_digest] = canonical_bytes(record) + b"\n"
    landing = _Landing()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=landing,
        target_resolver=_target,
        acknowledger=_RecordingAcknowledger(),
        consumer_ref="land:business-data",
    )

    with pytest.raises(runtime.AcquisitionIntegrityError):
        asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert landing.segments == []


def _land_segments(
    records_by_object: dict[str, tuple[AcquisitionRecord, ...]],
) -> list[StagedSegment]:
    intent = _intent()
    preparation, artifacts = _preparation(intent, records_by_object=records_by_object)
    landing = _Landing()
    asyncio.run(
        runtime.AcquisitionLandingCoordinator(
            artifact_store=_ArtifactStore(artifacts),
            landing=landing,
            target_resolver=_target,
            acknowledger=_RecordingAcknowledger(),
            consumer_ref="land:business-data",
        ).land_and_acknowledge(intent=intent, preparation=preparation)
    )
    return landing.segments


def _record(
    object_ref: str, key: str, *, updated_at: datetime, value: str = "same"
) -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref=object_ref,
        record_key=key,
        source_created_at=None,
        source_updated_at=updated_at,
        fields=(AcquisitionFieldValue(name="identifier", value=value),),
    )


def test_records_with_equal_fields_but_distinct_identity_land_as_distinct_rows() -> None:
    # A Stripe deletion carries its event time only in source_updated_at. Two such records with
    # equal fields must not collapse to one landed row, or two distinct batches would derive the
    # same generation id and collide on the destination's unique generation.
    earlier = datetime(2026, 9, 14, 11, tzinfo=UTC)
    first = _land_segments(
        {
            "accounts": (_record("accounts", "accounts-1", updated_at=earlier),),
            "orders": (_record("orders", "orders-1", updated_at=earlier),),
        }
    )
    second = _land_segments(
        {
            "accounts": (_record("accounts", "accounts-1", updated_at=_NOW),),
            "orders": (_record("orders", "orders-1", updated_at=earlier),),
        }
    )

    assert first[0].rows != second[0].rows
    assert first[0].segment_digest != second[0].segment_digest
    assert first[1].segment_digest == second[1].segment_digest


def test_multi_record_segments_keep_record_order_and_count() -> None:
    records = tuple(
        _record("accounts", f"accounts-{index}", updated_at=_NOW, value=f"value-{index}")
        for index in range(3)
    )
    (accounts, _orders) = _land_segments({"accounts": records})

    assert accounts.record_count == 3
    assert [json.loads(row)["identifier"] for row in accounts.rows] == [
        "value-0",
        "value-1",
        "value-2",
    ]
    assert [json.loads(row)["pillarmesh:record"]["record_key"] for row in accounts.rows] == [
        "accounts-0",
        "accounts-1",
        "accounts-2",
    ]


def test_a_field_named_like_the_reserved_identity_key_is_refused_before_land() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(
        intent,
        fields=(AcquisitionFieldValue(name="pillarmesh:record", value="forged"),),
    )
    landing = _Landing()
    coordinator = runtime.AcquisitionLandingCoordinator(
        artifact_store=_ArtifactStore(artifacts),
        landing=landing,
        target_resolver=_target,
        acknowledger=_RecordingAcknowledger(),
        consumer_ref="land:business-data",
    )

    with pytest.raises(runtime.AcquisitionIntegrityError, match="reserved"):
        asyncio.run(coordinator.land_and_acknowledge(intent=intent, preparation=preparation))

    assert landing.segments == []
