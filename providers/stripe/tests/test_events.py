from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    AcquisitionCeilingExceeded,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSessionIncomplete,
    CompletedAcquisition,
    acquisition_intent_key,
)
from pillarmesh_provider_stripe import (
    STRIPE_API_VERSION,
    StripeClient,
    StripeEventCursor,
    StripeSettings,
)
from pillarmesh_provider_stripe.acquisition import StripeAcquisitionProvider
from pydantic import ValidationError

from providers.stripe.tests.test_codecs import charge_payload, invoice_payload
from providers.stripe.tests.test_settings import _settings_values

type StripePage = tuple[Mapping[str, object], ...]

_PRIOR_CREATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_UPPER_CREATED = datetime(2026, 9, 1, 12, 10, tzinfo=UTC)
_OVERLAP = timedelta(minutes=5)
_VERSION_SET_DIGEST = digest((STRIPE_API_VERSION,))
_PRIVATE_CANARIES = (
    "rk_test_private-event-key",
    "evt_private-cursor-canary",
    "acct_private-account-canary",
    "req_private-request-id-canary",
    "https://api.stripe.com/v1/events?starting_after=private",
    "private-event-body-canary",
)


class FakeStripeClient:
    def __init__(
        self,
        pages: tuple[StripePage, ...] = (),
        *,
        failure: Exception | None = None,
        failure_at_page: int = 0,
        object_pages: Mapping[str, tuple[StripePage, ...]] | None = None,
    ) -> None:
        self._pages = pages
        self._failure = failure
        self._failure_at_page = failure_at_page
        self._object_pages = object_pages
        self.event_queries: list[tuple[int, int, tuple[str, ...]]] = []
        self.object_queries: list[tuple[str, int]] = []
        self.pages_yielded = 0

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[StripePage]:
        self.event_queries.append((created_gte, created_lte, event_types))
        for page_index, page in enumerate(self._pages):
            if self._failure is not None and page_index == self._failure_at_page:
                raise self._failure
            self.pages_yielded += 1
            yield page
        if self._failure is not None and self._failure_at_page >= len(self._pages):
            raise self._failure

    def iter_object_pages(
        self,
        *,
        object_kind: str,
        created_lte: int,
    ) -> Iterator[StripePage]:
        if self._object_pages is not None:
            self.object_queries.append((object_kind, created_lte))
            yield from self._object_pages[object_kind]
            return
        raise AssertionError(
            f"incremental event acquisition cannot list {object_kind} through {created_lte}"
        )


def _settings() -> StripeSettings:
    return StripeSettings.model_validate(_settings_values())


def _schemas() -> tuple[AcquisitionObjectSchema, ...]:
    return tuple(
        AcquisitionObjectSchema(
            logical_object_ref=declaration.logical_object_ref,
            schema_digest=digest(declaration.normalized_fields),
            fields=declaration.normalized_fields,
            record_key_fields=(declaration.normalized_fields[0].name,),
            source_updated_at_field="created",
        )
        for declaration in _settings().objects
    )


def _schemas_for(
    object_refs: tuple[str, ...],
    *,
    source_updated_at_field: str | None = "created",
) -> tuple[AcquisitionObjectSchema, ...]:
    return tuple(
        schema.model_copy(update={"source_updated_at_field": source_updated_at_field})
        for schema in _schemas()
        if schema.logical_object_ref in object_refs
    )


def _cursor(
    *,
    created: datetime = _PRIOR_CREATED,
    event_id: str = "evt_prior_m",
    version_set_digest: str = _VERSION_SET_DIGEST,
) -> StripeEventCursor:
    return StripeEventCursor(
        last_event_created=created,
        last_event_id=event_id,
        api_version_set_digest=version_set_digest,
    )


def _intent(
    private_cursor: bytes,
    *,
    record_ceiling: int = 1_000,
    object_refs: tuple[str, ...] | None = None,
) -> AcquisitionIntent:
    admitted_object_refs = object_refs or tuple(schema.logical_object_ref for schema in _schemas())
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref="1" * 64,
            contract_digest="2" * 64,
            source_binding_ref="source-binding-stripe",
            acquisition_mode="incremental",
            object_refs=admitted_object_refs,
            prior_checkpoint_revision=1,
        ),
        tenant_id="tenant-a",
        run_intent_ref="1" * 64,
        contract_ref="contract-stripe",
        contract_digest="2" * 64,
        source_binding_ref="source-binding-stripe",
        source_observation_digest="3" * 64,
        acquisition_mode="incremental",
        object_refs=admitted_object_refs,
        prior_checkpoint_revision=1,
        prior_checkpoint_digest=hashlib.sha256(private_cursor).hexdigest(),
        record_ceiling=record_ceiling,
        encoded_byte_ceiling=1_000_000,
        admitted_at=_UPPER_CREATED,
    )


