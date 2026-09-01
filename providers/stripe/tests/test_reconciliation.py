from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProvider,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSessionIncomplete,
    acquisition_intent_key,
)
from pillarmesh_provider_stripe import (
    STRIPE_API_VERSION,
    StripeEventCursor,
    StripeObjectDeclaration,
    StripeSettings,
)
from pillarmesh_provider_stripe.acquisition import StripeAcquisitionProvider
from pillarmesh_provider_stripe.settings import StripeObjectKind
from pydantic import SecretStr

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_UPPER_EPOCH = int(_NOW.timestamp())
_OBJECT_REFS = ("charges", "customers", "invoices", "refunds")
_OBJECT_KIND_BY_REF: dict[str, StripeObjectKind] = {
    "charges": "charge",
    "customers": "customer",
    "invoices": "invoice",
    "refunds": "refund",
}
_RECORD_KEY_FIELD = {
    "charges": "charge_id",
    "customers": "customer_id",
    "invoices": "invoice_id",
    "refunds": "refund_id",
}
_NORMALIZED_FIELDS: dict[str, tuple[AcquisitionField, ...]] = {
    "charges": (
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
    "customers": (
        AcquisitionField(name="customer_id", value_type="string", nullable=False),
        AcquisitionField(name="created", value_type="timestamp", nullable=True),
        AcquisitionField(name="deleted", value_type="boolean", nullable=False),
        AcquisitionField(name="customer_identity", value_type="string", nullable=True),
    ),
    "invoices": (
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
    "refunds": (
        AcquisitionField(name="refund_id", value_type="string", nullable=False),
        AcquisitionField(name="charge_id", value_type="string", nullable=False),
        AcquisitionField(name="created", value_type="timestamp", nullable=False),
        AcquisitionField(name="currency", value_type="string", nullable=False),
        AcquisitionField(name="amount", value_type="integer", nullable=False),
        AcquisitionField(name="status", value_type="string", nullable=False),
    ),
}


class FakeStripeClient:
    def __init__(
        self,
        pages: Mapping[StripeObjectKind, tuple[tuple[Mapping[str, object], ...], ...]],
        *,
        cleanup_failure: bool = False,
    ) -> None:
        self.pages = pages
        self.cleanup_failure = cleanup_failure
        self.object_requests: list[tuple[StripeObjectKind, int]] = []
        self.event_request_count = 0
        self.closed_object_streams: set[StripeObjectKind] = set()

    def iter_object_pages(
        self,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Iterator[tuple[Mapping[str, object], ...]]:
        self.object_requests.append((object_kind, created_lte))
        completed = False
        try:
            yield from self.pages[object_kind]
            completed = True
        finally:
            self.closed_object_streams.add(object_kind)
            if self.cleanup_failure and not completed:
                raise RuntimeError("private-stripe-cleanup-canary")

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[tuple[Mapping[str, object], ...]]:
        self.event_request_count += 1
        raise AssertionError(
            "reconciliation must use complete object snapshots, not the incremental Events API"
        )


def _settings() -> StripeSettings:
    return StripeSettings(
        restricted_api_key=SecretStr("rk_test_private-reconciliation-key"),
        connection_handle="opaque-stripe-source-capability",
        account_mode="test",
        api_version="2026-02-25.clover",
        supported_creation_versions=("2026-02-25.clover",),
        objects=(
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
                normalized_fields=_NORMALIZED_FIELDS["charges"],
            ),
            StripeObjectDeclaration(
                logical_object_ref="customers",
                object_kind="customer",
                source_field_names=("created", "deleted", "id", "livemode", "metadata"),
                normalized_fields=_NORMALIZED_FIELDS["customers"],
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
                normalized_fields=_NORMALIZED_FIELDS["invoices"],
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
                normalized_fields=_NORMALIZED_FIELDS["refunds"],
            ),
        ),
        event_types=(
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
        ),
        customer_identity_metadata_key="pillarmesh_customer_id",
    )


def _schemas() -> tuple[AcquisitionObjectSchema, ...]:
    return tuple(
        AcquisitionObjectSchema(
            logical_object_ref=object_ref,
            schema_digest=digest(_NORMALIZED_FIELDS[object_ref]),
            fields=_NORMALIZED_FIELDS[object_ref],
            record_key_fields=(_RECORD_KEY_FIELD[object_ref],),
            source_updated_at_field=None,
        )
        for object_ref in _OBJECT_REFS
    )


def _cursor(*, api_version_set_digest: str | None = None) -> bytes:
    return StripeEventCursor(
        last_event_created=datetime(2026, 8, 31, 23, 55, tzinfo=UTC),
        last_event_id="evt_prior_reconciliation",
        api_version_set_digest=api_version_set_digest or digest((STRIPE_API_VERSION,)),
    ).to_payload()


def _intent(private_cursor: bytes) -> AcquisitionIntent:
    run_intent_ref = "1" * 64
    contract_digest = "2" * 64
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref="source-binding-a",
            acquisition_mode="reconciliation",
            object_refs=_OBJECT_REFS,
            prior_checkpoint_revision=7,
        ),
        tenant_id="tenant-a",
        run_intent_ref=run_intent_ref,
        contract_ref="contract-a",
        contract_digest=contract_digest,
        source_binding_ref="source-binding-a",
        source_observation_digest="3" * 64,
        acquisition_mode="reconciliation",
        object_refs=_OBJECT_REFS,
        prior_checkpoint_revision=7,
        prior_checkpoint_digest=hashlib.sha256(private_cursor).hexdigest(),
        record_ceiling=100,
        encoded_byte_ceiling=1_000_000,
        admitted_at=_NOW,
    )


