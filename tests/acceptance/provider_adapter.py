from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

import psycopg
import snowflake.connector
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import OrderRow
from psycopg import sql

from .config import (
    POSTGRES_AUDIT_FUNCTION,
    POSTGRES_MARKER_TABLE,
    SNOWFLAKE_ENVIRONMENT_MARKER,
    SNOWFLAKE_FILE_FORMAT,
    SNOWFLAKE_OWNER_ROLE,
    AcceptanceConfig,
    HarnessError,
)
from .private_files import EnvironmentReservation

POSTGRES_RUNTIME_GRANTS = (
    "CONNECT_DATABASE",
    "EXECUTE_AUDIT",
    "SELECT_MARKER",
    "SELECT_SOURCE",
    "USAGE_SCHEMA",
)
POSTGRES_FIXTURE_GRANTS = (
    "CONNECT_DATABASE",
    "DELETE_SOURCE",
    "INSERT_SOURCE",
    "SELECT_SOURCE_KEY_COLUMN",
    "USAGE_SCHEMA",
)
SNOWFLAKE_RUNTIME_GRANTS = (
    "INSERT_LEDGER",
    "INSERT_TARGET",
    "READ_STAGE",
    "SELECT_ENVIRONMENT_MARKER",
    "SELECT_LEDGER",
    "SELECT_NEGATIVE_TARGET",
    "SELECT_TARGET",
    "UPDATE_TARGET",
    "USAGE_DATABASE",
    "USAGE_SCHEMA",
    "USAGE_WAREHOUSE",
    "WRITE_STAGE",
)
SNOWFLAKE_OBJECT_KINDS = (
    ("database", "DATABASE"),
    ("environment_marker", "TABLE"),
    ("file_format", "FILE FORMAT"),
    ("ledger", "TABLE"),
    ("negative_target", "TABLE"),
    ("schema", "SCHEMA"),
    ("stage", "STAGE"),
    ("target", "TABLE"),
    ("warehouse", "WAREHOUSE"),
)

TRUSTED_POSTGRES_AUDIT_BODY = """
SELECT coalesce(sum(s.calls), 0)::bigint
FROM public.pg_stat_statements AS s
JOIN pg_catalog.pg_roles AS r ON r.oid = s.userid
WHERE pg_catalog.pg_has_role(session_user, r.oid, 'MEMBER')
  AND s.query ILIKE '%pillarmesh_m0%orders%'
  AND s.query ~* '^[[:space:]]*(select|declare)'
""".strip()


def _sql_shape_digest(value: str) -> str:
    return hashlib.sha256(" ".join(value.split()).casefold().encode()).hexdigest()


TRUSTED_POSTGRES_AUDIT_BODY_DIGEST = _sql_shape_digest(TRUSTED_POSTGRES_AUDIT_BODY)
TRUSTED_POSTGRES_AUDIT_CONFIG = ("search_path=pg_catalog, public",)

_SNOWFLAKE_METADATA_QUERIES = (
    "SELECT table_type FROM information_schema.tables "
    "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s",
    "SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale, "
    "character_maximum_length, datetime_precision FROM information_schema.columns "
    "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s "
    "ORDER BY ordinal_position",
    "SELECT kcu.column_name, tc.constraint_type "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "ON kcu.constraint_catalog = tc.constraint_catalog "
    "AND kcu.constraint_schema = tc.constraint_schema "
    "AND kcu.constraint_name = tc.constraint_name "
    "WHERE tc.table_catalog = %s AND tc.table_schema = %s AND tc.table_name = %s "
    "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
    "ORDER BY CASE tc.constraint_type WHEN 'PRIMARY KEY' THEN 0 ELSE 1 END, "
    "kcu.ordinal_position",
)


