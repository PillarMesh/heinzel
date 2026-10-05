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

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import psycopg
from psycopg import sql

__all__ = [
    "DEMO_MAX_WRITE_TRANSACTION_DURATION",
    "DEMO_RETAINED_GENERATION_OWNER",
    "DEMO_SOURCE_DAYS",
    "DEMO_WAREHOUSE_CONNECT_BUDGET",
    "DEMO_WAREHOUSE_ROLES",
    "DemoWarehouseRoles",
    "ProvisioningRefused",
    "WarehouseUnreachable",
    "demo_warehouse_is_provisioned",
    "grant_demo_product_read",
    "provision_demo_warehouse",
    "role_dsn",
    "seal_demo_product_generation",
    "set_demo_role_passwords",
    "wait_for_demo_warehouse",
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
DEMO_SOURCE_DAYS = ("2026-09-10", "2026-09-11", "2026-09-12")

# How long a write to the source might still be inside an open transaction. The acquisition
# provider reads a snapshot up to `snapshot_time - this` and no further, because a row written
# more recently could still be rolled back. It lives here rather than beside the provider
# settings in `generation.py` so that the seed and the bound that excludes it are one decision:
# rows seeded inside the bound are read by nothing, and the acquisition returns empty.
DEMO_MAX_WRITE_TRANSACTION_DURATION = timedelta(minutes=5)

# Who owns a committed product generation. A role that cannot log in, because the answer path
# refuses to read a generation whose owner can: `PostgreSQLAnswerQueryProvider` checks that the
# relation's owner is not a login role and that no login role holds `INSERT`, `UPDATE`, `DELETE`
# or `TRUNCATE` on it, and reports `integrity_failure` otherwise. A generation nobody can write
# is what makes the receipt describing it still true when the answer is read.
DEMO_RETAINED_GENERATION_OWNER = "retained_generation_owner"
_SEED_ORDERS = (
    (1, 101, DEMO_SOURCE_DAYS[0], "10.00"),
    (2, 102, DEMO_SOURCE_DAYS[0], "20.00"),
    (3, 103, DEMO_SOURCE_DAYS[1], "100.00"),
    (4, 104, DEMO_SOURCE_DAYS[1], "25.50"),
    (5, 105, DEMO_SOURCE_DAYS[2], "99.00"),
)


# The schemas whose presence means this demonstration already provisioned the database. Named
# once, because the check that refuses a provisioned database and the check that recognises its
# own earlier work must agree: a schema in one list and not the other is a second start that
# either re-provisions over itself or refuses a database it could have resumed.
_DEMONSTRATION_SCHEMAS = (
    "source_data",
    "private_admin",
    "raw",
    "land_control",
    "product_control",
    "consumption",
)


# How long the console waits for the warehouse to accept connections before giving up. Compose
# holds the first start back until the warehouse reports healthy, so this is for the starts it
# does not: `docker compose restart` restarts both containers without re-reading `depends_on`, so
# the console comes back while PostgreSQL is still starting. A minute is far longer than a local
# PostgreSQL takes to accept connections and short enough that an address nothing is listening on
# is reported rather than waited out.
DEMO_WAREHOUSE_CONNECT_BUDGET = timedelta(seconds=60)
_CONNECT_POLL_SECONDS = 1.0

# `cannot_connect_now`: the server answered and said it is still starting up. Worth waiting for,
# unlike every other state it has a code for.
_STARTING_UP = "57P03"


class _ClosableConnection(Protocol):
    """What the wait below needs of a connection: that it can be given back.

    Narrower than `psycopg.Connection` on purpose. The wait opens a connection to learn one
    thing -- whether the warehouse accepts one -- and closes it again, so a caller substituting
    its own connect does not have to produce a whole driver connection to be checked here.
    """

    def close(self) -> None: ...


class WarehouseUnreachable(RuntimeError):
    """The warehouse did not accept a connection inside the budget."""


class ProvisioningRefused(RuntimeError):
    """The database is not an empty one this demonstration may provision."""


@dataclass(frozen=True, slots=True)
class DemoWarehouseRoles:
    """The least-privilege logins the demonstration's providers connect as.

    Five rather than three, because reading the product to answer a question is not the same
    privilege as writing it. `answer` and `estimator` are granted `SELECT` on the materialized
    product and nothing else, so neither can reach the landed rows the product was built from,
    and neither can write anything at all.
    """

    acquisition: str
    landing: str
    materialization: str
    answer: str
    estimator: str

    def __iter__(self) -> Iterator[str]:
        """Every role name, so a caller that must cover all of them cannot miss one."""
        return iter(
            (self.acquisition, self.landing, self.materialization, self.answer, self.estimator)
        )


# The logins the demonstration's providers connect as. One instance rather than a literal per
# caller, because provisioning, password rotation and every provider DSN have to name the same
# roles. `answer_runtime` is not a name chosen here: `PostgreSQLAnswerQuerySettings` declares
# `principal_class` as the literal `answer_runtime`, so that is the principal the provider says
# it connects as.
DEMO_WAREHOUSE_ROLES = DemoWarehouseRoles(
    acquisition="acquisition_runtime",
    landing="landing_runtime",
    materialization="materialization_runtime",
    answer="answer_runtime",
    estimator="query_estimator",
)


def wait_for_demo_warehouse(
    bootstrap_dsn: str,
    *,
    connect: Callable[[str], _ClosableConnection] = psycopg.connect,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    budget: timedelta = DEMO_WAREHOUSE_CONNECT_BUDGET,
) -> None:
    """Wait for the warehouse to accept a connection, or refuse saying it never did.

    Only a failure that is not the server's answer is waited through: a refused or reset
    connection, which `psycopg` reports with no `sqlstate` because nothing on the other end
    produced one, and `cannot_connect_now`, which is the server saying it is still starting.
    Anything else -- a password that is wrong, a database that does not exist -- is the server
    answering, and waiting for an answer to change would turn a misconfiguration into a minute of
    silence followed by the same error.

    This exists because the console's first act is to connect, and nothing it does afterwards
    would retry. Compose waits for the warehouse's healthcheck before starting the console, which
    covers the first start; `docker compose restart` does not honour `depends_on` at all, and
    without this the console would exit while PostgreSQL was still coming back up.
    """
    deadline = monotonic() + budget.total_seconds()
    while True:
        try:
            connect(bootstrap_dsn).close()
            return
        except psycopg.OperationalError as unreachable:
            if unreachable.sqlstate is not None and unreachable.sqlstate != _STARTING_UP:
                raise
            if monotonic() >= deadline:
                raise WarehouseUnreachable(
                    f"the warehouse did not accept a connection within {budget}"
                ) from unreachable
        sleep(_CONNECT_POLL_SECONDS)


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
        (list(_DEMONSTRATION_SCHEMAS),),
    ).fetchall()
    if existing:
        raise ProvisioningRefused(
            "this database already carries the demonstration's schemas "
            f"({', '.join(sorted(str(row[0]) for row in existing))}). Start from an empty "
            "database, or remove the volume with `docker compose down -v`."
        )