def _provider(
    client: FakeStripeClient,
    *,
    private_boundaries: dict[str, bytes] | None = None,
) -> AcquisitionProvider:
    boundary_payloads = private_boundaries if private_boundaries is not None else {}
    return StripeAcquisitionProvider(
        _settings(),
        client=client,
        clock=lambda: _NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda tenant_id, reference, payload: boundary_payloads.__setitem__(
            f"{tenant_id}:{reference}", payload
        ),
    )


def _empty_pages() -> dict[StripeObjectKind, tuple[tuple[Mapping[str, object], ...], ...]]:
    return {object_kind: ((),) for object_kind in _OBJECT_KIND_BY_REF.values()}


def _charge(object_id: str, created: int, *, amount: int = 1_000) -> dict[str, object]:
    return {
        "id": object_id,
        "object": "charge",
        "created": created,
        "livemode": False,
        "customer": "cus_fixture",
        "invoice": "in_fixture",
        "currency": "usd",
        "amount": amount,
        "amount_captured": amount,
        "amount_refunded": 0,
        "paid": True,
        "refunded": False,
        "status": "succeeded",
        "metadata": {},
    }


def _customer(object_id: str, created: int) -> dict[str, object]:
    return {
        "id": object_id,
        "object": "customer",
        "created": created,
        "livemode": False,
        "deleted": False,
        "metadata": {},
    }


def _refund(object_id: str, created: int, *, amount: int = 250) -> dict[str, object]:
    return {
        "id": object_id,
        "object": "refund",
        "created": created,
        "livemode": False,
        "charge": "ch_fixture",
        "currency": "usd",
        "amount": amount,
        "status": "succeeded",
        "metadata": {},
    }


def _record_identifier(record: AcquisitionRecord) -> object:
    identifier_name = _RECORD_KEY_FIELD[record.logical_object_ref]
    return next(field.value for field in record.fields if field.name == identifier_name)


def _open_and_consume(
    provider: AcquisitionProvider,
    private_cursor: bytes,
) -> tuple[AcquisitionRecord, ...]:
    session = provider.open_acquisition(_intent(private_cursor), _schemas(), private_cursor)
    return tuple(session)


