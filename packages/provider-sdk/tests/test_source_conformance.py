from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, cast, overload

import heinzel_provider_sdk.source_conformance as source_conformance_module
import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBoundary,
    AcquisitionCeilingExceeded,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    ProviderError,
    ProviderObservation,
    acquisition_intent_key,
)
from heinzel_provider_sdk.acquisition_models import AcquisitionScalar
from heinzel_provider_sdk.acquisition_protocols import (
    AcquisitionObjectObservation,
    AcquisitionSession,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    CompletedAcquisition,
    SourceObservationRequest,
)
from heinzel_provider_sdk.errors import (
    AcquisitionProviderError,
    AcquisitionProviderErrorClassification,
    AcquisitionProviderKind,
    AcquisitionProviderReasonCode,
)
from heinzel_provider_sdk.source_conformance import (
    SourceConformanceError,
    SourceConformanceScenario,
    exercise_source_provider,
    verify_abandoned_session,
    verify_checkpoint_lifecycle,
    verify_cross_tenant_denial,
    verify_private_cursor_containment,
    verify_source_replay,
)
from pydantic import ValidationError

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_NON_UTC = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=1)))
_PRIVATE_CURSOR = b"cursor-account-private-canary"


def _schema() -> AcquisitionObjectSchema:
    fields = (
        AcquisitionField(name="order_id", value_type="integer", nullable=False),
        AcquisitionField(name="amount", value_type="decimal", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    return AcquisitionObjectSchema(
        logical_object_ref="order",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("order_id",),
        source_updated_at_field="updated_at",
    )


def _invoice_schema() -> AcquisitionObjectSchema:
    fields = (AcquisitionField(name="invoice_id", value_type="integer", nullable=False),)
    return AcquisitionObjectSchema(
        logical_object_ref="invoice",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("invoice_id",),
        source_updated_at_field=None,
    )


def _observation(
    *,
    tenant_id: str = "tenant-a",
    source_binding_ref: str = "source-binding-a",
    connection_handle: str = "connection-handle-a",
    object_identity: str = "private-object-identity",
    schema_digest: str | None = None,
    provider_kind: AcquisitionProviderKind = "postgresql",
    schemas: tuple[AcquisitionObjectSchema, ...] | None = None,
) -> AcquisitionSourceObservation:
    observed_schemas = schemas or (_schema(),)
    return AcquisitionSourceObservation(
        tenant_id=tenant_id,
        source_binding_ref=source_binding_ref,
        provider_kind=provider_kind,
        object_observations=tuple(
            AcquisitionObjectObservation(
                logical_object_ref=schema.logical_object_ref,
                provider_observation=ProviderObservation(
                    provider=provider_kind,
                    connection_handle=connection_handle,
                    object_identity=object_identity,
                    object_kind="base_table",
                    schema_digest=schema_digest or schema.schema_digest,
                    columns=(),
                    key_name=schema.record_key_fields[0],
                    key_type="BIGINT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=("snapshot_read",),
                    observed_at=_NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            )
            for schema in observed_schemas
        ),
    )


def _intent(
    *,
    source_observation_digest: str | None = None,
    schemas: tuple[AcquisitionObjectSchema, ...] | None = None,
) -> AcquisitionIntent:
    tenant_id = "tenant-a"
    run_intent_ref = "1" * 64
    contract_digest = "2" * 64
    source_binding_ref = "source-binding-a"
    acquisition_mode: Literal["snapshot"] = "snapshot"
    intent_schemas = schemas or (_schema(),)
    object_refs = tuple(schema.logical_object_ref for schema in intent_schemas)
    prior_checkpoint_revision = 0
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id=tenant_id,
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            acquisition_mode=acquisition_mode,
            object_refs=object_refs,
            prior_checkpoint_revision=prior_checkpoint_revision,
        ),
        tenant_id=tenant_id,
        run_intent_ref=run_intent_ref,
        contract_digest=contract_digest,
        source_binding_ref=source_binding_ref,
        acquisition_mode=acquisition_mode,
        object_refs=object_refs,
        prior_checkpoint_revision=prior_checkpoint_revision,
        contract_ref="contract-a-v1",
        source_observation_digest=source_observation_digest
        or digest(_observation(schemas=intent_schemas)),
        prior_checkpoint_digest=None,
        record_ceiling=100,
        encoded_byte_ceiling=100_000,
        admitted_at=_NOW,
    )


def _record(record_key: str, amount: str = "10.50") -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="order",
        record_key=record_key,
        source_created_at=None,
        source_updated_at=_NOW,
        fields=(
            AcquisitionFieldValue(name="order_id", value=int(record_key.rsplit(":", 1)[1])),
            AcquisitionFieldValue(name="amount", value=Decimal(amount)),
            AcquisitionFieldValue(name="updated_at", value=_NOW),
        ),
    )


def _invoice_record(record_key: str) -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="invoice",
        record_key=record_key,
        source_created_at=None,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(
                name="invoice_id",
                value=int(record_key.rsplit(":", 1)[1]),
            ),
        ),
    )


def _boundary(
    record_count: int,
    *,
    logical_object_ref: str = "order",
    schema_digest: str | None = None,
    upper_cursor_digest: str = "4" * 64,
    private_boundary_ref: str = "private-boundary-a",
    opened_at: datetime = _NOW,
    closed_at: datetime = _NOW,
    acquisition_mode: Literal["snapshot", "incremental", "reconciliation"] = "snapshot",
    lower_cursor_digest: str | None = None,
) -> AcquisitionBoundary:
    return AcquisitionBoundary(
        logical_object_ref=logical_object_ref,
        acquisition_mode=acquisition_mode,
        schema_digest=schema_digest or _schema().schema_digest,
        lower_cursor_digest=lower_cursor_digest,
        upper_cursor_digest=upper_cursor_digest,
        query_shape_digest="5" * 64,
        snapshot_identity_digest="6" * 64,
        key_range_digest="7" * 64,
        private_boundary_ref=private_boundary_ref,
        record_count=record_count,
        opened_at=opened_at,
        closed_at=closed_at,
    )


class _Session:
    def __init__(
        self,
        records: tuple[AcquisitionRecord, ...],
        *,
        fail_at: int | None = None,
        abort_fails: bool = False,
        cursor_version: str = "postgresql-compound-v1",
        boundary: AcquisitionBoundary | None = None,
        boundaries: tuple[AcquisitionBoundary, ...] | None = None,
    ) -> None:
        self._records = records
        self._fail_at = fail_at
        self._abort_fails = abort_fails
        self._cursor_version = cursor_version
        self._boundary = boundary
        self._boundaries = boundaries
        self._index = 0
        self.aborted = False
        self.abort_count = 0

    def __iter__(self) -> _Session:
        return self

    def __next__(self) -> AcquisitionRecord:
        if self._fail_at == self._index:
            raise ValueError("provider iteration failed")
        if self._index == len(self._records):
            raise StopIteration
        record = self._records[self._index]
        self._index += 1
        return record

    def complete(self) -> CompletedAcquisition:
        if self._index != len(self._records):
            raise AcquisitionSessionIncomplete()
        return CompletedAcquisition(
            boundaries=self._boundaries or (self._boundary or _boundary(len(self._records)),),
            cursor_version=self._cursor_version,
            candidate_cursor_payload=_PRIVATE_CURSOR,
        )

    def abort(self) -> None:
        self.aborted = True
        self.abort_count += 1
        if self._abort_fails:
            raise RuntimeError("cleanup exposed a different failure")


