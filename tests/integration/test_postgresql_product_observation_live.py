from __future__ import annotations

import base64
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pillarmesh_contract_model import digest
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtDecimalMagnitudeCheck,
    DbtInvoker,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
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

_TENANT_ID = "tenant-live-a"
_PRODUCT_ID = "product-revenue"
_PRODUCT_REVISION = 2
_CONTRACT_DIGEST = "1" * 64
_INPUT_GENERATION_DIGESTS = ("2" * 64,)
_COMPILER_KEY_ID = "compiler-key-live"
_COMPILER_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
_OUTPUT_SCHEMA_DIGEST = postgresql_materialized_schema_digest(
    (
        PostgreSQLMaterializedColumn(ordinal=1, name="region", data_type="text", nullable=False),
        PostgreSQLMaterializedColumn(
            ordinal=2, name="total_revenue", data_type="numeric", nullable=False
        ),
    )
)


class _DbtMustNotRun:
    def invoke(self, **_kwargs: object) -> object:
        raise AssertionError("dbt must not overwrite a retained product generation")


def _signed_model(*, product_generation: int) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name=f"product_revenue_g{product_generation}",
        contract_digest=_CONTRACT_DIGEST,
        provider="postgresql",
        input_generation_digests=_INPUT_GENERATION_DIGESTS,
        target_schema=f"product_generation_{product_generation}",
        output_columns=("region", "total_revenue"),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        compiled_sql="SELECT region, total_revenue FROM raw.revenue",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id=_COMPILER_KEY_ID,
        signature=base64.b64encode(
            _COMPILER_PRIVATE_KEY.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


def _trusted_compiler_keys() -> dict[str, Ed25519PublicKey]:
    return {_COMPILER_KEY_ID: _COMPILER_PRIVATE_KEY.public_key()}


def _physical_plan(signed_model: SignedCompiledDbtModel) -> ProductPhysicalPlan:
    model = signed_model.model
    return ProductPhysicalPlan(
        compiler_version="compiler-live-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id=_TENANT_ID,
        product_id=_PRODUCT_ID,
        product_revision=_PRODUCT_REVISION,
        contract_ref="contract-revenue",
        contract_revision=1,
        contract_digest=model.contract_digest,
        iir_digest="8" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-live",
        warehouse_binding_revision=1,
        provider_observation_digest="9" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="revenue",
            generation_column="generation_id",
            payload_column="payload",
            generation_id=digest("postgresql-live-observation-source-generation"),
            landing_receipt_digest=model.input_generation_digests[0],
            observed_source_schema_digest="3" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="total_revenue", json_field="revenue", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(namespace=model.target_schema, relation_name=model.model_name),
        emitted_statement=model.compiled_sql,
        statement_digest=digest(model.compiled_sql),
        output_columns=model.output_columns,
        expected_output_schema_digest=_OUTPUT_SCHEMA_DIGEST,
        decimal_output_checks=tuple(
            Decimal57OutputCheck(column_name=check.column_name)
            for check in model.output_magnitude_checks
        ),
    )


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


def _materialization_request(
    signed_model: SignedCompiledDbtModel,
    *,
    run_id: str,
    product_generation: int,
    retention_seconds: int,
) -> MaterializationRequest:
    model = signed_model.model
    physical_plan = _physical_plan(signed_model)
    return MaterializationRequest(
        run_id=run_id,
        tenant_id=_TENANT_ID,
        product_id=_PRODUCT_ID,
        product_revision=_PRODUCT_REVISION,
        product_generation=product_generation,
        retention_seconds=retention_seconds,
        contract_digest=model.contract_digest,
        physical_plan=physical_plan,
        physical_plan_digest=digest(physical_plan),
        compiled_model_digest=signed_model.model_digest,
        input_generation_digests=model.input_generation_digests,
        input_cardinality_evidence_digest=_cardinality_evidence_digest(
            plan_digest=digest(physical_plan),
            input_generation_digests=model.input_generation_digests,
        ),
        expected_output_schema_digest=physical_plan.expected_output_schema_digest,
    )


def _commit_reference(
    signed_model: SignedCompiledDbtModel,
    *,
    product_generation: int,
    relation_identity: tuple[object, ...],
) -> str:
    return digest(
        {
            "domain": "pillarmesh-postgresql-product-generation-v1",
            "tenant_id": _TENANT_ID,
            "product_id": _PRODUCT_ID,
            "product_revision": _PRODUCT_REVISION,
            "product_generation": product_generation,
            "model_digest": signed_model.model_digest,
            "relation_identity": relation_identity,
            "output_magnitude_checks": tuple(
                {"declaration": check.model_dump(mode="python"), "violation_count": 0}
                for check in signed_model.model.output_magnitude_checks
            ),
        }
    )


def _materialization_receipt(
    signed_model: SignedCompiledDbtModel, commit_reference: str
) -> ProductMaterializationReceipt:
    now = datetime.now(UTC)
    request = _materialization_request(
        signed_model,
        run_id="run-live-observation",
        product_generation=3,
        retention_seconds=3600,
    )
    return ProductMaterializationReceipt(
        run_id=request.run_id,
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        product_revision=request.product_revision,
        product_generation=request.product_generation,
        contract_digest=request.contract_digest,
        input_generation_digests=request.input_generation_digests,
        input_cardinality_evidence_digest=request.input_cardinality_evidence_digest,
        execution_authorization_digest="7" * 64,
        legality_decision_digest="8" * 64,
        physical_plan_digest=request.physical_plan_digest,
        compiled_model_digest=request.compiled_model_digest,
        output_schema_digest=request.expected_output_schema_digest,
        output_row_count=1,
        provider_commit_reference=commit_reference,
        dbt_manifest_digest="4" * 64,
        dbt_run_results_digest="5" * 64,
        lineage_digest="6" * 64,
        quality_assertion_count=0,
        quality_disposition="not_asserted",
        committed_at=now,
        retained_until=now + timedelta(seconds=request.retention_seconds),
    )


def _materialization_observation(
    signed_model: SignedCompiledDbtModel,
    *,
    product_generation: int,
    relation_identity: tuple[object, ...],
) -> MaterializationObservation:
    return MaterializationObservation(
        provider_commit_reference=_commit_reference(
            signed_model,
            product_generation=product_generation,
            relation_identity=relation_identity,
        ),
        output_schema_digest=_OUTPUT_SCHEMA_DIGEST,
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
        commit_reference = _commit_reference(
            _signed_model(product_generation=3),
            product_generation=3,
            relation_identity=relation_identity,
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
        signed_model = _signed_model(product_generation=3)
        observer = PostgreSQLProductSemanticObserver(
            settings,
            signed_model=signed_model,
            trusted_compiler_keys=_trusted_compiler_keys(),
        )
        request = _materialization_receipt(signed_model, commit_reference)

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

        publication_request = _materialization_request(
            signed_model,
            run_id="run-live-replaced-publication",
            product_generation=request.product_generation,
            retention_seconds=3600,
        )
        warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings.model_copy(update={"dsn": SecretStr(bootstrap_dsn)}),
            signed_model=signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )
        committed_output = MaterializationObservation(
            provider_commit_reference=commit_reference,
            output_schema_digest=request.output_schema_digest,
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
        signed_model = _signed_model(product_generation=3)
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
        request = _materialization_request(
            signed_model,
            run_id="run-live-reused-target",
            product_generation=3,
            retention_seconds=3600,
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
        current_signed_model = _signed_model(product_generation=3)
        current_request = _materialization_request(
            current_signed_model,
            run_id="run-live-exact-pointer-replay",
            product_generation=3,
            retention_seconds=7200,
        )
        current_warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings,
            signed_model=current_signed_model,
            invoker=cast(DbtInvoker, _DbtMustNotRun()),
        )

        current_warehouse.switch_consumption_view(
            current_request,
            _materialization_observation(
                current_signed_model,
                product_generation=current_request.product_generation,
                relation_identity=current_relation_identity,
            ),
        )

        with psycopg.connect(bootstrap_dsn) as connection:
            retained_after = connection.execute(
                "SELECT retained_until FROM product_control.product_generation_pointers "
                "WHERE tenant_id = 'tenant-live-a' AND product_id = 'product-revenue'"
            ).fetchone()
        assert retained_after == retained_before

        stale_signed_model = _signed_model(product_generation=2)
        stale_request = _materialization_request(
            stale_signed_model,
            run_id="run-live-stale-pointer",
            product_generation=2,
            retention_seconds=7200,
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
                    stale_signed_model,
                    product_generation=stale_request.product_generation,
                    relation_identity=stale_relation_identity,
                ),
            )

        assert caught.value.classification == "integrity_failure"
        with psycopg.connect(bootstrap_dsn) as connection:
            rows = connection.execute("SELECT region FROM consumption.product_revenue").fetchall()
        assert rows == [("west",)]
