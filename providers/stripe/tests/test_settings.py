from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import AcquisitionField
from heinzel_provider_stripe import (
    STRIPE_API_VERSION,
    StripeEventCursor,
    StripeObjectDeclaration,
    StripeSettings,
)
from pydantic import SecretStr, ValidationError

_TEST_KEY = "rk_test_private-key-canary"
_LIVE_KEY = "rk_live_private-key-canary"

_APPROVED_EVENT_TYPES = (
    "charge.captured",
    "charge.failed",
    "charge.pending",
    "charge.refunded",
    "charge.succeeded",
    "charge.updated",
    "customer.created",
    "customer.deleted",
    "customer.updated",
    "invoice.created",
    "invoice.finalized",
    "invoice.marked_uncollectible",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.updated",
    "invoice.voided",
    "refund.created",
    "refund.failed",
    "refund.updated",
)


def _field(name: str, value_type: str, *, nullable: bool = False) -> AcquisitionField:
    return AcquisitionField.model_validate(
        {"name": name, "value_type": value_type, "nullable": nullable}
    )


def _normalized_fields(object_kind: str) -> tuple[AcquisitionField, ...]:
    fields = {
        "charge": (
            _field("charge_id", "string"),
            _field("customer_id", "string", nullable=True),
            _field("invoice_id", "string", nullable=True),
            _field("created", "timestamp"),
            _field("currency", "string"),
            _field("amount", "integer"),
            _field("amount_captured", "integer"),
            _field("amount_refunded", "integer"),
            _field("paid", "boolean"),
            _field("refunded", "boolean"),
            _field("status", "string"),
        ),
        "customer": (
            _field("customer_id", "string"),
            _field("created", "timestamp", nullable=True),
            _field("deleted", "boolean"),
            _field("customer_identity", "string", nullable=True),
        ),
        "invoice": (
            _field("invoice_id", "string"),
            _field("customer_id", "string", nullable=True),
            _field("created", "timestamp"),
            _field("currency", "string"),
            _field("amount_due", "integer"),
            _field("amount_paid", "integer"),
            _field("amount_remaining", "integer"),
            _field("status", "string"),
            _field("paid", "boolean"),
            _field("paid_at", "timestamp", nullable=True),
        ),
        "refund": (
            _field("refund_id", "string"),
            _field("charge_id", "string"),
            _field("created", "timestamp"),
            _field("currency", "string"),
            _field("amount", "integer"),
            _field("status", "string"),
        ),
    }
    return fields[object_kind]


def _approved_objects() -> tuple[StripeObjectDeclaration, ...]:
    return (
        StripeObjectDeclaration(
            logical_object_ref="charges",
            object_kind="charge",
            source_field_names=(
                "amount",
                "amount_captured",
                "amount_refunded",
                "created",
                "currency",
                "customer",
                "id",
                "invoice",
                "livemode",
                "metadata",
                "paid",
                "refunded",
                "status",
            ),
            normalized_fields=_normalized_fields("charge"),
        ),
        StripeObjectDeclaration(
            logical_object_ref="customers",
            object_kind="customer",
            source_field_names=("created", "deleted", "id", "livemode", "metadata"),
            normalized_fields=_normalized_fields("customer"),
        ),
        StripeObjectDeclaration(
            logical_object_ref="invoices",
            object_kind="invoice",
            source_field_names=(
                "amount_due",
                "amount_paid",
                "amount_remaining",
                "created",
                "currency",
                "customer",
                "id",
                "livemode",
                "metadata",
                "paid",
                "status",
                "status_transitions.paid_at",
            ),
            normalized_fields=_normalized_fields("invoice"),
        ),
        StripeObjectDeclaration(
            logical_object_ref="refunds",
            object_kind="refund",
            source_field_names=(
                "amount",
                "charge",
                "created",
                "currency",
                "id",
                "livemode",
                "metadata",
                "status",
            ),
            normalized_fields=_normalized_fields("refund"),
        ),
    )


