from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Any

import psycopg
from pillarmesh_contract_model import digest
from psycopg import sql

from .warehouse_settings import POSTGRESQL_SERVER_VERSION_NUM

_DATABASE_NAME = "pillarmesh_warehouse"
_PRINCIPALS = (
    "administration",
    "ingestion_runtime",
    "transformation_runtime",
    "backup_restore",
    "customer_sql",
    "catalog",
    "bi",
)
_NAMESPACES = ("raw", "conformed", "product", "consumption", "quarantine", "control")
_TABLE_SPECS = (
    ("raw", "ingestion_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    ("conformed", "transformation_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    ("product", "product_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    ("consumption", "customer_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    ("consumption", "certified_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    ("quarantine", "quarantine_probe", "id integer PRIMARY KEY, payload text NOT NULL"),
    (
        "control",
        "lifecycle_ledger",
        "operation_marker text PRIMARY KEY, marker_digest text NOT NULL",
    ),
    ("control", "ingestion_append_ledger", "marker_digest text NOT NULL"),
)


@dataclass(frozen=True, slots=True)
class PostgreSQLConnectionTarget:
    host: str
    port: int
    root_certificate: Path
    client_certificate: Path
    client_private_key: Path


@dataclass(frozen=True, slots=True)
class PostgreSQLGrantPlan:
    prefix: str

    def role(self, principal: str) -> str:
        if principal not in _PRINCIPALS:
            raise ValueError("unknown PostgreSQL warehouse principal")
        return f"{self.prefix}_{principal}"

    def probe_role(self, principal: str) -> str:
        return f"{self.role(principal)}_probe"

    def namespace(self, namespace: str) -> str:
        if namespace not in _NAMESPACES:
            raise ValueError("unknown PostgreSQL warehouse namespace")
        return f"{self.prefix}_{namespace}"

    @property
    def unrelated_namespace(self) -> str:
        return f"{self.prefix}_unrelated"

    @property
    def unrelated_database(self) -> str:
        return f"{self.prefix}_unrelated_database"

    @property
    def administration_test_role(self) -> str:
        return f"{self.prefix}_administration_test"


@dataclass(frozen=True, slots=True)
class PostgreSQLDatabaseObservation:
    engine_version: str
    engine_build_digest: str
    principal_profile_digest: str
    namespace_grant_matrix_digest: str
    tls_probe_digest: str
    positive_probe_digest: str
    denial_probe_digest: str
    ledger_probe_digest: str
    monitoring_probe_digest: str
    storage_integrity_probe_digest: str
    representative_data_digest: str
    schema_metadata_digest: str
    integrity_marker_digest: str
    query_behavior_digest: str


type _Connect = Callable[..., Any]
_CREDENTIAL_DENIAL_ERRORS = (
    psycopg.errors.InvalidPassword,
    psycopg.errors.InvalidAuthorizationSpecification,
)
_DATABASE_DENIAL_ERRORS = (*_CREDENTIAL_DENIAL_ERRORS, psycopg.errors.InsufficientPrivilege)


class PostgreSQLProbeCleanupError(RuntimeError):
    def __init__(self, *, surviving_roles: tuple[str, ...]) -> None:
        super().__init__("PostgreSQL temporary probe login cleanup failed")
        self.surviving_roles = surviving_roles


def derive_grant_plan(private_resource_handle: str) -> PostgreSQLGrantPlan:
    return PostgreSQLGrantPlan(prefix="pm_" + digest(private_resource_handle)[:16])


def connect_tls(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    user: str,
    password: str,
    database_name: str = _DATABASE_NAME,
) -> Any:
    return connect(
        dbname=database_name,
        user=user,
        password=password,
        host=target.host,
        port=target.port,
        sslmode="verify-full",
        sslrootcert=str(target.root_certificate),
        sslcert=str(target.client_certificate),
        sslkey=str(target.client_private_key),
        connect_timeout=5,
    )


def wait_for_tls_connection(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    user: str,
    password: str,
    timeout_seconds: float = 120,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            connection = connect_tls(connect, target, user=user, password=password)
            connection.autocommit = True
            return connection
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def assert_plaintext_connection_denied(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    user: str,
    password: str,
    denial_connect: _Connect | None = None,
) -> None:
    probe_connect = denial_connect or connect
    for attempt in range(2):
        try:
            connection = probe_connect(
                dbname=_DATABASE_NAME,
                user=user,
                password=password,
                host=target.host,
                port=target.port,
                sslmode="disable",
                connect_timeout=5,
            )
        except _CREDENTIAL_DENIAL_ERRORS:
            if attempt == 0:
                with closing(connect_tls(connect, target, user=user, password=password)):
                    pass
                continue
            return
        with closing(connection):
            raise RuntimeError("PostgreSQL warehouse accepted a plaintext connection")
    raise RuntimeError("PostgreSQL plaintext denial probe was incomplete")


def assert_password_connection_denied(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    user: str,
    password: str,
    active_password: str,
    denial_connect: _Connect | None = None,
) -> None:
    probe_connect = denial_connect or connect
    for attempt in range(2):
        try:
            connection = connect_tls(probe_connect, target, user=user, password=password)
        except _CREDENTIAL_DENIAL_ERRORS:
            if attempt == 0:
                with closing(connect_tls(connect, target, user=user, password=active_password)):
                    pass
                continue
            return
        with closing(connection):
            raise RuntimeError("PostgreSQL warehouse accepted a retired bootstrap credential")
    raise RuntimeError("PostgreSQL bootstrap credential denial probe was incomplete")


def rotate_bootstrap_password(connection: Any, administration_password: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER ROLE postgres PASSWORD {}").format(sql.Literal(administration_password))
        )


def prepare_database(connection: Any, plan: PostgreSQLGrantPlan) -> None:
    _create_unrelated_database(connection, plan)
    with connection.cursor() as cursor:
        _create_roles(cursor, plan)
        _create_namespaces(cursor, plan)
        _create_probe_tables(cursor, plan)
        _remove_default_access(cursor, plan)
        _apply_grants(cursor, plan)
        _seed_representative_data(cursor, plan)


def prepare_restored_database(connection: Any, plan: PostgreSQLGrantPlan) -> None:
    _create_unrelated_database(connection, plan)
    with connection.cursor() as cursor:
        _assert_restored_objects(cursor, plan)
        _restore_canonical_ownership(cursor, plan)
        _remove_default_access(cursor, plan)
        _apply_grants(cursor, plan)


def create_canonical_roles(connection: Any, plan: PostgreSQLGrantPlan) -> None:
    with connection.cursor() as cursor:
        _create_roles(cursor, plan)


def create_probe_logins(
    connection: Any,
    plan: PostgreSQLGrantPlan,
    passwords: Mapping[str, str],
) -> None:
    for principal, password in passwords.items():
        if principal not in _PRINCIPALS:
            raise ValueError("unknown PostgreSQL warehouse probe principal")
        probe_role = plan.probe_role(principal)
        _ensure_role(connection, probe_role, login=True)
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(probe_role),
                    sql.Literal(password),
                )
            )
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(plan.role(principal)),
                    sql.Identifier(probe_role),
                )
            )


