from __future__ import annotations

import asyncio
import base64
import ipaddress
import secrets
import shutil
import sqlite3
import ssl
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Literal

import httpx
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from heinzel_access_control import (
    SignedEntitlementBody,
    SignedHttpConnectedPolicyAuthority,
    SignedHttpPolicyAuthoritySettings,
)
from heinzel_bi_control import (
    DashboardCompositionService,
    DashboardContract,
    DashboardContractSigner,
    DashboardContractVerifier,
    DashboardControlService,
    DashboardDatasetConnectionBinding,
    PublishDashboardCommand,
    SQLiteDashboardConnectionRepository,
    SQLiteDashboardContractRepository,
    SQLiteDashboardRepository,
)
from heinzel_catalog_control import CatalogBinding, CatalogBindingState
from heinzel_compiler import (
    GovernedQueryInput,
    GovernedQueryPlan,
    ProductGenerationReference,
    QueryCeilings,
    QueryConsumptionObject,
    QueryDimension,
    QueryMetric,
    QueryOrder,
    QueryReference,
    QueryScan,
    QueryScanEstimate,
    compile_governed_query,
)
from heinzel_compiler.postgresql_sql import emit_generation_scoped_postgresql
from heinzel_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier
from heinzel_contract_model import (
    AccessPolicy,
    ApprovedSemanticVersion,
    ArtifactReference,
    ContractFormationStatus,
    DestinationProductRequirement,
    EvidencePolicy,
    FailurePolicy,
    FieldMapping,
    FreshnessRequirement,
    ManagedIntegrationContract,
    QualityPolicy,
    SemanticObject,
    TriggerRequirement,
    digest,
)
from heinzel_contract_service import (
    SourceFreshnessObservation,
    SQLiteSourceFreshnessObservationRepository,
)
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
    Decimal57OutputCheck,
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
    PostgreSQLAnswerGenerationAuthority,
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLMaterializedColumn,
    PostgreSQLProductGenerationAuthority,
    postgresql_materialized_schema_digest,
)
from heinzel_provider_sdk import (
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    RawGenerationTarget,
    StagedSegment,
    staged_segment_digest,
)
from heinzel_provider_sdk.bi import BiApplyResult, BiDashboardDefinition
from heinzel_request_management import (
    AnswerIntentCandidate,
    AnswerQuestion,
    AnswerScopePolicyApproval,
    AnswerScopePolicyDraft,
    AnswerScopePolicyLifecycle,
    AnswerValidationContext,
    BoundSemanticReference,
    ProductOwnerAuthority,
    RequestState,
)
from heinzel_request_management import (
    AnswerProductGenerationReference as RequestProductGenerationReference,
)
from heinzel_runtime import (
    GenerationLedger,
    LandingResult,
    LandingRunner,
    MaterializationRequest,
    ProductCatalogCompositionConfig,
    ProductInputCardinalityResolver,
    ProductInputGenerationExpectation,
    ProductMaterializationAdmission,
    ProductMaterializationRunner,
    SQLiteProductInputCardinalityEvidenceRepository,
    compose_authoritative_product_catalog,
)
from heinzel_semantic_registry import (
    ApprovedProductQueryBinding,
    ApprovedProductVersionMetadata,
    ProductCatalogColumnAuthority,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationProvider,
    ProductQueryBindingApproval,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteApprovedProductVersionRepository,
    SQLiteProductCatalogPublicationRepository,
    SQLiteProductQueryBindingRepository,
)
from heinzel_warehouse_control import EngineKind, WarehouseBinding, WarehouseBindingState
from psycopg import sql
from pydantic import SecretStr

from tests.acceptance.console_answer_runtime import GovernedAnswerRuntimeConfiguration
from tests.acceptance.console_policy_authority import (
    LocalDevelopmentPolicyAuthority,
    create_local_policy_server,
)
from tests.acceptance.console_product_authority import (
    ApprovedProductAnswerMetadataReader,
    SourceFreshnessReader,
)
from tests.acceptance.run_console_governed import (
    REQUESTER,
    REQUESTER_PRINCIPAL,
    GovernedConsoleDeployment,
)
from tests.integration.test_postgresql_product_materialization_live import (
    _acquire_rows,
    _fresh_postgresql_cluster,
    _provision_cluster,
    _role_dsn,
    _write_dbt_profile,
)

