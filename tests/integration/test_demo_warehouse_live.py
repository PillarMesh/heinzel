"""The demonstration's warehouse is provisioned with the boundaries it exists to show.

The quickstart reaches a governed answer only over a real warehouse, and a warehouse whose
roles could all read everything would demonstrate nothing about the separation the
providers rely on. These run against a cluster started here, so the grants are checked by
PostgreSQL rather than by a reading of the statements that create them.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from heinzel_console.demo.generation import acquire_demo_rows, land_demo_rows
from heinzel_console.demo.materialization import (
    materialize_demo_generation,
    unadmitted_decision_digest,
)
from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import (
    DemoWarehouseRoles,
    ProvisioningRefused,
    provision_demo_warehouse,
    role_dsn,
)
from heinzel_contract_model import digest
from heinzel_provider_sdk.errors import AcquisitionProviderError
from heinzel_runtime import GenerationLedger
from psycopg import sql

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster

pytestmark = pytest.mark.live

_ROLES = DemoWarehouseRoles(
    acquisition="acquisition_runtime",
    landing="landing_runtime",
    materialization="materialization_runtime",
)


def _provisioned(bootstrap_dsn: str) -> dict[str, str]:
    passwords = {
        "acquisition": secrets.token_urlsafe(24),
        "landing": secrets.token_urlsafe(24),
        "materialization": secrets.token_urlsafe(24),
    }
    provision_demo_warehouse(
        bootstrap_dsn,
        roles=_ROLES,
        acquisition_password=passwords["acquisition"],
        landing_password=passwords["landing"],
        materialization_password=passwords["materialization"],
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

        assert [(str(day), str(total)) for day, total in totals] == [
            ("2026-09-10", "30.00"),
            ("2026-09-11", "125.50"),
            ("2026-09-12", "99.00"),
        ]


def test_acquisition_reads_its_approved_columns_and_nothing_else(tmp_path: Path) -> None:
    """The grant is the boundary, so a provider reaching past it is refused by PostgreSQL."""
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        acquisition = role_dsn(bootstrap_dsn, _ROLES.acquisition, passwords["acquisition"])

        with psycopg.connect(acquisition) as connection:
            approved = connection.execute(
                "SELECT order_id, customer_id, ordered_on, order_total, updated_at "
                "FROM source_data.customer_orders"
            ).fetchall()
        assert len(approved) == 5

        # The landed relation belongs to landing and materialization, never to acquisition.
        with psycopg.connect(acquisition) as connection, pytest.raises(psycopg.Error):
            connection.execute("SELECT * FROM raw.raw_customer_orders")

        # PUBLIC's default CREATE is revoked, so no role can place objects outside its grants.
        with psycopg.connect(acquisition) as connection, pytest.raises(psycopg.Error):
            connection.execute("CREATE TABLE public.escaped (value text)")


def test_landing_may_write_the_raw_relation_but_not_read_the_source(tmp_path: Path) -> None:
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        landing = role_dsn(bootstrap_dsn, _ROLES.landing, passwords["landing"])

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
            role_dsn(bootstrap_dsn, _ROLES.acquisition, passwords["acquisition"]),
            tenant_id=publication.contract.tenant_id,
        )
        assert len(acquired.rows) == 5

        ledger = GenerationLedger.in_memory()
        landed = asyncio.run(
            land_demo_rows(
                role_dsn(bootstrap_dsn, _ROLES.landing, passwords["landing"]),
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
        acquisition = role_dsn(bootstrap_dsn, _ROLES.acquisition, passwords["acquisition"])

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


def test_the_demonstration_materializes_one_generation_through_dbt(tmp_path: Path) -> None:
    """The whole chain: acquire, land, compile, materialize, and the product is queryable.

    This is the generation the governed answer is asked of. Nothing here writes the
    consumption view itself -- dbt runs the compiler's own statement, and the runner
    commits the generation only once quality passes.
    """
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")

    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        passwords = _provisioned(bootstrap_dsn)
        stores = DemoStores(tmp_path / "state")
        try:
            contract = build_demo_publication(
                stores, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
            ).contract
        finally:
            stores.close()

        acquired = acquire_demo_rows(
            role_dsn(bootstrap_dsn, _ROLES.acquisition, passwords["acquisition"]),
            tenant_id=contract.tenant_id,
        )
        ledger = GenerationLedger.in_memory()
        landed = asyncio.run(
            land_demo_rows(
                role_dsn(bootstrap_dsn, _ROLES.landing, passwords["landing"]),
                acquired,
                contract=contract,
                ledger=ledger,
            )
        )

        materialized = materialize_demo_generation(
            bootstrap_dsn=bootstrap_dsn,
            materialization_password=passwords["materialization"],
            dbt_executable=Path(dbt_executable),
            workspace=tmp_path / "materialization",
            contract=contract,
            landing_receipt_digest=digest(landed.receipt),
            generation_id=landed.receipt.generation_id,
            record_count=landed.receipt.record_count,
            ledger=ledger,
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

        assert [(str(day), str(total)) for day, total in rows] == [
            ("2026-09-10", "30.000000000"),
            ("2026-09-11", "125.500000000"),
            ("2026-09-12", "99.000000000"),
        ]

        # The receipt follows back to a refusal rather than an approval. `compile_product_iir`
        # ends every product compilation at its governed gates, so no admitted decision
        # exists, and the demonstration must not look as though one does.
        assert materialized.receipt.legality_decision_digest == unadmitted_decision_digest(
            materialized.physical_plan
        )
