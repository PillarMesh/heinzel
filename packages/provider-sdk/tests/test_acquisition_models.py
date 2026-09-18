from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TypedDict

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk.acquisition_models import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionBoundary,
    AcquisitionCheckpointReceipt,
    AcquisitionField,
    AcquisitionFieldValue,
    AcquisitionIntent,
    AcquisitionMode,
    AcquisitionNoValidPlan,
    AcquisitionObjectSchema,
    AcquisitionPreparedReceipt,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    ResynchronizationRequired,
    acquisition_batch_id,
    acquisition_intent_key,
)
from pydantic import ValidationError

_UTC_A = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_UTC_B = datetime(2026, 8, 31, 12, 5, tzinfo=UTC)


class _IntentIdentity(TypedDict):
    """The identity fields `acquisition_intent_key` hashes and the intent repeats.

    Typed so the `**identity` expansion below is checked against the key function
    and the model, rather than erased to `object`.
    """

    tenant_id: str
    run_intent_ref: str
    contract_digest: str
    source_binding_ref: str
    acquisition_mode: AcquisitionMode
    object_refs: tuple[str, ...]
    prior_checkpoint_revision: int


def _intent_payload() -> dict[str, object]:
    identity: _IntentIdentity = {
        "tenant_id": "tenant-a",
        "run_intent_ref": "1" * 64,
        "contract_digest": "2" * 64,
        "source_binding_ref": "source-binding-a",
        "acquisition_mode": "snapshot",
        "object_refs": ("account_segment", "order", "subscription"),
        "prior_checkpoint_revision": 0,
    }
    return {
        "intent_key": acquisition_intent_key(**identity),
        **identity,
        "contract_ref": "contract-a-v1",
        "source_observation_digest": "3" * 64,
        "prior_checkpoint_digest": None,
        "record_ceiling": 1_000,
        "encoded_byte_ceiling": 1_000_000,
        "admitted_at": _UTC_A,
    }


def test_intent_key_excludes_admission_time_and_binds_tenant_authority() -> None:
    first = AcquisitionIntent.model_validate(_intent_payload())
    replay = AcquisitionIntent.model_validate({**_intent_payload(), "admitted_at": _UTC_B})

    assert replay.intent_key == first.intent_key

    wrong_tenant = {**_intent_payload(), "tenant_id": "tenant-b"}
    with pytest.raises(ValidationError, match="intent_key does not match"):
        AcquisitionIntent.model_validate(wrong_tenant)


@pytest.mark.parametrize(
    ("update", "object_refs", "prior_revision", "message"),
    (
        ({"object_refs": ("order", "order")}, ("order", "order"), 0, "unique"),
        (
            {"object_refs": ("subscription", "order")},
            ("subscription", "order"),
            0,
            "canonical order",
        ),
        (
            {"prior_checkpoint_revision": 1},
            ("account_segment", "order", "subscription"),
            1,
            "prior_checkpoint_digest",
        ),
        (
            {"admitted_at": datetime(2026, 8, 31, 12, 0)},
            ("account_segment", "order", "subscription"),
            0,
            "timezone-aware UTC",
        ),
        (
            {"record_ceiling": 0},
            ("account_segment", "order", "subscription"),
            0,
            "greater than 0",
        ),
    ),
)
def test_intent_rejects_ambiguous_replay_inputs(
    update: dict[str, object],
    object_refs: tuple[str, ...],
    prior_revision: int,
    message: str,
) -> None:
    payload = {**_intent_payload(), **update}
    payload["intent_key"] = acquisition_intent_key(
        tenant_id="tenant-a",
        run_intent_ref="1" * 64,
        contract_digest="2" * 64,
        source_binding_ref="source-binding-a",
        acquisition_mode="snapshot",
        object_refs=object_refs,
        prior_checkpoint_revision=prior_revision,
    )

    with pytest.raises(ValidationError, match=message):
        AcquisitionIntent.model_validate(payload)


def test_intent_rejects_unknown_fields_and_mutation() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AcquisitionIntent.model_validate({**_intent_payload(), "raw_cursor": "private"})

    intent = AcquisitionIntent.model_validate(_intent_payload())
    with pytest.raises(ValidationError, match="frozen"):
        intent.tenant_id = "tenant-b"


def _order_schema() -> AcquisitionObjectSchema:
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
        operation_semantics="upsert_only",
    )


