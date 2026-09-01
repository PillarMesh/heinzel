from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import Literal

from pillarmesh_contract_model import digest
from pillarmesh_evidence import AcquisitionEvidenceReceipt
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionBoundary,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    CompletedAcquisition,
    ProviderObservation,
    SourceConformanceResult,
    SourceConformanceScenario,
    SourceObservationRequest,
    acquisition_batch_id,
    acquisition_intent_key,
    encode_canonical_jsonl,
    exercise_source_provider,
    verify_private_cursor_containment,
    verify_source_replay,
)
from pillarmesh_provider_sdk.acquisition_models import AcquisitionScalar
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_CAPABILITIES = ("incremental", "reconciliation", "snapshot")


@dataclass(frozen=True)
class SourceAcquisitionFixture:
    scenario: SourceConformanceScenario
    observation: AcquisitionSourceObservation
    records: tuple[AcquisitionRecord, ...]
    candidate_cursor_payload: bytes


@dataclass(frozen=True)
class SharedSourceConformanceResult:
    provider_result: SourceConformanceResult
    batch_manifest: AcquisitionBatchManifest
    prepared_receipt: AcquisitionPreparedReceipt
    acknowledgement: AcquisitionAcknowledgement
    checkpoint_receipt: AcquisitionCheckpointReceipt
    public_evidence_receipts: tuple[AcquisitionEvidenceReceipt, ...]


class DestinationProviderResolutionGuard:
    def __init__(self) -> None:
        self._resolved_provider_refs: list[str] = []

    @property
    def resolved_provider_refs(self) -> tuple[str, ...]:
        return tuple(self._resolved_provider_refs)

    def __call__(self, provider_ref: str) -> object:
        self._resolved_provider_refs.append(provider_ref)
        raise AssertionError("destination_provider_resolution_forbidden")


class StrictAcknowledgementError(RuntimeError):
    pass


class StrictAcknowledgementConsumer:
    consumer_ref = "strict-source-test-consumer"

    def acknowledge(
        self,
        prepared: AcquisitionPreparedReceipt,
        manifest: AcquisitionBatchManifest,
    ) -> AcquisitionAcknowledgement:
        try:
            prepared = AcquisitionPreparedReceipt.model_validate(
                prepared.model_dump(warnings="none")
            )
            manifest = AcquisitionBatchManifest.model_validate(manifest.model_dump(warnings="none"))
        except (TypeError, ValueError):
            raise StrictAcknowledgementError("malformed_acknowledgement_input") from None
        prepared_linkage = (
            prepared.tenant_id,
            prepared.intent_key,
            prepared.batch_id,
            prepared.batch_manifest_digest,
            prepared.prior_checkpoint_revision,
            prepared.candidate_checkpoint_digest,
        )
        expected_linkage = (
            manifest.tenant_id,
            manifest.intent_key,
            manifest.batch_id,
            digest(manifest),
            manifest.prior_checkpoint_revision,
            manifest.candidate_checkpoint_digest,
        )
        if prepared_linkage != expected_linkage:
            raise StrictAcknowledgementError("acknowledgement_authority_mismatch")
        return AcquisitionAcknowledgement(
            acknowledgement_id="acknowledgement-opaque-1",
            tenant_id=prepared.tenant_id,
            consumer_ref=self.consumer_ref,
            contract_digest=manifest.contract_digest,
            source_binding_ref=manifest.source_binding_ref,
            batch_id=prepared.batch_id,
            batch_manifest_digest=prepared.batch_manifest_digest,
            prior_checkpoint_revision=prepared.prior_checkpoint_revision,
            candidate_checkpoint_digest=prepared.candidate_checkpoint_digest,
            consumer_receipt_digest=digest(
                {
                    "domain": "strict-source-consumer-v1",
                    "batch_manifest_digest": prepared.batch_manifest_digest,
                }
            ),
            acknowledged_at=_NOW,
        )


class _FakeSourceSession:
    def __init__(self, fixture: SourceAcquisitionFixture) -> None:
        self._records = fixture.records
        self._completion = _completion(fixture)
        self._index = 0
        self.abort_count = 0
        self.completion_count = 0

    def __iter__(self) -> _FakeSourceSession:
        return self

    def __next__(self) -> AcquisitionRecord:
        if self._index == len(self._records):
            raise StopIteration
        record = self._records[self._index]
        self._index += 1
        return record

    def complete(self) -> CompletedAcquisition:
        self.completion_count += 1
        if self._index != len(self._records):
            raise AcquisitionSessionIncomplete()
        return self._completion

    def abort(self) -> None:
        self.abort_count += 1


