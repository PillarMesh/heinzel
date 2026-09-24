from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Literal

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionCeilingExceeded,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSessionIncomplete,
    acquisition_intent_key,
)
from heinzel_provider_stripe import (
    StripeAcquisitionProvider,
    StripeObjectDeclaration,
    StripeObjectKind,
    StripeSettings,
)
from heinzel_provider_stripe.settings import (
    _APPROVED_EVENT_TYPES,
    _NORMALIZED_FIELDS,
    _SOURCE_FIELDS,
)
from pydantic import SecretStr

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_API_VERSION: Literal["2026-02-25.clover"] = "2026-02-25.clover"
_UPPER_EPOCH = int(_NOW.timestamp())
_OBJECT_REFS = ("charges", "customers", "invoices", "refunds")
_KIND_BY_REF: dict[str, StripeObjectKind] = {
    "charges": "charge",
    "customers": "customer",
    "invoices": "invoice",
    "refunds": "refund",
}
_REF_BY_KIND = {object_kind: object_ref for object_ref, object_kind in _KIND_BY_REF.items()}
type _Page = tuple[Mapping[str, object], ...]
type _PageStep = _Page | BaseException


def _declarations() -> tuple[StripeObjectDeclaration, ...]:
    return tuple(
        StripeObjectDeclaration(
            logical_object_ref=logical_object_ref,
            object_kind=object_kind,
            source_field_names=_SOURCE_FIELDS[object_kind],
            normalized_fields=_NORMALIZED_FIELDS[object_kind],
        )
        for logical_object_ref, object_kind in _KIND_BY_REF.items()
    )


def _settings() -> StripeSettings:
    return StripeSettings(
        restricted_api_key=SecretStr("rk_test_private-snapshot-canary"),
        connection_handle="opaque-stripe-source-capability",
        account_mode="test",
        api_version=_API_VERSION,
        supported_creation_versions=(_API_VERSION,),
        objects=_declarations(),
        event_types=_APPROVED_EVENT_TYPES,
        customer_identity_metadata_key="heinzel_customer_id",
    )


def _schemas() -> tuple[AcquisitionObjectSchema, ...]:
    return tuple(
        AcquisitionObjectSchema(
            logical_object_ref=declaration.logical_object_ref,
            schema_digest=digest(declaration.normalized_fields),
            fields=declaration.normalized_fields,
            record_key_fields=(declaration.normalized_fields[0].name,),
            source_updated_at_field=None,
        )
        for declaration in _declarations()
    )


def _intent(*, record_ceiling: int = 100) -> AcquisitionIntent:
    run_intent_ref = "1" * 64
    contract_digest = "2" * 64
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref=run_intent_ref,
            contract_digest=contract_digest,
            source_binding_ref="stripe-binding-a",
            acquisition_mode="snapshot",
            object_refs=_OBJECT_REFS,
            prior_checkpoint_revision=0,
        ),
        tenant_id="tenant-a",
        run_intent_ref=run_intent_ref,
        contract_ref="contract-a-v1",
        contract_digest=contract_digest,
        source_binding_ref="stripe-binding-a",
        source_observation_digest="3" * 64,
        acquisition_mode="snapshot",
        object_refs=_OBJECT_REFS,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=record_ceiling,
        encoded_byte_ceiling=100_000,
        admitted_at=_NOW,
    )