def test_object_schema_requires_exact_unique_field_references() -> None:
    schema = _order_schema()

    assert schema.fields[1].value_type == "decimal"

    with pytest.raises(ValidationError, match="field names must be unique"):
        AcquisitionObjectSchema.model_validate(
            {
                **schema.model_dump(),
                "fields": (schema.fields[0], schema.fields[0]),
                "schema_digest": digest((schema.fields[0], schema.fields[0])),
            }
        )
    with pytest.raises(ValidationError, match="record_key_fields"):
        AcquisitionObjectSchema.model_validate(
            {**schema.model_dump(), "record_key_fields": ("missing",)}
        )
    with pytest.raises(ValidationError, match="source_updated_at_field"):
        AcquisitionObjectSchema.model_validate(
            {**schema.model_dump(), "source_updated_at_field": "missing"}
        )


def test_record_fields_reject_non_scalar_and_naive_timestamp_values() -> None:
    valid = AcquisitionRecord(
        logical_object_ref="order",
        record_key="order:7",
        source_created_at=None,
        source_updated_at=_UTC_A,
        fields=(
            AcquisitionFieldValue(name="order_id", value=7),
            AcquisitionFieldValue(name="amount", value=Decimal("10.50")),
            AcquisitionFieldValue(name="updated_at", value=_UTC_A),
        ),
    )

    assert valid.operation == "upsert"

    # Both values are outside the declared scalar union; rejecting them is the
    # assertion, so the checker is right and the scope is one argument each.
    with pytest.raises(ValidationError, match="scalar"):
        AcquisitionFieldValue(name="amount", value={"nested": 1})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="floating-point"):
        AcquisitionFieldValue(name="amount", value=10.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        AcquisitionFieldValue(name="updated_at", value=datetime(2026, 8, 31, 12, 0))
    with pytest.raises(ValidationError, match="field names must be unique"):
        AcquisitionRecord.model_validate(
            {**valid.model_dump(), "fields": (valid.fields[0], valid.fields[0])}
        )


def _boundary(record_count: int = 1) -> AcquisitionBoundary:
    return AcquisitionBoundary(
        logical_object_ref="order",
        acquisition_mode="snapshot",
        schema_digest="4" * 64,
        lower_cursor_digest=None,
        upper_cursor_digest="5" * 64,
        query_shape_digest="6" * 64,
        snapshot_identity_digest="7" * 64,
        key_range_digest="8" * 64,
        private_boundary_ref="private-boundary-a",
        record_count=record_count,
        opened_at=_UTC_A,
        closed_at=_UTC_B,
    )


def _segment(record_count: int = 1, encoded_bytes: int = 128) -> AcquisitionSegmentManifest:
    return AcquisitionSegmentManifest(
        segment_name="0001-" + "9" * 64 + ".jsonl",
        logical_object_ref="order",
        record_schema_digest="4" * 64,
        boundary_digest=digest(_boundary(record_count)),
        content_digest="a" * 64,
        record_set_digest="b" * 64,
        record_count=record_count,
        encoded_bytes=encoded_bytes,
    )


class _BatchIdentity(TypedDict):
    intent_key: str
    prior_checkpoint_revision: int
    candidate_checkpoint_digest: str
    segment_manifests: tuple[AcquisitionSegmentManifest, ...]


def _batch() -> AcquisitionBatchManifest:
    segment = _segment()
    identity: _BatchIdentity = {
        "intent_key": "c" * 64,
        "prior_checkpoint_revision": 0,
        "candidate_checkpoint_digest": "d" * 64,
        "segment_manifests": (segment,),
    }
    return AcquisitionBatchManifest(
        batch_id=acquisition_batch_id(**identity),
        **identity,
        tenant_id="tenant-a",
        contract_ref="contract-a-v1",
        contract_digest="2" * 64,
        source_binding_ref="source-binding-a",
        source_observation_digest="3" * 64,
        acquisition_mode="snapshot",
        prior_checkpoint_digest=None,
        total_record_count=1,
        total_encoded_bytes=128,
        prepared_at=_UTC_A,
    )


def test_boundary_and_batch_reject_incomplete_or_contradictory_totals() -> None:
    with pytest.raises(ValidationError, match="closed_at"):
        AcquisitionBoundary.model_validate(
            {**_boundary().model_dump(), "closed_at": datetime(2026, 8, 31, 11, 59, tzinfo=UTC)}
        )

    batch = _batch()
    assert batch.batch_id == acquisition_batch_id(
        intent_key=batch.intent_key,
        prior_checkpoint_revision=batch.prior_checkpoint_revision,
        candidate_checkpoint_digest=batch.candidate_checkpoint_digest,
        segment_manifests=batch.segment_manifests,
    )

    with pytest.raises(ValidationError, match="total_record_count"):
        AcquisitionBatchManifest.model_validate({**batch.model_dump(), "total_record_count": 2})
    with pytest.raises(ValidationError, match="batch_id does not match"):
        AcquisitionBatchManifest.model_validate({**batch.model_dump(), "batch_id": "e" * 64})


def test_receipts_bind_exact_batch_and_checkpoint_identity() -> None:
    batch = _batch()
    prepared = AcquisitionPreparedReceipt(
        prepared_receipt_id="prepared-ref-a",
        tenant_id=batch.tenant_id,
        intent_key=batch.intent_key,
        batch_id=batch.batch_id,
        batch_manifest_digest=digest(batch),
        prior_checkpoint_revision=0,
        candidate_checkpoint_digest=batch.candidate_checkpoint_digest,
        cursor_version="postgresql-compound-v1",
        prepared_at=_UTC_A,
    )
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement-a",
        tenant_id=batch.tenant_id,
        consumer_ref="strict-test-consumer",
        contract_digest=batch.contract_digest,
        source_binding_ref=batch.source_binding_ref,
        batch_id=batch.batch_id,
        batch_manifest_digest=prepared.batch_manifest_digest,
        prior_checkpoint_revision=0,
        candidate_checkpoint_digest=batch.candidate_checkpoint_digest,
        consumer_receipt_digest="f" * 64,
        acknowledged_at=_UTC_B,
    )
    receipt = AcquisitionCheckpointReceipt(
        checkpoint_receipt_id="checkpoint-ref-a",
        tenant_id=batch.tenant_id,
        contract_digest=batch.contract_digest,
        source_binding_ref=batch.source_binding_ref,
        previous_revision=0,
        committed_revision=1,
        cursor_digest="0" * 64,
        batch_id=batch.batch_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        committed_at=_UTC_B,
    )

    assert receipt.committed_revision == receipt.previous_revision + 1

    with pytest.raises(ValidationError, match="committed_revision"):
        AcquisitionCheckpointReceipt.model_validate(
            {**receipt.model_dump(), "committed_revision": 2}
        )