def _require_outside_the_lag_bound(
    connection: psycopg.Connection[tuple[object, ...]], seeded_at: datetime
) -> None:
    """Refuse a watermark the acquisition's own snapshot bound would exclude.

    The provider reads up to `snapshot_time - DEMO_MAX_WRITE_TRANSACTION_DURATION`, so rows
    stamped later than that are read by nothing and the acquisition returns empty -- with no
    error, because reading no rows is a legitimate outcome. Checked against the server's clock
    rather than this process's, because the server's is the one the provider compares against.
    """
    row = connection.execute("SELECT now()").fetchone()
    if row is None or not isinstance(row[0], datetime):
        raise ProvisioningRefused("the warehouse did not report its own clock")
    bound = row[0].astimezone(UTC) - DEMO_MAX_WRITE_TRANSACTION_DURATION
    if seeded_at > bound:
        raise ProvisioningRefused(
            "the seeded watermark is inside the acquisition's write-transaction lag bound, so "
            "its snapshot would read no rows at all. Seed at least "
            f"{DEMO_MAX_WRITE_TRANSACTION_DURATION} before the warehouse's own clock."
        )


def grant_demo_product_read(
    bootstrap_dsn: str, *, roles: DemoWarehouseRoles, namespace: str, relation_name: str
) -> None:
    """Let the answer and estimator roles read the materialized product, and nothing more.

    Granted once the product exists rather than at provisioning time, because the relation is
    named by the materialization. `GRANT` is idempotent, so a restart that found an earlier
    generation runs this again and ends in the same place; that also repairs a database whose
    grants were lost without its data.

    `SELECT` on this one relation, never `ALL` and never on the schema: the answer path reads
    the approved product and must not be able to reach the landed rows behind it, and the
    estimator explains a statement rather than running one.

    The answer role also reads `product_control`, where the materialization records which
    generation is current and which relation carries it. `PostgreSQLProductGenerationAuthority`
    refuses an answer over a generation it cannot observe there, so withholding this would make
    every answer refuse. `SELECT` alone: the answer path reads which generation is addressable
    and never changes it. The estimator is not given it -- it explains one statement and has no
    business knowing what else was ever materialized.
    """
    with psycopg.connect(bootstrap_dsn) as connection:
        for role in (roles.answer, roles.estimator):
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(namespace), sql.Identifier(role)
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    sql.Identifier(namespace),
                    sql.Identifier(relation_name),
                    sql.Identifier(role),
                )
            )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA product_control TO {}").format(
                sql.Identifier(roles.answer)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT ON product_control.product_generations, "
                "product_control.product_generation_pointers TO {}"
            ).format(sql.Identifier(roles.answer))
        )


