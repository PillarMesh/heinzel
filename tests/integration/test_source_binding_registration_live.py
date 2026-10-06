"""A source is registered through the connection broker, against a real PostgreSQL source.

`SourceBindingService` can drive a binding `draft -> validating -> ready`, but only on evidence a
`SourceCapabilityProbe` returned, over a capability a `SourceSecretResolver` resolved. This walks
that whole path with both collaborators real: the demonstration's secret store holds the connection
detail behind the references, and `PostgreSQLSourceCapabilityProbe` observes the source itself --
the declared relation reachable, and every relation outside the declaration refused by the server.

What makes the result evidence rather than a rendering: the ready binding is read back out of the
repository, at revision 3, carrying the `capability_profile_digest` and `source_observation_ref`
the recorded evidence carries. Nothing but `record_validation` writes `ready`, and it writes the
binding and its evidence in one transaction.

The denial half is proved as a denial too: the same journey over a role that holds `USAGE` on the
schema it was declared away from is refused `authorization_denied`, and leaves the binding in
`validating` with no capability authority at all.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from heinzel_connection_broker import (
    SourceBindingBoundaryError,
    SourceBindingNotFoundError,
    SourceBindingService,
    SourceConnectionBindingState,
    SQLiteSourceBindingRepository,
)
from heinzel_console.demo.source_secrets import (
    DemoPostgreSQLSourceCapabilityAuthority,
    DemoSourceSecretStore,
)
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionSettings,
    PostgreSQLSourceCapabilityProbe,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import AcquisitionProviderError
from psycopg import sql
from pydantic import SecretStr

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster

pytestmark = pytest.mark.live

_TENANT = "tenant-live-a"
_CONNECTION_HANDLE = "registered-live-source"
_LOGICAL_OBJECT = "sales"
_UNRELATED_SCHEMA = "private_admin"
_APPROVED_COLUMNS = ("sale_id", "region", "customer_id", "revenue", "updated_at")
_SEEDED_AT = datetime(2026, 9, 12, tzinfo=UTC)
_FIRST = datetime(2026, 9, 17, 12, tzinfo=UTC)


class _Clock:
    """A clock the journey advances by hand, so every revision carries a distinct moment."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _role_dsn(bootstrap_dsn: str, role: str, password: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=parsed["host"],
        port=parsed["port"],
        dbname=parsed["dbname"],
        user=role,
        password=password,
    )


def _provision_source(bootstrap_dsn: str, *, role: str, password: str) -> None:
    """A source the acquisition role can read, and a schema it is declared away from.

    `private_admin.secrets` is not decoration: the provider's least-privilege predicate requires
    the unrelated schema to exist, and the probe's denial half requires a relation inside it to be
    refused on. Without one there is nothing the role can be observed failing to reach.
    """
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        connection.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(connection.info.dbname)
            )
        )
        connection.execute("REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC")
        connection.execute("CREATE SCHEMA source_data")
        connection.execute(f"CREATE SCHEMA {_UNRELATED_SCHEMA}")
        connection.execute(
            "CREATE TABLE source_data.sales ("
            "sale_id bigint PRIMARY KEY, region text NOT NULL, customer_id bigint NOT NULL, "
            "revenue numeric NOT NULL, updated_at timestamptz NOT NULL)"
        )
        connection.execute(
            "INSERT INTO source_data.sales VALUES "
            "(1, 'west', 101, 10.00, %s), (2, 'east', 102, 99.00, %s)",
            (_SEEDED_AT, _SEEDED_AT),
        )
        connection.execute(f"CREATE TABLE {_UNRELATED_SCHEMA}.secrets (secret_value text NOT NULL)")
        connection.execute(f"INSERT INTO {_UNRELATED_SCHEMA}.secrets VALUES ('unreachable')")
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA source_data TO {}").format(sql.Identifier(role))
        )
        connection.execute(
            sql.SQL("GRANT SELECT ({}) ON source_data.sales TO {}").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in _APPROVED_COLUMNS),
                sql.Identifier(role),
            )
        )


def _settings(connection_handle: str, dsn: SecretStr) -> PostgreSQLAcquisitionSettings:
    """The declaration the deployment would acquire under, composed over the resolved DSN."""
    return PostgreSQLAcquisitionSettings(
        dsn=dsn,
        connection_handle=connection_handle,
        objects=(
            PostgreSQLSourceObjectDeclaration(
                logical_object_ref=_LOGICAL_OBJECT,
                schema_name="source_data",
                table_name="sales",
                field_names=_APPROVED_COLUMNS,
                key_name="sale_id",
                source_updated_at_field="updated_at",
            ),
        ),
        unrelated_schema_name=_UNRELATED_SCHEMA,
        max_write_transaction_duration=timedelta(minutes=5),
    )


@dataclass(frozen=True)
class _Registry:
    service: SourceBindingService
    repository: SQLiteSourceBindingRepository
    store: DemoSourceSecretStore
    probe: PostgreSQLSourceCapabilityProbe


def _registry(tmp_path: Path, *, clock: _Clock) -> _Registry:
    repository = SQLiteSourceBindingRepository(str(tmp_path / "source-bindings.sqlite3"))
    store = DemoSourceSecretStore(tmp_path / "state")
    probe = PostgreSQLSourceCapabilityProbe(
        settings_authority=DemoPostgreSQLSourceCapabilityAuthority(store, _settings)
    )
    return _Registry(
        service=SourceBindingService(
            repository,
            secret_resolver=store,
            capability_probes={"postgresql": probe},
            clock=clock,
        ),
        repository=repository,
        store=store,
        probe=probe,
    )


