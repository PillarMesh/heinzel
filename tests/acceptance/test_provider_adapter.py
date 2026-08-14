from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.config import AcceptanceConfig, HarnessError
from tests.acceptance.provider_adapter import (
    _SNOWFLAKE_METADATA_QUERIES,
    POSTGRES_FIXTURE_GRANTS,
    POSTGRES_RUNTIME_GRANTS,
    SNOWFLAKE_RUNTIME_GRANTS,
    TRUSTED_POSTGRES_AUDIT_BODY,
    TRUSTED_POSTGRES_AUDIT_BODY_DIGEST,
    LiveProviderActions,
    _is_allowed_metadata_query,
    validate_attestation,
)
from tests.acceptance.test_harness import KEY, _environment

Result = tuple[tuple[tuple[Any, ...], ...], tuple[str, ...]]
Router = Callable[[object, tuple[Any, ...] | None], Result]


class ScriptedCursor:
    def __init__(
        self, router: Router, statements: list[tuple[str, tuple[Any, ...] | None]]
    ) -> None:
        self._router = router
        self._statements = statements
        self._rows: tuple[tuple[Any, ...], ...] = ()
        self._position = 0
        self.description: tuple[tuple[str], ...] = ()
        self.sfqid = "query-offline"

    def execute(self, query: object, parameters: tuple[Any, ...] | None = None) -> None:
        text = str(query)
        self._statements.append((text, parameters))
        rows, columns = self._router(query, parameters)
        self._rows = rows
        self._position = 0
        self.description = tuple((column,) for column in columns)

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> tuple[tuple[Any, ...], ...]:
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return rows

    def __enter__(self) -> ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class ScriptedConnection:
    def __init__(self, router: Router) -> None:
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self._cursor = ScriptedCursor(router, self.statements)
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> ScriptedConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _config(tmp_path: Path) -> AcceptanceConfig:
    return AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )


def _postgres_router(
    config: AcceptanceConfig,
    principal: str,
    *,
    session_principal: str | None = None,
    extra_privileges: frozenset[str] = frozenset(),
    audit_body: str = TRUSTED_POSTGRES_AUDIT_BODY,
) -> Router:
    env = config.environment
    session = principal if session_principal is None else session_principal
    source = f"{env['PILLARMESH_POSTGRES_SCHEMA']}.{env['PILLARMESH_POSTGRES_TABLE']}"
    marker = f"{env['PILLARMESH_POSTGRES_SCHEMA']}.environment_marker"

    def route(query: object, parameters: tuple[Any, ...] | None) -> Result:
        text = str(query)
        rendered = text.casefold()
        params = parameters or ()
        if "session_user, current_user, current_database()," in rendered:
            return (((session, principal, "m0_acceptance", "m0_owner"),), ())
        if "current_user, current_database()," in rendered:
            return (((principal, "m0_acceptance", "m0_owner"),), ())
        if text == "SELECT session_user, current_user, current_database()":
            return (((session, principal, "m0_acceptance"),), ())
        if text == "SELECT current_user, current_database()":
            return (((principal, "m0_acceptance"),), ())
        if "from pg_catalog.pg_namespace n where n.nspname=%s" in rendered:
            return ((("m0_owner",),), ())
        if "from pg_catalog.pg_class c" in rendered:
            return (
                (
                    (env["PILLARMESH_POSTGRES_TABLE"], "r", "m0_owner"),
                    ("environment_marker", "r", "m0_owner"),
                ),
                (),
            )
        if "from pg_catalog.pg_proc p" in rendered:
            return (
                (
                    (
                        "f",
                        "m0_owner",
                        True,
                        "sql",
                        "s",
                        "bigint",
                        ["search_path=pg_catalog, public"],
                        audit_body,
                    ),
                ),
                (),
            )
        if "environment_id" in rendered and "identifier" in rendered:
            return (((config.environment_identity,),), ())
        if "select exists (select 1 from pg_catalog.pg_namespace" in rendered:
            return (((True,),), ())
        if rendered.startswith("select has_schema_privilege(current_user"):
            return (((False,),), ())
        if rendered.startswith("select has_"):
            allowed = False
            if "has_database_privilege" in rendered and "'connect'" in rendered:
                allowed = True
            elif "has_schema_privilege" in rendered and "'usage'" in rendered:
                allowed = len(params) > 1 and params[1] == env["PILLARMESH_POSTGRES_SCHEMA"]
            elif "has_table_privilege" in rendered and "has_any_column" not in rendered:
                privilege = rendered.rsplit("'", 2)[1].upper()
                table = str(params[1])
                allowed = principal == "runtime_one" and table == source and privilege == "SELECT"
                allowed |= principal == "runtime_one" and table == marker and privilege == "SELECT"
                allowed |= (
                    principal == "fixture" and table == source and privilege in {"INSERT", "DELETE"}
                )
                allowed |= f"TABLE_{privilege}_{'SOURCE' if table == source else 'MARKER'}" in (
                    extra_privileges
                )
            elif "has_any_column_privilege" in rendered:
                privilege = (
                    rendered.split("has_any_column_privilege", 1)[1].split("'", 2)[1].upper()
                )
                table = str(params[1])
                allowed = f"COLUMN_{privilege}_{'SOURCE' if table == source else 'MARKER'}" in (
                    extra_privileges
                )
            elif "has_function_privilege" in rendered:
                allowed = principal == "runtime_one"
            return (((allowed,),), ())
        raise AssertionError((text, parameters))

    return route