def _snapshot_intent() -> AcquisitionIntent:
    object_refs = tuple(schema.logical_object_ref for schema in _schemas())
    return AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id="tenant-a",
            run_intent_ref="1" * 64,
            contract_digest="2" * 64,
            source_binding_ref="source-binding-stripe",
            acquisition_mode="snapshot",
            object_refs=object_refs,
            prior_checkpoint_revision=0,
        ),
        tenant_id="tenant-a",
        run_intent_ref="1" * 64,
        contract_ref="contract-stripe",
        contract_digest="2" * 64,
        source_binding_ref="source-binding-stripe",
        source_observation_digest="3" * 64,
        acquisition_mode="snapshot",
        object_refs=object_refs,
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=1_000,
        encoded_byte_ceiling=1_000_000,
        admitted_at=_UPPER_CREATED,
    )


def _provider(
    client: FakeStripeClient,
    *,
    overlap: timedelta | None = _OVERLAP,
    clock: datetime = _UPPER_CREATED,
) -> StripeAcquisitionProvider:
    typed_client: StripeClient = client
    return StripeAcquisitionProvider(
        _settings(),
        client=typed_client,
        event_overlap_window=overlap,
        clock=lambda: clock,
        private_boundary_reference_factory=(
            lambda tenant_id, logical_object_ref: f"private:{tenant_id}:{logical_object_ref}"
        ),
        private_boundary_writer=lambda _tenant_id, _reference, _payload: None,
    )


def _consume(
    client: FakeStripeClient,
    *,
    cursor: StripeEventCursor | None = None,
    record_ceiling: int = 1_000,
    clock: datetime = _UPPER_CREATED,
) -> tuple[tuple[AcquisitionRecord, ...], CompletedAcquisition]:
    prior = cursor or _cursor()
    private_cursor = prior.to_payload()
    session = _provider(client, clock=clock).open_acquisition(
        _intent(private_cursor, record_ceiling=record_ceiling),
        _schemas(),
        private_cursor,
    )
    records = tuple(session)
    return records, session.complete()


def _event(
    event_id: str,
    created: datetime,
    *,
    invoice_id: str | None = None,
    api_version: str = STRIPE_API_VERSION,
    amount_due: int = 2_500,
    livemode: bool = False,
) -> dict[str, object]:
    embedded_invoice = invoice_payload()
    embedded_invoice["id"] = invoice_id or "in_" + event_id.removeprefix("evt_")
    embedded_invoice["amount_due"] = amount_due
    return {
        "id": event_id,
        "object": "event",
        "api_version": api_version,
        "created": int(created.timestamp()),
        "livemode": livemode,
        "type": "invoice.updated",
        "data": {"object": embedded_invoice},
    }


def _charge_event(event_id: str, created: datetime) -> dict[str, object]:
    embedded_charge = charge_payload()
    embedded_charge["id"] = "ch_" + event_id.removeprefix("evt_")
    return {
        "id": event_id,
        "object": "event",
        "api_version": STRIPE_API_VERSION,
        "created": int(created.timestamp()),
        "livemode": False,
        "type": "charge.updated",
        "data": {"object": embedded_charge},
    }


def _invoice_ids(records: tuple[AcquisitionRecord, ...]) -> tuple[str, ...]:
    return tuple(str(record.fields[0].value) for record in records)


def _assert_failure(
    error: AcquisitionProviderError,
    *,
    classification: str,
    reason_code: str,
) -> None:
    assert error.classification == classification
    assert error.reason_code == reason_code
    public_surface = " ".join((str(error), repr(error), repr(error.args)))
    for canary in _PRIVATE_CANARIES:
        assert canary not in public_surface


def test_event_cursor_is_strict_canonical_and_rejects_an_unapproved_version_set() -> None:
    prior = _cursor()

    assert StripeEventCursor.from_payload(prior.to_payload()) == prior
    with pytest.raises(ValidationError, match="extra"):
        StripeEventCursor.model_validate(
            {**prior.model_dump(), "private_cursor": "evt_private-cursor-canary"},
            strict=True,
        )

    mismatched = _cursor(version_set_digest="4" * 64)
    client = FakeStripeClient()
    private_cursor = mismatched.to_payload()
    with pytest.raises(AcquisitionProviderError) as captured:
        _provider(client).open_acquisition(
            _intent(private_cursor),
            _schemas(),
            private_cursor,
        )

    _assert_failure(
        captured.value,
        classification="permanent_configuration",
        reason_code="permanent_configuration",
    )
    assert client.event_queries == []


