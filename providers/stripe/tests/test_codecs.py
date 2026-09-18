from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import AcquisitionFieldValue, AcquisitionRecord
from heinzel_provider_stripe.codecs import (
    StripeCodecError,
    normalize_stripe_event,
    normalize_stripe_object,
)

API_VERSION = "2026-02-25.clover"
IDENTITY_METADATA_KEY = "heinzel_customer_identity"
CREATED_EPOCH = 1_700_000_000
EVENT_CREATED_EPOCH = 1_700_000_120
CREATED_AT = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
EVENT_CREATED_AT = datetime(2023, 11, 14, 22, 15, 20, tzinfo=UTC)


def customer_payload() -> dict[str, object]:
    return {
        "id": "cus_approved",
        "object": "customer",
        "created": CREATED_EPOCH,
        "livemode": False,
        "metadata": {IDENTITY_METADATA_KEY: "crm-customer-42"},
    }


def invoice_payload() -> dict[str, object]:
    return {
        "id": "in_approved",
        "object": "invoice",
        "created": CREATED_EPOCH,
        "livemode": False,
        "customer": "cus_approved",
        "currency": "usd",
        "amount_due": 2_500,
        "amount_paid": 2_500,
        "amount_remaining": 0,
        "status": "paid",
        "paid": True,
        "status_transitions": {
            "finalized_at": CREATED_EPOCH + 30,
            "marked_uncollectible_at": None,
            "paid_at": CREATED_EPOCH + 60,
            "voided_at": None,
        },
        "metadata": {},
    }


def charge_payload() -> dict[str, object]:
    return {
        "id": "ch_approved",
        "object": "charge",
        "created": CREATED_EPOCH,
        "livemode": False,
        "customer": "cus_approved",
        "currency": "usd",
        "amount": 2_500,
        "amount_captured": 2_500,
        "amount_refunded": 500,
        "paid": True,
        "refunded": False,
        "status": "succeeded",
        "metadata": {},
    }


def refund_payload() -> dict[str, object]:
    return {
        "id": "re_approved",
        "object": "refund",
        "created": CREATED_EPOCH,
        "livemode": False,
        "charge": "ch_approved",
        "currency": "usd",
        "amount": 500,
        "status": "succeeded",
        "metadata": {},
    }


def expected_customer() -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="customers",
        record_key=digest({"logical_object_ref": "customers", "key": "cus_approved"}),
        source_created_at=CREATED_AT,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(name="customer_id", value="cus_approved"),
            AcquisitionFieldValue(name="created", value=CREATED_AT),
            AcquisitionFieldValue(name="deleted", value=False),
            AcquisitionFieldValue(name="customer_identity", value="crm-customer-42"),
        ),
    )


def expected_invoice() -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="invoices",
        record_key=digest({"logical_object_ref": "invoices", "key": "in_approved"}),
        source_created_at=CREATED_AT,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(name="invoice_id", value="in_approved"),
            AcquisitionFieldValue(name="customer_id", value="cus_approved"),
            AcquisitionFieldValue(name="created", value=CREATED_AT),
            AcquisitionFieldValue(name="currency", value="usd"),
            AcquisitionFieldValue(name="amount_due", value=2_500),
            AcquisitionFieldValue(name="amount_paid", value=2_500),
            AcquisitionFieldValue(name="amount_remaining", value=0),
            AcquisitionFieldValue(name="status", value="paid"),
            AcquisitionFieldValue(name="paid", value=True),
            AcquisitionFieldValue(
                name="paid_at",
                value=datetime(2023, 11, 14, 22, 14, 20, tzinfo=UTC),
            ),
        ),
    )