def inspect_probe_logins(connection: Any, plan: PostgreSQLGrantPlan) -> tuple[str, ...]:
    probe_roles = (
        *(plan.probe_role(principal) for principal in _PRINCIPALS),
        plan.administration_test_role,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname FROM pg_catalog.pg_roles "
            "WHERE rolname = ANY(%s) AND rolcanlogin ORDER BY rolname",
            (list(probe_roles),),
        )
        return tuple(str(row[0]) for row in cursor.fetchall())


def drop_probe_logins(connection: Any, plan: PostgreSQLGrantPlan) -> None:
    failures: list[Exception] = []
    for principal in _PRINCIPALS:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(
                        sql.Identifier(plan.probe_role(principal))
                    )
                )
        except Exception as error:
            failures.append(error)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(plan.administration_test_role)
                )
            )
    except Exception as error:
        failures.append(error)
    try:
        surviving_roles = inspect_probe_logins(connection, plan)
    except Exception as error:
        failures.append(error)
        surviving_roles = ()
    if failures or surviving_roles:
        raise PostgreSQLProbeCleanupError(surviving_roles=surviving_roles)


@contextmanager
def probe_login_scope(
    connection: Any,
    plan: PostgreSQLGrantPlan,
    passwords: Mapping[str, str],
) -> Iterator[None]:
    primary_failure: BaseException | None = None
    primary_traceback: TracebackType | None = None
    try:
        create_probe_logins(connection, plan, passwords)
        yield
    except BaseException as error:
        primary_failure = error
        primary_traceback = error.__traceback__
    try:
        drop_probe_logins(connection, plan)
    except Exception:
        if primary_failure is None:
            raise
        primary_failure.add_note("PostgreSQL probe cleanup also failed")
    if primary_failure is not None:
        raise primary_failure.with_traceback(primary_traceback) from None


def with_probe_login_cleanup_evidence(
    connection: Any,
    plan: PostgreSQLGrantPlan,
    observation: PostgreSQLDatabaseObservation,
) -> PostgreSQLDatabaseObservation:
    surviving_roles = inspect_probe_logins(connection, plan)
    if surviving_roles:
        raise PostgreSQLProbeCleanupError(surviving_roles=surviving_roles)
    return replace(
        observation,
        denial_probe_digest=digest(
            {
                "domain": "pillarmesh-postgresql-probe-cleanup-evidence-v1",
                "probe_denial_digest": observation.denial_probe_digest,
                "temporary_probe_logins_absent": True,
            }
        ),
    )