def _payload(
    object_kind: StripeObjectKind,
    object_id: str,
    *,
    created: int,
) -> dict[str, object]:
    common: dict[str, object] = {
        "id": object_id,
        "object": object_kind,
        "created": created,
        "livemode": False,
        "metadata": {},
    }
    if object_kind == "customer":
        common["metadata"] = {"heinzel_customer_id": f"crm-{object_id}"}
    elif object_kind == "invoice":
        common.update(
            {
                "customer": "cus_owner",
                "currency": "usd",
                "amount_due": 2_500,
                "amount_paid": 2_500,
                "amount_remaining": 0,
                "status": "paid",
                "paid": True,
                "status_transitions": {
                    "finalized_at": created,
                    "marked_uncollectible_at": None,
                    "paid_at": created,
                    "voided_at": None,
                },
            }
        )
    elif object_kind == "charge":
        common.update(
            {
                "customer": "cus_owner",
                "invoice": "in_owner",
                "currency": "usd",
                "amount": 2_500,
                "amount_captured": 2_500,
                "amount_refunded": 0,
                "paid": True,
                "refunded": False,
                "status": "succeeded",
            }
        )
    else:
        common.update(
            {
                "charge": "ch_owner",
                "currency": "usd",
                "amount": 500,
                "status": "succeeded",
            }
        )
    return common


class _TrackedPageIterator:
    def __init__(self, steps: tuple[_PageStep, ...]) -> None:
        self._steps = steps
        self._index = 0
        self.closed = False
        self.close_count = 0
        self.next_count = 0

    def __iter__(self) -> _TrackedPageIterator:
        return self

    def __next__(self) -> _Page:
        if self._index == len(self._steps):
            self.closed = True
            raise StopIteration
        step = self._steps[self._index]
        self._index += 1
        self.next_count += 1
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


class _FakeStripeClient:
    def __init__(self, pages: Mapping[StripeObjectKind, tuple[_PageStep, ...]]) -> None:
        self._pages = pages
        self.object_calls: list[tuple[StripeObjectKind, int]] = []
        self.iterators: list[_TrackedPageIterator] = []
        self.event_calls = 0

    def iter_object_pages(
        self,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Iterator[_Page]:
        self.object_calls.append((object_kind, created_lte))
        iterator = _TrackedPageIterator(self._pages.get(object_kind, ((),)))
        self.iterators.append(iterator)
        return iterator

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[_Page]:
        self.event_calls += 1
        raise AssertionError("snapshot_must_not_resolve_event_pages")


def _provider(
    client: _FakeStripeClient,
) -> tuple[StripeAcquisitionProvider, dict[str, bytes]]:
    private_boundaries: dict[str, bytes] = {}
    provider = StripeAcquisitionProvider(
        _settings(),
        client=client,
        clock=lambda: _NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda tenant_id, reference, payload: (
            private_boundaries.__setitem__(f"{tenant_id}:{reference}", payload)
        ),
    )
    return provider, private_boundaries


def _reverse_pages() -> dict[StripeObjectKind, tuple[_PageStep, ...]]:
    pages: dict[StripeObjectKind, tuple[_PageStep, ...]] = {}
    for object_kind in _KIND_BY_REF.values():
        prefix = {
            "charge": "ch",
            "customer": "cus",
            "invoice": "in",
            "refund": "re",
        }[object_kind]
        pages[object_kind] = (
            (_payload(object_kind, f"{prefix}_z", created=_UPPER_EPOCH - 1),),
            (
                _payload(object_kind, f"{prefix}_b", created=_UPPER_EPOCH - 2),
                _payload(object_kind, f"{prefix}_a", created=_UPPER_EPOCH - 2),
            ),
        )
    return pages


def _record_source_id(record: AcquisitionRecord) -> object:
    return record.fields[0].value


def test_snapshot_reads_all_resources_at_one_upper_time_and_canonicalizes_reverse_pages() -> None:
    client = _FakeStripeClient(_reverse_pages())
    provider, private_boundaries = _provider(client)

    session = provider.open_acquisition(_intent(), _schemas(), None)
    records = tuple(session)
    completion = session.complete()

    assert client.object_calls == [
        ("charge", _UPPER_EPOCH),
        ("customer", _UPPER_EPOCH),
        ("invoice", _UPPER_EPOCH),
        ("refund", _UPPER_EPOCH),
    ]
    assert client.event_calls == 0
    grouped_ids = {
        object_ref: tuple(
            _record_source_id(record)
            for record in records
            if record.logical_object_ref == object_ref
        )
        for object_ref in _OBJECT_REFS
    }
    assert grouped_ids == {
        "charges": ("ch_a", "ch_b", "ch_z"),
        "customers": ("cus_a", "cus_b", "cus_z"),
        "invoices": ("in_a", "in_b", "in_z"),
        "refunds": ("re_a", "re_b", "re_z"),
    }
    assert tuple(record.logical_object_ref for record in records) == tuple(
        object_ref for object_ref in _OBJECT_REFS for _index in range(3)
    )
    assert tuple(boundary.logical_object_ref for boundary in completion.boundaries) == _OBJECT_REFS
    assert all(boundary.acquisition_mode == "snapshot" for boundary in completion.boundaries)
    assert all(boundary.record_count == 3 for boundary in completion.boundaries)
    assert all(boundary.upper_cursor_digest for boundary in completion.boundaries)
    assert all(boundary.private_boundary_ref for boundary in completion.boundaries)
    assert len(private_boundaries) == 4


def test_snapshot_deduplicates_only_byte_identical_objects_across_pages() -> None:
    duplicate = _payload("customer", "cus_duplicate", created=_UPPER_EPOCH - 1)
    client = _FakeStripeClient(
        {
            "charge": ((),),
            "customer": ((duplicate,), (dict(duplicate),)),
            "invoice": ((),),
            "refund": ((),),
        }
    )
    provider, _private_boundaries = _provider(client)

    session = provider.open_acquisition(_intent(), _schemas(), None)
    records = tuple(session)

    assert tuple(_record_source_id(record) for record in records) == ("cus_duplicate",)
    assert session.complete().boundaries[1].record_count == 1


def test_snapshot_duplicate_at_exact_record_ceiling_does_not_overcount() -> None:
    duplicate = _payload("charge", "ch_exact_ceiling", created=_UPPER_EPOCH - 1)
    client = _FakeStripeClient(
        {
            "charge": ((duplicate,), (dict(duplicate),)),
            "customer": ((),),
            "invoice": ((),),
            "refund": ((),),
        }
    )
    provider, _private_boundaries = _provider(client)

    session = provider.open_acquisition(_intent(record_ceiling=1), _schemas(), None)

    assert tuple(_record_source_id(record) for record in session) == ("ch_exact_ceiling",)
    assert session.complete().boundaries[0].record_count == 1


def test_snapshot_rejects_a_page_larger_than_stripes_limit_as_invalid_response() -> None:
    oversized_page = tuple(
        _payload("charge", f"ch_oversized_{index}", created=_UPPER_EPOCH - 1)
        for index in range(101)
    )
    client = _FakeStripeClient({"charge": (oversized_page,)})
    provider, _private_boundaries = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(record_ceiling=1_000), _schemas(), None)

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"
    assert client.iterators[0].closed is True