def expected_charge() -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="charges",
        record_key=digest({"logical_object_ref": "charges", "key": "ch_approved"}),
        source_created_at=CREATED_AT,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(name="charge_id", value="ch_approved"),
            AcquisitionFieldValue(name="customer_id", value="cus_approved"),
            AcquisitionFieldValue(name="invoice_id", value=None),
            AcquisitionFieldValue(name="created", value=CREATED_AT),
            AcquisitionFieldValue(name="currency", value="usd"),
            AcquisitionFieldValue(name="amount", value=2_500),
            AcquisitionFieldValue(name="amount_captured", value=2_500),
            AcquisitionFieldValue(name="amount_refunded", value=500),
            AcquisitionFieldValue(name="paid", value=True),
            AcquisitionFieldValue(name="refunded", value=False),
            AcquisitionFieldValue(name="status", value="succeeded"),
        ),
    )


def expected_refund() -> AcquisitionRecord:
    return AcquisitionRecord(
        logical_object_ref="refunds",
        record_key=digest({"logical_object_ref": "refunds", "key": "re_approved"}),
        source_created_at=CREATED_AT,
        source_updated_at=None,
        fields=(
            AcquisitionFieldValue(name="refund_id", value="re_approved"),
            AcquisitionFieldValue(name="charge_id", value="ch_approved"),
            AcquisitionFieldValue(name="created", value=CREATED_AT),
            AcquisitionFieldValue(name="currency", value="usd"),
            AcquisitionFieldValue(name="amount", value=500),
            AcquisitionFieldValue(name="status", value="succeeded"),
        ),
    )


type _PayloadFactory = Callable[[], dict[str, object]]
type _RecordFactory = Callable[[], AcquisitionRecord]
type _LogicalObjectRef = Literal["charges", "customers", "invoices", "refunds"]

RESOURCE_CASES: tuple[tuple[_LogicalObjectRef, _PayloadFactory, _RecordFactory], ...] = (
    ("customers", customer_payload, expected_customer),
    ("invoices", invoice_payload, expected_invoice),
    ("charges", charge_payload, expected_charge),
    ("refunds", refund_payload, expected_refund),
)


def normalize_object(
    logical_object_ref: _LogicalObjectRef,
    payload: dict[str, object],
    *,
    expected_livemode: bool = False,
    creation_version: str = API_VERSION,
    identity_metadata_key: str | None = IDENTITY_METADATA_KEY,
) -> AcquisitionRecord:
    return normalize_stripe_object(
        payload,
        logical_object_ref=logical_object_ref,
        expected_livemode=expected_livemode,
        creation_version=creation_version,
        customer_identity_metadata_key=identity_metadata_key,
    )


def assert_codec_error(
    reason_code: str,
    callback: Callable[[], AcquisitionRecord],
) -> None:
    with pytest.raises(StripeCodecError) as captured:
        callback()

    assert captured.value.reason_code == reason_code


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory", "expected_factory"),
    RESOURCE_CASES,
)
def test_versioned_object_codecs_return_exact_acquisition_records(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
    expected_factory: _RecordFactory,
) -> None:
    record = normalize_object(logical_object_ref, payload_factory())

    assert type(record) is AcquisitionRecord
    assert record == expected_factory()


