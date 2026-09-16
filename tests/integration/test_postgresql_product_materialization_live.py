from __future__ import annotations

import asyncio
import base64
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_contract_model import ArtifactReference, canonical_bytes, digest
from pillarmesh_contract_service import (
    SourceFreshnessObservation,
    SQLiteSourceFreshnessObservationRepository,
)
from pillarmesh_dbt_adapter import (
    CompiledDbtModel,
    DbtColumnTest,
    DbtDecimalMagnitudeCheck,
    DbtInvoker,
    DbtSubprocessSettings,
    SignedCompiledDbtModel,
    SubprocessDbtRunner,
    compiled_dbt_model_signing_bytes,
)
from pillarmesh_execution_graph import (
    Decimal57OutputCheck,
    GenerationScopedProductSource,
    ProductExecutionAuthorizationSigner,
    ProductExecutionAuthorizationVerifier,
    ProductJsonFieldBinding,
    ProductPhysicalPlan,
    ProductTarget,
)
from pillarmesh_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLMaterializedColumn,
    PostgreSQLProductGenerationAuthority,
    PostgreSQLSourceObjectDeclaration,
    postgresql_materialized_schema_digest,
)
from pillarmesh_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    ProviderError,
    RawGenerationTarget,
    SourceObservationRequest,
    StagedSegment,
    acquisition_intent_key,
    staged_segment_digest,
)
from pillarmesh_runtime import (
    AnswerProductGenerationReference,
    AnswerQueryReference,
    CatalogPublicationError,
    GenerationLedger,
    LandingResult,
    LandingRunner,
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

from tests.integration.test_postgresql_answer_query_live import (
    _fresh_postgresql_cluster as _fresh_postgresql_cluster,
)


class _UnavailableCatalog:
    calls = 0

    def publish(
        self, request: MaterializationRequest, receipt: ProductMaterializationReceipt
    ) -> str:
        del request, receipt
        self.calls += 1
        raise CatalogPublicationError("OpenMetadata is not configured for local acceptance")


def _role_dsn(bootstrap_dsn: str, role: str, password: str) -> str:
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    return psycopg.conninfo.make_conninfo(
        host=parsed["host"],
        port=parsed["port"],
        dbname=parsed["dbname"],
        user=role,
        password=password,
    )


def _create_role(
    connection: psycopg.Connection[tuple[object, ...]], role: str, password: str
) -> None:
    try:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    except psycopg.Error:
        raise RuntimeError("PostgreSQL test role provisioning failed") from None


def _provision_cluster(
    bootstrap_dsn: str,
    *,
    acquisition_password: str,
    landing_password: str,
    materialization_password: str,
) -> None:
    observed_at = datetime(2026, 9, 12, tzinfo=UTC)
    with psycopg.connect(bootstrap_dsn) as connection:
        _create_role(connection, "acquisition_runtime", acquisition_password)
        _create_role(connection, "landing_runtime", landing_password)
        _create_role(connection, "materialization_runtime", materialization_password)
        connection.execute("REVOKE CREATE ON DATABASE postgres FROM PUBLIC")
        connection.execute("REVOKE CREATE, USAGE ON SCHEMA public FROM PUBLIC")
        connection.execute("CREATE SCHEMA source_data")
        connection.execute("CREATE SCHEMA private_admin")
        connection.execute("CREATE SCHEMA raw")
        connection.execute("CREATE SCHEMA land_control")
        connection.execute("CREATE SCHEMA product_control")
        connection.execute("CREATE SCHEMA consumption AUTHORIZATION materialization_runtime")
        connection.execute(
            "CREATE TABLE source_data.sales ("
            "sale_id bigint PRIMARY KEY, region text NOT NULL, customer_id bigint NOT NULL, "
            "revenue numeric NOT NULL, updated_at timestamptz NOT NULL)"
        )
        connection.execute(
            "INSERT INTO source_data.sales VALUES "
            "(1, 'west', 101, 10.00, %s), (2, 'west', 102, 20.00, %s), "
            "(3, 'east', 103, 99.00, %s)",
            (observed_at, observed_at, observed_at),
        )
        connection.execute("CREATE TABLE private_admin.secrets (secret_value text NOT NULL)")
        connection.execute(
            "CREATE TABLE raw.raw_sales (generation_id text NOT NULL, row_ordinal bigint NOT NULL, "
            "segment_digest text NOT NULL, payload jsonb NOT NULL, "
            "PRIMARY KEY (generation_id, row_ordinal))"
        )
        approved_columns = ("sale_id", "region", "customer_id", "revenue", "updated_at")
        connection.execute("GRANT USAGE ON SCHEMA source_data TO acquisition_runtime")
        connection.execute(
            sql.SQL("GRANT SELECT ({}) ON source_data.sales TO acquisition_runtime").format(
                sql.SQL(", ").join(sql.Identifier(name) for name in approved_columns)
            )
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
        connection.execute("GRANT USAGE ON SCHEMA raw, land_control TO landing_runtime")
        connection.execute("GRANT SELECT, INSERT ON raw.raw_sales TO landing_runtime")
        connection.execute("GRANT SELECT, INSERT ON land_control.land_receipts TO landing_runtime")
        connection.execute("GRANT USAGE ON SCHEMA raw TO materialization_runtime")
        connection.execute("GRANT SELECT ON raw.raw_sales TO materialization_runtime")
        connection.execute("GRANT USAGE ON SCHEMA product_control TO materialization_runtime")
        connection.execute(
            "GRANT SELECT, INSERT, UPDATE ON product_control.product_generations, "
            "product_control.product_generation_pointers TO materialization_runtime"
        )


def _acquire_rows(
    dsn: str, *, tenant_id: str = "tenant-live-a"
) -> tuple[tuple[bytes, ...], datetime, datetime]:
    fields = (
        AcquisitionField(name="sale_id", value_type="integer", nullable=False),
        AcquisitionField(name="region", value_type="string", nullable=False),
        AcquisitionField(name="customer_id", value_type="integer", nullable=False),
        AcquisitionField(name="revenue", value_type="decimal", nullable=False),
        AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
    )
    approved_schema = AcquisitionObjectSchema(
        logical_object_ref="sales",
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("sale_id",),
        source_updated_at_field="updated_at",
    )
    provider = PostgreSQLAcquisitionProvider(
        PostgreSQLAcquisitionSettings(
            dsn=SecretStr(dsn),
            connection_handle="native-live-source",
            objects=(
                PostgreSQLSourceObjectDeclaration(
                    logical_object_ref="sales",
                    schema_name="source_data",
                    table_name="sales",
                    field_names=tuple(field.name for field in fields),
                    key_name="sale_id",
                    source_updated_at_field="updated_at",
                ),
            ),
            unrelated_schema_name="private_admin",
            max_write_transaction_duration=timedelta(minutes=5),
        ),
        private_boundary_reference_factory=lambda tenant, reference: (
            f"private://{tenant}/{reference}"
        ),
        private_boundary_writer=lambda _tenant, _reference, _payload: None,
    )
    observation = provider.observe_source(
        SourceObservationRequest(
            tenant_id=tenant_id,
            source_binding_ref="source-live-a",
            object_refs=("sales",),
        )
    )
    intent = AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id=tenant_id,
            run_intent_ref="1" * 64,
            contract_digest="2" * 64,
            source_binding_ref="source-live-a",
            acquisition_mode="snapshot",
            object_refs=("sales",),
            prior_checkpoint_revision=0,
        ),
        tenant_id=tenant_id,
        run_intent_ref="1" * 64,
        contract_ref="contract-live-a",
        contract_digest="2" * 64,
        source_binding_ref="source-live-a",
        source_observation_digest=digest(observation),
        acquisition_mode="snapshot",
        object_refs=("sales",),
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=10,
        encoded_byte_ceiling=100_000,
        admitted_at=datetime.now(UTC),
    )
    session = provider.open_acquisition(intent, (approved_schema,), None)
    records = tuple(session)
    completed = session.complete()
    assert completed.boundaries[0].record_count == 3
    watermarks = tuple(record.source_updated_at for record in records)
    if any(watermark is None for watermark in watermarks):
        raise AssertionError("snapshot records must carry the measured source watermark")
    watermark_at = max(watermark for watermark in watermarks if watermark is not None)
    rows = tuple(
        canonical_bytes({field.name: field.value for field in record.fields}) for record in records
    )
    return rows, watermark_at, completed.boundaries[0].closed_at


