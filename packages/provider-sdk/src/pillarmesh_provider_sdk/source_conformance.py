from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from itertools import chain

from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import BaseModel

from .acquisition_encoding import (
    AcquisitionCeilingExceeded,
    EncodedAcquisitionSegment,
    encode_canonical_jsonl,
)
from .acquisition_models import (
    AcquisitionAcknowledgement,
    AcquisitionCheckpointReceipt,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
)
from .acquisition_protocols import (
    AcquisitionProvider,
    AcquisitionSession,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    CompletedAcquisition,
    SourceObservationRequest,
)
from .errors import AcquisitionProviderError, AcquisitionProviderKind, ProviderError


class SourceConformanceError(AssertionError):
    pass


@dataclass(frozen=True)
class SourceConformanceScenario:
    request: SourceObservationRequest
    intent: AcquisitionIntent
    schemas: tuple[AcquisitionObjectSchema, ...]
    private_cursor: bytes | None = field(repr=False)
    expected_provider_kind: AcquisitionProviderKind
    expected_connection_handle: str
    expected_capabilities: tuple[str, ...]
    observation_not_before: datetime
    expected_record_identities: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SourceConformanceResult:
    observation: AcquisitionSourceObservation
    records: tuple[AcquisitionRecord, ...]
    completion: CompletedAcquisition
    encoding_result: EncodedAcquisitionSegment

    @property
    def candidate_cursor_digest(self) -> str:
        return self.completion.candidate_cursor_digest


def _record_identity(record: AcquisitionRecord) -> tuple[str, str]:
    return record.logical_object_ref, record.record_key


def _abort_preserving_failure(session: object) -> None:
    with suppress(BaseException):
        abort = getattr(session, "abort", None)
        if callable(abort):
            abort()


def _revalidate_model[T: BaseModel](
    model_type: type[T],
    value: object,
    *,
    reason: str,
) -> T:
    try:
        payload = value.model_dump(warnings="none") if isinstance(value, BaseModel) else value
        return model_type.model_validate(payload)
    except BaseException:
        raise SourceConformanceError(reason) from None


def _sanitized_provider_error(error: AcquisitionProviderError) -> AcquisitionProviderError:
    try:
        return AcquisitionProviderError(
            provider_kind=error.provider_kind,
            classification=error.classification,
            reason_code=error.reason_code,
        )
    except BaseException:
        raise SourceConformanceError("unclassified_provider_error") from None


def _observe(
    provider: AcquisitionProvider,
    scenario: SourceConformanceScenario,
) -> AcquisitionSourceObservation:
    try:
        raw_observation = provider.observe_source(scenario.request)
    except AcquisitionProviderError as error:
        raise _sanitized_provider_error(error) from None
    except ProviderError:
        raise SourceConformanceError("unclassified_provider_error") from None
    except BaseException:
        raise SourceConformanceError("unclassified_provider_error") from None
    return _revalidate_model(
        AcquisitionSourceObservation,
        raw_observation,
        reason="malformed_source_observation",
    )


def _open(
    provider: AcquisitionProvider,
    scenario: SourceConformanceScenario,
) -> AcquisitionSession:
    try:
        return provider.open_acquisition(
            scenario.intent,
            scenario.schemas,
            scenario.private_cursor,
        )
    except AcquisitionProviderError as error:
        raise _sanitized_provider_error(error) from None
    except ProviderError:
        raise SourceConformanceError("unclassified_provider_error") from None
    except BaseException:
        raise SourceConformanceError("unclassified_provider_error") from None


def _validate_scenario(scenario: SourceConformanceScenario) -> None:
    schema_object_refs = tuple(schema.logical_object_ref for schema in scenario.schemas)
    if (
        scenario.request.tenant_id != scenario.intent.tenant_id
        or scenario.request.source_binding_ref != scenario.intent.source_binding_ref
    ):
        raise SourceConformanceError("scenario_authority_mismatch")
    if (
        scenario.request.object_refs != scenario.intent.object_refs
        or schema_object_refs != scenario.intent.object_refs
    ):
        raise SourceConformanceError("scenario_object_allowlist_mismatch")
    if scenario.expected_capabilities != tuple(sorted(set(scenario.expected_capabilities))):
        raise SourceConformanceError("scenario_capability_allowlist_mismatch")


