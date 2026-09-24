from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread
from typing import cast

import pytest
from heinzel_warehouse_control import (
    EngineKind,
    WarehouseBinding,
    WarehouseBindingState,
    WarehouseControlService,
)
from heinzel_warehouse_control.repository import SQLiteWarehouseRepository
from pydantic import ValidationError

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def service() -> WarehouseControlService:
    return WarehouseControlService(SQLiteWarehouseRepository(":memory:"), clock=lambda: NOW)


def draft(control: WarehouseControlService, tenant_id: str = "tenant-a") -> WarehouseBinding:
    return control.create_draft(
        tenant_id=tenant_id,
        engine_kind=EngineKind.POSTGRESQL,
        region="us-west",
        capacity_profile="mvp-fixed",
    )


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (WarehouseBindingState.VALIDATING, WarehouseBindingState.READY),
        (WarehouseBindingState.SUSPENDED, WarehouseBindingState.READY),
        (WarehouseBindingState.RETIRING, WarehouseBindingState.RETIRED),
        (WarehouseBindingState.FAILED, WarehouseBindingState.RETIRED),
    ),
)
def test_plain_transition_refuses_evidence_gated_state(
    source: WarehouseBindingState, target: WarehouseBindingState
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=lambda: NOW)
    warehouse_binding = draft(control)
    source_binding = WarehouseBinding.model_validate(
        {
            **warehouse_binding.model_dump(),
            "lifecycle_state": source,
            "revision": warehouse_binding.revision + 1,
        }
    )
    repository.save(source_binding)

    with pytest.raises(
        ValueError,
        match=f"transition {source.value} -> {target.value} is not allowed",
    ):
        control.transition(
            source_binding.tenant_id,
            source_binding.binding_id,
            target,
            expected_revision=source_binding.revision,
        )


@pytest.mark.parametrize(
    "source",
    (WarehouseBindingState.PROVISIONING, WarehouseBindingState.VALIDATING),
)
def test_plain_transition_refuses_unclassified_terminal_failure(
    source: WarehouseBindingState,
) -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=lambda: NOW)
    warehouse_binding = draft(control)
    source_binding = WarehouseBinding.model_validate(
        {
            **warehouse_binding.model_dump(),
            "lifecycle_state": source,
            "revision": warehouse_binding.revision + 1,
        }
    )
    repository.save(source_binding)

    with pytest.raises(
        ValueError,
        match=f"transition {source.value} -> failed is not allowed",
    ):
        control.transition(
            source_binding.tenant_id,
            source_binding.binding_id,
            WarehouseBindingState.FAILED,
            expected_revision=source_binding.revision,
        )


def test_binding_becomes_immutable_when_provisioning_starts() -> None:
    control = service()
    binding = draft(control)

    provisioning = control.transition(
        "tenant-a",
        binding.binding_id,
        WarehouseBindingState.PROVISIONING,
        expected_revision=binding.revision,
    )

    assert provisioning.engine_kind is EngineKind.POSTGRESQL
    with pytest.raises(ValueError, match="immutable after draft"):
        control.revise_draft(
            "tenant-a",
            provisioning.binding_id,
            engine_kind=EngineKind.CLICKHOUSE,
            expected_revision=provisioning.revision,
        )


def test_plain_nonterminal_transition_preserves_the_first_provisioned_time() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=lambda: NOW)
    warehouse_binding = draft(control)
    ready = WarehouseBinding.model_validate(
        {
            **warehouse_binding.model_dump(),
            "lifecycle_state": WarehouseBindingState.READY,
            "revision": warehouse_binding.revision + 1,
            "provisioned_at": NOW - timedelta(days=1),
        }
    )
    repository.save(ready)

    retiring = control.transition(
        ready.tenant_id,
        ready.binding_id,
        WarehouseBindingState.RETIRING,
        expected_revision=ready.revision,
    )

    assert retiring.provisioned_at == ready.provisioned_at


def test_ready_cannot_transition_directly_to_retired() -> None:
    repository = SQLiteWarehouseRepository(":memory:")
    control = WarehouseControlService(repository, clock=lambda: NOW)
    binding = control.create_draft(
        tenant_id="tenant-a",
        engine_kind=EngineKind.CLICKHOUSE,
        region="us-west",
        capacity_profile="mvp-fixed",
    )
    binding = WarehouseBinding.model_validate(
        {
            **binding.model_dump(),
            "lifecycle_state": WarehouseBindingState.READY,
            "revision": binding.revision + 1,
            "provisioned_at": NOW,
        }
    )
    repository.save(binding)

    with pytest.raises(ValueError, match="transition ready -> retired is not allowed"):
        control.transition(
            "tenant-a",
            binding.binding_id,
            WarehouseBindingState.RETIRED,
            expected_revision=binding.revision,
        )


def test_one_tenant_cannot_read_or_move_another_tenants_binding() -> None:
    control = service()
    binding = draft(control, tenant_id="tenant-a")

    with pytest.raises(KeyError, match="warehouse binding was not found"):
        control.get("tenant-b", binding.binding_id)
    with pytest.raises(KeyError, match="warehouse binding was not found"):
        control.transition(
            "tenant-b",
            binding.binding_id,
            WarehouseBindingState.PROVISIONING,
            expected_revision=binding.revision,
        )
    assert control.get("tenant-a", binding.binding_id).lifecycle_state is (
        WarehouseBindingState.DRAFT
    )