async def _land_rows(
    dsn: str, rows: tuple[bytes, ...], *, ledger: GenerationLedger
) -> LandingResult:
    segment = StagedSegment(
        segment_digest=staged_segment_digest(rows),
        schema_digest="3" * 64,
        record_count=len(rows),
        rows=rows,
    )
    target = RawGenerationTarget(
        tenant_id="tenant-live-a",
        contract_ref="contract-live-a",
        contract_revision=1,
        trigger_window="2026-09-12T00:00:00Z/PT1H",
        destination_binding_ref="destination-live-a",
        logical_object_ref="sales",
        table_ref="raw_sales",
        schema_digest=segment.schema_digest,
    )
    provider = PostgreSQLDestinationProvider(
        store=PostgreSQLLandStore(
            PostgreSQLLandStoreSettings(
                dsn=SecretStr(dsn),
                raw_schema_name="raw",
                ledger_schema_name="land_control",
                ledger_table_name="land_receipts",
            )
        )
    )
    return await LandingRunner(provider=provider, ledger=ledger).land(
        segment=segment,
        target=target,
        idempotency_key="4" * 64,
        batch_id="7" * 64,
        batch_manifest_digest="5" * 64,
        candidate_checkpoint_digest="6" * 64,
        prior_checkpoint_revision=0,
        contract_digest="2" * 64,
        source_binding_ref="source-live-a",
        consumer_ref="destination-live-a",
    )


