from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import AcquisitionFieldValue, AcquisitionRecord
from pillarmesh_provider_sdk.acquisition_models import AcquisitionScalar

from .settings import (
    _APPROVED_EVENT_TYPES,
    _NORMALIZED_FIELDS,
    _SOURCE_FIELDS,
    STRIPE_API_VERSION,
    StripeObjectKind,
)

type StripeCodecReason = Literal[
    "event_type_not_allowed",
    "expansion_not_allowed",
    "field_not_allowlisted",
    "livemode_mismatch",
    "malformed_scalar",
    "metadata_not_allowed",
    "normalized_schema_mismatch",
    "sensitive_field_not_allowed",
    "unsupported_creation_version",
    "wrong_object_discriminator",
]
type _LogicalObjectRef = Literal["charges", "customers", "invoices", "refunds"]

_OBJECT_KIND: dict[_LogicalObjectRef, StripeObjectKind] = {
    "charges": "charge",
    "customers": "customer",
    "invoices": "invoice",
    "refunds": "refund",
}
_ALLOWED_FIELDS: dict[_LogicalObjectRef, frozenset[str]] = {
    logical_object_ref: frozenset(
        (
            *(field_name.split(".", maxsplit=1)[0] for field_name in _SOURCE_FIELDS[object_kind]),
            "object",
        )
    )
    for logical_object_ref, object_kind in _OBJECT_KIND.items()
}
_SENSITIVE_FIELDS: dict[_LogicalObjectRef, frozenset[str]] = {
    "charges": frozenset(
        (
            "billing_details",
            "description",
            "payment_method_details",
            "receipt_email",
            "receipt_url",
            "shipping",
        )
    ),
    "customers": frozenset(("address", "description", "email", "name", "phone", "shipping")),
    "invoices": frozenset(
        ("account_name", "customer_address", "customer_email", "customer_name", "description")
    ),
    "refunds": frozenset(("destination_details", "instructions_email", "receipt_number")),
}
_EVENT_FIELDS = frozenset(("api_version", "created", "data", "id", "livemode", "object", "type"))
_APPROVED_EVENT_TYPE_SET = frozenset(_APPROVED_EVENT_TYPES)
_LOGICAL_REF_BY_EVENT_PREFIX: dict[str, _LogicalObjectRef] = {
    "charge": "charges",
    "customer": "customers",
    "invoice": "invoices",
    "refund": "refunds",
}
_CURRENCY_PATTERN = re.compile(r"^[a-z]{3}$")
_INVOICE_STATUS_TRANSITION_FIELDS = frozenset(
    ("finalized_at", "marked_uncollectible_at", "paid_at", "voided_at")
)


class StripeCodecError(ValueError):
    def __init__(self, reason_code: StripeCodecReason) -> None:
        self.reason_code = reason_code
        super().__init__(f"Stripe payload failed normalization: {reason_code}")


def normalize_stripe_object(
    payload: Mapping[str, object],
    *,
    logical_object_ref: _LogicalObjectRef,
    expected_livemode: bool,
    creation_version: str,
    customer_identity_metadata_key: str | None,
) -> AcquisitionRecord:
    if creation_version != STRIPE_API_VERSION:
        raise StripeCodecError("unsupported_creation_version")
    if payload.get("object") != _OBJECT_KIND[logical_object_ref]:
        raise StripeCodecError("wrong_object_discriminator")
    _require_allowlisted_fields(payload, logical_object_ref=logical_object_ref)
    livemode = _require_bool(payload.get("livemode"))
    if livemode is not expected_livemode:
        raise StripeCodecError("livemode_mismatch")
    object_id = _require_string(payload.get("id"))
    created = _timestamp(payload.get("created"))
    builders: dict[
        _LogicalObjectRef, Callable[[Mapping[str, object], str, datetime], AcquisitionRecord]
    ] = {
        "charges": _charge_record,
        "customers": lambda source, key, timestamp: _customer_record(
            source,
            key,
            timestamp,
            customer_identity_metadata_key=customer_identity_metadata_key,
        ),
        "invoices": _invoice_record,
        "refunds": _refund_record,
    }
    return builders[logical_object_ref](payload, object_id, created)