def observe_database(
    connection: Any,
    plan: PostgreSQLGrantPlan,
    *,
    connect: _Connect,
    denial_connect: _Connect | None = None,
    target: PostgreSQLConnectionTarget,
    probe_passwords: Mapping[str, str],
) -> PostgreSQLDatabaseObservation:
    version, tls_summary, monitoring_summary = _observe_engine(connection)
    if version["server_version_num"] != POSTGRESQL_SERVER_VERSION_NUM:
        raise RuntimeError("PostgreSQL warehouse server version is not the pinned version")
    principal_summary = _principal_summary(connection, plan)
    grant_summary = _grant_summary(connection, plan)
    public_schema_summary = _public_schema_summary(connection, plan)
    ledger_summary = _ledger_replay_summary(connection, plan)
    representative_summary = _representative_data_summary(connection, plan)
    schema_summary = _schema_summary(connection, plan)
    positive_summary, denial_summary, query_summary = _probe_summaries(
        connect,
        target,
        plan,
        probe_passwords,
        denial_connect=denial_connect,
    )
    return PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest=digest(
            {
                "domain": "pillarmesh-postgresql-engine-build-v1",
                "version": version,
            }
        ),
        principal_profile_digest=digest(principal_summary),
        namespace_grant_matrix_digest=digest(
            {"governed": grant_summary, "public": public_schema_summary}
        ),
        tls_probe_digest=digest(tls_summary),
        positive_probe_digest=digest(positive_summary),
        denial_probe_digest=digest(denial_summary),
        ledger_probe_digest=digest(ledger_summary),
        monitoring_probe_digest=digest(monitoring_summary),
        storage_integrity_probe_digest=digest(
            {
                "domain": "pillarmesh-postgresql-storage-integrity-v1",
                "representative": representative_summary,
                "ledger": ledger_summary,
            }
        ),
        representative_data_digest=digest(representative_summary),
        schema_metadata_digest=digest(schema_summary),
        integrity_marker_digest=digest(ledger_summary),
        query_behavior_digest=digest(query_summary),
    )


def observe_restored_database(
    connection: Any,
    plan: PostgreSQLGrantPlan,
) -> PostgreSQLDatabaseObservation:
    version, tls_summary, monitoring_summary = _observe_engine(connection)
    if version["server_version_num"] != POSTGRESQL_SERVER_VERSION_NUM:
        raise RuntimeError("restored PostgreSQL server version is not the pinned version")
    principal_summary = _principal_summary(connection, plan)
    grant_summary = _grant_summary(connection, plan)
    public_schema_summary = _public_schema_summary(connection, plan)
    ledger_summary = _ledger_replay_summary(connection, plan)
    representative_summary = _representative_data_summary(connection, plan)
    schema_summary = _schema_summary(connection, plan)
    query_summary = _restored_query_summary(connection, plan)
    return PostgreSQLDatabaseObservation(
        engine_version="18.6",
        engine_build_digest=digest(
            {"domain": "pillarmesh-postgresql-engine-build-v1", "version": version}
        ),
        principal_profile_digest=digest(principal_summary),
        namespace_grant_matrix_digest=digest(
            {"governed": grant_summary, "public": public_schema_summary}
        ),
        tls_probe_digest=digest(tls_summary),
        positive_probe_digest=digest(
            {"domain": "pillarmesh-postgresql-restored-positive-probes-v1", "query": query_summary}
        ),
        denial_probe_digest=digest(
            {"domain": "pillarmesh-postgresql-restored-denial-probes-v1", "query": query_summary}
        ),
        ledger_probe_digest=digest(ledger_summary),
        monitoring_probe_digest=digest(monitoring_summary),
        storage_integrity_probe_digest=digest(
            {
                "domain": "pillarmesh-postgresql-storage-integrity-v1",
                "representative": representative_summary,
                "ledger": ledger_summary,
            }
        ),
        representative_data_digest=digest(representative_summary),
        schema_metadata_digest=digest(schema_summary),
        integrity_marker_digest=digest(ledger_summary),
        query_behavior_digest=digest(query_summary),
    )


def _create_roles(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    for principal in _PRINCIPALS:
        role = plan.role(principal)
        cursor.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        attributes = (
            "CREATEDB CREATEROLE" if principal == "administration" else "NOCREATEDB NOCREATEROLE"
        )
        cursor.execute(
            sql.SQL(
                "ALTER ROLE {} NOLOGIN NOSUPERUSER {} NOREPLICATION NOBYPASSRLS INHERIT"
            ).format(sql.Identifier(role), sql.SQL(attributes))
        )
    cursor.execute(
        sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(plan.role("backup_restore")))
    )


def _create_namespaces(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    administration = sql.Identifier(plan.role("administration"))
    for namespace in _NAMESPACES:
        identifier = sql.Identifier(plan.namespace(namespace))
        cursor.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                identifier, administration
            )
        )
        cursor.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(identifier, administration))
    unrelated = sql.Identifier(plan.unrelated_namespace)
    cursor.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION postgres").format(unrelated)
    )
    cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(unrelated))
    cursor.execute(
        sql.SQL("CREATE TABLE IF NOT EXISTS {}.private_probe (value integer PRIMARY KEY)").format(
            unrelated
        )
    )
    cursor.execute(
        sql.SQL("INSERT INTO {}.private_probe (value) VALUES (1) ON CONFLICT DO NOTHING").format(
            unrelated
        )
    )


def _create_unrelated_database(connection: Any, plan: PostgreSQLGrantPlan) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s",
            (plan.unrelated_database,),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(plan.unrelated_database))
            )
        cursor.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(plan.unrelated_database)
            )
        )


def _create_probe_tables(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    for namespace, table, columns in _TABLE_SPECS:
        cursor.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
                sql.Identifier(plan.namespace(namespace)),
                sql.Identifier(table),
                sql.SQL(columns),
            )
        )
        cursor.execute(
            sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                sql.Identifier(plan.namespace(namespace)),
                sql.Identifier(table),
                sql.Identifier(plan.role("administration")),
            )
        )