def verify_private_cursor_containment(
    value: object,
    *,
    private_values: tuple[bytes | None, ...],
) -> None:
    private_bytes = tuple(private_value for private_value in private_values if private_value)
    private_byte_needles: set[bytes] = set(private_bytes)
    private_raw_text_needles: set[str] = set()
    private_encoded_text_needles: set[str] = set()
    for private_value in private_bytes:
        base64_value = base64.b64encode(private_value)
        urlsafe_base64_value = base64.urlsafe_b64encode(private_value)
        encoded_values = {
            private_value.hex().encode(),
            private_value.hex().upper().encode(),
            base64_value,
            base64_value.rstrip(b"="),
            urlsafe_base64_value,
            urlsafe_base64_value.rstrip(b"="),
        }
        private_byte_needles.update(encoded_values)
        private_encoded_text_needles.update(value.decode() for value in encoded_values)
        private_raw_text_needles.add(repr(private_value))
        with suppress(UnicodeDecodeError):
            private_raw_text_needles.add(private_value.decode())
    seen: set[int] = set()

    def contains_private(candidate: object) -> bool:
        if isinstance(candidate, (bytes, bytearray)):
            return any(needle in bytes(candidate) for needle in private_byte_needles)

        text_needles = private_raw_text_needles
        if isinstance(candidate, str):
            text_needles = text_needles | private_encoded_text_needles
        rendered_values = (repr(candidate), str(candidate))
        if any(needle in rendered for needle in text_needles for rendered in rendered_values):
            return True

        candidate_id = id(candidate)
        if candidate_id in seen:
            return False
        seen.add(candidate_id)

        if isinstance(candidate, Mapping):
            children: Iterable[object] = chain(candidate.keys(), candidate.values())
        elif isinstance(candidate, (Sequence, set, frozenset)) and not isinstance(candidate, str):
            children = candidate
        elif is_dataclass(candidate) and not isinstance(candidate, type):
            children = tuple(getattr(candidate, item.name) for item in fields(candidate))
        elif hasattr(candidate, "__dict__"):
            children = tuple(vars(candidate).values())
        else:
            children = ()
        return any(contains_private(child) for child in children)

    if contains_private(value):
        raise SourceConformanceError("private_cursor_exposed")


def verify_cross_tenant_denial(
    operation: Callable[[], object],
    *,
    denial_error: type[BaseException],
) -> None:
    if denial_error.__module__ == "builtins":
        raise SourceConformanceError("cross_tenant_denial_type_is_not_domain_specific")
    try:
        operation()
    except BaseException as error:
        if type(error) is denial_error and str(error) == "cross_tenant_denied":
            return
        raise SourceConformanceError("cross_tenant_denial_error_mismatch") from None
    raise SourceConformanceError("cross_tenant_access_allowed")


def _validate_record_fields(
    record: AcquisitionRecord,
    schema: AcquisitionObjectSchema,
) -> None:
    if tuple(field.name for field in record.fields) != tuple(field.name for field in schema.fields):
        raise SourceConformanceError("record_field_allowlist_mismatch")

    expected_types: dict[str, type[object]] = {
        "boolean": bool,
        "integer": int,
        "decimal": Decimal,
        "string": str,
        "timestamp": datetime,
    }
    for value, declared in zip(record.fields, schema.fields, strict=True):
        if value.value is None:
            if declared.nullable or declared.value_type == "null":
                continue
            raise SourceConformanceError("record_field_nullability_mismatch")
        if declared.value_type == "null":
            raise SourceConformanceError("record_field_type_mismatch")
        expected_type = expected_types[declared.value_type]
        if type(value.value) is not expected_type:
            raise SourceConformanceError("record_field_type_mismatch")


def verify_checkpoint_lifecycle(
    prepared: AcquisitionPreparedReceipt,
    acknowledgement: AcquisitionAcknowledgement,
    checkpoint: AcquisitionCheckpointReceipt,
) -> None:
    prepared = _revalidate_model(
        AcquisitionPreparedReceipt,
        prepared,
        reason="malformed_checkpoint_lifecycle",
    )
    acknowledgement = _revalidate_model(
        AcquisitionAcknowledgement,
        acknowledgement,
        reason="malformed_checkpoint_lifecycle",
    )
    checkpoint = _revalidate_model(
        AcquisitionCheckpointReceipt,
        checkpoint,
        reason="malformed_checkpoint_lifecycle",
    )
    prepared_linkage = (
        prepared.tenant_id,
        prepared.batch_id,
        prepared.batch_manifest_digest,
        prepared.prior_checkpoint_revision,
        prepared.candidate_checkpoint_digest,
    )
    acknowledgement_linkage = (
        acknowledgement.tenant_id,
        acknowledgement.batch_id,
        acknowledgement.batch_manifest_digest,
        acknowledgement.prior_checkpoint_revision,
        acknowledgement.candidate_checkpoint_digest,
    )
    checkpoint_linkage = (
        checkpoint.tenant_id,
        checkpoint.batch_id,
        checkpoint.previous_revision,
        checkpoint.cursor_digest,
        checkpoint.acknowledgement_id,
        checkpoint.contract_digest,
        checkpoint.source_binding_ref,
    )
    expected_checkpoint_linkage = (
        prepared.tenant_id,
        prepared.batch_id,
        prepared.prior_checkpoint_revision,
        prepared.candidate_checkpoint_digest,
        acknowledgement.acknowledgement_id,
        acknowledgement.contract_digest,
        acknowledgement.source_binding_ref,
    )
    if (
        prepared_linkage != acknowledgement_linkage
        or checkpoint_linkage != expected_checkpoint_linkage
    ):
        raise SourceConformanceError("checkpoint_lifecycle_mismatch")


