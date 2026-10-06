"""The probe returns evidence only for a source whose both probes passed.

`SourceBindingValidationEvidence` types `positive_probe_succeeded` and `denial_probe_succeeded` as
`Literal[True]`, so there is no evidence that records a probe which failed. These are the refusals
that leaves: each one has to raise, and each is proved here against a driver that answers the
denial probe's statements rather than a server -- the denial probe runs first, so these never reach
the source at all.

The declaration checks need no connection, and are given one that fails if it is opened: a
disagreement between the settings an authority resolved and the binding being validated is settled
before a credential is used for anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from heinzel_connection_broker import (
    PrivateSourceCapability,
    SourceConnectionBinding,
    SourceConnectionBindingState,
)
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceCapabilityProbe,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import AcquisitionProviderError
from pydantic import SecretStr

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
APPROVED_COLUMNS = ("sale_id", "region", "updated_at")
UNRELATED_SCHEMA = "private_admin"
# current_user, session_user, unrelated schemas named, USAGE on it, then the five role attributes
# that make any relation reachable whatever the grants say.
LEAST_PRIVILEGED_ROLE_ROW: tuple[object, ...] = (
    "acquisition_runtime",
    "acquisition_runtime",
    1,
    False,
    False,
    False,
    False,
    False,
    False,
)


@dataclass
class Answers:
    """What the fake driver answers the denial probe's three statements with."""

    role_row: tuple[object, ...] | None = LEAST_PRIVILEGED_ROLE_ROW
    relations: tuple[tuple[object, ...], ...] = (("secrets", "r"),)
    unrelated_read_refused: bool = True


class FakeCursor:
    def __init__(self, answers: Answers) -> None:
        self._answers = answers
        self._row: tuple[object, ...] | None = None
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query: object, params: object = None) -> object:
        if isinstance(query, str) and "current_user, session_user" in query:
            self._row = self._answers.role_row
            return None
        if isinstance(query, str) and "pg_catalog.pg_class relation" in query:
            self._rows = list(self._answers.relations)
            return None
        if not isinstance(query, str):
            # The composed `SELECT 1 FROM <unrelated>.<relation> LIMIT 1`.
            if self._answers.unrelated_read_refused:
                raise psycopg.errors.InsufficientPrivilege(
                    f"permission denied for schema {UNRELATED_SCHEMA}"
                )
            self._rows = [(1,)]
            return None
        raise AssertionError(f"the probe issued an unexpected statement: {query!r}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return iter(self._rows)

    def close(self) -> None:
        return None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> object:
        return None


class FakeConnection:
    def __init__(self, answers: Answers) -> None:
        self._answers = answers
        self.rollbacks = 0
        self.closed = False

    def execute(self, query: str) -> object:
        raise AssertionError("the denial probe opens no transaction of its own")

    def cursor(self, name: str | None = None) -> FakeCursor:
        return FakeCursor(self._answers)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class Authority:
    """Resolves the settings a deployment would acquire under, over a DSN nothing dials."""

    def __init__(self, settings: PostgreSQLAcquisitionSettings) -> None:
        self._settings = settings

    def resolve_source_acquisition(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        credential_revision: int,
        endpoint_reference: str,
        credential_reference: str,
    ) -> PostgreSQLAcquisitionSettings:
        return self._settings


def settings(
    *,
    connection_handle: str = "registered-source",
    object_refs: tuple[str, ...] = ("sales",),
) -> PostgreSQLAcquisitionSettings:
    return PostgreSQLAcquisitionSettings(
        dsn=SecretStr("postgresql://acquisition_runtime@127.0.0.1:1/source"),
        connection_handle=connection_handle,
        objects=tuple(
            PostgreSQLSourceObjectDeclaration(
                logical_object_ref=object_ref,
                schema_name="source_data",
                table_name=object_ref,
                field_names=APPROVED_COLUMNS,
                key_name="sale_id",
                source_updated_at_field="updated_at",
            )
            for object_ref in object_refs
        ),
        unrelated_schema_name=UNRELATED_SCHEMA,
        max_write_transaction_duration=timedelta(minutes=5),
    )


def binding(*, connection_handle: str = "registered-source") -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id="src-live-a",
        tenant_id="tenant-live-a",
        provider_kind="postgresql",
        connection_handle=connection_handle,
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.VALIDATING,
        approved_object_refs=("sales",),
        credential_revision=1,
        revision=2,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(seconds=1),
    )


