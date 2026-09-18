from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import (
    AnswerQueryCursor,
    AnswerQueryProviderResolver,
    ReadOnlyAnswerQuery,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState


class _Provider:
    def __init__(self, engine_kind: Literal["postgresql", "clickhouse"]) -> None:
        self.engine_kind = engine_kind

    def execute_read_only(self, request: ReadOnlyAnswerQuery) -> AnswerQueryCursor:
        raise AssertionError(request)


def _binding(
    *,
    engine_kind: EngineKind = EngineKind.POSTGRESQL,
    lifecycle_state: WarehouseBindingState = WarehouseBindingState.READY,
) -> WarehouseBinding:
    return WarehouseBinding(
        binding_id="warehouse-1",
        tenant_id="tenant-a",
        engine_kind=engine_kind,
        region="local",
        capability_profile_digest="b" * 64,
        lifecycle_state=lifecycle_state,
        revision=3,
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
        updated_at=datetime(2026, 9, 11, tzinfo=UTC),
        provisioned_at=datetime(2026, 9, 11, tzinfo=UTC),
    )


def test_answer_provider_resolver_uses_ready_tenant_binding_and_matching_factory() -> None:
    binding = _binding()

    class Authority:
        def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
            assert (tenant_id, binding_id) == ("tenant-a", "warehouse-1")
            return binding

    resolver = AnswerQueryProviderResolver(
        tenant_id="tenant-a",
        binding_id="warehouse-1",
        binding_authority=Authority(),
        provider_factories={EngineKind.POSTGRESQL: lambda _resolved: _Provider("postgresql")},
    )

    provider = resolver("postgresql")

    assert provider.engine_kind == "postgresql"


@pytest.mark.parametrize(
    "binding",
    (
        _binding(lifecycle_state=WarehouseBindingState.SUSPENDED),
        _binding(engine_kind=EngineKind.CLICKHOUSE),
    ),
)
def test_answer_provider_resolver_denies_nonready_or_engine_mismatched_binding(
    binding: WarehouseBinding,
) -> None:
    class Authority:
        def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
            del tenant_id, binding_id
            return binding

    resolver = AnswerQueryProviderResolver(
        tenant_id="tenant-a",
        binding_id="warehouse-1",
        binding_authority=Authority(),
        provider_factories={EngineKind.POSTGRESQL: lambda _resolved: _Provider("postgresql")},
    )

    with pytest.raises(ProviderError) as captured:
        resolver("postgresql")

    assert captured.value.classification == "authorization_denied"


def test_answer_provider_resolver_rejects_factory_engine_substitution() -> None:
    class Authority:
        def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
            del tenant_id, binding_id
            return _binding()

    resolver = AnswerQueryProviderResolver(
        tenant_id="tenant-a",
        binding_id="warehouse-1",
        binding_authority=Authority(),
        provider_factories={EngineKind.POSTGRESQL: lambda _binding: _Provider("clickhouse")},
    )

    with pytest.raises(ProviderError) as captured:
        resolver("postgresql")

    assert captured.value.classification == "authorization_denied"