def normalize_stripe_event(
    payload: Mapping[str, object],
    *,
    expected_livemode: bool,
    customer_identity_metadata_key: str | None,
) -> AcquisitionRecord:
    if set(payload) != _EVENT_FIELDS:
        raise StripeCodecError("field_not_allowlisted")
    if payload.get("object") != "event":
        raise StripeCodecError("wrong_object_discriminator")
    api_version = payload.get("api_version")
    if api_version != STRIPE_API_VERSION:
        raise StripeCodecError("unsupported_creation_version")
    _require_string(payload.get("id"))
    event_created = _timestamp(payload.get("created"))
    livemode = _require_bool(payload.get("livemode"))
    if livemode is not expected_livemode:
        raise StripeCodecError("livemode_mismatch")
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        raise StripeCodecError("malformed_scalar")
    if event_type not in _APPROVED_EVENT_TYPE_SET:
        raise StripeCodecError("event_type_not_allowed")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise StripeCodecError("malformed_scalar")
    if set(data) != {"object"}:
        raise StripeCodecError("field_not_allowlisted")
    embedded = data.get("object")
    if not isinstance(embedded, Mapping):
        raise StripeCodecError("malformed_scalar")
    prefix = event_type.split(".", maxsplit=1)[0]
    logical_object_ref = _LOGICAL_REF_BY_EVENT_PREFIX[prefix]
    if event_type == "customer.deleted":
        return _customer_tombstone(embedded, event_created=event_created)
    record = normalize_stripe_object(
        embedded,
        logical_object_ref=logical_object_ref,
        expected_livemode=expected_livemode,
        creation_version=api_version,
        customer_identity_metadata_key=customer_identity_metadata_key,
    )
    return record.model_copy(update={"source_updated_at": event_created})


def _require_allowlisted_fields(
    payload: Mapping[str, object],
    *,
    logical_object_ref: _LogicalObjectRef,
) -> None:
    fields = set(payload)
    if fields & _SENSITIVE_FIELDS[logical_object_ref]:
        raise StripeCodecError("sensitive_field_not_allowed")
    if not fields.issubset(_ALLOWED_FIELDS[logical_object_ref]):
        raise StripeCodecError("field_not_allowlisted")


def _customer_record(
    payload: Mapping[str, object],
    object_id: str,
    created: datetime,
    *,
    customer_identity_metadata_key: str | None,
) -> AcquisitionRecord:
    deleted = payload.get("deleted", False)
    deleted = _require_bool(deleted)
    metadata = _require_metadata(payload.get("metadata"))
    if customer_identity_metadata_key is None:
        if metadata:
            raise StripeCodecError("metadata_not_allowed")
        customer_identity: str | None = None
    else:
        if not set(metadata).issubset({customer_identity_metadata_key}):
            raise StripeCodecError("metadata_not_allowed")
        customer_identity_value = metadata.get(customer_identity_metadata_key)
        customer_identity = (
            None if customer_identity_value is None else _require_string(customer_identity_value)
        )
    return _record(
        logical_object_ref="customers",
        object_id=object_id,
        created=created,
        fields=(
            ("customer_id", object_id),
            ("created", created),
            ("deleted", deleted),
            ("customer_identity", customer_identity),
        ),
    )


def _invoice_record(
    payload: Mapping[str, object], object_id: str, created: datetime
) -> AcquisitionRecord:
    _require_empty_metadata(payload.get("metadata"))
    status_transitions = payload.get("status_transitions")
    if not isinstance(status_transitions, Mapping):
        raise StripeCodecError("malformed_scalar")
    transition_fields = set(status_transitions)
    if not transition_fields.issubset(_INVOICE_STATUS_TRANSITION_FIELDS):
        raise StripeCodecError("field_not_allowlisted")
    if transition_fields != _INVOICE_STATUS_TRANSITION_FIELDS:
        raise StripeCodecError("malformed_scalar")
    for transition_name in _INVOICE_STATUS_TRANSITION_FIELDS:
        transition_value = status_transitions.get(transition_name)
        if transition_value is not None:
            _timestamp(transition_value)
    paid_at_raw = status_transitions.get("paid_at")
    paid_at = None if paid_at_raw is None else _timestamp(paid_at_raw)
    return _record(
        logical_object_ref="invoices",
        object_id=object_id,
        created=created,
        fields=(
            ("invoice_id", object_id),
            ("customer_id", _require_optional_string(payload.get("customer"))),
            ("created", created),
            ("currency", _require_currency(payload.get("currency"))),
            ("amount_due", _require_integer(payload.get("amount_due"))),
            ("amount_paid", _require_integer(payload.get("amount_paid"))),
            ("amount_remaining", _require_integer(payload.get("amount_remaining"))),
            ("status", _require_string(payload.get("status"))),
            ("paid", _require_bool(payload.get("paid"))),
            ("paid_at", paid_at),
        ),
    )


def _charge_record(
    payload: Mapping[str, object], object_id: str, created: datetime
) -> AcquisitionRecord:
    _require_empty_metadata(payload.get("metadata"))
    invoice_id = _require_unexpanded_optional_relationship(payload.get("invoice"))
    return _record(
        logical_object_ref="charges",
        object_id=object_id,
        created=created,
        fields=(
            ("charge_id", object_id),
            ("customer_id", _require_optional_string(payload.get("customer"))),
            ("invoice_id", invoice_id),
            ("created", created),
            ("currency", _require_currency(payload.get("currency"))),
            ("amount", _require_integer(payload.get("amount"))),
            ("amount_captured", _require_integer(payload.get("amount_captured"))),
            ("amount_refunded", _require_integer(payload.get("amount_refunded"))),
            ("paid", _require_bool(payload.get("paid"))),
            ("refunded", _require_bool(payload.get("refunded"))),
            ("status", _require_string(payload.get("status"))),
        ),
    )


