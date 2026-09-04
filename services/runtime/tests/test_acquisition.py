from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import BinaryIO, Literal

import pytest
from pillarmesh_connection_broker import (
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_evidence import AcquisitionEvidenceReceipt
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBoundary,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionNoValidPlan,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    CompletedAcquisition,
    ProviderObservation,
    ResynchronizationRequired,
    acquisition_intent_key,
)
from pillarmesh_provider_sdk.acquisition_models import (
    AcquisitionMode,
    AcquisitionScalar,
    AcquisitionValueType,
)
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind
from pillarmesh_runtime import (
    AcquisitionAuthorizationError,
    AcquisitionCeilingExceeded,
    AcquisitionContractError,
    AcquisitionDriftError,
    AcquisitionEvidenceWriter,
    AcquisitionIntegrityError,
    AcquisitionOwnershipError,
    AcquisitionPreparationResult,
    AcquisitionRunner,
    AcquisitionStaleRevision,
    AcquisitionThrottledError,
    AcquisitionTransientError,
    ActivatedAcquisitionContract,
)
from pillarmesh_state import (
    AcquisitionArtifactStoreError,
    AcquisitionStateConflictError,
    AcquisitionStateNotFoundError,
    PreparedAcquisitionState,
    PreparedAcquisitionStateStatus,
    SourceCheckpointState,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
TENANT = "tenant-a"
CONTRACT_REF = "contract:orders:v1"
CONTRACT_DIGEST = "c" * 64
BINDING_REF = "source-binding:orders"
OBSERVATION_REF = "source-observation:orders"
RUN_INTENT_REF = "1" * 64


def _schema() -> AcquisitionObjectSchema:
    fields = (
        AcquisitionField(name="order_id", value_type="integer", nullable=False),
        AcquisitionField(name="amount", value_type="decimal", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    return AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("order_id",),
        source_updated_at_field="updated_at",
    )


def _record(record_key: str = "1", *, amount: Decimal = Decimal("10.50")) -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="orders",
        record_key=record_key,
        source_created_at=None,
        source_updated_at=NOW,
        fields=(
            AcquisitionFieldValue(name="order_id", value=int(record_key)),
            AcquisitionFieldValue(name="amount", value=amount),
            AcquisitionFieldValue(name="updated_at", value=NOW),
        ),
    )


def _single_key_schema(value_type: AcquisitionValueType) -> AcquisitionObjectSchema:
    fields = (AcquisitionField(name="key", value_type=value_type, nullable=False),)
    return AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("key",),
        source_updated_at_field=None,
    )


def _key_record(record_key: str, value: AcquisitionScalar) -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="orders",
        record_key=record_key,
        source_created_at=None,
        source_updated_at=None,
        fields=(AcquisitionFieldValue(name="key", value=value),),
    )


def _observation(
    schema: AcquisitionObjectSchema | tuple[AcquisitionObjectSchema, ...],
    *,
    provider_kind: str = "postgresql",
    capabilities: tuple[str, ...] = ("snapshot",),
) -> AcquisitionSourceObservation:
    schemas = schema if isinstance(schema, tuple) else (schema,)
    return AcquisitionSourceObservation(
        tenant_id=TENANT,
        source_binding_ref=BINDING_REF,
        provider_kind=provider_kind,  # type: ignore[arg-type]
        object_observations=tuple(
            AcquisitionObjectObservation(
                logical_object_ref=item.logical_object_ref,
                provider_observation=ProviderObservation(
                    provider=provider_kind,  # type: ignore[arg-type]
                    connection_handle="source-handle",
                    object_identity=f"private-{item.logical_object_ref}-identity",
                    object_kind="base_table",
                    schema_digest=item.schema_digest,
                    columns=(),
                    key_name=item.record_key_fields[0],
                    key_type=next(
                        field.value_type
                        for field in item.fields
                        if field.name == item.record_key_fields[0]
                    ),
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=capabilities,
                    observed_at=NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            )
            for item in schemas
        ),
    )


def _contract(
    schema: AcquisitionObjectSchema | tuple[AcquisitionObjectSchema, ...],
    observation: AcquisitionSourceObservation,
    **changes: object,
) -> ActivatedAcquisitionContract:
    schemas = schema if isinstance(schema, tuple) else (schema,)
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "contract_ref": CONTRACT_REF,
        "contract_digest": CONTRACT_DIGEST,
        "source_binding_ref": BINDING_REF,
        "source_binding_revision": 3,
        "credential_revision": 1,
        "acknowledgement_consumer_ref": "strict-consumer",
        "capability_profile_digest": "a" * 64,
        "source_observation_ref": OBSERVATION_REF,
        "source_observation_digest": digest(observation),
        "lifecycle_state": "activated",
        "acquisition_modes": ("snapshot",),
        "object_schemas": schemas,
        "record_ceiling": 10,
        "encoded_byte_ceiling": 100_000,
    }
    values.update(changes)
    return ActivatedAcquisitionContract.model_validate(values)


def _binding(*, provider_kind: str = "postgresql") -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id=BINDING_REF,
        tenant_id=TENANT,
        provider_kind=provider_kind,  # type: ignore[arg-type]
        connection_handle="source-handle",
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.READY,
        approved_object_refs=("orders",),
        capability_profile_digest="a" * 64,
        source_observation_ref=OBSERVATION_REF,
        credential_revision=1,
        revision=3,
        created_at=NOW,
        updated_at=NOW,
    )


def _intent(
    observation: AcquisitionSourceObservation,
    **changes: object,
) -> AcquisitionIntent:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "run_intent_ref": RUN_INTENT_REF,
        "contract_ref": CONTRACT_REF,
        "contract_digest": CONTRACT_DIGEST,
        "source_binding_ref": BINDING_REF,
        "source_observation_digest": digest(observation),
        "acquisition_mode": "snapshot",
        "object_refs": ("orders",),
        "prior_checkpoint_revision": 0,
        "prior_checkpoint_digest": None,
        "record_ceiling": 10,
        "encoded_byte_ceiling": 100_000,
        "admitted_at": NOW,
    }
    values.update(changes)
    prior_checkpoint_revision = values["prior_checkpoint_revision"]
    assert isinstance(prior_checkpoint_revision, int)
    values["intent_key"] = acquisition_intent_key(
        tenant_id=str(values["tenant_id"]),
        run_intent_ref=str(values["run_intent_ref"]),
        contract_digest=str(values["contract_digest"]),
        source_binding_ref=str(values["source_binding_ref"]),
        acquisition_mode=values["acquisition_mode"],  # type: ignore[arg-type]
        object_refs=values["object_refs"],  # type: ignore[arg-type]
        prior_checkpoint_revision=prior_checkpoint_revision,
    )
    return AcquisitionIntent.model_validate(values)


