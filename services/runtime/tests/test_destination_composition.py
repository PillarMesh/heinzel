from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

import pytest
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    CommitObservation,
    DestinationProviderKind,
    LandReceipt,
    ProviderError,
    raw_generation_key,
)
from pillarmesh_provider_sdk.destination_conformance import destination_segment, destination_target
from pillarmesh_runtime import DestinationLandingRuntime, GenerationLedger, LandingResult
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehousePersistenceError,
)

_NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


def _binding(*, engine_kind: EngineKind = EngineKind.POSTGRESQL) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="warehouse-a",
        tenant_id="tenant-a",
        engine_kind=engine_kind,
        region="local",
        capability_profile_digest="1" * 64,
        lifecycle_state=WarehouseBindingState.READY,
        revision=4,
        created_at=_NOW,
        updated_at=_NOW,
        provisioned_at=_NOW,
    )


class _BindingAuthority:
    def __init__(self, binding: WarehouseBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if (tenant_id, binding_id) != (self._binding.tenant_id, self._binding.binding_id):
            return None
        return self._binding


class _Provider:
    def __init__(
        self,
        *,
        provider_kind: DestinationProviderKind = "postgresql",
        first_observation: Literal["committed", "not_found"] = "committed",
    ) -> None:
        self.provider_kind = provider_kind
        self._first_observation = first_observation
        self._receipt: LandReceipt | None = None
        self.land_count = 0
        self.inspect_count = 0

    async def land(self, *, segment: object, target: object, idempotency_key: str) -> LandReceipt:
        self.land_count += 1
        typed_segment = destination_segment().model_validate(segment)
        typed_target = destination_target().model_validate(target)
        if self._receipt is None:
            self._receipt = LandReceipt(
                receipt_id="receipt-a",
                idempotency_key=idempotency_key,
                tenant_id=typed_target.tenant_id,
                contract_ref=typed_target.contract_ref,
                contract_revision=typed_target.contract_revision,
                trigger_window=typed_target.trigger_window,
                destination_binding_ref=typed_target.destination_binding_ref,
                logical_object_ref=typed_target.logical_object_ref,
                target_table_ref=typed_target.table_ref,
                generation_id=raw_generation_key(
                    target=typed_target,
                    segment_digest=typed_segment.segment_digest,
                ),
                segment_digest=typed_segment.segment_digest,
                schema_digest=typed_segment.schema_digest,
                record_count=typed_segment.record_count,
                provider_commit_ref="provider-commit-a",
                committed_at=_NOW,
            )
        return self._receipt

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation:
        self.inspect_count += 1
        outcome = self._first_observation if self.inspect_count == 1 else "committed"
        return CommitObservation(
            outcome=outcome,
            receipt_digest=digest(receipt) if outcome == "committed" else None,
            observed_at=_NOW,
        )


class _WrongTableProvider(_Provider):
    async def land(self, *, segment: object, target: object, idempotency_key: str) -> LandReceipt:
        receipt = await super().land(
            segment=segment,
            target=target,
            idempotency_key=idempotency_key,
        )
        return receipt.model_copy(update={"target_table_ref": "somebody_elses_table"})


async def _land(runtime: DestinationLandingRuntime) -> LandingResult:
    return await runtime.land(
        segment=destination_segment(),
        target=destination_target(),
        idempotency_key="b" * 64,
        batch_id="c" * 64,
        batch_manifest_digest="d" * 64,
        candidate_checkpoint_digest="e" * 64,
        prior_checkpoint_revision=0,
        contract_digest="f" * 64,
        source_binding_ref="source-a",
        consumer_ref="runtime-land",
    )


def test_destination_binding_selects_a_provider_and_replays_the_terminal_land() -> None:
    provider = _Provider()
    runtime = DestinationLandingRuntime(
        binding_authority=_BindingAuthority(_binding()),
        provider_factories={EngineKind.POSTGRESQL: lambda _binding: provider},
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )

    first = asyncio.run(_land(runtime))
    replay = asyncio.run(_land(runtime))

    assert replay == first
    assert provider.land_count == 1


def test_destination_binding_rejects_a_factory_for_the_wrong_provider() -> None:
    provider = _Provider(provider_kind="clickhouse")
    runtime = DestinationLandingRuntime(
        binding_authority=_BindingAuthority(_binding()),
        provider_factories={EngineKind.POSTGRESQL: lambda _binding: provider},
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(_land(runtime))

    assert captured.value.classification == "authorization_denied"
    assert provider.land_count == 0


def test_destination_binding_recovers_an_ambiguous_commit_on_replay() -> None:
    provider = _Provider(first_observation="not_found")
    runtime = DestinationLandingRuntime(
        binding_authority=_BindingAuthority(_binding()),
        provider_factories={EngineKind.POSTGRESQL: lambda _binding: provider},
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(_land(runtime))
    recovered = asyncio.run(_land(runtime))

    assert captured.value.classification == "ambiguous_outcome"
    assert recovered.receipt.provider_commit_ref == "provider-commit-a"
    assert provider.land_count == 2


def test_destination_binding_rejects_a_receipt_for_a_different_target_table() -> None:
    runtime = DestinationLandingRuntime(
        binding_authority=_BindingAuthority(_binding()),
        provider_factories={EngineKind.POSTGRESQL: lambda _binding: _WrongTableProvider()},
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(_land(runtime))

    assert captured.value.classification == "integrity_failure"


def test_destination_binding_store_unavailability_remains_transient() -> None:
    class UnavailableAuthority:
        def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
            del tenant_id, binding_id
            raise WarehousePersistenceError("private database path")

    runtime = DestinationLandingRuntime(
        binding_authority=UnavailableAuthority(),
        provider_factories={},
        ledger=GenerationLedger.in_memory(),
        clock=lambda: _NOW,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(_land(runtime))

    assert captured.value.classification == "transient_unavailable"
    assert captured.value.__cause__ is None
    assert "private database path" not in str(captured.value)