def seal_demo_product_generation(
    bootstrap_dsn: str, *, roles: DemoWarehouseRoles, namespace: str, relation_name: str
) -> None:
    """Hand the committed generation to an owner that cannot log in, and leave it read-only.

    `PostgreSQLAnswerQueryProvider` refuses to read a generation whose relation is owned by a role
    that can log in, or on which any login role holds `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE`,
    and reports `integrity_failure`. That is not incidental: the materialization receipt says what
    this relation holds, and a relation a live login can still rewrite makes the receipt a
    statement about the past rather than about the data being read.

    dbt creates the relation as the materialization role, which is a login role, so a generation
    is unsealed until this runs. Idempotent, like the grants beside it: a restart that found an
    earlier generation runs it again and ends in the same place.

    The materialization role keeps `SELECT`, which it needs to observe the generation it
    committed. It loses everything else, including by losing ownership.
    """
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                sql.Identifier(namespace),
                sql.Identifier(relation_name),
                sql.Identifier(DEMO_RETAINED_GENERATION_OWNER),
            )
        )
        connection.execute(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {}.{} FROM {}").format(
                sql.Identifier(namespace),
                sql.Identifier(relation_name),
                sql.Identifier(roles.materialization),
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                sql.Identifier(namespace),
                sql.Identifier(relation_name),
                sql.Identifier(roles.materialization),
            )
        )


def demo_warehouse_is_provisioned(bootstrap_dsn: str) -> bool:
    """Whether this database already carries the demonstration's schemas.

    Asked before provisioning rather than inferred from a refusal, because `ProvisioningRefused`
    also covers a database that is somebody else's: a start that treated every refusal as its
    own earlier work would carry on against a database it has no business in.

    All of the schemas, not any of them: a database holding some is a provisioning that failed
    partway, which is a state to report rather than one to resume from.
    """
    with psycopg.connect(bootstrap_dsn) as connection:
        found = connection.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
            (list(_DEMONSTRATION_SCHEMAS),),
        ).fetchall()
    return {str(row[0]) for row in found} == set(_DEMONSTRATION_SCHEMAS)