def _assert_restored_objects(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    schemas = (
        *(plan.namespace(namespace) for namespace in _NAMESPACES),
        plan.unrelated_namespace,
    )
    cursor.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE' "
        "ORDER BY table_schema, table_name",
        (list(schemas),),
    )
    observed = tuple(tuple(row) for row in cursor.fetchall())
    expected_items = [
        (plan.namespace(namespace), table) for namespace, table, _columns in _TABLE_SPECS
    ]
    expected_items.append((plan.unrelated_namespace, "private_probe"))
    expected = tuple(sorted(expected_items))
    if observed != expected:
        raise RuntimeError("PostgreSQL restored object inventory is incomplete")


def _restore_canonical_ownership(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    administration = sql.Identifier(plan.role("administration"))
    for namespace in _NAMESPACES:
        cursor.execute(
            sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                sql.Identifier(plan.namespace(namespace)),
                administration,
            )
        )
    for namespace, table, _columns in _TABLE_SPECS:
        cursor.execute(
            sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                sql.Identifier(plan.namespace(namespace)),
                sql.Identifier(table),
                administration,
            )
        )


def _remove_default_access(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    cursor.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(_DATABASE_NAME))
    )
    cursor.execute("REVOKE USAGE, CREATE ON SCHEMA public FROM PUBLIC")
    for namespace in _NAMESPACES:
        identifier = sql.Identifier(plan.namespace(namespace))
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(identifier))
        cursor.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(identifier)
        )
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} REVOKE ALL ON TABLES FROM PUBLIC"
            ).format(identifier)
        )


def _apply_grants(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    for principal in _PRINCIPALS:
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(_DATABASE_NAME),
                sql.Identifier(plan.role(principal)),
            )
        )
    _grant_schema(cursor, plan, "ingestion_runtime", ("raw",), "USAGE, CREATE")
    _grant_schema(cursor, plan, "ingestion_runtime", ("control",), "USAGE")
    _grant_schema(cursor, plan, "transformation_runtime", ("raw",), "USAGE")
    _grant_schema(
        cursor,
        plan,
        "transformation_runtime",
        ("conformed", "product", "consumption", "quarantine"),
        "USAGE, CREATE",
    )
    _grant_schema(cursor, plan, "customer_sql", ("consumption",), "USAGE")
    _grant_schema(cursor, plan, "catalog", _NAMESPACES, "USAGE")
    _grant_schema(cursor, plan, "bi", ("consumption",), "USAGE")
    cursor.execute(
        sql.SQL("GRANT INSERT, UPDATE ON {}.ingestion_probe TO {}").format(
            sql.Identifier(plan.namespace("raw")),
            sql.Identifier(plan.role("ingestion_runtime")),
        )
    )
    cursor.execute(
        sql.SQL("GRANT INSERT ON {}.ingestion_append_ledger TO {}").format(
            sql.Identifier(plan.namespace("control")),
            sql.Identifier(plan.role("ingestion_runtime")),
        )
    )
    cursor.execute(
        sql.SQL("GRANT SELECT ON {}.ingestion_probe TO {}").format(
            sql.Identifier(plan.namespace("raw")),
            sql.Identifier(plan.role("transformation_runtime")),
        )
    )
    for namespace, table in (
        ("conformed", "transformation_probe"),
        ("product", "product_probe"),
        ("consumption", "customer_probe"),
        ("consumption", "certified_probe"),
        ("quarantine", "quarantine_probe"),
    ):
        cursor.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {}.{} TO {}").format(
                sql.Identifier(plan.namespace(namespace)),
                sql.Identifier(table),
                sql.Identifier(plan.role("transformation_runtime")),
            )
        )
    cursor.execute(
        sql.SQL("GRANT SELECT ON {}.customer_probe TO {}").format(
            sql.Identifier(plan.namespace("consumption")),
            sql.Identifier(plan.role("customer_sql")),
        )
    )
    cursor.execute(
        sql.SQL("GRANT SELECT ON {}.certified_probe TO {}").format(
            sql.Identifier(plan.namespace("consumption")),
            sql.Identifier(plan.role("bi")),
        )
    )


def _grant_schema(
    cursor: Any,
    plan: PostgreSQLGrantPlan,
    principal: str,
    namespaces: tuple[str, ...],
    privileges: str,
) -> None:
    for namespace in namespaces:
        cursor.execute(
            sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
                sql.SQL(privileges),
                sql.Identifier(plan.namespace(namespace)),
                sql.Identifier(plan.role(principal)),
            )
        )


def _seed_representative_data(cursor: Any, plan: PostgreSQLGrantPlan) -> None:
    cursor.execute(
        sql.SQL(
            "INSERT INTO {}.ingestion_probe (id, payload) "
            "SELECT value, md5(value::text) || md5((value + 50000)::text) "
            "FROM generate_series(1, 50000) AS value ON CONFLICT DO NOTHING"
        ).format(sql.Identifier(plan.namespace("raw")))
    )
    for table in ("customer_probe", "certified_probe"):
        cursor.execute(
            sql.SQL("INSERT INTO {}.{} (id, payload) VALUES (1, %s) ON CONFLICT DO NOTHING").format(
                sql.Identifier(plan.namespace("consumption")), sql.Identifier(table)
            ),
            (f"representative-{table}",),
        )


