from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

import httpx
import pytest
from pillarmesh_provider_sdk import AcquisitionProviderError
from pillarmesh_provider_stripe.client import HttpxStripeClient, StripeClient
from pydantic import SecretStr

type StripeObjectKind = Literal["customer", "invoice", "charge", "refund"]

_API_VERSION = "2026-02-25.clover"
_CREATED_LTE = 1_788_220_800
_API_KEY_CANARY = "rk_test_private-api-key-canary"
_BODY_CANARY = "private-response-body-canary"
_URL_CANARY = "private-pagination-cursor-canary"
_ACCOUNT_CANARY = "acct_private-account-canary"
_REQUEST_ID_CANARY = "req_private-request-id-canary"
_EVENT_OBJECT_KIND: dict[str, StripeObjectKind] = {
    "charge": "charge",
    "customer": "customer",
    "invoice": "invoice",
    "refund": "refund",
}


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    account_mode: Literal["live", "test"] = "test",
) -> tuple[StripeClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client: StripeClient = HttpxStripeClient(
        api_key=SecretStr(_API_KEY_CANARY),
        api_version=_API_VERSION,
        account_mode=account_mode,
        http_client=http_client,
    )
    return client, http_client


def _resource(
    object_kind: StripeObjectKind,
    object_id: str,
    *,
    livemode: bool = False,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": object_id,
        "object": object_kind,
        "created": _CREATED_LTE - 1,
        "livemode": livemode,
    }
    if extra is not None:
        payload.update(extra)
    return payload


def _page(
    object_kind: StripeObjectKind,
    path: str,
    *,
    data: list[object],
    has_more: bool,
) -> dict[str, object]:
    return {
        "object": "list",
        "url": path,
        "has_more": has_more,
        "data": data,
    }


def _event(event_id: str, event_type: str, *, created: int = _CREATED_LTE - 1) -> dict[str, object]:
    object_kind = _EVENT_OBJECT_KIND[event_type.split(".", maxsplit=1)[0]]
    return {
        "id": event_id,
        "object": "event",
        "api_version": _API_VERSION,
        "created": created,
        "livemode": False,
        "type": event_type,
        "data": {"object": _resource(object_kind, f"{object_kind}_fixture")},
    }


def _list_objects(
    client: StripeClient,
    *,
    object_kind: StripeObjectKind,
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        resource
        for page in client.iter_object_pages(
            object_kind=object_kind,
            created_lte=_CREATED_LTE,
        )
        for resource in page
    )


@pytest.mark.parametrize(
    ("object_kind", "path", "object_id"),
    (
        ("customer", "/v1/customers", "cus_fixture"),
        ("invoice", "/v1/invoices", "in_fixture"),
        ("charge", "/v1/charges", "ch_fixture"),
        ("refund", "/v1/refunds", "re_fixture"),
    ),
)
def test_list_objects_uses_exact_endpoint_object_kind_and_pinned_request(
    object_kind: StripeObjectKind,
    path: str,
    object_id: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_page(
                object_kind,
                path,
                data=[_resource(object_kind, object_id)],
                has_more=False,
            ),
        )

    client, http_client = _client(handler)
    try:
        resources = _list_objects(client, object_kind=object_kind)
    finally:
        http_client.close()

    assert tuple(resource["id"] for resource in resources) == (object_id,)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.stripe.com"
    assert request.url.path == path
    assert dict(request.url.params) == {"created[lte]": str(_CREATED_LTE), "limit": "100"}
    assert not any(name.startswith("expand") for name in request.url.params)
    assert request.headers["Stripe-Version"] == _API_VERSION
    assert request.headers["Authorization"] == f"Bearer {_API_KEY_CANARY}"
    assert request.content == b""


def test_list_objects_follows_only_the_last_seen_id_and_pins_every_page_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=_page(
                    "customer",
                    "/v1/customers",
                    data=[
                        _resource("customer", "cus_new"),
                        _resource("customer", "cus_middle"),
                    ],
                    has_more=True,
                ),
            )
        return httpx.Response(
            200,
            json=_page(
                "customer",
                "/v1/customers",
                data=[_resource("customer", "cus_old")],
                has_more=False,
            ),
        )

    client, http_client = _client(handler)
    try:
        pages = client.iter_object_pages(object_kind="customer", created_lte=_CREATED_LTE)
        first_page = next(pages)
        assert tuple(resource["id"] for resource in first_page) == ("cus_new", "cus_middle")
        assert len(requests) == 1
        second_page = next(pages)
        assert tuple(resource["id"] for resource in second_page) == ("cus_old",)
        with pytest.raises(StopIteration):
            next(pages)
    finally:
        http_client.close()

    assert len(requests) == 2
    assert dict(requests[0].url.params) == {
        "created[lte]": str(_CREATED_LTE),
        "limit": "100",
    }
    assert dict(requests[1].url.params) == {
        "created[lte]": str(_CREATED_LTE),
        "limit": "100",
        "starting_after": "cus_middle",
    }
    assert all(request.headers["Stripe-Version"] == _API_VERSION for request in requests)
    assert all(
        not any(name.startswith("expand") for name in request.url.params) for request in requests
    )