def test_reconciliation_reads_all_pages_for_all_objects_and_preserves_the_prior_cursor() -> None:
    pages = _empty_pages()
    pages["charge"] = (
        (
            _charge("ch_same_time_b", _UPPER_EPOCH - 10),
            _charge("ch_new", _UPPER_EPOCH - 1),
        ),
        (
            _charge("ch_old", _UPPER_EPOCH - 100),
            _charge("ch_same_time_a", _UPPER_EPOCH - 10),
        ),
    )
    pages["customer"] = (
        (_customer("cus_b", _UPPER_EPOCH - 20),),
        (_customer("cus_a", _UPPER_EPOCH - 20),),
    )
    pages["refund"] = (
        (_refund("re_new", _UPPER_EPOCH - 2),),
        (_refund("re_old", _UPPER_EPOCH - 200),),
    )
    client = FakeStripeClient(pages)
    private_boundaries: dict[str, bytes] = {}
    prior_cursor = _cursor()
    provider = _provider(client, private_boundaries=private_boundaries)

    session = provider.open_acquisition(_intent(prior_cursor), _schemas(), prior_cursor)
    records = tuple(session)
    completion = session.complete()

    assert tuple((record.logical_object_ref, _record_identifier(record)) for record in records) == (
        ("charges", "ch_old"),
        ("charges", "ch_same_time_a"),
        ("charges", "ch_same_time_b"),
        ("charges", "ch_new"),
        ("customers", "cus_a"),
        ("customers", "cus_b"),
        ("refunds", "re_old"),
        ("refunds", "re_new"),
    )
    assert client.object_requests == [
        ("charge", _UPPER_EPOCH),
        ("customer", _UPPER_EPOCH),
        ("invoice", _UPPER_EPOCH),
        ("refund", _UPPER_EPOCH),
    ]
    assert client.event_request_count == 0
    assert client.closed_object_streams == {"charge", "customer", "invoice", "refund"}
    assert completion.candidate_cursor_payload == prior_cursor
    assert completion.candidate_cursor_digest == hashlib.sha256(prior_cursor).hexdigest()
    assert completion.cursor_version == "stripe-event-v1"
    assert tuple(boundary.logical_object_ref for boundary in completion.boundaries) == _OBJECT_REFS
    assert tuple(boundary.record_count for boundary in completion.boundaries) == (4, 2, 0, 2)
    expected_cursor_digest = hashlib.sha256(prior_cursor).hexdigest()
    for boundary, schema in zip(completion.boundaries, _schemas(), strict=True):
        assert boundary.acquisition_mode == "reconciliation"
        assert boundary.schema_digest == schema.schema_digest
        assert boundary.lower_cursor_digest == expected_cursor_digest
        assert boundary.upper_cursor_digest == expected_cursor_digest
        assert boundary.snapshot_identity_digest is None
        assert boundary.key_range_digest is None
        assert boundary.query_shape_digest != expected_cursor_digest
        assert boundary.private_boundary_ref == (f"private:tenant-a:{boundary.logical_object_ref}")
    assert set(private_boundaries) == {
        f"tenant-a:private:tenant-a:{object_ref}" for object_ref in _OBJECT_REFS
    }
    for boundary, expected_count in zip(completion.boundaries, (4, 2, 0, 2), strict=True):
        private_payload = json.loads(
            private_boundaries[f"tenant-a:{boundary.private_boundary_ref}"]
        )
        assert set(private_payload) == {
            "logical_object_ref",
            "record_count",
            "record_set_digest",
            "upper_created",
        }
        assert private_payload["logical_object_ref"] == boundary.logical_object_ref
        assert private_payload["record_count"] == expected_count
        assert private_payload["upper_created"] == _UPPER_EPOCH
        assert isinstance(private_payload["record_set_digest"], str)
        assert len(private_payload["record_set_digest"]) == 64

    public_bytes = b"".join(canonical_bytes(record) for record in records)
    public_bytes += canonical_bytes(completion.model_dump(exclude={"candidate_cursor_payload"}))
    public_bytes += completion.candidate_cursor_payload
    public_bytes += b"".join(private_boundaries.values())
    for forbidden_claim in (b"destination", b"warehouse", b"clickhouse", b"snowflake"):
        assert forbidden_claim not in public_bytes.lower()


def test_reconciliation_deduplicates_only_byte_identical_objects() -> None:
    repeated = _charge("ch_repeated", _UPPER_EPOCH - 10)
    pages = _empty_pages()
    pages["charge"] = ((repeated,), (dict(repeated),))
    client = FakeStripeClient(pages)
    prior_cursor = _cursor()
    provider = _provider(client)

    session = provider.open_acquisition(_intent(prior_cursor), _schemas(), prior_cursor)
    records = tuple(session)
    completion = session.complete()

    assert tuple(_record_identifier(record) for record in records) == ("ch_repeated",)
    assert tuple(boundary.record_count for boundary in completion.boundaries) == (1, 0, 0, 0)
    assert completion.candidate_cursor_payload == prior_cursor


def test_reconciliation_rejects_contradictory_content_for_one_object_identity() -> None:
    pages = _empty_pages()
    pages["charge"] = (
        (_charge("ch_contradiction", _UPPER_EPOCH - 10, amount=1_000),),
        (_charge("ch_contradiction", _UPPER_EPOCH - 10, amount=2_000),),
    )
    client = FakeStripeClient(pages, cleanup_failure=True)
    prior_cursor = _cursor()
    provider = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        _open_and_consume(provider, prior_cursor)

    assert captured.value.provider_kind == "stripe"
    assert captured.value.classification == "integrity_failure"
    assert captured.value.reason_code == "integrity_failure"
    assert "private-stripe-cleanup-canary" not in str(captured.value)
    assert "charge" in client.closed_object_streams