def _show_rows(privileges: tuple[tuple[str, str], ...]) -> Result:
    return (privileges, ("privilege", "grantee_name"))


def _snowflake_router(config: AcceptanceConfig) -> Router:
    env = config.environment
    runtime = env["PILLARMESH_SNOWFLAKE_ROLE"]
    owner = "PILLARMESH_M0_OWNER"
    database = env["PILLARMESH_SNOWFLAKE_DATABASE"]
    schema = f"{database}.{env['PILLARMESH_SNOWFLAKE_SCHEMA']}"

    def qualified(name: str) -> str:
        return f"{schema}.{name}"

    def route(query: object, parameters: tuple[Any, ...] | None) -> Result:
        text = str(query)
        if text == (
            "SELECT CURRENT_USER(), CURRENT_ACCOUNT_NAME(), CURRENT_ACCOUNT(), CURRENT_ROLE()"
        ):
            row = (env["PILLARMESH_SNOWFLAKE_USER"], "DEDICATED_ACCOUNT", "LOCATOR", runtime)
            return ((row,), ())
        if text.startswith("SHOW GRANTS TO USER"):
            return ((("USER", runtime),), ("granted_to", "role"))
        if text.startswith("SHOW GRANTS ON"):
            privileges: list[tuple[str, str]] = [("OWNERSHIP", owner)]
            usage_objects = {
                f"SHOW GRANTS ON WAREHOUSE {env['PILLARMESH_SNOWFLAKE_WAREHOUSE']}",
                f"SHOW GRANTS ON DATABASE {database}",
                f"SHOW GRANTS ON SCHEMA {schema}",
            }
            if text in usage_objects:
                privileges.append(("USAGE", runtime))
            elif text == f"SHOW GRANTS ON STAGE {qualified(env['PILLARMESH_SNOWFLAKE_STAGE'])}":
                privileges.extend((("READ", runtime), ("WRITE", runtime)))
            elif text == (
                f"SHOW GRANTS ON TABLE {qualified(env['PILLARMESH_SNOWFLAKE_TARGET_TABLE'])}"
            ):
                privileges.extend((("SELECT", runtime), ("INSERT", runtime), ("UPDATE", runtime)))
            elif text == (
                "SHOW GRANTS ON TABLE "
                f"{qualified(env['PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE'])}"
            ):
                privileges.append(("SELECT", runtime))
            elif text == (
                f"SHOW GRANTS ON TABLE {qualified(env['PILLARMESH_SNOWFLAKE_LEDGER_TABLE'])}"
            ):
                privileges.extend((("SELECT", runtime), ("INSERT", runtime)))
            elif text == f"SHOW GRANTS ON TABLE {qualified('ENVIRONMENT_MARKER')}":
                privileges.append(("SELECT", runtime))
            return _show_rows(tuple(privileges))
        if text.startswith("SHOW GRANTS TO ROLE"):
            rows = (
                ("WAREHOUSE", env["PILLARMESH_SNOWFLAKE_WAREHOUSE"], "USAGE"),
                ("DATABASE", database, "USAGE"),
                ("SCHEMA", schema, "USAGE"),
                ("STAGE", qualified(env["PILLARMESH_SNOWFLAKE_STAGE"]), "READ"),
                ("STAGE", qualified(env["PILLARMESH_SNOWFLAKE_STAGE"]), "WRITE"),
                ("TABLE", qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]), "SELECT"),
                ("TABLE", qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]), "INSERT"),
                ("TABLE", qualified(env["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]), "UPDATE"),
                (
                    "TABLE",
                    qualified(env["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"]),
                    "SELECT",
                ),
                ("TABLE", qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"]), "SELECT"),
                ("TABLE", qualified(env["PILLARMESH_SNOWFLAKE_LEDGER_TABLE"]), "INSERT"),
                ("TABLE", qualified("ENVIRONMENT_MARKER"), "SELECT"),
            )
            return (rows, ("granted_on", "name", "privilege"))
        if text.startswith("SELECT environment_identity, owner_user, owner_role"):
            return (
                (
                    (
                        config.environment_identity,
                        "M0_OWNER",
                        owner,
                        "UNRELATED_PRIVATE",
                        owner,
                    ),
                ),
                (),
            )
        if text == "SELECT CURRENT_USER()":
            return (((env["PILLARMESH_SNOWFLAKE_USER"],),), ())
        if text.startswith("SELECT COUNT(*) FROM PILLARMESH_M0.TRANSFER.ORDERS"):
            return (((0,),), ())
        if text.startswith("SELECT COUNT(*) FROM IDENTIFIER"):
            error = RuntimeError("denied")
            error.sqlstate = "42501"  # type: ignore[attr-defined]
            raise error
        raise AssertionError((text, parameters))

    return route


def test_live_preflight_maps_real_grant_shapes_and_keeps_fixture_write_only(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runtime = ScriptedConnection(_postgres_router(config, "runtime_one"))
    fixture = ScriptedConnection(_postgres_router(config, "fixture"))
    snowflake = ScriptedConnection(_snowflake_router(config))

    def postgres_connect(dsn: str) -> ScriptedConnection:
        return fixture if dsn == config.environment["PILLARMESH_POSTGRES_FIXTURE_DSN"] else runtime

    actions = LiveProviderActions(
        config,
        postgres_connect=postgres_connect,
        snowflake_connect=lambda **_kwargs: snowflake,
    )
    attestation = actions.preflight()
    validate_attestation(config, attestation)

    assert attestation.postgres_runtime_grants == POSTGRES_RUNTIME_GRANTS
    assert attestation.postgres_fixture_grants == POSTGRES_FIXTURE_GRANTS
    assert attestation.postgres_audit_body_digest == TRUSTED_POSTGRES_AUDIT_BODY_DIGEST
    assert attestation.snowflake_runtime_grants == SNOWFLAKE_RUNTIME_GRANTS
    assert attestation.snowflake_marker_denial_database == "UNRELATED_PRIVATE"
    assert attestation.snowflake_marker_denial_database_owner_role == "PILLARMESH_M0_OWNER"
    assert any(query.startswith("SHOW GRANTS ON") for query, _params in snowflake.statements)
    assert not any("environment_id" in query.casefold() for query, _params in fixture.statements)
    assert not any("pg_proc" in query.casefold() for query, _params in fixture.statements)


@pytest.mark.parametrize(
    ("runtime_session", "runtime_current", "fixture_session", "fixture_current"),
    (
        ("m0_owner", "runtime_one", "fixture", "fixture"),
        ("runtime_one", "m0_owner", "fixture", "fixture"),
        ("runtime_one", "runtime_one", "m0_owner", "fixture"),
        ("runtime_one", "runtime_one", "fixture", "m0_owner"),
    ),
)
def test_live_preflight_rejects_assumed_postgres_runtime_or_fixture_identity(
    tmp_path: Path,
    runtime_session: str,
    runtime_current: str,
    fixture_session: str,
    fixture_current: str,
) -> None:
    config = _config(tmp_path)
    runtime = ScriptedConnection(
        _postgres_router(config, runtime_current, session_principal=runtime_session)
    )
    fixture = ScriptedConnection(
        _postgres_router(config, fixture_current, session_principal=fixture_session)
    )

    def postgres_connect(dsn: str) -> ScriptedConnection:
        return fixture if dsn == config.environment["PILLARMESH_POSTGRES_FIXTURE_DSN"] else runtime

    actions = LiveProviderActions(
        config,
        postgres_connect=postgres_connect,
        snowflake_connect=lambda **_kwargs: ScriptedConnection(_snowflake_router(config)),
    )

    with pytest.raises(HarnessError, match="connected runtime principal is not isolated"):
        validate_attestation(config, actions.preflight())


@pytest.mark.parametrize(
    "extra_privilege",
    (
        "COLUMN_SELECT_SOURCE",
        "COLUMN_SELECT_MARKER",
        "TABLE_MAINTAIN_SOURCE",
        "TABLE_MAINTAIN_MARKER",
    ),
)
def test_live_preflight_rejects_fixture_column_select_or_maintain_privilege(
    tmp_path: Path, extra_privilege: str
) -> None:
    config = _config(tmp_path)
    runtime = ScriptedConnection(_postgres_router(config, "runtime_one"))
    fixture = ScriptedConnection(
        _postgres_router(config, "fixture", extra_privileges=frozenset((extra_privilege,)))
    )

    def postgres_connect(dsn: str) -> ScriptedConnection:
        return fixture if dsn == config.environment["PILLARMESH_POSTGRES_FIXTURE_DSN"] else runtime

    actions = LiveProviderActions(
        config,
        postgres_connect=postgres_connect,
        snowflake_connect=lambda **_kwargs: ScriptedConnection(_snowflake_router(config)),
    )

    with pytest.raises(HarnessError, match="dedicated environment attestation failed"):
        validate_attestation(config, actions.preflight())


def test_live_preflight_rejects_audit_that_misses_assumed_role_statements(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    stale_body = """
SELECT coalesce(sum(s.calls), 0)::bigint
FROM public.pg_stat_statements AS s
JOIN pg_catalog.pg_roles AS r ON r.oid = s.userid
WHERE r.rolname = session_user
  AND s.query ILIKE '%pillarmesh_m0%orders%'
  AND s.query ~* '^[[:space:]]*(select|declare)'
""".strip()
    runtime = ScriptedConnection(_postgres_router(config, "runtime_one", audit_body=stale_body))
    fixture = ScriptedConnection(_postgres_router(config, "fixture"))

    def postgres_connect(dsn: str) -> ScriptedConnection:
        return fixture if dsn == config.environment["PILLARMESH_POSTGRES_FIXTURE_DSN"] else runtime

    actions = LiveProviderActions(
        config,
        postgres_connect=postgres_connect,
        snowflake_connect=lambda **_kwargs: ScriptedConnection(_snowflake_router(config)),
    )

    with pytest.raises(HarnessError, match="dedicated environment attestation failed"):
        validate_attestation(config, actions.preflight())


def test_live_provider_admission_uses_stable_database_lock_and_releases_it(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    def route(query: object, _parameters: tuple[Any, ...] | None) -> Result:
        text = str(query)
        if "pg_try_advisory_lock" in text:
            return ((("m0_acceptance", True),), ())
        if "pg_advisory_unlock" in text:
            return (((True,),), ())
        raise AssertionError(text)

    connection = ScriptedConnection(route)
    actions = LiveProviderActions(config, postgres_connect=lambda _dsn: connection)

    with actions.admission(config.environment_identity):
        assert not connection.closed

    assert connection.closed
    assert [query for query, _params in connection.statements] == [
        "SELECT current_database(), pg_try_advisory_lock(%s, %s)",
        "SELECT pg_advisory_unlock(%s, %s)",
    ]


def test_live_provider_admission_rejects_a_lock_held_on_another_host(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def route(query: object, _parameters: tuple[Any, ...] | None) -> Result:
        assert "pg_try_advisory_lock" in str(query)
        return ((("m0_acceptance", False),), ())

    connection = ScriptedConnection(route)
    actions = LiveProviderActions(config, postgres_connect=lambda _dsn: connection)

    with (
        pytest.raises(HarnessError, match="already admitted"),
        actions.admission(config.environment_identity),
    ):
        pass

    assert connection.closed
    assert len(connection.statements) == 1


def test_metadata_allowlist_is_exact_and_does_not_exclude_broad_information_schema() -> None:
    assert all(_is_allowed_metadata_query(query) for query in _SNOWFLAKE_METADATA_QUERIES)
    assert not _is_allowed_metadata_query("SELECT * FROM INFORMATION_SCHEMA.TABLES")
    assert not _is_allowed_metadata_query(
        "SELECT * FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='ORDERS'"
    )


def test_live_replay_and_negative_proofs_map_rows_stage_and_tagged_history(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    committed = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    row = (KEY, "synthetic-row-canary", Decimal("10.50"), "USD", "acceptance", committed)
    target = "PILLARMESH_M0.TRANSFER.ORDERS"
    negative = "PILLARMESH_M0.TRANSFER.ORDERS_UNSUPPORTED_KEY"
    ledger = "PILLARMESH_M0.TRANSFER.COMMIT_LEDGER"
    stage = "PILLARMESH_M0.TRANSFER.M0_STAGE"
    history = (
        ("SELECT", _SNOWFLAKE_METADATA_QUERIES[0]),
        ("SELECT", "SELECT * FROM INFORMATION_SCHEMA.TABLES"),
        ("UPDATE", f"UPDATE {target} SET order_status='acceptance'"),
    )

    def snowflake_route(query: object, _parameters: tuple[Any, ...] | None) -> Result:
        text = str(query)
        if text.startswith("SELECT order_id,customer_ref"):
            return ((row,), ())
        if text.startswith("SELECT manifest_digest,committed_at"):
            return ((("1" * 64, committed),), ())
        if text == f"LIST @{stage}/runs/batch-opaque":
            return ((("part-000.csv", 128, "etag"),), ())
        if "QUERY_HISTORY_BY_USER" in text:
            return (history, ())
        if text == f"SELECT COUNT(*),HASH_AGG(*) FROM {target}":
            return (((1, "positive-hash"),), ())
        if text == f"SELECT COUNT(*),HASH_AGG(*) FROM {negative}":
            return (((0, None),), ())
        if text == f"LIST @{stage}":
            return ((("part-000.csv", 128, "etag"),), ())
        if text == f"SELECT COUNT(*),HASH_AGG(*) FROM {ledger}":
            return (((1, "ledger-hash"),), ())
        raise AssertionError(text)

    def postgres_route(query: object, _parameters: tuple[Any, ...] | None) -> Result:
        assert "runtime_source_read_count" in str(query)
        return (((7,),), ())

    actions = LiveProviderActions(
        config,
        postgres_connect=lambda dsn: ScriptedConnection(postgres_route),
        snowflake_connect=lambda **_kwargs: ScriptedConnection(snowflake_route),
    )

    replay = actions.replay_observation(KEY, "batch-opaque")
    negative_observation = actions.negative_observation()

    assert replay.target_rows == 1
    assert replay.ledger_rows == 1
    assert replay.stage_state_digest
    assert replay.product_mutation_count == 1
    assert negative_observation.postgres_source_data_read_count == 7
    assert negative_observation.snowflake_product_data_query_count == 2
    assert negative_observation.metadata_observation_count == 1
    assert negative_observation.positive_target_state_digest


def test_live_destination_absence_proof_rejects_an_existing_key(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def route(_query: object, parameters: tuple[Any, ...] | None) -> Result:
        assert parameters == (KEY,)
        return (((1,),), ())

    actions = LiveProviderActions(
        config, snowflake_connect=lambda **_kwargs: ScriptedConnection(route)
    )

    with pytest.raises(HarnessError, match="fresh acceptance key"):
        actions.prove_destination_absent(KEY)