def _ensure_role(connection: Any, role: str, *, login: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE ROLE {} {}").format(
                    sql.Identifier(role),
                    sql.SQL("LOGIN" if login else "NOLOGIN"),
                )
            )


def _observe_engine(
    connection: Any,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('server_version_num'), current_setting('server_version')"
        )
        version_number, version_text = cursor.fetchone()
        cursor.execute(
            "SELECT ssl, version, cipher, client_dn IS NOT NULL "
            "FROM pg_catalog.pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        ssl_enabled, protocol, cipher, has_client_identity = cursor.fetchone()
        cursor.execute(
            "SELECT numbackends, xact_commit, xact_rollback "
            "FROM pg_catalog.pg_stat_database WHERE datname = current_database()"
        )
        backends, commits, rollbacks = cursor.fetchone()
        cursor.execute("SELECT pg_database_size(current_database())")
        database_bytes = cursor.fetchone()[0]
    version: dict[str, object] = {
        "server_version_num": str(version_number),
        "server_version": str(version_text),
    }
    tls: dict[str, object] = {
        "ssl": bool(ssl_enabled),
        "protocol": str(protocol),
        "cipher": str(cipher),
        "client_identity_present": bool(has_client_identity),
    }
    if (
        tls["ssl"] is not True
        or tls["protocol"] != "TLSv1.3"
        or not isinstance(cipher, str)
        or not cipher
        or not has_client_identity
    ):
        raise RuntimeError("PostgreSQL warehouse TLS observation is insufficient")
    monitoring: dict[str, object] = {
        "numbackends_nonnegative": int(backends) >= 0,
        "xact_commit_nonnegative": int(commits) >= 0,
        "xact_rollback_nonnegative": int(rollbacks) >= 0,
        "database_bytes_positive": int(database_bytes) > 0,
    }
    if not all(monitoring.values()):
        raise RuntimeError("PostgreSQL warehouse monitoring observation is insufficient")
    return version, tls, monitoring


def _principal_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    names = tuple(plan.role(principal) for principal in _PRINCIPALS)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname, rolcanlogin, rolcreatedb, rolcreaterole, rolsuper, rolinherit, "
            "rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(names),),
        )
        roles = tuple(tuple(row) for row in cursor.fetchall())
        cursor.execute(
            "SELECT member_role.rolname, granted_role.rolname "
            "FROM pg_catalog.pg_auth_members memberships "
            "JOIN pg_catalog.pg_roles member_role ON member_role.oid = memberships.member "
            "JOIN pg_catalog.pg_roles granted_role ON granted_role.oid = memberships.roleid "
            "WHERE member_role.rolname = ANY(%s) "
            "ORDER BY member_role.rolname, granted_role.rolname",
            (list(names),),
        )
        memberships = tuple(tuple(row) for row in cursor.fetchall())
    expected_roles = tuple(
        sorted(
            (
                plan.role(principal),
                False,
                principal == "administration",
                principal == "administration",
                False,
                True,
                False,
                False,
            )
            for principal in _PRINCIPALS
        )
    )
    expected_memberships = ((plan.role("backup_restore"), "pg_read_all_data"),)
    if roles != expected_roles or memberships != expected_memberships:
        raise RuntimeError("PostgreSQL warehouse principal profile is incomplete")
    return {
        "domain": "pillarmesh-postgresql-principal-profile-v1",
        "roles": roles,
        "memberships": memberships,
    }


def _grant_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    checks: list[tuple[str, str, str, bool]] = []
    with connection.cursor() as cursor:
        for principal in _PRINCIPALS:
            role = plan.role(principal)
            for namespace in (*_NAMESPACES, "unrelated"):
                schema = (
                    plan.unrelated_namespace
                    if namespace == "unrelated"
                    else plan.namespace(namespace)
                )
                for privilege in ("USAGE", "CREATE"):
                    cursor.execute(
                        "SELECT has_schema_privilege(%s, %s, %s)",
                        (role, schema, privilege),
                    )
                    allowed = bool(cursor.fetchone()[0])
                    checks.append((principal, namespace, privilege.lower(), allowed))
                    if allowed is not _expected_schema_privilege(
                        principal,
                        namespace,
                        privilege,
                    ):
                        raise RuntimeError("PostgreSQL warehouse grant matrix is incorrect")
            for database_name, target in (
                (_DATABASE_NAME, "database"),
                (plan.unrelated_database, "unrelated_database"),
            ):
                cursor.execute(
                    "SELECT has_database_privilege(%s, %s, 'CONNECT')",
                    (role, database_name),
                )
                allowed = bool(cursor.fetchone()[0])
                checks.append((principal, target, "connect", allowed))
                if allowed is not (target == "database"):
                    raise RuntimeError("PostgreSQL warehouse grant matrix is incorrect")
        table_specs = (
            ("raw", "ingestion_probe"),
            ("conformed", "transformation_probe"),
            ("product", "product_probe"),
            ("consumption", "customer_probe"),
            ("consumption", "certified_probe"),
            ("quarantine", "quarantine_probe"),
            ("control", "lifecycle_ledger"),
            ("control", "ingestion_append_ledger"),
        )
        table_privileges = (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        )
        for principal in _PRINCIPALS:
            for namespace, table in table_specs:
                for privilege in table_privileges:
                    cursor.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        (
                            plan.role(principal),
                            f"{plan.namespace(namespace)}.{table}",
                            privilege,
                        ),
                    )
                    allowed = bool(cursor.fetchone()[0])
                    checks.append((principal, f"{namespace}.{table}", privilege.lower(), allowed))
                    if allowed is not _expected_table_privilege(
                        principal,
                        namespace,
                        table,
                        privilege,
                    ):
                        raise RuntimeError("PostgreSQL warehouse grant matrix is incorrect")
    return {"domain": "pillarmesh-postgresql-grant-matrix-v1", "checks": tuple(checks)}


