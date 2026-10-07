"""Bring the demonstration's warehouse to one published product generation.

This is the chain the console runs before a browser connects, when it is given a warehouse:
provision the roles and the seeded source, acquire the approved columns, land them under a
receipt, compile and materialize the product, and publish it through the product authorities.
What comes out is a generation the governed answer can be asked of.

It is written to be safe to run again. A second start over the same warehouse and the same
state directory finds its own earlier generation and returns it rather than committing a
second one, and a start that finds the two disagreeing refuses with what to do about it
instead of materializing into a warehouse whose history it cannot see. Acquisition is the one
step that cannot be repeated at all: landing acknowledges the batch and so advances the source
checkpoint, after which the provider admits no second snapshot -- so a start that already
landed reads its generation back out of the ledger rather than acquiring again.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk import LandReceipt

from .bi_provider import DemoWarehouseRoute, demo_superset_database_uri
from .catalog import compose_demo_product_catalog, demo_source_freshness_observation
from .generation import DemoAcquisition, LandedDemoGeneration
from .materialization import (
    DEMO_GROUP_COLUMN,
    DEMO_MEASURE_COLUMN,
    materialize_demo_generation,
)
from .model_authority import SignedModelAuthority
from .publication import DemoPublication
from .stores import DemoLandedGeneration, DemoStores
from .warehouse import (
    DEMO_MAX_WRITE_TRANSACTION_DURATION,
    DEMO_WAREHOUSE_ROLES,
    ProvisioningRefused,
    demo_warehouse_is_provisioned,
    grant_demo_product_read,
    provision_demo_warehouse,
    role_dsn,
    seal_demo_product_generation,
    set_demo_role_passwords,
    wait_for_demo_warehouse,
)

__all__ = ["DemoWarehouseGeneration", "ensure_demo_generation"]

# The one generation the demonstration commits.
_GENERATION = 1

# How far before the start the seeded source rows are stamped. Comfortably outside the
# acquisition's write-transaction lag bound, which excludes anything more recent, and recent
# enough that the product's staleness stays well inside any scope policy a reader would write.
_SEED_AGE = max(timedelta(hours=1), DEMO_MAX_WRITE_TRANSACTION_DURATION * 2)


@dataclass(frozen=True, slots=True)
class DemoWarehouseGeneration:
    """The published product generation the demonstration's governed answer reads."""

    product_ref: ArtifactReference
    generation: int
    namespace: str
    relation_name: str
    # Kept out of the representation: these carry role passwords, and a repr of this object
    # reaches a log or a traceback. Three rather than one because reading the product to answer a
    # question, explaining a statement to estimate its scan, and querying it for a published
    # dashboard are different privileges, held by different roles -- none of which may write
    # anything. ADR-0007 is why the third is not the first: a BI provider connecting as
    # `answer_runtime` would make a database login stand in for an access decision.
    answer_dsn: str = field(repr=False)
    estimator_dsn: str = field(repr=False)
    # The one that is not a libpq DSN: Superset takes a SQLAlchemy URI, and `demo/bi_provider.py`
    # owns that conversion.
    dashboard_database_uri: str = field(repr=False)
    # The compiled model the answer path re-verifies before it answers, and the key it verifies
    # with. Read back from the store rather than carried from the materialization, so a restart
    # that found an earlier generation hands back the same thing a fresh one does.
    signed_model: SignedModelAuthority


def _fresh_passwords() -> dict[str, str]:
    """One new password per role, held in this process and written nowhere.

    Generated on every start rather than stored, so the demonstration keeps no credential on
    its state volume. `set_demo_role_passwords` makes PostgreSQL agree with what was generated
    here, which is why a restart does not need to remember the last ones.

    Keyed by role name and derived from the role set, so a role added to `DemoWarehouseRoles`
    cannot be left without one.
    """
    return {role: secrets.token_urlsafe(24) for role in DEMO_WAREHOUSE_ROLES}


def _database_name(bootstrap_dsn: str) -> str:
    name = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)["dbname"]
    if not isinstance(name, str) or not name:
        raise ProvisioningRefused("the warehouse connection names no database")
    return name