class FakeSourceProvider:
    def __init__(
        self,
        fixture: SourceAcquisitionFixture,
        *,
        replay_fixture: SourceAcquisitionFixture | None = None,
    ) -> None:
        self._fixture = fixture
        self._replay_fixture = replay_fixture or fixture
        self.opened_sessions: list[_FakeSourceSession] = []

    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        if request != self._fixture.scenario.request:
            raise AssertionError("unexpected_source_observation_request")
        return self._fixture.observation

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _FakeSourceSession:
        scenario = self._fixture.scenario
        expected_intent = scenario.intent.model_dump(
            exclude={"record_ceiling", "encoded_byte_ceiling"}
        )
        actual_intent = intent.model_dump(exclude={"record_ceiling", "encoded_byte_ceiling"})
        if (
            actual_intent != expected_intent
            or schemas != scenario.schemas
            or private_cursor != scenario.private_cursor
        ):
            raise AssertionError("unexpected_source_acquisition_authority")
        session_fixture = self._fixture if not self.opened_sessions else self._replay_fixture
        session = _FakeSourceSession(session_fixture)
        self.opened_sessions.append(session)
        return session


def _schema(
    logical_object_ref: str,
    fields: tuple[AcquisitionField, ...],
    *,
    key_name: str,
) -> AcquisitionObjectSchema:
    return AcquisitionObjectSchema(
        logical_object_ref=logical_object_ref,
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=(key_name,),
        source_updated_at_field="updated_at",
    )


def _record(
    schema: AcquisitionObjectSchema,
    *,
    record_key: str,
    values: tuple[AcquisitionScalar, ...],
) -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref=schema.logical_object_ref,
        record_key=record_key,
        source_created_at=None,
        source_updated_at=_NOW,
        fields=tuple(
            AcquisitionFieldValue(name=field.name, value=value)
            for field, value in zip(schema.fields, values, strict=True)
        ),
    )


def _observation(
    *,
    provider_kind: AcquisitionProviderKind,
    schemas: tuple[AcquisitionObjectSchema, ...],
) -> AcquisitionSourceObservation:
    return AcquisitionSourceObservation(
        tenant_id="tenant-a",
        source_binding_ref="source-binding-a",
        provider_kind=provider_kind,
        object_observations=tuple(
            AcquisitionObjectObservation(
                logical_object_ref=schema.logical_object_ref,
                provider_observation=ProviderObservation(
                    provider=provider_kind,
                    connection_handle="source-connection-opaque",
                    object_identity="private-provider-object-id-collision",
                    object_kind="base_table",
                    schema_digest=schema.schema_digest,
                    columns=(),
                    key_name=schema.record_key_fields[0],
                    key_type="TEXT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=_CAPABILITIES,
                    observed_at=_NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            )
            for schema in schemas
        ),
    )


def _completion(fixture: SourceAcquisitionFixture) -> CompletedAcquisition:
    candidate_digest = hashlib.sha256(fixture.candidate_cursor_payload).hexdigest()
    boundaries = tuple(
        AcquisitionBoundary(
            logical_object_ref=schema.logical_object_ref,
            acquisition_mode=fixture.scenario.intent.acquisition_mode,
            schema_digest=schema.schema_digest,
            lower_cursor_digest=None,
            upper_cursor_digest=digest(
                {
                    "logical_object_ref": schema.logical_object_ref,
                    "candidate_cursor_digest": candidate_digest,
                }
            ),
            query_shape_digest=digest(
                {
                    "logical_object_ref": schema.logical_object_ref,
                    "fields": tuple(field.name for field in schema.fields),
                }
            ),
            snapshot_identity_digest=digest(
                {"logical_object_ref": schema.logical_object_ref, "snapshot": "fixture"}
            ),
            key_range_digest=digest(
                {"logical_object_ref": schema.logical_object_ref, "range": "fixture"}
            ),
            private_boundary_ref=f"private-boundary:{schema.logical_object_ref}",
            record_count=sum(
                record.logical_object_ref == schema.logical_object_ref for record in fixture.records
            ),
            opened_at=_NOW,
            closed_at=_NOW,
        )
        for schema in fixture.scenario.schemas
    )
    return CompletedAcquisition(
        boundaries=boundaries,
        cursor_version=f"{fixture.scenario.expected_provider_kind}-fixture-v1",
        candidate_cursor_payload=fixture.candidate_cursor_payload,
    )


def build_plan4a_application_source_fixture(
    *,
    provider_kind: AcquisitionProviderKind,
) -> SourceAcquisitionFixture:
    schemas = (
        _schema(
            "account-segments",
            (
                AcquisitionField(name="account_id", value_type="string", nullable=False),
                AcquisitionField(name="segment", value_type="string", nullable=False),
                AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
            ),
            key_name="account_id",
        ),
        _schema(
            "orders",
            (
                AcquisitionField(name="order_id", value_type="string", nullable=False),
                AcquisitionField(name="amount", value_type="decimal", nullable=False),
                AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
            ),
            key_name="order_id",
        ),
        _schema(
            "subscriptions",
            (
                AcquisitionField(name="subscription_id", value_type="string", nullable=False),
                AcquisitionField(
                    name="lifecycle_status",
                    value_type="string",
                    nullable=False,
                ),
                AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
            ),
            key_name="subscription_id",
        ),
    )
    records = (
        _record(
            schemas[0],
            record_key="private-account-id-7",
            values=("private-account-id-7", "enterprise", _NOW),
        ),
        _record(
            schemas[1],
            record_key="private-order-id-7",
            values=("private-order-id-7", Decimal("125.00"), _NOW),
        ),
        _record(
            schemas[2],
            record_key="private-subscription-id-7",
            values=("private-subscription-id-7", "active", _NOW),
        ),
    )
    observation = _observation(provider_kind=provider_kind, schemas=schemas)
    object_refs = tuple(schema.logical_object_ref for schema in schemas)
    run_intent_ref = "1" * 64
    contract_digest = "2" * 64
    acquisition_mode: Literal["snapshot"] = "snapshot"
    intent = AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref="source-binding-a",
            acquisition_mode=acquisition_mode,
            object_refs=object_refs,
            prior_checkpoint_revision=0,
        ),
        tenant_id="tenant-a",
        run_intent_ref=run_intent_ref,
        contract_ref="contract-a-v1",
        contract_digest=contract_digest,
        source_binding_ref="source-binding-a",
        source_observation_digest=digest(observation),
        acquisition_mode=acquisition_mode,
        object_refs=object_refs,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=100,
        encoded_byte_ceiling=100_000,
        admitted_at=_NOW,
    )
    candidate_cursor_payload = b"\x00private-source-cursor-canary\xff"
    scenario = SourceConformanceScenario(
        request=SourceObservationRequest(
            tenant_id=intent.tenant_id,
            source_binding_ref=intent.source_binding_ref,
            object_refs=object_refs,
        ),
        intent=intent,
        schemas=schemas,
        private_cursor=None,
        expected_provider_kind=provider_kind,
        expected_connection_handle="source-connection-opaque",
        expected_capabilities=_CAPABILITIES,
        observation_not_before=_NOW,
        expected_record_identities=tuple(
            (record.logical_object_ref, record.record_key) for record in records
        ),
    )
    return SourceAcquisitionFixture(
        scenario=scenario,
        observation=observation,
        records=records,
        candidate_cursor_payload=candidate_cursor_payload,
    )