def test_incremental_rejects_noncanonical_private_cursor_before_calling_stripe() -> None:
    client = FakeStripeClient()
    private_cursor = _cursor().to_payload() + b"\n"

    with pytest.raises(AcquisitionProviderError) as captured:
        _provider(client).open_acquisition(
            _intent(private_cursor),
            _schemas(),
            private_cursor,
        )

    _assert_failure(
        captured.value,
        classification="integrity_failure",
        reason_code="integrity_failure",
    )
    assert client.event_queries == []


@pytest.mark.parametrize("overlap", (None, timedelta(0), timedelta(seconds=-1)))
def test_incremental_requires_a_positive_overlap_window(overlap: timedelta | None) -> None:
    with pytest.raises(ValueError, match="overlap"):
        _provider(FakeStripeClient(), overlap=overlap)


def test_incremental_queries_inclusive_overlap_and_advances_to_greatest_processed_event() -> None:
    lower = _PRIOR_CREATED - _OVERLAP
    tied_created = _UPPER_CREATED
    client = FakeStripeClient(
        (
            (
                _event("evt_upper_z", tied_created, invoice_id="in_upper_z"),
                _event("evt_upper_a", tied_created, invoice_id="in_upper_a"),
                _event("evt_prior_m", _PRIOR_CREATED, invoice_id="in_prior"),
                _event("evt_overlap_start", lower, invoice_id="in_overlap_start"),
            ),
        )
    )

    records, completion = _consume(client)
    candidate = StripeEventCursor.from_payload(completion.candidate_cursor_payload)

    assert client.event_queries == [
        (
            int(lower.timestamp()),
            int(_UPPER_CREATED.timestamp()),
            _settings().event_types,
        )
    ]
    assert _invoice_ids(records) == ("in_upper_a", "in_upper_z")
    assert candidate == _cursor(created=tied_created, event_id="evt_upper_z")


def test_invoice_incremental_requests_only_invoice_events_and_rejects_a_charge_event() -> None:
    object_refs = ("invoices",)
    invoice_event_types = tuple(
        event_type for event_type in _settings().event_types if event_type.startswith("invoice.")
    )
    client = FakeStripeClient(
        (
            (
                _event("evt_prior_m", _PRIOR_CREATED),
                _charge_event("evt_out_of_scope", _UPPER_CREATED),
            ),
        )
    )
    private_cursor = _cursor().to_payload()
    session = _provider(client).open_acquisition(
        _intent(private_cursor, object_refs=object_refs),
        _schemas_for(object_refs),
        private_cursor,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        tuple(session)

    _assert_failure(
        captured.value,
        classification="integrity_failure",
        reason_code="integrity_failure",
    )
    assert client.event_queries == [
        (
            int((_PRIOR_CREATED - _OVERLAP).timestamp()),
            int(_UPPER_CREATED.timestamp()),
            invoice_event_types,
        )
    ]


def test_overlap_window_without_the_exact_prior_event_is_a_continuity_failure() -> None:
    """A window that never contains the anchor cannot prove continuity.

    This asserts a genuinely missing anchor, not an anchor on a later page: see
    `test_continuity_anchor_may_arrive_on_any_page_of_the_overlap_window`, which
    covers the ordinary multi-page case that Stripe's reverse-chronological
    pagination produces.
    """
    client = FakeStripeClient(
        (
            (_event("evt_new", _UPPER_CREATED),),
            (_event("evt_other", _PRIOR_CREATED, invoice_id="in_other"),),
        )
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification="resynchronization_required",
        reason_code="stripe_event_overlap_gap",
    )
    assert client.pages_yielded == 2


def test_continuity_anchor_may_arrive_on_any_page_of_the_overlap_window() -> None:
    """Stripe lists newest first and `starting_after` walks toward older events.

    The prior cursor's event is the oldest boundary of the overlap window, so on
    any account with more than one page of events it arrives on the last page.
    Proving continuity from the first page alone would demand resynchronization
    on every incremental run.
    """
    newer = _PRIOR_CREATED + timedelta(minutes=1)
    client = FakeStripeClient(
        (
            (
                _event("evt_new_b", newer, invoice_id="in_new_b"),
                _event("evt_new_a", newer, invoice_id="in_new_a"),
            ),
            (_event("evt_prior_m", _PRIOR_CREATED, invoice_id="in_prior"),),
        )
    )

    records, completion = _consume(client)

    assert _invoice_ids(records) == ("in_new_a", "in_new_b")
    assert StripeEventCursor.from_payload(completion.candidate_cursor_payload) == _cursor(
        created=newer,
        event_id="evt_new_b",
    )


def test_event_older_than_overlap_start_is_a_continuity_failure() -> None:
    client = FakeStripeClient(
        (
            (
                _event("evt_prior_m", _PRIOR_CREATED),
                _event("evt_too_old", _PRIOR_CREATED - _OVERLAP - timedelta(seconds=1)),
            ),
        )
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification="resynchronization_required",
        reason_code="stripe_event_overlap_gap",
    )


def test_cursor_older_than_the_thirty_day_retrieval_window_requires_resynchronization() -> None:
    expired_created = _UPPER_CREATED - timedelta(days=30, seconds=1)
    client = FakeStripeClient()

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client, cursor=_cursor(created=expired_created))

    _assert_failure(
        captured.value,
        classification="resynchronization_required",
        reason_code="stripe_event_cursor_expired",
    )
    assert client.event_queries == []