def test_two_drafts_for_one_tenant_receive_distinct_identities() -> None:
    # Once provisioning begins a binding's tenant, engine, deployment mode and region are
    # immutable, so a migration must create a NEW binding for the same tenant. Under the
    # frozen clock these are created in the same instant.
    control = service()

    first = draft(control)
    second = draft(control)

    assert first.binding_id != second.binding_id
    assert control.get("tenant-a", first.binding_id).binding_id == first.binding_id


def test_stale_revision_loses_the_transition() -> None:
    control = service()
    binding = draft(control)
    control.transition(
        "tenant-a",
        binding.binding_id,
        WarehouseBindingState.PROVISIONING,
        expected_revision=binding.revision,
    )

    with pytest.raises(ValueError, match="binding revision is stale"):
        control.transition(
            "tenant-a",
            binding.binding_id,
            WarehouseBindingState.FAILED,
            expected_revision=binding.revision,
        )


@pytest.mark.parametrize(
    "clock_value",
    (
        datetime(2026, 8, 17, 12),
        datetime(2026, 8, 17, 12, tzinfo=timezone(timedelta(hours=1))),
    ),
)
def test_create_draft_rejects_non_utc_clock(clock_value: datetime) -> None:
    control = WarehouseControlService(
        SQLiteWarehouseRepository(":memory:"), clock=lambda: clock_value
    )

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        draft(control)


def test_revise_draft_rejects_invalid_runtime_engine_before_persisting() -> None:
    control = service()
    binding = draft(control)

    with pytest.raises(ValidationError, match="engine_kind"):
        control.revise_draft(
            "tenant-a",
            binding.binding_id,
            engine_kind=cast(EngineKind, "invalid-engine"),
            expected_revision=binding.revision,
        )

    persisted = control.get("tenant-a", binding.binding_id)
    assert persisted.engine_kind is EngineKind.POSTGRESQL
    assert persisted.revision == 1


def test_two_connection_transition_race_returns_stale_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "warehouse.db"
    initializer = WarehouseControlService(
        SQLiteWarehouseRepository(str(database_path)), clock=lambda: NOW
    )
    binding = draft(initializer)
    barrier = Barrier(2)
    outcomes: Queue[WarehouseBinding | Exception] = Queue()

    def synchronized_clock() -> datetime:
        barrier.wait(timeout=5)
        return NOW

    def transition() -> None:
        control = WarehouseControlService(
            SQLiteWarehouseRepository(str(database_path)), clock=synchronized_clock
        )
        try:
            outcomes.put(
                control.transition(
                    "tenant-a",
                    binding.binding_id,
                    WarehouseBindingState.PROVISIONING,
                    expected_revision=binding.revision,
                )
            )
        except Exception as error:
            outcomes.put(error)

    first = Thread(target=transition)
    second = Thread(target=transition)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    results = [outcomes.get_nowait() for _ in range(2)]
    assert sum(isinstance(result, WarehouseBinding) for result in results) == 1
    stale_errors = [
        result
        for result in results
        if isinstance(result, ValueError) and str(result) == "binding revision is stale"
    ]
    assert len(stale_errors) == 1


def test_binding_lifecycle_transition_table_is_exact_and_retired_is_terminal() -> None:
    """Every binding state has exactly these successors; a ready binding drains via retiring."""
    from heinzel_warehouse_control.service import _TRANSITIONS

    assert set(_TRANSITIONS) == set(WarehouseBindingState)
    assert {
        source.value: frozenset(target.value for target in targets)
        for source, targets in _TRANSITIONS.items()
    } == {
        "draft": frozenset({"provisioning", "retired"}),
        "provisioning": frozenset({"validating", "failed"}),
        "validating": frozenset({"ready", "failed"}),
        "ready": frozenset({"suspended", "retiring"}),
        "suspended": frozenset({"ready", "retiring"}),
        "retiring": frozenset({"retired"}),
        "failed": frozenset({"retired"}),
        "retired": frozenset(),
    }


def test_plain_transitions_are_exact_and_never_enter_ready_or_retired() -> None:
    """Plain transitions never produce ready, retired, failed or suspended states."""
    from heinzel_warehouse_control.service import _PLAIN_TRANSITIONS, _TRANSITIONS

    assert set(_PLAIN_TRANSITIONS) == set(WarehouseBindingState)
    assert {
        source.value: frozenset(target.value for target in targets)
        for source, targets in _PLAIN_TRANSITIONS.items()
    } == {
        "draft": frozenset({"provisioning"}),
        "provisioning": frozenset({"validating"}),
        "validating": frozenset(),
        "ready": frozenset({"retiring"}),
        "suspended": frozenset({"retiring"}),
        "retiring": frozenset(),
        "failed": frozenset(),
        "retired": frozenset(),
    }
    assert all(targets <= _TRANSITIONS[source] for source, targets in _PLAIN_TRANSITIONS.items())
