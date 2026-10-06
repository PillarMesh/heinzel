from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from heinzel_contract_model import canonical_bytes, digest
from heinzel_request_management import (
    InboxRequest,
    RequestManagementService,
    SQLiteRequestRepository,
)
from heinzel_request_management.intake import RequestDigestMismatch, RequestIntakeContent
from heinzel_request_management.models import (
    DataAccessRequest,
    QuestionTermSelection,
    StakeholderQuestion,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _content(kind: str) -> RequestIntakeContent:
    payload = (
        StakeholderQuestion(purpose="Review café ☕", question='Why?\n"Revenue"')
        if kind == "question"
        else DataAccessRequest(
            purpose="Review café ☕",
            data_product_id="product-revenue",
            requested_fields=("total", "date"),
            access_mode="export",
            expires_at=datetime(2026, 9, 15, 12, 30, 0, 123456, tzinfo=UTC),
        )
    )
    return RequestIntakeContent(title="Weekly 📊", payload=payload)


def _submit(
    service: RequestManagementService, content: RequestIntakeContent, declared: str
) -> InboxRequest:
    payload = content.payload
    if isinstance(payload, StakeholderQuestion):
        return service.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            title=content.title,
            purpose=payload.purpose,
            question=payload.question,
            request_digest=declared,
        )
    return service.submit_access_request(
        tenant_id="tenant-a",
        requester_id="requester-a",
        title=content.title,
        purpose=payload.purpose,
        data_product_id=payload.data_product_id,
        requested_fields=payload.requested_fields,
        access_mode=payload.access_mode,
        expires_at=payload.expires_at,
        request_digest=declared,
    )


@pytest.mark.parametrize("kind", ["question", "access"])
def test_matching_content_is_stored_and_the_digest_is_not_a_deduplication_key(kind: str) -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    content = _content(kind)

    first = _submit(service, content, digest(content))
    second = _submit(service, content, digest(content))

    assert first.payload == content.payload
    assert first.title == content.title
    assert first.request_id != second.request_id
    assert len(service.list_inbox("tenant-a")) == 2
    repository.close()


@pytest.mark.parametrize("kind", ["question", "access"])
@pytest.mark.parametrize("declared", ["0" * 64, "", "A" * 64])
def test_digest_mismatch_never_allocates_an_identity_or_writes(kind: str, declared: str) -> None:
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    sequence = repository.peek_next_sequence("tenant-a")

    with pytest.raises(RequestDigestMismatch, match="request digest does not match"):
        _submit(service, _content(kind), declared)

    assert repository.peek_next_sequence("tenant-a") == sequence
    assert service.list_inbox("tenant-a") == ()
    repository.close()


@pytest.mark.parametrize("kind", ["question", "access"])
def test_title_and_each_payload_field_are_bound_by_the_digest(kind: str) -> None:
    content = _content(kind)
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    changes: list[RequestIntakeContent] = [content.model_copy(update={"title": "Changed"})]
    for field, value in content.payload.model_dump().items():
        if field == "request_type":
            continue
        replacement: object
        if field == "access_mode":
            replacement = "query"
        elif isinstance(value, datetime):
            replacement = value + timedelta(seconds=1)
        elif isinstance(value, tuple):
            replacement = tuple(reversed(value))
        else:
            replacement = str(value) + "changed"
        changed_payload = type(content.payload).model_validate(
            content.payload.model_dump() | {field: replacement}
        )
        changes.append(content.model_copy(update={"payload": changed_payload}))

    for changed in changes:
        with pytest.raises(RequestDigestMismatch):
            _submit(service, changed, digest(content))

    assert service.list_inbox("tenant-a") == ()
    repository.close()


def test_question_canonical_bytes_preserve_unicode_and_escape_control_characters() -> None:
    expected = (
        '{"payload":{"purpose":"Review café ☕",'
        '"question":"Why?\\n\\"Revenue\\"",'
        '"request_type":"stakeholder_question"},'
        '"title":"Weekly 📊"}'
    )
    assert canonical_bytes(_content("question")) == expected.encode("utf-8")


def test_access_canonical_bytes_preserve_microseconds_and_field_order() -> None:
    expected = (
        '{"payload":{"access_mode":"export",'
        '"data_product_id":"product-revenue",'
        '"expires_at":"2026-09-15T12:30:00.123456Z",'
        '"purpose":"Review café ☕",'
        '"request_type":"data_access",'
        '"requested_fields":["total",'
        '"date"]},'
        '"title":"Weekly 📊"}'
    )
    assert canonical_bytes(_content("access")) == expected.encode("utf-8")