def test_identical_duplicate_events_are_deduplicated_before_normalization() -> None:
    new_event = _event("evt_new", _UPPER_CREATED, invoice_id="in_new")
    client = FakeStripeClient(
        (
            (new_event, _event("evt_prior_m", _PRIOR_CREATED)),
            (dict(new_event),),
        )
    )

    records, completion = _consume(client)

    assert _invoice_ids(records) == ("in_new",)
    assert StripeEventCursor.from_payload(completion.candidate_cursor_payload) == _cursor(
        created=_UPPER_CREATED,
        event_id="evt_new",
    )


def test_contradictory_payloads_for_one_new_event_id_fail_integrity() -> None:
    client = FakeStripeClient(
        (
            (
                _event("evt_new", _UPPER_CREATED, amount_due=2_500),
                _event("evt_prior_m", _PRIOR_CREATED),
            ),
            (_event("evt_new", _UPPER_CREATED, amount_due=9_999),),
        )
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification="integrity_failure",
        reason_code="integrity_failure",
    )


def test_deduplication_precedes_discard_of_already_committed_events() -> None:
    client = FakeStripeClient(
        (
            (_event("evt_prior_m", _PRIOR_CREATED, amount_due=2_500),),
            (_event("evt_prior_m", _PRIOR_CREATED, amount_due=9_999),),
        )
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification="integrity_failure",
        reason_code="integrity_failure",
    )


@pytest.mark.parametrize(
    ("defect", "classification", "reason_code"),
    (
        ("unsupported_version", "permanent_configuration", "permanent_configuration"),
        ("wrong_account_mode", "invalid_provider_response", "invalid_provider_response"),
        ("invalid_invoice", "invalid_provider_response", "invalid_provider_response"),
    ),
)
def test_already_committed_anchor_is_validated_before_discard(
    defect: str,
    classification: str,
    reason_code: str,
) -> None:
    anchor = _event("evt_prior_m", _PRIOR_CREATED)
    if defect == "unsupported_version":
        anchor["api_version"] = "2025-12-15.preview"
    elif defect == "wrong_account_mode":
        anchor["livemode"] = True
    else:
        embedded_invoice = anchor["data"]
        assert isinstance(embedded_invoice, dict)
        invoice = embedded_invoice["object"]
        assert isinstance(invoice, dict)
        del invoice["amount_due"]
    client = FakeStripeClient(((anchor,),))

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification=classification,
        reason_code=reason_code,
    )
    assert client.pages_yielded == 1


def test_timestamp_ties_are_ordered_by_event_id_not_page_order() -> None:
    tied_created = _PRIOR_CREATED + timedelta(minutes=1)
    client = FakeStripeClient(
        (
            (
                _event("evt_tie_z", tied_created, invoice_id="in_z"),
                _event("evt_prior_m", _PRIOR_CREATED),
            ),
            (
                _event("evt_tie_a", tied_created, invoice_id="in_a"),
                _event("evt_tie_m", tied_created, invoice_id="in_m"),
            ),
        )
    )

    records, completion = _consume(client)

    assert _invoice_ids(records) == ("in_a", "in_m", "in_z")
    assert StripeEventCursor.from_payload(completion.candidate_cursor_payload) == _cursor(
        created=tied_created,
        event_id="evt_tie_z",
    )