def test_snapshot_rejects_contradictory_content_under_one_provider_object_id() -> None:
    first = _payload("charge", "ch_duplicate", created=_UPPER_EPOCH - 1)
    contradictory = dict(first)
    contradictory["amount"] = 9_999
    client = _FakeStripeClient(
        {
            "charge": ((first,), (contradictory,)),
            "customer": ((),),
            "invoice": ((),),
            "refund": ((),),
        }
    )
    provider, _private_boundaries = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(), _schemas(), None)

    assert captured.value.classification == "integrity_failure"
    assert captured.value.reason_code == "integrity_failure"
    assert client.iterators[0].closed is True


def test_snapshot_returns_four_zero_count_boundaries_for_empty_lists() -> None:
    client = _FakeStripeClient({object_kind: ((),) for object_kind in _KIND_BY_REF.values()})
    provider, private_boundaries = _provider(client)

    session = provider.open_acquisition(_intent(), _schemas(), None)

    assert tuple(session) == ()
    completion = session.complete()
    assert tuple(boundary.logical_object_ref for boundary in completion.boundaries) == _OBJECT_REFS
    assert tuple(boundary.record_count for boundary in completion.boundaries) == (0, 0, 0, 0)
    assert len(private_boundaries) == 4


def _invalid_page_error() -> AcquisitionProviderError:
    return AcquisitionProviderError(
        provider_kind="stripe",
        classification="invalid_provider_response",
        reason_code="invalid_provider_response",
    )