def test_normalized_records_preserve_exact_scalar_types() -> None:
    invoice = normalize_object("invoices", invoice_payload())
    values = {field.name: field.value for field in invoice.fields}

    assert type(values["amount_due"]) is int
    assert type(values["paid"]) is bool
    assert type(values["created"]) is datetime
    assert type(values["currency"]) is str
    assert values["paid_at"] == datetime(2023, 11, 14, 22, 14, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory", "wrong_discriminator"),
    (
        ("customers", customer_payload, "invoice"),
        ("invoices", invoice_payload, "charge"),
        ("charges", charge_payload, "refund"),
        ("refunds", refund_payload, "customer"),
    ),
)
def test_object_codecs_reject_the_wrong_object_discriminator(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
    wrong_discriminator: str,
) -> None:
    payload = payload_factory()
    payload["object"] = wrong_discriminator

    assert_codec_error(
        "wrong_object_discriminator",
        lambda: normalize_object(logical_object_ref, payload),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory"),
    tuple(
        (logical_object_ref, payload_factory)
        for logical_object_ref, payload_factory, _ in RESOURCE_CASES
    ),
)
def test_object_codecs_reject_live_and_test_mode_mixing(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
) -> None:
    payload = payload_factory()
    payload["livemode"] = True

    assert_codec_error(
        "livemode_mismatch",
        lambda: normalize_object(logical_object_ref, payload, expected_livemode=False),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory", "relationship"),
    (
        ("invoices", invoice_payload, "customer"),
        ("charges", charge_payload, "customer"),
        ("refunds", refund_payload, "charge"),
    ),
)
def test_object_codecs_reject_expanded_relationships(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
    relationship: str,
) -> None:
    payload = payload_factory()
    payload[relationship] = {"id": payload[relationship], "object": relationship}

    assert_codec_error(
        "expansion_not_allowed",
        lambda: normalize_object(logical_object_ref, payload),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory", "field_name", "sensitive_value"),
    (
        ("customers", customer_payload, "email", "private@example.test"),
        ("invoices", invoice_payload, "customer_email", "private@example.test"),
        ("charges", charge_payload, "receipt_url", "https://private.example.test/receipt"),
        (
            "refunds",
            refund_payload,
            "destination_details",
            {"card": {"reference": "private-card-reference"}},
        ),
    ),
)
def test_object_codecs_reject_sensitive_fields_instead_of_silently_normalizing_them(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
    field_name: str,
    sensitive_value: object,
) -> None:
    payload = payload_factory()
    payload[field_name] = sensitive_value

    assert_codec_error(
        "sensitive_field_not_allowed",
        lambda: normalize_object(logical_object_ref, payload),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory"),
    tuple(
        (logical_object_ref, payload_factory)
        for logical_object_ref, payload_factory, _ in RESOURCE_CASES
    ),
)
def test_object_codecs_reject_unknown_fields_after_versioned_normalization(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
) -> None:
    payload = payload_factory()
    payload["future_unreviewed_field"] = "not-authority"

    assert_codec_error(
        "field_not_allowlisted",
        lambda: normalize_object(logical_object_ref, payload),
    )


def test_customer_codec_supports_exactly_one_approved_identity_metadata_key() -> None:
    accepted = normalize_object("customers", customer_payload())
    accepted_fields = {field.name: field.value for field in accepted.fields}
    assert accepted_fields["customer_identity"] == "crm-customer-42"

    absent = customer_payload()
    absent["metadata"] = {}
    absent_fields = {
        field.name: field.value for field in normalize_object("customers", absent).fields
    }
    assert absent_fields["customer_identity"] is None

    unknown = customer_payload()
    unknown["metadata"] = {"other_identity": "private-customer"}
    assert_codec_error(
        "metadata_not_allowed",
        lambda: normalize_object("customers", unknown),
    )

    unrestricted = customer_payload()
    unrestricted["metadata"] = {
        IDENTITY_METADATA_KEY: "crm-customer-42",
        "campaign": "private-campaign",
    }
    assert_codec_error(
        "metadata_not_allowed",
        lambda: normalize_object("customers", unrestricted),
    )

    assert_codec_error(
        "metadata_not_allowed",
        lambda: normalize_object(
            "customers",
            customer_payload(),
            identity_metadata_key=None,
        ),
    )


def test_invoice_codec_rejects_an_unknown_nested_status_transition() -> None:
    payload = invoice_payload()
    raw_transitions = payload["status_transitions"]
    assert isinstance(raw_transitions, dict)
    transitions = dict(raw_transitions)
    transitions["future_unreviewed_transition"] = CREATED_EPOCH
    payload["status_transitions"] = transitions

    assert_codec_error(
        "field_not_allowlisted",
        lambda: normalize_object("invoices", payload),
    )


