from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from pillarmesh_provider_sdk import (
    DestinationProvider,
    IdempotencyKey,
    ProviderError,
    RawGenerationTarget,
    StagedSegment,
)
from pillarmesh_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehousePersistenceError,
)

from .generation_ledger import GenerationLedger
from .landing import LandingFaultHook, LandingResult, LandingRunner


def _clock() -> datetime:
    return datetime.now(UTC)


def _noop_fault_hook(checkpoint: str) -> None:
    del checkpoint


class DestinationBindingAuthority(Protocol):
    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None: ...


type DestinationProviderFactory = Callable[[WarehouseBinding], DestinationProvider]


class DestinationLandingRuntime:
    """Resolve ready warehouse authority before constructing a LAND provider."""

    def __init__(
        self,
        *,
        binding_authority: DestinationBindingAuthority,
        provider_factories: Mapping[EngineKind, DestinationProviderFactory],
        ledger: GenerationLedger,
        clock: Callable[[], datetime] = _clock,
        fault_hook: LandingFaultHook = _noop_fault_hook,
    ) -> None:
        self._binding_authority = binding_authority
        self._provider_factories = dict(provider_factories)
        self._ledger = ledger
        self._clock = clock
        self._fault_hook = fault_hook

    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
        batch_id: str,
        batch_manifest_digest: str,
        candidate_checkpoint_digest: str,
        prior_checkpoint_revision: int,
        contract_digest: str,
        source_binding_ref: str,
        consumer_ref: str,
    ) -> LandingResult:
        try:
            binding = self._binding_authority.load(
                target.tenant_id,
                target.destination_binding_ref,
            )
        except WarehousePersistenceError:
            raise ProviderError(
                "destination binding authority is unavailable",
                "transient_unavailable",
            ) from None
        except Exception:
            raise ProviderError(
                "destination binding authority returned invalid state",
                "integrity_failure",
            ) from None
        if (
            binding is None
            or binding.tenant_id != target.tenant_id
            or binding.binding_id != target.destination_binding_ref
            or binding.lifecycle_state is not WarehouseBindingState.READY
        ):
            raise ProviderError(
                "destination binding is not authorized for LAND",
                "authorization_denied",
            )
        factory = self._provider_factories.get(binding.engine_kind)
        if factory is None:
            raise ProviderError(
                "destination binding provider is unavailable",
                "permanent_configuration",
            )
        try:
            provider = factory(binding)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                "destination provider capability resolution failed",
                "authorization_denied",
            ) from None
        if provider.provider_kind != binding.engine_kind.value:
            raise ProviderError(
                "destination binding provider mismatch",
                "authorization_denied",
            )
        runner = LandingRunner(
            provider=provider,
            ledger=self._ledger,
            clock=self._clock,
            fault_hook=self._fault_hook,
        )
        return await runner.land(
            segment=segment,
            target=target,
            idempotency_key=idempotency_key,
            batch_id=batch_id,
            batch_manifest_digest=batch_manifest_digest,
            candidate_checkpoint_digest=candidate_checkpoint_digest,
            prior_checkpoint_revision=prior_checkpoint_revision,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            consumer_ref=consumer_ref,
        )
