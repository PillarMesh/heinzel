from __future__ import annotations

import asyncio
import io
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
)
from pillarmesh_runtime import AcquisitionPreparationResult
from pydantic import ValidationError

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
) -> tuple[AcquisitionPreparationResult, dict[str, bytes]]:
    artifacts: dict[str, bytes] = {}
    segment_manifests: list[AcquisitionSegmentManifest] = []
    for ordinal, object_ref in enumerate(intent.object_refs):
        records = (
            ()
            if object_ref in empty_object_refs
            else (
                AcquisitionRecord(
                    logical_object_ref=object_ref,
                    record_key=f"{object_ref}-1",
                    source_created_at=_NOW,
                    source_updated_at=_NOW,
                    fields=(AcquisitionFieldValue(name="identifier", value=f"{object_ref}-1"),),
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
        self.targets: list[RawGenerationTarget] = []

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
        self.targets.append(target)
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


def _routing(
    *,
    table_refs: dict[str, str] | None = None,
    contract_ref: str = _CONTRACT_REF,
    tenant_id: str = _TENANT,
) -> runtime.AcquisitionDestinationRouting:
    routed = table_refs or {"accounts": "raw_accounts", "orders": "raw_orders"}
    return runtime.AcquisitionDestinationRouting(
        tenant_id=tenant_id,
        contract_ref=contract_ref,
        destination_binding_ref=_DESTINATION_BINDING_REF,
        routes=tuple(
            runtime.AcquisitionObjectRoute(logical_object_ref=object_ref, table_ref=table_ref)
            for object_ref, table_ref in routed.items()
        ),
    )


def _authority(
    consumer_ref: str = "land:business-data", *, contract_digest: str = _CONTRACT_DIGEST
) -> runtime.LandingContractAuthority:
    return runtime.LandingContractAuthority(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT_REF,
        contract_digest=contract_digest,
        revision=4,
        acknowledgement_consumer_ref=consumer_ref,
    )


def _composed(
    artifacts: dict[str, bytes],
    landing: _Landing,
    acknowledger: _RecordingAcknowledger,
    *,
    routing: runtime.AcquisitionDestinationRouting | None = None,
    authority: runtime.LandingContractAuthority | None = None,
) -> runtime.AcquisitionLandingApplication:
    resolved = authority or _authority()
    return runtime.compose_acquisition_landing(
        contract_resolver=lambda tenant_id, contract_ref: resolved,
        routing=routing or _routing(),
        landing=landing,
        artifact_store=_ArtifactStore(artifacts),
        acknowledger=acknowledger,
    )


def test_composed_landing_routes_each_object_to_its_table_under_the_contract_s_authority() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()
    acknowledger = _RecordingAcknowledger()

    result = asyncio.run(
        _composed(artifacts, landing, acknowledger).land(
            trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation
        )
    )

    assert landing.landed_objects == ["accounts", "orders"]
    assert [target.table_ref for target in landing.targets] == ["raw_accounts", "raw_orders"]
    assert {target.contract_revision for target in landing.targets} == {4}
    assert {target.trigger_window for target in landing.targets} == {_TRIGGER_WINDOW}
    assert {target.destination_binding_ref for target in landing.targets} == {
        _DESTINATION_BINDING_REF
    }
    assert len(acknowledger.acknowledgements) == 1
    assert result.checkpoint_receipt.committed_revision == 1


def test_composed_landing_acknowledges_as_the_consumer_the_contract_names() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    acknowledger = _RecordingAcknowledger()

    asyncio.run(
        _composed(
            artifacts,
            _Landing(),
            acknowledger,
            authority=_authority("land:another-consumer"),
        ).land(trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation)
    )

    assert acknowledger.acknowledgements[0].consumer_ref == "land:another-consumer"


def test_composed_landing_refuses_an_object_the_routing_does_not_cover() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()

    with pytest.raises(runtime.AcquisitionContractError, match="destination_routing_incomplete"):
        asyncio.run(
            _composed(
                artifacts,
                landing,
                _RecordingAcknowledger(),
                routing=_routing(table_refs={"accounts": "raw_accounts"}),
            ).land(trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation)
        )

    assert landing.landed_objects == []


def test_composed_landing_refuses_routing_that_names_another_contract() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()

    with pytest.raises(runtime.AcquisitionContractError, match="destination_routing_authority"):
        asyncio.run(
            _composed(
                artifacts,
                landing,
                _RecordingAcknowledger(),
                routing=_routing(contract_ref="contract:somebody-else"),
            ).land(trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation)
        )

    assert landing.landed_objects == []


def test_composed_landing_refuses_an_authority_for_a_different_contract_revision() -> None:
    """A batch lands under the contract it was prepared against, never a newer activation.

    The resolver answers with whatever is activated now. Landing a batch prepared under an
    earlier contract would stamp a revision it was never validated against into the generation
    key and the LAND receipt, so the batch is refused instead.
    """
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()

    with pytest.raises(runtime.AcquisitionContractError, match="contract_authority_drift"):
        asyncio.run(
            _composed(
                artifacts,
                landing,
                _RecordingAcknowledger(),
                authority=_authority(contract_digest="d" * 64),
            ).land(trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation)
        )

    assert landing.landed_objects == []


def test_composed_landing_refuses_routing_that_names_another_tenant() -> None:
    intent = _intent()
    preparation, artifacts = _preparation(intent)
    landing = _Landing()

    with pytest.raises(runtime.AcquisitionContractError, match="destination_routing_authority"):
        asyncio.run(
            _composed(
                artifacts,
                landing,
                _RecordingAcknowledger(),
                routing=_routing(tenant_id="tenant-somebody-else"),
            ).land(trigger_window=_TRIGGER_WINDOW, intent=intent, preparation=preparation)
        )

    assert landing.landed_objects == []


def test_a_routing_cannot_be_mutated_after_it_is_stated() -> None:
    routing = _routing()

    with pytest.raises(ValidationError):
        routing.routes[0].table_ref = "raw_elsewhere"  # type: ignore[misc]
    assert {route.table_ref for route in routing.routes} == {"raw_accounts", "raw_orders"}