def test_reconciliation_requires_the_exact_admitted_normalized_schema_before_reading() -> None:
    schemas = _schemas()
    changed_fields = (*schemas[0].fields[:-1],)
    changed_charge_schema = schemas[0].model_copy(
        update={"fields": changed_fields, "schema_digest": digest(changed_fields)}
    )
    client = FakeStripeClient(_empty_pages())
    prior_cursor = _cursor()
    provider = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(
            _intent(prior_cursor),
            (changed_charge_schema, *schemas[1:]),
            prior_cursor,
        )

    assert captured.value.classification == "permanent_configuration"
    assert captured.value.reason_code == "permanent_configuration"
    assert client.object_requests == []


@pytest.mark.parametrize("missing_object_ref", _OBJECT_REFS)
def test_reconciliation_requires_exactly_all_four_approved_objects(
    missing_object_ref: str,
) -> None:
    private_cursor = _cursor()
    object_refs = tuple(
        object_ref for object_ref in _OBJECT_REFS if object_ref != missing_object_ref
    )
    base_intent = _intent(private_cursor)
    subset_intent = base_intent.model_copy(
        update={
            "object_refs": object_refs,
            "intent_key": acquisition_intent_key(
                tenant_id=base_intent.tenant_id,
                run_intent_ref=base_intent.run_intent_ref,
                contract_digest=base_intent.contract_digest,
                source_binding_ref=base_intent.source_binding_ref,
                acquisition_mode="reconciliation",
                object_refs=object_refs,
                prior_checkpoint_revision=base_intent.prior_checkpoint_revision,
            ),
        }
    )
    schemas = tuple(schema for schema in _schemas() if schema.logical_object_ref in object_refs)
    client = FakeStripeClient(_empty_pages())
    provider = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(subset_intent, schemas, private_cursor)

    assert captured.value.classification == "permanent_configuration"
    assert captured.value.reason_code == "permanent_configuration"
    assert client.object_requests == []


def test_reconciliation_rejects_cursor_from_an_unapproved_api_version_set_before_reading() -> None:
    private_cursor = _cursor(api_version_set_digest="9" * 64)
    client = FakeStripeClient(_empty_pages())
    provider = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(private_cursor), _schemas(), private_cursor)

    assert captured.value.classification == "integrity_failure"
    assert captured.value.reason_code == "integrity_failure"
    assert client.object_requests == []


def test_reconciliation_rejects_cross_account_objects_and_sanitizes_the_failure() -> None:
    wrong_account = _customer("cus_live", _UPPER_EPOCH - 10)
    wrong_account["livemode"] = True
    pages = _empty_pages()
    pages["customer"] = ((wrong_account,),)
    client = FakeStripeClient(pages)
    prior_cursor = _cursor()
    provider = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        _open_and_consume(provider, prior_cursor)

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"
    assert "cus_live" not in str(captured.value)
    assert "customer" in client.closed_object_streams


def test_reconciliation_sanitizes_private_boundary_writer_failure_as_integrity_error() -> None:
    diagnostic = (
        "private-boundary-writer-canary acct_private-account-canary req_private-request-id-canary"
    )

    def fail_private_boundary_write(
        _tenant_id: str,
        _reference: str,
        _payload: bytes,
    ) -> None:
        raise RuntimeError(diagnostic)

    client = FakeStripeClient(_empty_pages())
    private_cursor = _cursor()
    provider = StripeAcquisitionProvider(
        _settings(),
        client=client,
        clock=lambda: _NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=fail_private_boundary_write,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(private_cursor), _schemas(), private_cursor)

    assert captured.value.provider_kind == "stripe"
    assert captured.value.classification == "integrity_failure"
    assert captured.value.reason_code == "integrity_failure"
    public_surface = " ".join(
        (str(captured.value), repr(captured.value), repr(captured.value.args))
    )
    assert diagnostic not in public_surface
    assert "private-boundary-writer-canary" not in public_surface
    assert "acct_private-account-canary" not in public_surface
    assert "req_private-request-id-canary" not in public_surface


def test_reconciliation_abort_is_idempotent_and_prevents_completion() -> None:
    pages = _empty_pages()
    pages["charge"] = (
        (
            _charge("ch_first", _UPPER_EPOCH - 20),
            _charge("ch_second", _UPPER_EPOCH - 10),
        ),
    )
    client = FakeStripeClient(pages)
    prior_cursor = _cursor()
    provider = _provider(client)
    session = provider.open_acquisition(_intent(prior_cursor), _schemas(), prior_cursor)

    next(iter(session))
    session.abort()
    session.abort()

    assert "charge" in client.closed_object_streams
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()