class Session:
    def __init__(
        self,
        records: tuple[AcquisitionRecord, ...],
        schema: AcquisitionObjectSchema | tuple[AcquisitionObjectSchema, ...],
        *,
        incomplete: bool = False,
        lower_cursor_digest: str | None = None,
        boundary_record_count: int | None = None,
        stream_error: Exception | None = None,
        abort_error: Exception | None = None,
        cursor_version: str = "postgresql-compound-v1",
        acquisition_mode: AcquisitionMode = "snapshot",
    ) -> None:
        self._records = records
        self._schemas = schema if isinstance(schema, tuple) else (schema,)
        self._incomplete = incomplete
        self._lower_cursor_digest = lower_cursor_digest
        self._boundary_record_count = boundary_record_count
        self._stream_error = stream_error
        self._abort_error = abort_error
        self._cursor_version = cursor_version
        self._acquisition_mode = acquisition_mode
        self.aborted = False
        self.exhausted = False

    def __iter__(self) -> Iterator[AcquisitionRecord]:
        yield from self._records
        if self._stream_error is not None:
            raise self._stream_error
        self.exhausted = True

    def complete(self) -> CompletedAcquisition:
        if self._incomplete or not self.exhausted:
            raise AcquisitionSessionIncomplete
        return CompletedAcquisition(
            boundaries=tuple(
                AcquisitionBoundary(
                    logical_object_ref=schema.logical_object_ref,
                    acquisition_mode=self._acquisition_mode,
                    schema_digest=schema.schema_digest,
                    lower_cursor_digest=self._lower_cursor_digest,
                    upper_cursor_digest=hashlib.sha256(b"candidate-cursor").hexdigest(),
                    query_shape_digest="2" * 64,
                    snapshot_identity_digest="3" * 64,
                    key_range_digest="4" * 64,
                    private_boundary_ref=f"private-boundary-ref:{schema.logical_object_ref}",
                    record_count=(
                        sum(
                            record.logical_object_ref == schema.logical_object_ref
                            for record in self._records
                        )
                        if self._boundary_record_count is None
                        else self._boundary_record_count
                    ),
                    opened_at=NOW,
                    closed_at=NOW,
                )
                for schema in self._schemas
            ),
            cursor_version=self._cursor_version,
            candidate_cursor_payload=b"candidate-cursor",
        )

    def abort(self) -> None:
        self.aborted = True
        if self._abort_error is not None:
            raise self._abort_error