class _Provider:
    def __init__(
        self,
        sessions: tuple[_Session, ...],
        *,
        expected_private_cursor: bytes | None = None,
        observation_connection_handle: str = "connection-handle-a",
        observation_object_identity: str = "private-object-identity",
        observation_tenant_id: str = "tenant-a",
        observation_source_binding_ref: str = "source-binding-a",
        observation_provider_kind: AcquisitionProviderKind = "postgresql",
        observation_read_only: bool | None = True,
        observation_evidence_safe: bool | None = True,
        observation_observed_at: datetime | None = _NOW,
        observation_capabilities: tuple[str, ...] | None = ("snapshot_read",),
        schemas: tuple[AcquisitionObjectSchema, ...] | None = None,
        observation_schemas: tuple[AcquisitionObjectSchema, ...] | None = None,
    ) -> None:
        self._sessions = iter(sessions)
        self._expected_private_cursor = expected_private_cursor
        self._observation_connection_handle = observation_connection_handle
        self._observation_object_identity = observation_object_identity
        self._observation_tenant_id = observation_tenant_id
        self._observation_source_binding_ref = observation_source_binding_ref
        self._observation_provider_kind = observation_provider_kind
        self._observation_read_only = observation_read_only
        self._observation_evidence_safe = observation_evidence_safe
        self._observation_observed_at = observation_observed_at
        self._observation_capabilities = observation_capabilities
        self._schemas = schemas or (_schema(),)
        self._observation_schemas = observation_schemas or self._schemas
        self.last_session: _Session | None = None

    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        assert request == SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=tuple(schema.logical_object_ref for schema in self._schemas),
        )
        observation = _observation(
            tenant_id=self._observation_tenant_id,
            source_binding_ref=self._observation_source_binding_ref,
            connection_handle=self._observation_connection_handle,
            object_identity=self._observation_object_identity,
            provider_kind=self._observation_provider_kind,
            schemas=self._observation_schemas,
        )
        return observation.model_copy(
            update={
                "object_observations": tuple(
                    item.model_copy(
                        update={
                            "provider_observation": item.provider_observation.model_copy(
                                update={
                                    "read_only": self._observation_read_only,
                                    "evidence_safe": self._observation_evidence_safe,
                                    "observed_at": self._observation_observed_at,
                                    "capabilities": self._observation_capabilities,
                                }
                            )
                        }
                    )
                    for item in observation.object_observations
                )
            }
        )

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _Session:
        assert intent.tenant_id == "tenant-a"
        assert schemas == self._schemas
        assert private_cursor == self._expected_private_cursor
        self.last_session = next(self._sessions)
        return self.last_session


class _RawPrematureCompletionSession(_Session):
    def complete(self) -> CompletedAcquisition:
        raise RuntimeError("raw driver completion failure account-private")


class _LeakyTypedCompletionFailureSession(_Session):
    def complete(self) -> CompletedAcquisition:
        raise _leaky_provider_error()


class _LegacyCompletionFailureSession(_Session):
    def complete(self) -> CompletedAcquisition:
        raise ProviderError("legacy completion account-private", "retryable")


class _MalformedCompletionSession(_Session):
    def complete(self) -> CompletedAcquisition:
        return cast(CompletedAcquisition, object())


class _ForgedCompletionSession(_Session):
    def __init__(self, records: tuple[AcquisitionRecord, ...], update: dict[str, object]) -> None:
        super().__init__(records)
        self._completion_update = update

    def complete(self) -> CompletedAcquisition:
        completion = super().complete()
        for field_name, value in self._completion_update.items():
            object.__setattr__(completion, field_name, value)
        return completion


class _AbortLookupFailureSession(_Session):
    def __getattribute__(self, name: str) -> object:
        if name == "abort":
            raise RuntimeError("cleanup exposed a different failure")
        return super().__getattribute__(name)


def _scenario() -> SourceConformanceScenario:
    return SourceConformanceScenario(
        request=SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=("order",),
        ),
        intent=_intent(),
        schemas=(_schema(),),
        private_cursor=None,
        expected_provider_kind="postgresql",
        expected_connection_handle="connection-handle-a",
        expected_capabilities=("snapshot_read",),
        observation_not_before=_NOW,
        expected_record_identities=(("order", "order:7"), ("order", "order:8")),
    )


def test_conformance_consumes_records_before_completion_and_hides_private_cursor() -> None:
    provider = _Provider((_Session((_record("order:7"), _record("order:8"))),))

    result = exercise_source_provider(provider, _scenario())

    assert tuple(record.record_key for record in result.records) == ("order:7", "order:8")
    assert (
        result.observation.object_observations[0].provider_observation.connection_handle
        == "connection-handle-a"
    )
    assert result.candidate_cursor_digest == hashlib.sha256(_PRIVATE_CURSOR).hexdigest()
    assert result.encoding_result.record_count == 2
    assert result.encoding_result.encoded_bytes > 0
    assert _PRIVATE_CURSOR.decode() not in repr(result)
    assert _PRIVATE_CURSOR.decode() not in repr(result.completion)


def test_conformance_aborts_and_classifies_raw_iteration_failure_when_cleanup_fails() -> None:
    session = _Session((_record("order:7"),), fail_at=0, abort_fails=True)
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert captured.value.__cause__ is None
    assert session.aborted is True


def test_conformance_rejects_malformed_provider_record_and_aborts() -> None:
    malformed = cast(
        AcquisitionRecord,
        {"logical_object_ref": "order", "private": "value"},
    )
    session = _Session((malformed,))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "malformed_acquisition_record"
    assert session.aborted is True


@pytest.mark.parametrize(
    "forged_record",
    (
        _record("order:7").model_copy(update={"operation": "delete"}),
        _record("order:7").model_copy(update={"source_updated_at": _NON_UTC}),
    ),
)
def test_conformance_revalidates_forged_provider_record_instances(
    forged_record: AcquisitionRecord,
) -> None:
    session = _Session((forged_record, _record("order:8")))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "malformed_acquisition_record"
    assert session.aborted is True