def _public_schema_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT "
            "COALESCE(bool_or(acl.privilege_type = 'USAGE'), false), "
            "COALESCE(bool_or(acl.privilege_type = 'CREATE'), false) "
            "FROM pg_catalog.pg_namespace namespace "
            "LEFT JOIN LATERAL aclexplode(COALESCE(namespace.nspacl, "
            "acldefault('n', namespace.nspowner))) acl ON true "
            "WHERE namespace.nspname = 'public' AND acl.grantee = 0"
        )
        row = cursor.fetchone()
    if row is None or len(row) != 2:
        raise RuntimeError("PostgreSQL public namespace observation is incomplete")
    public_usage, public_create = (bool(row[0]), bool(row[1]))
    if public_usage or public_create:
        raise RuntimeError("PostgreSQL public namespace access was not denied")
    return {
        "domain": "pillarmesh-postgresql-public-namespace-v1",
        "grant_plan_digest": digest(plan.prefix),
        "public_usage": public_usage,
        "public_create": public_create,
    }


def _expected_schema_privilege(principal: str, namespace: str, privilege: str) -> bool:
    usage_namespaces = {
        "administration": frozenset(_NAMESPACES),
        "ingestion_runtime": frozenset({"raw", "control"}),
        "transformation_runtime": frozenset(
            {"raw", "conformed", "product", "consumption", "quarantine"}
        ),
        "backup_restore": frozenset((*_NAMESPACES, "unrelated")),
        "customer_sql": frozenset({"consumption"}),
        "catalog": frozenset(_NAMESPACES),
        "bi": frozenset({"consumption"}),
    }
    create_namespaces = {
        "administration": frozenset(_NAMESPACES),
        "ingestion_runtime": frozenset({"raw"}),
        "transformation_runtime": frozenset({"conformed", "product", "consumption", "quarantine"}),
        "backup_restore": frozenset(),
        "customer_sql": frozenset(),
        "catalog": frozenset(),
        "bi": frozenset(),
    }
    namespaces = (
        usage_namespaces[principal] if privilege == "USAGE" else create_namespaces[principal]
    )
    return namespace in namespaces


def _expected_table_privilege(
    principal: str,
    namespace: str,
    table: str,
    privilege: str,
) -> bool:
    if principal == "administration":
        return True
    if principal == "backup_restore":
        return privilege == "SELECT"
    target = (namespace, table)
    if principal == "ingestion_runtime":
        return (target, privilege) in {
            (("raw", "ingestion_probe"), "INSERT"),
            (("raw", "ingestion_probe"), "UPDATE"),
            (("control", "ingestion_append_ledger"), "INSERT"),
        }
    if principal == "transformation_runtime":
        if target == ("raw", "ingestion_probe"):
            return privilege == "SELECT"
        return target in {
            ("conformed", "transformation_probe"),
            ("product", "product_probe"),
            ("consumption", "customer_probe"),
            ("consumption", "certified_probe"),
            ("quarantine", "quarantine_probe"),
        } and privilege in {"SELECT", "INSERT", "UPDATE", "DELETE"}
    if principal == "customer_sql":
        return target == ("consumption", "customer_probe") and privilege == "SELECT"
    if principal == "bi":
        return target == ("consumption", "certified_probe") and privilege == "SELECT"
    return False


def _ledger_replay_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    marker = "deterministic-lifecycle-marker-v1"
    marker_digest = digest({"domain": "pillarmesh-postgresql-ledger-marker-v1"})
    with connection.cursor() as cursor:
        for _ in range(2):
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.lifecycle_ledger (operation_marker, marker_digest) "
                    "VALUES (%s, %s) ON CONFLICT (operation_marker) DO NOTHING"
                ).format(sql.Identifier(plan.namespace("control"))),
                (marker, marker_digest),
            )
        cursor.execute(
            sql.SQL(
                "SELECT count(*), min(marker_digest), max(marker_digest) "
                "FROM {}.lifecycle_ledger WHERE operation_marker = %s"
            ).format(sql.Identifier(plan.namespace("control"))),
            (marker,),
        )
        count, minimum, maximum = cursor.fetchone()
    if count != 1 or minimum != marker_digest or maximum != marker_digest:
        raise RuntimeError("PostgreSQL warehouse ledger replay was not deterministic")
    return {
        "domain": "pillarmesh-postgresql-ledger-replay-v1",
        "row_count": int(count),
        "marker_digest": str(minimum),
    }