class Provider:
    def __init__(
        self,
        session: Session,
        *,
        expected_private_cursor: bytes | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.session = session
        self.expected_private_cursor = expected_private_cursor
        self.open_error = open_error
        self.calls = 0

    def observe_source(self, request: object) -> AcquisitionSourceObservation:
        del request
        raise AssertionError("prepare must not observe source")

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> Session:
        self.calls += 1
        if self.open_error is not None:
            raise self.open_error
        assert intent.tenant_id == TENANT
        assert tuple(item.logical_object_ref for item in schemas) == tuple(
            item.logical_object_ref for item in self.session._schemas
        )
        assert private_cursor == self.expected_private_cursor
        return self.session


class ArtifactStore:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.payloads: dict[str, bytes] = {}

    def put_if_absent(self, *, tenant_id: str, artifact_digest: str, reader: BinaryIO) -> None:
        assert tenant_id == TENANT
        if self.fail:
            raise AcquisitionArtifactStoreError(operation="put", detail="private-path")
        payload = reader.read()
        assert hashlib.sha256(payload).hexdigest() == artifact_digest
        self.events.append("manifest" if b'"batch_id"' in payload else "segment")
        self.payloads[artifact_digest] = payload

    def open_verified(self, *, tenant_id: str, artifact_digest: str) -> BinaryIO:
        assert tenant_id == TENANT
        try:
            return io.BytesIO(self.payloads[artifact_digest])
        except KeyError:
            raise AcquisitionArtifactStoreError(
                operation="open",
                detail="artifact does not exist",
            ) from None

    def exists_verified(self, *, tenant_id: str, artifact_digest: str) -> bool:
        assert tenant_id == TENANT
        return artifact_digest in self.payloads


class StateStore:
    def __init__(
        self,
        events: list[str],
        *,
        checkpoint: tuple[SourceCheckpointState, bytes] | None = None,
        fail_preparation: bool = False,
    ) -> None:
        self.events = events
        self.checkpoint = checkpoint
        self.fail_preparation = fail_preparation
        self.prepared: list[AcquisitionPreparedReceipt] = []
        self.prepared_states: dict[
            tuple[str, str, str, int],
            tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt],
        ] = {}
        self.checkpoint_receipt: AcquisitionCheckpointReceipt | None = None
        self.acknowledgements: list[AcquisitionAcknowledgement] = []
        self.outcomes: list[dict[str, object]] = []

    def admit_authority(
        self,
        *,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
    ) -> tuple[int, int]:
        assert (tenant_id, contract_digest, source_binding_ref, source_binding_revision) == (
            TENANT,
            CONTRACT_DIGEST,
            BINDING_REF,
            3,
        )
        return 0, 0

    def load_checkpoint_with_cursor(
        self, tenant_id: str, contract_digest: str, source_binding_ref: str
    ) -> tuple[SourceCheckpointState, bytes]:
        assert (tenant_id, contract_digest, source_binding_ref) == (
            TENANT,
            CONTRACT_DIGEST,
            BINDING_REF,
        )
        if self.checkpoint is None:
            raise AcquisitionStateNotFoundError
        return self.checkpoint

    def prepare_exact(
        self,
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState:
        if self.fail_preparation:
            raise RuntimeError("prepared-state-private-detail")
        self.events.append("state")
        self.prepared.append(receipt)
        state = PreparedAcquisitionState(
            tenant_id=receipt.tenant_id,
            intent_key=receipt.intent_key,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            source_binding_revision=source_binding_revision,
            credential_revision=credential_revision,
            binding_authority_epoch=binding_authority_epoch,
            contract_authority_epoch=contract_authority_epoch,
            acknowledgement_consumer_ref=acknowledgement_consumer_ref,
            provider_kind=provider_kind,
            prior_checkpoint_revision=receipt.prior_checkpoint_revision,
            batch_id=receipt.batch_id,
            batch_manifest_digest=receipt.batch_manifest_digest,
            candidate_cursor_ciphertext=candidate_cursor_plaintext,
            candidate_checkpoint_digest=receipt.candidate_checkpoint_digest,
            cursor_version=receipt.cursor_version,
            state=PreparedAcquisitionStateStatus.PREPARED,
            acknowledgement_digest=None,
            created_at=receipt.prepared_at,
            updated_at=receipt.prepared_at,
        )
        self.prepared_states[
            (
                receipt.tenant_id,
                contract_digest,
                source_binding_ref,
                receipt.prior_checkpoint_revision,
            )
        ] = (state, receipt)
        return state

    def load_preparation_for_replay(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        try:
            return self.prepared_states[
                (tenant_id, contract_digest, source_binding_ref, prior_checkpoint_revision)
            ]
        except KeyError:
            raise AcquisitionStateNotFoundError from None

    def load_preparation(
        self,
        tenant_id: str,
        contract_digest: str,
        source_binding_ref: str,
        prior_checkpoint_revision: int,
    ) -> tuple[PreparedAcquisitionState, AcquisitionPreparedReceipt]:
        return self.load_preparation_for_replay(
            tenant_id,
            contract_digest,
            source_binding_ref,
            prior_checkpoint_revision,
        )

    def acknowledge_exact(
        self,
        acknowledgement: AcquisitionAcknowledgement,
        *,
        expected_consumer_ref: str,
        provider_kind: str,
        cursor_version: str,
    ) -> AcquisitionCheckpointReceipt:
        assert acknowledgement.consumer_ref == expected_consumer_ref
        assert provider_kind == "postgresql"
        assert cursor_version == "postgresql-compound-v1"
        self.acknowledgements.append(acknowledgement)
        if self.checkpoint_receipt is None:
            self.checkpoint_receipt = AcquisitionCheckpointReceipt(
                checkpoint_receipt_id="checkpoint-receipt:1",
                tenant_id=acknowledgement.tenant_id,
                contract_digest=acknowledgement.contract_digest,
                source_binding_ref=acknowledgement.source_binding_ref,
                previous_revision=acknowledgement.prior_checkpoint_revision,
                committed_revision=acknowledgement.prior_checkpoint_revision + 1,
                cursor_digest=acknowledgement.candidate_checkpoint_digest,
                batch_id=acknowledgement.batch_id,
                acknowledgement_id=acknowledgement.acknowledgement_id,
                committed_at=acknowledgement.acknowledged_at,
            )
        return self.checkpoint_receipt

    def load_acknowledgement_replay(
        self,
        acknowledgement: AcquisitionAcknowledgement,
    ) -> AcquisitionCheckpointReceipt | None:
        if self.checkpoint_receipt is None or not self.acknowledgements:
            return None
        if self.acknowledgements[-1] != acknowledgement:
            raise AcquisitionStateConflictError("contradictory acknowledgement")
        return self.checkpoint_receipt

    def record_governed_outcome(self, **values: object) -> object:
        self.events.append("outcome")
        self.outcomes.append(values)
        return values


class EvidenceWriter:
    """Records what the runner wrote, and optionally forwards it to a real writer.

    The delegate lets a cross-component test drive this same runner into the durable
    evidence store without restating the runner's nine collaborators, while every
    test here keeps asserting against the recorded receipts.
    """

    def __init__(
        self, events: list[str], *, delegate: AcquisitionEvidenceWriter | None = None
    ) -> None:
        self.events = events
        self.receipts: list[AcquisitionEvidenceReceipt] = []
        self._delegate = delegate

    def append(self, receipt: AcquisitionEvidenceReceipt) -> None:
        self.events.append("evidence")
        self.receipts.append(receipt)
        if self._delegate is not None:
            self._delegate.append(receipt)


def _runner(
    *,
    records: tuple[AcquisitionRecord, ...] = (_record(),),
    contract_changes: dict[str, object] | None = None,
    incomplete: bool = False,
    artifact_failure: bool = False,
    checkpoint: tuple[SourceCheckpointState, bytes] | None = None,
    state_failure: bool = False,
    boundary_record_count: int | None = None,
    stream_error: Exception | None = None,
    abort_error: Exception | None = None,
    open_error: Exception | None = None,
    cursor_version: str = "postgresql-compound-v1",
    binding_changes: dict[str, object] | None = None,
    provider_kind: str = "postgresql",
    schema: AcquisitionObjectSchema | tuple[AcquisitionObjectSchema, ...] | None = None,
    capabilities: tuple[str, ...] = ("snapshot",),
    acquisition_mode: AcquisitionMode = "snapshot",
    fault_hook: Callable[[str], None] | None = None,
    reference_prefix: str = "",
    evidence_delegate: AcquisitionEvidenceWriter | None = None,
) -> tuple[
    AcquisitionRunner,
    AcquisitionSourceObservation,
    Session,
    Provider,
    list[str],
    StateStore,
    ArtifactStore,
    EvidenceWriter,
    list[str],
]:
    selected_schema = schema or _schema()
    observation = _observation(
        selected_schema,
        provider_kind=provider_kind,
        capabilities=capabilities,
    )
    contract = _contract(selected_schema, observation, **(contract_changes or {}))
    binding = _binding(provider_kind=provider_kind).model_copy(update=binding_changes or {})
    events: list[str] = []
    prior_cursor = None if checkpoint is None else checkpoint[1]
    lower_cursor_digest = None if checkpoint is None else checkpoint[0].cursor_digest
    session = Session(
        records,
        selected_schema,
        incomplete=incomplete,
        lower_cursor_digest=lower_cursor_digest,
        boundary_record_count=boundary_record_count,
        stream_error=stream_error,
        abort_error=abort_error,
        cursor_version=cursor_version,
        acquisition_mode=acquisition_mode,
    )
    provider = Provider(session, expected_private_cursor=prior_cursor, open_error=open_error)
    resolutions: list[str] = []
    state = StateStore(events, checkpoint=checkpoint, fail_preparation=state_failure)
    artifacts = ArtifactStore(events, fail=artifact_failure)
    evidence = EvidenceWriter(events, delegate=evidence_delegate)
    references = iter(
        (
            f"{reference_prefix}receipt-ref:prepared-1",
            f"{reference_prefix}evidence-ref:prepared-1",
            f"{reference_prefix}outcome-ref:1",
            f"{reference_prefix}evidence-ref:outcome-1",
            f"{reference_prefix}evidence-ref:acknowledged-1",
            f"{reference_prefix}evidence-ref:failure-1",
            f"{reference_prefix}evidence-ref:replay-1",
            f"{reference_prefix}evidence-ref:replay-2",
        )
    )

    def resolve_provider(value: SourceConnectionBinding) -> Provider:
        resolutions.append(value.binding_id)
        return provider

    runner = AcquisitionRunner(
        binding_resolver=lambda tenant_id, binding_ref: binding,
        contract_resolver=lambda tenant_id, contract_ref: contract,
        observation_resolver=lambda tenant_id, observation_digest: observation,
        provider_resolver=resolve_provider,
        state_store=state,
        artifact_store=artifacts,
        evidence_writer=evidence,
        reference_factory=lambda kind: next(references),
        clock=lambda: NOW,
        fault_hook=fault_hook,
    )
    return runner, observation, session, provider, resolutions, state, artifacts, evidence, events


def test_prepare_persists_artifacts_state_and_public_evidence_in_order() -> None:
    runner, observation, session, provider, resolutions, state, artifacts, evidence, events = (
        _runner(records=(_record("1"), _record("2")))
    )

    result = runner.prepare(_intent(observation))

    assert isinstance(result, AcquisitionPreparationResult)
    assert result.evidence.outcome == "prepared"
    assert result.prepared_receipt is not None
    assert provider.calls == 1
    assert resolutions == [BINDING_REF]
    assert session.exhausted and not session.aborted
    assert events == ["segment", "manifest", "state", "evidence"]
    assert len(artifacts.payloads) == 2
    assert len(state.prepared) == 1
    assert evidence.receipts == [result.evidence]
    assert "private-orders-identity" not in result.evidence.model_dump_json()
    assert result.batch_manifest is not None
    assert result.prepared_receipt.batch_manifest_digest == digest(result.batch_manifest)
    segment_manifest = result.batch_manifest.segment_manifests[0]
    assert segment_manifest.segment_name == f"0000-{digest('orders')}.jsonl"
    assert segment_manifest.boundary_digest == digest(session.complete().boundaries[0])


def test_zero_record_batch_is_prepared_instead_of_becoming_a_failure() -> None:
    runner, observation, session, *_rest = _runner(records=())

    result = runner.prepare(_intent(observation))

    assert result.evidence.outcome == "prepared"
    assert result.batch_manifest is not None
    assert result.batch_manifest.total_record_count == 0
    assert session.exhausted and not session.aborted


def test_authority_failure_precedes_provider_resolution() -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()

    with pytest.raises(AcquisitionOwnershipError, match="authority"):
        runner.prepare(_intent(observation, tenant_id="tenant-b"))

    assert resolutions == []


def test_source_observation_is_loaded_through_the_ready_binding_reference() -> None:
    runner, observation, _session, _provider, _resolutions, *_rest = _runner()
    requested_observations: list[tuple[str, str]] = []

    def resolve_observation(tenant_id: str, observation_ref: str) -> AcquisitionSourceObservation:
        requested_observations.append((tenant_id, observation_ref))
        return observation

    runner._observation_resolver = resolve_observation

    runner.prepare(_intent(observation))

    assert requested_observations == [(TENANT, OBSERVATION_REF)]


@pytest.mark.parametrize(
    "mismatch",
    ("tenant", "binding", "provider", "connection_handle"),
)
def test_each_source_observation_authority_field_must_match_exactly(mismatch: str) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    item = observation.object_observations[0]
    provider_observation = item.provider_observation
    if mismatch == "tenant":
        candidate = observation.model_copy(update={"tenant_id": "tenant-b"})
    elif mismatch == "binding":
        candidate = observation.model_copy(update={"source_binding_ref": "source-binding:other"})
    elif mismatch == "provider":
        changed_provider = provider_observation.model_copy(update={"provider": "stripe"})
        candidate = observation.model_copy(
            update={
                "provider_kind": "stripe",
                "object_observations": (
                    item.model_copy(update={"provider_observation": changed_provider}),
                ),
            }
        )
    else:
        changed_provider = provider_observation.model_copy(
            update={"connection_handle": "source-handle:other"}
        )
        candidate = observation.model_copy(
            update={
                "object_observations": (
                    item.model_copy(update={"provider_observation": changed_provider}),
                )
            }
        )
    contract = _contract(_schema(), candidate)
    runner._contract_resolver = lambda tenant_id, contract_ref: contract
    runner._observation_resolver = lambda tenant_id, observation_ref: candidate

    with pytest.raises(AcquisitionDriftError, match="authority"):
        runner.prepare(_intent(candidate))

    assert resolutions == []


@pytest.mark.parametrize(
    "mismatch",
    ("objects", "schema", "capability", "missing_time", "future_time"),
)
def test_observation_objects_schema_and_freshness_are_exact(mismatch: str) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    item = observation.object_observations[0]
    provider_observation = item.provider_observation
    if mismatch == "objects":
        changed_item = item.model_copy(update={"logical_object_ref": "customers"})
    elif mismatch == "schema":
        changed_item = item.model_copy(
            update={
                "provider_observation": provider_observation.model_copy(
                    update={"schema_digest": "d" * 64}
                )
            }
        )
    elif mismatch == "capability":
        changed_item = item.model_copy(
            update={
                "provider_observation": provider_observation.model_copy(
                    update={"capabilities": ("incremental",)}
                )
            }
        )
    elif mismatch == "missing_time":
        changed_item = item.model_copy(
            update={
                "provider_observation": provider_observation.model_copy(
                    update={"observed_at": None}
                )
            }
        )
    else:
        changed_item = item.model_copy(
            update={
                "provider_observation": provider_observation.model_copy(
                    update={"observed_at": NOW + timedelta(seconds=1)}
                )
            }
        )
    candidate = observation.model_copy(update={"object_observations": (changed_item,)})
    contract = _contract(_schema(), candidate)
    runner._contract_resolver = lambda tenant_id, contract_ref: contract
    runner._observation_resolver = lambda tenant_id, observation_ref: candidate

    with pytest.raises(AcquisitionDriftError, match="observation"):
        runner.prepare(_intent(candidate))

    assert resolutions == []


def test_contract_authority_is_loaded_by_exact_tenant_and_reference() -> None:
    runner, observation, _session, _provider, _resolutions, *_rest = _runner()
    contract = _contract(_schema(), observation)
    requests: list[tuple[str, str]] = []

    def resolve_contract(tenant_id: str, contract_ref: str) -> ActivatedAcquisitionContract:
        requests.append((tenant_id, contract_ref))
        return contract

    runner._contract_resolver = resolve_contract

    runner.prepare(_intent(observation))

    assert requests == [(TENANT, CONTRACT_REF)]


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    (("contract_ref", "contract:other:v1"), ("contract_digest", "d" * 64)),
)
def test_contract_reference_and_digest_must_each_match_exactly(
    changed_field: str,
    changed_value: str,
) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    mismatched = _contract(_schema(), observation, **{changed_field: changed_value})
    runner._contract_resolver = lambda tenant_id, contract_ref: mismatched

    with pytest.raises(AcquisitionContractError, match="authority"):
        runner.prepare(_intent(observation))

    assert resolutions == []


def test_unactivated_contract_records_no_valid_plan_without_provider_execution() -> None:
    runner, observation, _session, provider, resolutions, state, artifacts, evidence, events = (
        _runner(contract_changes={"lifecycle_state": "inactive"})
    )

    result = runner.prepare(_intent(observation))

    assert result.evidence.outcome == "no_valid_plan"
    assert result.governed_outcome is not None
    assert provider.calls == 0
    assert resolutions == []
    assert not artifacts.payloads
    assert not state.prepared
    assert events == ["outcome", "evidence"]
    assert evidence.receipts[0].reason_codes == ("contract_not_activated",)


@pytest.mark.parametrize(
    ("contract_changes", "reason_code", "failed_constraint"),
    (
        (
            {"source_binding_ref": "source-binding:other"},
            "source_binding_not_admitted",
            "contract.source_binding_ref",
        ),
        (
            {"source_observation_digest": "d" * 64},
            "source_observation_not_admitted",
            "contract.source_observation_digest",
        ),
        (
            {"acquisition_modes": ("incremental",)},
            "acquisition_mode_not_admitted",
            "contract.acquisition_modes",
        ),
        (
            {"object_schemas": (_schema().model_copy(update={"logical_object_ref": "customers"}),)},
            "logical_object_not_admitted",
            "contract.object_schemas",
        ),
        ({"record_ceiling": 9}, "record_ceiling_not_admitted", "contract.record_ceiling"),
        (
            {"encoded_byte_ceiling": 99_999},
            "encoded_byte_ceiling_not_admitted",
            "contract.encoded_byte_ceiling",
        ),
    ),
)
def test_every_contract_narrowing_violation_records_exact_no_valid_plan(
    contract_changes: dict[str, object],
    reason_code: str,
    failed_constraint: str,
) -> None:
    runner, observation, _session, provider, resolutions, state, artifacts, evidence, _events = (
        _runner(contract_changes=contract_changes)
    )

    result = runner.prepare(_intent(observation))

    assert isinstance(result.governed_outcome, AcquisitionNoValidPlan)
    assert result.governed_outcome.reason_codes == (reason_code,)
    assert result.governed_outcome.failed_constraints == (failed_constraint,)
    assert evidence.receipts[0].reason_codes == (reason_code,)
    assert provider.calls == 0
    assert resolutions == []
    assert not artifacts.payloads
    assert not state.prepared


def test_binding_authority_is_loaded_by_exact_tenant_and_reference() -> None:
    runner, observation, _session, _provider, _resolutions, *_rest = _runner()
    binding = _binding()
    requests: list[tuple[str, str]] = []

    def resolve_binding(tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
        requests.append((tenant_id, binding_ref))
        return binding

    runner._binding_resolver = resolve_binding

    runner.prepare(_intent(observation))

    assert requests == [(TENANT, BINDING_REF)]


@pytest.mark.parametrize("changed_field", ("tenant_id", "binding_id"))
def test_binding_tenant_and_reference_must_each_match_exactly(changed_field: str) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    mismatched = _binding().model_copy(update={changed_field: f"other-{changed_field}"})
    runner._binding_resolver = lambda tenant_id, binding_ref: mismatched

    with pytest.raises(AcquisitionOwnershipError, match="authority"):
        runner.prepare(_intent(observation))

    assert resolutions == []


@pytest.mark.parametrize(
    "binding",
    (
        _binding().model_copy(
            update={
                "lifecycle_state": SourceConnectionBindingState.VALIDATING,
                "capability_profile_digest": None,
                "source_observation_ref": None,
            }
        ),
        _binding().model_copy(update={"approved_object_refs": ("customers",)}),
    ),
)
def test_binding_must_be_ready_and_authorize_every_object(
    binding: SourceConnectionBinding,
) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    runner._binding_resolver = lambda tenant_id, binding_ref: binding

    with pytest.raises(AcquisitionAuthorizationError):
        runner.prepare(_intent(observation))

    assert resolutions == []


@pytest.mark.parametrize(
    "binding_changes",
    (
        {"revision": 4},
        {"credential_revision": 2},
        {"source_observation_ref": "source-observation:rotated"},
        {"capability_profile_digest": "b" * 64},
    ),
)
def test_rotated_binding_authority_requires_contract_reactivation(
    binding_changes: dict[str, object],
) -> None:
    runner, observation, _session, provider, resolutions, state, artifacts, evidence, _events = (
        _runner(binding_changes=binding_changes)
    )

    result = runner.prepare(_intent(observation))

    assert result.evidence.outcome == "no_valid_plan"
    assert isinstance(result.governed_outcome, AcquisitionNoValidPlan)
    assert "source_binding_authority_stale" in result.governed_outcome.reason_codes
    assert provider.calls == 0
    assert resolutions == []
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].reason_codes == ("source_binding_authority_stale",)


@pytest.mark.parametrize("limit_kind", ("records", "encoded_bytes"))
def test_later_record_ceiling_crossing_aborts_without_durable_effect(limit_kind: str) -> None:
    first = _record("1")
    second = _record("2")
    runner, observation, session, _provider, _resolutions, state, artifacts, evidence, _events = (
        _runner(records=(first, second))
    )
    intent_changes = (
        {"record_ceiling": 1}
        if limit_kind == "records"
        else {"encoded_byte_ceiling": len(canonical_bytes(first)) + 1}
    )

    with pytest.raises(AcquisitionCeilingExceeded, match="ceiling") as captured:
        runner.prepare(_intent(observation, **intent_changes))

    assert captured.value.logical_object_ref == "orders"
    assert captured.value.limit_kind == limit_kind
    assert captured.value.ceiling == next(iter(intent_changes.values()))
    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared
    assert len(evidence.receipts) == 1
    assert evidence.receipts[0].outcome == "failed"
    expected_reason = (
        "record_ceiling_exceeded" if limit_kind == "records" else "encoded_byte_ceiling_exceeded"
    )
    assert evidence.receipts[0].reason_codes == (expected_reason,)


def test_exact_record_and_encoded_byte_ceilings_are_admitted() -> None:
    record = _record()
    exact_bytes = len(canonical_bytes(record)) + 1
    runner, observation, session, _provider, _resolutions, state, _artifacts, *_rest = _runner(
        records=(record,)
    )

    result = runner.prepare(
        _intent(
            observation,
            record_ceiling=1,
            encoded_byte_ceiling=exact_bytes,
        )
    )

    assert result.batch_manifest is not None
    assert result.batch_manifest.total_record_count == 1
    assert result.batch_manifest.total_encoded_bytes == exact_bytes
    assert session.exhausted and not session.aborted
    assert len(state.prepared) == 1


def test_encoded_byte_ceiling_is_global_across_selected_objects() -> None:
    order_schema = _schema()
    payment_fields = (AcquisitionField(name="payment_id", value_type="integer", nullable=False),)
    payment_schema = AcquisitionObjectSchema(
        logical_object_ref="payments",
        schema_digest=digest(payment_fields),
        fields=payment_fields,
        record_key_fields=("payment_id",),
        source_updated_at_field=None,
    )
    order_record = _record()
    payment_record = AcquisitionRecord(
        logical_object_ref="payments",
        record_key="1",
        source_created_at=None,
        source_updated_at=None,
        fields=(AcquisitionFieldValue(name="payment_id", value=1),),
    )
    order_observation = _observation(order_schema)
    payment_observation = _observation(payment_schema)
    observation = order_observation.model_copy(
        update={
            "object_observations": (
                order_observation.object_observations[0],
                payment_observation.object_observations[0],
            )
        }
    )
    contract = _contract(
        order_schema,
        observation,
        object_schemas=(order_schema, payment_schema),
    )
    binding = _binding().model_copy(update={"approved_object_refs": ("orders", "payments")})
    (
        runner,
        _unused_observation,
        _unused_session,
        _unused_provider,
        _resolutions,
        state,
        artifacts,
        evidence,
        _events,
    ) = _runner()
    session = Session((order_record, payment_record), (order_schema, payment_schema))
    provider = Provider(session)
    runner._contract_resolver = lambda tenant_id, contract_ref: contract
    runner._binding_resolver = lambda tenant_id, binding_ref: binding
    runner._observation_resolver = lambda tenant_id, observation_ref: observation
    runner._provider_resolver = lambda resolved_binding: provider
    exact_combined_bytes = (
        len(canonical_bytes(order_record)) + len(canonical_bytes(payment_record)) + 2
    )

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        runner.prepare(
            _intent(
                observation,
                object_refs=("orders", "payments"),
                encoded_byte_ceiling=exact_combined_bytes - 1,
            )
        )

    assert captured.value.logical_object_ref == "payments"
    assert captured.value.limit_kind == "encoded_bytes"
    assert captured.value.ceiling == exact_combined_bytes - 1
    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].reason_codes == ("encoded_byte_ceiling_exceeded",)


