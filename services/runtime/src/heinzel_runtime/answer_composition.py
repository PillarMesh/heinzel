from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from heinzel_provider_sdk import ProviderError
from heinzel_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehousePersistenceError,
)

from .query_execution import AnswerQueryProvider


class AnswerQueryBindingAuthority(Protocol):
    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None: ...


type AnswerQueryProviderFactory = Callable[[WarehouseBinding], AnswerQueryProvider]


class AnswerQueryProviderResolver:
    """Bind one tenant runtime to its ready warehouse and engine-specific query capability."""

    def __init__(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        binding_authority: AnswerQueryBindingAuthority,
        provider_factories: Mapping[EngineKind, AnswerQueryProviderFactory],
    ) -> None:
        self._tenant_id = tenant_id
        self._binding_id = binding_id
        self._binding_authority = binding_authority
        self._provider_factories = dict(provider_factories)

    def __call__(self, engine_kind: str) -> AnswerQueryProvider:
        try:
            expected_engine = EngineKind(engine_kind)
        except ValueError:
            raise ProviderError(
                "answer query engine is not supported", "permanent_configuration"
            ) from None
        try:
            binding = self._binding_authority.load(self._tenant_id, self._binding_id)
        except WarehousePersistenceError:
            raise ProviderError(
                "answer query binding authority is unavailable", "transient_unavailable"
            ) from None
        except Exception:
            raise ProviderError(
                "answer query binding authority returned invalid state", "integrity_failure"
            ) from None
        if (
            binding is None
            or binding.tenant_id != self._tenant_id
            or binding.binding_id != self._binding_id
            or binding.lifecycle_state is not WarehouseBindingState.READY
            or binding.engine_kind is not expected_engine
        ):
            raise ProviderError("answer query binding is not authorized", "authorization_denied")
        factory = self._provider_factories.get(expected_engine)
        if factory is None:
            raise ProviderError("answer query provider is unavailable", "permanent_configuration")
        try:
            provider = factory(binding)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                "answer query provider capability resolution failed", "authorization_denied"
            ) from None
        if provider.engine_kind != expected_engine.value:
            raise ProviderError("answer query provider engine mismatch", "authorization_denied")
        return provider