# The digest of a question carrying no term selection, which is every question submitted before
# selections existed. It is the SHA-256 of exactly the canonical bytes pinned above, and it is
# written out rather than derived so that a change to `StakeholderQuestion` which altered what an
# existing record hashes to fails here instead of silently invalidating every stored digest.
_UNSELECTED_QUESTION_DIGEST = "cc69e96caed1a7409e4d017f9adc802255b1d7117299e05200462287e22fe2c1"


def test_a_question_with_no_selection_digests_to_what_it_always_digested_to() -> None:
    """An optional field must leave existing records exactly where they were.

    `selection` is excluded from serialization when absent, so it contributes no key, no null and
    no digest change. A record stored before the field existed still validates, and the digest it
    was accepted under still matches.
    """
    content = _content("question")

    assert content.payload.selection is None
    assert "selection" not in content.payload.model_dump()
    assert digest(content) == _UNSELECTED_QUESTION_DIGEST


def test_a_question_with_no_selection_is_still_submitted_and_stored_unchanged() -> None:
    """Intake asks for no selection, and a request without one reaches the inbox as before."""
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    content = _content("question")

    stored = _submit(service, content, _UNSELECTED_QUESTION_DIGEST)

    assert stored.payload == content.payload
    assert isinstance(stored.payload, StakeholderQuestion)
    assert stored.payload.selection is None
    repository.close()


def test_a_selected_question_binds_every_term_it_names_into_its_digest() -> None:
    """The browser composes the same bytes, so this pins them exactly.

    The console digests the content it submits and the service verifies that digest against the
    content it rebuilds. The nested selection's own `schema_version` is part of those bytes, and a
    caller that left it out would be refused at intake rather than storing a different artifact.
    """
    content = RequestIntakeContent(
        title="Weekly 📊",
        payload=StakeholderQuestion(
            purpose="Review café ☕",
            question='Why?\n"Revenue"',
            selection=QuestionTermSelection(
                metric_ref="daily-order-value",
                dimension_refs=("order_day", "order_region"),
            ),
        ),
    )
    expected = (
        '{"payload":{"purpose":"Review café ☕",'
        '"question":"Why?\\n\\"Revenue\\"",'
        '"request_type":"stakeholder_question",'
        '"selection":{"dimension_refs":["order_day","order_region"],'
        '"metric_ref":"daily-order-value",'
        '"schema_version":"1"}},'
        '"title":"Weekly 📊"}'
    )

    assert canonical_bytes(content) == expected.encode("utf-8")
    assert digest(content) != _UNSELECTED_QUESTION_DIGEST


def test_a_selection_submitted_with_another_selections_digest_is_refused() -> None:
    """The terms a question names are content, so swapping them invalidates the digest."""
    repository = SQLiteRequestRepository.open(":memory:")
    service = RequestManagementService(repository, clock=lambda: NOW)
    selection = QuestionTermSelection(metric_ref="daily-order-value", dimension_refs=("order_day",))
    declared = digest(
        RequestIntakeContent(
            title="Weekly 📊",
            payload=StakeholderQuestion(
                purpose="Review café ☕", question="Why?", selection=selection
            ),
        )
    )

    with pytest.raises(RequestDigestMismatch, match="request digest does not match"):
        service.submit_question(
            tenant_id="tenant-a",
            requester_id="requester-a",
            title="Weekly 📊",
            purpose="Review café ☕",
            question="Why?",
            selection=selection.model_copy(update={"dimension_refs": ("order_region",)}),
            request_digest=declared,
        )

    assert service.list_inbox("tenant-a") == ()
    repository.close()


@pytest.mark.parametrize(
    ("metric_ref", "dimension_refs", "message"),
    (
        ("daily-order-value", ("order_day", "order_day"), "must not contain duplicates"),
        ("daily-order-value", ("daily-order-value",), "both the metric and a dimension"),
        ("daily-order-value", (" ",), "cannot be blank"),
    ),
)
def test_a_selection_that_names_a_term_twice_or_blankly_is_never_stored(
    metric_ref: str, dimension_refs: tuple[str, ...], message: str
) -> None:
    """The artifact refuses these, so no request carries one and no interpreter has to resolve it.

    A duplicated breakdown would group twice by one column, and a term that is both the measure
    and the breakdown is not a question the semantic layer can be asked.
    """
    with pytest.raises(ValueError, match=message):
        QuestionTermSelection(metric_ref=metric_ref, dimension_refs=dimension_refs)


def test_a_selection_must_name_a_metric_and_at_least_one_breakdown() -> None:
    """An empty breakdown asks for a total no published query binding offers."""
    with pytest.raises(ValueError, match="at least 1 item"):
        QuestionTermSelection(metric_ref="daily-order-value", dimension_refs=())