def test_governed_outcomes_keep_private_constraints_out_of_public_shapes() -> None:
    no_plan = AcquisitionNoValidPlan(
        reason_codes=("physical_delete_capture_unsupported",),
        failed_constraints=("contract:delete-guarantee",),
    )
    resynchronization = ResynchronizationRequired(
        reason_code="stripe_event_cursor_expired",
        source_binding_ref="source-binding-a",
        affected_object_refs=("charge", "refund"),
        last_proven_checkpoint_digest="1" * 64,
        required_scope="all_approved_stripe_objects",
        created_at=_UTC_A,
    )

    assert no_plan.failed_constraints == ("contract:delete-guarantee",)
    assert resynchronization.last_proven_checkpoint_digest == "1" * 64


def test_provider_sdk_exports_acquisition_contracts_and_observes_stripe() -> None:
    from heinzel_provider_sdk import AcquisitionIntent as PublicAcquisitionIntent
    from heinzel_provider_sdk import ProviderObservation

    observation = ProviderObservation(
        provider="stripe",
        connection_handle="stripe-source-a",
        object_identity="private-account-digest",
        object_kind="unknown",
        schema_digest="1" * 64,
        columns=(),
        key_name=None,
        key_type=None,
        key_nullable=None,
        key_constraint=None,
        stable_key_order=None,
        read_only=True,
        capabilities=("event_read", "object_list"),
        observed_at=_UTC_A,
        snapshot_semantics="snapshot",
        commit_ledger_object_kind=None,
        commit_ledger_columns=None,
        commit_ledger_key_name=None,
        commit_ledger_key_constraint=None,
        evidence_safe=True,
    )

    assert PublicAcquisitionIntent is AcquisitionIntent
    assert observation.provider == "stripe"
