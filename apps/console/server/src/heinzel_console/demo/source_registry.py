"""The connection broker the demonstration registers its own source through.

Until now the demonstration had no broker. The binding its acquisition ran under was assembled
by hand in `generation.py` and served from a reader that holds one value, because reaching
`ready` in `SourceBindingService` takes a private source capability and two-probe validation
evidence -- and the demonstration could invent neither. So the sources setup stage was a closed
door, and the binding behind the one working lane carried a placeholder where a capability
authority belongs.

Both halves are now reachable, because the role passwords stopped changing between starts. An
enrolment in `DemoSourceSecretStore` is immutable per handle, so a DSN carrying a password that
was minted fresh each start was refused on the second one; with the password derived from a kept
root, the same handle resolves to the same detail for as long as the state directory lives. That
is what this module needs and all it needed.

What it composes is the real thing on every side but custody: `SQLiteSourceBindingRepository` is
the broker's own store, `SourceBindingService` is the broker's own lifecycle, and
`PostgreSQLSourceCapabilityProbe` observes the source itself -- the declared relation reachable,
and every relation outside the declaration refused by the server. The one substitution is
`DemoSourceSecretStore`, which holds the connection detail in a file instead of a secret manager
and says so in its own documentation.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from heinzel_connection_broker import SourceBindingService, SQLiteSourceBindingRepository
from heinzel_provider_postgresql import PostgreSQLSourceCapabilityProbe
from pydantic import SecretStr

from ..governed_adapters import EnrolledSourceConnection
from .generation import (
    DEMO_LOGICAL_OBJECT,
    DEMO_SOURCE_CONNECTION_HANDLE,
    demo_source_acquisition_settings,
)
from .publication import DEMO_TENANT_ID
from .source_secrets import (
    DemoPostgreSQLSourceCapabilityAuthority,
    DemoSourceSecretStore,
)

__all__ = [
    "SOURCE_BINDING_DATABASE_FILENAME",
    "DemoEnrolledSourceConnections",
    "DemoSourceRegistry",
    "open_demo_source_registry",
]

SOURCE_BINDING_DATABASE_FILENAME = "source-bindings.sqlite3"


class DemoEnrolledSourceConnections:
    """What the demonstration offers an architect to register, read from its own secret store.

    Satisfies `EnrolledSourceConnectionReader`. The handles come from the store's records and the
    declaration comes from the deployment -- the same declaration the probe validates against and
    the acquisition reads under, because all three are built from one function. Nothing here
    reaches a connection detail: enrolled handles are names.

    A handle already registered is not filtered out here. The setup read does that, by taking the
    difference between what is offered and what the broker holds, so this reader answers one
    question only: what has an operator enrolled.
    """

    def __init__(self, store: DemoSourceSecretStore) -> None:
        self._store = store

    def list_enrolled_source_connections(
        self, tenant_id: str
    ) -> tuple[EnrolledSourceConnection, ...]:
        if tenant_id != DEMO_TENANT_ID:
            return ()
        return tuple(
            EnrolledSourceConnection(
                connection_handle=handle,
                provider_kind="postgresql",
                account_mode="not_applicable",
                declared_object_refs=(DEMO_LOGICAL_OBJECT,),
            )
            for handle in self._store.enrolled_connection_handles()
        )


@dataclass(frozen=True, slots=True)
class DemoSourceRegistry:
    """The broker, its store, and the two readers the console is composed from."""

    service: SourceBindingService
    repository: SQLiteSourceBindingRepository
    store: DemoSourceSecretStore
    enrolled: DemoEnrolledSourceConnections

    def close(self) -> None:
        self.repository.close()


def open_demo_source_registry(
    state_dir: Path,
    *,
    acquisition_dsn: str,
    clock: Callable[[], datetime],
) -> DemoSourceRegistry:
    """Open the broker over this state directory, with the demonstration's source enrolled.

    Enrolling here rather than leaving it to the architect is the one place this stands in for an
    operator: a deployment's operator enrols a connection detail out of band, before anyone opens
    the console, and the architect's work starts at registering what was enrolled. The
    demonstration has no operator, so it enrols its own warehouse's source role and stops --
    registering it is left to whoever is running the demonstration, because that is the step the
    sources stage exists to show.

    `enroll_connection` is immutable per handle and returns without writing when the same detail
    is already enrolled, so this is safe on every start and refuses loudly if the detail changed
    underneath it -- which would mean a binding validated against a credential that no longer
    exists, and is better reported than papered over.
    """
    store = DemoSourceSecretStore(state_dir)
    store.enroll_connection(
        connection_handle=DEMO_SOURCE_CONNECTION_HANDLE, dsn=SecretStr(acquisition_dsn)
    )
    repository = SQLiteSourceBindingRepository(
        str(state_dir / SOURCE_BINDING_DATABASE_FILENAME),
        # The register is opened here, once, and then read and written from whichever thread
        # serves a request: the console's reads run on its event loop and its commands run on a
        # threadpool, so a connection carrying the affinity of this thread would refuse every
        # registration -- and refuse it as a persistence failure the console can only report as
        # a downstream outage, which is the least diagnosable shape a wiring mistake can take.
        check_same_thread=False,
    )
    return DemoSourceRegistry(
        service=SourceBindingService(
            repository,
            secret_resolver=store,
            capability_probes={
                "postgresql": PostgreSQLSourceCapabilityProbe(
                    settings_authority=DemoPostgreSQLSourceCapabilityAuthority(
                        store, demo_source_acquisition_settings
                    )
                )
            },
            clock=clock,
        ),
        repository=repository,
        store=store,
        enrolled=DemoEnrolledSourceConnections(store),
    )