def _refund_record(
    payload: Mapping[str, object], object_id: str, created: datetime
) -> AcquisitionRecord:
    _require_empty_metadata(payload.get("metadata"))
    return _record(
        logical_object_ref="refunds",
        object_id=object_id,
        created=created,
        fields=(
            ("refund_id", object_id),
            ("charge_id", _require_string_relationship(payload.get("charge"))),
            ("created", created),
            ("currency", _require_currency(payload.get("currency"))),
            ("amount", _require_integer(payload.get("amount"))),
            ("status", _require_string(payload.get("status"))),
        ),
    )


def _customer_tombstone(
    payload: Mapping[str, object], *, event_created: datetime
) -> AcquisitionRecord:
    if set(payload) != {"deleted", "id", "object"}:
        raise StripeCodecError("field_not_allowlisted")
    if payload.get("object") != "customer":
        raise StripeCodecError("wrong_object_discriminator")
    object_id = _require_string(payload.get("id"))
    if _require_bool(payload.get("deleted")) is not True:
        raise StripeCodecError("malformed_scalar")
    record = AcquisitionRecord(
        logical_object_ref="customers",
        record_key=_record_key("customers", object_id),
        source_created_at=None,
        source_updated_at=event_created,
        fields=(
            AcquisitionFieldValue(name="customer_id", value=object_id),
            AcquisitionFieldValue(name="created", value=None),
            AcquisitionFieldValue(name="deleted", value=True),
            AcquisitionFieldValue(name="customer_identity", value=None),
        ),
    )
    _require_normalized_schema(record, logical_object_ref="customers")
    return record


def _record(
    *,
    logical_object_ref: _LogicalObjectRef,
    object_id: str,
    created: datetime,
    fields: tuple[tuple[str, AcquisitionScalar], ...],
) -> AcquisitionRecord:
    record = AcquisitionRecord(
        logical_object_ref=logical_object_ref,
        record_key=_record_key(logical_object_ref, object_id),
        source_created_at=created,
        source_updated_at=None,
        fields=tuple(AcquisitionFieldValue(name=name, value=value) for name, value in fields),
    )
    _require_normalized_schema(record, logical_object_ref=logical_object_ref)
    return record


def _record_key(logical_object_ref: _LogicalObjectRef, object_id: str) -> str:
    return digest({"logical_object_ref": logical_object_ref, "key": object_id})


def _require_normalized_schema(
    record: AcquisitionRecord,
    *,
    logical_object_ref: _LogicalObjectRef,
) -> None:
    approved_fields = _NORMALIZED_FIELDS[_OBJECT_KIND[logical_object_ref]]
    if tuple(field.name for field in record.fields) != tuple(
        field.name for field in approved_fields
    ):
        raise StripeCodecError("normalized_schema_mismatch")
    for value, approved in zip(record.fields, approved_fields, strict=True):
        if not _matches_field_type(value.value, approved.value_type, nullable=approved.nullable):
            raise StripeCodecError("normalized_schema_mismatch")


def _matches_field_type(value: AcquisitionScalar, value_type: str, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    expected_types: dict[str, type[object]] = {
        "boolean": bool,
        "decimal": Decimal,
        "integer": int,
        "string": str,
        "timestamp": datetime,
    }
    expected_type = expected_types.get(value_type)
    return expected_type is not None and type(value) is expected_type


def _timestamp(value: object) -> datetime:
    epoch = _require_integer(value)
    if epoch < 0:
        raise StripeCodecError("malformed_scalar")
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, OSError, ValueError):
        raise StripeCodecError("malformed_scalar") from None


def _require_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise StripeCodecError("malformed_scalar")
    return value


def _require_optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _require_string_relationship(value)


def _require_string_relationship(value: object) -> str:
    if isinstance(value, Mapping):
        raise StripeCodecError("expansion_not_allowed")
    return _require_string(value)


def _require_unexpanded_optional_relationship(value: object) -> str | None:
    if value is None:
        return None
    return _require_string_relationship(value)


def _require_integer(value: object) -> int:
    if type(value) is not int:
        raise StripeCodecError("malformed_scalar")
    return value


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        raise StripeCodecError("malformed_scalar")
    return value


def _require_currency(value: object) -> str:
    currency = _require_string(value)
    if _CURRENCY_PATTERN.fullmatch(currency) is None:
        raise StripeCodecError("malformed_scalar")
    return currency


def _require_metadata(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise StripeCodecError("malformed_scalar")
    return value


def _require_empty_metadata(value: object) -> None:
    metadata = _require_metadata(value)
    if metadata:
        raise StripeCodecError("metadata_not_allowed")
