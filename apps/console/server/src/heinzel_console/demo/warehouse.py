"""The demonstration's own PostgreSQL warehouse: provisioning and seed source rows.

The demonstration reaches a governed answer only if a real product generation exists to
ask it of, and a generation exists only if source rows were acquired, landed and
materialized. This module provisions the warehouse that holds them.

Nothing here is a shortcut around the governed path. The roles are separated the way the
providers expect -- acquisition reads the approved source columns and nothing else,
landing writes the raw relation and its receipts, materialization reads raw and writes the
product control tables -- so a provider that reaches past its grant fails here as it would
anywhere. A demonstration that granted one superuser role would prove nothing about the
boundaries it exists to show.

The demonstration owns this database. Provisioning is refused rather than merged into a
database that already holds objects, because the alternative is a half-provisioned
warehouse whose failures read as product defects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import sql

__all__ = [
    "DemoWarehouseRoles",
    "ProvisioningRefused",
    "provision_demo_warehouse",
    "role_dsn",
]

# The columns acquisition is allowed to read. The source table carries more, and the grant
# below names these alone so that a provider asking for another column is refused by
# PostgreSQL rather than by a check this demonstration would have to write itself.
_APPROVED_SOURCE_COLUMNS = ("order_id", "customer_id", "ordered_on", "order_total", "updated_at")

# The demonstration's own source, named for the semantic version it is published under:
# `customer_orders` carrying the `order_total` the `daily-order-value` metric sums. A source
# shaped for some other story would make the seeded question unanswerable over its own data.
SOURCE_SCHEMA = "source_data"
SOURCE_TABLE = "customer_orders"

# Fixed so that the demonstration's numbers are the same on every machine, and a reader
# comparing a screenshot to their own container sees no difference. Three days, so the
# answer has more than one row and a reader can see the grouping is real:
# 2026-09-10 -> 30.00, 2026-09-11 -> 125.50, 2026-09-12 -> 99.00.
_SEED_OBSERVED_AT = datetime(2026, 9, 12, tzinfo=UTC)
_SEED_ORDERS = (
    (1, 101, "2026-09-10", "10.00"),
    (2, 102, "2026-09-10", "20.00"),
    (3, 103, "2026-09-11", "100.00"),
    (4, 104, "2026-09-11", "25.50"),
    (5, 105, "2026-09-12", "99.00"),
)


class ProvisioningRefused(RuntimeError):
    """The database is not an empty one this demonstration may provision."""


@dataclass(frozen=True, slots=True)
class DemoWarehouseRoles:
    """The three least-privilege logins the demonstration's providers connect as."""

    acquisition: str
    landing: str
    materialization: str


def role_dsn(bootstrap_dsn: str, role: str, password: str) -> str:
    """The bootstrap DSN with its login replaced, keeping host, port and database."""
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=str(parsed["host"]),
        port=str(parsed["port"]),
        dbname=str(parsed["dbname"]),
        user=role,
        password=password,
    )


def _create_role(
    connection: psycopg.Connection[tuple[object, ...]], role: str, password: str
) -> None:
    """Create one login role.

    The password reaches PostgreSQL as a literal in the statement, so a failure here is
    reported without the driver's message: libpq puts the failing statement in it, and the
    demonstration's generated passwords would reach the container log with it.
    """
    try:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    except psycopg.Error:
        raise ProvisioningRefused(f"could not create the {role} role") from None


