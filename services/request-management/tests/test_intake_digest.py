from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_request_management import (
    InboxRequest,
    RequestManagementService,
    SQLiteRequestRepository,
)
from pillarmesh_request_management.intake import RequestDigestMismatch, RequestIntakeContent
from pillarmesh_request_management.models import DataAccessRequest, StakeholderQuestion

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