@pytest.fixture(name="cluster")
def _cluster(tmp_path: Path) -> Iterator[str]:
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        yield bootstrap_dsn


def test_a_registered_postgresql_source_reaches_ready_with_probed_capability_authority(
    tmp_path: Path, cluster: str
) -> None:
    """Draft, validating, ready -- with the capability authority the probe observed."""
    password = secrets.token_urlsafe(32)
    _provision_source(cluster, role="acquisition_runtime", password=password)
    clock = _Clock(_FIRST)
    registry = _registry(tmp_path, clock=clock)
    service, repository = registry.service, registry.repository
    registry.store.enroll_connection(
        connection_handle=_CONNECTION_HANDLE,
        dsn=SecretStr(_role_dsn(cluster, "acquisition_runtime", password)),
    )

    draft = service.create_draft(
        tenant_id=_TENANT,
        provider_kind="postgresql",
        connection_handle=_CONNECTION_HANDLE,
        account_mode="not_applicable",
        approved_object_refs=(_LOGICAL_OBJECT,),
    )
    assert draft.lifecycle_state is SourceConnectionBindingState.DRAFT
    assert (draft.revision, draft.credential_revision) == (1, 1)
    assert draft.capability_profile_digest is None

    clock.advance(1)
    validating = service.transition(
        _TENANT,
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=draft.revision,
    )
    assert validating.revision == 2

    clock.advance(1)
    ready = service.validate(_TENANT, draft.binding_id, expected_revision=validating.revision)
    assert ready.lifecycle_state is SourceConnectionBindingState.READY
    # `_assert_advance` forces one revision per step, so a registered binding lands at three:
    # the draft, the transition into validating, and the recorded validation.
    assert ready.revision == 3
    assert ready.capability_profile_digest is not None
    assert ready.source_observation_ref is not None

    # Read back out of the store rather than trusted from the return value: the ready state and
    # its authority are only real if `record_validation` committed them.
    stored = repository.load(_TENANT, draft.binding_id)
    assert stored.lifecycle_state is SourceConnectionBindingState.READY
    assert stored.revision == 3
    assert stored.capability_profile_digest == ready.capability_profile_digest
    assert stored.source_observation_ref == ready.source_observation_ref

    evidence = repository.load_validation(_TENANT, draft.binding_id, 2)
    assert evidence.provider_kind == "postgresql"
    assert evidence.positive_probe_succeeded is True
    assert evidence.denial_probe_succeeded is True
    assert evidence.positive_probe_digest != evidence.denial_probe_digest
    assert evidence.capability_profile_digest == stored.capability_profile_digest
    assert evidence.source_observation_ref == stored.source_observation_ref
    assert evidence.source_observation_ref.endswith(evidence.positive_probe_digest)
    assert evidence.binding_revision == 2
    assert evidence.credential_revision == 1
    repository.close()


def test_a_source_whose_role_reaches_outside_its_declaration_is_never_recorded_ready(
    tmp_path: Path, cluster: str
) -> None:
    """The denial probe is a denial: granted the unrelated schema, no evidence exists at all.

    This is the case the evidence model cannot express. `positive_probe_succeeded` and
    `denial_probe_succeeded` are `Literal[True]`, so a failed probe has to raise -- and this shows
    what that leaves behind: a binding still in `validating`, with no capability authority, and no
    evidence row for the revision the validation was attempted at.
    """
    password = secrets.token_urlsafe(32)
    _provision_source(cluster, role="acquisition_runtime", password=password)
    with psycopg.connect(cluster) as connection:
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO acquisition_runtime").format(
                sql.Identifier(_UNRELATED_SCHEMA)
            )
        )
    clock = _Clock(_FIRST)
    registry = _registry(tmp_path, clock=clock)
    service, repository = registry.service, registry.repository
    registry.store.enroll_connection(
        connection_handle=_CONNECTION_HANDLE,
        dsn=SecretStr(_role_dsn(cluster, "acquisition_runtime", password)),
    )
    draft = service.create_draft(
        tenant_id=_TENANT,
        provider_kind="postgresql",
        connection_handle=_CONNECTION_HANDLE,
        account_mode="not_applicable",
        approved_object_refs=(_LOGICAL_OBJECT,),
    )
    clock.advance(1)
    validating = service.transition(
        _TENANT,
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=draft.revision,
    )

    clock.advance(1)
    # Asked of the probe directly as well as through the service, because the service wraps
    # whatever a probe raises into `SourceBindingBoundaryError` without its cause. Without this
    # the test would pass on any refusal at all, including one for the wrong reason.
    capability = registry.store.resolve(
        tenant_id=_TENANT,
        binding_id=draft.binding_id,
        provider_kind="postgresql",
        connection_handle=_CONNECTION_HANDLE,
        account_mode="not_applicable",
        credential_revision=1,
    )
    with pytest.raises(AcquisitionProviderError) as probed:
        registry.probe.validate(binding=validating, capability=capability, observed_at=clock())
    assert probed.value.reason_code == "authorization_denied"

    with pytest.raises(SourceBindingBoundaryError) as refusal:
        service.validate(_TENANT, draft.binding_id, expected_revision=validating.revision)
    assert refusal.value.operation == "probe source capability"

    stored = repository.load(_TENANT, draft.binding_id)
    assert stored.lifecycle_state is SourceConnectionBindingState.VALIDATING
    assert stored.revision == 2
    assert stored.capability_profile_digest is None
    assert stored.source_observation_ref is None
    with pytest.raises(SourceBindingNotFoundError):
        repository.load_validation(_TENANT, draft.binding_id, 2)
    repository.close()