def test_record_schema_mismatch_aborts_before_publication() -> None:
    invalid = _record().model_copy(
        update={
            "fields": (
                AcquisitionFieldValue(name="amount", value=Decimal("10.50")),
                AcquisitionFieldValue(name="order_id", value=1),
                AcquisitionFieldValue(name="updated_at", value=NOW),
            )
        }
    )
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        records=(invalid,)
    )

    with pytest.raises(AcquisitionIntegrityError, match="record_schema"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


def test_incomplete_provider_boundary_aborts_without_publication() -> None:
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        incomplete=True
    )

    with pytest.raises(AcquisitionIntegrityError, match="incomplete"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


def test_completed_boundary_must_match_the_exact_record_count() -> None:
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        boundary_record_count=2
    )

    with pytest.raises(AcquisitionIntegrityError, match="boundary"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


def test_unclassified_provider_stream_failure_is_integrity_not_retryable() -> None:
    runner, observation, session, _provider, _resolutions, state, artifacts, evidence, _events = (
        _runner(
            stream_error=OSError("provider-private-response"),
            abort_error=OSError("cleanup-private-path"),
        )
    )

    with pytest.raises(AcquisitionIntegrityError, match="provider_stream_failed") as captured:
        runner.prepare(_intent(observation))

    assert session.aborted
    assert "provider-private-response" not in str(captured.value)
    assert "cleanup-private-path" not in str(captured.value)
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].outcome == "failed"


