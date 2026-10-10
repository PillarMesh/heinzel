"""The broker the demonstration composes, and what it offers before anything is registered."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from heinzel_connection_broker import SourceConnectionBindingState
from heinzel_console.demo.generation import DEMO_LOGICAL_OBJECT, DEMO_SOURCE_CONNECTION_HANDLE
from heinzel_console.demo.publication import DEMO_TENANT_ID
from heinzel_console.demo.source_registry import (
    SOURCE_BINDING_DATABASE_FILENAME,
    DemoSourceRegistry,
    open_demo_source_registry,
)
from heinzel_console.governed_adapters import EnrolledSourceConnectionReader

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
DSN = "postgresql://acquisition_runtime:derived-password@127.0.0.1:5432/warehouse"


def _clock() -> datetime:
    return NOW


def _registry(state_dir: Path, *, dsn: str = DSN) -> DemoSourceRegistry:
    return open_demo_source_registry(state_dir, acquisition_dsn=dsn, clock=_clock)


def _reader(registry: DemoSourceRegistry) -> EnrolledSourceConnectionReader:
    """The offering, seen as the console backend sees it.

    The annotation is the check: it makes the type checker answer for this class satisfying the
    protocol `GovernedConsoleBackend` takes, rather than a comment claiming it does.
    """
    return registry.enrolled


def test_opening_the_registry_enrols_the_demonstrations_own_source(tmp_path: Path) -> None:
    """One handle offered, under the declaration the acquisition reads the same source by."""
    registry = _registry(tmp_path)

    offered = _reader(registry).list_enrolled_source_connections(DEMO_TENANT_ID)

    assert [item.connection_handle for item in offered] == [DEMO_SOURCE_CONNECTION_HANDLE]
    (connection,) = offered
    assert connection.provider_kind == "postgresql"
    assert connection.account_mode == "not_applicable"
    assert connection.declared_object_refs == (DEMO_LOGICAL_OBJECT,)
    registry.close()


def test_nothing_is_offered_to_another_tenant(tmp_path: Path) -> None:
    """The demonstration has one tenant, and the offering is scoped to it rather than global."""
    registry = _registry(tmp_path)

    assert _reader(registry).list_enrolled_source_connections("tenant-other") == ()
    registry.close()


def test_a_second_start_over_one_state_directory_offers_the_same_one_handle(
    tmp_path: Path,
) -> None:
    """Enrolment is immutable per handle, so re-opening must neither duplicate nor refuse.

    This is the property that was missing while the role passwords were minted per start: the
    same handle arrived with a different detail and the store refused it, which is why the
    demonstration enrolled nothing at all.
    """
    first = _registry(tmp_path)
    first.close()

    second = _registry(tmp_path)

    assert [
        item.connection_handle
        for item in _reader(second).list_enrolled_source_connections(DEMO_TENANT_ID)
    ] == [DEMO_SOURCE_CONNECTION_HANDLE]
    second.close()


def test_the_broker_holds_no_binding_until_an_architect_registers_one(tmp_path: Path) -> None:
    """Enrolling is the operator's step and registering is the architect's, and they are apart.

    A registry that registered on the way up would leave the sources stage complete before anyone
    opened the console, which is the one step the stage exists to show.
    """
    registry = _registry(tmp_path)

    assert registry.repository.list_for_tenant(DEMO_TENANT_ID) == ()
    assert (tmp_path / SOURCE_BINDING_DATABASE_FILENAME).exists()
    registry.close()


def test_a_draft_reaches_the_broker_over_the_demonstrations_own_store(tmp_path: Path) -> None:
    """The first of the three registration transactions, with the demonstration's resolver.

    Driving further takes a real PostgreSQL source for the probe to observe, which
    `tests/integration/test_source_binding_registration_live.py` does. What this holds is the
    composition: the draft is minted and persisted, and the capability behind it was resolved
    from the store this module injected rather than from a double.
    """
    registry = _registry(tmp_path)

    draft = registry.service.create_draft(
        tenant_id=DEMO_TENANT_ID,
        provider_kind="postgresql",
        connection_handle=DEMO_SOURCE_CONNECTION_HANDLE,
        account_mode="not_applicable",
        approved_object_refs=(DEMO_LOGICAL_OBJECT,),
    )

    assert draft.lifecycle_state is SourceConnectionBindingState.DRAFT
    assert draft.capability_profile_digest is None
    (stored,) = registry.repository.list_for_tenant(DEMO_TENANT_ID)
    assert stored.binding_id == draft.binding_id
    assert stored.connection_handle == DEMO_SOURCE_CONNECTION_HANDLE
    registry.close()
