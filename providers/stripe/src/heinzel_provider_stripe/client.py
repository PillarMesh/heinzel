from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType
from typing import Protocol

import httpx
from heinzel_provider_sdk import AcquisitionProviderError
from heinzel_provider_sdk.errors import (
    AcquisitionProviderErrorClassification,
    AcquisitionProviderReasonCode,
)
from pydantic import SecretStr

from .models import _StripeListPage
from .settings import (
    _APPROVED_EVENT_TYPES,
    STRIPE_API_VERSION,
    StripeAccountMode,
    StripeObjectKind,
)

_LIST_PATHS: dict[StripeObjectKind, str] = {
    "charge": "/v1/charges",
    "customer": "/v1/customers",
    "invoice": "/v1/invoices",
    "refund": "/v1/refunds",
}
_EVENTS_PATH = "/v1/events"
_PAGE_KEYS = frozenset(("data", "has_more", "object", "url"))
_ALLOWED_NESTED_FIELDS = frozenset(("metadata", "status_transitions"))
_APPROVED_EVENT_TYPE_SET = frozenset(_APPROVED_EVENT_TYPES)

type _PageValidator = Callable[[object], Mapping[str, object]]


class StripeClient(Protocol):
    def iter_object_pages(
        self,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Iterator[tuple[Mapping[str, object], ...]]: ...

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[tuple[Mapping[str, object], ...]]: ...


class HttpxStripeClient:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_version: str,
        account_mode: StripeAccountMode,
        http_client: httpx.Client | None = None,
    ) -> None:
        if api_version != STRIPE_API_VERSION:
            raise ValueError("Stripe client API version is unsupported")
        expected_key_prefix = f"rk_{account_mode}_"
        if not api_key.get_secret_value().startswith(expected_key_prefix):
            raise ValueError("Stripe client key does not match account mode")
        self._api_key = api_key
        self._account_mode = account_mode
        self._http_client = http_client or httpx.Client(timeout=10.0)

    def iter_object_pages(
        self,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Iterator[tuple[Mapping[str, object], ...]]:
        _require_epoch_bound(created_lte, "created_lte")
        path = _LIST_PATHS[object_kind]
        yield from self._iter_pages(
            path=path,
            base_params=(("created[lte]", str(created_lte)), ("limit", "100")),
            validator=lambda candidate: self._validated_resource(
                candidate,
                object_kind=object_kind,
                created_lte=created_lte,
            ),
        )

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[tuple[Mapping[str, object], ...]]:
        _require_epoch_bound(created_gte, "created_gte")
        _require_epoch_bound(created_lte, "created_lte")
        if created_gte > created_lte:
            raise ValueError("Stripe event lower bound cannot exceed its upper bound")
        if (
            not event_types
            or event_types != tuple(sorted(set(event_types)))
            or not set(event_types).issubset(_APPROVED_EVENT_TYPE_SET)
        ):
            raise ValueError("Stripe event types must be a canonical approved subset")
        base_params = (
            ("created[gte]", str(created_gte)),
            ("created[lte]", str(created_lte)),
            ("limit", "100"),
            *(("types[]", event_type) for event_type in event_types),
        )
        yield from self._iter_pages(
            path=_EVENTS_PATH,
            base_params=base_params,
            validator=lambda candidate: self._validated_event(
                candidate,
                created_gte=created_gte,
                created_lte=created_lte,
                event_types=event_types,
            ),
        )

    def _iter_pages(
        self,
        *,
        path: str,
        base_params: tuple[tuple[str, str], ...],
        validator: _PageValidator,
    ) -> Iterator[tuple[Mapping[str, object], ...]]:
        starting_after: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = base_params
            if starting_after is not None:
                params = (*params, ("starting_after", starting_after))
            page = self._list_page(path=path, params=params, validator=validator)
            yield page.data
            if not page.has_more:
                return
            next_cursor = page.next_cursor
            if next_cursor is None or next_cursor in seen_cursors:
                raise _provider_error("invalid_provider_response")
            seen_cursors.add(next_cursor)
            starting_after = next_cursor

    def _list_page(
        self,
        *,
        path: str,
        params: tuple[tuple[str, str], ...],
        validator: _PageValidator,
    ) -> _StripeListPage:
        response = self._request(path=path, params=params)
        payload: object = None
        malformed_json = False
        try:
            payload = response.json()
        except ValueError:
            malformed_json = True
        if malformed_json:
            raise _provider_error("invalid_provider_response")
        return self._validated_page(payload=payload, path=path, validator=validator)

    def _request(
        self,
        *,
        path: str,
        params: tuple[tuple[str, str], ...],
    ) -> httpx.Response:
        response: httpx.Response | None = None
        failure: AcquisitionProviderError | None = None
        try:
            response = self._http_client.get(
                "https://api.stripe.com" + path,
                params=params,
                headers={
                    "Authorization": "Bearer " + self._api_key.get_secret_value(),
                    "Stripe-Version": STRIPE_API_VERSION,
                },
            )
        except httpx.TimeoutException:
            failure = _provider_error("provider_unavailable")
        except httpx.TransportError:
            failure = _provider_error("transport_failure")
        except Exception:
            failure = _provider_error("integrity_failure")
        if failure is not None:
            raise failure
        if response is None:
            raise _provider_error("integrity_failure")
        if response.is_success:
            return response
        if response.status_code == 429:
            raise _provider_error("rate_limited")
        if response.status_code in {401, 403}:
            raise _provider_error("authorization_denied")
        if response.status_code == 408 or response.status_code >= 500:
            raise _provider_error("provider_unavailable")
        if 400 <= response.status_code < 500:
            raise _provider_error("statement_rejected")
        raise _provider_error("invalid_provider_response")

    @staticmethod
    def _validated_page(
        *,
        payload: object,
        path: str,
        validator: _PageValidator,
    ) -> _StripeListPage:
        if not isinstance(payload, dict) or set(payload) != _PAGE_KEYS:
            raise _provider_error("invalid_provider_response")
        if payload.get("object") != "list" or payload.get("url") != path:
            raise _provider_error("invalid_provider_response")
        has_more = payload.get("has_more")
        data = payload.get("data")
        if type(has_more) is not bool or not isinstance(data, list):
            raise _provider_error("invalid_provider_response")
        validated = tuple(validator(candidate) for candidate in data)
        if has_more and not validated:
            raise _provider_error("invalid_provider_response")
        next_cursor = None
        if has_more:
            candidate_cursor = validated[-1].get("id")
            if not isinstance(candidate_cursor, str) or not candidate_cursor:
                raise _provider_error("invalid_provider_response")
            next_cursor = candidate_cursor
        return _StripeListPage(validated, has_more, next_cursor)

    def _validated_resource(
        self,
        candidate: object,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Mapping[str, object]:
        if not isinstance(candidate, dict):
            raise _provider_error("invalid_provider_response")
        object_id = candidate.get("id")
        created = candidate.get("created")
        livemode = candidate.get("livemode")
        if (
            not isinstance(object_id, str)
            or not object_id
            or candidate.get("object") != object_kind
            or type(created) is not int
            or created < 0
            or created > created_lte
            or type(livemode) is not bool
            or livemode is not (self._account_mode == "live")
        ):
            raise _provider_error("invalid_provider_response")
        for field_name, value in candidate.items():
            if isinstance(value, Mapping):
                if field_name not in _ALLOWED_NESTED_FIELDS or "object" in value:
                    raise _provider_error("invalid_provider_response")
            elif isinstance(value, list):
                raise _provider_error("invalid_provider_response")
        return MappingProxyType(dict(candidate))

    def _validated_event(
        self,
        candidate: object,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Mapping[str, object]:
        if not isinstance(candidate, dict):
            raise _provider_error("invalid_provider_response")
        event_id = candidate.get("id")
        created = candidate.get("created")
        livemode = candidate.get("livemode")
        event_type = candidate.get("type")
        if (
            not isinstance(event_id, str)
            or not event_id
            or candidate.get("object") != "event"
            or type(created) is not int
            or not created_gte <= created <= created_lte
            or type(livemode) is not bool
            or livemode is not (self._account_mode == "live")
            or event_type not in event_types
            or not isinstance(candidate.get("data"), Mapping)
        ):
            raise _provider_error("invalid_provider_response")
        return MappingProxyType(dict(candidate))


def _require_epoch_bound(value: int, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"Stripe {field_name} must be a non-negative integer")


def _provider_error(reason_code: AcquisitionProviderReasonCode) -> AcquisitionProviderError:
    classifications: dict[AcquisitionProviderReasonCode, AcquisitionProviderErrorClassification] = {
        "authorization_denied": "authorization_denied",
        "integrity_failure": "integrity_failure",
        "invalid_provider_response": "invalid_provider_response",
        "provider_unavailable": "transient_unavailable",
        "rate_limited": "throttled",
        "statement_rejected": "statement_rejected",
        "transport_failure": "transient_transport",
    }
    return AcquisitionProviderError(
        provider_kind="stripe",
        classification=classifications[reason_code],
        reason_code=reason_code,
    )