def _require_unprovisioned(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """Refuse a database that already carries this demonstration's schemas.

    Re-running provisioning over them would fail partway, leaving grants from one run and
    tables from another. The container's named volume carries the state directory, not this
    database, so a second start against a provisioned database is a configuration to report
    rather than a state to repair.
    """
    existing = connection.execute(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
        (["source_data", "raw", "land_control", "product_control", "consumption"],),
    ).fetchall()
    if existing:
        raise ProvisioningRefused(
            "this database already carries the demonstration's schemas "
            f"({', '.join(sorted(str(row[0]) for row in existing))}). Start from an empty "
            "database, or remove the volume with `docker compose down -v`."
        )


def provision_demo_warehouse(
    bootstrap_dsn: str,
    *,
    roles: DemoWarehouseRoles,
    acquisition_password: str,
    landing_password: str,
    materialization_password: str,
) -> None:
    """Create the demonstration's roles, schemas, seed source rows and grants."""
    with psycopg.connect(bootstrap_dsn) as connection:
        _require_unprovisioned(connection)
        _create_role(connection, roles.acquisition, acquisition_password)
        _create_role(connection, roles.landing, landing_password)
        _create_role(connection, roles.materialization, materialization_password)

        # PUBLIC holds CREATE on the database and on `public` by default, which would let
        # any of the three roles above create objects outside the grants below.
        connection.execute("REVOKE CREATE ON DATABASE postgres FROM PUBLIC")
        connection.execute("REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC")

        connection.execute("CREATE SCHEMA source_data")
        # Not decoration. The acquisition provider proves least privilege by checking that
        # the acquiring role holds no privilege on a relation outside its declaration, and
        # `PostgreSQLAcquisitionSettings.unrelated_schema_name` names where to look. Without
        # a schema and a relation here there is nothing to prove the role cannot reach, and
        # the provider refuses the acquisition `authorization_denied`.
        connection.execute("CREATE SCHEMA private_admin")
        connection.execute("CREATE SCHEMA raw")
        connection.execute("CREATE SCHEMA land_control")
        connection.execute("CREATE SCHEMA product_control")
        connection.execute(
            sql.SQL("CREATE SCHEMA consumption AUTHORIZATION {}").format(
                sql.Identifier(roles.materialization)
            )
        )

        connection.execute(
            # `ordered_on` is text rather than `date` because the acquisition provider
            # admits int2/int4/int8, numeric, bool, text/varchar and timestamptz alone: a
            # `date` column raises "PostgreSQL column type is not admitted" and cannot be
            # acquired at all. That is the provider's gap, not a modelling preference here.
            #
            # An ISO day string rather than a midnight `timestamptz` so that grouping by it
            # is the day structurally. A timestamp would group correctly only while every
            # seeded order sits exactly on midnight, which is a trap for whoever edits the
            # seed next.
            "CREATE TABLE source_data.customer_orders ("
            "order_id bigint PRIMARY KEY, customer_id bigint NOT NULL, "
            "ordered_on text NOT NULL, order_total numeric NOT NULL, "
            "updated_at timestamptz NOT NULL)"
        )
        for order_id, customer_id, ordered_on, order_total in _SEED_ORDERS:
            connection.execute(
                "INSERT INTO source_data.customer_orders VALUES (%s, %s, %s, %s, %s)",
                (order_id, customer_id, ordered_on, order_total, _SEED_OBSERVED_AT),
            )

        connection.execute("CREATE TABLE private_admin.secrets (secret_value text NOT NULL)")
        connection.execute(
            "CREATE TABLE raw.raw_customer_orders ("
            "generation_id text NOT NULL, row_ordinal bigint NOT NULL, "
            "segment_digest text NOT NULL, payload jsonb NOT NULL, "
            "PRIMARY KEY (generation_id, row_ordinal))"
        )
        connection.execute(
            "CREATE TABLE land_control.land_receipts (idempotency_key text PRIMARY KEY, "
            "generation_id text UNIQUE NOT NULL, receipt_payload jsonb NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE product_control.product_generations ("
            "tenant_id text NOT NULL, product_id text NOT NULL, product_revision bigint NOT NULL, "
            "product_generation bigint NOT NULL, generation_schema text NOT NULL, "
            "generation_table text NOT NULL, provider_commit_reference text NOT NULL, "
            "retained_until timestamptz NOT NULL, PRIMARY KEY "
            "(tenant_id, product_id, product_revision, product_generation))"
        )
        connection.execute(
            "CREATE TABLE product_control.product_generation_pointers ("
            "tenant_id text NOT NULL, product_id text NOT NULL, product_revision bigint NOT NULL, "
            "product_generation bigint NOT NULL, generation_schema text NOT NULL, "
            "generation_table text NOT NULL, provider_commit_reference text NOT NULL, "
            "retained_until timestamptz NOT NULL, updated_at timestamptz NOT NULL, "
            "PRIMARY KEY (tenant_id, product_id))"
        )

        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA source_data TO {}").format(
                sql.Identifier(roles.acquisition)
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT ({}) ON source_data.customer_orders TO {}").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in _APPROVED_SOURCE_COLUMNS),
                sql.Identifier(roles.acquisition),
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA raw, land_control TO {}").format(
                sql.Identifier(roles.landing)
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON raw.raw_customer_orders TO {}").format(
                sql.Identifier(roles.landing)
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON land_control.land_receipts TO {}").format(
                sql.Identifier(roles.landing)
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA raw TO {}").format(sql.Identifier(roles.materialization))
        )
        connection.execute(
            sql.SQL("GRANT SELECT ON raw.raw_customer_orders TO {}").format(
                sql.Identifier(roles.materialization)
            )
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA product_control TO {}").format(
                sql.Identifier(roles.materialization)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE ON product_control.product_generations, "
                "product_control.product_generation_pointers TO {}"
            ).format(sql.Identifier(roles.materialization))
        )