def _signed_model(
    private_key: Ed25519PrivateKey,
    *,
    input_generation_digest: str,
    generation_id: str,
    target_schema: str,
) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name="product_revenue_v1_g1",
        contract_digest="2" * 64,
        provider="postgresql",
        input_generation_digests=(input_generation_digest,),
        target_schema=target_schema,
        output_columns=("region", "total_revenue"),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
        quality_tests=(
            DbtColumnTest(column_name="region", kind="not_null"),
            DbtColumnTest(column_name="region", kind="unique"),
            DbtColumnTest(column_name="total_revenue", kind="not_null"),
        ),
        compiled_sql=(
            "SELECT payload->>'region' AS region, "
            "SUM((payload->>'revenue')::numeric) AS total_revenue "
            "FROM raw.raw_sales "
            f"WHERE generation_id = '{generation_id}' GROUP BY payload->>'region'"
        ),
    )
    signature = private_key.sign(compiled_dbt_model_signing_bytes(model))
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-live-1",
        signature=base64.b64encode(signature).decode("ascii"),
    )


def _product_physical_plan(
    signed_model: SignedCompiledDbtModel,
    *,
    input_generation_digest: str,
    generation_id: str,
    expected_output_schema_digest: str,
    product_revision: int = 1,
) -> ProductPhysicalPlan:
    return ProductPhysicalPlan(
        compiler_version="compiler-live-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id="tenant-live-a",
        product_id="product-revenue",
        product_revision=product_revision,
        contract_ref="contract-live-a",
        contract_revision=1,
        contract_digest="2" * 64,
        iir_digest="8" * 64,
        provider="postgresql",
        warehouse_binding_id="warehouse-live-a",
        warehouse_binding_revision=1,
        provider_observation_digest="9" * 64,
        source=GenerationScopedProductSource(
            namespace="raw",
            relation_name="raw_sales",
            generation_column="generation_id",
            payload_column="payload",
            generation_id=generation_id,
            landing_receipt_digest=input_generation_digest,
            observed_source_schema_digest="3" * 64,
            field_bindings=(
                ProductJsonFieldBinding(
                    logical_field="region", json_field="region", scalar_type="string"
                ),
                ProductJsonFieldBinding(
                    logical_field="revenue", json_field="revenue", scalar_type="decimal"
                ),
            ),
        ),
        target=ProductTarget(
            namespace=signed_model.model.target_schema,
            relation_name=signed_model.model.model_name,
        ),
        emitted_statement=signed_model.model.compiled_sql,
        statement_digest=digest(signed_model.model.compiled_sql),
        output_columns=signed_model.model.output_columns,
        expected_output_schema_digest=expected_output_schema_digest,
        decimal_output_checks=(Decimal57OutputCheck(column_name="total_revenue"),),
    )


def _write_dbt_profile(
    directory: Path,
    bootstrap_dsn: str,
    *,
    target_schema: str,
) -> None:
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    directory.mkdir(mode=0o700)
    (directory / "profiles.yml").write_text(
        "pillarmesh_materialization:\n"
        "  target: postgresql\n"
        "  outputs:\n"
        "    postgresql:\n"
        "      type: postgres\n"
        f"      host: {parsed['host']}\n"
        f"      port: {parsed['port']}\n"
        f"      dbname: {parsed['dbname']}\n"
        "      user: materialization_runtime\n"
        "      password: \"{{ env_var('PILLARMESH_DBT_TEST_PASSWORD') }}\"\n"
        f"      schema: {target_schema}\n"
        "      threads: 1\n"
        "      sslmode: disable\n",
        encoding="utf-8",
    )


