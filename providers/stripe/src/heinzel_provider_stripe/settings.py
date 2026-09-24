from __future__ import annotations

import re
from typing import Literal, Self

from heinzel_provider_sdk import AcquisitionField
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

STRIPE_API_VERSION = "2026-02-25.clover"

type StripeAccountMode = Literal["test", "live"]
type StripeObjectKind = Literal["charge", "customer", "invoice", "refund"]
type StripeEventType = Literal[
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
]

_SOURCE_FIELDS: dict[StripeObjectKind, tuple[str, ...]] = {
    "charge": (
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
    "customer": ("created", "deleted", "id", "livemode", "metadata"),
    "invoice": (
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
    "refund": (
        "amount",
        "charge",
        "created",
        "currency",
        "id",
        "livemode",
        "metadata",
        "status",
    ),
}
_NORMALIZED_FIELDS: dict[StripeObjectKind, tuple[AcquisitionField, ...]] = {
    "charge": (
        AcquisitionField(name="charge_id", value_type="string", nullable=False),
        AcquisitionField(name="customer_id", value_type="string", nullable=True),
        AcquisitionField(name="invoice_id", value_type="string", nullable=True),
        AcquisitionField(name="created", value_type="timestamp", nullable=False),
        AcquisitionField(name="currency", value_type="string", nullable=False),
        AcquisitionField(name="amount", value_type="integer", nullable=False),
        AcquisitionField(name="amount_captured", value_type="integer", nullable=False),
        AcquisitionField(name="amount_refunded", value_type="integer", nullable=False),
        AcquisitionField(name="paid", value_type="boolean", nullable=False),
        AcquisitionField(name="refunded", value_type="boolean", nullable=False),
        AcquisitionField(name="status", value_type="string", nullable=False),
    ),
    "customer": (
        AcquisitionField(name="customer_id", value_type="string", nullable=False),
        AcquisitionField(name="created", value_type="timestamp", nullable=True),
        AcquisitionField(name="deleted", value_type="boolean", nullable=False),
        AcquisitionField(name="customer_identity", value_type="string", nullable=True),
    ),
    "invoice": (
        AcquisitionField(name="invoice_id", value_type="string", nullable=False),
        AcquisitionField(name="customer_id", value_type="string", nullable=True),
        AcquisitionField(name="created", value_type="timestamp", nullable=False),
        AcquisitionField(name="currency", value_type="string", nullable=False),
        AcquisitionField(name="amount_due", value_type="integer", nullable=False),
        AcquisitionField(name="amount_paid", value_type="integer", nullable=False),
        AcquisitionField(name="amount_remaining", value_type="integer", nullable=False),
        AcquisitionField(name="status", value_type="string", nullable=False),
        AcquisitionField(name="paid", value_type="boolean", nullable=False),
        AcquisitionField(name="paid_at", value_type="timestamp", nullable=True),
    ),
    "refund": (
        AcquisitionField(name="refund_id", value_type="string", nullable=False),
        AcquisitionField(name="charge_id", value_type="string", nullable=False),
        AcquisitionField(name="created", value_type="timestamp", nullable=False),
        AcquisitionField(name="currency", value_type="string", nullable=False),
        AcquisitionField(name="amount", value_type="integer", nullable=False),
        AcquisitionField(name="status", value_type="string", nullable=False),
    ),
}
_APPROVED_EVENT_TYPES: tuple[StripeEventType, ...] = (
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
_LOGICAL_OBJECT_REF: dict[StripeObjectKind, str] = {
    "charge": "charges",
    "customer": "customers",
    "invoice": "invoices",
    "refund": "refunds",
}
_METADATA_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class StripeObjectDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    logical_object_ref: str = Field(min_length=1)
    object_kind: StripeObjectKind
    source_field_names: tuple[str, ...] = Field(min_length=1)
    normalized_fields: tuple[AcquisitionField, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def requires_exact_allowlisted_shape(self) -> Self:
        if self.logical_object_ref != _LOGICAL_OBJECT_REF[self.object_kind]:
            raise ValueError("Stripe logical object reference must match its object kind")
        if self.source_field_names != _SOURCE_FIELDS[self.object_kind]:
            raise ValueError("Stripe source fields must match the approved allowlist")
        if self.normalized_fields != _NORMALIZED_FIELDS[self.object_kind]:
            raise ValueError("Stripe normalized fields must match the approved schema")
        return self


class StripeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    restricted_api_key: SecretStr
    connection_handle: str = Field(min_length=1)
    account_mode: StripeAccountMode
    api_version: Literal["2026-02-25.clover"]
    supported_creation_versions: tuple[Literal["2026-02-25.clover"], ...] = Field(min_length=1)
    objects: tuple[StripeObjectDeclaration, ...] = Field(min_length=1)
    event_types: tuple[StripeEventType, ...] = Field(min_length=1)
    customer_identity_metadata_key: str | None = None
    base_url: Literal["https://api.stripe.com"] = "https://api.stripe.com"
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("supported_creation_versions")
    @classmethod
    def requires_exact_creation_version_set(
        cls,
        value: tuple[Literal["2026-02-25.clover"], ...],
    ) -> tuple[Literal["2026-02-25.clover"], ...]:
        if value != (STRIPE_API_VERSION,):
            raise ValueError("Stripe creation versions must contain the one tested version")
        return value

    @field_validator("customer_identity_metadata_key")
    @classmethod
    def requires_one_explicit_metadata_key(cls, value: str | None) -> str | None:
        if value is not None and (
            _METADATA_KEY_PATTERN.fullmatch(value) is None or value == "metadata"
        ):
            raise ValueError("Stripe metadata configuration must name one explicit key")
        return value

    @model_validator(mode="after")
    def requires_exact_account_and_object_authority(self) -> Self:
        key = self.restricted_api_key.get_secret_value()
        expected_prefix = f"rk_{self.account_mode}_"
        if (
            not key.startswith(expected_prefix)
            or len(key) == len(expected_prefix)
            or any(character.isspace() or character == "\x00" for character in key)
        ):
            raise ValueError("Stripe restricted key must match the admitted account mode")
        expected_objects = tuple(sorted(_LOGICAL_OBJECT_REF.values()))
        actual_objects = tuple(item.logical_object_ref for item in self.objects)
        if actual_objects != expected_objects:
            raise ValueError("Stripe objects must contain each approved object in canonical order")
        if self.event_types != _APPROVED_EVENT_TYPES:
            raise ValueError("Stripe event types must match the approved canonical allowlist")
        return self