def _query_shape(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    return re.sub(r"'(?:''|[^'])*'|%s|\?|:[a-z0-9_]+", "<bind>", normalized)


_SNOWFLAKE_METADATA_QUERY_SHAPES = frozenset(map(_query_shape, _SNOWFLAKE_METADATA_QUERIES))


def _is_allowed_metadata_query(value: str) -> bool:
    return _query_shape(value) in _SNOWFLAKE_METADATA_QUERY_SHAPES


def _required_row(value: tuple[Any, ...] | None, context: str) -> tuple[Any, ...]:
    if value is None:
        raise HarnessError(f"{context} returned no row")
    return value


def _error_label(error: BaseException) -> str:
    """Identify a driver failure without repeating a message that may embed a DSN."""
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return f"{type(error).__name__} [{sqlstate}]"
    return type(error).__name__


def snowflake_access_denied(error: BaseException) -> bool:
    return getattr(error, "sqlstate", None) == "42501"


@dataclass(frozen=True)
class RuntimeIdentities:
    postgres_runtime_session: str
    postgres_runtime_current: str
    postgres_fixture_session: str
    postgres_fixture_current: str
    snowflake_runtime: str


@dataclass(frozen=True)
class DedicatedEnvironmentAttestation:
    identities: RuntimeIdentities
    postgres_database: str
    postgres_database_owner: str
    postgres_schema_owner: str
    postgres_marker_environment_id: str
    postgres_source_kind: str
    postgres_marker_kind: str
    postgres_source_owner: str
    postgres_marker_owner: str
    postgres_audit_kind: str
    postgres_audit_owner: str
    postgres_audit_security_definer: bool
    postgres_audit_language: str
    postgres_audit_volatility: str
    postgres_audit_result: str
    postgres_audit_config: tuple[str, ...]
    postgres_audit_body_digest: str
    postgres_runtime_grants: tuple[str, ...]
    postgres_fixture_grants: tuple[str, ...]
    postgres_denial_schema_exists: bool
    snowflake_account: str
    snowflake_account_locator: str
    snowflake_role: str
    snowflake_user_roles: tuple[str, ...]
    snowflake_marker_environment_identity: str
    snowflake_marker_owner_user: str
    snowflake_marker_owner_role: str
    snowflake_marker_denial_database: str
    snowflake_marker_denial_database_owner_role: str
    snowflake_object_kinds: tuple[tuple[str, str], ...]
    snowflake_object_owners: tuple[tuple[str, str], ...]
    snowflake_object_grants: tuple[tuple[str, str, str], ...]
    snowflake_runtime_grants: tuple[str, ...]


@dataclass(frozen=True)
class VisibilityObservation:
    value_digest: str
    query_id: str


@dataclass(frozen=True)
class ReplayObservation:
    target_rows: int
    target_value_digest: str
    ledger_rows: int
    ledger_manifest_digest: str
    ledger_committed_identity: str
    stage_state_digest: str
    product_mutation_count: int


@dataclass(frozen=True)
class NegativeObservation:
    positive_target_state_digest: str
    negative_target_state_digest: str
    stage_state_digest: str
    ledger_state_digest: str
    postgres_source_data_read_count: int
    snowflake_product_data_query_count: int
    metadata_observation_count: int

    def effect_state(self) -> tuple[object, ...]:
        return (
            self.positive_target_state_digest,
            self.negative_target_state_digest,
            self.stage_state_digest,
            self.ledger_state_digest,
            self.postgres_source_data_read_count,
            self.snowflake_product_data_query_count,
        )


@dataclass(frozen=True)
class ProviderResourceState:
    source_row_exists: bool
    target_row_exists: bool | None
    commit_ledger_entry_exists: bool | None
    staged_segment_exists: bool | None


@dataclass(frozen=True)
class FixtureRow:
    order_id: int
    customer_ref: str
    amount: Decimal
    currency: str
    status: str
    updated_at: datetime


class ProviderActions(Protocol):
    def admission(self, environment_identity: str) -> AbstractContextManager[ProviderAdmission]: ...

    def preflight(self) -> DedicatedEnvironmentAttestation: ...

    def prove_destination_absent(self, acceptance_key: int) -> None: ...

    def insert_fixture(self, row: object) -> None: ...

    def verify_destination(self, acceptance_key: int) -> VisibilityObservation: ...

    def replay_observation(self, acceptance_key: int, batch_id: str) -> ReplayObservation: ...

    def negative_observation(self) -> NegativeObservation: ...

    def reconcile_resources(
        self, acceptance_key: int, batch_id: str | None
    ) -> ProviderResourceState: ...


class ProviderAdmission(Protocol):
    def assert_intact(self) -> None: ...


_ADMISSION_INTEGRITY_QUERY = (
    "SELECT current_database(), pg_backend_pid(), "
    "(SELECT COUNT(*) FROM pg_catalog.pg_locks "
    "WHERE pid=pg_catalog.pg_backend_pid() AND locktype='advisory' AND granted)"
)


@dataclass
class _PostgresAdmission:
    connection: Any
    expected_database: str
    expected_backend_pid: int
    lost: bool = False

    def assert_intact(self) -> None:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(_ADMISSION_INTEGRITY_QUERY)
                database, backend_pid, advisory_locks = _required_row(
                    cursor.fetchone(), "provider admission integrity probe"
                )
        except Exception as error:
            self.lost = True
            # The driver message can carry connection detail, so it is dropped; the
            # exception type and SQLSTATE are what an operator acts on and are safe.
            raise HarnessError(
                f"provider admission lock could not be confirmed: {_error_label(error)}"
            ) from None
        if (
            str(database) != self.expected_database
            or int(backend_pid) != self.expected_backend_pid
            or int(advisory_locks) != 1
        ):
            self.lost = True
            raise HarnessError("provider admission lock was lost")


def validate_attestation(
    config: AcceptanceConfig, attestation: DedicatedEnvironmentAttestation
) -> None:
    env = config.environment
    identities = attestation.identities
    postgres_runtime_session = identities.postgres_runtime_session.casefold()
    postgres_runtime_current = identities.postgres_runtime_current.casefold()
    postgres_fixture_session = identities.postgres_fixture_session.casefold()
    postgres_fixture_current = identities.postgres_fixture_current.casefold()
    declared_runtime = env["PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL"].casefold()
    declared_fixture = env["PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL"].casefold()
    declared_owner = env["PILLARMESH_POSTGRES_OWNER_PRINCIPAL"].casefold()
    if (
        postgres_runtime_session != declared_runtime
        or postgres_runtime_current != declared_runtime
        or postgres_fixture_session != declared_fixture
        or postgres_fixture_current != declared_fixture
        or identities.snowflake_runtime.casefold() != env["PILLARMESH_SNOWFLAKE_USER"].casefold()
        or postgres_runtime_session in {declared_fixture, declared_owner}
        or postgres_runtime_current in {declared_fixture, declared_owner}
        or postgres_fixture_session == declared_owner
        or postgres_fixture_current == declared_owner
        or identities.snowflake_runtime.casefold()
        == env["PILLARMESH_SNOWFLAKE_OWNER_USER"].casefold()
    ):
        raise HarnessError("connected runtime principal is not isolated")

    owner = env["PILLARMESH_POSTGRES_OWNER_PRINCIPAL"].casefold()
    snowflake_owner = SNOWFLAKE_OWNER_ROLE.casefold()
    expected_owners = tuple((name, SNOWFLAKE_OWNER_ROLE) for name, _ in SNOWFLAKE_OBJECT_KINDS)
    expected_object_grants = tuple(
        sorted(
            (
                *(
                    (name, "OWNERSHIP", SNOWFLAKE_OWNER_ROLE)
                    for name, _kind in SNOWFLAKE_OBJECT_KINDS
                ),
                ("database", "USAGE", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("schema", "USAGE", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("warehouse", "USAGE", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("stage", "READ", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("stage", "WRITE", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("target", "SELECT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("target", "INSERT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("target", "UPDATE", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("negative_target", "SELECT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("ledger", "SELECT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("ledger", "INSERT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
                ("environment_marker", "SELECT", env["PILLARMESH_SNOWFLAKE_ROLE"]),
            )
        )
    )
    dedicated = (
        attestation.postgres_database == env["PILLARMESH_POSTGRES_DATABASE"]
        and attestation.postgres_database_owner.casefold() == owner
        and attestation.postgres_schema_owner.casefold() == owner
        and attestation.postgres_marker_environment_id == config.environment_identity
        and attestation.postgres_source_kind == "base_table"
        and attestation.postgres_marker_kind == "base_table"
        and attestation.postgres_source_owner.casefold() == owner
        and attestation.postgres_marker_owner.casefold() == owner
        and attestation.postgres_audit_kind == "function"
        and attestation.postgres_audit_owner.casefold() == owner
        and attestation.postgres_audit_security_definer
        and attestation.postgres_audit_language == "sql"
        and attestation.postgres_audit_volatility == "stable"
        and attestation.postgres_audit_result == "bigint"
        and attestation.postgres_audit_config == TRUSTED_POSTGRES_AUDIT_CONFIG
        and attestation.postgres_audit_body_digest == TRUSTED_POSTGRES_AUDIT_BODY_DIGEST
        and attestation.postgres_runtime_grants == POSTGRES_RUNTIME_GRANTS
        and attestation.postgres_fixture_grants == POSTGRES_FIXTURE_GRANTS
        and attestation.postgres_denial_schema_exists
        and env["PILLARMESH_SNOWFLAKE_ACCOUNT"].casefold()
        in {
            attestation.snowflake_account.casefold(),
            attestation.snowflake_account_locator.casefold(),
        }
        and attestation.snowflake_role.casefold() == env["PILLARMESH_SNOWFLAKE_ROLE"].casefold()
        and tuple(value.casefold() for value in attestation.snowflake_user_roles)
        == (env["PILLARMESH_SNOWFLAKE_ROLE"].casefold(),)
        and attestation.snowflake_marker_environment_identity == config.environment_identity
        and attestation.snowflake_marker_owner_user.casefold()
        == env["PILLARMESH_SNOWFLAKE_OWNER_USER"].casefold()
        and attestation.snowflake_marker_owner_role.casefold() == snowflake_owner
        and attestation.snowflake_marker_denial_database.casefold()
        == env["PILLARMESH_SNOWFLAKE_DENIAL_DATABASE"].casefold()
        and attestation.snowflake_marker_denial_database_owner_role.casefold() == snowflake_owner
        and tuple((name, kind.upper()) for name, kind in attestation.snowflake_object_kinds)
        == SNOWFLAKE_OBJECT_KINDS
        and tuple((name, value.casefold()) for name, value in attestation.snowflake_object_owners)
        == tuple((name, value.casefold()) for name, value in expected_owners)
        and all(
            value == snowflake_owner
            for _name, value in tuple(
                (name, owner_value.casefold())
                for name, owner_value in attestation.snowflake_object_owners
            )
        )
        and tuple(
            sorted(
                (name, privilege.upper(), grantee.casefold())
                for name, privilege, grantee in attestation.snowflake_object_grants
            )
        )
        == tuple(
            (name, privilege, grantee.casefold())
            for name, privilege, grantee in expected_object_grants
        )
        and attestation.snowflake_runtime_grants == SNOWFLAKE_RUNTIME_GRANTS
    )
    if not dedicated:
        raise HarnessError("dedicated environment attestation failed")


def private_attestation_record(
    config: AcceptanceConfig, attestation: DedicatedEnvironmentAttestation
) -> dict[str, object]:
    env = config.environment
    identities = attestation.identities
    return {
        "status": "passed",
        "environment_identity": config.environment_identity,
        "declared": {
            "postgres_database": env["PILLARMESH_POSTGRES_DATABASE"],
            "postgres_runtime_principal": env["PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL"],
            "postgres_fixture_principal": env["PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL"],
            "postgres_owner_principal": env["PILLARMESH_POSTGRES_OWNER_PRINCIPAL"],
            "postgres_schema": env["PILLARMESH_POSTGRES_SCHEMA"],
            "postgres_table": env["PILLARMESH_POSTGRES_TABLE"],
            "postgres_denial_schema": env["PILLARMESH_POSTGRES_DENIAL_SCHEMA"],
            "snowflake_account": env["PILLARMESH_SNOWFLAKE_ACCOUNT"],
            "snowflake_runtime_user": env["PILLARMESH_SNOWFLAKE_USER"],
            "snowflake_owner_user": env["PILLARMESH_SNOWFLAKE_OWNER_USER"],
            "snowflake_role": env["PILLARMESH_SNOWFLAKE_ROLE"],
            "snowflake_warehouse": env["PILLARMESH_SNOWFLAKE_WAREHOUSE"],
            "snowflake_database": env["PILLARMESH_SNOWFLAKE_DATABASE"],
            "snowflake_schema": env["PILLARMESH_SNOWFLAKE_SCHEMA"],
            "snowflake_stage": env["PILLARMESH_SNOWFLAKE_STAGE"],
            "snowflake_target": env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"],
            "snowflake_negative_target": env["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"],
            "snowflake_ledger": env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"],
            "snowflake_denial_database": env["PILLARMESH_SNOWFLAKE_DENIAL_DATABASE"],
        },
        "observed": {
            "postgres_runtime_session": identities.postgres_runtime_session,
            "postgres_runtime_current": identities.postgres_runtime_current,
            "postgres_fixture_session": identities.postgres_fixture_session,
            "postgres_fixture_current": identities.postgres_fixture_current,
            "snowflake_runtime": identities.snowflake_runtime,
            "postgres_database": attestation.postgres_database,
            "postgres_database_owner": attestation.postgres_database_owner,
            "postgres_schema_owner": attestation.postgres_schema_owner,
            "postgres_marker_environment_id": attestation.postgres_marker_environment_id,
            "postgres_source_kind": attestation.postgres_source_kind,
            "postgres_marker_kind": attestation.postgres_marker_kind,
            "postgres_source_owner": attestation.postgres_source_owner,
            "postgres_marker_owner": attestation.postgres_marker_owner,
            "postgres_audit_kind": attestation.postgres_audit_kind,
            "postgres_audit_owner": attestation.postgres_audit_owner,
            "postgres_audit_security_definer": attestation.postgres_audit_security_definer,
            "postgres_audit_language": attestation.postgres_audit_language,
            "postgres_audit_volatility": attestation.postgres_audit_volatility,
            "postgres_audit_result": attestation.postgres_audit_result,
            "postgres_audit_config": attestation.postgres_audit_config,
            "postgres_audit_body_digest": attestation.postgres_audit_body_digest,
            "postgres_runtime_grants": attestation.postgres_runtime_grants,
            "postgres_fixture_grants": attestation.postgres_fixture_grants,
            "postgres_denial_schema_exists": attestation.postgres_denial_schema_exists,
            "snowflake_account": attestation.snowflake_account,
            "snowflake_account_locator": attestation.snowflake_account_locator,
            "snowflake_role": attestation.snowflake_role,
            "snowflake_user_roles": attestation.snowflake_user_roles,
            "snowflake_marker_environment_identity": (
                attestation.snowflake_marker_environment_identity
            ),
            "snowflake_marker_owner_user": attestation.snowflake_marker_owner_user,
            "snowflake_marker_owner_role": attestation.snowflake_marker_owner_role,
            "snowflake_marker_denial_database": (attestation.snowflake_marker_denial_database),
            "snowflake_marker_denial_database_owner_role": (
                attestation.snowflake_marker_denial_database_owner_role
            ),
            "snowflake_object_kinds": attestation.snowflake_object_kinds,
            "snowflake_object_owners": attestation.snowflake_object_owners,
            "snowflake_object_grants": attestation.snowflake_object_grants,
            "snowflake_runtime_grants": attestation.snowflake_runtime_grants,
        },
    }


def preflight(
    environment: Mapping[str, str],
    *,
    repository_root: Any,
    provider_factory: Callable[[AcceptanceConfig], ProviderActions],
) -> AcceptanceConfig:
    config = AcceptanceConfig.from_environment(environment, repository_root=repository_root)
    with EnvironmentReservation(config.reservation_path) as reservation:
        reservation.assert_intact()
        providers = provider_factory(config)
        with providers.admission(config.environment_identity):
            validate_attestation(config, providers.preflight())
        reservation.assert_intact()
    return config


class LiveProviderActions:
    def __init__(
        self,
        config: AcceptanceConfig,
        *,
        postgres_connect: Callable[..., Any] = psycopg.connect,
        snowflake_connect: Callable[..., Any] = snowflake.connector.connect,
    ) -> None:
        self._config = config
        self._environment = config.environment
        self._postgres_connect = postgres_connect
        self._snowflake_connect = snowflake_connect

    def _snowflake_connection(self) -> Any:
        env = self._environment
        return self._snowflake_connect(
            account=env["PILLARMESH_SNOWFLAKE_ACCOUNT"],
            user=env["PILLARMESH_SNOWFLAKE_USER"],
            password=env["PILLARMESH_SNOWFLAKE_PASSWORD"],
            role=env["PILLARMESH_SNOWFLAKE_ROLE"],
            warehouse=env["PILLARMESH_SNOWFLAKE_WAREHOUSE"],
            database=env["PILLARMESH_SNOWFLAKE_DATABASE"],
            schema=env["PILLARMESH_SNOWFLAKE_SCHEMA"],
            autocommit=True,
            session_parameters={"QUERY_TAG": "pillarmesh-m0-acceptance-observer"},
        )

    @contextmanager
    def admission(self, environment_identity: str) -> Iterator[ProviderAdmission]:
        if environment_identity != self._config.environment_identity:
            raise HarnessError("provider admission identity is invalid")
        raw = bytes.fromhex(environment_identity)
        lock_key = (
            int.from_bytes(raw[:4], "big", signed=True),
            int.from_bytes(raw[4:8], "big", signed=True),
        )
        connection = self._postgres_connect(self._environment["PILLARMESH_POSTGRES_DSN"])
        admission: _PostgresAdmission | None = None
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), pg_backend_pid(), pg_try_advisory_lock(%s, %s)",
                    lock_key,
                )
                database, backend_pid, admitted = _required_row(
                    cursor.fetchone(), "provider admission probe"
                )
                if str(database) != self._environment["PILLARMESH_POSTGRES_DATABASE"]:
                    raise HarnessError("provider admission database does not match")
                if not bool(admitted):
                    raise HarnessError("acceptance environment is already admitted")
                admission = _PostgresAdmission(
                    connection=connection,
                    expected_database=self._environment["PILLARMESH_POSTGRES_DATABASE"],
                    expected_backend_pid=int(backend_pid),
                )
            try:
                yield admission
                admission.assert_intact()
            except BaseException as error:
                # A release problem must never replace the failure that ended the run:
                # that failure is the diagnosis the operator acts on. Attach it to the
                # original instead, so neither is lost.
                try:
                    self._release_admission(connection, lock_key, admission)
                except Exception as release_error:
                    error.add_note(
                        f"provider admission release also failed: {_error_label(release_error)}"
                    )
                raise
            else:
                self._release_admission(connection, lock_key, admission)
        finally:
            connection.close()

    def _release_admission(
        self,
        connection: Any,
        lock_key: tuple[int, int],
        admission: _PostgresAdmission | None,
    ) -> None:
        if admission is None or admission.lost:
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", lock_key)
            unlocked = _required_row(cursor.fetchone(), "provider admission release")[0]
        if not bool(unlocked):
            raise HarnessError("provider admission release failed")

    def _qualified(self, name: str) -> str:
        env = self._environment
        return f"{env['PILLARMESH_SNOWFLAKE_DATABASE']}.{env['PILLARMESH_SNOWFLAKE_SCHEMA']}.{name}"

    def _postgres_grants(self, cursor: Any, principal: str) -> tuple[str, ...]:
        env = self._environment
        function = f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_AUDIT_FUNCTION}()"
        checks = {
            "CONNECT_DATABASE": (
                "has_database_privilege(%s, %s, 'CONNECT')",
                (principal, env["PILLARMESH_POSTGRES_DATABASE"]),
            ),
            "CREATE_DATABASE": (
                "has_database_privilege(%s, %s, 'CREATE')",
                (principal, env["PILLARMESH_POSTGRES_DATABASE"]),
            ),
            "TEMP_DATABASE": (
                "has_database_privilege(%s, %s, 'TEMPORARY')",
                (principal, env["PILLARMESH_POSTGRES_DATABASE"]),
            ),
            "USAGE_SCHEMA": (
                "has_schema_privilege(%s, %s, 'USAGE')",
                (principal, env["PILLARMESH_POSTGRES_SCHEMA"]),
            ),
            "SELECT_SOURCE": (
                "has_table_privilege(%s, %s, 'SELECT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "INSERT_SOURCE": (
                "has_table_privilege(%s, %s, 'INSERT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "UPDATE_SOURCE": (
                "has_table_privilege(%s, %s, 'UPDATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "DELETE_SOURCE": (
                "has_table_privilege(%s, %s, 'DELETE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "TRUNCATE_SOURCE": (
                "has_table_privilege(%s, %s, 'TRUNCATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "REFERENCES_SOURCE": (
                "has_table_privilege(%s, %s, 'REFERENCES')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "TRIGGER_SOURCE": (
                "has_table_privilege(%s, %s, 'TRIGGER')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "MAINTAIN_SOURCE": (
                "has_table_privilege(%s, %s, 'MAINTAIN')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "COLUMN_INSERT_SOURCE": (
                "has_any_column_privilege(%s, %s, 'INSERT') "
                "AND NOT has_table_privilege(%s, %s, 'INSERT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "COLUMN_UPDATE_SOURCE": (
                "has_any_column_privilege(%s, %s, 'UPDATE') "
                "AND NOT has_table_privilege(%s, %s, 'UPDATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "COLUMN_REFERENCES_SOURCE": (
                "has_any_column_privilege(%s, %s, 'REFERENCES') "
                "AND NOT has_table_privilege(%s, %s, 'REFERENCES')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}",
                ),
            ),
            "CREATE_SCHEMA_OBJECT": (
                "has_schema_privilege(%s, %s, 'CREATE')",
                (principal, env["PILLARMESH_POSTGRES_SCHEMA"]),
            ),
            "SELECT_MARKER": (
                "has_table_privilege(%s, %s, 'SELECT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "COLUMN_SELECT_MARKER": (
                "has_any_column_privilege(%s, %s, 'SELECT') "
                "AND NOT has_table_privilege(%s, %s, 'SELECT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "INSERT_MARKER": (
                "has_table_privilege(%s, %s, 'INSERT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "UPDATE_MARKER": (
                "has_table_privilege(%s, %s, 'UPDATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "DELETE_MARKER": (
                "has_table_privilege(%s, %s, 'DELETE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "TRUNCATE_MARKER": (
                "has_table_privilege(%s, %s, 'TRUNCATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "REFERENCES_MARKER": (
                "has_table_privilege(%s, %s, 'REFERENCES')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "TRIGGER_MARKER": (
                "has_table_privilege(%s, %s, 'TRIGGER')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "MAINTAIN_MARKER": (
                "has_table_privilege(%s, %s, 'MAINTAIN')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "COLUMN_INSERT_MARKER": (
                "has_any_column_privilege(%s, %s, 'INSERT') "
                "AND NOT has_table_privilege(%s, %s, 'INSERT')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "COLUMN_UPDATE_MARKER": (
                "has_any_column_privilege(%s, %s, 'UPDATE') "
                "AND NOT has_table_privilege(%s, %s, 'UPDATE')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "COLUMN_REFERENCES_MARKER": (
                "has_any_column_privilege(%s, %s, 'REFERENCES') "
                "AND NOT has_table_privilege(%s, %s, 'REFERENCES')",
                (
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                    principal,
                    f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{POSTGRES_MARKER_TABLE}",
                ),
            ),
            "EXECUTE_AUDIT": ("has_function_privilege(%s, %s, 'EXECUTE')", (principal, function)),
        }
        source_table = f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}"
        for column in (
            "order_id",
            "customer_ref",
            "amount",
            "currency",
            "status",
            "updated_at",
        ):
            label = (
                "SELECT_SOURCE_KEY_COLUMN"
                if column == "order_id"
                else f"SELECT_SOURCE_{column.upper()}_COLUMN"
            )
            checks[label] = (
                "has_column_privilege(%s, %s, %s, 'SELECT') "
                "AND NOT has_table_privilege(%s, %s, 'SELECT')",
                (principal, source_table, column, principal, source_table),
            )
        present: list[str] = []
        for label, (expression, parameters) in checks.items():
            cursor.execute(f"SELECT {expression}", parameters)
            if bool(_required_row(cursor.fetchone(), "PostgreSQL grant probe")[0]):
                present.append(label)
        return tuple(sorted(present))

    def _postgres_objects(self, cursor: Any) -> tuple[tuple[str, str], tuple[str, str]]:
        env = self._environment
        cursor.execute(
            "SELECT c.relname, c.relkind, pg_catalog.pg_get_userbyid(c.relowner) "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname IN (%s,%s)",
            (
                env["PILLARMESH_POSTGRES_SCHEMA"],
                env["PILLARMESH_POSTGRES_TABLE"],
                POSTGRES_MARKER_TABLE,
            ),
        )
        values = {
            str(row[0]): ("base_table" if row[1] == "r" else str(row[1]), str(row[2]))
            for row in cursor.fetchall()
        }
        try:
            return (
                values[env["PILLARMESH_POSTGRES_TABLE"]],
                values[POSTGRES_MARKER_TABLE],
            )
        except KeyError:
            raise HarnessError("PostgreSQL dedicated marker or source table is absent") from None

    def _postgres_audit_function(
        self, cursor: Any
    ) -> tuple[str, str, bool, str, str, str, tuple[str, ...], str]:
        env = self._environment
        cursor.execute(
            "SELECT p.prokind, pg_catalog.pg_get_userbyid(p.proowner), p.prosecdef, "
            "l.lanname, p.provolatile, pg_catalog.pg_get_function_result(p.oid), "
            "coalesce(p.proconfig, ARRAY[]::text[]), p.prosrc "
            "FROM pg_catalog.pg_proc p "
            "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_catalog.pg_language l ON l.oid=p.prolang "
            "WHERE n.nspname=%s AND p.proname=%s AND p.pronargs=0",
            (
                env["PILLARMESH_POSTGRES_SCHEMA"],
                POSTGRES_AUDIT_FUNCTION,
            ),
        )
        row = _required_row(cursor.fetchone(), "PostgreSQL audit function attestation")
        return (
            "function" if row[0] == "f" else str(row[0]),
            str(row[1]),
            bool(row[2]),
            str(row[3]),
            "stable" if row[4] == "s" else str(row[4]),
            str(row[5]),
            tuple(str(value) for value in row[6]),
            _sql_shape_digest(str(row[7])),
        )

    @staticmethod
    def _rows_as_dicts(cursor: Any) -> tuple[dict[str, object], ...]:
        names = tuple(str(item[0]).lower() for item in cursor.description)
        return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())

    def _snowflake_grants_on(
        self, cursor: Any, object_type: str, identifier: str
    ) -> tuple[tuple[str, str], ...]:
        cursor.execute(f"SHOW GRANTS ON {object_type} {identifier}")
        rows = self._rows_as_dicts(cursor)
        return tuple(sorted((str(row["privilege"]), str(row["grantee_name"])) for row in rows))

    def _snowflake_attestation(
        self, cursor: Any
    ) -> tuple[
        str,
        str,
        str,
        tuple[str, ...],
        tuple[str, str, str, str, str],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str, str], ...],
        tuple[str, ...],
    ]:
        env = self._environment
        cursor.execute(
            "SELECT CURRENT_USER(), CURRENT_ACCOUNT_NAME(), CURRENT_ACCOUNT(), CURRENT_ROLE()"
        )
        _user, account, account_locator, role = _required_row(
            cursor.fetchone(), "Snowflake identity probe"
        )
        cursor.execute(f"SHOW GRANTS TO USER {env['PILLARMESH_SNOWFLAKE_USER']}")
        user_grants = self._rows_as_dicts(cursor)
        user_roles = tuple(
            sorted(
                str(row["role"])
                for row in user_grants
                if row.get("granted_to") == "USER" and row.get("role") is not None
            )
        )
        objects = {
            "database": ("DATABASE", env["PILLARMESH_SNOWFLAKE_DATABASE"]),
            "environment_marker": (
                "TABLE",
                self._qualified(SNOWFLAKE_ENVIRONMENT_MARKER),
            ),
            "file_format": (
                "FILE FORMAT",
                self._qualified(SNOWFLAKE_FILE_FORMAT),
            ),
            "schema": (
                "SCHEMA",
                f"{env['PILLARMESH_SNOWFLAKE_DATABASE']}.{env['PILLARMESH_SNOWFLAKE_SCHEMA']}",
            ),
            "warehouse": ("WAREHOUSE", env["PILLARMESH_SNOWFLAKE_WAREHOUSE"]),
            "stage": ("STAGE", self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"])),
            "target": ("TABLE", self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])),
            "negative_target": (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"]),
            ),
            "ledger": ("TABLE", self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"])),
        }
        owners: list[tuple[str, str]] = []
        object_grants: list[tuple[str, str, str]] = []
        for name, (kind, identifier) in objects.items():
            grants_on = self._snowflake_grants_on(cursor, kind, identifier)
            object_grants.extend((name, privilege, grantee) for privilege, grantee in grants_on)
            object_owners = [
                grantee for privilege, grantee in grants_on if privilege == "OWNERSHIP"
            ]
            if len(object_owners) != 1:
                raise HarnessError("Snowflake object ownership attestation failed")
            owners.append((name, object_owners[0]))
        cursor.execute(f"SHOW GRANTS TO ROLE {env['PILLARMESH_SNOWFLAKE_ROLE']}")
        rows = self._rows_as_dicts(cursor)
        expected = {
            ("WAREHOUSE", env["PILLARMESH_SNOWFLAKE_WAREHOUSE"], "USAGE"): "USAGE_WAREHOUSE",
            ("DATABASE", env["PILLARMESH_SNOWFLAKE_DATABASE"], "USAGE"): "USAGE_DATABASE",
            (
                "SCHEMA",
                f"{env['PILLARMESH_SNOWFLAKE_DATABASE']}.{env['PILLARMESH_SNOWFLAKE_SCHEMA']}",
                "USAGE",
            ): "USAGE_SCHEMA",
            ("STAGE", self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"]), "READ"): "READ_STAGE",
            ("STAGE", self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"]), "WRITE"): "WRITE_STAGE",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]),
                "SELECT",
            ): "SELECT_TARGET",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]),
                "INSERT",
            ): "INSERT_TARGET",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]),
                "UPDATE",
            ): "UPDATE_TARGET",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"]),
                "SELECT",
            ): "SELECT_NEGATIVE_TARGET",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"]),
                "SELECT",
            ): "SELECT_LEDGER",
            (
                "TABLE",
                self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"]),
                "INSERT",
            ): "INSERT_LEDGER",
            (
                "TABLE",
                self._qualified(SNOWFLAKE_ENVIRONMENT_MARKER),
                "SELECT",
            ): "SELECT_ENVIRONMENT_MARKER",
        }
        grants: list[str] = []
        for row in rows:
            key = (
                str(row.get("granted_on")),
                str(row.get("name")),
                str(row.get("privilege")),
            )
            known = expected.get(key)
            grants.append(known if known is not None else "UNEXPECTED_GRANT")
        cursor.execute(
            "SELECT environment_identity, owner_user, owner_role, "
            "denial_database, denial_database_owner_role "
            f"FROM {self._qualified(SNOWFLAKE_ENVIRONMENT_MARKER)}"
        )
        marker_rows = tuple(cursor.fetchall())
        if len(marker_rows) != 1:
            raise HarnessError("Snowflake environment marker attestation failed")
        marker = tuple(str(value) for value in marker_rows[0])
        return (
            str(account),
            str(account_locator),
            str(role),
            user_roles,
            cast(tuple[str, str, str, str, str], marker),
            SNOWFLAKE_OBJECT_KINDS,
            tuple(sorted(owners)),
            tuple(sorted(object_grants)),
            tuple(sorted(grants)),
        )

    def preflight(self) -> DedicatedEnvironmentAttestation:
        env = self._environment
        with (
            self._postgres_connect(env["PILLARMESH_POSTGRES_DSN"]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT session_user, current_user, current_database(), "
                "pg_catalog.pg_get_userbyid(d.datdba) "
                "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
            )
            runtime_session_user, runtime_current_user, database, database_owner = _required_row(
                cursor.fetchone(), "PostgreSQL runtime identity probe"
            )
            cursor.execute(
                "SELECT pg_catalog.pg_get_userbyid(n.nspowner) "
                "FROM pg_catalog.pg_namespace n WHERE n.nspname=%s",
                (env["PILLARMESH_POSTGRES_SCHEMA"],),
            )
            schema_owner = str(
                _required_row(cursor.fetchone(), "PostgreSQL schema ownership probe")[0]
            )
            source, marker = self._postgres_objects(cursor)
            (
                audit_kind,
                audit_owner,
                audit_security_definer,
                audit_language,
                audit_volatility,
                audit_result,
                audit_config,
                audit_body_digest,
            ) = self._postgres_audit_function(cursor)
            cursor.execute(
                sql.SQL("SELECT environment_id FROM {}.{}").format(
                    sql.Identifier(env["PILLARMESH_POSTGRES_SCHEMA"]),
                    sql.Identifier(POSTGRES_MARKER_TABLE),
                )
            )
            marker_environment = str(_required_row(cursor.fetchone(), "PostgreSQL marker probe")[0])
            runtime_grants = self._postgres_grants(cursor, str(runtime_current_user))
            cursor.execute(
                "SELECT has_schema_privilege(current_user, %s, 'USAGE')",
                (env["PILLARMESH_POSTGRES_DENIAL_SCHEMA"],),
            )
            if bool(_required_row(cursor.fetchone(), "PostgreSQL denial probe")[0]):
                raise HarnessError("PostgreSQL expected-denial probe unexpectedly succeeded")
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s)",
                (env["PILLARMESH_POSTGRES_DENIAL_SCHEMA"],),
            )
            denial_schema_exists = bool(
                _required_row(cursor.fetchone(), "PostgreSQL denial schema existence probe")[0]
            )
        with (
            self._postgres_connect(env["PILLARMESH_POSTGRES_FIXTURE_DSN"]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT session_user, current_user, current_database()")
            fixture_session_user, fixture_current_user, fixture_database = _required_row(
                cursor.fetchone(), "PostgreSQL fixture identity probe"
            )
            if str(fixture_database) != str(database):
                raise HarnessError("PostgreSQL fixture database does not match runtime database")
            fixture_grants = self._postgres_grants(cursor, str(fixture_current_user))
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            (
                account,
                account_locator,
                role,
                user_roles,
                snowflake_marker,
                kinds,
                owners,
                object_grants,
                snowflake_grants,
            ) = self._snowflake_attestation(cursor)
            cursor.execute("SELECT CURRENT_USER()")
            snowflake_user = str(_required_row(cursor.fetchone(), "Snowflake identity probe")[0])
            target = self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])
            cursor.execute(
                f"SELECT COUNT(*) FROM {target} WHERE order_id=%s",
                (-1,),
            )
            cursor.fetchone()
            denied = f"{env['PILLARMESH_SNOWFLAKE_DENIAL_DATABASE']}.INFORMATION_SCHEMA.TABLES"
            try:
                cursor.execute("SELECT COUNT(*) FROM IDENTIFIER(%s)", (denied,))
                cursor.fetchone()
            except Exception as error:
                if not snowflake_access_denied(error):
                    raise HarnessError("Snowflake denial probe was inconclusive") from None
            else:
                raise HarnessError("Snowflake expected-denial probe unexpectedly succeeded")
        return DedicatedEnvironmentAttestation(
            identities=RuntimeIdentities(
                postgres_runtime_session=str(runtime_session_user),
                postgres_runtime_current=str(runtime_current_user),
                postgres_fixture_session=str(fixture_session_user),
                postgres_fixture_current=str(fixture_current_user),
                snowflake_runtime=snowflake_user,
            ),
            postgres_database=str(database),
            postgres_database_owner=str(database_owner),
            postgres_schema_owner=schema_owner,
            postgres_marker_environment_id=marker_environment,
            postgres_source_kind=source[0],
            postgres_marker_kind=marker[0],
            postgres_source_owner=source[1],
            postgres_marker_owner=marker[1],
            postgres_audit_kind=audit_kind,
            postgres_audit_owner=audit_owner,
            postgres_audit_security_definer=audit_security_definer,
            postgres_audit_language=audit_language,
            postgres_audit_volatility=audit_volatility,
            postgres_audit_result=audit_result,
            postgres_audit_config=audit_config,
            postgres_audit_body_digest=audit_body_digest,
            postgres_runtime_grants=runtime_grants,
            postgres_fixture_grants=fixture_grants,
            postgres_denial_schema_exists=denial_schema_exists,
            snowflake_account=account,
            snowflake_account_locator=account_locator,
            snowflake_role=role,
            snowflake_user_roles=user_roles,
            snowflake_marker_environment_identity=snowflake_marker[0],
            snowflake_marker_owner_user=snowflake_marker[1],
            snowflake_marker_owner_role=snowflake_marker[2],
            snowflake_marker_denial_database=snowflake_marker[3],
            snowflake_marker_denial_database_owner_role=snowflake_marker[4],
            snowflake_object_kinds=kinds,
            snowflake_object_owners=owners,
            snowflake_object_grants=object_grants,
            snowflake_runtime_grants=snowflake_grants,
        )

    def prove_destination_absent(self, acceptance_key: int) -> None:
        target = self._qualified(self._environment["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM {target} WHERE order_id=%s",
                (acceptance_key,),
            )
            if int(_required_row(cursor.fetchone(), "destination absence probe")[0]) != 0:
                raise HarnessError("fresh acceptance key is already present in destination")

    def insert_fixture(self, row: object) -> None:
        if not isinstance(row, FixtureRow):
            raise HarnessError("fixture row is invalid")
        env = self._environment
        with self._postgres_connect(env["PILLARMESH_POSTGRES_FIXTURE_DSN"]) as connection:
            connection.execute(
                sql.SQL(
                    "INSERT INTO {}.{} "
                    "(order_id,customer_ref,amount,currency,status,updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s)"
                ).format(
                    sql.Identifier(env["PILLARMESH_POSTGRES_SCHEMA"]),
                    sql.Identifier(env["PILLARMESH_POSTGRES_TABLE"]),
                ),
                (
                    row.order_id,
                    row.customer_ref,
                    row.amount,
                    row.currency,
                    row.status,
                    row.updated_at,
                ),
            )

    def _destination_rows(self, cursor: Any, acceptance_key: int) -> tuple[tuple[Any, ...], ...]:
        target = self._qualified(self._environment["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])
        cursor.execute(
            "SELECT order_id,customer_ref,amount,currency,order_status,updated_at "
            f"FROM {target} WHERE order_id=%s",
            (acceptance_key,),
        )
        return cast(tuple[tuple[Any, ...], ...], tuple(cursor.fetchall()))

    @staticmethod
    def _row_digest(raw: tuple[Any, ...]) -> str:
        return digest(
            OrderRow.model_validate(
                dict(
                    zip(
                        ("order_id", "customer_ref", "amount", "currency", "status", "updated_at"),
                        raw,
                        strict=True,
                    )
                )
            )
        )

    def verify_destination(self, acceptance_key: int) -> VisibilityObservation:
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            rows = self._destination_rows(cursor, acceptance_key)
            query_id = str(cursor.sfqid)
        if len(rows) != 1:
            raise HarnessError("acceptance destination cardinality is not exactly one")
        return VisibilityObservation(self._row_digest(rows[0]), query_id)

    @staticmethod
    def _product_query_history(cursor: Any) -> tuple[tuple[str, str], ...]:
        cursor.execute(
            "SELECT QUERY_TYPE, QUERY_TEXT FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER("
            "USER_NAME=>CURRENT_USER(),RESULT_LIMIT=>10000)) "
            "WHERE QUERY_TAG='pillarmesh-m0'"
        )
        return tuple((str(row[0]).upper(), str(row[1])) for row in cursor.fetchall())

    def _product_mutation_count(self, cursor: Any) -> int:
        env = self._environment
        identifiers = tuple(
            value.casefold()
            for value in (
                self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]),
                self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"]),
                self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"]),
            )
        )
        mutation_types = {"MERGE", "UPDATE", "INSERT", "DELETE", "PUT", "REMOVE", "COPY"}
        return sum(
            query_type in mutation_types
            and any(identifier in query_text.casefold() for identifier in identifiers)
            for query_type, query_text in self._product_query_history(cursor)
        )

    def replay_observation(self, acceptance_key: int, batch_id: str) -> ReplayObservation:
        env = self._environment
        ledger_table = self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"])
        stage = self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"])
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            target_rows = self._destination_rows(cursor, acceptance_key)
            cursor.execute(
                f"SELECT manifest_digest,committed_at FROM {ledger_table} WHERE batch_id=%s",
                (batch_id,),
            )
            ledger_rows = tuple(cursor.fetchall())
            cursor.execute(f"LIST @{stage}/runs/{batch_id}")
            stage_state = tuple(tuple(row) for row in cursor.fetchall())
            mutation_count = self._product_mutation_count(cursor)
        if not target_rows or not ledger_rows:
            raise HarnessError("replay destination state is incomplete")
        raw = target_rows[0]
        ledger = ledger_rows[0]
        committed = ledger[1]
        if isinstance(committed, datetime) and committed.tzinfo is None:
            committed = committed.replace(tzinfo=UTC)
        return ReplayObservation(
            target_rows=len(target_rows),
            target_value_digest=self._row_digest(raw),
            ledger_rows=len(ledger_rows),
            ledger_manifest_digest=str(ledger[0]),
            ledger_committed_identity=digest({"batch_id": batch_id, "committed_at": committed}),
            stage_state_digest=self._state_digest(stage_state),
            product_mutation_count=mutation_count,
        )

    @staticmethod
    def _state_digest(rows: object) -> str:
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def negative_observation(self) -> NegativeObservation:
        env = self._environment
        negative_target = self._qualified(env["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"])
        positive_target = self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])
        stage = self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"])
        ledger_table = self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"])
        with (
            self._postgres_connect(env["PILLARMESH_POSTGRES_DSN"]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("SELECT {}.{}()").format(
                    sql.Identifier(env["PILLARMESH_POSTGRES_SCHEMA"]),
                    sql.Identifier(POSTGRES_AUDIT_FUNCTION),
                )
            )
            source_reads = int(
                _required_row(cursor.fetchone(), "PostgreSQL persistent read audit")[0]
            )
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*),HASH_AGG(*) FROM {positive_target}")
            positive_state = tuple(cursor.fetchone())
            cursor.execute(f"SELECT COUNT(*),HASH_AGG(*) FROM {negative_target}")
            negative_state = tuple(cursor.fetchone())
            cursor.execute(f"LIST @{stage}")
            stage_state = tuple(tuple(row) for row in cursor.fetchall())
            cursor.execute(f"SELECT COUNT(*),HASH_AGG(*) FROM {ledger_table}")
            ledger_state = tuple(cursor.fetchone())
            history = self._product_query_history(cursor)
            observed_types = {
                "SELECT",
                "MERGE",
                "UPDATE",
                "INSERT",
                "DELETE",
                "PUT",
                "REMOVE",
                "COPY",
            }
            product_queries = sum(
                query_type in observed_types and not _is_allowed_metadata_query(query_text)
                for query_type, query_text in history
            )
            metadata_queries = sum(
                query_type == "SELECT" and _is_allowed_metadata_query(query_text)
                for query_type, query_text in history
            )
        return NegativeObservation(
            self._state_digest(positive_state),
            self._state_digest(negative_state),
            self._state_digest(stage_state),
            self._state_digest(ledger_state),
            source_reads,
            product_queries,
            metadata_queries,
        )

    def reconcile_resources(
        self, acceptance_key: int, batch_id: str | None
    ) -> ProviderResourceState:
        env = self._environment
        target = self._qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"])
        with (
            self._postgres_connect(env["PILLARMESH_POSTGRES_DSN"]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {}.{} WHERE order_id=%s)").format(
                    sql.Identifier(env["PILLARMESH_POSTGRES_SCHEMA"]),
                    sql.Identifier(env["PILLARMESH_POSTGRES_TABLE"]),
                ),
                (acceptance_key,),
            )
            source = bool(_required_row(cursor.fetchone(), "source reconciliation")[0])
        if batch_id is None:
            return ProviderResourceState(source, None, None, None)
        ledger_table = self._qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"])
        stage = self._qualified(env["PILLARMESH_SNOWFLAKE_STAGE"])
        with closing(self._snowflake_connection()) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT EXISTS (SELECT 1 FROM {target} WHERE order_id=%s)",
                (acceptance_key,),
            )
            target_exists = bool(_required_row(cursor.fetchone(), "target reconciliation")[0])
            cursor.execute(
                f"SELECT EXISTS (SELECT 1 FROM {ledger_table} WHERE batch_id=%s)",
                (batch_id,),
            )
            ledger = bool(_required_row(cursor.fetchone(), "ledger reconciliation")[0])
            cursor.execute(f"LIST @{stage}/runs/{batch_id}")
            staged = cursor.fetchone() is not None
        return ProviderResourceState(source, target_exists, ledger, staged)
