from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest
from pillarmesh_contract_model import digest
from pillarmesh_dbt_adapter import CompiledDbtModel, DbtInvoker, SignedCompiledDbtModel
from pillarmesh_provider_postgresql import (
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLMaterializedColumn,
    PostgreSQLProductSemanticObserver,
    postgresql_materialized_schema_digest,
)
from pillarmesh_provider_sdk import ProviderError
from pillarmesh_runtime import (
    MaterializationObservation,
    MaterializationRequest,
    ProductMaterializationReceipt,
)
from psycopg import sql
from pydantic import SecretStr

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster


class _DbtMustNotRun:
    def invoke(self, **_kwargs: object) -> object:
        raise AssertionError("dbt must not overwrite a retained product generation")


def _cardinality_evidence_digest(
    *, plan_digest: str, input_generation_digests: tuple[str, ...]
) -> str:
    return digest(
        {
            "domain": "pillarmesh-observation-only-cardinality-fixture-v1",
            "plan_digest": plan_digest,
            "input_generation_digests": input_generation_digests,
        }
    )


def _materialization_receipt(commit_reference: str) -> ProductMaterializationReceipt:
    now = datetime.now(UTC)
    plan_digest = "b" * 64
    input_generation_digests = ("2" * 64,)
    return ProductMaterializationReceipt(
        run_id="run-live-observation",
        tenant_id="tenant-live-a",
        product_id="product-revenue",
        product_revision=2,
        product_generation=3,
        contract_digest="1" * 64,
        input_generation_digests=input_generation_digests,
        input_cardinality_evidence_digest=_cardinality_evidence_digest(
            plan_digest=plan_digest,
            input_generation_digests=input_generation_digests,
        ),
        execution_authorization_digest="7" * 64,
        legality_decision_digest="8" * 64,
        plan_digest=plan_digest,
        output_schema_digest="3" * 64,
        output_row_count=1,
        provider_commit_reference=commit_reference,
        dbt_manifest_digest="4" * 64,
        dbt_run_results_digest="5" * 64,
        lineage_digest="6" * 64,
        quality_assertion_count=0,
        quality_disposition="not_asserted",
        committed_at=now,
        retained_until=now + timedelta(hours=1),
    )


def _materialization_observation(
    *,
    request: MaterializationRequest,
    model_digest: str,
    relation_identity: tuple[object, ...],
) -> MaterializationObservation:
    return MaterializationObservation(
        provider_commit_reference=digest(
            {
                "domain": "pillarmesh-postgresql-product-generation-v1",
                "tenant_id": request.tenant_id,
                "product_id": request.product_id,
                "product_revision": request.product_revision,
                "product_generation": request.product_generation,
                "model_digest": model_digest,
                "relation_identity": relation_identity,
            }
        ),
        output_schema_digest=postgresql_materialized_schema_digest(
            (
                PostgreSQLMaterializedColumn(
                    ordinal=1, name="region", data_type="text", nullable=False
                ),
                PostgreSQLMaterializedColumn(
                    ordinal=2, name="total_revenue", data_type="numeric", nullable=False
                ),
            )
        ),
        output_row_count=1,
        dbt_manifest_digest="4" * 64,
        dbt_run_results_digest="5" * 64,
        lineage_digest="6" * 64,
        quality_assertion_count=0,
        quality_disposition="not_asserted",
    )


def _role_dsn(bootstrap_dsn: str, role: str, password: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=parsed["host"],
        port=parsed["port"],
        dbname=parsed["dbname"],
        user=role,
        password=password,
    )