def set_demo_role_passwords(
    bootstrap_dsn: str, *, roles: DemoWarehouseRoles, passwords: Mapping[str, str]
) -> None:
    """Give every one of the demonstration's login roles these passwords, whatever they held.

    This is what lets the demonstration keep no password anywhere. A restart generates a fresh
    one per role and sets them here, so the credentials live only in the process that uses them:
    nothing is written to the state volume, and a reader of the volume finds no secret because
    none was ever stored.

    The password is a literal in the statement rather than a parameter, because `ALTER ROLE` is
    a utility statement and takes none. The driver's own message is dropped on failure for the
    same reason: it quotes the failing statement, and with it the password.
    """
    missing = tuple(role for role in roles if role not in passwords)
    if missing:
        raise ProvisioningRefused(f"no password was given for {', '.join(missing)}")
    with psycopg.connect(bootstrap_dsn) as connection:
        for role in roles:
            password = passwords[role]
            try:
                connection.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            except psycopg.Error:
                # Reported without the driver's message, which carries the failing statement
                # and with it the password, into the container log.
                raise ProvisioningRefused(f"could not set the {role} role's password") from None


def provision_demo_warehouse(
    bootstrap_dsn: str,
    *,
    roles: DemoWarehouseRoles,
    passwords: Mapping[str, str],
    seeded_at: datetime,
) -> None:
    """Create the demonstration's roles, schemas, seed source rows and grants.

    `seeded_at` is the seeded rows' `updated_at`, and so the watermark the acquisition measures
    and the product's freshness observation carries. It is the moment the source was populated
    rather than a date written beside the rows: the governed answer refuses a product staler
    than its scope policy allows, and a watermark fixed in the past grows staler every day until
    no policy a reader would write could admit it. The order days stay fixed -- a row placed
    three weeks ago and written to the warehouse just now is not a stale read of the source,
    it is an up-to-date read of old orders.

    `answer` and `estimator` are created here and granted nothing: the product they read does
    not exist until it has been materialized, and `grant_demo_product_read` gives them `SELECT`
    on it then. A grant written here would have to name a relation that is not there.
    """
    if seeded_at.tzinfo is None or seeded_at.utcoffset() != timedelta(0):
        raise ProvisioningRefused("the seeded watermark must be timezone-aware UTC")
    missing = tuple(role for role in roles if role not in passwords)
    if missing:
        raise ProvisioningRefused(f"no password was given for {', '.join(missing)}")
    with psycopg.connect(bootstrap_dsn) as connection:
        _require_unprovisioned(connection)
        _require_outside_the_lag_bound(connection, seeded_at)
        for role in roles:
            _create_role(connection, role, passwords[role])
        # No password, because nothing connects as it. It exists to own committed generations,
        # which is a thing a role can do without being able to log in -- and must, here.
        try:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(DEMO_RETAINED_GENERATION_OWNER))
            )
        except psycopg.Error:
            raise ProvisioningRefused(
                f"could not create the {DEMO_RETAINED_GENERATION_OWNER} role"
            ) from None

        # Anything PUBLIC holds, the roles above hold, so whatever would let them create
        # objects outside the grants below is removed here. PostgreSQL 15 and later give PUBLIC
        # neither CREATE on a database nor CREATE on `public`, so the first of these is a guard
        # against a cluster where somebody granted it rather than a default being undone; USAGE
        # on `public` is held by default and this is what removes it.
        #
        # The database is the one this connection is on rather than a name written here.
        # Database-level privileges live in a shared catalog, so `REVOKE ... ON DATABASE
        # postgres` succeeds from any database and would act somewhere else entirely, leaving
        # the database the demonstration runs in as it found it.
        connection.execute(
            sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(connection.info.dbname)
            )
        )
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
                (order_id, customer_id, ordered_on, order_total, seeded_at),
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