def test_invoice_codec_validates_ignored_status_transition_scalars() -> None:
    payload = invoice_payload()
    raw_transitions = payload["status_transitions"]
    assert isinstance(raw_transitions, dict)
    transitions = dict(raw_transitions)
    transitions["voided_at"] = "not-an-epoch"
    payload["status_transitions"] = transitions

    assert_codec_error(
        "malformed_scalar",
        lambda: normalize_object("invoices", payload),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory"),
    (
        ("invoices", invoice_payload),
        ("charges", charge_payload),
        ("refunds", refund_payload),
    ),
)
def test_non_customer_codecs_reject_nonempty_metadata(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
) -> None:
    payload = payload_factory()
    payload["metadata"] = {IDENTITY_METADATA_KEY: "must-not-cross-resource-boundary"}

    assert_codec_error(
        "metadata_not_allowed",
        lambda: normalize_object(logical_object_ref, payload),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory"),
    tuple(
        (logical_object_ref, payload_factory)
        for logical_object_ref, payload_factory, _ in RESOURCE_CASES
    ),
)
def test_object_codecs_reject_unsupported_creation_versions(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
) -> None:
    assert_codec_error(
        "unsupported_creation_version",
        lambda: normalize_object(
            logical_object_ref,
            payload_factory(),
            creation_version="2025-12-15.preview",
        ),
    )


@pytest.mark.parametrize(
    ("logical_object_ref", "payload_factory", "field_name", "malformed_value"),
    (
        ("customers", customer_payload, "id", 7),
        ("customers", customer_payload, "created", True),
        ("customers", customer_payload, "livemode", 0),
        ("invoices", invoice_payload, "amount_due", True),
        ("invoices", invoice_payload, "currency", "US dollars"),
        ("invoices", invoice_payload, "paid", 1),
        ("invoices", invoice_payload, "status_transitions", []),
        ("charges", charge_payload, "amount", Decimal("2500")),
        ("charges", charge_payload, "refunded", 0),
        ("refunds", refund_payload, "status", None),
    ),
)
def test_object_codecs_reject_malformed_scalar_fields_without_coercion(
    logical_object_ref: _LogicalObjectRef,
    payload_factory: _PayloadFactory,
    field_name: str,
    malformed_value: object,
) -> None:
    payload = payload_factory()
    payload[field_name] = malformed_value

    assert_codec_error(
        "malformed_scalar",
        lambda: normalize_object(logical_object_ref, payload),
    )


APPROVED_EVENT_CASES: tuple[tuple[str, _PayloadFactory], ...] = (
    ("customer.created", customer_payload),
    ("customer.updated", customer_payload),
    ("invoice.created", invoice_payload),
    ("invoice.finalized", invoice_payload),
    ("invoice.marked_uncollectible", invoice_payload),
    ("invoice.paid", invoice_payload),
    ("invoice.payment_failed", invoice_payload),
    ("invoice.updated", invoice_payload),
    ("invoice.voided", invoice_payload),
    ("charge.captured", charge_payload),
    ("charge.failed", charge_payload),
    ("charge.pending", charge_payload),
    ("charge.refunded", charge_payload),
    ("charge.succeeded", charge_payload),
    ("charge.updated", charge_payload),
    ("refund.created", refund_payload),
    ("refund.failed", refund_payload),
    ("refund.updated", refund_payload),
)


def event_payload(event_type: str, object_payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": "evt_approved",
        "object": "event",
        "api_version": API_VERSION,
        "created": EVENT_CREATED_EPOCH,
        "livemode": False,
        "type": event_type,
        "data": {"object": object_payload},
    }


def normalize_event(payload: dict[str, object]) -> AcquisitionRecord:
    return normalize_stripe_event(
        payload,
        expected_livemode=False,
        customer_identity_metadata_key=IDENTITY_METADATA_KEY,
    )


@pytest.mark.parametrize(("event_type", "payload_factory"), APPROVED_EVENT_CASES)
def test_approved_event_types_use_their_creation_version_codec(
    event_type: str,
    payload_factory: _PayloadFactory,
) -> None:
    record = normalize_event(event_payload(event_type, payload_factory()))

    assert type(record) is AcquisitionRecord
    assert record.source_updated_at == EVENT_CREATED_AT
    assert (
        record.logical_object_ref
        == {
            "customer": "customers",
            "invoice": "invoices",
            "charge": "charges",
            "refund": "refunds",
        }[event_type.split(".", maxsplit=1)[0]]
    )