def test_event_pages_use_exact_endpoint_filters_and_preserve_page_boundaries() -> None:
    requests: list[httpx.Request] = []
    event_types = ("charge.succeeded", "invoice.paid")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "url": "/v1/events",
                "has_more": False,
                "data": [_event("evt_fixture", "invoice.paid")],
            },
        )

    client, http_client = _client(handler)
    try:
        pages = tuple(
            client.iter_event_pages(
                created_gte=_CREATED_LTE - 300,
                created_lte=_CREATED_LTE,
                event_types=event_types,
            )
        )
    finally:
        http_client.close()

    assert len(pages) == 1
    assert tuple(event["id"] for event in pages[0]) == ("evt_fixture",)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/events"
    assert request.url.params.multi_items() == [
        ("created[gte]", str(_CREATED_LTE - 300)),
        ("created[lte]", str(_CREATED_LTE)),
        ("limit", "100"),
        ("types[]", "charge.succeeded"),
        ("types[]", "invoice.paid"),
    ]
    assert request.headers["Stripe-Version"] == _API_VERSION


@pytest.mark.parametrize(
    ("event_types", "created", "livemode"),
    (
        (("payment_intent.succeeded",), _CREATED_LTE - 1, False),
        (("invoice.paid", "charge.succeeded"), _CREATED_LTE - 1, False),
        (("invoice.paid",), _CREATED_LTE + 1, False),
        (("invoice.paid",), _CREATED_LTE - 301, False),
        (("invoice.paid",), _CREATED_LTE - 1, True),
    ),
)
def test_event_pages_reject_unapproved_authority_or_mismatched_payload(
    event_types: tuple[str, ...],
    created: int,
    livemode: bool,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        event = _event("evt_fixture", "invoice.paid", created=created)
        event["livemode"] = livemode
        return httpx.Response(
            200,
            json={"object": "list", "url": "/v1/events", "has_more": False, "data": [event]},
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises((AcquisitionProviderError, ValueError)):
            tuple(
                client.iter_event_pages(
                    created_gte=_CREATED_LTE - 300,
                    created_lte=_CREATED_LTE,
                    event_types=event_types,
                )
            )
    finally:
        http_client.close()

    if event_types != tuple(sorted(set(event_types))) or "payment_intent.succeeded" in event_types:
        assert request_count == 0


def test_list_objects_rejects_an_expanded_relationship() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(
                "charge",
                "/v1/charges",
                data=[
                    _resource(
                        "charge",
                        "ch_fixture",
                        extra={
                            "customer": {
                                "id": "cus_expanded",
                                "object": "customer",
                            }
                        },
                    )
                ],
                has_more=False,
            ),
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="charge")
    finally:
        http_client.close()

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"object": "collection", "url": "/v1/customers", "has_more": False, "data": []},
        {
            "object": "list",
            "url": f"/v1/customers?starting_after={_URL_CANARY}",
            "has_more": False,
            "data": [],
        },
        {"object": "list", "url": "/v1/customers", "has_more": "false", "data": []},
        {"object": "list", "url": "/v1/customers", "has_more": False, "data": {}},
        _page(
            "customer",
            "/v1/customers",
            data=[_resource("invoice", "in_wrong-kind")],
            has_more=False,
        ),
        _page("customer", "/v1/customers", data=[], has_more=True),
        _page(
            "customer",
            "/v1/customers",
            data=[{"object": "customer", "created": _CREATED_LTE - 1, "livemode": False}],
            has_more=True,
        ),
        _page(
            "customer",
            "/v1/customers",
            data=[_resource("customer", "cus_live", livemode=True)],
            has_more=False,
        ),
        _page(
            "customer",
            "/v1/customers",
            data=[
                _resource(
                    "customer",
                    "cus_outside_bound",
                    extra={"created": _CREATED_LTE + 1},
                )
            ],
            has_more=False,
        ),
    ),
)
def test_list_objects_rejects_a_malformed_or_mismatched_page(payload: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"
    _assert_public_error_is_sanitized(captured.value)


def test_list_objects_rejects_invalid_json_as_a_malformed_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                f"{_BODY_CANARY} {_URL_CANARY} {_ACCOUNT_CANARY} {_REQUEST_ID_CANARY}"
            ).encode(),
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"
    _assert_public_error_is_sanitized(captured.value)


