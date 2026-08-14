from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, Literal

import snowflake.connector
from pillarmesh_provider_sdk import ProviderObservation
from pillarmesh_provider_snowflake import SnowflakeProvider, SnowflakeSettings

type KeyConstraint = Literal["primary_key", "unique", "none"]

_SINGLE_COLUMN_PRIMARY_KEY = re.compile(
    r'\bPRIMARY\s+KEY\s*\(\s*(?P<column>"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)\s*\)',
    re.IGNORECASE,
)


def connect_to_localstack(**kwargs: object) -> Any:
    return snowflake.connector.connect(
        **kwargs,
        host="snowflake.localhost.localstack.cloud",
        port=4566,
        protocol="http",
    )


def localstack_settings() -> SnowflakeSettings:
    return SnowflakeSettings(
        account="test",
        user="test",
        password="test",
        role="test",
        warehouse="PILLARMESH_M0_WH",
        database="PILLARMESH_M0",
        schema_name="TRANSFER",
        stage="M0_STAGE",
        target_table="ORDERS",
        ledger_table="COMMIT_LEDGER",
        connection_handle="localstack-snowflake",
    )


class LocalStackSnowflakeProvider(SnowflakeProvider):
    """Use LocalStack's supported DDL metadata without changing production observation."""

    def _key_constraint(self, cursor: Any, table_name: str) -> tuple[str | None, KeyConstraint]:
        cursor.execute(
            "SELECT GET_DDL('TABLE', %s)",
            (self._qualified(table_name),),
        )
        row = cursor.fetchone()
        if row is None:
            return None, "none"
        match = _SINGLE_COLUMN_PRIMARY_KEY.search(str(row[0]))
        if match is None:
            return None, "none"
        return match.group("column").strip('"').lower(), "primary_key"


def localstack_provider() -> LocalStackSnowflakeProvider:
    return LocalStackSnowflakeProvider(localstack_settings(), connect=connect_to_localstack)


def assert_expected_observation(observation: ProviderObservation) -> None:
    def require(field: str, actual: object, expected: object) -> None:
        if actual != expected:
            raise AssertionError(f"{field} mismatch: expected={expected!r}, actual={actual!r}")

    require("object_kind", observation.object_kind, "base_table")
    target_columns = tuple(
        (column.name, column.type_name, column.nullable) for column in observation.columns
    )
    require(
        "columns",
        target_columns,
        (
            ("order_id", "NUMBER(19,0)", False),
            ("customer_ref", "VARCHAR(65535)", False),
            ("amount", "NUMBER(18,2)", False),
            ("currency", "VARCHAR(3)", False),
            ("order_status", "VARCHAR(65535)", False),
            ("updated_at", "TIMESTAMP_TZ(6)", False),
        ),
    )
    require("key_name", observation.key_name, None)
    require("key_type", observation.key_type, None)
    require("key_nullable", observation.key_nullable, None)
    require("key_constraint", observation.key_constraint, "none")
    require("commit_ledger_object_kind", observation.commit_ledger_object_kind, "base_table")
    ledger_columns = tuple(
        (column.name, column.type_name, column.nullable)
        for column in observation.commit_ledger_columns or ()
    )
    require(
        "commit_ledger_columns",
        ledger_columns,
        (
            ("batch_id", "VARCHAR(16777216)", False),
            ("manifest_digest", "VARCHAR(64)", False),
            ("committed_at", "TIMESTAMP_TZ(9)", False),
        ),
    )
    require("commit_ledger_key_name", observation.commit_ledger_key_name, None)
    require(
        "commit_ledger_key_constraint",
        observation.commit_ledger_key_constraint,
        "none",
    )


def wait_for_expected_observation(
    observe: Callable[[], ProviderObservation],
    *,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            assert_expected_observation(observe())
        except Exception as error:
            last_error = error
            sleep(1)
        else:
            return
    if last_error is None:
        raise RuntimeError("LocalStack Snowflake initialization did not complete")
    raise RuntimeError(
        "LocalStack Snowflake initialization did not complete; "
        f"last observation failure: {type(last_error).__name__}: {last_error}"
    ) from last_error