def test_classified_provider_stream_failure_preserves_retry_semantics() -> None:
    provider_error = AcquisitionProviderError(
        provider_kind="postgresql",
        classification="throttled",
        reason_code="rate_limited",
    )
    runner, observation, session, _provider, _resolutions, state, artifacts, evidence, _events = (
        _runner(stream_error=provider_error)
    )

    with pytest.raises(AcquisitionThrottledError, match="rate_limited"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].reason_codes == ("rate_limited",)


def test_unclassified_provider_open_failure_is_integrity_not_retryable() -> None:
    runner, observation, session, _provider, _resolutions, state, artifacts, evidence, _events = (
        _runner(open_error=RuntimeError("private-provider-open-canary"))
    )

    with pytest.raises(AcquisitionIntegrityError, match="provider_open_failed") as captured:
        runner.prepare(_intent(observation))

    assert "private-provider-open-canary" not in str(captured.value)
    assert not session.aborted
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].reason_codes == ("integrity_failure",)


def test_provider_record_order_must_be_strictly_deterministic() -> None:
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        records=(_record("2"), _record("1"))
    )

    with pytest.raises(AcquisitionIntegrityError, match="deterministic"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


def test_postgresql_snapshot_uses_typed_primary_key_order() -> None:
    runner, observation, session, *_rest = _runner(records=(_record("2"), _record("10")))

    result = runner.prepare(_intent(observation))

    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted


def test_duplicate_record_identity_is_rejected_even_when_timestamp_changes() -> None:
    duplicate = _record("1").model_copy(update={"source_updated_at": NOW + timedelta(seconds=1)})
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        records=(_record("1"), duplicate)
    )

    with pytest.raises(AcquisitionIntegrityError, match="duplicate"):
        runner.prepare(_intent(observation))

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


