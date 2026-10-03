"""The demonstration's product: its intent, and the plan the compiler emits for it.

The product the governed answer is asked of is compiled, not written down. The intent
below goes through `compose_product_physical_plan_candidate`, the compiler's own entry
point, so the statement that materializes the product is one the compiler emitted under
its restricted shape and its legality rule. A plan assembled by hand here would run the
same dbt model and prove nothing about whether the product is expressible.

The shape is deliberately narrow. `AggregateMeasure.function` admits `sum` alone, which is
why the demonstration's metric is a daily order value rather than a daily order count:
a counted metric has no product behind it. Governed queries do admit `count`, so this is
the product shape's limit and not the query's.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler import ProductPhysicalPlanAuthority, compose_product_physical_plan_candidate
from heinzel_contract_model import ManagedIntegrationContract, digest
from heinzel_dbt_adapter import (
    CompiledDbtModel,
    DbtColumnTest,
    DbtDecimalMagnitudeCheck,
    DbtInvoker,
    DbtSubprocessSettings,
    SignedCompiledDbtModel,
    SubprocessDbtRunner,
    compiled_dbt_model_signing_bytes,
)
from heinzel_execution_graph import (
    GenerationScopedProductSource,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from heinzel_iir import (
    AggregateMeasure,
    AggregateOperation,
    ColumnDeclaration,
    ColumnReference,
    NamedExpression,
    ProductIntentIR,
    ProjectOperation,
    SourceRelation,
)
from heinzel_provider_postgresql import (
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLMaterializedColumn,
    postgresql_materialized_schema_digest,
)
from heinzel_runtime import (
    GenerationLedger,
    MaterializationRequest,
    ProductInputCardinalityResolver,
    ProductInputGenerationExpectation,
    ProductMaterializationAdmission,
    ProductMaterializationReceipt,
    ProductMaterializationRunner,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from psycopg import sql
from pydantic import SecretStr

from .generation import DEMO_LOGICAL_OBJECT, DEMO_RAW_TABLE
from .warehouse import role_dsn

__all__ = [
    "DEMO_COMPILER_KEY_ID",
    "DEMO_GROUP_COLUMN",
    "DEMO_MEASURE_COLUMN",
    "DEMO_PRODUCT_REF",
    "MaterializedDemoProduct",
    "compose_demo_physical_plan",
    "demo_product_intent",
    "materialize_demo_generation",
    "sign_demo_model",
    "unadmitted_decision_digest",
]

DEMO_PRODUCT_REF = "orders_daily"
DEMO_GROUP_COLUMN = "ordered_on"
DEMO_MEASURE_COLUMN = "total_order_value"
DEMO_COMPILER_KEY_ID = "compiler-demo-1"

# dbt reads the materialization password from this variable, so the profile on disk holds
# no credential and the container log never carries one. The name is not free: the adapter
# admits `HEINZEL_DBT_` and `DBT_ENV_SECRET_` prefixes alone and refuses the invocation
# outright for anything else, before dbt is reached.
_DBT_PASSWORD_VARIABLE = "HEINZEL_DBT_DEMO_PASSWORD"

_SOURCE_VALUE_COLUMN = "order_total"
_RAW_RELATION = "raw_customer_orders"

# The demonstration measures one day of orders, so a product older than a day is stale.
_FRESHNESS_SECONDS = 86400


def demo_product_intent() -> ProductIntentIR:
    """The demonstration's product: order value summed per day.

    `ordered_on` is a string because the acquisition provider admits no `date` column, so
    the day reaches the landed payload as an ISO string. Grouping by it is the day itself
    rather than an instant that happens to fall on midnight.
    """
    ordered_on = ColumnReference(relation_alias=DEMO_LOGICAL_OBJECT, column_name=DEMO_GROUP_COLUMN)
    order_total = ColumnReference(
        relation_alias=DEMO_LOGICAL_OBJECT, column_name=_SOURCE_VALUE_COLUMN
    )
    return ProductIntentIR(
        product_ref=DEMO_PRODUCT_REF,
        source=SourceRelation(
            relation_namespace="logical",
            relation_name=DEMO_LOGICAL_OBJECT,
            alias=DEMO_LOGICAL_OBJECT,
            columns=(
                ColumnDeclaration(name=DEMO_GROUP_COLUMN, value_type="string", nullable=False),
                ColumnDeclaration(name=_SOURCE_VALUE_COLUMN, value_type="decimal", nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(output_name=DEMO_GROUP_COLUMN, expression=ordered_on),
                    NamedExpression(output_name=_SOURCE_VALUE_COLUMN, expression=order_total),
                )
            ),
            AggregateOperation(
                group_by=(ordered_on,),
                measures=(
                    AggregateMeasure(
                        function="sum",
                        argument=order_total,
                        output_name=DEMO_MEASURE_COLUMN,
                    ),
                ),
            ),
        ),
        grain=(ordered_on,),
        freshness_seconds=_FRESHNESS_SECONDS,
    )


def demo_generation_source(
    *, generation_id: str, landing_receipt_digest: str, observed_schema_digest: str
) -> GenerationScopedProductSource:
    """The landed relation the product reads, scoped to one generation.

    The generation is part of the source rather than a filter applied afterwards, so a
    statement that read a different generation would be a different statement.
    """
    return GenerationScopedProductSource(
        namespace="raw",
        relation_name=_RAW_RELATION,
        generation_column="generation_id",
        payload_column="payload",
        generation_id=generation_id,
        landing_receipt_digest=landing_receipt_digest,
        observed_source_schema_digest=observed_schema_digest,
        field_bindings=(
            ProductJsonFieldBinding(
                logical_field=DEMO_GROUP_COLUMN,
                json_field=DEMO_GROUP_COLUMN,
                scalar_type="string",
            ),
            ProductJsonFieldBinding(
                logical_field=_SOURCE_VALUE_COLUMN,
                json_field=_SOURCE_VALUE_COLUMN,
                scalar_type="decimal",
            ),
        ),
    )


def compose_demo_physical_plan(
    *,
    contract: ManagedIntegrationContract,
    source: GenerationScopedProductSource,
    target_schema: str,
    model_name: str,
    expected_output_schema_digest: str,
    provider_observation_digest: str,
) -> ProductPhysicalPlan:
    """Compile the demonstration's intent into the statement that materializes it."""
    authority = ProductPhysicalPlanAuthority(
        tenant_id=contract.tenant_id,
        product_id=DEMO_PRODUCT_REF,
        product_revision=contract.version,
        contract_ref=contract.contract_id,
        contract_revision=contract.version,
        contract_digest=digest(contract),
        warehouse_binding_id=contract.destination_product.warehouse_binding_id,
        warehouse_binding_revision=1,
        source=source,
        target=ProductTarget(namespace=target_schema, relation_name=model_name),
        expected_output_schema_digest=expected_output_schema_digest,
    )
    return compose_product_physical_plan_candidate(
        demo_product_intent(),
        authority=authority,
        engine="postgresql",
        provider_observation_digest=provider_observation_digest,
    )