TENANT = "tenant-a"
SEMANTIC_VERSION = ApprovedSemanticVersion(
    semantic_version_id="semantic-revenue",
    tenant_id=TENANT,
    version=1,
    process_package_ref=ArtifactReference(
        artifact_id="process-revenue", version=1, digest="1" * 64
    ),
    candidate_set_digest="2" * 64,
    review_bundle_digest="3" * 64,
    entities=(
        SemanticObject(
            object_id="region",
            name="Region",
            definition="The sales region assigned to revenue.",
            source_refs=("process-revenue",),
        ),
    ),
    events=(),
    states=(),
    relationships=(),
    identity_rules=(),
    constraints=(),
    metrics=(
        SemanticObject(
            object_id="total-revenue",
            name="Total revenue",
            definition="The sum of approved revenue values.",
            source_refs=("process-revenue",),
        ),
    ),
    classifications=(),
    authority_bindings=(),
    approval_ids=("semantic-approval-live-1",),
    created_at=datetime(2026, 9, 12, tzinfo=UTC),
)
SEMANTIC = ArtifactReference(
    artifact_id=SEMANTIC_VERSION.semantic_version_id,
    version=SEMANTIC_VERSION.version,
    digest=digest(SEMANTIC_VERSION),
)
CONTRACT = ManagedIntegrationContract(
    contract_id="contract-revenue",
    tenant_id=TENANT,
    version=1,
    formation_status=ContractFormationStatus.READY_TO_ACTIVATE,
    semantic_version_ref=SEMANTIC,
    source_observation_refs=(),
    mappings=(
        FieldMapping(
            source_ref="sales.revenue",
            semantic_ref="total-revenue",
            transformation="derived",
        ),
    ),
    integrity_constraints=(),
    destination_product=DestinationProductRequirement(
        product_name="product-revenue",
        warehouse_binding_id="warehouse-a",
        supported_engines=("postgresql",),
    ),
    freshness=FreshnessRequirement(maximum_age_seconds=3_600),
    quality=QualityPolicy(required_constraint_ids=()),
    trigger_policy=TriggerRequirement(run_now_allowed=True),
    access_policy=AccessPolicy(
        classification_refs=(), required_approver_refs=("owner:product-revenue",)
    ),
    evidence_policy=EvidencePolicy(),
    failure_policy=FailurePolicy(),
    approval_ids=("contract-approval-live-1",),
)
PRODUCT = ArtifactReference(
    artifact_id=CONTRACT.destination_product.product_name,
    version=CONTRACT.version,
    digest=digest(CONTRACT.destination_product),
)
METRIC = ArtifactReference(
    artifact_id=SEMANTIC_VERSION.metrics[0].object_id,
    version=SEMANTIC_VERSION.version,
    digest=digest(SEMANTIC_VERSION.metrics[0]),
)
DIMENSION = ArtifactReference(
    artifact_id=SEMANTIC_VERSION.entities[0].object_id,
    version=SEMANTIC_VERSION.version,
    digest=digest(SEMANTIC_VERSION.entities[0]),
)


@dataclass(frozen=True, slots=True)
class NativeMaterializedProduct:
    runtime_dsn: str = field(repr=False)
    query_binding: ApprovedProductQueryBinding
    answer_generation_authority: PostgreSQLAnswerGenerationAuthority
    receipt_reader: ProductMaterializationRunner
    freshness_reader: SourceFreshnessReader
    metadata_reader: ApprovedProductAnswerMetadataReader
    generation_reader: PostgreSQLProductGenerationAuthority
    warehouse_binding: WarehouseBinding
    catalog_binding: CatalogBinding
    product_ref: ArtifactReference = PRODUCT
    semantic_ref: ArtifactReference = SEMANTIC
    metric_ref: ArtifactReference = METRIC


@dataclass(frozen=True, slots=True)
class NativeGovernedAnswer:
    deployment: GovernedConsoleDeployment = field(repr=False)
    product_publications: SQLiteProductCatalogPublicationRepository = field(repr=False)
    request_id: str
    _revoke: Callable[[], None] = field(repr=False)

    def revoke_access(self) -> None:
        self._revoke()


class _NativeDashboardProvider:
    """Deterministic BI effect for the persistent browser fixture.

    The separate live Superset suite proves the HTTP provider. This fixture keeps the
    provider process local while exercising the same BI-control desired-state and receipt
    boundary that the console reads.
    """

    provider_kind: Literal["superset"] = "superset"

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        return BiApplyResult(
            stable_external_key=definition.stable_external_key,
            desired_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url="https://superset.invalid/dashboard",
            provider_version="native-fixture-v1",
        )


@dataclass(frozen=True, slots=True)
class _NativeSignedPolicyAuthority:
    reader: SignedHttpConnectedPolicyAuthority
    database_path: Path
    signing_key: Ed25519PrivateKey = field(repr=False)

    def publish(self, body: SignedEntitlementBody) -> None:
        with sqlite3.connect(self.database_path) as connection:
            LocalDevelopmentPolicyAuthority(
                connection,
                key_ref="local-policy-key",
                signing_key=self.signing_key,
            ).publish(body)


class _NativeInterpreter:
    def interpret(self, question: AnswerQuestion) -> AnswerIntentCandidate:
        if question.tenant_id != TENANT:
            raise ValueError("native answer interpreter received another tenant")
        return AnswerIntentCandidate(
            intent_kind="metric_value",
            metric_refs=("total_revenue",),
            dimension_refs=("region",),
            filters=(),
            time_window=None,
            ordering=(),
            row_limit=10,
        )


class _ThreadSafeFreshnessReader:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read_for_generation(
        self, *, tenant_id: str, input_generation_digest: str
    ) -> SourceFreshnessObservation | None:
        repository = SQLiteSourceFreshnessObservationRepository(str(self._path))
        try:
            return repository.read_for_generation(
                tenant_id=tenant_id,
                input_generation_digest=input_generation_digest,
            )
        finally:
            repository.close()


