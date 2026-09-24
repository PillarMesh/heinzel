from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionProviderError,
    ColumnObservation,
    SourceObservationRequest,
)
from heinzel_provider_stripe import StripeAcquisitionProvider, StripeClient, StripeSettings
from heinzel_provider_stripe.settings import StripeObjectKind

from providers.stripe.tests.test_settings import _settings_values

_NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
_UPPER_EPOCH = int(_NOW.timestamp())
_OBJECT_REFS = ("charges", "customers", "invoices", "refunds")
_OBJECT_KINDS: tuple[StripeObjectKind, ...] = ("charge", "customer", "invoice", "refund")
_PRIVATE_CANARIES = (
    "private-stripe-observation-body",
    "private-stripe-observation-request-id",
    "private-stripe-observation-cleanup",
)

type _Page = tuple[Mapping[str, object], ...]
type _ProbeStep = _Page | BaseException


class _TrackedProbeIterator:
    def __init__(
        self,
        steps: tuple[_ProbeStep, ...],
        *,
        close_failure: BaseException | None = None,
    ) -> None:
        self._steps = steps
        self._close_failure = close_failure
        self._position = 0
        self.closed = False

    def __iter__(self) -> _TrackedProbeIterator:
        return self

    def __next__(self) -> _Page:
        if self._position == len(self._steps):
            raise StopIteration
        step = self._steps[self._position]
        self._position += 1
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self) -> None:
        self.closed = True
        if self._close_failure is not None:
            raise self._close_failure


class FakeStripeObservationClient:
    def __init__(
        self,
        *,
        object_steps: Mapping[StripeObjectKind, tuple[_ProbeStep, ...]] | None = None,
        event_steps: tuple[_ProbeStep, ...] = ((),),
        close_failure: BaseException | None = None,
    ) -> None:
        self._object_steps = object_steps or {object_kind: ((),) for object_kind in _OBJECT_KINDS}
        self._event_steps = event_steps
        self._close_failure = close_failure
        self.object_queries: list[tuple[StripeObjectKind, int]] = []
        self.event_queries: list[tuple[int, int, tuple[str, ...]]] = []
        self.iterators: list[_TrackedProbeIterator] = []

    def iter_object_pages(
        self,
        *,
        object_kind: StripeObjectKind,
        created_lte: int,
    ) -> Iterator[_Page]:
        self.object_queries.append((object_kind, created_lte))
        iterator = _TrackedProbeIterator(
            self._object_steps[object_kind],
            close_failure=self._close_failure,
        )
        self.iterators.append(iterator)
        return iterator

    def iter_event_pages(
        self,
        *,
        created_gte: int,
        created_lte: int,
        event_types: tuple[str, ...],
    ) -> Iterator[_Page]:
        self.event_queries.append((created_gte, created_lte, event_types))
        iterator = _TrackedProbeIterator(
            self._event_steps,
            close_failure=self._close_failure,
        )
        self.iterators.append(iterator)
        return iterator


def _settings() -> StripeSettings:
    return StripeSettings.model_validate(_settings_values())


def _provider(client: FakeStripeObservationClient) -> StripeAcquisitionProvider:
    typed_client: StripeClient = client
    return StripeAcquisitionProvider(
        _settings(),
        client=typed_client,
        clock=lambda: _NOW,
        private_boundary_reference_factory=lambda tenant_id, object_ref: (
            f"private:{tenant_id}:{object_ref}"
        ),
        private_boundary_writer=lambda _tenant_id, _reference, _payload: None,
    )


def _request() -> SourceObservationRequest:
    return SourceObservationRequest(
        tenant_id="tenant-a",
        source_binding_ref="stripe-binding-a",
        object_refs=_OBJECT_REFS,
    )


def _assert_sanitized(error: AcquisitionProviderError) -> None:
    public_surface = " ".join((str(error), repr(error), repr(error.args)))
    for canary in _PRIVATE_CANARIES:
        assert canary not in public_surface
    assert error.__cause__ is None
    assert error.__context__ is None