def verify_abandoned_session(
    provider: AcquisitionProvider,
    scenario: SourceConformanceScenario,
) -> None:
    _validate_scenario(scenario)
    observation = _observe(provider, scenario)
    _validate_observation(
        observation,
        scenario,
        private_values=(scenario.private_cursor,),
    )
    provider_session = _open(provider, scenario)
    try:
        iterator = iter(provider_session)
        next(iterator)
    except StopIteration:
        _abort_preserving_failure(provider_session)
        raise SourceConformanceError("abandoned_session_requires_multiple_records") from None
    except AcquisitionProviderError as error:
        _abort_preserving_failure(provider_session)
        raise _sanitized_provider_error(error) from None
    except BaseException:
        _abort_preserving_failure(provider_session)
        raise SourceConformanceError("unclassified_provider_error") from None

    try:
        provider_session.complete()
    except AcquisitionSessionIncomplete:
        pass
    except AcquisitionProviderError as error:
        _abort_preserving_failure(provider_session)
        raise _sanitized_provider_error(error) from None
    except BaseException:
        _abort_preserving_failure(provider_session)
        raise SourceConformanceError("unclassified_provider_error") from None
    else:
        _abort_preserving_failure(provider_session)
        raise SourceConformanceError("premature_completion_succeeded")

    try:
        provider_session.abort()
        provider_session.abort()
    except BaseException:
        raise SourceConformanceError("abort_is_not_idempotent") from None


def _validate_observation(
    observation: AcquisitionSourceObservation,
    scenario: SourceConformanceScenario,
    *,
    private_values: tuple[bytes | None, ...],
) -> None:
    verify_private_cursor_containment(observation, private_values=private_values)
    if (
        observation.tenant_id != scenario.intent.tenant_id
        or observation.source_binding_ref != scenario.intent.source_binding_ref
    ):
        raise SourceConformanceError("observation_authority_mismatch")
    if observation.provider_kind != scenario.expected_provider_kind:
        raise SourceConformanceError("observation_provider_kind_mismatch")
    observed_object_refs = tuple(
        item.logical_object_ref for item in observation.object_observations
    )
    if observed_object_refs != scenario.request.object_refs:
        raise SourceConformanceError("observation_object_allowlist_mismatch")
    for item, schema in zip(
        observation.object_observations,
        scenario.schemas,
        strict=True,
    ):
        provider_observation = item.provider_observation
        if provider_observation.schema_digest != schema.schema_digest:
            raise SourceConformanceError("observation_schema_mismatch")
        if provider_observation.connection_handle != scenario.expected_connection_handle:
            raise SourceConformanceError("observation_connection_handle_mismatch")
        if provider_observation.read_only is not True:
            raise SourceConformanceError("observation_read_only_required")
        if provider_observation.evidence_safe is not True:
            raise SourceConformanceError("observation_evidence_unsafe")
        if (
            provider_observation.observed_at is None
            or provider_observation.observed_at < scenario.observation_not_before
        ):
            raise SourceConformanceError("observation_stale")
        if provider_observation.observed_at > scenario.intent.admitted_at:
            raise SourceConformanceError("observation_from_future")
        if provider_observation.capabilities != scenario.expected_capabilities:
            raise SourceConformanceError("observation_capability_mismatch")
    if digest(observation) != scenario.intent.source_observation_digest:
        raise SourceConformanceError("observation_digest_mismatch")


def _complete(session: AcquisitionSession) -> CompletedAcquisition:
    try:
        raw_completion = session.complete()
    except AcquisitionProviderError as error:
        raise _sanitized_provider_error(error) from None
    except ProviderError:
        raise SourceConformanceError("unclassified_provider_error") from None
    except BaseException:
        raise SourceConformanceError("unclassified_provider_error") from None
    return _revalidate_model(
        CompletedAcquisition,
        raw_completion,
        reason="malformed_completed_acquisition",
    )


def _provider_records(session: AcquisitionSession) -> Iterator[object]:
    try:
        iterator = iter(session)
    except AcquisitionProviderError as error:
        raise _sanitized_provider_error(error) from None
    except BaseException:
        raise SourceConformanceError("unclassified_provider_error") from None

    while True:
        try:
            raw_record = next(iterator)
        except StopIteration:
            return
        except AcquisitionProviderError as error:
            raise _sanitized_provider_error(error) from None
        except BaseException:
            raise SourceConformanceError("unclassified_provider_error") from None
        yield raw_record