def _batch_manifest(
    fixture: SourceAcquisitionFixture,
    provider_result: SourceConformanceResult,
) -> AcquisitionBatchManifest:
    boundaries = {
        boundary.logical_object_ref: boundary for boundary in provider_result.completion.boundaries
    }
    segment_manifests: list[AcquisitionSegmentManifest] = []
    for ordinal, schema in enumerate(fixture.scenario.schemas):
        records = tuple(
            record
            for record in provider_result.records
            if record.logical_object_ref == schema.logical_object_ref
        )
        encoding = encode_canonical_jsonl(
            records,
            BytesIO(),
            record_ceiling=fixture.scenario.intent.record_ceiling,
            encoded_byte_ceiling=fixture.scenario.intent.encoded_byte_ceiling,
        )
        object_ref_digest = digest({"logical_object_ref": schema.logical_object_ref})
        segment_manifests.append(
            AcquisitionSegmentManifest(
                segment_name=f"{ordinal:04d}-{object_ref_digest}.jsonl",
                logical_object_ref=schema.logical_object_ref,
                record_schema_digest=schema.schema_digest,
                boundary_digest=digest(boundaries[schema.logical_object_ref]),
                content_digest=encoding.content_digest,
                record_set_digest=encoding.record_set_digest,
                record_count=encoding.record_count,
                encoded_bytes=encoding.encoded_bytes,
            )
        )
    segments = tuple(segment_manifests)
    intent = fixture.scenario.intent
    batch_id = acquisition_batch_id(
        intent_key=intent.intent_key,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest=provider_result.candidate_cursor_digest,
        segment_manifests=segments,
    )
    return AcquisitionBatchManifest(
        batch_id=batch_id,
        intent_key=intent.intent_key,
        tenant_id=intent.tenant_id,
        contract_ref=intent.contract_ref,
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        source_observation_digest=intent.source_observation_digest,
        acquisition_mode=intent.acquisition_mode,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        prior_checkpoint_digest=intent.prior_checkpoint_digest,
        candidate_checkpoint_digest=provider_result.candidate_cursor_digest,
        segment_manifests=segments,
        total_record_count=sum(segment.record_count for segment in segments),
        total_encoded_bytes=sum(segment.encoded_bytes for segment in segments),
        prepared_at=_NOW,
    )