def _representative_data_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "SELECT count(*), min(id), max(id), "
                "md5(string_agg(payload, '' ORDER BY id)) FROM {}.ingestion_probe"
            ).format(sql.Identifier(plan.namespace("raw")))
        )
        count, minimum, maximum, payload_digest = cursor.fetchone()
    if int(count) < 50_000:
        raise RuntimeError("PostgreSQL warehouse representative data is incomplete")
    return {
        "domain": "pillarmesh-postgresql-representative-data-v1",
        "row_count": int(count),
        "minimum_id": int(minimum),
        "maximum_id": int(maximum),
        "payload_digest": str(payload_digest),
    }


def _schema_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    schemas = tuple(plan.namespace(namespace) for namespace in _NAMESPACES)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_schema, table_name, column_name, ordinal_position, data_type "
            "FROM information_schema.columns WHERE table_schema = ANY(%s) "
            "ORDER BY table_schema, table_name, ordinal_position",
            (list(schemas),),
        )
        columns = tuple(tuple(row) for row in cursor.fetchall())
    if not columns:
        raise RuntimeError("PostgreSQL warehouse schema metadata is absent")
    return {"domain": "pillarmesh-postgresql-schema-metadata-v1", "columns": columns}


def _probe_summaries(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    plan: PostgreSQLGrantPlan,
    passwords: Mapping[str, str],
    *,
    denial_connect: _Connect | None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    positive: list[tuple[str, str]] = []
    denials: list[tuple[str, str]] = []
    for principal in (value for value in _PRINCIPALS if value in passwords):
        password = passwords[principal]
        with closing(
            connect_tls(
                connect,
                target,
                user=plan.probe_role(principal),
                password=password,
            )
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(plan.role(principal))))
            _positive_probe(connection, plan, principal)
            positive.append((principal, "allowed"))
            if principal != "administration":
                _require_denial(
                    connection,
                    sql.SQL("CREATE ROLE {}").format(
                        sql.Identifier(f"{plan.prefix}_{principal}_forbidden_role")
                    ),
                )
                denials.append((principal, "role_create"))
                _require_denial(
                    connection,
                    sql.SQL("GRANT pg_read_all_data TO {}").format(
                        sql.Identifier(plan.probe_role(principal))
                    ),
                )
                denials.append((principal, "grant_change"))
                _require_denial(
                    connection,
                    sql.SQL("DELETE FROM {}.lifecycle_ledger").format(
                        sql.Identifier(plan.namespace("control"))
                    ),
                )
                denials.append((principal, "control_delete"))
                if principal == "ingestion_runtime":
                    _require_denial(
                        connection,
                        sql.SQL("CREATE TABLE {}.forbidden_control_table (value integer)").format(
                            sql.Identifier(plan.namespace("control"))
                        ),
                    )
                    denials.append((principal, "control_create"))
                if principal == "backup_restore":
                    _require_database_denial(
                        connect,
                        target,
                        user=plan.probe_role(principal),
                        password=password,
                        database_name=plan.unrelated_database,
                        denial_connect=denial_connect,
                    )
                    denials.append((principal, "unrelated_database"))
                else:
                    _require_denial(
                        connection,
                        sql.SQL("SELECT value FROM {}.private_probe").format(
                            sql.Identifier(plan.unrelated_namespace)
                        ),
                    )
                    denials.append((principal, "unrelated_namespace"))
                if principal == "catalog":
                    _require_denial(
                        connection,
                        sql.SQL("SELECT payload FROM {}.ingestion_probe").format(
                            sql.Identifier(plan.namespace("raw"))
                        ),
                    )
                    denials.append((principal, "governed_row_read"))
                    _require_denial(
                        connection,
                        sql.SQL(
                            "INSERT INTO {}.ingestion_probe (id, payload) VALUES (-1, 'forbidden')"
                        ).format(sql.Identifier(plan.namespace("raw"))),
                    )
                    denials.append((principal, "governed_row_write"))
    return (
        {"domain": "pillarmesh-postgresql-positive-probes-v1", "probes": tuple(positive)},
        {"domain": "pillarmesh-postgresql-denial-probes-v1", "probes": tuple(denials)},
        {
            "domain": "pillarmesh-postgresql-query-behavior-v1",
            "positive_count": len(positive),
            "denial_count": len(denials),
        },
    )