def test_snapshot_propagates_cursor_loop_error_and_closes_the_page_iterator() -> None:
    client = _FakeStripeClient(
        {
            "charge": (
                (_payload("charge", "ch_first", created=_UPPER_EPOCH - 1),),
                _invalid_page_error(),
            )
        }
    )
    provider, _private_boundaries = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(), _schemas(), None)

    assert captured.value.reason_code == "invalid_provider_response"
    assert client.iterators[0].next_count == 2
    assert client.iterators[0].closed is True
    assert client.iterators[0].close_count == 1
    assert client.object_calls == [("charge", _UPPER_EPOCH)]


@pytest.mark.parametrize(
    ("pages", "record_ceiling", "expected_pages_read"),
    (
        (
            (
                tuple(
                    _payload("charge", f"ch_page_{index}", created=_UPPER_EPOCH - 1)
                    for index in range(3)
                ),
                (_payload("charge", "ch_never", created=_UPPER_EPOCH - 2),),
            ),
            2,
            1,
        ),
        (
            (
                tuple(
                    _payload("charge", f"ch_first_{index}", created=_UPPER_EPOCH - 1)
                    for index in range(2)
                ),
                tuple(
                    _payload("charge", f"ch_second_{index}", created=_UPPER_EPOCH - 2)
                    for index in range(2)
                ),
                (_payload("charge", "ch_never", created=_UPPER_EPOCH - 3),),
            ),
            3,
            2,
        ),
    ),
)
def test_snapshot_refuses_page_and_cumulative_record_ceilings_before_further_reads(
    pages: tuple[_PageStep, ...],
    record_ceiling: int,
    expected_pages_read: int,
) -> None:
    client = _FakeStripeClient({"charge": pages})
    provider, private_boundaries = _provider(client)

    with pytest.raises(AcquisitionCeilingExceeded) as captured:
        provider.open_acquisition(
            _intent(record_ceiling=record_ceiling),
            _schemas(),
            None,
        )

    assert captured.value.logical_object_ref == "charges"
    assert captured.value.limit_kind == "records"
    assert captured.value.ceiling == record_ceiling
    assert client.iterators[0].next_count == expected_pages_read
    assert client.iterators[0].closed is True
    assert client.object_calls == [("charge", _UPPER_EPOCH)]
    assert private_boundaries == {}


def test_aborted_snapshot_session_is_idempotent_and_cannot_complete() -> None:
    client = _FakeStripeClient(_reverse_pages())
    provider, _private_boundaries = _provider(client)
    session = provider.open_acquisition(_intent(), _schemas(), None)

    next(iter(session))
    session.abort()
    session.abort()

    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()
    assert all(iterator.closed for iterator in client.iterators)


@pytest.mark.parametrize("authority_failure", ("missing", "changed_schema"))
def test_snapshot_requires_exact_object_schema_authority_before_reading(
    authority_failure: Literal["missing", "changed_schema"],
) -> None:
    schemas = _schemas()
    if authority_failure == "missing":
        invalid_schemas = schemas[:-1]
    else:
        original = schemas[1]
        changed_fields = original.fields[:-1]
        invalid_schemas = (
            schemas[0],
            AcquisitionObjectSchema(
                logical_object_ref=original.logical_object_ref,
                schema_digest=digest(changed_fields),
                fields=changed_fields,
                record_key_fields=original.record_key_fields,
                source_updated_at_field=None,
            ),
            *schemas[2:],
        )
    client = _FakeStripeClient(_reverse_pages())
    provider, _private_boundaries = _provider(client)

    with pytest.raises(AcquisitionProviderError) as captured:
        provider.open_acquisition(_intent(), invalid_schemas, None)

    assert captured.value.classification == "permanent_configuration"
    assert client.object_calls == []