@pytest.mark.parametrize(
    ("value_type", "lower", "higher"),
    (
        ("boolean", False, True),
        ("integer", 2, 10),
        ("decimal", Decimal("2.5"), Decimal("10.5")),
        ("string", "a", "b"),
        ("timestamp", NOW, NOW + timedelta(seconds=1)),
    ),
)
def test_postgresql_snapshot_orders_each_supported_key_scalar(
    value_type: AcquisitionValueType,
    lower: AcquisitionScalar,
    higher: AcquisitionScalar,
) -> None:
    schema = _single_key_schema(value_type)
    ascending, observation, session, *_rest = _runner(
        schema=schema,
        records=(_key_record("lower", lower), _key_record("higher", higher)),
    )

    result = ascending.prepare(_intent(observation))

    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted

    descending, observation, session, *_rest = _runner(
        schema=schema,
        records=(_key_record("higher", higher), _key_record("lower", lower)),
    )
    with pytest.raises(AcquisitionIntegrityError, match="deterministic"):
        descending.prepare(_intent(observation))
    assert session.aborted


def test_composite_key_order_compares_later_components_after_equal_prefix() -> None:
    fields = (
        AcquisitionField(name="partition", value_type="integer", nullable=False),
        AcquisitionField(name="sequence", value_type="integer", nullable=False),
    )
    schema = AcquisitionObjectSchema(
        logical_object_ref="orders",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("partition", "sequence"),
        source_updated_at_field=None,
    )

    def record(record_key: str, sequence: int) -> AcquisitionRecord:
        return AcquisitionRecord(
            logical_object_ref="orders",
            record_key=record_key,
            source_created_at=None,
            source_updated_at=None,
            fields=(
                AcquisitionFieldValue(name="partition", value=1),
                AcquisitionFieldValue(name="sequence", value=sequence),
            ),
        )

    runner, observation, session, *_rest = _runner(
        schema=schema,
        records=(record("1:2", 2), record("1:1", 1)),
    )

    with pytest.raises(AcquisitionIntegrityError, match="deterministic"):
        runner.prepare(_intent(observation))

    assert session.aborted

    ascending, observation, session, *_rest = _runner(
        schema=schema,
        records=(record("1:1", 1), record("1:2", 2)),
    )
    result = ascending.prepare(_intent(observation))
    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted


def test_equal_provider_order_keys_are_rejected_even_with_different_record_strings() -> None:
    schema = _single_key_schema("integer")
    runner, observation, session, *_rest = _runner(
        schema=schema,
        records=(_key_record("canonical-1", 1), _key_record("alias-1", 1)),
    )

    with pytest.raises(AcquisitionIntegrityError, match="deterministic"):
        runner.prepare(_intent(observation))

    assert session.aborted


def test_postgresql_incremental_orders_by_updated_time_before_primary_key() -> None:
    first = _record("10")
    second = _record("2").model_copy(update={"source_updated_at": NOW + timedelta(seconds=1)})
    runner, observation, session, *_rest = _runner(
        records=(first, second),
        contract_changes={"acquisition_modes": ("incremental",)},
        capabilities=("incremental",),
        acquisition_mode="incremental",
    )

    result = runner.prepare(_intent(observation, acquisition_mode="incremental"))

    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted


@pytest.mark.parametrize("mode", ("snapshot", "reconciliation"))
def test_stripe_bounded_modes_order_by_creation_time_before_object_key(
    mode: Literal["snapshot", "reconciliation"],
) -> None:
    first = _record("10").model_copy(update={"source_created_at": NOW})
    second = _record("2").model_copy(update={"source_created_at": NOW + timedelta(seconds=1)})
    runner, observation, session, *_rest = _runner(
        records=(first, second),
        provider_kind="stripe",
        contract_changes={"acquisition_modes": (mode,)},
        capabilities=(mode,),
        acquisition_mode=mode,
    )

    result = runner.prepare(_intent(observation, acquisition_mode=mode))

    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted


def test_stripe_incremental_falls_back_to_creation_time_for_ordering() -> None:
    first = _record("10").model_copy(update={"source_created_at": NOW, "source_updated_at": None})
    second = _record("2").model_copy(
        update={
            "source_created_at": NOW,
            "source_updated_at": NOW + timedelta(seconds=1),
        }
    )
    runner, observation, session, *_rest = _runner(
        records=(first, second),
        provider_kind="stripe",
        contract_changes={"acquisition_modes": ("incremental",)},
        capabilities=("incremental",),
        acquisition_mode="incremental",
    )

    result = runner.prepare(_intent(observation, acquisition_mode="incremental"))

    assert result.evidence.outcome == "prepared"
    assert session.exhausted and not session.aborted


def test_artifact_failure_is_sanitized_and_never_records_prepared_state() -> None:
    runner, observation, session, _provider, _resolutions, state, _artifacts, evidence, _events = (
        _runner(artifact_failure=True)
    )

    with pytest.raises(AcquisitionIntegrityError) as captured:
        runner.prepare(_intent(observation))

    assert "private-path" not in str(captured.value)
    assert not session.aborted
    assert not state.prepared
    assert evidence.receipts[0].outcome == "failed"


def test_prepared_state_failure_is_sanitized_after_durable_artifacts() -> None:
    runner, observation, _session, _provider, _resolutions, state, artifacts, evidence, events = (
        _runner(state_failure=True)
    )

    with pytest.raises(AcquisitionIntegrityError) as captured:
        runner.prepare(_intent(observation))

    assert "prepared-state-private-detail" not in str(captured.value)
    assert len(artifacts.payloads) == 2
    assert not state.prepared
    assert evidence.receipts[0].outcome == "failed"
    assert events == ["segment", "manifest", "evidence"]