def run_shared_source_conformance(
    provider: FakeSourceProvider,
    fixture: SourceAcquisitionFixture,
    *,
    destination_provider_resolver: DestinationProviderResolutionGuard,
) -> SharedSourceConformanceResult:
    provider_result = verify_source_replay(provider, fixture.scenario)
    if destination_provider_resolver.resolved_provider_refs:
        raise AssertionError("destination_provider_resolution_forbidden")
    manifest = _batch_manifest(fixture, provider_result)
    prepared = AcquisitionPreparedReceipt(
        prepared_receipt_id="prepared-receipt-opaque-1",
        tenant_id=manifest.tenant_id,
        intent_key=manifest.intent_key,
        batch_id=manifest.batch_id,
        batch_manifest_digest=digest(manifest),
        prior_checkpoint_revision=manifest.prior_checkpoint_revision,
        candidate_checkpoint_digest=manifest.candidate_checkpoint_digest,
        cursor_version=provider_result.completion.cursor_version,
        prepared_at=_NOW,
    )
    acknowledgement = StrictAcknowledgementConsumer().acknowledge(prepared, manifest)
    checkpoint = AcquisitionCheckpointReceipt(
        checkpoint_receipt_id="checkpoint-receipt-opaque-1",
        tenant_id=acknowledgement.tenant_id,
        contract_digest=acknowledgement.contract_digest,
        source_binding_ref=acknowledgement.source_binding_ref,
        previous_revision=prepared.prior_checkpoint_revision,
        committed_revision=prepared.prior_checkpoint_revision + 1,
        cursor_digest=prepared.candidate_checkpoint_digest,
        batch_id=prepared.batch_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        committed_at=_NOW,
    )
    public_evidence = (
        AcquisitionEvidenceReceipt(
            evidence_id="evidence-prepared-opaque-1",
            tenant_id=manifest.tenant_id,
            run_intent_ref=fixture.scenario.intent.run_intent_ref,
            contract_ref=manifest.contract_ref,
            source_binding_ref=manifest.source_binding_ref,
            acquisition_mode=manifest.acquisition_mode,
            logical_object_refs=fixture.scenario.intent.object_refs,
            prepared_receipt_ref=prepared.prepared_receipt_id,
            checkpoint_receipt_ref=None,
            prior_checkpoint_revision=prepared.prior_checkpoint_revision,
            resulting_checkpoint_revision=None,
            reason_codes=(),
            outcome="prepared",
            created_at=_NOW,
        ),
        AcquisitionEvidenceReceipt(
            evidence_id="evidence-acknowledged-opaque-1",
            tenant_id=manifest.tenant_id,
            run_intent_ref=fixture.scenario.intent.run_intent_ref,
            contract_ref=manifest.contract_ref,
            source_binding_ref=manifest.source_binding_ref,
            acquisition_mode=manifest.acquisition_mode,
            logical_object_refs=fixture.scenario.intent.object_refs,
            prepared_receipt_ref=prepared.prepared_receipt_id,
            checkpoint_receipt_ref=checkpoint.checkpoint_receipt_id,
            prior_checkpoint_revision=prepared.prior_checkpoint_revision,
            resulting_checkpoint_revision=checkpoint.committed_revision,
            reason_codes=(),
            outcome="acknowledged",
            created_at=_NOW,
        ),
    )
    verify_private_cursor_containment(
        public_evidence,
        private_values=(
            fixture.scenario.private_cursor,
            fixture.candidate_cursor_payload,
        ),
    )
    return SharedSourceConformanceResult(
        provider_result=provider_result,
        batch_manifest=manifest,
        prepared_receipt=prepared,
        acknowledgement=acknowledgement,
        checkpoint_receipt=checkpoint,
        public_evidence_receipts=public_evidence,
    )


def exercise_source_ceiling_refusal(
    provider: FakeSourceProvider,
    fixture: SourceAcquisitionFixture,
    *,
    limit_kind: Literal["records", "encoded_bytes"],
    destination_provider_resolver: DestinationProviderResolutionGuard,
) -> SourceConformanceResult:
    intent_update = (
        {"record_ceiling": 2} if limit_kind == "records" else {"encoded_byte_ceiling": 1}
    )
    narrowed_scenario = replace(
        fixture.scenario,
        intent=fixture.scenario.intent.model_copy(update=intent_update),
    )
    result = exercise_source_provider(provider, narrowed_scenario)
    if destination_provider_resolver.resolved_provider_refs:
        raise AssertionError("destination_provider_resolution_forbidden")
    return result