@pytest.mark.live
def test_fresh_source_acquisition_land_and_dbt_materialization_commit_one_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")
    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        landing_password = secrets.token_urlsafe(24)
        materialization_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=landing_password,
            materialization_password=materialization_password,
        )
        staged_rows, source_watermark_at, source_observed_at = _acquire_rows(
            _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password)
        )
        generation_ledger = GenerationLedger.in_memory()
        landing = asyncio.run(
            _land_rows(
                _role_dsn(bootstrap_dsn, "landing_runtime", landing_password),
                staged_rows,
                ledger=generation_ledger,
            )
        )
        input_generation_digest = digest(landing.receipt)
        source_freshness_path = tmp_path / "source-freshness.sqlite3"
        source_freshness_repository = SQLiteSourceFreshnessObservationRepository(
            str(source_freshness_path)
        )
        source_freshness = source_freshness_repository.store(
            SourceFreshnessObservation(
                observation_id=digest(
                    {
                        "domain": "pillarmesh-source-freshness-v1",
                        "source_ref": "source-live-a",
                        "input_generation_digest": input_generation_digest,
                    }
                ),
                tenant_id="tenant-live-a",
                version=1,
                source_ref="source-live-a",
                input_generation_digest=input_generation_digest,
                data_observation_ref=ArtifactReference(
                    artifact_id=landing.receipt.generation_id,
                    version=1,
                    digest=digest(landing.receipt),
                ),
                watermark_at=source_watermark_at,
                observed_at=source_observed_at,
            )
        )
        source_freshness_repository.close()
        source_freshness_repository = SQLiteSourceFreshnessObservationRepository(
            str(source_freshness_path)
        )
        durable_source_freshness = source_freshness_repository.read_for_generation(
            tenant_id="tenant-live-a",
            input_generation_digest=input_generation_digest,
        )
        source_freshness_repository.close()
        target_schema = "contract_" + ("2" * 64)[:54]
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION materialization_runtime").format(
                    sql.Identifier(target_schema)
                )
            )
        profiles_directory = tmp_path / "dbt-profiles"
        _write_dbt_profile(profiles_directory, bootstrap_dsn, target_schema=target_schema)
        monkeypatch.setenv("PILLARMESH_DBT_TEST_PASSWORD", materialization_password)
        private_key = Ed25519PrivateKey.generate()
        signed_model = _signed_model(
            private_key,
            input_generation_digest=input_generation_digest,
            generation_id=landing.receipt.generation_id,
            target_schema=target_schema,
        )
        cardinality_evidence_repository = (
            SQLiteProductInputCardinalityEvidenceRepository.in_memory()
        )
        cardinality_resolver = ProductInputCardinalityResolver(
            ledger=generation_ledger,
            clock=lambda: datetime.now(UTC),
            authority_ref="live-generation-ledger",
        )
        settings = PostgreSQLMaterializationSettings(
            tenant_id="tenant-live-a",
            dsn=SecretStr(
                _role_dsn(bootstrap_dsn, "materialization_runtime", materialization_password)
            ),
            consumption_schema_name="consumption",
            consumption_view_name="product_revenue",
            control_schema_name="product_control",
            generation_table_name="product_generations",
            generation_pointer_table_name="product_generation_pointers",
        )
        warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings,
            signed_model=signed_model,
            invoker=DbtInvoker(
                trusted_compiler_keys={"compiler-live-1": private_key.public_key()},
                runner=SubprocessDbtRunner(
                    DbtSubprocessSettings(
                        executable=Path(dbt_executable),
                        profiles_directory=profiles_directory,
                        workspace_directory=tmp_path,
                        timeout_seconds=180,
                        credential_environment_names=("PILLARMESH_DBT_TEST_PASSWORD",),
                    )
                ),
            ),
        )
        expected_schema_digest = postgresql_materialized_schema_digest(
            (
                PostgreSQLMaterializedColumn(
                    ordinal=1, name="region", data_type="text", nullable=True
                ),
                PostgreSQLMaterializedColumn(
                    ordinal=2, name="total_revenue", data_type="numeric", nullable=True
                ),
            )
        )
        physical_plan = _product_physical_plan(
            signed_model,
            input_generation_digest=input_generation_digest,
            generation_id=landing.receipt.generation_id,
            expected_output_schema_digest=expected_schema_digest,
        )
        cardinality_evidence_digest = cardinality_evidence_repository.record(
            cardinality_resolver.resolve(
                tenant_id="tenant-live-a",
                contract_ref="contract-live-a",
                contract_revision=1,
                contract_digest="2" * 64,
                product_plan_digest=digest(physical_plan),
                relation_ref="raw_sales",
                generations=(
                    ProductInputGenerationExpectation(
                        generation_id=landing.receipt.generation_id,
                        receipt_digest=input_generation_digest,
                    ),
                ),
                policy_maximum_contributing_rows=len(staged_rows),
            )
        )
        execution_signer = ProductExecutionAuthorizationSigner.generate("runtime-execution-live-1")
        legality_decision_digest = "a" * 64
        authorization_issued_at = datetime.now(UTC)
        admission = ProductMaterializationAdmission(
            legality_decision_digest=legality_decision_digest,
            cardinality_evidence_digest=cardinality_evidence_digest,
            signed_execution_authorization=execution_signer.sign(
                physical_plan=physical_plan,
                legality_decision_digest=legality_decision_digest,
                cardinality_evidence_digest=cardinality_evidence_digest,
                issued_at=authorization_issued_at,
                expires_at=authorization_issued_at + timedelta(minutes=15),
            ),
        )
        execution_verifier = ProductExecutionAuthorizationVerifier(
            {"runtime-execution-live-1": execution_signer.public_key}
        )
        catalog = _UnavailableCatalog()
        ledger_path = tmp_path / "materializations.sqlite3"
        materialization_connection = sqlite3.connect(ledger_path)
        runner = ProductMaterializationRunner(
            materialization_connection,
            warehouse=warehouse,
            catalog=catalog,
            cardinality_evidence_reader=cardinality_evidence_repository,
            execution_authorization_verifier=execution_verifier,
            clock=lambda: datetime.now(UTC),
        )
        request = MaterializationRequest(
            run_id="run-live-1",
            tenant_id="tenant-live-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
            retention_seconds=3600,
            contract_digest="2" * 64,
            physical_plan=physical_plan,
            physical_plan_digest=digest(physical_plan),
            compiled_model_digest=signed_model.model_digest,
            input_generation_digests=(input_generation_digest,),
            input_cardinality_evidence_digest=cardinality_evidence_digest,
            expected_output_schema_digest=expected_schema_digest,
        )

        result = runner.materialize(request, admission=admission)
        materialization_connection.close()
        replay_connection = sqlite3.connect(ledger_path)
        replay_runner = ProductMaterializationRunner(
            replay_connection,
            warehouse=warehouse,
            catalog=catalog,
            cardinality_evidence_reader=cardinality_evidence_repository,
            execution_authorization_verifier=execution_verifier,
            clock=lambda: datetime.now(UTC),
        )
        replay = replay_runner.materialize(request, admission=admission)
        durable_receipt = replay_runner.read_receipt(
            tenant_id="tenant-live-a",
            product_id="product-revenue",
            product_revision=1,
            product_generation=1,
        )
        generation_state = PostgreSQLProductGenerationAuthority(settings).observe(
            AnswerProductGenerationReference(
                product_ref=AnswerQueryReference(
                    artifact_id="product-revenue",
                    version=1,
                    digest="7" * 64,
                ),
                generation=1,
            )
        )
        with psycopg.connect(bootstrap_dsn) as connection:
            rows = connection.execute(
                "SELECT region, total_revenue FROM consumption.product_revenue ORDER BY region"
            ).fetchall()
            landed_count = connection.execute("SELECT count(*) FROM raw.raw_sales").fetchone()
            generation_count = connection.execute(
                "SELECT count(*) FROM product_control.product_generations"
            ).fetchone()

        assert len(staged_rows) == 3
        assert landed_count == (3,)
        assert result.publication_pending is True and result.publication_ref is None
        assert replay.receipt == result.receipt
        assert durable_receipt == result.receipt
        assert replay.publication_pending is True
        assert catalog.calls == 2
        assert result.receipt.input_generation_digests == (input_generation_digest,)
        assert result.receipt.input_cardinality_evidence_digest == cardinality_evidence_digest
        assert durable_source_freshness is not None
        assert durable_source_freshness == source_freshness
        assert durable_source_freshness.watermark_at == source_watermark_at
        assert durable_source_freshness.data_observation_ref.digest == digest(landing.receipt)
        assert result.receipt.output_row_count == 2
        assert result.receipt.product_generation == 1
        assert result.receipt.retained_until > result.receipt.committed_at
        assert result.receipt.quality_assertion_count == 3
        assert result.receipt.quality_disposition == "passed"
        assert generation_state.addressable is True
        assert generation_state.current_generation == 1
        assert generation_count == (1,)
        assert rows == [("east", 99), ("west", 30)]
        invalid_model = signed_model.model.model_copy(
            update={
                "model_name": "product_revenue_v1_g2",
                "compiled_sql": "SELECT NULL::text AS region, 1::numeric AS total_revenue",
            }
        )
        invalid_signed_model = SignedCompiledDbtModel(
            model=invalid_model,
            model_digest=digest(invalid_model),
            key_id="compiler-live-1",
            signature=base64.b64encode(
                private_key.sign(compiled_dbt_model_signing_bytes(invalid_model))
            ).decode(),
        )
        invalid_physical_plan = _product_physical_plan(
            invalid_signed_model,
            input_generation_digest=input_generation_digest,
            generation_id=landing.receipt.generation_id,
            expected_output_schema_digest=expected_schema_digest,
            product_revision=2,
        )
        invalid_cardinality_evidence_digest = cardinality_evidence_repository.record(
            cardinality_resolver.resolve(
                tenant_id="tenant-live-a",
                contract_ref="contract-live-a",
                contract_revision=1,
                contract_digest="2" * 64,
                product_plan_digest=digest(invalid_physical_plan),
                relation_ref="raw_sales",
                generations=(
                    ProductInputGenerationExpectation(
                        generation_id=landing.receipt.generation_id,
                        receipt_digest=input_generation_digest,
                    ),
                ),
                policy_maximum_contributing_rows=len(staged_rows),
            )
        )
        invalid_admission = ProductMaterializationAdmission(
            legality_decision_digest=legality_decision_digest,
            cardinality_evidence_digest=invalid_cardinality_evidence_digest,
            signed_execution_authorization=execution_signer.sign(
                physical_plan=invalid_physical_plan,
                legality_decision_digest=legality_decision_digest,
                cardinality_evidence_digest=invalid_cardinality_evidence_digest,
                issued_at=authorization_issued_at,
                expires_at=authorization_issued_at + timedelta(minutes=15),
            ),
        )
        invalid_warehouse = PostgreSQLMaterializationWarehouse(
            settings=settings,
            signed_model=invalid_signed_model,
            invoker=DbtInvoker(
                trusted_compiler_keys={"compiler-live-1": private_key.public_key()},
                runner=SubprocessDbtRunner(
                    DbtSubprocessSettings(
                        executable=Path(dbt_executable),
                        profiles_directory=profiles_directory,
                        workspace_directory=tmp_path,
                        timeout_seconds=180,
                        credential_environment_names=("PILLARMESH_DBT_TEST_PASSWORD",),
                    )
                ),
            ),
        )
        replacement_runner = ProductMaterializationRunner(
            replay_connection,
            warehouse=invalid_warehouse,
            catalog=catalog,
            cardinality_evidence_reader=cardinality_evidence_repository,
            execution_authorization_verifier=execution_verifier,
            clock=lambda: datetime.now(UTC),
        )
        with pytest.raises(ProviderError, match="did not produce verified dbt evidence") as error:
            replacement_runner.materialize(
                request.model_copy(
                    update={
                        "run_id": "run-live-invalid-quality",
                        "product_generation": 2,
                        "product_revision": 2,
                        "physical_plan": invalid_physical_plan,
                        "physical_plan_digest": digest(invalid_physical_plan),
                        "compiled_model_digest": invalid_signed_model.model_digest,
                        "input_cardinality_evidence_digest": (invalid_cardinality_evidence_digest),
                    }
                ),
                admission=invalid_admission,
            )
        assert error.value.classification == "ambiguous_outcome"
        with psycopg.connect(bootstrap_dsn) as connection:
            preserved_rows = connection.execute(
                "SELECT region, total_revenue FROM consumption.product_revenue ORDER BY region"
            ).fetchall()
            preserved_generation_count = connection.execute(
                "SELECT count(*) FROM product_control.product_generations"
            ).fetchone()
        assert preserved_rows == rows
        assert preserved_generation_count == (1,)
        assert (
            replacement_runner.read_receipt(
                tenant_id="tenant-live-a",
                product_id="product-revenue",
                product_revision=1,
                product_generation=2,
            )
            is None
        )
        replay_connection.close()