def _warehouse_holds_a_generation(bootstrap_dsn: str, *, tenant_id: str, product_id: str) -> bool:
    """Whether the warehouse's own control table records any generation of this product.

    This is the demonstration asking whether its two volumes still agree, not a governed read of
    product state: `PostgreSQLProductGenerationAuthority.observe` answers for one named
    generation and reports `integrity_failure` when there is none, which is the wrong answer to
    a question about whether provisioning and the state directory were discarded together.
    """
    with psycopg.connect(bootstrap_dsn) as connection:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM product_control.product_generations "
            "WHERE tenant_id = %s AND product_id = %s)",
            (tenant_id, product_id),
        ).fetchone()
    return bool(row is not None and row[0])


def _acquire_and_land(
    bootstrap_dsn: str,
    *,
    publication: DemoPublication,
    stores: DemoStores,
    passwords: Mapping[str, str],
    clock: Callable[[], datetime],
) -> LandedDemoGeneration:
    """Acquire the approved source columns and land them, under the runtime's governance.

    Observing the source, activating the contract and preparing the batch happen in one object
    because they must happen in one process; see `DemoAcquisition`.

    `asyncio.run` rather than an awaited call, because this whole chain runs once before the
    server starts and has no loop of its own. Calling it from inside a running loop would raise,
    which is the right failure: landing is startup work, not something a request handler does.
    """
    acquisition = DemoAcquisition(
        role_dsn(
            bootstrap_dsn,
            DEMO_WAREHOUSE_ROLES.acquisition,
            passwords[DEMO_WAREHOUSE_ROLES.acquisition],
        ),
        publication=publication,
        stores=stores,
        clock=clock,
    )
    prepared = acquisition.prepare()
    return asyncio.run(
        acquisition.land(
            prepared,
            landing_dsn=role_dsn(
                bootstrap_dsn,
                DEMO_WAREHOUSE_ROLES.landing,
                passwords[DEMO_WAREHOUSE_ROLES.landing],
            ),
        )
    )


def _landed_generation(
    bootstrap_dsn: str,
    *,
    publication: DemoPublication,
    stores: DemoStores,
    passwords: Mapping[str, str],
    clock: Callable[[], datetime],
) -> tuple[LandReceipt, datetime]:
    """The landed generation this demonstration stands on, acquired now or found from before.

    A start that already landed must not reach acquisition again. The acknowledgement that
    closes a landing advances the source checkpoint, and from there the provider refuses a
    snapshot (`permanent_configuration`: a snapshot is admitted only at revision 0) and the
    state store refuses to replay an acknowledged preparation as pending. So what was landed
    is recorded the moment it was landed, and a later start reads the receipt back out of the
    generation ledger instead.

    The watermark comes back from the same record rather than being measured again. The
    freshness observation composed from it is stored under an identity derived from the
    landing receipt, in a store that is immutable, so a second start that measured a fresh
    watermark would be refused for an identity collision it caused itself.
    """
    contract = publication.contract
    recorded = stores.landed_generations.read(
        tenant_id=contract.tenant_id, contract_ref=contract.contract_id
    )
    if recorded is not None:
        record = stores.generations.load_record(recorded.generation_key)
        if record is None:
            raise ProvisioningRefused(
                "the demonstration recorded a landed generation its generation ledger does not "
                "hold, so what the product would be built from cannot be read back. Discard "
                "the state directory and the warehouse together with `docker compose down -v`."
            )
        return record.receipt, recorded.watermark_at
    landed = _acquire_and_land(
        bootstrap_dsn,
        publication=publication,
        stores=stores,
        passwords=passwords,
        clock=clock,
    )
    # Recorded immediately, because the acknowledgement above has already moved the source
    # checkpoint: from here on this is the only route back to the generation, and a start that
    # failed before writing it is refused rather than left to acquire a second time.
    stores.landed_generations.record(
        tenant_id=contract.tenant_id,
        contract_ref=contract.contract_id,
        landed=DemoLandedGeneration(
            generation_key=landed.generation_key, watermark_at=landed.watermark_at
        ),
    )
    return landed.receipt, landed.watermark_at