def _provision_observed_generation(bootstrap_dsn: str, password: str) -> tuple[str, int, str]:
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE product_observer LOGIN PASSWORD {}").format(sql.Literal(password))
        )
        connection.execute("ALTER ROLE product_observer SET timezone TO 'UTC'")
        connection.execute("CREATE SCHEMA product_control")
        connection.execute("CREATE SCHEMA product_generation_3")
        connection.execute("CREATE SCHEMA consumption")
        connection.execute(
            "CREATE TABLE product_generation_3.product_revenue_g3 "
            '(region text COLLATE "C" PRIMARY KEY, total_revenue numeric(18, 2) NOT NULL, '
            "CONSTRAINT product_revenue_total_key UNIQUE (total_revenue) DEFERRABLE)"
        )
        connection.execute(
            "INSERT INTO product_generation_3.product_revenue_g3 VALUES ('west', 30.00)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX product_revenue_total_index "
            "ON product_generation_3.product_revenue_g3 (total_revenue)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX product_revenue_partial_index "
            "ON product_generation_3.product_revenue_g3 (total_revenue) "
            "WHERE total_revenue > 0"
        )
        connection.execute(
            "CREATE UNIQUE INDEX product_revenue_expression_index "
            "ON product_generation_3.product_revenue_g3 (lower(region))"
        )
        connection.execute(
            "CREATE VIEW consumption.product_revenue AS "
            "SELECT * FROM product_generation_3.product_revenue_g3"
        )
        relation_oid = connection.execute(
            "SELECT 'product_generation_3.product_revenue_g3'::regclass::oid"
        ).fetchone()
        assert relation_oid is not None and type(relation_oid[0]) is int
        relation_identity = connection.execute(
            "SELECT relation.oid::text, relation.relfilenode::text "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'product_generation_3' "
            "AND relation.relname = 'product_revenue_g3'"
        ).fetchone()
        assert relation_identity is not None
        commit_reference = digest(
            {
                "domain": "pillarmesh-postgresql-product-generation-v1",
                "tenant_id": "tenant-live-a",
                "product_id": "product-revenue",
                "product_revision": 2,
                "product_generation": 3,
                "model_digest": "b" * 64,
                "relation_identity": relation_identity,
            }
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
        values = (
            "tenant-live-a",
            "product-revenue",
            2,
            3,
            "product_generation_3",
            "product_revenue_g3",
            commit_reference,
        )
        connection.execute(
            "INSERT INTO product_control.product_generations VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + interval '1 hour')",
            values,
        )
        connection.execute(
            "INSERT INTO product_control.product_generation_pointers VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP + interval '1 hour', "
            "CURRENT_TIMESTAMP)",
            values,
        )
        connection.execute(
            "GRANT USAGE ON SCHEMA product_control, product_generation_3 TO product_observer"
        )
        connection.execute(
            "GRANT SELECT ON product_control.product_generations, "
            "product_control.product_generation_pointers, "
            "product_generation_3.product_revenue_g3 TO product_observer"
        )
    return (
        _role_dsn(bootstrap_dsn, "product_observer", password),
        relation_oid[0],
        commit_reference,
    )


@pytest.mark.live
def test_fresh_postgresql_observation_reads_actual_context_and_owned_generation(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    password = secrets.token_urlsafe(32)
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        observer_dsn, relation_oid, commit_reference = _provision_observed_generation(
            bootstrap_dsn, password
        )
        settings = PostgreSQLMaterializationSettings(
            tenant_id="tenant-live-a",
            dsn=SecretStr(observer_dsn),
            consumption_schema_name="consumption",
            consumption_view_name="product_revenue",
            control_schema_name="product_control",
            generation_table_name="product_generations",
            generation_pointer_table_name="product_generation_pointers",
        )
        observer = PostgreSQLProductSemanticObserver(settings)
        request = _materialization_receipt(commit_reference)

        observation = observer.observe(request)

        assert observation.current_database == "postgres"
        assert observation.session_timezone == "UTC"
        assert observation.relation_oid == relation_oid
        assert observation.relation_schema == "product_generation_3"
        assert observation.relation_name == "product_revenue_g3"
        assert tuple(
            (column.name, column.formatted_type, column.nullable) for column in observation.columns
        ) == (
            ("region", "text", False),
            ("total_revenue", "numeric(18,2)", False),
        )
        assert observation.columns[0].collation_name == "C"
        assert observation.server_version_num.isdecimal()
        assert observation.engine_build_digest == digest(
            {
                "domain": "pillarmesh-postgresql-engine-build-v1",
                "version": {
                    "server_version_num": observation.server_version_num,
                    "server_version": observation.engine_version,
                },
            }
        )
        assert tuple(
            constraint.constraint_name for constraint in observation.eligible_unique_constraints
        ) == ("product_revenue_g3_pkey",)
        assert tuple(
            (
                column.key_position,
                column.physical_ordinal,
                column.name,
            )
            for column in observation.eligible_unique_constraints[0].key_columns
        ) == ((1, 1, "region"),)
        assert observation.provider_commit_reference == commit_reference
        assert password not in observation.model_dump_json()

        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                "UPDATE product_control.product_generation_pointers "
                "SET provider_commit_reference = %s WHERE tenant_id = 'tenant-live-a' "
                "AND product_id = 'product-revenue'",
                ("b" * 64,),
            )
        with pytest.raises(ProviderError) as conflicting_pointer:
            observer.observe(request)
        assert conflicting_pointer.value.classification == "integrity_failure"

        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                "UPDATE product_control.product_generation_pointers "
                "SET provider_commit_reference = %s, product_generation = 4 "
                "WHERE tenant_id = 'tenant-live-a' "
                "AND product_id = 'product-revenue'",
                (commit_reference,),
            )
        with pytest.raises(ProviderError) as stale:
            observer.observe(request)
        assert stale.value.classification == "integrity_failure"

        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                "UPDATE product_control.product_generation_pointers "
                "SET product_generation = 3 WHERE tenant_id = 'tenant-live-a' "
                "AND product_id = 'product-revenue'"
            )
            connection.execute(
                "REVOKE SELECT ON product_generation_3.product_revenue_g3 FROM product_observer"
            )
        with pytest.raises(ProviderError) as denied:
            observer.observe(request)
        assert denied.value.classification == "authorization_denied"

        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute("DROP TABLE product_generation_3.product_revenue_g3 CASCADE")
            connection.execute(
                "CREATE TABLE product_generation_3.product_revenue_g3 "
                '(region text COLLATE "C" NOT NULL, total_revenue numeric(18, 2) NOT NULL)'
            )
            connection.execute(
                "INSERT INTO product_generation_3.product_revenue_g3 VALUES ('west', 30.00)"
            )
            connection.execute(
                "CREATE VIEW consumption.product_revenue AS "
                "SELECT * FROM product_generation_3.product_revenue_g3"
            )
            connection.execute(
                "GRANT SELECT ON product_generation_3.product_revenue_g3 TO product_observer"
            )
        with pytest.raises(ProviderError) as replaced:
            observer.observe(request)
        assert replaced.value.classification == "integrity_failure"

        model = CompiledDbtModel(
            model_name="product_revenue_g3",
            contract_digest=request.contract_digest,
            provider="postgresql",
            input_generation_digests=request.input_generation_digests,
            target_schema="product_generation_3",
            compiled_sql="SELECT 1",
        )
        signed_model = SignedCompiledDbtModel(
            model=model,
            model_digest=request.plan_digest,
            key_id="compiler",
            signature="unused-during-publication",
        )
        publication_request = MaterializationRequest(
            run_id="run-live-replaced-publication",
            tenant_id=request.tenant_id,
            product_id=request.product_id,
            product_revision=request.product_revision,
            product_generation=request.product_generation,
            retention_seconds=3600,
            contract_digest=request.contract_digest,
            plan_digest=request.plan_digest,
            input_generation_digests=request.input_generation_digests,
            input_cardinality_evidence_digest=request.input_cardinality_evidence_digest,
            expected_output_schema_digest=request.output_schema_digest,
        )
        warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings.model_copy(update={"dsn": SecretStr(bootstrap_dsn)}),
            signed_model=signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )
        committed_output = MaterializationObservation(
            provider_commit_reference=commit_reference,
            output_schema_digest=postgresql_materialized_schema_digest(
                (
                    PostgreSQLMaterializedColumn(
                        ordinal=1, name="region", data_type="text", nullable=False
                    ),
                    PostgreSQLMaterializedColumn(
                        ordinal=2, name="total_revenue", data_type="numeric", nullable=False
                    ),
                )
            ),
            output_row_count=1,
            dbt_manifest_digest=request.dbt_manifest_digest,
            dbt_run_results_digest=request.dbt_run_results_digest,
            lineage_digest=request.lineage_digest,
            quality_assertion_count=request.quality_assertion_count,
            quality_disposition=request.quality_disposition,
        )

        with pytest.raises(ProviderError) as publication_replaced:
            warehouse.switch_consumption_view(publication_request, committed_output)

        assert publication_replaced.value.classification == "integrity_failure"