def test_tied_events_use_event_id_for_cursor_and_object_id_for_grouped_records() -> None:
    tied_created = _PRIOR_CREATED + timedelta(minutes=1)
    client = FakeStripeClient(
        (
            (
                _event("evt_tie_a", tied_created, invoice_id="in_z"),
                _event("evt_prior_m", _PRIOR_CREATED),
                _event("evt_tie_z", tied_created, invoice_id="in_a"),
            ),
        )
    )

    records, completion = _consume(client)

    assert _invoice_ids(records) == ("in_a", "in_z")
    assert StripeEventCursor.from_payload(completion.candidate_cursor_payload) == _cursor(
        created=tied_created,
        event_id="evt_tie_z",
    )


def test_unsupported_event_creation_version_aborts_without_candidate_cursor() -> None:
    client = FakeStripeClient(
        (
            (
                _event("evt_prior_m", _PRIOR_CREATED),
                _event(
                    "evt_unsupported",
                    _UPPER_CREATED,
                    api_version="2025-12-15.preview",
                ),
            ),
        )
    )
    private_cursor = _cursor().to_payload()
    session = _provider(client).open_acquisition(
        _intent(private_cursor),
        _schemas(),
        private_cursor,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        tuple(session)

    _assert_failure(
        captured.value,
        classification="permanent_configuration",
        reason_code="permanent_configuration",
    )
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()


def test_page_larger_than_stripes_limit_is_rejected_before_publication() -> None:
    oversized_page = (
        *(
            _event(
                "evt_page_" + str(index),
                _PRIOR_CREATED + timedelta(seconds=index + 1),
            )
            for index in range(100)
        ),
        _event("evt_prior_m", _PRIOR_CREATED),
    )
    client = FakeStripeClient((oversized_page,))

    with pytest.raises(AcquisitionProviderError) as captured:
        _consume(client)

    _assert_failure(
        captured.value,
        classification="invalid_provider_response",
        reason_code="invalid_provider_response",
    )


def test_cumulative_record_ceiling_refuses_a_later_page_without_completion() -> None:
    client = FakeStripeClient(
        (
            (
                _event("evt_new_1", _PRIOR_CREATED + timedelta(minutes=1)),
                _event("evt_prior_m", _PRIOR_CREATED),
            ),
            (
                _event("evt_new_2", _PRIOR_CREATED + timedelta(minutes=2)),
                _event("evt_new_3", _PRIOR_CREATED + timedelta(minutes=3)),
            ),
            (_event("evt_must_remain_unread", _PRIOR_CREATED + timedelta(minutes=4)),),
        )
    )
    private_cursor = _cursor().to_payload()
    session = _provider(client).open_acquisition(
        _intent(private_cursor, record_ceiling=2),
        _schemas(),
        private_cursor,
    )

    with pytest.raises(AcquisitionCeilingExceeded):
        tuple(session)
    assert client.pages_yielded == 2
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()


def test_snapshot_candidate_bytes_bootstrap_incremental_and_handoff_event_cursor() -> None:
    object_pages = {
        object_kind: ((),) for object_kind in ("charge", "customer", "invoice", "refund")
    }
    snapshot_client = FakeStripeClient(object_pages=object_pages)
    snapshot_session = _provider(snapshot_client).open_acquisition(
        _snapshot_intent(),
        _schemas_for(
            tuple(schema.logical_object_ref for schema in _schemas()),
            source_updated_at_field=None,
        ),
        None,
    )

    assert tuple(snapshot_session) == ()
    snapshot_candidate = snapshot_session.complete().candidate_cursor_payload
    snapshot_cursor = StripeEventCursor.from_payload(snapshot_candidate)

    event_created = _UPPER_CREATED + timedelta(minutes=1)
    event_client = FakeStripeClient(
        ((_event("evt_first_incremental", event_created, invoice_id="in_first"),),)
    )
    incremental_session = _provider(
        event_client,
        clock=_UPPER_CREATED + timedelta(minutes=2),
    ).open_acquisition(
        _intent(snapshot_candidate),
        _schemas(),
        snapshot_candidate,
    )

    records = tuple(incremental_session)
    handoff = StripeEventCursor.from_payload(
        incremental_session.complete().candidate_cursor_payload
    )

    assert snapshot_cursor.last_event_created == _UPPER_CREATED
    assert _invoice_ids(records) == ("in_first",)
    assert handoff == _cursor(created=event_created, event_id="evt_first_incremental")


def test_bootstrap_cursor_does_not_regress_to_an_event_before_snapshot_boundary() -> None:
    object_pages = {
        object_kind: ((),) for object_kind in ("charge", "customer", "invoice", "refund")
    }
    snapshot_session = _provider(FakeStripeClient(object_pages=object_pages)).open_acquisition(
        _snapshot_intent(),
        _schemas_for(
            tuple(schema.logical_object_ref for schema in _schemas()),
            source_updated_at_field=None,
        ),
        None,
    )
    assert tuple(snapshot_session) == ()
    snapshot_candidate = snapshot_session.complete().candidate_cursor_payload
    snapshot_cursor = StripeEventCursor.from_payload(snapshot_candidate)
    pre_snapshot_event = _event(
        "evt_before_snapshot",
        _UPPER_CREATED - timedelta(minutes=1),
        invoice_id="in_before_snapshot",
    )

    first_incremental = _provider(
        FakeStripeClient(((pre_snapshot_event,),)),
        clock=_UPPER_CREATED + timedelta(minutes=1),
    ).open_acquisition(_intent(snapshot_candidate), _schemas(), snapshot_candidate)

    assert _invoice_ids(tuple(first_incremental)) == ("in_before_snapshot",)
    assert first_incremental.complete().candidate_cursor_payload == snapshot_candidate
    assert StripeEventCursor.from_payload(snapshot_candidate) == snapshot_cursor


def test_bootstrap_cursor_advances_only_to_a_post_snapshot_event() -> None:
    object_pages = {
        object_kind: ((),) for object_kind in ("charge", "customer", "invoice", "refund")
    }
    snapshot_session = _provider(FakeStripeClient(object_pages=object_pages)).open_acquisition(
        _snapshot_intent(),
        _schemas_for(
            tuple(schema.logical_object_ref for schema in _schemas()),
            source_updated_at_field=None,
        ),
        None,
    )
    assert tuple(snapshot_session) == ()
    snapshot_candidate = snapshot_session.complete().candidate_cursor_payload
    post_snapshot_created = _UPPER_CREATED + timedelta(seconds=1)
    client = FakeStripeClient(
        (
            (
                _event(
                    "evt_before_snapshot",
                    _UPPER_CREATED - timedelta(minutes=1),
                    invoice_id="in_before_snapshot",
                ),
                _event(
                    "evt_after_snapshot",
                    post_snapshot_created,
                    invoice_id="in_after_snapshot",
                ),
            ),
        )
    )

    incremental = _provider(
        client,
        clock=_UPPER_CREATED + timedelta(minutes=1),
    ).open_acquisition(_intent(snapshot_candidate), _schemas(), snapshot_candidate)
    records = tuple(incremental)

    assert _invoice_ids(records) == ("in_before_snapshot", "in_after_snapshot")
    assert StripeEventCursor.from_payload(
        incremental.complete().candidate_cursor_payload
    ) == _cursor(
        created=post_snapshot_created,
        event_id="evt_after_snapshot",
    )


def test_abort_is_idempotent_and_never_completes_a_partially_consumed_session() -> None:
    client = FakeStripeClient(
        (
            (
                _event("evt_new_1", _PRIOR_CREATED + timedelta(minutes=1)),
                _event("evt_new_2", _PRIOR_CREATED + timedelta(minutes=2)),
                _event("evt_prior_m", _PRIOR_CREATED),
            ),
        )
    )
    private_cursor = _cursor().to_payload()
    session = _provider(client).open_acquisition(
        _intent(private_cursor),
        _schemas(),
        private_cursor,
    )

    first_record = next(iter(session))
    session.abort()
    session.abort()

    assert first_record.logical_object_ref == "invoices"
    assert tuple(session) == ()
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()


def test_unclassified_client_failure_aborts_with_sanitized_integrity_error() -> None:
    diagnostic = " ".join(_PRIVATE_CANARIES)
    client = FakeStripeClient(
        ((_event("evt_prior_m", _PRIOR_CREATED),),),
        failure=RuntimeError(diagnostic),
        failure_at_page=0,
    )
    private_cursor = _cursor().to_payload()
    session = _provider(client).open_acquisition(
        _intent(private_cursor),
        _schemas(),
        private_cursor,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        tuple(session)

    _assert_failure(
        captured.value,
        classification="integrity_failure",
        reason_code="integrity_failure",
    )
    with pytest.raises(AcquisitionSessionIncomplete):
        session.complete()