def sign_demo_model(
    private_key: Ed25519PrivateKey,
    *,
    plan: ProductPhysicalPlan,
    contract: ManagedIntegrationContract,
    input_generation_digest: str,
    target_schema: str,
    model_name: str,
) -> SignedCompiledDbtModel:
    """Sign the compiler's own statement, so dbt runs what the compiler emitted.

    `compiled_sql` is the plan's emitted statement rather than a second emission: a model
    signed over anything else would materialize a product the plan does not describe, and
    the signature would still verify.
    """
    model = CompiledDbtModel(
        model_name=model_name,
        contract_digest=digest(contract),
        provider="postgresql",
        input_generation_digests=(input_generation_digest,),
        target_schema=target_schema,
        output_columns=plan.output_columns,
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name=DEMO_MEASURE_COLUMN),),
        quality_tests=(
            DbtColumnTest(column_name=DEMO_GROUP_COLUMN, kind="not_null"),
            DbtColumnTest(column_name=DEMO_GROUP_COLUMN, kind="unique"),
            DbtColumnTest(column_name=DEMO_MEASURE_COLUMN, kind="not_null"),
        ),
        compiled_sql=plan.emitted_statement,
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id=DEMO_COMPILER_KEY_ID,
        signature=base64.b64encode(
            private_key.sign(compiled_dbt_model_signing_bytes(model))
        ).decode("ascii"),
    )


@dataclass(frozen=True, slots=True)
class MaterializedDemoProduct:
    """One committed product generation, and where it landed."""

    receipt: ProductMaterializationReceipt
    target_schema: str
    model_name: str
    signed_model: SignedCompiledDbtModel
    physical_plan: ProductPhysicalPlan


