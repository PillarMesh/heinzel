"""The request-to-product journey with the compiler in it, on the pinned PostgreSQL engine.

This journey uses no hand-written statement and no placeholder compiler input: no invented IIR
or observation digests, fake legality rule, unsigned cardinality, or local PostgreSQL that is not
the pinned engine. Every input is the owning service's real artifact, on the digest-pinned
PostgreSQL 18.6 image:

- source rows are acquired and landed through the real providers and generation ledger;
- the landing relation is observed and signed by the PostgreSQL provider observer;
- the physical plan is composed by the compiler from a generation authority built from the
  committed LAND receipt;
- input cardinality is resolved from the generation ledger and signed by the runtime;
- the compiler is run with all of it, and must leave exactly the three governed preconditions
  (15 runtime magnitude, 17 live evidence review, 18 independent review) unsatisfied;
- the compiler's own guarded statement is then materialized through dbt, and the product
  reconciles to the inserted source rows.

Nothing is admitted. The compiler returns ``NoValidPlan``; the materialization step signs its own
execution authorization with a legality decision digest that is explicitly derived from that
refusal, so it cannot be mistaken for an approval. What this proves is that every input the
compiler needs to reach its governed gates can be produced live, and that the statement it emits
works end to end. It does not prove admission.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import shutil
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler import (
    NoValidPlan,
    ProductPhysicalPlanAuthority,
    compile_product_iir,
    compose_product_physical_plan_candidate,
)
from heinzel_contract_model import ArtifactReference, digest
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
    ProductInputCardinalityEvidenceSigner,
    ProductInputCardinalityEvidenceVerifier,
    ProductJsonFieldBinding,
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
from heinzel_provider_openmetadata import OpenMetadataProductCatalogProvider
from heinzel_provider_postgresql import (
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLMaterializedColumn,
    PostgreSQLProductSqlObservationRequest,
    PostgreSQLProductSqlObservationSettings,
    PostgreSQLProductSqlObserver,
    postgresql_materialized_schema_digest,
)
from heinzel_provider_postgresql.product_sql_observation import _pinned_image_digest
from heinzel_provider_sdk import (
    ProductSqlProviderObservationSigner,
    ProductSqlProviderObservationVerifier,
)
from heinzel_runtime import (
    GenerationLedger,
    MaterializationCatalog,
    MaterializationRequest,
    ProductInputCardinalityResolver,
    ProductInputGenerationExpectation,
    ProductMaterializationAdmission,
    ProductMaterializationRunner,
    SQLiteProductInputCardinalityEvidenceRepository,
)
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from psycopg import sql
from pydantic import SecretStr

from tests.integration.compiled_journey_catalog_authorities import (
    MEASURE_COLUMN,
    compose_journey_catalog,
    journey_product_authorities,
)
from tests.integration.openmetadata_live_harness import LocalOpenMetadata
from tests.integration.test_postgresql_checked_sum_evidence import _pinned_postgresql
from tests.integration.test_postgresql_product_materialization_live import (
    _acquire_rows,
    _land_rows,
    _provision_cluster,
    _role_dsn,
    _UnavailableCatalog,
    _write_dbt_profile,
)

_RUN_LIVE = os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") == "1"
_TENANT = "tenant-live-a"
_BINDING_ID = "warehouse-live-a"
_BINDING_REVISION = 1
_PRODUCT_ID = "product_revenue"
_NAMESPACE = "consumption"
_RELATION_NAME = "product_revenue"
_RELATION_REF = "relation-raw-sales-v1"
_MODEL_NAME = "product_revenue_v1_g1"
_GOVERNED_GATES = (15, 17, 18)

# The product's approved authorities, from which the contract reference and digest are read rather
# than written down. A hand-written contract digest cannot be published: the catalog adapter
# requires the materialization's digest to equal the digest of the approved contract, and no
# literal equals that. See tests/integration/test_compiled_journey_catalog_authorities.py, which
# holds that refusal offline.
_AUTHORITIES = journey_product_authorities(
    tenant_id=_TENANT,
    product_id=_PRODUCT_ID,
    warehouse_binding_id=_BINDING_ID,
    warehouse_binding_revision=_BINDING_REVISION,
    namespace=_NAMESPACE,
    relation_name=_RELATION_NAME,
)
_CONTRACT_REF = _AUTHORITIES.contract.contract_id
_CONTRACT_DIGEST = _AUTHORITIES.contract_digest


def _product() -> ProductIntentIR:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    return ProductIntentIR(
        product_ref="product_revenue",
        source=SourceRelation(
            relation_namespace="raw",
            relation_name="revenue_events",
            alias="revenue_events",
            columns=(
                ColumnDeclaration(name="region", value_type="string", nullable=False),
                ColumnDeclaration(name="revenue", value_type="decimal", nullable=False),
            ),
        ),
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(output_name="region", expression=region),
                    NamedExpression(output_name="revenue", expression=revenue),
                )
            ),
            AggregateOperation(
                group_by=(region,),
                measures=(
                    AggregateMeasure(function="sum", argument=revenue, output_name="total_revenue"),
                ),
            ),
        ),
        grain=(region,),
        freshness_seconds=3600,
    )


def _warehouse_validation(dsn: str) -> WarehouseValidationEvidence:
    """Validation evidence for the engine actually running, read from the server itself."""
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            "SELECT current_setting('server_version_num'), current_setting('server_version')"
        ).fetchone()
    assert row is not None
    version_number, version_text = str(row[0]), str(row[1])
    return WarehouseValidationEvidence(
        evidence_id="warehouse-validation-compiled-journey-1",
        tenant_id=_TENANT,
        binding_id=_BINDING_ID,
        binding_revision=_BINDING_REVISION,
        validation_profile=WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        engine_kind=EngineKind.POSTGRESQL,
        engine_version=f"{int(version_number) // 10_000}.{int(version_number) % 10_000}",
        engine_build_digest=digest(
            {
                "domain": "heinzel-postgresql-engine-build-v1",
                "version": {"server_version_num": version_number, "server_version": version_text},
            }
        ),
        engine_image_digest=_pinned_image_digest(),
        principal_profile_digest="1" * 64,
        namespace_grant_matrix_digest="2" * 64,
        tls_probe_digest="3" * 64,
        network_isolation_probe_digest="4" * 64,
        encryption_at_rest_evidence_digest="5" * 64,
        encryption_at_rest_disposition=EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE,
        positive_probe_digest="6" * 64,
        denial_probe_digest="7" * 64,
        ledger_probe_digest="8" * 64,
        monitoring_probe_digest="9" * 64,
        capacity_alert_probe_digest="a" * 64,
        backup_artifact_digest="b" * 64,
        restore_verification_digest="c" * 64,
        restore_cleanup_digest="d" * 64,
        observed_at=datetime.now(UTC),
    )


def _openmetadata_state_directory(tmp_path: Path) -> Path:
    """The directory the harness keeps its catalog database and operation secrets in.

    Created here rather than left to the harness: it opens a SQLite database directly inside this
    path, and SQLite reports a missing parent as "unable to open database file", which the catalog
    repository then translates into a persistence failure a long way from the cause.
    """

    directory = tmp_path / "openmetadata"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def openmetadata_factory(tmp_path: Path) -> Generator[Callable[[], LocalOpenMetadata]]:
    """Start a local OpenMetadata only if the test asks for one, and always clean it up.

    A fixture that started the catalog unconditionally would make the variant that needs no
    catalog pay for four more containers, which is the cost this journey is careful about.
    """

    started: list[LocalOpenMetadata] = []

    def start() -> LocalOpenMetadata:
        local = LocalOpenMetadata(_openmetadata_state_directory(tmp_path))
        started.append(local)
        return local

    try:
        yield start
    finally:
        for local in started:
            local.cleanup()


@pytest.mark.live
@pytest.mark.emulator
@pytest.mark.skipif(not _RUN_LIVE, reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1")
@pytest.mark.parametrize(
    "publish_to_catalog",
    # Both variants run the same journey. The first keeps the proof that compilation and
    # materialization stand up without a catalog in the picture, so a catalog outage cannot take
    # that signal with it; the second adds the publication the product needs to be consumable.
    [False, True],
    ids=["catalog_unavailable", "catalog_published"],
)
def test_compiled_product_journey_reaches_the_governed_gates_and_materializes_on_the_pinned_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openmetadata_factory: Callable[[], LocalOpenMetadata],
    publish_to_catalog: bool,
) -> None:
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")
    with _pinned_postgresql() as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        landing_password = secrets.token_urlsafe(24)
        materialization_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=landing_password,
            materialization_password=materialization_password,
        )
        materialization_dsn = _role_dsn(
            bootstrap_dsn, "materialization_runtime", materialization_password
        )

        # Acquire and land through the real providers and generation ledger.
        staged_rows, _watermark_at, _observed_at = _acquire_rows(
            _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password)
        )
        generation_ledger = GenerationLedger.in_memory()
        landing = asyncio.run(
            _land_rows(
                _role_dsn(bootstrap_dsn, "landing_runtime", landing_password),
                staged_rows,
                ledger=generation_ledger,
                # The landed generation carries this journey's contract identity, not the
                # helper's defaults: the cardinality resolver requires the receipt's
                # contract_ref and the acknowledgement's contract_digest to equal the ones
                # resolved against, and this journey's digest is the approved contract's.
                contract_ref=_CONTRACT_REF,
                contract_digest=_CONTRACT_DIGEST,
            )
        )
        landing_receipt_digest = digest(landing.receipt)

        # Observe the landing relation the statement reads, signed inside the provider boundary.
        validation = _warehouse_validation(bootstrap_dsn)
        provider_key = Ed25519PrivateKey.generate()
        signed_observation = PostgreSQLProductSqlObserver(
            PostgreSQLProductSqlObservationSettings(
                tenant_id=_TENANT, dsn=SecretStr(materialization_dsn)
            ),
            signer=ProductSqlProviderObservationSigner("provider-live-1", provider_key),
        ).observe_signed(
            PostgreSQLProductSqlObservationRequest(
                tenant_id=_TENANT,
                warehouse_binding_id=_BINDING_ID,
                warehouse_binding_revision=_BINDING_REVISION,
                relation_ref=_RELATION_REF,
                relation_namespace="raw",
                relation_name="raw_sales",
            ),
            warehouse_validation=validation,
        )

        # A generation authority built from the committed LAND receipt, not invented.
        target_schema = "contract_" + _CONTRACT_DIGEST[:54]
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
        authority = ProductPhysicalPlanAuthority(
            tenant_id=_TENANT,
            product_id="product_revenue",
            product_revision=1,
            contract_ref=_CONTRACT_REF,
            contract_revision=1,
            contract_digest=_CONTRACT_DIGEST,
            warehouse_binding_id=_BINDING_ID,
            warehouse_binding_revision=_BINDING_REVISION,
            source=GenerationScopedProductSource(
                namespace="raw",
                relation_name="raw_sales",
                generation_column="generation_id",
                payload_column="payload",
                generation_id=landing.receipt.generation_id,
                landing_receipt_digest=landing_receipt_digest,
                observed_source_schema_digest=digest(signed_observation.observation.columns),
                field_bindings=(
                    ProductJsonFieldBinding(
                        logical_field="region", json_field="region", scalar_type="string"
                    ),
                    ProductJsonFieldBinding(
                        logical_field="revenue", json_field="revenue", scalar_type="decimal"
                    ),
                ),
            ),
            target=ProductTarget(namespace=target_schema, relation_name=_MODEL_NAME),
            expected_output_schema_digest=expected_schema_digest,
        )
        physical_plan = compose_product_physical_plan_candidate(
            _product(),
            authority=authority,
            engine="postgresql",
            provider_observation_digest=signed_observation.observation_digest,
        )

        # Input cardinality from the generation ledger, signed by the runtime.
        cardinality_signer = ProductInputCardinalityEvidenceSigner.generate("cardinality-live-1")
        signed_cardinality = ProductInputCardinalityResolver(
            ledger=generation_ledger,
            clock=lambda: datetime.now(UTC),
            authority_ref="live-generation-ledger",
            signer=cardinality_signer,
        ).resolve_signed(
            tenant_id=_TENANT,
            contract_ref=_CONTRACT_REF,
            contract_revision=1,
            contract_digest=_CONTRACT_DIGEST,
            product_plan_digest=digest(physical_plan),
            relation_ref="raw_sales",
            generations=(
                ProductInputGenerationExpectation(
                    generation_id=landing.receipt.generation_id,
                    receipt_digest=landing_receipt_digest,
                ),
            ),
            policy_maximum_contributing_rows=len(staged_rows),
        )

        # The compiler, with every input produced live.
        outcome = compile_product_iir(
            _product(),
            engine="postgresql",
            signed_provider_observation=signed_observation,
            provider_observation_verifier=ProductSqlProviderObservationVerifier(
                {"provider-live-1": provider_key.public_key()},
                maximum_observation_age=timedelta(minutes=10),
            ),
            expected_provider_observation_digest=signed_observation.observation_digest,
            expected_tenant_id=_TENANT,
            expected_warehouse_binding_id=_BINDING_ID,
            expected_warehouse_binding_revision=_BINDING_REVISION,
            expected_relation_ref=_RELATION_REF,
            expected_relation_namespace="raw",
            expected_engine_image_digest=validation.engine_image_digest,
            expected_engine_build_digest=validation.engine_build_digest,
            evaluated_at=datetime.now(UTC),
            physical_plan_authority=authority,
            signed_cardinality_evidence=signed_cardinality,
            cardinality_evidence_verifier=ProductInputCardinalityEvidenceVerifier(
                {"cardinality-live-1": cardinality_signer.public_key}
            ),
        )

        assert isinstance(outcome, NoValidPlan)
        assert outcome.execution_occurred is False
        unsatisfied = tuple(
            item.number for item in outcome.preconditions if item.status != "satisfied"
        )
        assert unsatisfied == _GOVERNED_GATES, [
            (item.number, item.reason)
            for item in outcome.preconditions
            if item.status != "satisfied"
        ]
        assert "OPERATOR(pg_catalog.~)" in physical_plan.emitted_statement

        # Materialize the compiler's own statement. Nothing was admitted, so the legality decision
        # digest is derived from the refusal and cannot be read as an approval.
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION materialization_runtime").format(
                    sql.Identifier(target_schema)
                )
            )
        profiles_directory = tmp_path / "dbt-profiles"
        _write_dbt_profile(profiles_directory, bootstrap_dsn, target_schema=target_schema)
        monkeypatch.setenv("HEINZEL_DBT_TEST_PASSWORD", materialization_password)
        compiler_key = Ed25519PrivateKey.generate()
        model = CompiledDbtModel(
            model_name=_MODEL_NAME,
            contract_digest=_CONTRACT_DIGEST,
            provider="postgresql",
            input_generation_digests=(landing_receipt_digest,),
            target_schema=target_schema,
            output_columns=physical_plan.output_columns,
            output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_revenue"),),
            quality_tests=(
                DbtColumnTest(column_name="region", kind="not_null"),
                DbtColumnTest(column_name="region", kind="unique"),
                DbtColumnTest(column_name="total_revenue", kind="not_null"),
            ),
            compiled_sql=physical_plan.emitted_statement,
        )
        signed_model = SignedCompiledDbtModel(
            model=model,
            model_digest=digest(model),
            key_id="compiler-live-1",
            signature=base64.b64encode(
                compiler_key.sign(compiled_dbt_model_signing_bytes(model))
            ).decode("ascii"),
        )
        cardinality_repository = SQLiteProductInputCardinalityEvidenceRepository.in_memory()
        cardinality_evidence_digest = cardinality_repository.record(signed_cardinality.evidence)
        unadmitted_decision_digest = digest(
            {
                "domain": "heinzel-unadmitted-live-composition-v1",
                "compiler_outcome_digest": digest(outcome),
            }
        )
        execution_signer = ProductExecutionAuthorizationSigner.generate("runtime-execution-live-1")
        issued_at = datetime.now(UTC)
        admission = ProductMaterializationAdmission(
            legality_decision_digest=unadmitted_decision_digest,
            cardinality_evidence_digest=cardinality_evidence_digest,
            signed_execution_authorization=execution_signer.sign(
                physical_plan=physical_plan,
                legality_decision_digest=unadmitted_decision_digest,
                cardinality_evidence_digest=cardinality_evidence_digest,
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=15),
            ),
        )
        catalog_state_directory = tmp_path / "catalog-state"
        catalog_state_directory.mkdir()
        composed_catalog = None
        catalog: MaterializationCatalog = _UnavailableCatalog()
        if publish_to_catalog:
            local_openmetadata = openmetadata_factory()
            managed_binding = local_openmetadata.provision_and_validate(_TENANT)
            composed_catalog = compose_journey_catalog(
                authorities=_AUTHORITIES,
                catalog_binding=managed_binding.binding,
                publication_provider=OpenMetadataProductCatalogProvider(
                    local_openmetadata.administrator_client(managed_binding)
                ),
                # The catalog records where the product physically lives, so the database it names
                # is the one the journey provisioned, not a configured guess.
                database_name=str(psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)["dbname"]),
                landing_receipt_digest=landing_receipt_digest,
                state_directory=catalog_state_directory,
            )
            catalog = composed_catalog.catalog
        runner = ProductMaterializationRunner(
            sqlite3.connect(tmp_path / "materializations.sqlite3"),
            warehouse=PostgreSQLMaterializationWarehouse(
                settings=PostgreSQLMaterializationSettings(
                    tenant_id=_TENANT,
                    dsn=SecretStr(materialization_dsn),
                    consumption_schema_name="consumption",
                    consumption_view_name="product_revenue",
                    control_schema_name="product_control",
                    generation_table_name="product_generations",
                    generation_pointer_table_name="product_generation_pointers",
                ),
                signed_model=signed_model,
                invoker=DbtInvoker(
                    trusted_compiler_keys={"compiler-live-1": compiler_key.public_key()},
                    runner=SubprocessDbtRunner(
                        DbtSubprocessSettings(
                            executable=Path(dbt_executable),
                            profiles_directory=profiles_directory,
                            workspace_directory=tmp_path,
                            timeout_seconds=180,
                            credential_environment_names=("HEINZEL_DBT_TEST_PASSWORD",),
                        )
                    ),
                ),
            ),
            catalog=catalog,
            cardinality_evidence_reader=cardinality_repository,
            execution_authorization_verifier=ProductExecutionAuthorizationVerifier(
                {"runtime-execution-live-1": execution_signer.public_key}
            ),
            clock=lambda: datetime.now(UTC),
        )
        result = runner.materialize(
            MaterializationRequest(
                run_id="run-compiled-journey-1",
                tenant_id=_TENANT,
                product_id="product_revenue",
                product_revision=1,
                product_generation=1,
                retention_seconds=3600,
                contract_digest=_CONTRACT_DIGEST,
                physical_plan=physical_plan,
                physical_plan_digest=digest(physical_plan),
                compiled_model_digest=signed_model.model_digest,
                input_generation_digests=(landing_receipt_digest,),
                input_cardinality_evidence_digest=cardinality_evidence_digest,
                expected_output_schema_digest=expected_schema_digest,
            ),
            admission=admission,
        )

        with psycopg.connect(bootstrap_dsn) as connection:
            rows = connection.execute(
                "SELECT region, total_revenue FROM consumption.product_revenue ORDER BY region"
            ).fetchall()
            engine_version = connection.execute("SHOW server_version").fetchone()

        assert engine_version is not None and str(engine_version[0]).startswith("18.6")
        assert len(staged_rows) == 3
        assert rows == [("east", 99), ("west", 30)]
        assert result.receipt.output_row_count == 2
        assert result.receipt.quality_disposition == "passed"
        assert result.receipt.legality_decision_digest == unadmitted_decision_digest
        assert result.receipt.input_generation_digests == (landing_receipt_digest,)
        # The contract digest is the approved contract's, not a literal. The catalog refuses any
        # other, so this is the agreement that makes the generation publishable at all.
        assert result.receipt.contract_digest == digest(_AUTHORITIES.contract)
        assert result.receipt.magnitude_asserted_columns == (MEASURE_COLUMN,)

        if composed_catalog is None:
            # No catalog to publish through. The generation is committed and the publication is
            # owed, which is the state the runner is required to leave behind rather than fail in.
            assert result.publication_pending is True
            assert result.publication_ref is None
            return

        assert result.publication_pending is False
        assert result.publication_ref is not None
        product_ref = ArtifactReference(
            artifact_id=_AUTHORITIES.contract.destination_product.product_name,
            version=_AUTHORITIES.contract.version,
            digest=digest(_AUTHORITIES.contract.destination_product),
        )
        published = composed_catalog.publication_repository.definition_for_reference(
            tenant_id=_TENANT, product_ref=product_ref
        )
        assert published is not None
        # Read back out of the catalog itself, not out of what was sent to it: a publication that
        # cannot be observed again is not evidence that anything was published.
        observed = OpenMetadataProductCatalogProvider(
            local_openmetadata.administrator_client(managed_binding)
        ).observe(tenant_id=_TENANT, stable_external_key=published.stable_external_key)

        assert observed.definition == published
        assert observed.definition.generation == 1
        assert observed.definition.catalog_revision == managed_binding.binding.revision
        assert tuple(column.name for column in observed.definition.columns) == (
            "region",
            MEASURE_COLUMN,
        )
        assert observed.definition.materialization_receipt_ref.digest == digest(result.receipt)