@pytest.mark.live
def test_fresh_postgresql_materialization_refuses_a_retained_output_relation(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    password = secrets.token_urlsafe(32)
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        observer_dsn, _relation_oid, _commit_reference = _provision_observed_generation(
            bootstrap_dsn, password
        )
        model = CompiledDbtModel(
            model_name="product_revenue_g3",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="product_generation_3",
            compiled_sql="SELECT 1",
        )
        signed_model = SignedCompiledDbtModel(
            model=model,
            model_digest=digest(model),
            key_id="compiler",
            signature="unused-before-dbt",
        )
        warehouse = PostgreSQLMaterializationWarehouse(
            settings=PostgreSQLMaterializationSettings(
                tenant_id="tenant-live-a",
                dsn=SecretStr(observer_dsn),
                consumption_schema_name="consumption",
                consumption_view_name="product_revenue",
                control_schema_name="product_control",
                generation_table_name="product_generations",
                generation_pointer_table_name="product_generation_pointers",
            ),
            signed_model=signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )
        request = MaterializationRequest(
            run_id="run-live-reused-target",
            tenant_id="tenant-live-a",
            product_id="product-revenue",
            product_revision=2,
            product_generation=3,
            retention_seconds=3600,
            contract_digest=model.contract_digest,
            plan_digest=digest(model),
            input_generation_digests=model.input_generation_digests,
            input_cardinality_evidence_digest=_cardinality_evidence_digest(
                plan_digest=digest(model),
                input_generation_digests=model.input_generation_digests,
            ),
            expected_output_schema_digest="3" * 64,
        )

        with pytest.raises(ProviderError) as caught:
            warehouse.execute(request)

        assert caught.value.classification == "integrity_failure"


@pytest.mark.live
def test_fresh_postgresql_materialization_preserves_exact_and_refuses_stale_replay(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to initialize a cluster as root")
    password = secrets.token_urlsafe(32)
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        _observer_dsn, _relation_oid, _commit_reference = _provision_observed_generation(
            bootstrap_dsn, password
        )
        with psycopg.connect(bootstrap_dsn) as connection:
            current_relation_identity = connection.execute(
                "SELECT relation.oid::text, relation.relfilenode::text "
                "FROM pg_catalog.pg_class AS relation "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'product_generation_3' "
                "AND relation.relname = 'product_revenue_g3'"
            ).fetchone()
            retained_before = connection.execute(
                "SELECT retained_until FROM product_control.product_generation_pointers "
                "WHERE tenant_id = 'tenant-live-a' AND product_id = 'product-revenue'"
            ).fetchone()
            connection.execute("CREATE SCHEMA product_generation_2")
            connection.execute(
                "CREATE TABLE product_generation_2.product_revenue_g2 "
                '(region text COLLATE "C" NOT NULL, total_revenue numeric(18, 2) NOT NULL)'
            )
            connection.execute(
                "INSERT INTO product_generation_2.product_revenue_g2 VALUES ('old', 20.00)"
            )
            stale_relation_identity = connection.execute(
                "SELECT relation.oid::text, relation.relfilenode::text "
                "FROM pg_catalog.pg_class AS relation "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'product_generation_2' "
                "AND relation.relname = 'product_revenue_g2'"
            ).fetchone()
        assert current_relation_identity is not None
        assert stale_relation_identity is not None
        assert retained_before is not None
        settings = PostgreSQLMaterializationSettings(
            tenant_id="tenant-live-a",
            dsn=SecretStr(bootstrap_dsn),
            consumption_schema_name="consumption",
            consumption_view_name="product_revenue",
            control_schema_name="product_control",
            generation_table_name="product_generations",
            generation_pointer_table_name="product_generation_pointers",
        )
        current_model = CompiledDbtModel(
            model_name="product_revenue_g3",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="product_generation_3",
            compiled_sql="SELECT 1",
        )
        current_signed_model = SignedCompiledDbtModel(
            model=current_model,
            model_digest="b" * 64,
            key_id="compiler",
            signature="unused-during-publication",
        )
        current_request = MaterializationRequest(
            run_id="run-live-exact-pointer-replay",
            tenant_id="tenant-live-a",
            product_id="product-revenue",
            product_revision=2,
            product_generation=3,
            retention_seconds=7200,
            contract_digest=current_model.contract_digest,
            plan_digest=current_signed_model.model_digest,
            input_generation_digests=current_model.input_generation_digests,
            input_cardinality_evidence_digest=_cardinality_evidence_digest(
                plan_digest=current_signed_model.model_digest,
                input_generation_digests=current_model.input_generation_digests,
            ),
            expected_output_schema_digest="3" * 64,
        )
        current_warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings,
            signed_model=current_signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )

        current_warehouse.switch_consumption_view(
            current_request,
            _materialization_observation(
                request=current_request,
                model_digest=current_signed_model.model_digest,
                relation_identity=current_relation_identity,
            ),
        )

        with psycopg.connect(bootstrap_dsn) as connection:
            retained_after = connection.execute(
                "SELECT retained_until FROM product_control.product_generation_pointers "
                "WHERE tenant_id = 'tenant-live-a' AND product_id = 'product-revenue'"
            ).fetchone()
        assert retained_after == retained_before

        stale_model = current_model.model_copy(
            update={"model_name": "product_revenue_g2", "target_schema": "product_generation_2"}
        )
        stale_signed_model = SignedCompiledDbtModel(
            model=stale_model,
            model_digest="c" * 64,
            key_id="compiler",
            signature="unused-during-publication",
        )
        stale_request = current_request.model_copy(
            update={
                "run_id": "run-live-stale-pointer",
                "product_generation": 2,
                "plan_digest": stale_signed_model.model_digest,
                "input_cardinality_evidence_digest": _cardinality_evidence_digest(
                    plan_digest=stale_signed_model.model_digest,
                    input_generation_digests=stale_model.input_generation_digests,
                ),
            }
        )
        stale_warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings,
            signed_model=stale_signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )

        with pytest.raises(ProviderError) as caught:
            stale_warehouse.switch_consumption_view(
                stale_request,
                _materialization_observation(
                    request=stale_request,
                    model_digest=stale_signed_model.model_digest,
                    relation_identity=stale_relation_identity,
                ),
            )

        assert caught.value.classification == "integrity_failure"
        with psycopg.connect(bootstrap_dsn) as connection:
            rows = connection.execute("SELECT region FROM consumption.product_revenue").fetchall()
        assert rows == [("west",)]