def _checkpoint(
    *,
    revision: int,
    cursor: bytes,
    provider_kind: str = "postgresql",
) -> tuple[SourceCheckpointState, bytes]:
    return (
        SourceCheckpointState(
            tenant_id=TENANT,
            contract_digest=CONTRACT_DIGEST,
            source_binding_ref=BINDING_REF,
            provider_kind=provider_kind,  # type: ignore[arg-type]
            cursor_version="postgresql-compound-v1",
            revision=revision,
            encrypted_cursor_payload=b"ciphertext",
            cursor_digest=hashlib.sha256(cursor).hexdigest(),
            last_batch_id=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        cursor,
    )


def test_exact_checkpoint_revision_and_private_cursor_are_required() -> None:
    checkpoint = _checkpoint(revision=1, cursor=b"prior-cursor")
    runner, observation, _session, provider, *_rest = _runner(checkpoint=checkpoint)

    result = runner.prepare(
        _intent(
            observation,
            prior_checkpoint_revision=1,
            prior_checkpoint_digest=checkpoint[0].cursor_digest,
        )
    )

    assert result.evidence.outcome == "prepared"
    assert provider.calls == 1


def test_preparation_persists_the_provider_cursor_version() -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner(
        cursor_version="postgresql-compound-v2"
    )

    result = runner.prepare(_intent(observation))

    assert result.prepared_receipt is not None
    assert result.prepared_receipt.cursor_version == "postgresql-compound-v2"
    assert state.prepared[0].cursor_version == "postgresql-compound-v2"


def test_incremental_preparation_rejects_a_changed_cursor_version() -> None:
    checkpoint = _checkpoint(revision=1, cursor=b"prior-cursor")
    runner, observation, session, _provider, _resolutions, state, artifacts, *_rest = _runner(
        checkpoint=checkpoint,
        cursor_version="postgresql-compound-v2",
    )

    with pytest.raises(AcquisitionIntegrityError, match="cursor_version"):
        runner.prepare(
            _intent(
                observation,
                prior_checkpoint_revision=1,
                prior_checkpoint_digest=checkpoint[0].cursor_digest,
            )
        )

    assert session.aborted
    assert not artifacts.payloads
    assert not state.prepared


def test_stale_checkpoint_revision_fails_before_provider_resolution() -> None:
    checkpoint = _checkpoint(revision=2, cursor=b"prior-cursor")
    runner, observation, _session, provider, resolutions, *_rest = _runner(checkpoint=checkpoint)

    with pytest.raises(AcquisitionStaleRevision, match="revision"):
        runner.prepare(
            _intent(
                observation,
                prior_checkpoint_revision=1,
                prior_checkpoint_digest=checkpoint[0].cursor_digest,
            )
        )

    assert provider.calls == 0
    assert resolutions == []


@pytest.mark.parametrize("changed_field", ("tenant_id", "contract_digest", "source_binding_ref"))
def test_checkpoint_tenant_contract_and_binding_must_each_match_exactly(
    changed_field: str,
) -> None:
    checkpoint, cursor = _checkpoint(revision=1, cursor=b"prior-cursor")
    replacement = "d" * 64 if changed_field == "contract_digest" else f"other-{changed_field}"
    mismatched = checkpoint.model_copy(update={changed_field: replacement})
    runner, observation, _session, provider, resolutions, *_rest = _runner(
        checkpoint=(mismatched, cursor)
    )

    with pytest.raises(AcquisitionOwnershipError, match="checkpoint"):
        runner.prepare(
            _intent(
                observation,
                prior_checkpoint_revision=1,
                prior_checkpoint_digest=checkpoint.cursor_digest,
            )
        )

    assert provider.calls == 0
    assert resolutions == []


def test_missing_nonzero_checkpoint_fails_before_provider_resolution() -> None:
    runner, observation, _session, provider, resolutions, *_rest = _runner()

    with pytest.raises(AcquisitionStaleRevision, match="not_found"):
        runner.prepare(
            _intent(
                observation,
                prior_checkpoint_revision=1,
                prior_checkpoint_digest="d" * 64,
            )
        )

    assert provider.calls == 0
    assert resolutions == []


@pytest.mark.parametrize(
    ("classification", "reason_code", "error_type"),
    (
        ("authorization_denied", "authorization_denied", AcquisitionAuthorizationError),
        ("throttled", "rate_limited", AcquisitionThrottledError),
        ("transient_transport", "transport_failure", AcquisitionTransientError),
        ("transient_unavailable", "provider_unavailable", AcquisitionTransientError),
        ("statement_rejected", "statement_rejected", AcquisitionContractError),
        ("invalid_provider_response", "invalid_provider_response", AcquisitionIntegrityError),
        ("integrity_failure", "integrity_failure", AcquisitionIntegrityError),
        ("ambiguous_outcome", "ambiguous_outcome", AcquisitionIntegrityError),
        ("permanent_configuration", "permanent_configuration", AcquisitionContractError),
    ),
)
def test_provider_classification_maps_to_sanitized_runtime_failure(
    classification: str,
    reason_code: str,
    error_type: type[Exception],
) -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()

    def fail_provider(_binding: SourceConnectionBinding) -> Provider:
        resolutions.append("resolved")
        raise AcquisitionProviderError(
            provider_kind="postgresql",
            classification=classification,  # type: ignore[arg-type]
            reason_code=reason_code,  # type: ignore[arg-type]
        )

    runner._provider_resolver = fail_provider

    with pytest.raises(error_type) as captured:
        runner.prepare(_intent(observation))

    assert reason_code in str(captured.value)
    assert resolutions == ["resolved"]


@pytest.mark.parametrize(
    "reason_code",
    ("stripe_event_cursor_expired", "stripe_event_overlap_gap"),
)
def test_provider_continuity_failure_records_private_resynchronization_outcome(
    reason_code: Literal["stripe_event_cursor_expired", "stripe_event_overlap_gap"],
) -> None:
    checkpoint = _checkpoint(revision=1, cursor=b"prior-cursor", provider_kind="stripe")
    provider_error = AcquisitionProviderError(
        provider_kind="stripe",
        classification="resynchronization_required",
        reason_code=reason_code,
    )
    runner, observation, _session, provider, _resolutions, state, artifacts, evidence, events = (
        _runner(checkpoint=checkpoint, open_error=provider_error, provider_kind="stripe")
    )

    result = runner.prepare(
        _intent(
            observation,
            prior_checkpoint_revision=1,
            prior_checkpoint_digest=checkpoint[0].cursor_digest,
        )
    )

    assert result.evidence.outcome == "resynchronization_required"
    assert isinstance(result.governed_outcome, ResynchronizationRequired)
    assert result.governed_outcome.reason_code == reason_code
    assert result.governed_outcome.last_proven_checkpoint_digest == checkpoint[0].cursor_digest
    assert provider.calls == 1
    assert state.outcomes[0]["outcome"] == result.governed_outcome
    assert not artifacts.payloads
    assert not state.prepared
    assert evidence.receipts[0].reason_codes == (reason_code,)
    assert events == ["outcome", "evidence"]


def test_resynchronization_without_a_prior_checkpoint_fails_integrity() -> None:
    provider_error = AcquisitionProviderError(
        provider_kind="stripe",
        classification="resynchronization_required",
        reason_code="stripe_event_cursor_expired",
    )
    runner, observation, _session, provider, _resolutions, state, artifacts, evidence, _events = (
        _runner(open_error=provider_error, provider_kind="stripe")
    )

    with pytest.raises(AcquisitionIntegrityError, match="without_checkpoint"):
        runner.prepare(_intent(observation))

    assert provider.calls == 1
    assert not state.outcomes
    assert not artifacts.payloads
    assert evidence.receipts[0].reason_codes == ("integrity_failure",)


def test_provider_error_kind_must_match_the_resolved_binding() -> None:
    runner, observation, _session, _provider, resolutions, *_rest, evidence, _events = _runner()

    def fail_provider(_binding: SourceConnectionBinding) -> Provider:
        resolutions.append("resolved")
        raise AcquisitionProviderError(
            provider_kind="stripe",
            classification="throttled",
            reason_code="rate_limited",
        )

    runner._provider_resolver = fail_provider

    with pytest.raises(AcquisitionIntegrityError, match="authority"):
        runner.prepare(_intent(observation))

    assert resolutions == ["resolved"]
    assert evidence.receipts[0].reason_codes == ("integrity_failure",)


def test_private_runtime_reason_is_mapped_to_closed_public_reason() -> None:
    runner, observation, _session, _provider, _resolutions, *_rest, evidence, _events = _runner()

    def fail_binding(tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
        raise AcquisitionAuthorizationError("private-account-canary")

    runner._binding_resolver = fail_binding

    with pytest.raises(AcquisitionAuthorizationError, match="private-account-canary"):
        runner.prepare(_intent(observation))

    assert evidence.receipts[0].reason_codes == ("authorization_denied",)
    assert "private-account-canary" not in evidence.receipts[0].model_dump_json()


def test_source_observation_drift_precedes_provider_resolution() -> None:
    runner, observation, _session, _provider, resolutions, *_rest = _runner()
    drifted = observation.model_copy(update={"tenant_id": "tenant-b"})
    runner._observation_resolver = lambda tenant_id, observation_digest: drifted

    with pytest.raises(AcquisitionDriftError, match="observation"):
        runner.prepare(_intent(observation))

    assert resolutions == []