def exercise_source_provider(
    provider: AcquisitionProvider,
    scenario: SourceConformanceScenario,
) -> SourceConformanceResult:
    _validate_scenario(scenario)
    observation = _observe(provider, scenario)
    _validate_observation(
        observation,
        scenario,
        private_values=(scenario.private_cursor,),
    )
    provider_session = _open(provider, scenario)
    records: list[AcquisitionRecord] = []
    seen: dict[tuple[str, str], bytes] = {}
    schemas = {schema.logical_object_ref: schema for schema in scenario.schemas}

    def validated_records() -> Iterator[AcquisitionRecord]:
        for raw_record in _provider_records(provider_session):
            record = _revalidate_model(
                AcquisitionRecord,
                raw_record,
                reason="malformed_acquisition_record",
            )
            schema = schemas.get(record.logical_object_ref)
            if schema is None:
                raise SourceConformanceError("record_object_allowlist_mismatch")
            _validate_record_fields(record, schema)
            identity = _record_identity(record)
            payload = canonical_bytes(record)
            prior = seen.get(identity)
            if prior is not None:
                reason = (
                    "duplicate_record_identity"
                    if prior == payload
                    else "contradictory_record_identity"
                )
                raise SourceConformanceError(reason)
            seen[identity] = payload
            records.append(record)
            yield record

    try:
        encoding_result = encode_canonical_jsonl(
            validated_records(),
            BytesIO(),
            record_ceiling=scenario.intent.record_ceiling,
            encoded_byte_ceiling=scenario.intent.encoded_byte_ceiling,
        )
        completion = _complete(provider_session)
        verify_private_cursor_containment(
            (observation, completion.boundaries, completion.cursor_version),
            private_values=(scenario.private_cursor, completion.candidate_cursor_payload),
        )
        record_identities = tuple(_record_identity(record) for record in records)
        if record_identities != scenario.expected_record_identities:
            raise SourceConformanceError("record_order_or_completeness_mismatch")
        boundary_identity = tuple(
            (boundary.logical_object_ref, boundary.schema_digest)
            for boundary in completion.boundaries
        )
        expected_boundary_identity = tuple(
            (schema.logical_object_ref, schema.schema_digest) for schema in scenario.schemas
        )
        if boundary_identity != expected_boundary_identity:
            raise SourceConformanceError("boundary_allowlist_mismatch")
        record_counts = Counter(record.logical_object_ref for record in records)
        if any(
            boundary.record_count != record_counts[boundary.logical_object_ref]
            for boundary in completion.boundaries
        ):
            raise SourceConformanceError("boundary_record_count_mismatch")
        expected_lower_cursor = (
            scenario.intent.prior_checkpoint_digest
            if scenario.intent.acquisition_mode in {"incremental", "reconciliation"}
            else None
        )
        if any(
            boundary.acquisition_mode != scenario.intent.acquisition_mode
            or boundary.lower_cursor_digest != expected_lower_cursor
            for boundary in completion.boundaries
        ):
            raise SourceConformanceError("boundary_cursor_or_mode_mismatch")
        if scenario.intent.acquisition_mode == "reconciliation" and (
            scenario.private_cursor is None
            or completion.candidate_cursor_payload != scenario.private_cursor
            or completion.candidate_cursor_digest != scenario.intent.prior_checkpoint_digest
        ):
            raise SourceConformanceError("reconciliation_cursor_advanced")
    except AcquisitionProviderError as error:
        _abort_preserving_failure(provider_session)
        raise _sanitized_provider_error(error) from None
    except (AcquisitionCeilingExceeded, SourceConformanceError):
        _abort_preserving_failure(provider_session)
        raise
    except BaseException:
        _abort_preserving_failure(provider_session)
        raise SourceConformanceError("unclassified_provider_error") from None

    result = SourceConformanceResult(
        observation=observation,
        records=tuple(records),
        completion=completion,
        encoding_result=encoding_result,
    )
    return result


def _replay_signature(result: SourceConformanceResult) -> tuple[object, ...]:
    semantic_boundaries = tuple(
        boundary.model_dump(exclude={"opened_at", "closed_at", "private_boundary_ref"})
        for boundary in result.completion.boundaries
    )
    return (
        tuple(canonical_bytes(record) for record in result.records),
        semantic_boundaries,
        result.completion.cursor_version,
        result.candidate_cursor_digest,
    )


def verify_source_replay(
    provider: AcquisitionProvider,
    scenario: SourceConformanceScenario,
) -> SourceConformanceResult:
    first = exercise_source_provider(provider, scenario)
    replay = exercise_source_provider(provider, scenario)
    if _replay_signature(first) != _replay_signature(replay):
        raise SourceConformanceError("non_deterministic_replay")
    return replay