class _DemoCatalog:
    """The demonstration's own catalog: a reference derived from what was committed.

    `MaterializationCatalog` exists so a generation is published somewhere before it can
    be consumed. The demonstration has no external catalog service, so its publication
    reference is derived from the receipt rather than issued by one. It is deterministic
    and names the generation it describes, so two generations never share a reference.
    """

    def publish(
        self, request: MaterializationRequest, receipt: ProductMaterializationReceipt
    ) -> str:
        return (
            f"demo-catalog://{request.tenant_id}/{request.product_id}/{receipt.product_generation}"
        )


def unadmitted_decision_digest(plan: ProductPhysicalPlan) -> str:
    """The legality decision this generation was materialized under: none.

    `compile_product_iir` ends every product compilation at its governed gates, so no
    admitted legality decision exists for this product and none is invented here. The
    digest is derived under a domain that says so, and the materialization receipt carries
    it, so a reader who follows the receipt back finds a refusal rather than an approval.
    A placeholder digest would record the same thing illegibly.
    """
    return digest(
        {
            "domain": "heinzel-demonstration-unadmitted-product-v1",
            "physical_plan_digest": digest(plan),
        }
    )


def write_demo_dbt_profile(directory: Path, bootstrap_dsn: str, *, target_schema: str) -> None:
    """The dbt profile the materialization runs under, reading its password from the environment.

    The password is named rather than written: `DbtSubprocessSettings` passes the named
    variables through to dbt, so the profile on disk carries no credential.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "profiles.yml").write_text(
        "heinzel_materialization:\n"
        "  target: postgresql\n"
        "  outputs:\n"
        "    postgresql:\n"
        "      type: postgres\n"
        f"      host: {parsed['host']}\n"
        f"      port: {parsed['port']}\n"
        f"      dbname: {parsed['dbname']}\n"
        "      user: materialization_runtime\n"
        "      password: \"{{ env_var('" + _DBT_PASSWORD_VARIABLE + "') }}\"\n"
        f"      schema: {target_schema}\n"
        "      threads: 1\n"
        "      sslmode: disable\n",
        encoding="utf-8",
    )


def materialize_demo_generation(
    *,
    bootstrap_dsn: str,
    materialization_password: str,
    dbt_executable: Path,
    workspace: Path,
    contract: ManagedIntegrationContract,
    landing_receipt_digest: str,
    generation_id: str,
    record_count: int,
    ledger: GenerationLedger,
) -> MaterializedDemoProduct:
    """Compile, admit and run the demonstration's product, committing one generation.

    Every input is the owning component's own artifact: the statement comes from the
    compiler, the input cardinality from the generation ledger, and the execution
    authorization is signed over the plan it authorizes. What is deliberately absent is an
    admitted legality decision -- see `unadmitted_decision_digest`.
    """
    target_schema = "contract_" + digest(contract)[:54]
    model_name = f"{DEMO_PRODUCT_REF}_g1"

    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION materialization_runtime").format(
                sql.Identifier(target_schema)
            )
        )

    profiles = workspace / "dbt-profiles"
    write_demo_dbt_profile(profiles, bootstrap_dsn, target_schema=target_schema)

    schema_digest = postgresql_materialized_schema_digest(
        (
            PostgreSQLMaterializedColumn(
                ordinal=1, name=DEMO_GROUP_COLUMN, data_type="text", nullable=True
            ),
            PostgreSQLMaterializedColumn(
                ordinal=2, name=DEMO_MEASURE_COLUMN, data_type="numeric", nullable=True
            ),
        )
    )
    plan = compose_demo_physical_plan(
        contract=contract,
        source=demo_generation_source(
            generation_id=generation_id,
            landing_receipt_digest=landing_receipt_digest,
            observed_schema_digest=digest(contract),
        ),
        target_schema=target_schema,
        model_name=model_name,
        expected_output_schema_digest=schema_digest,
        provider_observation_digest=digest(contract),
    )
    # The key is held rather than generated inline, because the invoker verifies the model
    # against the public half: a signature nobody can check is not a signature.
    compiler_key = Ed25519PrivateKey.generate()
    signed_model = sign_demo_model(
        compiler_key,
        plan=plan,
        contract=contract,
        input_generation_digest=landing_receipt_digest,
        target_schema=target_schema,
        model_name=model_name,
    )

    cardinality = ProductInputCardinalityResolver(
        ledger=ledger,
        clock=lambda: datetime.now(UTC),
        authority_ref="demo-generation-ledger-v1",
    ).resolve(
        tenant_id=contract.tenant_id,
        contract_ref=contract.contract_id,
        contract_revision=contract.version,
        contract_digest=digest(contract),
        product_plan_digest=digest(plan),
        # The landed relation, not the logical object: the cardinality resolver compares
        # this against the landing receipt's `target_table_ref`, and a logical name that
        # merely describes the same data fails there rather than here.
        relation_ref=DEMO_RAW_TABLE,
        generations=(
            ProductInputGenerationExpectation(
                generation_id=generation_id, receipt_digest=landing_receipt_digest
            ),
        ),
        policy_maximum_contributing_rows=record_count,
    )
    cardinality_repository = SQLiteProductInputCardinalityEvidenceRepository(
        sqlite3.connect(workspace / "product-input-cardinality.sqlite3", check_same_thread=False)
    )
    cardinality_digest = cardinality_repository.record(cardinality)

    legality_digest = unadmitted_decision_digest(plan)
    signer = ProductExecutionAuthorizationSigner.generate("demo-runtime-execution-1")
    issued_at = datetime.now(UTC)
    admission = ProductMaterializationAdmission(
        legality_decision_digest=legality_digest,
        cardinality_evidence_digest=cardinality_digest,
        signed_execution_authorization=signer.sign(
            physical_plan=plan,
            legality_decision_digest=legality_digest,
            cardinality_evidence_digest=cardinality_digest,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=15),
        ),
    )
    runner = ProductMaterializationRunner.in_memory(
        warehouse=PostgreSQLMaterializationWarehouse(
            settings=PostgreSQLMaterializationSettings(
                tenant_id=contract.tenant_id,
                dsn=SecretStr(
                    role_dsn(bootstrap_dsn, "materialization_runtime", materialization_password)
                ),
                consumption_schema_name="consumption",
                consumption_view_name=DEMO_PRODUCT_REF,
                control_schema_name="product_control",
                generation_table_name="product_generations",
                generation_pointer_table_name="product_generation_pointers",
            ),
            signed_model=signed_model,
            invoker=DbtInvoker(
                trusted_compiler_keys={DEMO_COMPILER_KEY_ID: compiler_key.public_key()},
                runner=SubprocessDbtRunner(
                    DbtSubprocessSettings(
                        executable=dbt_executable,
                        profiles_directory=profiles,
                        workspace_directory=workspace,
                        timeout_seconds=180,
                        credential_environment_names=(_DBT_PASSWORD_VARIABLE,),
                    )
                ),
            ),
        ),
        catalog=_DemoCatalog(),
        cardinality_evidence_reader=cardinality_repository,
        execution_authorization_verifier=ProductExecutionAuthorizationVerifier(
            {"demo-runtime-execution-1": signer.public_key}
        ),
        clock=lambda: datetime.now(UTC),
    )
    # The password reaches dbt through the environment and is removed again, so it does not
    # outlive the materialization in this process's environment where anything that reads
    # `os.environ` -- a later subprocess, a crash reporter -- would find it.
    previous = os.environ.get(_DBT_PASSWORD_VARIABLE)
    os.environ[_DBT_PASSWORD_VARIABLE] = materialization_password
    try:
        result = runner.materialize(
            MaterializationRequest(
                run_id="demo-materialization-1",
                tenant_id=contract.tenant_id,
                product_id=DEMO_PRODUCT_REF,
                product_revision=contract.version,
                product_generation=1,
                retention_seconds=86400,
                contract_digest=digest(contract),
                physical_plan=plan,
                physical_plan_digest=digest(plan),
                compiled_model_digest=signed_model.model_digest,
                input_generation_digests=(landing_receipt_digest,),
                input_cardinality_evidence_digest=cardinality_digest,
                expected_output_schema_digest=schema_digest,
            ),
            admission=admission,
        )
    finally:
        if previous is None:
            os.environ.pop(_DBT_PASSWORD_VARIABLE, None)
        else:
            os.environ[_DBT_PASSWORD_VARIABLE] = previous
    if result.receipt.quality_disposition != "passed":
        raise RuntimeError(
            "the demonstration's materialization did not establish passing quality: "
            f"{result.receipt.quality_disposition}"
        )
    return MaterializedDemoProduct(
        receipt=result.receipt,
        target_schema=target_schema,
        model_name=model_name,
        signed_model=signed_model,
        physical_plan=plan,
    )
