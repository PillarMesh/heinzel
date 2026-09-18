"""Regressions for ways the console server misbehaved.

Each test names the way the product misbehaved, not the shape of the fix.
"""

from __future__ import annotations

import sqlite3
import time
from threading import Thread

import pytest
from heinzel_console import create_app
from heinzel_console.auth import (
    InFlightCommandKeys,
    SessionCsrfTokens,
    TrustedActorContext,
)
from heinzel_console.errors import ConsoleConflict
from heinzel_console.governed_adapters import (
    classify_downstream_failure,
    console_error_for,
)
from heinzel_console.operation_handles import (
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    mint_console_handle,
)
from pydantic import BaseModel, ValidationError


def _context(actor_id: str = "actor-a", session_id: str = "session-a") -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id="tenant-a",
        actor_id=actor_id,
        roles=("data_architect",),
        active_role="data_architect",
        session_id=session_id,
    )


def test_the_allowed_origin_follows_the_port_the_launch_scripts_advertise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both launch scripts expose HEINZEL_CONSOLE_API_PORT.

    With the origin fixed at 8000, every read worked and every command failed
    `same_origin_required`, which reads as a broken product rather than a
    misconfigured one.
    """
    monkeypatch.setenv("HEINZEL_CONSOLE_ALLOWED_ORIGIN", "http://127.0.0.1:8123")
    app = create_app()

    assert app.state.allowed_origin == "http://127.0.0.1:8123"


def test_an_explicit_allowed_origin_still_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEINZEL_CONSOLE_ALLOWED_ORIGIN", "http://127.0.0.1:8123")
    app = create_app(allowed_origin="http://127.0.0.1:9000")

    assert app.state.allowed_origin == "http://127.0.0.1:9000"


def test_managed_link_origin_is_absent_until_explicitly_configured() -> None:
    app = create_app()

    assert app.state.managed_link_origin is None


def test_managed_link_origin_is_normalized_from_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEINZEL_CONSOLE_MANAGED_LINK_ORIGIN", "https://BI.EXAMPLE.TEST:443/")

    app = create_app()

    assert app.state.managed_link_origin == "https://bi.example.test"


@pytest.mark.parametrize(
    "origin",
    (
        "http://bi.example.test",
        "//bi.example.test",
        "https://user@bi.example.test",
        "https://bi.example.test/path",
        "https://bi.example.test?query=1",
        "https://bi.example.test?",
        "https://bi.example.test#fragment",
        "https://bi.example.test#",
        "https://bi.example.test:bad",
        "https:\\bi.example.test",
    ),
)
def test_invalid_managed_link_origin_configuration_is_rejected(origin: str) -> None:
    with pytest.raises(ValueError, match="managed link origin"):
        create_app(managed_link_origin=origin)


def test_a_malformed_persisted_artifact_is_not_reported_as_a_stale_revision() -> None:
    """Pydantic's ValidationError is a ValueError.

    Classifying it as a conflict told the operator to reload a resource that had
    not changed, and to keep reloading a row that will never validate.
    """

    class Artifact(BaseModel):
        revision: int

    try:
        Artifact.model_validate({"revision": "not an integer"})
    except ValidationError as error:
        corrupt = error
    else:  # pragma: no cover - the model must reject this input
        raise AssertionError("the artifact validated unexpectedly")

    assert classify_downstream_failure(corrupt) == "integrity"
    console_error = console_error_for(corrupt)
    assert console_error.recovery_action != "reload"
    assert not isinstance(console_error, ConsoleConflict)


def test_a_composition_error_is_not_reported_as_a_stale_revision() -> None:
    """The warehouse repository refuses an ambiguous construction with ValueError."""
    from heinzel_warehouse_control.repository import SQLiteWarehouseRepository

    connection = sqlite3.connect(":memory:")
    try:
        SQLiteWarehouseRepository(database_path=":memory:", connection=connection)
    except ValueError as error:
        assert classify_downstream_failure(error) == "integrity"
    else:  # pragma: no cover
        raise AssertionError("the repository accepted an ambiguous construction")
    finally:
        connection.close()


def test_an_accidental_key_error_is_not_silently_a_missing_resource() -> None:
    """A mapping typo inside console code used to surface as a 404.

    The owning services signal "not yours or not there" at the ownership
    boundary, which translates it there; anything else must stay an error.
    """
    with pytest.raises(KeyError):
        classify_downstream_failure(KeyError("capability_kind"))


def test_a_duplicate_command_gives_up_rather_than_pinning_a_worker() -> None:
    """A hung command plus browser retries after a lost response exhausted the threadpool."""
    keys = InFlightCommandKeys(wait_timeout_seconds=0.2)
    context = _context()
    started = time.monotonic()
    outcome: list[BaseException | str] = []

    def hold() -> None:
        with keys.serialize(context, "idempotency-held"):
            time.sleep(2.0)

    holder = Thread(target=hold, daemon=True)
    holder.start()
    time.sleep(0.05)

    def duplicate() -> None:
        try:
            with keys.serialize(context, "idempotency-held"):
                outcome.append("entered")
        except BaseException as error:
            outcome.append(error)

    worker = Thread(target=duplicate)
    worker.start()
    worker.join(timeout=5)

    assert len(outcome) == 1
    assert isinstance(outcome[0], ConsoleConflict)
    # The conflict itself is the property; this only rules out the second holder
    # having blocked on the lock first. `join` already bounds the wait at five
    # seconds, so a tighter figure here just measures the host.
    assert time.monotonic() - started < 5


def test_csrf_bindings_do_not_accumulate_without_bound() -> None:
    tokens = SessionCsrfTokens(maximum_bindings=4)
    first = _context(session_id="session-0")
    tokens.issue(first)
    for index in range(1, 12):
        tokens.issue(_context(session_id=f"session-{index}"))

    assert tokens.tracked_bindings() <= 4
    assert tokens.matches(first, tokens.issue(first))


def test_operation_handles_do_not_accumulate_without_bound() -> None:
    repository = InMemoryOperationHandleRepository(maximum_records=4)
    handles = []
    for index in range(12):
        handle = mint_console_handle()
        handles.append(handle)
        repository.store(
            OperationHandleRecord(
                tenant_id="tenant-a",
                console_handle=handle,
                capability_kind="warehouse_lifecycle",
                private_identity=f"private-{index}",
            )
        )

    assert repository.tracked_records() <= 4
    assert repository.load(tenant_id="tenant-a", console_handle=handles[-1]) is not None
    assert repository.load(tenant_id="tenant-a", console_handle=handles[0]) is None