def capability() -> PrivateSourceCapability:
    return PrivateSourceCapability(
        tenant_id="tenant-live-a",
        binding_id="src-live-a",
        provider_kind="postgresql",
        connection_handle="registered-source",
        account_mode="not_applicable",
        credential_revision=1,
        endpoint_reference="endpoint-ref:" + "a" * 64,
        credential_reference="credential-ref:" + "b" * 64,
    )


def probe_over(
    answers: Answers, resolved: PostgreSQLAcquisitionSettings
) -> tuple[PostgreSQLSourceCapabilityProbe, list[FakeConnection]]:
    opened: list[FakeConnection] = []

    def connect(dsn: str) -> FakeConnection:
        connection = FakeConnection(answers)
        opened.append(connection)
        return connection

    return (
        PostgreSQLSourceCapabilityProbe(settings_authority=Authority(resolved), connect=connect),
        opened,
    )


def test_a_role_holding_usage_on_the_schema_it_was_declared_away_from_yields_no_evidence() -> None:
    answers = Answers(
        role_row=("acquisition_runtime", "acquisition_runtime", 1, True) + (False,) * 5
    )
    probe, opened = probe_over(answers, settings())
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "authorization_denied"
    assert refusal.value.classification == "authorization_denied"
    assert opened[0].closed is True


def test_a_role_that_can_read_outside_its_declaration_yields_no_evidence() -> None:
    """The catalog says the privilege is withheld and the read succeeds anyway.

    A privilege answer is what the server believes; the attempted read is what it does. Only the
    second can catch a relation reachable through something the predicate did not ask about.
    """
    probe, opened = probe_over(Answers(unrelated_read_refused=False), settings())
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "authorization_denied"
    assert opened[0].closed is True


def test_an_unrelated_schema_with_nothing_to_be_refused_on_yields_no_evidence() -> None:
    """A denial probe with no subject would pass by having nothing to try."""
    probe, _opened = probe_over(Answers(relations=()), settings())
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "permanent_configuration"


def test_a_role_whose_session_differs_from_its_current_user_yields_no_evidence() -> None:
    """Answers taken inside `SET ROLE` describe the assumed role, not the resolved credential."""
    answers = Answers(role_row=("acquisition_runtime", "postgres", 1) + (False,) * 6)
    probe, _opened = probe_over(answers, settings())
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "authorization_denied"


def test_a_superuser_role_yields_no_evidence() -> None:
    """`rolsuper` makes every relation reachable whatever the grants answer."""
    answers = Answers(
        role_row=("acquisition_runtime", "acquisition_runtime", 1, False, True) + (False,) * 4
    )
    probe, _opened = probe_over(answers, settings())
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "authorization_denied"


def test_settings_declaring_more_than_the_binding_approved_are_refused_before_connecting() -> None:
    """The least-privilege predicate is evaluated over every declared object, so it must match.

    Settings declaring a second object would have the denial probe prove least privilege over a
    declaration wider than the one the binding carries -- and the binding would then be ready over
    an object nobody approved.
    """
    probe, opened = probe_over(Answers(), settings(object_refs=("orders", "sales")))
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "permanent_configuration"
    assert opened == []


def test_settings_naming_another_connection_handle_are_refused_before_connecting() -> None:
    probe, opened = probe_over(Answers(), settings(connection_handle="another-source"))
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "permanent_configuration"
    assert opened == []


def test_a_driver_failure_during_the_denial_probe_is_not_reported_as_a_denial() -> None:
    """A server that cannot be reached is not a source refusing a role."""

    def connect(dsn: str) -> FakeConnection:
        raise psycopg.errors.ConnectionFailure("the source did not answer")

    probe = PostgreSQLSourceCapabilityProbe(
        settings_authority=Authority(settings()), connect=connect
    )
    with pytest.raises(AcquisitionProviderError) as refusal:
        probe.validate(binding=binding(), capability=capability(), observed_at=NOW)
    assert refusal.value.reason_code == "transport_failure"
    assert refusal.value.classification == "transient_transport"