def _settings_values() -> dict[str, object]:
    return {
        "restricted_api_key": SecretStr(_TEST_KEY),
        "connection_handle": "opaque-stripe-source-capability",
        "account_mode": "test",
        "api_version": "2026-02-25.clover",
        "supported_creation_versions": ("2026-02-25.clover",),
        "objects": _approved_objects(),
        "event_types": _APPROVED_EVENT_TYPES,
        "customer_identity_metadata_key": "heinzel_customer_id",
    }


def test_settings_are_strict_immutable_and_pin_the_tested_api_version() -> None:
    settings = StripeSettings.model_validate(_settings_values())

    assert STRIPE_API_VERSION == "2026-02-25.clover"
    assert settings.api_version == STRIPE_API_VERSION
    assert settings.supported_creation_versions == (STRIPE_API_VERSION,)
    assert settings.restricted_api_key.get_secret_value() == _TEST_KEY
    with pytest.raises(ValidationError, match="frozen"):
        settings.account_mode = "live"
    with pytest.raises(ValidationError, match="extra"):
        StripeSettings.model_validate({**_settings_values(), "unexpected": "authority"})


@pytest.mark.parametrize(
    ("restricted_api_key", "account_mode"),
    ((_LIVE_KEY, "test"), (_TEST_KEY, "live")),
)
def test_settings_reject_restricted_key_and_account_mode_mismatch(
    restricted_api_key: str,
    account_mode: str,
) -> None:
    values = {
        **_settings_values(),
        "restricted_api_key": SecretStr(restricted_api_key),
        "account_mode": account_mode,
    }

    with pytest.raises(ValidationError, match=r"key.*account mode|account mode.*key"):
        StripeSettings.model_validate(values)


@pytest.mark.parametrize("suffix", ("private key", "private\nkey", "private\x00key"))
def test_settings_reject_malformed_restricted_key_material(suffix: str) -> None:
    values = {
        **_settings_values(),
        "restricted_api_key": SecretStr("rk_test_" + suffix),
    }

    with pytest.raises(ValidationError, match=r"key.*account mode|account mode.*key"):
        StripeSettings.model_validate(values)


def test_settings_require_an_explicit_pinned_request_api_version() -> None:
    missing_version = _settings_values()
    del missing_version["api_version"]

    with pytest.raises(ValidationError, match="api_version"):
        StripeSettings.model_validate(missing_version)
    with pytest.raises(ValidationError, match=r"supported|2026-02-25\.clover"):
        StripeSettings.model_validate({**_settings_values(), "api_version": "2025-12-15.preview"})


@pytest.mark.parametrize(
    "creation_versions",
    (
        (),
        ("2025-12-15.preview",),
        ("2026-02-25.clover", "2025-12-15.preview"),
        ("2026-02-25.clover", "2026-02-25.clover"),
    ),
)
def test_settings_reject_empty_unsupported_or_duplicate_creation_versions(
    creation_versions: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match=r"creation.*version"):
        StripeSettings.model_validate(
            {**_settings_values(), "supported_creation_versions": creation_versions}
        )


@pytest.mark.parametrize("account_mode", ("not_applicable", "sandbox", "production", ""))
def test_settings_reject_account_modes_outside_test_and_live(account_mode: str) -> None:
    with pytest.raises(ValidationError, match="account_mode"):
        StripeSettings.model_validate({**_settings_values(), "account_mode": account_mode})


@pytest.mark.parametrize(
    "metadata_key",
    ("*", "customer.*", "metadata", "heinzel_customer_id,other_key", "", "   "),
)
def test_settings_reject_unrestricted_or_malformed_metadata_configuration(
    metadata_key: str,
) -> None:
    with pytest.raises(ValidationError, match="metadata"):
        StripeSettings.model_validate(
            {**_settings_values(), "customer_identity_metadata_key": metadata_key}
        )