def test_conformance_rejects_contradictory_content_under_one_record_identity() -> None:
    session = _Session((_record("order:7"), _record("order:7", amount="11.00")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError, match="contradictory_record_identity") as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "contradictory_record_identity"
    assert session.aborted is True


def test_conformance_rejects_an_exact_duplicate_record_identity() -> None:
    session = _Session((_record("order:7"), _record("order:7")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "duplicate_record_identity"
    assert session.aborted is True


def test_conformance_rejects_incomplete_or_wrong_record_order() -> None:
    session = _Session((_record("order:8"), _record("order:7")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "record_order_or_completeness_mismatch"
    assert session.aborted is True


def test_conformance_compares_object_qualified_record_identities() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider((session,))
    scenario = replace(
        _scenario(),
        expected_record_identities=(("invoice", "order:7"), ("order", "order:8")),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "record_order_or_completeness_mismatch"
    assert session.aborted is True


def test_conformance_rejects_a_record_outside_the_schema_field_allowlist() -> None:
    record = AcquisitionRecord(
        logical_object_ref="order",
        record_key="order:7",
        source_created_at=None,
        source_updated_at=_NOW,
        fields=(
            AcquisitionFieldValue(name="order_id", value=7),
            AcquisitionFieldValue(name="updated_at", value=_NOW),
        ),
    )
    session = _Session((record, _record("order:8")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "record_field_allowlist_mismatch"
    assert session.aborted is True


@pytest.mark.parametrize(
    ("amount", "expected_reason"),
    (
        ("10.50", "record_field_type_mismatch"),
        (None, "record_field_nullability_mismatch"),
    ),
)
def test_conformance_rejects_field_values_that_violate_the_activated_schema(
    amount: str | None,
    expected_reason: str,
) -> None:
    record = AcquisitionRecord(
        logical_object_ref="order",
        record_key="order:7",
        source_created_at=None,
        source_updated_at=_NOW,
        fields=(
            AcquisitionFieldValue(name="order_id", value=7),
            AcquisitionFieldValue(name="amount", value=amount),
            AcquisitionFieldValue(name="updated_at", value=_NOW),
        ),
    )
    session = _Session((record, _record("order:8")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == expected_reason
    assert session.aborted is True


def _single_field_scenario(
    declared_field: AcquisitionField,
    value: AcquisitionScalar,
) -> tuple[_Provider, SourceConformanceScenario]:
    schema = AcquisitionObjectSchema(
        logical_object_ref="order",
        schema_digest=digest((declared_field,)),
        fields=(declared_field,),
        record_key_fields=(declared_field.name,),
        source_updated_at_field=None,
    )
    record = AcquisitionRecord(
        logical_object_ref="order",
        record_key="order:7",
        source_created_at=None,
        source_updated_at=None,
        fields=(AcquisitionFieldValue(name=declared_field.name, value=value),),
    )
    boundary = _boundary(1, schema_digest=schema.schema_digest)
    provider = _Provider((_Session((record,), boundary=boundary),), schemas=(schema,))
    scenario = replace(
        _scenario(),
        intent=_intent(schemas=(schema,)),
        schemas=(schema,),
        expected_record_identities=(("order", "order:7"),),
    )
    return provider, scenario


@pytest.mark.parametrize(
    ("declared_field", "value"),
    (
        (AcquisitionField(name="active", value_type="boolean", nullable=False), True),
        (AcquisitionField(name="name", value_type="string", nullable=False), "approved"),
        (AcquisitionField(name="missing", value_type="null", nullable=False), None),
    ),
)
def test_conformance_accepts_each_schema_value_kind_not_covered_by_the_default_fixture(
    declared_field: AcquisitionField,
    value: AcquisitionScalar,
) -> None:
    provider, scenario = _single_field_scenario(declared_field, value)

    result = exercise_source_provider(provider, scenario)

    assert result.records[0].fields[0].value == value


@pytest.mark.parametrize(
    ("declared_field", "value"),
    (
        (AcquisitionField(name="active", value_type="boolean", nullable=False), 1),
        (AcquisitionField(name="count", value_type="integer", nullable=False), True),
        (AcquisitionField(name="missing", value_type="null", nullable=False), "present"),
    ),
)
def test_conformance_rejects_values_with_python_types_that_only_look_compatible(
    declared_field: AcquisitionField,
    value: AcquisitionScalar,
) -> None:
    provider, scenario = _single_field_scenario(declared_field, value)

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "record_field_type_mismatch"


def test_conformance_validates_fields_after_a_nullable_value() -> None:
    fields = (
        AcquisitionField(name="optional_name", value_type="string", nullable=True),
        AcquisitionField(name="amount", value_type="decimal", nullable=False),
    )
    schema = AcquisitionObjectSchema(
        logical_object_ref="order",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("optional_name",),
        source_updated_at_field=None,
    )
    record = AcquisitionRecord(
        logical_object_ref="order",
        record_key="order:7",
        source_created_at=None,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(name="optional_name", value=None),
            AcquisitionFieldValue(name="amount", value="10.50"),
        ),
    )
    provider = _Provider(
        (_Session((record,), boundary=_boundary(1, schema_digest=schema.schema_digest)),),
        schemas=(schema,),
    )
    scenario = replace(
        _scenario(),
        intent=_intent(schemas=(schema,)),
        schemas=(schema,),
        expected_record_identities=(("order", "order:7"),),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "record_field_type_mismatch"


def test_conformance_rejects_a_record_outside_the_object_allowlist() -> None:
    record = AcquisitionRecord(
        logical_object_ref="invoice",
        record_key="invoice:7",
        source_created_at=None,
        source_updated_at=_NOW,
        fields=(AcquisitionFieldValue(name="invoice_id", value=7),),
    )
    session = _Session((record, _record("order:8")))
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "record_object_allowlist_mismatch"
    assert session.aborted is True


@pytest.mark.parametrize(
    ("boundary", "expected_reason"),
    (
        (_boundary(1), "boundary_record_count_mismatch"),
        (_boundary(2, schema_digest="9" * 64), "boundary_allowlist_mismatch"),
    ),
)
def test_conformance_rejects_inexact_boundary_counts_or_schema(
    boundary: AcquisitionBoundary,
    expected_reason: str,
) -> None:
    session = _Session((_record("order:7"), _record("order:8")), boundary=boundary)
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == expected_reason
    assert session.aborted is True


def test_conformance_revalidates_forged_completion_boundaries() -> None:
    boundary = _boundary(2).model_copy(update={"record_count": -1})
    session = _Session(
        (_record("order:7"), _record("order:8")),
        boundary=boundary,
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "malformed_completed_acquisition"
    assert session.aborted is True


@pytest.mark.parametrize(
    "completion_update",
    (
        {"cursor_version": 123},
        {"candidate_cursor_payload": 3},
    ),
)
def test_conformance_revalidates_the_entire_forged_completion_envelope(
    completion_update: dict[str, object],
) -> None:
    records = (_record("order:7"), _record("order:8"))
    session = _ForgedCompletionSession(records, completion_update)

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "malformed_completed_acquisition"
    assert session.abort_count == 1


def test_conformance_rejects_per_object_boundary_counts_assigned_to_the_wrong_objects() -> None:
    schemas = (_invoice_schema(), _schema())
    object_refs = tuple(schema.logical_object_ref for schema in schemas)
    intent = _intent(schemas=schemas)
    scenario = SourceConformanceScenario(
        request=SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=object_refs,
        ),
        intent=intent,
        schemas=schemas,
        private_cursor=None,
        expected_provider_kind="postgresql",
        expected_connection_handle="connection-handle-a",
        expected_capabilities=("snapshot_read",),
        observation_not_before=_NOW,
        expected_record_identities=(("invoice", "invoice:1"), ("order", "order:7")),
    )
    boundaries = (
        _boundary(
            0,
            logical_object_ref="invoice",
            schema_digest=schemas[0].schema_digest,
        ),
        _boundary(2, logical_object_ref="order", schema_digest=schemas[1].schema_digest),
    )
    session = _Session(
        (_invoice_record("invoice:1"), _record("order:7")),
        boundaries=boundaries,
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,), schemas=schemas), scenario)

    assert str(captured.value) == "boundary_record_count_mismatch"
    assert session.aborted is True


def test_conformance_requires_an_observation_for_every_requested_object_before_open() -> None:
    schemas = (_invoice_schema(), _schema())
    object_refs = tuple(schema.logical_object_ref for schema in schemas)
    incomplete_observation = _observation(schemas=(schemas[0],))
    intent = _intent(
        schemas=schemas,
        source_observation_digest=digest(incomplete_observation),
    )
    scenario = SourceConformanceScenario(
        request=SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=object_refs,
        ),
        intent=intent,
        schemas=schemas,
        private_cursor=None,
        expected_provider_kind="postgresql",
        expected_connection_handle="connection-handle-a",
        expected_capabilities=("snapshot_read",),
        observation_not_before=_NOW,
        expected_record_identities=(("invoice", "invoice:1"), ("order", "order:7")),
    )
    session = _Session((_invoice_record("invoice:1"), _record("order:7")))
    provider = _Provider(
        (session,),
        schemas=schemas,
        observation_schemas=(schemas[0],),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "observation_object_allowlist_mismatch"
    assert provider.last_session is None


@pytest.mark.parametrize(
    "boundary",
    (
        _boundary(2, acquisition_mode="incremental"),
        _boundary(2, lower_cursor_digest="8" * 64),
    ),
)
def test_conformance_rejects_boundary_mode_or_cursor_that_disagrees_with_the_intent(
    boundary: AcquisitionBoundary,
) -> None:
    session = _Session((_record("order:7"), _record("order:8")), boundary=boundary)
    provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "boundary_cursor_or_mode_mismatch"
    assert session.aborted is True


def test_conformance_rejects_a_raw_cursor_embedded_in_a_boundary_and_aborts() -> None:
    boundary = _boundary(2, private_boundary_ref=_PRIVATE_CURSOR.decode())
    session = _Session((_record("order:7"), _record("order:8")), boundary=boundary)

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "private_cursor_exposed"
    assert session.aborted is True


def test_conformance_rejects_an_encoded_cursor_embedded_in_the_cursor_version() -> None:
    records = (_record("order:7"), _record("order:8"))
    session = _Session(records, cursor_version=_PRIVATE_CURSOR.hex())

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "private_cursor_exposed"
    assert session.abort_count == 1


def test_conformance_accepts_an_incremental_boundary_from_the_exact_prior_checkpoint() -> None:
    prior_checkpoint_digest = "8" * 64
    base = _intent()
    intent = AcquisitionIntent(
        **base.model_dump(
            exclude={
                "intent_key",
                "acquisition_mode",
                "prior_checkpoint_revision",
                "prior_checkpoint_digest",
            }
        ),
        intent_key=acquisition_intent_key(
            tenant_id=base.tenant_id,
            run_intent_ref=base.run_intent_ref,
            contract_digest=base.contract_digest,
            source_binding_ref=base.source_binding_ref,
            acquisition_mode="incremental",
            object_refs=base.object_refs,
            prior_checkpoint_revision=1,
        ),
        acquisition_mode="incremental",
        prior_checkpoint_revision=1,
        prior_checkpoint_digest=prior_checkpoint_digest,
    )
    boundary = _boundary(
        2,
        acquisition_mode="incremental",
        lower_cursor_digest=prior_checkpoint_digest,
    )
    records = (_record("order:7"), _record("order:8"))
    provider = _Provider((_Session(records, boundary=boundary),))

    result = exercise_source_provider(provider, replace(_scenario(), intent=intent))

    assert result.completion.boundaries[0].lower_cursor_digest == prior_checkpoint_digest


def test_conformance_accepts_reconciliation_bound_to_the_exact_prior_checkpoint() -> None:
    prior_checkpoint_digest = hashlib.sha256(_PRIVATE_CURSOR).hexdigest()
    base = _intent()
    intent = AcquisitionIntent(
        **base.model_dump(
            exclude={
                "intent_key",
                "acquisition_mode",
                "prior_checkpoint_revision",
                "prior_checkpoint_digest",
            }
        ),
        intent_key=acquisition_intent_key(
            tenant_id=base.tenant_id,
            run_intent_ref=base.run_intent_ref,
            contract_digest=base.contract_digest,
            source_binding_ref=base.source_binding_ref,
            acquisition_mode="reconciliation",
            object_refs=base.object_refs,
            prior_checkpoint_revision=1,
        ),
        acquisition_mode="reconciliation",
        prior_checkpoint_revision=1,
        prior_checkpoint_digest=prior_checkpoint_digest,
    )
    boundary = _boundary(
        2,
        acquisition_mode="reconciliation",
        lower_cursor_digest=prior_checkpoint_digest,
    )
    records = (_record("order:7"), _record("order:8"))
    provider = _Provider(
        (_Session(records, boundary=boundary),),
        expected_private_cursor=_PRIVATE_CURSOR,
    )

    result = exercise_source_provider(
        provider,
        replace(_scenario(), intent=intent, private_cursor=_PRIVATE_CURSOR),
    )

    assert result.completion.boundaries[0].lower_cursor_digest == prior_checkpoint_digest


def test_conformance_rejects_reconciliation_that_advances_the_private_cursor() -> None:
    prior_checkpoint_digest = hashlib.sha256(b"prior-private-cursor").hexdigest()
    base = _intent()
    intent = AcquisitionIntent(
        **base.model_dump(
            exclude={
                "intent_key",
                "acquisition_mode",
                "prior_checkpoint_revision",
                "prior_checkpoint_digest",
            }
        ),
        intent_key=acquisition_intent_key(
            tenant_id=base.tenant_id,
            run_intent_ref=base.run_intent_ref,
            contract_digest=base.contract_digest,
            source_binding_ref=base.source_binding_ref,
            acquisition_mode="reconciliation",
            object_refs=base.object_refs,
            prior_checkpoint_revision=1,
        ),
        acquisition_mode="reconciliation",
        prior_checkpoint_revision=1,
        prior_checkpoint_digest=prior_checkpoint_digest,
    )
    boundary = _boundary(
        2,
        acquisition_mode="reconciliation",
        lower_cursor_digest=prior_checkpoint_digest,
    )
    prior_cursor = b"prior-private-cursor"
    session = _Session((_record("order:7"), _record("order:8")), boundary=boundary)

    with pytest.raises(SourceConformanceError, match="reconciliation_cursor_advanced"):
        exercise_source_provider(
            _Provider((session,), expected_private_cursor=prior_cursor),
            replace(_scenario(), intent=intent, private_cursor=prior_cursor),
        )

    assert session.aborted is True


def test_conformance_requires_the_observation_to_name_the_expected_connection_handle() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider((session,), observation_connection_handle="connection-handle-b")

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "observation_connection_handle_mismatch"
    assert provider.last_session is None
    assert session.aborted is False


def test_conformance_requires_the_observation_to_name_the_expected_provider_before_open() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider((session,), observation_provider_kind="stripe")
    stripe_observation = _observation(provider_kind="stripe")
    scenario = replace(
        _scenario(),
        intent=_intent(source_observation_digest=digest(stripe_observation)),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "observation_provider_kind_mismatch"
    assert provider.last_session is None


@pytest.mark.parametrize(
    ("tenant_id", "source_binding_ref"),
    (("tenant-b", "source-binding-a"), ("tenant-a", "source-binding-b")),
)
def test_conformance_requires_observation_tenant_and_binding_authority_before_open(
    tenant_id: str,
    source_binding_ref: str,
) -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider(
        (session,),
        observation_tenant_id=tenant_id,
        observation_source_binding_ref=source_binding_ref,
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "observation_authority_mismatch"
    assert provider.last_session is None
    assert session.aborted is False


def test_conformance_rejects_an_observation_that_does_not_match_the_admitted_digest() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    scenario = replace(
        _scenario(),
        intent=_intent(source_observation_digest="9" * 64),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), scenario)

    assert str(captured.value) == "observation_digest_mismatch"
    assert session.aborted is False


def test_conformance_requires_each_observed_schema_to_match_the_activated_schema() -> None:
    observation = _observation()
    first = observation.object_observations[0]
    mismatched_observation = observation.model_copy(
        update={
            "object_observations": (
                first.model_copy(
                    update={
                        "provider_observation": first.provider_observation.model_copy(
                            update={"schema_digest": "9" * 64}
                        )
                    }
                ),
            )
        }
    )

    class _MismatchedSchemaProvider(_Provider):
        def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
            return mismatched_observation

    scenario = replace(
        _scenario(),
        intent=_intent(source_observation_digest=digest(mismatched_observation)),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_MismatchedSchemaProvider(()), scenario)

    assert str(captured.value) == "observation_schema_mismatch"


@pytest.mark.parametrize(
    ("read_only", "evidence_safe", "observed_at", "capabilities", "expected_reason"),
    (
        (False, True, _NOW, ("snapshot_read",), "observation_read_only_required"),
        (True, False, _NOW, ("snapshot_read",), "observation_evidence_unsafe"),
        (True, True, None, ("snapshot_read",), "observation_stale"),
        (True, True, _NOW, (), "observation_capability_mismatch"),
    ),
)
def test_conformance_rejects_an_observation_without_strict_fresh_read_capabilities(
    read_only: bool,
    evidence_safe: bool,
    observed_at: datetime | None,
    capabilities: tuple[str, ...],
    expected_reason: str,
) -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider(
        (session,),
        observation_read_only=read_only,
        observation_evidence_safe=evidence_safe,
        observation_observed_at=observed_at,
        observation_capabilities=capabilities,
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == expected_reason
    assert session.aborted is False


def test_conformance_revalidates_a_forged_non_utc_observation() -> None:
    class _ForgedObservationProvider(_Provider):
        def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
            observation = super().observe_source(request)
            first = observation.object_observations[0]
            return observation.model_copy(
                update={
                    "object_observations": (
                        first.model_copy(
                            update={
                                "provider_observation": first.provider_observation.model_copy(
                                    update={"observed_at": _NON_UTC}
                                )
                            }
                        ),
                    )
                }
            )

    session = _Session((_record("order:7"), _record("order:8")))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_ForgedObservationProvider((session,)), _scenario())

    assert str(captured.value) == "malformed_source_observation"
    assert session.aborted is False


def test_conformance_rejects_an_observation_from_after_intent_admission() -> None:
    future_observation = _observation()
    first = future_observation.object_observations[0]
    future_observation = future_observation.model_copy(
        update={
            "object_observations": (
                first.model_copy(
                    update={
                        "provider_observation": first.provider_observation.model_copy(
                            update={"observed_at": _NOW + timedelta(microseconds=1)}
                        )
                    }
                ),
            )
        }
    )

    class _FutureObservationProvider(_Provider):
        def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
            return future_observation

    session = _Session((_record("order:7"), _record("order:8")))
    scenario = replace(
        _scenario(),
        intent=_intent(source_observation_digest=digest(future_observation)),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_FutureObservationProvider((session,)), scenario)

    assert str(captured.value) == "observation_from_future"
    assert session.abort_count == 0


@pytest.mark.parametrize(
    ("intent", "expected_limit", "expected_records_read"),
    (
        (_intent().model_copy(update={"record_ceiling": 1}), "records", 2),
        (_intent().model_copy(update={"encoded_byte_ceiling": 1}), "encoded_bytes", 1),
    ),
)
def test_conformance_aborts_the_whole_batch_when_an_intent_ceiling_is_crossed(
    intent: AcquisitionIntent,
    expected_limit: str,
    expected_records_read: int,
) -> None:
    session = _Session((_record("order:7"), _record("order:8"), _record("order:9")))
    provider = _Provider((session,))
    scenario = replace(_scenario(), intent=intent)

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        exercise_source_provider(provider, scenario)

    assert captured.value.limit_kind == expected_limit
    assert session.aborted is True
    assert session._index == expected_records_read


def test_abandoned_session_requires_premature_completion_failure_and_idempotent_abort() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider((session,))

    verify_abandoned_session(provider, _scenario())

    assert session.abort_count == 2


def test_incomplete_session_error_uses_the_stable_reason_code() -> None:
    error = AcquisitionSessionIncomplete()

    assert str(error) == "acquisition_session_incomplete"


def test_abandoned_session_cleans_up_when_observation_authority_is_invalid() -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    provider = _Provider((session,), observation_connection_handle="connection-handle-b")

    with pytest.raises(SourceConformanceError) as captured:
        verify_abandoned_session(provider, _scenario())

    assert str(captured.value) == "observation_connection_handle_mismatch"
    assert provider.last_session is None
    assert session.abort_count == 0


def test_abandoned_session_cleans_up_empty_and_failed_iterators() -> None:
    empty_session = _Session(())
    failed_session = _Session((_record("order:7"), _record("order:8")), fail_at=0)

    with pytest.raises(SourceConformanceError) as empty_failure:
        verify_abandoned_session(_Provider((empty_session,)), _scenario())
    with pytest.raises(SourceConformanceError) as iteration_failure:
        verify_abandoned_session(_Provider((failed_session,)), _scenario())

    assert str(empty_failure.value) == "abandoned_session_requires_multiple_records"
    assert str(iteration_failure.value) == "unclassified_provider_error"
    assert empty_session.abort_count == 1
    assert failed_session.abort_count == 1


@pytest.mark.parametrize("failure_phase", ("iterate", "complete"))
def test_abandoned_session_sanitizes_typed_provider_failures_and_aborts(
    failure_phase: str,
) -> None:
    records = (_record("order:7"), _record("order:8"))
    session: _Session
    if failure_phase == "iterate":
        session = _LeakyTypedFailingSession(records)
    else:
        session = _LeakyTypedCompletionFailureSession(records)

    with pytest.raises(AcquisitionProviderError) as captured:
        verify_abandoned_session(_Provider((session,)), _scenario())

    assert str(captured.value) == "postgresql acquisition failed: provider_unavailable"
    assert session.abort_count == 1


def test_abandoned_session_rejects_premature_success_and_non_idempotent_abort() -> None:
    premature_session = _Session((_record("order:7"),))
    abort_failure_session = _Session(
        (_record("order:7"), _record("order:8")),
        abort_fails=True,
    )

    with pytest.raises(SourceConformanceError) as premature_failure:
        verify_abandoned_session(_Provider((premature_session,)), _scenario())
    with pytest.raises(SourceConformanceError) as abort_failure:
        verify_abandoned_session(_Provider((abort_failure_session,)), _scenario())

    assert str(premature_failure.value) == "premature_completion_succeeded"
    assert str(abort_failure.value) == "abort_is_not_idempotent"
    assert premature_session.abort_count == 1
    assert abort_failure_session.abort_count == 1


def test_abandoned_session_rejects_a_raw_premature_completion_failure() -> None:
    session = _RawPrematureCompletionSession((_record("order:7"), _record("order:8")))

    with pytest.raises(SourceConformanceError) as captured:
        verify_abandoned_session(_Provider((session,)), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "account-private" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert session.abort_count == 1


class _RawObservationFailureProvider(_Provider):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        raise RuntimeError(f"raw observation failure for {request.source_binding_ref}")


class _RawOpenFailureProvider(_Provider):
    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _Session:
        raise RuntimeError(f"raw open failure for {intent.source_binding_ref}")


class _GenericProviderFailure(_Provider):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        raise ProviderError(
            f"raw generic provider failure for {request.source_binding_ref}",
            "retryable",
        )


class _GenericOpenProviderFailure(_Provider):
    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _Session:
        raise ProviderError(
            f"raw generic open failure for {intent.source_binding_ref}",
            "retryable",
        )


class _LeakyAcquisitionProviderError(AcquisitionProviderError):
    def __str__(self) -> str:
        return "driver credential sk_live_private"


def _leaky_provider_error() -> _LeakyAcquisitionProviderError:
    return _LeakyAcquisitionProviderError(
        provider_kind="postgresql",
        classification="transient_unavailable",
        reason_code="provider_unavailable",
    )


class _LeakyTypedObservationFailureProvider(_Provider):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        raise _leaky_provider_error()


class _LeakyTypedOpenFailureProvider(_Provider):
    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _Session:
        raise _leaky_provider_error()


class _LeakyTypedFailingSession(_Session):
    def __next__(self) -> AcquisitionRecord:
        raise _leaky_provider_error()


class _MalformedObservationProvider(_Provider):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        observation = super().observe_source(request)
        return observation.model_copy(update={"provider_kind": "private-provider"})


class _LegacyFailingSession(_Session):
    def __next__(self) -> AcquisitionRecord:
        raise ProviderError("raw provider record identifier", "retryable")


class _SpoofedConformanceFailureProvider(_Provider):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        raise SourceConformanceError("credential-private")


class _SpoofedConformanceFailureSession(_Session):
    def __next__(self) -> AcquisitionRecord:
        raise SourceConformanceError("credential-private")


class _SpoofedCeilingFailureSession(_Session):
    def __next__(self) -> AcquisitionRecord:
        raise AcquisitionCeilingExceeded(
            logical_object_ref="credential-private",
            limit_kind="records",
            ceiling=1,
        )


class _SpoofedCompletionFailureSession(_Session):
    def complete(self) -> CompletedAcquisition:
        raise SourceConformanceError("credential-private")


class _RawIteratorStartFailureSession(_Session):
    def __iter__(self) -> Iterator[AcquisitionRecord]:
        raise RuntimeError("credential-private")


class _TypedIteratorStartFailureSession(_Session):
    def __iter__(self) -> Iterator[AcquisitionRecord]:
        raise _leaky_provider_error()


@pytest.mark.parametrize(
    "provider",
    (_RawObservationFailureProvider(()), _RawOpenFailureProvider(())),
)
def test_conformance_contains_raw_observation_and_open_failures(provider: _Provider) -> None:
    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert captured.value.__cause__ is None


def test_conformance_contains_legacy_provider_errors_without_a_safe_reason_code() -> None:
    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_GenericProviderFailure(()), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "source-binding-a" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "failure_phase",
    ("observe", "open", "iterate"),
)
def test_conformance_reconstructs_typed_provider_errors_with_safe_text(
    failure_phase: str,
) -> None:
    if failure_phase == "observe":
        provider: _Provider = _LeakyTypedObservationFailureProvider(())
    elif failure_phase == "open":
        provider = _LeakyTypedOpenFailureProvider(())
    else:
        provider = _Provider((_LeakyTypedFailingSession((_record("order:7"),)),))

    with pytest.raises(AcquisitionProviderError) as captured:
        exercise_source_provider(provider, _scenario())

    assert type(captured.value) is AcquisitionProviderError
    assert str(captured.value) == "postgresql acquisition failed: provider_unavailable"
    assert captured.value.classification == "transient_unavailable"
    assert captured.value.reason_code == "provider_unavailable"
    assert captured.value.__cause__ is None


def test_conformance_rejects_a_forged_typed_provider_error_with_stable_text() -> None:
    forged_error = _leaky_provider_error()
    object.__setattr__(forged_error, "reason_code", "account-private-reason")

    class _ForgedTypedFailureProvider(_Provider):
        def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
            raise forged_error

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_ForgedTypedFailureProvider(()), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "account-private" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_conformance_aborts_after_a_typed_iteration_failure() -> None:
    session = _LeakyTypedFailingSession((_record("order:7"),))

    with pytest.raises(AcquisitionProviderError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "postgresql acquisition failed: provider_unavailable"
    assert session.abort_count == 1


@pytest.mark.parametrize(
    ("session_kind", "expected_error_type", "expected_reason"),
    (
        (
            "malformed",
            SourceConformanceError,
            "malformed_completed_acquisition",
        ),
        (
            "raw",
            SourceConformanceError,
            "unclassified_provider_error",
        ),
        (
            "legacy",
            SourceConformanceError,
            "unclassified_provider_error",
        ),
        (
            "typed",
            AcquisitionProviderError,
            "postgresql acquisition failed: provider_unavailable",
        ),
    ),
)
def test_conformance_contains_completion_boundary_failures(
    session_kind: str,
    expected_error_type: type[BaseException],
    expected_reason: str,
) -> None:
    records = (_record("order:7"), _record("order:8"))
    session_types: dict[str, type[_Session]] = {
        "malformed": _MalformedCompletionSession,
        "raw": _RawPrematureCompletionSession,
        "legacy": _LegacyCompletionFailureSession,
        "typed": _LeakyTypedCompletionFailureSession,
    }
    session = session_types[session_kind](records)

    with pytest.raises(expected_error_type) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == expected_reason
    assert "account-private" not in str(captured.value)
    assert session.abort_count == 1


def test_conformance_contains_a_malformed_observation_inside_the_provider_boundary() -> None:
    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_MalformedObservationProvider(()), _scenario())

    assert str(captured.value) == "malformed_source_observation"
    assert captured.value.__cause__ is None


def test_conformance_contains_legacy_provider_errors_during_session_open() -> None:
    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_GenericOpenProviderFailure(()), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "source-binding-a" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_conformance_contains_and_cleans_up_legacy_provider_errors_during_iteration() -> None:
    session = _LegacyFailingSession((_record("order:7"), _record("order:8")))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "record identifier" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert session.aborted is True


@pytest.mark.parametrize(
    "failure_phase",
    ("observe", "iterate_conformance", "iterate_ceiling", "complete"),
)
def test_conformance_does_not_trust_provider_thrown_internal_exception_types(
    failure_phase: str,
) -> None:
    records = (_record("order:7"), _record("order:8"))
    session: _Session | None = None
    if failure_phase == "observe":
        provider: _Provider = _SpoofedConformanceFailureProvider(())
    elif failure_phase == "iterate_conformance":
        session = _SpoofedConformanceFailureSession(records)
        provider = _Provider((session,))
    elif failure_phase == "iterate_ceiling":
        session = _SpoofedCeilingFailureSession(records)
        provider = _Provider((session,))
    else:
        session = _SpoofedCompletionFailureSession(records)
        provider = _Provider((session,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "credential-private" not in str(captured.value)
    assert captured.value.__cause__ is None
    if session is not None:
        assert session.abort_count == 1


@pytest.mark.parametrize("failure_kind", ("raw", "typed"))
def test_conformance_contains_iterator_start_failures_and_aborts(failure_kind: str) -> None:
    records = (_record("order:7"), _record("order:8"))
    if failure_kind == "raw":
        session: _Session = _RawIteratorStartFailureSession(records)
        expected_error_type: type[BaseException] = SourceConformanceError
        expected_reason = "unclassified_provider_error"
    else:
        session = _TypedIteratorStartFailureSession(records)
        expected_error_type = AcquisitionProviderError
        expected_reason = "postgresql acquisition failed: provider_unavailable"

    with pytest.raises(expected_error_type) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == expected_reason
    assert "credential-private" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert session.abort_count == 1


def test_conformance_aborts_and_contains_an_unexpected_post_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session((_record("order:7"), _record("order:8")))
    real_verification = source_conformance_module.verify_private_cursor_containment
    call_count = 0

    def fail_after_open(
        value: object,
        *,
        private_values: tuple[bytes | None, ...],
    ) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("credential-private")
        real_verification(value, private_values=private_values)

    monkeypatch.setattr(
        source_conformance_module,
        "verify_private_cursor_containment",
        fail_after_open,
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "unclassified_provider_error"
    assert "credential-private" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert session.abort_count == 1


def test_cleanup_lookup_failure_never_replaces_the_original_conformance_error() -> None:
    malformed = cast(
        AcquisitionRecord,
        {"logical_object_ref": "order", "private": "value"},
    )
    session = _AbortLookupFailureSession((malformed,))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(_Provider((session,)), _scenario())

    assert str(captured.value) == "malformed_acquisition_record"
    assert captured.value.__cause__ is None


def test_replay_conformance_rejects_different_canonical_output() -> None:
    provider = _Provider(
        (
            _Session((_record("order:7"), _record("order:8"))),
            _Session((_record("order:7"), _record("order:8", amount="12.00"))),
        )
    )

    with pytest.raises(SourceConformanceError, match="non_deterministic_replay"):
        verify_source_replay(provider, _scenario())


def test_replay_conformance_rejects_changed_cursor_version() -> None:
    records = (_record("order:7"), _record("order:8"))
    provider = _Provider(
        (
            _Session(records, cursor_version="postgresql-compound-v1"),
            _Session(records, cursor_version="postgresql-compound-v2"),
        )
    )

    with pytest.raises(SourceConformanceError, match="non_deterministic_replay"):
        verify_source_replay(provider, _scenario())


def test_replay_conformance_compares_semantic_boundary_fields() -> None:
    records = (_record("order:7"), _record("order:8"))
    provider = _Provider(
        (
            _Session(records, boundary=_boundary(2, upper_cursor_digest="4" * 64)),
            _Session(records, boundary=_boundary(2, upper_cursor_digest="8" * 64)),
        )
    )

    with pytest.raises(SourceConformanceError) as captured:
        verify_source_replay(provider, _scenario())

    assert str(captured.value) == "non_deterministic_replay"


def test_replay_conformance_ignores_run_specific_boundary_fields() -> None:
    records = (_record("order:7"), _record("order:8"))
    later = datetime(2026, 9, 1, 12, 1, tzinfo=UTC)
    provider = _Provider(
        (
            _Session(records, boundary=_boundary(2)),
            _Session(
                records,
                boundary=_boundary(
                    2,
                    private_boundary_ref="private-boundary-b",
                    opened_at=later,
                    closed_at=later,
                ),
            ),
        )
    )

    result = verify_source_replay(provider, _scenario())

    assert result.records == records


class _IterableOnlySession:
    def __iter__(self) -> Iterator[AcquisitionRecord]:
        return iter((_record("order:7"), _record("order:8")))

    def complete(self) -> CompletedAcquisition:
        return CompletedAcquisition(
            boundaries=(_boundary(2),),
            cursor_version="postgresql-compound-v1",
            candidate_cursor_payload=_PRIVATE_CURSOR,
        )

    def abort(self) -> None:
        pass


def test_session_protocol_allows_a_separate_record_iterator() -> None:
    assert isinstance(_IterableOnlySession(), AcquisitionSession)


class _OwnershipError(RuntimeError):
    pass


class _SpecificOwnershipError(_OwnershipError):
    pass


class _StringOnlyCursorLeak:
    def __repr__(self) -> str:
        return "<safe-representation>"

    def __str__(self) -> str:
        return _PRIVATE_CURSOR.decode()


class _ReprOnlyCursorLeak:
    def __repr__(self) -> str:
        return repr(_PRIVATE_CURSOR)

    def __str__(self) -> str:
        return "<safe-string>"


class _HiddenCursorLeak:
    def __init__(self) -> None:
        self.hidden_cursor = _PRIVATE_CURSOR

    def __repr__(self) -> str:
        return "<safe-representation>"

    def __str__(self) -> str:
        return "<safe-string>"


class _HiddenMapping(Mapping[object, object]):
    def __init__(self, entries: tuple[tuple[object, object], ...]) -> None:
        self._entries = dict(entries)

    def __getitem__(self, key: object) -> object:
        return self._entries[key]

    def __iter__(self) -> Iterator[object]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return "<safe-mapping>"


class _HiddenSequence(Sequence[object]):
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return "<safe-sequence>"


class _HiddenSet(set[object]):
    def __repr__(self) -> str:
        return "<safe-set>"


@dataclass(frozen=True, slots=True)
class _HiddenDataclass:
    hidden_cursor: bytes = field(repr=False)


def test_cross_tenant_conformance_requires_the_declared_denial() -> None:
    verify_cross_tenant_denial(
        lambda: (_ for _ in ()).throw(_OwnershipError("cross_tenant_denied")),
        denial_error=_OwnershipError,
    )

    with pytest.raises(SourceConformanceError) as allowed:
        verify_cross_tenant_denial(lambda: object(), denial_error=_OwnershipError)
    with pytest.raises(SourceConformanceError) as wrong_error:
        verify_cross_tenant_denial(
            lambda: (_ for _ in ()).throw(ValueError("raw tenant leak")),
            denial_error=_OwnershipError,
        )
    with pytest.raises(SourceConformanceError) as broad_builtin:
        verify_cross_tenant_denial(
            lambda: (_ for _ in ()).throw(RuntimeError("raw tenant leak")),
            denial_error=RuntimeError,
        )
    with pytest.raises(SourceConformanceError) as wrong_subclass:
        verify_cross_tenant_denial(
            lambda: (_ for _ in ()).throw(_SpecificOwnershipError("not found")),
            denial_error=_OwnershipError,
        )
    with pytest.raises(SourceConformanceError) as sensitive_message:
        verify_cross_tenant_denial(
            lambda: (_ for _ in ()).throw(_OwnershipError("tenant-b-secret")),
            denial_error=_OwnershipError,
        )

    assert str(allowed.value) == "cross_tenant_access_allowed"
    assert str(wrong_error.value) == "cross_tenant_denial_error_mismatch"
    assert str(broad_builtin.value) == "cross_tenant_denial_type_is_not_domain_specific"
    assert str(wrong_subclass.value) == "cross_tenant_denial_error_mismatch"
    assert str(sensitive_message.value) == "cross_tenant_denial_error_mismatch"


def test_private_cursor_conformance_rejects_rendered_cursor_bytes() -> None:
    verify_private_cursor_containment(object(), private_values=(_PRIVATE_CURSOR,))

    with pytest.raises(SourceConformanceError, match="private_cursor_exposed"):
        verify_private_cursor_containment(
            f"diagnostic: {_PRIVATE_CURSOR!r}",
            private_values=(_PRIVATE_CURSOR,),
        )
    with pytest.raises(SourceConformanceError, match="private_cursor_exposed"):
        verify_private_cursor_containment(
            _PRIVATE_CURSOR.decode(),
            private_values=(None, _PRIVATE_CURSOR),
        )
    with pytest.raises(SourceConformanceError, match="private_cursor_exposed"):
        verify_private_cursor_containment(
            _StringOnlyCursorLeak(),
            private_values=(_PRIVATE_CURSOR,),
        )
    with pytest.raises(SourceConformanceError, match="private_cursor_exposed"):
        verify_private_cursor_containment(
            _ReprOnlyCursorLeak(),
            private_values=(_PRIVATE_CURSOR,),
        )
    with pytest.raises(SourceConformanceError, match="private_cursor_exposed"):
        verify_private_cursor_containment(
            _HiddenCursorLeak(),
            private_values=(_PRIVATE_CURSOR,),
        )
    verify_private_cursor_containment(
        object(),
        private_values=(b"\xff",),
    )


@pytest.mark.parametrize(
    "encoded_cursor",
    (
        _PRIVATE_CURSOR.hex(),
        _PRIVATE_CURSOR.hex().upper(),
        base64.b64encode(_PRIVATE_CURSOR).decode("ascii"),
        base64.b64encode(_PRIVATE_CURSOR).rstrip(b"=").decode("ascii"),
        base64.urlsafe_b64encode(_PRIVATE_CURSOR).decode("ascii"),
        base64.urlsafe_b64encode(_PRIVATE_CURSOR).rstrip(b"=").decode("ascii"),
    ),
)
def test_private_cursor_conformance_rejects_reversible_cursor_encodings(
    encoded_cursor: str,
) -> None:
    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(
            {"diagnostic": encoded_cursor},
            private_values=(_PRIVATE_CURSOR,),
        )

    assert str(captured.value) == "private_cursor_exposed"


@pytest.mark.parametrize(
    "encoded_cursor",
    (
        b"abc".hex(),
        base64.b64encode(b"abc").decode("ascii"),
        base64.urlsafe_b64encode(b"abc").decode("ascii"),
    ),
)
def test_private_cursor_conformance_rejects_short_reversible_cursor_encodings(
    encoded_cursor: str,
) -> None:
    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(encoded_cursor, private_values=(b"abc",))

    assert str(captured.value) == "private_cursor_exposed"


@pytest.mark.parametrize(
    ("private_cursor", "encoded_cursor"),
    (
        (b"ab", base64.b64encode(b"ab").rstrip(b"=").decode("ascii")),
        (b"ab", base64.urlsafe_b64encode(b"ab").rstrip(b"=").decode("ascii")),
        (b"\xfb\xff", base64.b64encode(b"\xfb\xff").rstrip(b"=").decode("ascii")),
        (b"\xfb\xff", base64.urlsafe_b64encode(b"\xfb\xff").rstrip(b"=").decode("ascii")),
    ),
)
def test_private_cursor_conformance_rejects_unpadded_standard_and_urlsafe_base64(
    private_cursor: bytes,
    encoded_cursor: str,
) -> None:
    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(encoded_cursor, private_values=(private_cursor,))

    assert str(captured.value) == "private_cursor_exposed"


def test_acquisition_observation_envelopes_have_stable_versioned_fields() -> None:
    observation = _observation()

    assert observation.schema_version == "1"
    assert observation.object_observations[0].schema_version == "1"
    assert tuple(AcquisitionObjectObservation.model_fields) == (
        "schema_version",
        "logical_object_ref",
        "provider_observation",
    )
    assert tuple(AcquisitionSourceObservation.model_fields) == (
        "schema_version",
        "tenant_id",
        "source_binding_ref",
        "provider_kind",
        "object_observations",
    )


def test_private_cursor_conformance_applies_encoding_canaries_at_the_minimum_length() -> None:
    private_cursor = b"12345678"

    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(
            private_cursor.hex(),
            private_values=(private_cursor,),
        )

    assert str(captured.value) == "private_cursor_exposed"


@pytest.mark.parametrize(
    "encoded_cursor",
    (
        _PRIVATE_CURSOR.hex().encode("ascii"),
        base64.b64encode(_PRIVATE_CURSOR),
        base64.urlsafe_b64encode(_PRIVATE_CURSOR),
    ),
)
def test_private_cursor_conformance_rejects_encoded_cursor_bytes(
    encoded_cursor: bytes,
) -> None:
    verify_private_cursor_containment(
        b"safe diagnostic",
        private_values=(_PRIVATE_CURSOR,),
    )

    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(
            encoded_cursor,
            private_values=(_PRIVATE_CURSOR,),
        )

    assert str(captured.value) == "private_cursor_exposed"


@pytest.mark.parametrize(
    "value",
    (
        _HiddenMapping(((_PRIVATE_CURSOR, "safe"),)),
        _HiddenMapping((("safe", _PRIVATE_CURSOR),)),
        _HiddenSequence((_PRIVATE_CURSOR,)),
        _HiddenSet((_PRIVATE_CURSOR,)),
        _HiddenDataclass(_PRIVATE_CURSOR),
    ),
)
def test_private_cursor_conformance_traverses_structural_containers(value: object) -> None:
    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(value, private_values=(_PRIVATE_CURSOR,))

    assert str(captured.value) == "private_cursor_exposed"


def test_private_cursor_conformance_handles_cycles_without_treating_them_as_leaks() -> None:
    cycle: list[object] = []
    cycle.append(cycle)

    verify_private_cursor_containment(cycle, private_values=(_PRIVATE_CURSOR,))


def test_private_cursor_conformance_does_not_confuse_distinct_nested_objects() -> None:
    value = (_HiddenCursorLeak(), _HiddenCursorLeak())
    value[0].hidden_cursor = b"safe"

    with pytest.raises(SourceConformanceError) as captured:
        verify_private_cursor_containment(value, private_values=(_PRIVATE_CURSOR,))

    assert str(captured.value) == "private_cursor_exposed"


def test_conformance_passes_private_prior_cursor_and_scans_the_result() -> None:
    records = (_record("order:7"), _record("order:8"))
    scenario = replace(_scenario(), private_cursor=_PRIVATE_CURSOR)
    provider = _Provider(
        (_Session(records),),
        expected_private_cursor=_PRIVATE_CURSOR,
        observation_object_identity=_PRIVATE_CURSOR.decode(),
    )

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == "private_cursor_exposed"
    assert provider.last_session is None


def test_conformance_passes_private_prior_cursor_to_the_open_session() -> None:
    records = (_record("order:7"), _record("order:8"))
    scenario = replace(_scenario(), private_cursor=_PRIVATE_CURSOR)
    provider = _Provider(
        (_Session(records),),
        expected_private_cursor=_PRIVATE_CURSOR,
    )

    result = exercise_source_provider(provider, scenario)

    assert result.encoding_result.record_count == 2
    assert provider.last_session is not None


def _checkpoint_lifecycle() -> tuple[
    AcquisitionPreparedReceipt,
    AcquisitionAcknowledgement,
    AcquisitionCheckpointReceipt,
]:
    prepared = AcquisitionPreparedReceipt(
        prepared_receipt_id="prepared-a",
        tenant_id="tenant-a",
        intent_key="1" * 64,
        batch_id="2" * 64,
        batch_manifest_digest="3" * 64,
        prior_checkpoint_revision=4,
        candidate_checkpoint_digest="5" * 64,
        cursor_version="postgresql-compound-v1",
        prepared_at=_NOW,
    )
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement-a",
        tenant_id="tenant-a",
        consumer_ref="consumer-a",
        contract_digest="6" * 64,
        source_binding_ref="source-binding-a",
        batch_id=prepared.batch_id,
        batch_manifest_digest=prepared.batch_manifest_digest,
        prior_checkpoint_revision=prepared.prior_checkpoint_revision,
        candidate_checkpoint_digest=prepared.candidate_checkpoint_digest,
        consumer_receipt_digest="7" * 64,
        acknowledged_at=_NOW,
    )
    checkpoint = AcquisitionCheckpointReceipt(
        checkpoint_receipt_id="checkpoint-a",
        tenant_id="tenant-a",
        contract_digest=acknowledgement.contract_digest,
        source_binding_ref=acknowledgement.source_binding_ref,
        previous_revision=prepared.prior_checkpoint_revision,
        committed_revision=prepared.prior_checkpoint_revision + 1,
        cursor_digest=prepared.candidate_checkpoint_digest,
        batch_id=prepared.batch_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        committed_at=_NOW,
    )
    return prepared, acknowledgement, checkpoint


def test_checkpoint_lifecycle_requires_exact_prepared_acknowledged_linkage() -> None:
    prepared, acknowledgement, checkpoint = _checkpoint_lifecycle()

    verify_checkpoint_lifecycle(prepared, acknowledgement, checkpoint)

    wrong_acknowledgement = acknowledgement.model_copy(update={"batch_id": "8" * 64})
    with pytest.raises(SourceConformanceError) as captured:
        verify_checkpoint_lifecycle(prepared, wrong_acknowledgement, checkpoint)

    assert str(captured.value) == "checkpoint_lifecycle_mismatch"


@pytest.mark.parametrize(
    "checkpoint_update",
    (
        {"committed_revision": 99},
        {"committed_at": _NON_UTC},
    ),
)
def test_checkpoint_lifecycle_revalidates_forged_receipt_instances(
    checkpoint_update: dict[str, object],
) -> None:
    prepared, acknowledgement, checkpoint = _checkpoint_lifecycle()
    forged_checkpoint = checkpoint.model_copy(update=checkpoint_update)

    with pytest.raises(SourceConformanceError) as captured:
        verify_checkpoint_lifecycle(prepared, acknowledgement, forged_checkpoint)

    assert str(captured.value) == "malformed_checkpoint_lifecycle"


@pytest.mark.parametrize("forged_receipt", ("prepared", "acknowledgement"))
def test_checkpoint_lifecycle_revalidates_each_upstream_receipt(
    forged_receipt: str,
) -> None:
    prepared, acknowledgement, checkpoint = _checkpoint_lifecycle()
    if forged_receipt == "prepared":
        prepared = prepared.model_copy(update={"prepared_at": _NON_UTC})
    else:
        acknowledgement = acknowledgement.model_copy(update={"acknowledged_at": _NON_UTC})

    with pytest.raises(SourceConformanceError) as captured:
        verify_checkpoint_lifecycle(prepared, acknowledgement, checkpoint)

    assert str(captured.value) == "malformed_checkpoint_lifecycle"


def test_acquisition_provider_error_exposes_only_safe_classification_and_reason() -> None:
    error = AcquisitionProviderError(
        provider_kind="stripe",
        classification="throttled",
        reason_code="rate_limited",
    )

    assert str(error) == "stripe acquisition failed: rate_limited"
    assert error.provider_kind == "stripe"
    assert error.classification == "throttled"
    assert error.reason_code == "rate_limited"

    with pytest.raises(ValueError) as invalid_reason:
        AcquisitionProviderError(
            provider_kind="stripe",
            classification="authorization_denied",
            reason_code=cast(AcquisitionProviderReasonCode, "acct_private_123"),
        )
    with pytest.raises(ValueError) as invalid_provider:
        AcquisitionProviderError(
            provider_kind=cast(AcquisitionProviderKind, "sk_live_private"),
            classification="authorization_denied",
            reason_code="authorization_denied",
        )
    with pytest.raises(ValueError) as invalid_classification:
        AcquisitionProviderError(
            provider_kind="stripe",
            classification=cast(AcquisitionProviderErrorClassification, "retryable"),
            reason_code="rate_limited",
        )
    with pytest.raises(ValueError) as contradictory_pair:
        AcquisitionProviderError(
            provider_kind="stripe",
            classification="transient_transport",
            reason_code="authorization_denied",
        )

    assert str(invalid_reason.value) == "reason_code must be allowlisted"
    assert str(invalid_provider.value) == "provider_kind must be allowlisted"
    assert str(invalid_classification.value) == "classification must be allowlisted"
    assert str(contradictory_pair.value) == "classification must match reason_code"


@pytest.mark.parametrize(
    ("reason_code", "classification"),
    (
        ("ambiguous_outcome", "ambiguous_outcome"),
        ("authorization_denied", "authorization_denied"),
        ("integrity_failure", "integrity_failure"),
        ("invalid_provider_response", "invalid_provider_response"),
        ("permanent_configuration", "permanent_configuration"),
        ("provider_unavailable", "transient_unavailable"),
        ("rate_limited", "throttled"),
        ("statement_rejected", "statement_rejected"),
        ("transport_failure", "transient_transport"),
    ),
)
def test_acquisition_provider_error_accepts_each_canonical_reason_classification_pair(
    reason_code: AcquisitionProviderReasonCode,
    classification: AcquisitionProviderErrorClassification,
) -> None:
    error = AcquisitionProviderError(
        provider_kind="postgresql",
        classification=classification,
        reason_code=reason_code,
    )

    assert error.classification == classification
    assert error.reason_code == reason_code


@pytest.mark.parametrize(
    "reason_code",
    ("stripe_event_cursor_expired", "stripe_event_overlap_gap"),
)
def test_stripe_continuity_errors_have_one_governed_classification(
    reason_code: AcquisitionProviderReasonCode,
) -> None:
    error = AcquisitionProviderError(
        provider_kind="stripe",
        classification="resynchronization_required",
        reason_code=reason_code,
    )

    assert error.classification == "resynchronization_required"
    with pytest.raises(ValueError, match="stripe provider"):
        AcquisitionProviderError(
            provider_kind="postgresql",
            classification="resynchronization_required",
            reason_code=reason_code,
        )


def test_observation_request_rejects_duplicate_or_noncanonical_objects() -> None:
    with pytest.raises(ValidationError, match="unique"):
        SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=("order", "order"),
        )
    with pytest.raises(ValidationError, match="canonical order"):
        SourceObservationRequest(
            tenant_id="tenant-a",
            source_binding_ref="source-binding-a",
            object_refs=("subscription", "order"),
        )


@pytest.mark.parametrize(
    ("scenario", "expected_reason"),
    (
        (
            replace(
                _scenario(),
                request=_scenario().request.model_copy(update={"tenant_id": "tenant-b"}),
            ),
            "scenario_authority_mismatch",
        ),
        (
            replace(
                _scenario(),
                request=_scenario().request.model_copy(
                    update={"source_binding_ref": "source-binding-b"}
                ),
            ),
            "scenario_authority_mismatch",
        ),
        (
            replace(
                _scenario(),
                request=_scenario().request.model_copy(update={"object_refs": ("invoice",)}),
            ),
            "scenario_object_allowlist_mismatch",
        ),
        (
            replace(
                _scenario(),
                schemas=(_schema().model_copy(update={"logical_object_ref": "invoice"}),),
            ),
            "scenario_object_allowlist_mismatch",
        ),
        (
            replace(
                _scenario(),
                expected_capabilities=("snapshot_read", "snapshot_read"),
            ),
            "scenario_capability_allowlist_mismatch",
        ),
    ),
)
def test_conformance_rejects_one_sided_scenario_authority_or_allowlist_mismatch(
    scenario: SourceConformanceScenario,
    expected_reason: str,
) -> None:
    provider = _Provider((_Session((_record("order:7"), _record("order:8"))),))

    with pytest.raises(SourceConformanceError) as captured:
        exercise_source_provider(provider, scenario)

    assert str(captured.value) == expected_reason


def test_provider_sdk_exports_the_shared_acquisition_boundary() -> None:
    from heinzel_provider_sdk import AcquisitionArtifactReader as PublicArtifactReader
    from heinzel_provider_sdk import AcquisitionObjectObservation as PublicObjectObservation
    from heinzel_provider_sdk import AcquisitionProvider as PublicProvider
    from heinzel_provider_sdk import AcquisitionSession as PublicSession
    from heinzel_provider_sdk import CompletedAcquisition as PublicCompletion
    from heinzel_provider_sdk import EncodedAcquisitionSegment as PublicEncodedSegment
    from heinzel_provider_sdk import SourceConformanceScenario as PublicScenario
    from heinzel_provider_sdk import verify_abandoned_session as PublicAbandonedSession
    from heinzel_provider_sdk import verify_checkpoint_lifecycle as PublicCheckpointLifecycle
    from heinzel_provider_sdk import verify_cross_tenant_denial as PublicCrossTenantDenial
    from heinzel_provider_sdk import (
        verify_private_cursor_containment as PublicCursorContainment,
    )

    assert PublicArtifactReader is not None
    assert PublicObjectObservation is AcquisitionObjectObservation
    assert PublicProvider is not None
    assert PublicSession is not None
    assert PublicCompletion is CompletedAcquisition
    assert PublicEncodedSegment is not None
    assert PublicScenario is SourceConformanceScenario
    assert PublicAbandonedSession is verify_abandoned_session
    assert PublicCheckpointLifecycle is verify_checkpoint_lifecycle
    assert PublicCrossTenantDenial is verify_cross_tenant_denial
    assert PublicCursorContainment is verify_private_cursor_containment