def _readable(
    bootstrap_dsn: str,
    *,
    stores: DemoStores,
    passwords: Mapping[str, str],
    tenant_id: str,
    product_ref: ArtifactReference,
    generation: int,
    namespace: str,
    relation_name: str,
    dashboard_route: DemoWarehouseRoute | None,
) -> DemoWarehouseGeneration:
    """Grant the read-only roles `SELECT` on this product, and describe how to reach it.

    Granted on the way out rather than at provisioning time, because the relation is named by
    the materialization and does not exist before it. Running on both paths -- a generation just
    committed and one found from an earlier start -- means a restart repairs grants that were
    lost without the data behind them.
    """
    seal_demo_product_generation(
        bootstrap_dsn,
        roles=DEMO_WAREHOUSE_ROLES,
        namespace=namespace,
        relation_name=relation_name,
    )
    grant_demo_product_read(
        bootstrap_dsn,
        roles=DEMO_WAREHOUSE_ROLES,
        namespace=namespace,
        relation_name=relation_name,
    )
    signed_model = stores.signed_models.read(
        tenant_id=tenant_id,
        product_id=product_ref.artifact_id,
        product_revision=product_ref.version,
        generation=generation,
    )
    if signed_model is None:
        raise ProvisioningRefused(
            "the demonstration's product generation has no signed compiled model, so the answer "
            "path could not verify what materialized it. Discard the state directory and the "
            "warehouse together with `docker compose down -v`."
        )
    return DemoWarehouseGeneration(
        product_ref=product_ref,
        generation=generation,
        namespace=namespace,
        relation_name=relation_name,
        signed_model=signed_model,
        answer_dsn=role_dsn(
            bootstrap_dsn, DEMO_WAREHOUSE_ROLES.answer, passwords[DEMO_WAREHOUSE_ROLES.answer]
        ),
        estimator_dsn=role_dsn(
            bootstrap_dsn,
            DEMO_WAREHOUSE_ROLES.estimator,
            passwords[DEMO_WAREHOUSE_ROLES.estimator],
        ),
        # Not `role_dsn`: Superset takes a SQLAlchemy URI, and on the warehouse-control path it
        # reaches the warehouse by a route of its own rather than by anything this DSN names.
        dashboard_database_uri=demo_superset_database_uri(
            bootstrap_dsn,
            role=DEMO_WAREHOUSE_ROLES.dashboard,
            password=passwords[DEMO_WAREHOUSE_ROLES.dashboard],
            route=dashboard_route,
        ),
    )