def test_observation_probes_every_admitted_read_capability_before_advertising_it() -> None:
    client = FakeStripeObservationClient()
    settings = _settings()
    provider = _provider(client)

    observation = provider.observe_source(_request())

    assert client.object_queries == [
        ("charge", _UPPER_EPOCH),
        ("customer", _UPPER_EPOCH),
        ("invoice", _UPPER_EPOCH),
        ("refund", _UPPER_EPOCH),
    ]
    assert client.event_queries == [(_UPPER_EPOCH, _UPPER_EPOCH, settings.event_types)]
    assert len(client.iterators) == 5
    assert all(iterator.closed for iterator in client.iterators)
    assert observation.tenant_id == "tenant-a"
    assert observation.source_binding_ref == "stripe-binding-a"
    assert observation.provider_kind == "stripe"
    assert (
        tuple(item.logical_object_ref for item in observation.object_observations) == _OBJECT_REFS
    )
    for item, declaration in zip(
        observation.object_observations,
        settings.objects,
        strict=True,
    ):
        provider_observation = item.provider_observation
        assert provider_observation.schema_digest == digest(declaration.normalized_fields)
        assert provider_observation.columns == tuple(
            ColumnObservation(
                name=field.name,
                type_name=field.value_type.upper(),
                nullable=field.nullable,
            )
            for field in declaration.normalized_fields
        )
        assert provider_observation.object_identity == digest(
            {
                "connection_handle": settings.connection_handle,
                "account_mode": settings.account_mode,
                "api_version": settings.api_version,
                "supported_creation_versions": settings.supported_creation_versions,
                "logical_object_ref": declaration.logical_object_ref,
                "object_kind": declaration.object_kind,
            }
        )
        assert provider_observation.capabilities == (
            "incremental",
            "reconciliation",
            "snapshot",
        )
        assert provider_observation.read_only is True
        assert provider_observation.evidence_safe is True


@pytest.mark.parametrize("failed_probe", ("charge", "events"))
def test_observation_propagates_typed_probe_failure_and_suppresses_cleanup_failure(
    failed_probe: str,
) -> None:
    provider_failure = AcquisitionProviderError(
        provider_kind="stripe",
        classification="authorization_denied",
        reason_code="authorization_denied",
    )
    object_steps: dict[StripeObjectKind, tuple[_ProbeStep, ...]] = {
        object_kind: ((),) for object_kind in _OBJECT_KINDS
    }
    event_steps: tuple[_ProbeStep, ...] = ((),)
    if failed_probe == "events":
        event_steps = (provider_failure,)
    else:
        object_steps["charge"] = (provider_failure,)
    client = FakeStripeObservationClient(
        object_steps=object_steps,
        event_steps=event_steps,
        close_failure=RuntimeError(_PRIVATE_CANARIES[2]),
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _provider(client).observe_source(_request())

    assert captured.value.classification == "authorization_denied"
    assert captured.value.reason_code == "authorization_denied"
    assert client.iterators[-1].closed is True
    _assert_sanitized(captured.value)


@pytest.mark.parametrize("failed_probe", ("customer", "events"))
def test_observation_translates_unclassified_probe_failure_to_sanitized_integrity(
    failed_probe: str,
) -> None:
    diagnostic = " ".join(_PRIVATE_CANARIES)
    object_steps: dict[StripeObjectKind, tuple[_ProbeStep, ...]] = {
        object_kind: ((),) for object_kind in _OBJECT_KINDS
    }
    event_steps: tuple[_ProbeStep, ...] = ((),)
    if failed_probe == "events":
        event_steps = (RuntimeError(diagnostic),)
    else:
        object_steps["customer"] = (RuntimeError(diagnostic),)
    client = FakeStripeObservationClient(
        object_steps=object_steps,
        event_steps=event_steps,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _provider(client).observe_source(_request())

    assert captured.value.classification == "integrity_failure"
    assert captured.value.reason_code == "integrity_failure"
    assert client.iterators[-1].closed is True
    _assert_sanitized(captured.value)


@pytest.mark.parametrize("empty_probe", ("refund", "events"))
def test_observation_refuses_a_probe_that_returns_no_page(empty_probe: str) -> None:
    object_steps: dict[StripeObjectKind, tuple[_ProbeStep, ...]] = {
        object_kind: ((),) for object_kind in _OBJECT_KINDS
    }
    event_steps: tuple[_ProbeStep, ...] = ((),)
    if empty_probe == "events":
        event_steps = ()
    else:
        object_steps["refund"] = ()
    client = FakeStripeObservationClient(
        object_steps=object_steps,
        event_steps=event_steps,
    )

    with pytest.raises(AcquisitionProviderError) as captured:
        _provider(client).observe_source(_request())

    assert captured.value.classification == "invalid_provider_response"
    assert captured.value.reason_code == "invalid_provider_response"
    assert client.iterators[-1].closed is True
    _assert_sanitized(captured.value)