def test_customer_deleted_is_the_only_approved_minimal_tombstone_event() -> None:
    payload = event_payload(
        "customer.deleted",
        {"id": "cus_deleted", "object": "customer", "deleted": True},
    )

    record = normalize_event(payload)

    assert record == AcquisitionRecord(
        logical_object_ref="customers",
        record_key=digest({"logical_object_ref": "customers", "key": "cus_deleted"}),
        source_created_at=None,
        source_updated_at=EVENT_CREATED_AT,
        fields=(
            AcquisitionFieldValue(name="customer_id", value="cus_deleted"),
            AcquisitionFieldValue(name="created", value=None),
            AcquisitionFieldValue(name="deleted", value=True),
            AcquisitionFieldValue(name="customer_identity", value=None),
        ),
    )


@pytest.mark.parametrize(
    "event_type",
    (
        "customer.subscription.updated",
        "invoice.deleted",
        "payment_intent.succeeded",
        "charge.dispute.created",
        "payout.paid",
        "refund.succeeded",
        "future.resource.changed",
    ),
)
def test_event_codec_rejects_every_unknown_or_unapproved_event_type(event_type: str) -> None:
    assert_codec_error(
        "event_type_not_allowed",
        lambda: normalize_event(event_payload(event_type, customer_payload())),
    )


def test_event_codec_rejects_event_and_embedded_object_discriminator_mismatches() -> None:
    wrong_event = event_payload("customer.updated", customer_payload())
    wrong_event["object"] = "list"
    assert_codec_error(
        "wrong_object_discriminator",
        lambda: normalize_event(wrong_event),
    )

    wrong_embedded_object = event_payload("invoice.updated", charge_payload())
    assert_codec_error(
        "wrong_object_discriminator",
        lambda: normalize_event(wrong_embedded_object),
    )


def test_event_codec_rejects_live_mode_mismatch_at_either_level() -> None:
    wrong_event_mode = event_payload("charge.updated", charge_payload())
    wrong_event_mode["livemode"] = True
    assert_codec_error(
        "livemode_mismatch",
        lambda: normalize_event(wrong_event_mode),
    )

    wrong_object_mode = event_payload("charge.updated", charge_payload())
    embedded_charge = charge_payload()
    embedded_charge["livemode"] = True
    wrong_object_mode["data"] = {"object": embedded_charge}
    assert_codec_error(
        "livemode_mismatch",
        lambda: normalize_event(wrong_object_mode),
    )


def test_event_codec_rejects_unsupported_or_absent_creation_versions() -> None:
    unsupported = event_payload("refund.updated", refund_payload())
    unsupported["api_version"] = "2025-12-15.preview"
    assert_codec_error(
        "unsupported_creation_version",
        lambda: normalize_event(unsupported),
    )

    absent = event_payload("refund.updated", refund_payload())
    absent["api_version"] = None
    assert_codec_error(
        "unsupported_creation_version",
        lambda: normalize_event(absent),
    )


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    (
        ("id", 7),
        ("created", 1_700_000_120.5),
        ("livemode", 0),
        ("type", None),
        ("data", []),
    ),
)
def test_event_codec_rejects_malformed_envelope_scalars(
    field_name: str,
    malformed_value: object,
) -> None:
    payload = event_payload("invoice.updated", invoice_payload())
    payload[field_name] = malformed_value

    assert_codec_error(
        "malformed_scalar",
        lambda: normalize_event(payload),
    )


def test_event_codec_rejects_unreviewed_envelope_and_data_fields() -> None:
    envelope_drift = event_payload("invoice.updated", invoice_payload())
    envelope_drift["request"] = {"id": "private-request-id"}
    assert_codec_error(
        "field_not_allowlisted",
        lambda: normalize_event(envelope_drift),
    )

    data_drift = event_payload("invoice.updated", invoice_payload())
    data_drift["data"] = {
        "object": invoice_payload(),
        "previous_attributes": {"customer_email": "private@example.test"},
    }
    assert_codec_error(
        "field_not_allowlisted",
        lambda: normalize_event(data_drift),
    )