def ensure_demo_generation(
    *,
    bootstrap_dsn: str,
    stores: DemoStores,
    publication: DemoPublication,
    dbt_executable: Path,
    workspace: Path,
    clock: Callable[[], datetime],
    dashboard_route: DemoWarehouseRoute | None = None,
) -> DemoWarehouseGeneration:
    """Provision, acquire, land, materialize and publish, or return what an earlier start did.

    `bootstrap_dsn` is a superuser connection to an otherwise empty database: provisioning
    creates the three least-privilege logins the providers connect as, and nothing after
    provisioning uses this connection except to create the product's target schema.
    """
    # Before anything reads or writes it. Compose holds the first start back until the warehouse
    # reports healthy, but `docker compose restart` restarts both containers without re-reading
    # `depends_on`, so the console can come back while PostgreSQL is still starting -- and every
    # step below connects without retrying.
    wait_for_demo_warehouse(bootstrap_dsn)
    contract = publication.contract
    product_ref = ArtifactReference(
        artifact_id=contract.destination_product.product_name,
        version=contract.version,
        digest=digest(contract.destination_product),
    )
    passwords = _fresh_passwords()
    if demo_warehouse_is_provisioned(bootstrap_dsn):
        set_demo_role_passwords(bootstrap_dsn, roles=DEMO_WAREHOUSE_ROLES, passwords=passwords)
    else:
        provision_demo_warehouse(
            bootstrap_dsn,
            roles=DEMO_WAREHOUSE_ROLES,
            passwords=passwords,
            seeded_at=clock() - _SEED_AGE,
        )

    published = stores.query_bindings.read_current(
        tenant_id=contract.tenant_id, product_ref=product_ref, generation=_GENERATION
    )
    committed = _warehouse_holds_a_generation(
        bootstrap_dsn, tenant_id=contract.tenant_id, product_id=product_ref.artifact_id
    )
    if (published is not None) != committed:
        # One volume was discarded and the other kept. Materializing now would either commit a
        # generation the warehouse already holds, or publish authority for a relation that is
        # not there; either leaves a console that loads and then refuses to answer.
        raise ProvisioningRefused(
            "the demonstration's state directory and its warehouse disagree about whether the "
            "product has been materialized. Discard both with `docker compose down -v`."
        )
    if published is not None:
        return _readable(
            bootstrap_dsn,
            stores=stores,
            passwords=passwords,
            tenant_id=contract.tenant_id,
            product_ref=product_ref,
            generation=_GENERATION,
            namespace=published.namespace,
            relation_name=published.relation_name,
            dashboard_route=dashboard_route,
        )

    receipt, watermark_at = _landed_generation(
        bootstrap_dsn,
        publication=publication,
        stores=stores,
        passwords=passwords,
        clock=clock,
    )
    landing_receipt_digest = digest(receipt)
    # The watermark the acquisition measured, not the seed's own constant: the observation
    # describes what was read, and measuring it from the acquired records is what keeps it true
    # of a source whose rows someone changes.
    #
    # The reading time comes from the landed receipt rather than from the acquisition's own
    # boundary clock, because this observation is stored under an identity derived from the
    # receipt digest and that store is immutable. `_landed_generation` hands back the receipt it
    # already holds, so the receipt's `committed_at` is the same value on every later start
    # while a fresh boundary reading is not. A start that fails between here and publishing
    # reaches this line again; with a moving reading it is refused for an identity collision it
    # caused itself, which no operator can act on. The rows were read no later than the
    # transaction that committed them, so the receipt's time is also the honest upper bound.
    freshness = demo_source_freshness_observation(
        landing_receipt_digest=landing_receipt_digest,
        generation_id=receipt.generation_id,
        watermark_at=watermark_at,
        observed_at=receipt.committed_at,
    )
    materialized = materialize_demo_generation(
        bootstrap_dsn=bootstrap_dsn,
        materialization_password=passwords[DEMO_WAREHOUSE_ROLES.materialization],
        dbt_executable=dbt_executable,
        workspace=workspace,
        contract=contract,
        landing_receipt_digest=landing_receipt_digest,
        generation_id=receipt.generation_id,
        record_count=receipt.record_count,
        ledger=stores.generations,
        materialization_ledger=stores.materialization_ledger,
        catalog=lambda namespace, relation_name: compose_demo_product_catalog(
            stores=stores,
            contract=contract,
            semantic_version=publication.semantic_version,
            database_name=_database_name(bootstrap_dsn),
            namespace=namespace,
            relation_name=relation_name,
            group_column=DEMO_GROUP_COLUMN,
            measure_column=DEMO_MEASURE_COLUMN,
            generation=_GENERATION,
            freshness_observation=freshness,
            clock=clock,
        ),
    )
    # Recorded before the generation is described as readable, so a start that fails between
    # materializing and storing this refuses on its next attempt rather than answering without it.
    stores.signed_models.store(
        tenant_id=contract.tenant_id,
        product_id=product_ref.artifact_id,
        product_revision=product_ref.version,
        generation=materialized.receipt.product_generation,
        signed_model=materialized.signed_model,
        public_key=materialized.compiler_public_key,
    )
    return _readable(
        bootstrap_dsn,
        stores=stores,
        passwords=passwords,
        tenant_id=contract.tenant_id,
        product_ref=product_ref,
        generation=materialized.receipt.product_generation,
        namespace=materialized.target_schema,
        relation_name=materialized.model_name,
        dashboard_route=dashboard_route,
    )