@pytest.mark.parametrize(
    "declaration_values",
    (
        {
            "logical_object_ref": "subscriptions",
            "object_kind": "subscription",
            "source_field_names": ("id", "status"),
            "normalized_fields": _normalized_fields("refund"),
        },
        {
            "logical_object_ref": "customers",
            "object_kind": "customer",
            "source_field_names": ("created", "email", "id", "livemode"),
            "normalized_fields": _normalized_fields("customer"),
        },
        {
            "logical_object_ref": "customers",
            "object_kind": "customer",
            "source_field_names": ("created", "id", "id", "livemode"),
            "normalized_fields": _normalized_fields("customer"),
        },
        {
            "logical_object_ref": "customer-private-alias",
            "object_kind": "customer",
            "source_field_names": ("created", "deleted", "id", "livemode", "metadata"),
            "normalized_fields": _normalized_fields("customer"),
        },
    ),
)
def test_object_declarations_reject_unknown_objects_fields_duplicates_and_aliases(
    declaration_values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        StripeObjectDeclaration.model_validate(declaration_values)


@pytest.mark.parametrize(
    "objects",
    (
        _approved_objects()[:-1],
        (*_approved_objects(), _approved_objects()[0]),
        tuple(reversed(_approved_objects())),
    ),
)
def test_settings_require_each_approved_object_once_in_canonical_order(
    objects: tuple[StripeObjectDeclaration, ...],
) -> None:
    with pytest.raises(ValidationError, match="object"):
        StripeSettings.model_validate({**_settings_values(), "objects": objects})


def test_object_declarations_are_immutable_and_reject_unknown_fields() -> None:
    declaration = _approved_objects()[0]

    with pytest.raises(ValidationError, match="frozen"):
        declaration.object_kind = "refund"
    with pytest.raises(ValidationError, match="extra"):
        StripeObjectDeclaration.model_validate(
            {
                **declaration.model_dump(),
                "expanded_fields": ("payment_intent",),
            }
        )


def test_object_declaration_rejects_a_changed_normalized_schema() -> None:
    declaration = _approved_objects()[0]
    changed_fields = (
        *declaration.normalized_fields[:-1],
        _field("future_unreviewed_field", "string"),
    )

    with pytest.raises(ValidationError, match="normalized"):
        StripeObjectDeclaration.model_validate(
            {**declaration.model_dump(), "normalized_fields": changed_fields},
            strict=True,
        )


@pytest.mark.parametrize(
    "event_types",
    (
        _APPROVED_EVENT_TYPES[:-1],
        (*_APPROVED_EVENT_TYPES, _APPROVED_EVENT_TYPES[-1]),
        tuple(reversed(_APPROVED_EVENT_TYPES)),
        (*_APPROVED_EVENT_TYPES[:-1], "payment_intent.succeeded"),
    ),
)
def test_settings_require_the_exact_canonical_event_allowlist(
    event_types: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="event"):
        StripeSettings.model_validate({**_settings_values(), "event_types": event_types})


def test_event_cursor_is_strict_immutable_canonical_and_round_trips() -> None:
    cursor = StripeEventCursor(
        last_event_created=datetime(2026, 9, 1, 12, tzinfo=UTC),
        last_event_id="evt_cursor_1",
        api_version_set_digest=digest((STRIPE_API_VERSION,)),
    )
    payload = cursor.to_payload()

    assert StripeEventCursor.from_payload(payload) == cursor
    with pytest.raises(ValidationError, match="frozen"):
        cursor.last_event_id = "evt_changed"
    with pytest.raises(ValueError, match="canonical"):
        StripeEventCursor.from_payload(payload + b"\n")


def test_event_cursor_rejects_naive_timestamps_duplicate_fields_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        StripeEventCursor(
            last_event_created=datetime(2026, 9, 1, 12),
            last_event_id="evt_cursor_1",
            api_version_set_digest=digest((STRIPE_API_VERSION,)),
        )

    with pytest.raises(ValueError, match="integrity"):
        StripeEventCursor.from_payload(
            b'{"api_version_set_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"last_event_created":"2026-09-01T12:00:00.000000Z",'
            b'"last_event_id":"evt_one","last_event_id":"evt_two","schema_version":"1"}'
        )
    cursor = StripeEventCursor(
        last_event_created=datetime(2026, 9, 1, 12, tzinfo=UTC),
        last_event_id="evt_cursor_1",
        api_version_set_digest=digest((STRIPE_API_VERSION,)),
    )
    payload_with_unknown = cursor.to_payload()[:-1] + b',"unknown":"authority"}'
    with pytest.raises(ValueError, match="integrity"):
        StripeEventCursor.from_payload(payload_with_unknown)