def test_list_objects_refuses_a_non_advancing_pagination_cursor() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json=_page(
                "customer",
                "/v1/customers",
                data=[_resource("customer", "cus_repeated")],
                has_more=True,
            ),
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert request_count == 2
    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"


@pytest.mark.parametrize(
    ("status_code", "classification", "reason_code"),
    (
        (429, "throttled", "rate_limited"),
        (408, "transient_unavailable", "provider_unavailable"),
        (500, "transient_unavailable", "provider_unavailable"),
        (503, "transient_unavailable", "provider_unavailable"),
        (401, "authorization_denied", "authorization_denied"),
        (403, "authorization_denied", "authorization_denied"),
        (400, "statement_rejected", "statement_rejected"),
        (422, "statement_rejected", "statement_rejected"),
    ),
)
def test_list_objects_classifies_http_failures_without_private_diagnostics(
    status_code: int,
    classification: str,
    reason_code: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text=f"{_BODY_CANARY} {_URL_CANARY}",
            headers={
                "Request-Id": _REQUEST_ID_CANARY,
                "Stripe-Account": _ACCOUNT_CANARY,
            },
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert captured.value.classification == classification
    assert captured.value.reason_code == reason_code
    _assert_public_error_is_sanitized(captured.value)


def test_list_objects_classifies_transport_failure_without_leaking_request_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"{_API_KEY_CANARY} {_BODY_CANARY} {_URL_CANARY} "
            f"{_ACCOUNT_CANARY} {_REQUEST_ID_CANARY}",
            request=request,
        )

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert captured.value.classification == "transient_transport"
    assert captured.value.reason_code == "transport_failure"
    _assert_public_error_is_sanitized(captured.value)


def test_list_objects_classifies_timeout_as_provider_unavailability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(_BODY_CANARY, request=request)

    client, http_client = _client(handler)
    try:
        with pytest.raises(AcquisitionProviderError) as captured:
            _list_objects(client, object_kind="customer")
    finally:
        http_client.close()

    assert captured.value.classification == "transient_unavailable"
    assert captured.value.reason_code == "provider_unavailable"
    _assert_public_error_is_sanitized(captured.value)


def _assert_public_error_is_sanitized(error: AcquisitionProviderError) -> None:
    public_surface = " ".join((str(error), repr(error), repr(error.args)))
    for canary in (
        _API_KEY_CANARY,
        _BODY_CANARY,
        _URL_CANARY,
        _ACCOUNT_CANARY,
        _REQUEST_ID_CANARY,
        "api.stripe.com",
        "/v1/customers",
    ):
        assert canary not in public_surface
    assert error.__cause__ is None
    assert error.__context__ is None