class _ThreadSafeMetadataReader:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read_current(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ApprovedProductVersionMetadata | None:
        repository = SQLiteApprovedProductVersionRepository(str(self._path))
        try:
            return repository.read_current(
                tenant_id=tenant_id,
                product_ref=product_ref,
                generation=generation,
            )
        finally:
            repository.close()


class _ThreadSafeQueryBindingReader:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read_current(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ApprovedProductQueryBinding | None:
        repository = SQLiteProductQueryBindingRepository(str(self._path))
        try:
            return repository.read_current(
                tenant_id=tenant_id,
                product_ref=product_ref,
                generation=generation,
            )
        finally:
            repository.close()


@dataclass(frozen=True, slots=True)
class _StaticCatalogBindingAuthority:
    binding: CatalogBinding

    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding:
        if tenant_id != self.binding.tenant_id or binding_id != self.binding.binding_id:
            raise KeyError((tenant_id, binding_id))
        return self.binding

    def current_binding(self, tenant_id: str) -> CatalogBinding | None:
        return self.binding if tenant_id == self.binding.tenant_id else None


@dataclass(frozen=True, slots=True)
class _StaticWarehouseBindingAuthority:
    binding: WarehouseBinding

    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None:
        if tenant_id != self.binding.tenant_id or binding_id != self.binding.binding_id:
            return None
        return self.binding

    def current_binding(self, tenant_id: str) -> WarehouseBinding | None:
        return self.binding if tenant_id == self.binding.tenant_id else None


@dataclass(frozen=True, slots=True)
class _ObservedCatalogSearchHealth:
    tenant_id: str

    def search_ready(self, tenant_id: str) -> bool:
        return tenant_id == self.tenant_id


class _NativeProductCatalogProvider:
    """Exact local provider double for the acceptance fixture's publication transaction."""

    provider_kind: Literal["openmetadata"] = "openmetadata"

    def __init__(self) -> None:
        self._product: CatalogProductDefinition | None = None
        self._table: CatalogNativeTableDefinition | None = None

    def publish(self, definition: CatalogProductDefinition) -> CatalogProductObservation:
        self._product = definition
        return self._product_observation(definition)

    def observe(self, *, tenant_id: str, stable_external_key: str) -> CatalogProductObservation:
        definition = self._product
        if (
            definition is None
            or definition.tenant_id != tenant_id
            or definition.stable_external_key != stable_external_key
        ):
            raise KeyError((tenant_id, stable_external_key))
        return self._product_observation(definition)

    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        self._table = definition
        return self._table_observation(definition)

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation:
        if self._table != definition:
            raise KeyError(definition.warehouse.table_name)
        return self._table_observation(definition)

    @staticmethod
    def _product_observation(definition: CatalogProductDefinition) -> CatalogProductObservation:
        return CatalogProductObservation(
            tenant_id=definition.tenant_id,
            stable_external_key=definition.stable_external_key,
            definition=definition,
            definition_digest=digest(definition),
            provider_version="native-acceptance-v1",
        )

    @staticmethod
    def _table_observation(
        definition: CatalogNativeTableDefinition,
    ) -> CatalogNativeTableObservation:
        warehouse = definition.warehouse
        return CatalogNativeTableObservation(
            definition=definition,
            definition_digest=digest(definition),
            table_fully_qualified_name=".".join(
                (
                    warehouse.database_service_name,
                    warehouse.database_name,
                    warehouse.schema_name,
                    warehouse.table_name,
                )
            ),
            provider_version="native-acceptance-v1",
        )


@contextmanager
def signed_https_policy_authority(
    root: Path,
    *,
    body: SignedEntitlementBody,
) -> Iterator[_NativeSignedPolicyAuthority]:
    root.mkdir(parents=True, exist_ok=True)
    tls_key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local policy test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=24))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(tls_key, algorithm=None)
    )
    certificate_path = root / "policy-tls.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path = root / "policy-tls-key.pem"
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate_path, key_path)
    signing_key = Ed25519PrivateKey.generate()
    database_path = root / "policy-authority.sqlite3"
    with sqlite3.connect(database_path) as connection:
        LocalDevelopmentPolicyAuthority(
            connection,
            key_ref="local-policy-key",
            signing_key=signing_key,
        ).publish(body)
    bearer = SecretStr(secrets.token_urlsafe(32))
    server = create_local_policy_server(
        database_path=database_path,
        signing_key=signing_key,
        key_ref="local-policy-key",
        bearer_credential=bearer,
        tls_context=tls_context,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = SignedHttpPolicyAuthoritySettings.model_validate(
        {
            "endpoint": f"https://127.0.0.1:{server.server_port}/entitlements/current",
            "tls_ca_bundle_path": certificate_path,
            "bearer_credential": bearer,
            "signing_key_ref": "local-policy-key",
            "signing_public_key_pem": signing_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "connected_authority_ref": "local-policy",
            "connection_binding_ref": "local-policy-binding",
            "adapter_ref": "signed-http-local-test",
            "timeout_seconds": 2.0,
        }
    )
    with httpx.Client(verify=ssl.create_default_context(cafile=str(certificate_path))) as client:
        try:
            yield _NativeSignedPolicyAuthority(
                reader=SignedHttpConnectedPolicyAuthority(settings=settings, http_client=client),
                database_path=database_path,
                signing_key=signing_key,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("local policy authority did not stop")


async def _land_rows_for_console_tenant(
    dsn: str,
    rows: tuple[bytes, ...],
    *,
    ledger: GenerationLedger,
) -> LandingResult:
    segment = StagedSegment(
        segment_digest=staged_segment_digest(rows),
        schema_digest="3" * 64,
        record_count=len(rows),
        rows=rows,
    )
    target = RawGenerationTarget(
        tenant_id=TENANT,
        contract_ref=CONTRACT.contract_id,
        contract_revision=CONTRACT.version,
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
        contract_digest=digest(CONTRACT),
        source_binding_ref="source-live-a",
        consumer_ref="destination-live-a",
    )


def _signed_product_model(
    private_key: Ed25519PrivateKey,
    *,
    input_generation_digest: str,
    generation_id: str,
    target_schema: str,
) -> SignedCompiledDbtModel:
    region = ColumnReference(relation_alias="revenue_events", column_name="region")
    revenue = ColumnReference(relation_alias="revenue_events", column_name="revenue")
    product_iir = ProductIntentIR(
        product_ref="revenue_by_region",
        source=SourceRelation(
            relation_namespace="logical",
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
    source = GenerationScopedProductSource(
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
    )
    emitted = emit_generation_scoped_postgresql(product_iir, source)
    model = CompiledDbtModel(
        model_name="product_revenue_v1_g1",
        contract_digest=digest(CONTRACT),
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
        compiled_sql=emitted.statement,
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
) -> ProductPhysicalPlan:
    return ProductPhysicalPlan(
        compiler_version="compiler-live-1",
        legality_rule_id="R-PRODUCT-AGGREGATE",
        legality_rule_version="1",
        tenant_id=TENANT,
        product_id=PRODUCT.artifact_id,
        product_revision=PRODUCT.version,
        contract_ref=CONTRACT.contract_id,
        contract_revision=CONTRACT.version,
        contract_digest=digest(CONTRACT),
        iir_digest="8" * 64,
        provider="postgresql",
        warehouse_binding_id=CONTRACT.destination_product.warehouse_binding_id,
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


def _replace_source_rows(*, bootstrap_dsn: str, source_updated_at: datetime) -> None:
    with psycopg.connect(bootstrap_dsn) as connection:
        connection.execute("TRUNCATE source_data.sales")
        connection.execute(
            "INSERT INTO source_data.sales VALUES "
            "(1, 'west', 101, 10.00, %s), (2, 'west', 102, 20.00, %s), "
            "(3, 'east', 103, 99.00, %s)",
            (source_updated_at, source_updated_at, source_updated_at),
        )


@contextmanager
def fresh_native_materialized_product(
    root: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    product_publications: SQLiteProductCatalogPublicationRepository,
    catalog_binding: CatalogBinding | None = None,
    catalog_provider: ProductCatalogPublicationProvider | None = None,
) -> Iterator[NativeMaterializedProduct]:
    """Create one measured source-to-product generation and keep its readers alive."""

    root.mkdir(parents=True, exist_ok=True)
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")
    with _fresh_postgresql_cluster(root) as bootstrap_dsn:
        acquisition_password = secrets.token_urlsafe(24)
        landing_password = secrets.token_urlsafe(24)
        materialization_password = secrets.token_urlsafe(24)
        runtime_password = secrets.token_urlsafe(24)
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=acquisition_password,
            landing_password=landing_password,
            materialization_password=materialization_password,
        )
        _replace_source_rows(
            bootstrap_dsn=bootstrap_dsn,
            source_updated_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        staged_rows, watermark_at, observed_at = _acquire_rows(
            _role_dsn(bootstrap_dsn, "acquisition_runtime", acquisition_password),
            tenant_id=TENANT,
        )
        generation_ledger = GenerationLedger.in_memory()
        landing = asyncio.run(
            _land_rows_for_console_tenant(
                _role_dsn(bootstrap_dsn, "landing_runtime", landing_password),
                staged_rows,
                ledger=generation_ledger,
            )
        )
        input_generation_digest = digest(landing.receipt)
        freshness = SQLiteSourceFreshnessObservationRepository(
            str(root / "source-freshness.sqlite3")
        )
        freshness_observation = SourceFreshnessObservation(
            observation_id=digest(
                {
                    "domain": "heinzel-source-freshness-v1",
                    "source_ref": "source-live-a",
                    "input_generation_digest": input_generation_digest,
                }
            ),
            tenant_id=TENANT,
            version=1,
            source_ref="source-live-a",
            input_generation_digest=input_generation_digest,
            data_observation_ref=ArtifactReference(
                artifact_id=landing.receipt.generation_id,
                version=1,
                digest=digest(landing.receipt),
            ),
            watermark_at=watermark_at,
            observed_at=observed_at,
        )
        freshness.store(freshness_observation)
        contract_digest = digest(CONTRACT)
        target_schema = "contract_" + contract_digest[:54]
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION materialization_runtime").format(
                    sql.Identifier(target_schema)
                )
            )
        profiles = root / "dbt-profiles"
        _write_dbt_profile(profiles, bootstrap_dsn, target_schema=target_schema)
        monkeypatch.setenv("HEINZEL_DBT_TEST_PASSWORD", materialization_password)
        compiler_key = Ed25519PrivateKey.generate()
        signed_model = _signed_product_model(
            compiler_key,
            input_generation_digest=input_generation_digest,
            generation_id=landing.receipt.generation_id,
            target_schema=target_schema,
        )
        schema_digest = postgresql_materialized_schema_digest(
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
            expected_output_schema_digest=schema_digest,
        )
        cardinality_evidence = ProductInputCardinalityResolver(
            ledger=generation_ledger,
            clock=lambda: datetime.now(UTC),
            authority_ref="runtime-generation-ledger-v1",
        ).resolve(
            tenant_id=TENANT,
            contract_ref=CONTRACT.contract_id,
            contract_revision=CONTRACT.version,
            contract_digest=contract_digest,
            product_plan_digest=digest(physical_plan),
            relation_ref="raw_sales",
            generations=(
                ProductInputGenerationExpectation(
                    generation_id=landing.receipt.generation_id,
                    receipt_digest=input_generation_digest,
                ),
            ),
            policy_maximum_contributing_rows=landing.receipt.record_count,
        )
        cardinality_connection = sqlite3.connect(
            root / "product-input-cardinality.sqlite3", check_same_thread=False
        )
        cardinality_repository = SQLiteProductInputCardinalityEvidenceRepository(
            cardinality_connection
        )
        cardinality_evidence_digest = cardinality_repository.record(cardinality_evidence)
        materialization_settings = PostgreSQLMaterializationSettings(
            tenant_id=TENANT,
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
            settings=materialization_settings,
            signed_model=signed_model,
            invoker=DbtInvoker(
                trusted_compiler_keys={"compiler-live-1": compiler_key.public_key()},
                runner=SubprocessDbtRunner(
                    DbtSubprocessSettings(
                        executable=Path(dbt_executable),
                        profiles_directory=profiles,
                        workspace_directory=root,
                        timeout_seconds=180,
                        credential_environment_names=("HEINZEL_DBT_TEST_PASSWORD",),
                    )
                ),
            ),
        )
        query_binding_path = root / "approved-query-bindings.sqlite3"
        query_bindings = SQLiteProductQueryBindingRepository(str(query_binding_path))
        metadata = SQLiteApprovedProductVersionRepository(str(root / "approved-products.sqlite3"))
        declaration = ProductQueryBindingDeclaration(
            engine_kind="postgresql",
            namespace=signed_model.model.target_schema,
            relation_name=signed_model.model.model_name,
            metric_bindings=(
                ProductQueryMetricBinding(
                    semantic_ref=METRIC,
                    aggregate="sum",
                    column_name="total_revenue",
                    output_name="total_revenue",
                ),
            ),
            dimension_bindings=(
                ProductQueryDimensionBinding(
                    semantic_ref=DIMENSION,
                    semantic_kind="entity",
                    column_name="region",
                    output_name="region",
                ),
            ),
            disclosure_entity_ref=DIMENSION,
            disclosure_entity_column="region",
        )
        approval = ProductQueryBindingApproval(
            approval_id="query-binding-approval-live-1",
            tenant_id=TENANT,
            product_ref=PRODUCT,
            generation=1,
            declaration_digest=digest(declaration),
            authority_ref="owner:product-revenue",
            actor_id="product-owner-live-1",
            decision="approve",
            created_at=datetime.now(UTC),
        )
        if (catalog_binding is None) != (catalog_provider is None):
            raise ValueError("catalog binding and provider must be supplied together")
        active_catalog_binding = catalog_binding or CatalogBinding(
            binding_id="catalog-native-a",
            tenant_id=TENANT,
            capability_profile_digest="a" * 64,
            lifecycle_state=CatalogBindingState.READY,
            revision=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            provisioned_at=datetime.now(UTC),
        )
        active_catalog_provider = catalog_provider or _NativeProductCatalogProvider()
        warehouse_binding = WarehouseBinding(
            binding_id=CONTRACT.destination_product.warehouse_binding_id,
            tenant_id=TENANT,
            engine_kind=EngineKind.POSTGRESQL,
            region="local",
            capability_profile_digest="b" * 64,
            lifecycle_state=WarehouseBindingState.READY,
            revision=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            provisioned_at=datetime.now(UTC),
        )
        database_name = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)["dbname"]
        if not isinstance(database_name, str):
            raise ValueError("native PostgreSQL database name is unavailable")
        authoritative_catalog = compose_authoritative_product_catalog(
            config=ProductCatalogCompositionConfig(
                catalog_binding_id=active_catalog_binding.binding_id,
                database_name=database_name,
            ),
            warehouse_bindings=_StaticWarehouseBindingAuthority(warehouse_binding),
            catalog_bindings=_StaticCatalogBindingAuthority(active_catalog_binding),
            publication_repository=product_publications,
            publication_provider=active_catalog_provider,
            product_version_repository=metadata,
            query_binding_repository=query_bindings,
            contract=CONTRACT,
            semantic_version=SEMANTIC_VERSION,
            definition_authority=ProductCatalogDefinitionAuthority(
                name="Current revenue by region",
                description="Approved revenue grouped by reporting region.",
                owner_refs=("owner:product-revenue",),
                namespace=declaration.namespace,
                relation_name=declaration.relation_name,
                columns=(
                    ProductCatalogColumnAuthority(
                        name="region",
                        type_name="TEXT",
                        nullable=True,
                        description="The approved reporting region.",
                    ),
                    ProductCatalogColumnAuthority(
                        name="total_revenue",
                        type_name="NUMERIC",
                        nullable=True,
                        description="The approved total revenue measure.",
                    ),
                ),
            ),
            query_binding_declaration=declaration,
            query_binding_approvals=(approval,),
            source_freshness_observations=(freshness_observation,),
            clock=lambda: datetime.now(UTC),
        )
        ledger_connection = sqlite3.connect(
            root / "materializations.sqlite3", check_same_thread=False
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
        materializations = ProductMaterializationRunner(
            ledger_connection,
            warehouse=warehouse,
            catalog=authoritative_catalog,
            cardinality_evidence_reader=cardinality_repository,
            execution_authorization_verifier=ProductExecutionAuthorizationVerifier(
                {"runtime-execution-live-1": execution_signer.public_key}
            ),
            clock=lambda: datetime.now(UTC),
        )
        result = materializations.materialize(
            MaterializationRequest(
                run_id="run-live-1",
                tenant_id=TENANT,
                product_id=PRODUCT.artifact_id,
                product_revision=PRODUCT.version,
                product_generation=1,
                retention_seconds=3600,
                contract_digest=contract_digest,
                physical_plan=physical_plan,
                physical_plan_digest=digest(physical_plan),
                compiled_model_digest=signed_model.model_digest,
                input_generation_digests=(input_generation_digest,),
                input_cardinality_evidence_digest=cardinality_evidence_digest,
                expected_output_schema_digest=schema_digest,
            ),
            admission=admission,
        )
        if result.receipt.quality_disposition != "passed":
            raise AssertionError("native materialization did not establish passing quality")
        if result.publication_pending or result.publication_ref is None:
            raise AssertionError("native materialization did not publish its product authority")
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                "CREATE ROLE retained_generation_owner NOLOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
            connection.execute(
                sql.SQL("ALTER TABLE {}.{} OWNER TO retained_generation_owner").format(
                    sql.Identifier(signed_model.model.target_schema),
                    sql.Identifier(signed_model.model.model_name),
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO materialization_runtime").format(
                    sql.Identifier(signed_model.model.target_schema),
                    sql.Identifier(signed_model.model.model_name),
                )
            )
        product_metadata = metadata.read_current(
            tenant_id=TENANT,
            product_ref=PRODUCT,
            generation=result.receipt.product_generation,
        )
        query_binding = query_bindings.read_current(
            tenant_id=TENANT,
            product_ref=PRODUCT,
            generation=result.receipt.product_generation,
        )
        if product_metadata is None or query_binding is None:
            raise AssertionError("native publication did not persist product authority")
        query_bindings.close()
        with psycopg.connect(bootstrap_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE ROLE answer_runtime LOGIN PASSWORD {}").format(
                    sql.Literal(runtime_password)
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO answer_runtime").format(
                    sql.Identifier(query_binding.namespace)
                )
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO answer_runtime").format(
                    sql.Identifier(query_binding.namespace),
                    sql.Identifier(query_binding.relation_name),
                )
            )
        runtime_dsn = _role_dsn(bootstrap_dsn, "answer_runtime", runtime_password)
        try:
            yield NativeMaterializedProduct(
                runtime_dsn=runtime_dsn,
                query_binding=query_binding,
                answer_generation_authority=PostgreSQLAnswerGenerationAuthority(
                    query_bindings=_ThreadSafeQueryBindingReader(query_binding_path),
                    materializations=materializations,
                    signed_model=signed_model,
                    trusted_compiler_keys={"compiler-live-1": compiler_key.public_key()},
                ),
                receipt_reader=materializations,
                freshness_reader=_ThreadSafeFreshnessReader(root / "source-freshness.sqlite3"),
                metadata_reader=_ThreadSafeMetadataReader(root / "approved-products.sqlite3"),
                generation_reader=PostgreSQLProductGenerationAuthority(materialization_settings),
                warehouse_binding=warehouse_binding,
                catalog_binding=active_catalog_binding,
            )
        finally:
            metadata.close()
            freshness.close()
            ledger_connection.close()
            cardinality_connection.close()


@contextmanager
def fresh_native_answer_deployment(
    root: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    permissions: tuple[Literal["dashboard", "download", "query", "view"], ...] = (
        "download",
        "query",
        "view",
    ),
    catalog_binding: CatalogBinding | None = None,
    catalog_provider: ProductCatalogPublicationProvider | None = None,
) -> Iterator[NativeGovernedAnswer]:
    """Deliver one governed answer and keep every authority boundary available to consumers."""

    root.mkdir(parents=True, exist_ok=True)
    purpose = "Review current product revenue"
    now = datetime.now(UTC)
    dimension = DIMENSION
    entitlement_claims = {
        "schema_version": "1",
        "tenant_id": TENANT,
        "principal_ref": REQUESTER_PRINCIPAL,
        "purpose_digest": digest(purpose),
        "decision": "active",
        "product_version_refs": (PRODUCT,),
        "semantic_refs": (SEMANTIC, METRIC, dimension),
        "filter_domains": (),
        "permissions": permissions,
        "effective_at": now - timedelta(minutes=5),
        "valid_until": now + timedelta(hours=24),
        "source_revision": 1,
    }
    entitlement = SignedEntitlementBody.model_validate(
        entitlement_claims
        | {
            "source_payload_digest": SignedEntitlementBody.compute_source_payload_digest(
                entitlement_claims
            )
        }
    )
    with (
        closing(
            SQLiteProductCatalogPublicationRepository(
                str(root / "product-catalog-publications.sqlite3"), check_same_thread=False
            )
        ) as product_publications,
        closing(
            SQLiteDashboardRepository(str(root / "dashboard-publications.sqlite3"))
        ) as dashboard_repository,
        closing(
            SQLiteDashboardContractRepository(str(root / "dashboard-contracts.sqlite3"))
        ) as dashboard_contracts,
        closing(
            SQLiteDashboardConnectionRepository(str(root / "dashboard-connections.sqlite3"))
        ) as dashboard_connections,
        fresh_native_materialized_product(
            root / "product",
            monkeypatch=monkeypatch,
            product_publications=product_publications,
            catalog_binding=catalog_binding,
            catalog_provider=catalog_provider,
        ) as product,
        signed_https_policy_authority(root / "policy", body=entitlement) as policy_authority,
    ):
        dashboard_control = DashboardControlService(
            dashboard_repository,
            _NativeDashboardProvider(),
            clock=lambda: datetime.now(UTC),
        )
        signing_key = Ed25519PrivateKey.generate()
        provider = PostgreSQLAnswerQueryProvider(
            settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(product.runtime_dsn)),
            generation_authority=product.answer_generation_authority,
        )
        deployment = GovernedConsoleDeployment(
            root / "console",
            answer_runtime_configuration=GovernedAnswerRuntimeConfiguration(
                connected_authority=policy_authority.reader,
                connected_authority_ref="local-policy",
                interpreter=_NativeInterpreter(),
                materializations=product.receipt_reader,
                freshness=product.freshness_reader,
                product_metadata=product.metadata_reader,
                generations=product.generation_reader,
                signature_verifier=QueryPlanVerifier({"compiler-live-1": signing_key.public_key()}),
                provider_resolver=lambda engine_kind: provider,
                clock=lambda: datetime.now(UTC),
                sleeper=lambda delay: None,
                intent_identifier=lambda: "intent-native-1",
                validation_identifier=lambda: "validation-native-1",
                admission_identifier=lambda: "admission-native-1",
            ),
            product_publications=product_publications,
            warehouse_binding_reader=_StaticWarehouseBindingAuthority(product.warehouse_binding),
            catalog_binding_reader=_StaticCatalogBindingAuthority(product.catalog_binding),
            catalog_search_health=_ObservedCatalogSearchHealth(TENANT),
            dashboards=dashboard_control,
        )
        try:
            seeded = deployment.seed()
            if seeded.data_product_ref != PRODUCT.artifact_id:
                raise AssertionError("native policy did not permit the published data product")
            runtime = deployment.answer_runtime
            if runtime is None:
                raise AssertionError("native answer runtime was not composed")
            request = deployment.requests.submit_question(
                tenant_id=TENANT,
                requester_id=REQUESTER,
                purpose=purpose,
                question="What is current revenue by region?",
                title="Current revenue by region",
            )
            investigating = deployment.requests.transition(
                TENANT,
                request.request_id,
                RequestState.INVESTIGATING,
                actor_id="architect-a",
                expected_revision=request.revision,
            )
            policy_draft = AnswerScopePolicyDraft(
                policy_id="answer-policy-native-1",
                tenant_id=TENANT,
                revision=1,
                principal_scope=(REQUESTER_PRINCIPAL,),
                purposes=(purpose,),
                semantic_version_ref=SEMANTIC,
                data_product_version_refs=(PRODUCT,),
                metric_version_refs=(METRIC,),
                dimension_refs=(dimension,),
                filter_domains=(),
                max_time_window=86_400,
                max_staleness=3_600,
                quality_disposition="block",
                disclosure_classifications=(),
                disclosure_entity="region",
                minimum_group_size=1,
                row_ceiling=10,
                byte_ceiling=10_000,
                scan_ceiling=100_000,
                period_scan_budget=1_000_000,
                statement_timeout=15,
                result_retention=3_600,
                agent_access="denied",
                model_disclosure="metadata",
                valid_from=now - timedelta(minutes=5),
                valid_until=now + timedelta(hours=24),
                created_at=now - timedelta(minutes=5),
            )
            authority_refs = (
                "role:data_engineering_architect",
                "owner:product-revenue",
            )
            approvals = tuple(
                AnswerScopePolicyApproval(
                    approval_id=f"native-approval-{position}",
                    tenant_id=TENANT,
                    policy_id=policy_draft.policy_id,
                    policy_revision=policy_draft.revision,
                    policy_digest=digest(policy_draft),
                    authority_ref=authority_ref,
                    actor_id=f"native-actor-{position}",
                    decision="approve",
                    created_at=now,
                )
                for position, authority_ref in enumerate(authority_refs, start=1)
            )
            policy = AnswerScopePolicyLifecycle(runtime.policies, clock=lambda: now).activate(
                draft=policy_draft,
                approvals=approvals,
                product_owner_bindings=(
                    ProductOwnerAuthority(
                        data_product_version_ref=PRODUCT,
                        authority_ref="owner:product-revenue",
                    ),
                ),
                period_scan_budget_threshold=2_000_000,
            )
            snapshot = runtime.entitlements.resolve_current(
                tenant_id=TENANT,
                principal_ref=REQUESTER_PRINCIPAL,
                purpose_digest=digest(purpose),
            )
            validated = runtime.questions.interpret_and_validate(
                question=AnswerQuestion(
                    tenant_id=TENANT,
                    request_id=request.request_id,
                    request_revision=investigating.revision,
                    question_digest=digest(request.payload),
                    interpreter="form",
                    interpreter_ref="answer-form-v1",
                ),
                policy=policy,
                context=AnswerValidationContext(
                    semantic_version_digest=SEMANTIC.digest,
                    entitlement_snapshot_digest=snapshot.snapshot_digest,
                    bindings=(
                        BoundSemanticReference(
                            canonical_ref="total_revenue",
                            kind="metric",
                            aliases=(),
                            version_ref=METRIC,
                            product_version_ref=PRODUCT,
                        ),
                        BoundSemanticReference(
                            canonical_ref="region",
                            kind="dimension",
                            aliases=(),
                            version_ref=dimension,
                            product_version_ref=PRODUCT,
                        ),
                    ),
                    entitled_refs=("region", "total_revenue"),
                    answer_enabled_product_refs=(PRODUCT,),
                    product_generation_refs=(
                        RequestProductGenerationReference(product_ref=PRODUCT, generation=1),
                    ),
                    product_staleness=0,
                    quality_blocked=False,
                    authority_conflict=False,
                    requester_principal_ref=REQUESTER_PRINCIPAL,
                    purpose=purpose,
                    acting_as_agent=False,
                    latest_policy_revision=policy.revision,
                ),
            )
            signer = QueryPlanSigner("compiler-live-1", signing_key)
            compiled = compile_governed_query(
                GovernedQueryInput(
                    tenant_id=TENANT,
                    validation_digest=digest(validated.validation),
                    intent_kind="metric_value",
                    engine_kind="postgresql",
                    consumption_object=QueryConsumptionObject(
                        object_ref=QueryReference.model_validate(
                            product.query_binding.consumption_object_ref.model_dump(mode="python"),
                            strict=True,
                        ),
                        namespace=product.query_binding.namespace,
                        relation_name=product.query_binding.relation_name,
                    ),
                    product_generation_refs=(
                        ProductGenerationReference(
                            product_ref=QueryReference.model_validate(
                                PRODUCT.model_dump(mode="python")
                            ),
                            generation=1,
                        ),
                    ),
                    metrics=(
                        QueryMetric(
                            metric_ref=QueryReference.model_validate(
                                METRIC.model_dump(mode="python")
                            ),
                            aggregate="sum",
                            column_name="total_revenue",
                            output_name="total_revenue",
                        ),
                    ),
                    dimensions=(
                        QueryDimension(
                            dimension_ref=QueryReference.model_validate(
                                dimension.model_dump(mode="python")
                            ),
                            column_name="region",
                            output_name="region",
                        ),
                    ),
                    filters=(),
                    time_window=None,
                    ordering=(QueryOrder(output_name="region", direction="ascending"),),
                    row_limit=10,
                    disclosure_entity_column="region",
                    minimum_group_size=1,
                    estimated_scan=QueryScanEstimate(
                        rows=2,
                        bytes=256,
                        estimator_version="native-fixture-v1",
                    ),
                    period_scan_consumed=QueryScan(rows=0, bytes=0),
                    ceilings=QueryCeilings(
                        row_limit=10,
                        scan=QueryScan(rows=100, bytes=10_000),
                        period_scan=QueryScan(rows=1_000, bytes=100_000),
                    ),
                ),
                signer=signer,
            )
            if not isinstance(compiled, GovernedQueryPlan):
                raise AssertionError("native governed query did not compile")
            plan = runtime.plans.save(compiled)
            admission = runtime.policy_admissions.admit(
                intent=validated.intent,
                validation=validated.validation,
                policy=policy,
                plan=plan,
                restatement_acceptance_ref=None,
                current_entitlement_snapshot_digest=snapshot.snapshot_digest,
                latest_policy_revision=policy.revision,
                actor_id="system:answer-policy",
            )
            if admission.receipt is None:
                raise AssertionError("native query did not receive policy admission")
            runtime.execute_answer(
                tenant_id=TENANT,
                request_id=request.request_id,
                actor_id="heinzel-runtime",
                expected_revision=admission.request.revision,
            )
            delivered = deployment.requests.get(TENANT, request.request_id)
            if delivered.state is not RequestState.DELIVERED:
                raise AssertionError("native answer did not reach delivered")
            governed_answer = runtime.answers.read_for_request(
                TENANT, REQUESTER, request.request_id
            )
            dashboard_signing_key = Ed25519PrivateKey.generate()
            dashboard_contract = DashboardContract(
                dashboard_id="dashboard-revenue",
                version=1,
                owner="owner:product-revenue",
                audience=(REQUESTER_PRINCIPAL,),
                data_product_versions=(PRODUCT,),
                metric_versions=(METRIC,),
                dimensions=(dimension,),
                filters=(),
                visual_intents=("bar", "table"),
                drill_paths=((dimension,),),
                freshness_requirement=CONTRACT.freshness,
                access_policy=ArtifactReference(
                    artifact_id="access-policy-revenue",
                    version=1,
                    digest=digest({"policy": "native-revenue-dashboard"}),
                ),
                report_delivery_policy=None,
                acceptance_tests=(),
                lifecycle_state="certified",
            )
            dashboard_contracts.store(
                DashboardContractSigner("dashboard-native-1", dashboard_signing_key).sign(
                    tenant_id=TENANT, contract=dashboard_contract
                )
            )
            dashboard_connections.store(
                DashboardDatasetConnectionBinding(
                    tenant_id=TENANT,
                    engine_kind="postgresql",
                    consumption_object_ref=product.query_binding.consumption_object_ref,
                    namespace=product.query_binding.namespace,
                    relation_name=product.query_binding.relation_name,
                    warehouse_binding_id=product.warehouse_binding.binding_id,
                    warehouse_binding_revision=product.warehouse_binding.revision,
                    warehouse_binding_digest=digest(product.warehouse_binding),
                    connection_secret_ref="secret://tenant-a/superset-database",
                )
            )
            DashboardCompositionService(
                contracts=dashboard_contracts,
                contract_verifier=DashboardContractVerifier(
                    {"dashboard-native-1": dashboard_signing_key.public_key()}
                ),
                answers=runtime.dashboard_answers,
                query_bindings=_ThreadSafeQueryBindingReader(
                    root / "product" / "approved-query-bindings.sqlite3"
                ),
                materializations=product.receipt_reader,
                product_publications=product_publications,
                connections=dashboard_connections,
                dashboard_control=dashboard_control,
                clock=lambda: datetime.now(UTC),
            ).publish(
                PublishDashboardCommand(
                    tenant_id=TENANT,
                    dashboard_id=dashboard_contract.dashboard_id,
                    dashboard_version=dashboard_contract.version,
                    request_id=request.request_id,
                    answer_id=governed_answer.answer_id,
                    expected_revision=1,
                )
            )
            revoked_claims = entitlement.model_dump(
                mode="python", exclude={"source_payload_digest"}
            ) | {
                "decision": "revoked",
                "product_version_refs": (),
                "semantic_refs": (),
                "filter_domains": (),
                "permissions": (),
                "source_revision": entitlement.source_revision + 1,
            }
            revoked_entitlement = SignedEntitlementBody.model_validate(
                revoked_claims
                | {
                    "source_payload_digest": SignedEntitlementBody.compute_source_payload_digest(
                        revoked_claims
                    )
                }
            )
            yield NativeGovernedAnswer(
                deployment=deployment,
                product_publications=product_publications,
                request_id=request.request_id,
                _revoke=lambda: policy_authority.publish(revoked_entitlement),
            )
        finally:
            deployment.close()
