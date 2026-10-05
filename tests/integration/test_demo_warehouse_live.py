"""The demonstration's warehouse is provisioned with the boundaries it exists to show.

The quickstart reaches a governed answer only over a real warehouse, and a warehouse whose
roles could all read everything would demonstrate nothing about the separation the
providers rely on. These run against a cluster started here, so the grants are checked by
PostgreSQL rather than by a reading of the statements that create them.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from heinzel_console.demo.bootstrap import ensure_demo_generation
from heinzel_console.demo.catalog import (
    compose_demo_product_catalog,
    demo_source_freshness_observation,
)
from heinzel_console.demo.generation import acquire_demo_rows, land_demo_rows
from heinzel_console.demo.materialization import (
    DEMO_GROUP_COLUMN,
    DEMO_MEASURE_COLUMN,
    materialize_demo_generation,
    unadmitted_decision_digest,
)
from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import (
    DEMO_SOURCE_DAYS,
    DEMO_WAREHOUSE_ROLES,
    ProvisioningRefused,
    demo_warehouse_is_provisioned,
    provision_demo_warehouse,
    role_dsn,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_dbt_adapter import compiled_dbt_model_signing_bytes
from heinzel_provider_sdk.errors import AcquisitionProviderError
from heinzel_runtime import GenerationLedger
from psycopg import sql

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster

pytestmark = pytest.mark.live


# The moment the live tests' own warehouse was populated. Fixed so that a test can assert the
# watermark the acquisition measures, which is this rather than any date carried by the rows.
_SEEDED_AT = datetime(2026, 9, 13, tzinfo=UTC)


def _provisioned(bootstrap_dsn: str) -> dict[str, str]:
    """Provision the demonstration's warehouse, returning each role's password by role name."""
    passwords = {role: secrets.token_urlsafe(24) for role in DEMO_WAREHOUSE_ROLES}
    provision_demo_warehouse(
        bootstrap_dsn,
        roles=DEMO_WAREHOUSE_ROLES,
        passwords=passwords,
        seeded_at=_SEEDED_AT,
    )
    return passwords


def test_the_seeded_source_rows_are_the_daily_totals_the_demonstration_shows(
    tmp_path: Path,
) -> None:
    """The seeded orders are what the `daily-order-value` metric sums, on every machine."""
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        _provisioned(bootstrap_dsn)

        with psycopg.connect(bootstrap_dsn) as connection:
            totals = connection.execute(
                "SELECT ordered_on, sum(order_total) FROM source_data.customer_orders "
                "GROUP BY ordered_on ORDER BY ordered_on"
            ).fetchall()

        assert [(str(day), str(total)) for day, total in totals] == list(
            zip(DEMO_SOURCE_DAYS, ("30.00", "125.50", "99.00"), strict=True)
        )


def test_acquisition_reads_its_approved_columns_and_nothing_else(tmp_path: Path) -> None:
    """The grant is the boundary, so a provider reaching past it is refused by PostgreSQL."""
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        acquisition = role_dsn(
            bootstrap_dsn,
            DEMO_WAREHOUSE_ROLES.acquisition,
            passwords[DEMO_WAREHOUSE_ROLES.acquisition],
        )

        with psycopg.connect(acquisition) as connection:
            approved = connection.execute(
                "SELECT order_id, customer_id, ordered_on, order_total, updated_at "
                "FROM source_data.customer_orders"
            ).fetchall()
        assert len(approved) == 5

        # The landed relation belongs to landing and materialization, never to acquisition.
        with psycopg.connect(acquisition) as connection, pytest.raises(psycopg.Error):
            connection.execute("SELECT * FROM raw.raw_customer_orders")

        # No role can place objects outside its grants: PUBLIC's USAGE on `public` is
        # revoked, and PostgreSQL 15 and later give PUBLIC no CREATE there to begin with.
        with psycopg.connect(acquisition) as connection, pytest.raises(psycopg.Error):
            connection.execute("CREATE TABLE public.escaped (value text)")


def test_landing_may_write_the_raw_relation_but_not_read_the_source(tmp_path: Path) -> None:
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        landing = role_dsn(
            bootstrap_dsn, DEMO_WAREHOUSE_ROLES.landing, passwords[DEMO_WAREHOUSE_ROLES.landing]
        )

        with psycopg.connect(landing) as connection:
            connection.execute(
                "INSERT INTO raw.raw_customer_orders VALUES ('generation-1', 1, %s, %s)",
                ("0" * 64, '{"order_id": 1}'),
            )
            count = connection.execute("SELECT count(*) FROM raw.raw_customer_orders").fetchone()
            assert count == (1,)

        # Landing receives rows from acquisition; it never reads the source itself.
        with psycopg.connect(landing) as connection, pytest.raises(psycopg.Error):
            connection.execute("SELECT * FROM source_data.customer_orders")


def test_provisioning_a_database_that_already_carries_the_schemas_is_refused(
    tmp_path: Path,
) -> None:
    """Half-applied provisioning would read as a product defect, so it is refused instead."""
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        _provisioned(bootstrap_dsn)

        with pytest.raises(ProvisioningRefused) as refusal:
            _provisioned(bootstrap_dsn)

        assert "already carries the demonstration's schemas" in str(refusal.value)
        assert "docker compose down -v" in str(refusal.value)


def test_acquisition_and_landing_produce_a_generation_the_ledger_records(
    tmp_path: Path,
) -> None:
    """The landed relation arrives through the providers, under a receipt.

    Writing `raw.raw_customer_orders` directly would leave the demonstration with landed
    rows no receipt describes, which is the one thing a generation is for.
    """
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        publication = build_demo_publication(
            DemoStores(tmp_path / "state"), clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
        )

        acquired = acquire_demo_rows(
            role_dsn(
                bootstrap_dsn,
                DEMO_WAREHOUSE_ROLES.acquisition,
                passwords[DEMO_WAREHOUSE_ROLES.acquisition],
            ),
            tenant_id=publication.contract.tenant_id,
        )
        assert len(acquired.rows) == 5
        # The provider measures the watermark off the rows it read: when the source was last
        # written, not the business day the orders fall on. A watermark fixed to an order day
        # would grow staler every day until no scope policy could admit the product.
        assert acquired.watermark_at == _SEEDED_AT
        assert acquired.observed_at >= acquired.watermark_at

        ledger = GenerationLedger.in_memory()
        landed = asyncio.run(
            land_demo_rows(
                role_dsn(
                    bootstrap_dsn,
                    DEMO_WAREHOUSE_ROLES.landing,
                    passwords[DEMO_WAREHOUSE_ROLES.landing],
                ),
                acquired,
                contract=publication.contract,
                ledger=ledger,
            )
        )

        assert landed.receipt.record_count == 5
        assert landed.receipt.generation_id

        # The rows are in the warehouse, under the generation the receipt names.
        with psycopg.connect(bootstrap_dsn) as connection:
            landed_rows = connection.execute(
                "SELECT count(*) FROM raw.raw_customer_orders WHERE generation_id = %s",
                (landed.receipt.generation_id,),
            ).fetchone()
        assert landed_rows == (5,)


def test_a_privilege_outside_the_declaration_refuses_the_whole_acquisition(
    tmp_path: Path,
) -> None:
    """Least privilege is checked, not assumed, and a wider grant is refused outright.

    The provider does not quietly read its declared columns and ignore the rest: it
    observes what the connecting role may reach and refuses `authorization_denied` when
    that is more than the declaration. A demonstration whose role drifted wider would
    otherwise keep working and keep claiming a boundary it no longer had.
    """
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        acquisition = role_dsn(
            bootstrap_dsn,
            DEMO_WAREHOUSE_ROLES.acquisition,
            passwords[DEMO_WAREHOUSE_ROLES.acquisition],
        )

        # Provisioned as it stands, the acquisition succeeds.
        assert len(acquire_demo_rows(acquisition, tenant_id="tenant-demo").rows) == 5

        # One column more than the approved schema names.
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute("ALTER TABLE source_data.customer_orders ADD COLUMN memo text")
            connection.execute(
                "GRANT SELECT (memo) ON source_data.customer_orders TO acquisition_runtime"
            )

        with pytest.raises(AcquisitionProviderError) as refusal:
            acquire_demo_rows(acquisition, tenant_id="tenant-demo")
        assert "authorization_denied" in str(refusal.value)


def test_the_demonstration_materializes_and_publishes_one_generation(tmp_path: Path) -> None:
    """The whole chain: acquire, land, compile, materialize, publish, and the product is queryable.

    This is the generation the governed answer is asked of. Nothing here writes the
    consumption view itself -- dbt runs the compiler's own statement, and the runner
    commits the generation only once quality passes.

    The publication is asserted in the same test rather than its own, because reaching it costs
    a cluster and a dbt run: a second test would double the live suite's slowest path to check
    what this one already has in hand.
    """
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")

    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        database_name = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)["dbname"]
        assert isinstance(database_name, str)
        stores = DemoStores(tmp_path / "state")
        try:
            published = build_demo_publication(
                stores, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
            )
            contract = published.contract

            acquired = acquire_demo_rows(
                role_dsn(
                    bootstrap_dsn,
                    DEMO_WAREHOUSE_ROLES.acquisition,
                    passwords[DEMO_WAREHOUSE_ROLES.acquisition],
                ),
                tenant_id=contract.tenant_id,
            )
            ledger = GenerationLedger.in_memory()
            landed = asyncio.run(
                land_demo_rows(
                    role_dsn(
                        bootstrap_dsn,
                        DEMO_WAREHOUSE_ROLES.landing,
                        passwords[DEMO_WAREHOUSE_ROLES.landing],
                    ),
                    acquired,
                    contract=contract,
                    ledger=ledger,
                )
            )

            materialized = materialize_demo_generation(
                bootstrap_dsn=bootstrap_dsn,
                materialization_password=passwords[DEMO_WAREHOUSE_ROLES.materialization],
                dbt_executable=Path(dbt_executable),
                workspace=tmp_path / "materialization",
                contract=contract,
                landing_receipt_digest=digest(landed.receipt),
                generation_id=landed.receipt.generation_id,
                record_count=landed.receipt.record_count,
                ledger=ledger,
                materialization_ledger=stores.materialization_ledger,
                catalog=lambda namespace, relation_name: compose_demo_product_catalog(
                    stores=stores,
                    contract=contract,
                    semantic_version=published.semantic_version,
                    database_name=database_name,
                    namespace=namespace,
                    relation_name=relation_name,
                    group_column=DEMO_GROUP_COLUMN,
                    measure_column=DEMO_MEASURE_COLUMN,
                    generation=1,
                    freshness_observation=demo_source_freshness_observation(
                        landing_receipt_digest=digest(landed.receipt),
                        generation_id=landed.receipt.generation_id,
                        watermark_at=_SEEDED_AT,
                        observed_at=datetime.now(UTC),
                    ),
                    clock=lambda: datetime.now(UTC),
                ),
            )

            assert materialized.receipt.quality_disposition == "passed"
            assert materialized.receipt.output_row_count == 3

            # The product holds the daily totals the demonstration's answer reads.
            with psycopg.connect(bootstrap_dsn) as connection:
                rows = connection.execute(
                    sql.SQL(
                        "SELECT ordered_on, total_order_value FROM {}.{} ORDER BY ordered_on"
                    ).format(
                        sql.Identifier(materialized.target_schema),
                        sql.Identifier(materialized.model_name),
                    )
                ).fetchall()

            assert [(str(day), str(total)) for day, total in rows] == list(
                zip(
                    DEMO_SOURCE_DAYS,
                    ("30.000000000", "125.500000000", "99.000000000"),
                    strict=True,
                )
            )

            # The receipt follows back to a refusal rather than an approval.
            # `compile_product_iir` ends every product compilation at its governed gates, so no
            # admitted decision exists, and the demonstration must not look as though one does.
            assert materialized.receipt.legality_decision_digest == unadmitted_decision_digest(
                materialized.physical_plan
            )

            # The generation is consumable: the three authorities the governed answer reads are
            # all present, and all describe this run rather than merely existing.
            product_ref = ArtifactReference(
                artifact_id=contract.destination_product.product_name,
                version=contract.version,
                digest=digest(contract.destination_product),
            )
            metadata = stores.product_versions.read_current(
                tenant_id=contract.tenant_id, product_ref=product_ref, generation=1
            )
            assert metadata is not None
            assert metadata.lineage_digest == materialized.receipt.lineage_digest
            assert metadata.materialization_receipt_ref.digest == digest(materialized.receipt)

            binding = stores.query_bindings.read_current(
                tenant_id=contract.tenant_id, product_ref=product_ref, generation=1
            )
            assert binding is not None
            # The relation the answer would read is the one dbt wrote, not a name composed
            # beside it: the catalog is handed the materialized relation rather than asked for one.
            assert (binding.namespace, binding.relation_name) == (
                materialized.target_schema,
                materialized.model_name,
            )
            assert [each.column_name for each in binding.metric_bindings] == [DEMO_MEASURE_COLUMN]
            assert [each.column_name for each in binding.dimension_bindings] == [DEMO_GROUP_COLUMN]

            observation = stores.source_freshness.read_for_generation(
                tenant_id=contract.tenant_id,
                input_generation_digest=digest(landed.receipt),
            )
            assert observation is not None
            assert observation.watermark_at == _SEEDED_AT

            # The publication is round-trip verified: what the provider was told to publish
            # was read back from it identically, and the receipt names the table it describes.
            receipt = stores.product_publications.read_for_product_generation(
                tenant_id=contract.tenant_id, product_ref=product_ref, generation=1
            )
            assert receipt is not None
            assert receipt.table_fully_qualified_name.endswith(
                f".{database_name}.{materialized.target_schema}.{materialized.model_name}"
            )
        finally:
            stores.close()


def test_provisioning_revokes_public_create_on_the_database_it_runs_in(tmp_path: Path) -> None:
    """The revoke acts on the database being provisioned, not on one named in the statement.

    Database-level privileges live in a shared catalog, so `REVOKE ... ON DATABASE postgres`
    succeeds from any database and acts somewhere else. Every other live test here runs on a
    cluster whose database is `postgres`, where that mistake is invisible, and PostgreSQL 15 and
    later grant PUBLIC no CREATE on a database anyway -- so this grants it first, giving the
    revoke something to remove and the wrong database something to be caught by.
    """
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        with psycopg.connect(bootstrap_dsn, autocommit=True) as connection:
            connection.execute("CREATE DATABASE heinzel")
            connection.execute("GRANT CREATE ON DATABASE heinzel TO PUBLIC")
        named_dsn = psycopg.conninfo.make_conninfo(bootstrap_dsn, dbname="heinzel")

        passwords = _provisioned(named_dsn)

        with psycopg.connect(named_dsn) as connection:
            granted = connection.execute(
                "SELECT has_database_privilege('public', current_database(), 'CREATE')"
            ).fetchone()
            assert granted is not None and granted[0] is False

        # And the role that inherits from PUBLIC cannot create a schema of its own.
        acquisition = role_dsn(
            named_dsn, DEMO_WAREHOUSE_ROLES.acquisition, passwords[DEMO_WAREHOUSE_ROLES.acquisition]
        )
        with psycopg.connect(acquisition) as connection, pytest.raises(psycopg.Error):
            connection.execute("CREATE SCHEMA escaped")


def test_a_seed_inside_the_acquisition_lag_bound_is_refused(tmp_path: Path) -> None:
    """Seeding at the current instant would make the acquisition read nothing, silently.

    The provider's snapshot stops at `snapshot_time - max_write_transaction_duration`, because a
    row written more recently could still be rolled back. Rows stamped now are all inside that
    bound, so the acquisition succeeds and returns zero records -- which is a legitimate outcome
    for a source with no new rows, and so reports nothing a reader could act on.
    """
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        with pytest.raises(ProvisioningRefused, match="lag bound"):
            provision_demo_warehouse(
                bootstrap_dsn,
                roles=DEMO_WAREHOUSE_ROLES,
                passwords={role: secrets.token_urlsafe(24) for role in DEMO_WAREHOUSE_ROLES},
                seeded_at=datetime.now(UTC),
            )

        # Refused before anything was created, so the database is still one to provision.
        assert not demo_warehouse_is_provisioned(bootstrap_dsn)


def test_the_bootstrap_chain_publishes_one_generation_and_is_safe_to_run_again(
    tmp_path: Path,
) -> None:
    """The whole startup path, twice, and then with its state directory thrown away.

    The second run must find its own earlier generation rather than commit another, and must
    still hand back a connection that works: it rotates the roles' passwords on every start,
    because the demonstration stores none.
    """
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")

    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        stores = DemoStores(tmp_path / "state")
        try:
            publication = build_demo_publication(
                stores, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
            )
            first = ensure_demo_generation(
                bootstrap_dsn=bootstrap_dsn,
                stores=stores,
                publication=publication,
                dbt_executable=Path(dbt_executable),
                workspace=tmp_path / "first",
                clock=lambda: datetime.now(UTC),
            )
            second = ensure_demo_generation(
                bootstrap_dsn=bootstrap_dsn,
                stores=stores,
                publication=publication,
                dbt_executable=Path(dbt_executable),
                workspace=tmp_path / "second",
                clock=lambda: datetime.now(UTC),
            )

            assert (second.namespace, second.relation_name, second.generation) == (
                first.namespace,
                first.relation_name,
                first.generation,
            )

            # The receipt the governed answer answers from survived the run that wrote it, and
            # is readable without composing the runner: `in_memory` would have committed it to a
            # database that died with the call, leaving nothing to answer from and no sign of it.
            receipt = stores.materialization_receipts.read_receipt(
                tenant_id=publication.contract.tenant_id,
                product_id=first.product_ref.artifact_id,
                product_revision=first.product_ref.version,
                product_generation=first.generation,
            )
            assert receipt is not None
            assert receipt.quality_disposition == "passed"
            assert receipt.output_row_count == 3

            # The model the answer path re-verifies survived too, and the key stored beside it
            # verifies it. Without this the restart would answer with no output magnitude checks
            # at all, because the provider returns none when it is given no signed model.
            assert second.signed_model == first.signed_model
            second.signed_model.public_key.verify(
                base64.b64decode(second.signed_model.signed_model.signature),
                compiled_dbt_model_signing_bytes(second.signed_model.signed_model.model),
            )
            assert second.signed_model.signed_model.model_digest == receipt.compiled_model_digest

            # The committed generation is sealed: its owner cannot log in, and no role that can
            # log in may write it. `PostgreSQLAnswerQueryProvider` refuses to read a generation
            # that fails either, reporting `integrity_failure` and naming neither -- so this is
            # asserted here, where a regression says which property broke.
            with psycopg.connect(bootstrap_dsn) as connection:
                # The provider's own predicate, superuser exclusion and role membership
                # included: a superuser can write anything, so counting one would make the
                # property unsatisfiable, and a login role that is a member of the owner can
                # write through it without holding a grant of its own.
                sealed = connection.execute(
                    "SELECT owner.rolcanlogin, owner.rolsuper, relation.relkind, EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.rolcanlogin "
                    "AND NOT role.rolsuper "
                    "AND (pg_has_role(role.oid, relation.relowner, 'MEMBER') "
                    "OR has_table_privilege(role.oid, relation.oid, 'INSERT') "
                    "OR has_table_privilege(role.oid, relation.oid, 'UPDATE') "
                    "OR has_table_privilege(role.oid, relation.oid, 'DELETE') "
                    "OR has_table_privilege(role.oid, relation.oid, 'TRUNCATE'))) "
                    "FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner "
                    "WHERE namespace.nspname = %s AND relation.relname = %s",
                    (first.namespace, first.relation_name),
                ).fetchone()
            # A plain table, owned by a role that can neither log in nor bypass this, with no
            # login role able to write it. All four are what the provider requires.
            assert sealed == (False, False, "r", False)

            # Sealing takes the write away from the role that made it, and leaves the read.
            # Asked of the catalog rather than by connecting, because the chain generates its own
            # passwords and hands out only the two read-only roles' DSNs.
            with psycopg.connect(bootstrap_dsn) as connection:
                materialization_privileges = connection.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT'), "
                    "has_table_privilege(%s, %s, 'INSERT')",
                    (
                        DEMO_WAREHOUSE_ROLES.materialization,
                        f"{first.namespace}.{first.relation_name}",
                        DEMO_WAREHOUSE_ROLES.materialization,
                        f"{first.namespace}.{first.relation_name}",
                    ),
                ).fetchone()
            assert materialization_privileges == (True, False)

            # One generation, not two: the second run published nothing further.
            with psycopg.connect(bootstrap_dsn) as connection:
                committed = connection.execute(
                    "SELECT count(*) FROM product_control.product_generations "
                    "WHERE tenant_id = %s AND product_id = %s",
                    (publication.contract.tenant_id, first.product_ref.artifact_id),
                ).fetchone()
            assert committed is not None and committed[0] == 1

            # The second run's DSNs carry the passwords it set on this start, so reading the
            # product through them is what proves the rotation reached PostgreSQL.
            for dsn in (second.answer_dsn, second.estimator_dsn):
                with psycopg.connect(dsn) as connection:
                    rows = connection.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(second.namespace), sql.Identifier(second.relation_name)
                        )
                    ).fetchone()
                assert rows is not None and rows[0] == 3

            # The first run's passwords no longer work, which is what makes rotation rotation:
            # a start that added a password rather than replacing it would leave every password
            # the demonstration has ever generated valid.
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(first.answer_dsn).close()

            # Reading the approved product is the whole of what either role may do. Neither can
            # reach the landed rows the product was built from, and neither can write.
            for dsn in (second.answer_dsn, second.estimator_dsn):
                with psycopg.connect(dsn) as connection, pytest.raises(psycopg.Error):
                    connection.execute("SELECT * FROM raw.raw_customer_orders")
                with psycopg.connect(dsn) as connection, pytest.raises(psycopg.Error):
                    connection.execute("SELECT * FROM source_data.customer_orders")
                with psycopg.connect(dsn) as connection, pytest.raises(psycopg.Error):
                    connection.execute(
                        sql.SQL("DELETE FROM {}.{}").format(
                            sql.Identifier(second.namespace), sql.Identifier(second.relation_name)
                        )
                    )

        finally:
            stores.close()

        # The state directory discarded and the warehouse kept. Materializing now would commit
        # a generation the warehouse already holds, so it is refused with what to do instead.
        discarded = DemoStores(tmp_path / "state-two")
        try:
            with pytest.raises(ProvisioningRefused, match="disagree"):
                ensure_demo_generation(
                    bootstrap_dsn=bootstrap_dsn,
                    stores=discarded,
                    publication=build_demo_publication(
                        discarded, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
                    ),
                    dbt_executable=Path(dbt_executable),
                    workspace=tmp_path / "third",
                    clock=lambda: datetime.now(UTC),
                )
        finally:
            discarded.close()