def _positive_probe(connection: Any, plan: PostgreSQLGrantPlan, principal: str) -> None:
    with connection.cursor() as cursor:
        if principal == "administration":
            cursor.execute(
                sql.SQL("CREATE ROLE {}").format(sql.Identifier(plan.administration_test_role))
            )
            cursor.execute(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(plan.administration_test_role))
            )
        elif principal == "ingestion_runtime":
            cursor.execute(
                sql.SQL("CREATE TABLE {}.ingestion_runtime_probe (payload text NOT NULL)").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.ingestion_runtime_probe VALUES ('ingestion')").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
            cursor.execute(
                sql.SQL("UPDATE {}.ingestion_runtime_probe SET payload = 'updated'").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.ingestion_append_ledger (marker_digest) VALUES (%s)"
                ).format(sql.Identifier(plan.namespace("control"))),
                (digest({"domain": "ingestion-probe"}),),
            )
            cursor.execute(
                sql.SQL("DROP TABLE {}.ingestion_runtime_probe").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
        elif principal == "transformation_runtime":
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.ingestion_probe").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.transformation_probe (id, payload) VALUES (1, 'transform') "
                    "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload"
                ).format(sql.Identifier(plan.namespace("conformed")))
            )
        elif principal == "backup_restore":
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.ingestion_probe").format(
                    sql.Identifier(plan.namespace("raw"))
                )
            )
        elif principal == "customer_sql":
            cursor.execute(
                sql.SQL("SELECT payload FROM {}.customer_probe").format(
                    sql.Identifier(plan.namespace("consumption"))
                )
            )
        elif principal == "catalog":
            _catalog_metadata_summary(connection, plan)
        elif principal == "bi":
            cursor.execute(
                sql.SQL("SELECT payload FROM {}.certified_probe").format(
                    sql.Identifier(plan.namespace("consumption"))
                )
            )


def _require_denial(connection: Any, statement: sql.Composable) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
    except psycopg.errors.InsufficientPrivilege:
        return
    raise RuntimeError("PostgreSQL warehouse denial probe unexpectedly succeeded")


def _require_database_denial(
    connect: _Connect,
    target: PostgreSQLConnectionTarget,
    *,
    user: str,
    password: str,
    database_name: str,
    denial_connect: _Connect | None = None,
) -> None:
    probe_connect = denial_connect or connect
    try:
        connection = connect_tls(
            probe_connect,
            target,
            user=user,
            password=password,
            database_name=database_name,
        )
    except _DATABASE_DENIAL_ERRORS:
        return
    with closing(connection):
        raise RuntimeError("PostgreSQL warehouse unrelated database was accessible")


def _catalog_metadata_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    governed_schemas = tuple(plan.namespace(namespace) for namespace in _NAMESPACES)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT namespace.nspname, relation.relname "
            "FROM pg_catalog.pg_class relation "
            "JOIN pg_catalog.pg_namespace namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = ANY(%s) AND relation.relkind IN ('r', 'p') "
            "ORDER BY namespace.nspname, relation.relname",
            (list(governed_schemas),),
        )
        objects = tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
    expected = tuple(
        sorted((plan.namespace(namespace), table) for namespace, table, _columns in _TABLE_SPECS)
    )
    if objects != expected:
        raise RuntimeError("PostgreSQL catalog metadata inventory is incomplete")
    return {"domain": "pillarmesh-postgresql-catalog-metadata-v1", "objects": objects}


def _restored_query_summary(connection: Any, plan: PostgreSQLGrantPlan) -> dict[str, object]:
    checks: list[tuple[str, str, bool]] = []
    for principal, statement in (
        (
            "transformation_runtime",
            sql.SQL("SELECT count(*) FROM {}.ingestion_probe").format(
                sql.Identifier(plan.namespace("raw"))
            ),
        ),
        (
            "customer_sql",
            sql.SQL("SELECT count(*) FROM {}.customer_probe").format(
                sql.Identifier(plan.namespace("consumption"))
            ),
        ),
        (
            "bi",
            sql.SQL("SELECT count(*) FROM {}.certified_probe").format(
                sql.Identifier(plan.namespace("consumption"))
            ),
        ),
    ):
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(plan.role(principal))))
            cursor.execute(statement)
            cursor.fetchone()
            cursor.execute("RESET ROLE")
        checks.append((principal, "positive", True))
    for principal in (
        "ingestion_runtime",
        "transformation_runtime",
        "backup_restore",
        "customer_sql",
        "catalog",
        "bi",
    ):
        if principal == "backup_restore":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT has_database_privilege(%s, %s, 'CONNECT')",
                    (plan.role(principal), plan.unrelated_database),
                )
                if cursor.fetchone()[0]:
                    raise RuntimeError("restored backup role can access unrelated database")
            checks.append((principal, "unrelated_database_denied", True))
        else:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(plan.role(principal))))
            try:
                _require_denial(
                    connection,
                    sql.SQL("SELECT value FROM {}.private_probe").format(
                        sql.Identifier(plan.unrelated_namespace)
                    ),
                )
            finally:
                with connection.cursor() as cursor:
                    cursor.execute("RESET ROLE")
            checks.append((principal, "unrelated_namespace_denied", True))
    return {"domain": "pillarmesh-postgresql-restored-query-behavior-v1", "checks": tuple(checks)}


__all__ = [
    "PostgreSQLConnectionTarget",
    "PostgreSQLDatabaseObservation",
    "PostgreSQLGrantPlan",
    "PostgreSQLProbeCleanupError",
    "assert_password_connection_denied",
    "assert_plaintext_connection_denied",
    "connect_tls",
    "create_canonical_roles",
    "create_probe_logins",
    "derive_grant_plan",
    "drop_probe_logins",
    "inspect_probe_logins",
    "observe_database",
    "observe_restored_database",
    "prepare_database",
    "prepare_restored_database",
    "probe_login_scope",
    "rotate_bootstrap_password",
    "wait_for_tls_connection",
    "with_probe_login_cleanup_evidence",
]
