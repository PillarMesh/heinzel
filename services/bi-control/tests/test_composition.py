from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pillarmesh_bi_control import (
    DashboardAnswerAuthority,
    DashboardCompositionError,
    DashboardCompositionService,
    DashboardConnectionAuthorityError,
    DashboardContract,
    DashboardContractSigner,
    DashboardContractVerifier,
    DashboardDatasetConnectionBinding,
    DashboardNoValidPlan,
    DashboardProductGenerationReference,
    DashboardStaleRevision,
    PublishDashboardCommand,
    SQLiteDashboardConnectionRepository,
    SQLiteDashboardContractRepository,
    SQLiteDashboardRepository,
)
from pillarmesh_contract_model import (
    ArtifactReference,
    FreshnessRequirement,
    canonical_bytes,
    digest,
)
from pillarmesh_provider_sdk.bi import BiApplyResult, BiDashboardDefinition
from pillarmesh_request_management import (
    AnswerAdmissionEvidence,
    AnswerExecutionEvidence,
    AnswerQueryColumnEvidence,
    AnswerResultEvidence,
    GovernedAnswer,
    GovernedAnswerVerificationError,
    InboxRequest,
    RequestManagementDashboardAnswerAuthorityReader,
    RequestState,
    StakeholderQuestion,
)
from pillarmesh_runtime import MaterializationAuthorityError, ProductMaterializationReceipt
from pillarmesh_semantic_registry import (
    ApprovedProductQueryBinding,
    ProductCatalogPublicationAuthorityError,
    ProductCatalogPublicationReceipt,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityError,
    ProductQueryBindingDeclaration,
    ProductQueryDimensionBinding,
    ProductQueryMetricBinding,
    SQLiteProductQueryBindingRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
type FreshnessDisposition = Literal["current", "stale", "unknown", "not_applicable"]


def _ref(artifact_id: str, *, version: int = 1, marker: str = "a") -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest=marker * 64)


def _contract(*, lifecycle_state: str = "certified", products: int = 1) -> DashboardContract:
    dimension = _ref("dimension:region")
    return DashboardContract.model_validate(
        {
            "dashboard_id": "dashboard:revenue",
            "version": 1,
            "owner": "principal:finance-owner",
            "audience": ("group:finance",),
            "data_product_versions": tuple(
                _ref(f"product:orders-{index}") for index in range(products)
            ),
            "metric_versions": (_ref("metric:revenue"),),
            "dimensions": (dimension,),
            "filters": (_ref("filter:completed"),),
            "visual_intents": ("bar",),
            "drill_paths": ((dimension,),),
            "freshness_requirement": FreshnessRequirement(maximum_age_seconds=3_600),
            "access_policy": _ref("policy:finance"),
            "report_delivery_policy": None,
            "acceptance_tests": (),
            "lifecycle_state": lifecycle_state,
        }
    )


def _materialization(product_ref: ArtifactReference) -> ProductMaterializationReceipt:
    return ProductMaterializationReceipt(
        run_id="materialization-1",
        tenant_id="tenant-a",
        product_id=product_ref.artifact_id,
        product_revision=product_ref.version,
        product_generation=7,
        contract_digest="c" * 64,
        input_generation_digests=("d" * 64,),
        input_cardinality_evidence_digest="5" * 64,
        execution_authorization_digest="6" * 64,
        legality_decision_digest="7" * 64,
        physical_plan_digest="e" * 64,
        compiled_model_digest="9" * 64,
        output_schema_digest="f" * 64,
        output_row_count=2,
        provider_commit_reference="1" * 64,
        dbt_manifest_digest="2" * 64,
        dbt_run_results_digest="3" * 64,
        lineage_digest="4" * 64,
        quality_assertion_count=1,
        quality_disposition="passed",
        committed_at=NOW - timedelta(minutes=5),
        retained_until=NOW + timedelta(hours=1),
    )


def _binding(
    product_ref: ArtifactReference, receipt: ProductMaterializationReceipt
) -> ApprovedProductQueryBinding:
    metric = ProductQueryMetricBinding(
        semantic_ref=_ref("metric:revenue"),
        aggregate="sum",
        column_name="revenue",
        output_name="total_revenue",
    )
    dimension = ProductQueryDimensionBinding(
        semantic_ref=_ref("dimension:region"),
        semantic_kind="entity",
        column_name="region",
        output_name="region",
    )
    declaration = ProductQueryBindingDeclaration(
        engine_kind="postgresql",
        namespace="analytics",
        relation_name="orders_current",
        metric_bindings=(metric,),
        dimension_bindings=(dimension,),
        disclosure_entity_ref=dimension.semantic_ref,
        disclosure_entity_column="region",
    )
    approval = ProductQueryBindingApproval(
        approval_id="approval-1",
        tenant_id="tenant-a",
        product_ref=product_ref,
        generation=7,
        declaration_digest=digest(declaration),
        authority_ref="authority:semantic",
        actor_id="architect-1",
        decision="approve",
        created_at=NOW - timedelta(minutes=3),
    )
    materialization_ref = ArtifactReference(
        artifact_id=receipt.run_id,
        version=receipt.product_generation,
        digest=digest(receipt),
    )
    consumption_ref = ArtifactReference(
        artifact_id=f"{product_ref.artifact_id}:consumption",
        version=7,
        digest=digest(
            {
                "domain": "pillarmesh-product-query-consumption-v1",
                "tenant_id": "tenant-a",
                "product_ref": product_ref,
                "generation": 7,
                "materialization_receipt_ref": materialization_ref,
                "engine_kind": declaration.engine_kind,
                "namespace": declaration.namespace,
                "relation_name": declaration.relation_name,
            }
        ),
    )
    return ApprovedProductQueryBinding(
        tenant_id="tenant-a",
        product_ref=product_ref,
        generation=7,
        contract_ref=_ref("integration-contract:orders", marker="c"),
        semantic_version_ref=_ref("semantic:finance"),
        materialization_receipt_ref=materialization_ref,
        lineage_digest=receipt.lineage_digest,
        declaration_digest=digest(declaration),
        approval_digest=digest((approval,)),
        consumption_object_ref=consumption_ref,
        engine_kind="postgresql",
        namespace=declaration.namespace,
        relation_name=declaration.relation_name,
        metric_bindings=declaration.metric_bindings,
        dimension_bindings=declaration.dimension_bindings,
        disclosure_entity_ref=declaration.disclosure_entity_ref,
        disclosure_entity_column=declaration.disclosure_entity_column,
        approvals=(approval,),
        recorded_at=NOW,
    )


class _Provider:
    provider_kind: Literal["superset"] = "superset"

    def __init__(self) -> None:
        self.definitions: list[BiDashboardDefinition] = []

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        self.definitions.append(definition)
        return BiApplyResult(
            stable_external_key=definition.stable_external_key,
            desired_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url="https://superset.test/dashboard/revenue",
            provider_version="4.1.1",
        )


class _AnswerReader:
    def __init__(self, value: DashboardAnswerAuthority | None) -> None:
        self.value = value

    def read_exact(
        self, *, tenant_id: str, request_id: str, answer_id: str
    ) -> DashboardAnswerAuthority | None:
        del tenant_id, request_id, answer_id
        return self.value


class _BindingReader:
    def __init__(self, value: ApprovedProductQueryBinding | None) -> None:
        self.value = value

    def read_current(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ApprovedProductQueryBinding | None:
        del tenant_id, product_ref, generation
        return self.value


class _MaterializationReader:
    def __init__(self, value: ProductMaterializationReceipt | None) -> None:
        self.value = value

    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None:
        del tenant_id, product_id, product_revision, product_generation
        return self.value


class _ProductPublicationReader:
    def __init__(self, value: ProductCatalogPublicationReceipt | None) -> None:
        self.value = value

    def read_for_product_generation(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductCatalogPublicationReceipt | None:
        del tenant_id, product_ref, generation
        return self.value


class _ConnectionReader:
    def __init__(self, value: DashboardDatasetConnectionBinding | None) -> None:
        self.value = value

    def resolve(
        self,
        *,
        tenant_id: str,
        engine_kind: Literal["postgresql", "clickhouse"],
        consumption_object_ref: ArtifactReference,
    ) -> DashboardDatasetConnectionBinding | None:
        del tenant_id, engine_kind, consumption_object_ref
        return self.value


class _Requests:
    def __init__(self, request: InboxRequest) -> None:
        self.request = request

    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None:
        if self.request.tenant_id != tenant_id or self.request.request_id != request_id:
            return None
        return self.request


class _Answers:
    def __init__(self, answer: GovernedAnswer) -> None:
        self.answer = answer

    def read(self, tenant_id: str, answer_id: str) -> GovernedAnswer | None:
        if self.answer.tenant_id != tenant_id or self.answer.answer_id != answer_id:
            return None
        return self.answer

    def list_for_request(self, tenant_id: str, request_id: str) -> tuple[GovernedAnswer, ...]:
        if self.answer.tenant_id != tenant_id or self.answer.request_id != request_id:
            return ()
        return (self.answer,)


class _Admissions:
    def __init__(self, admission: AnswerAdmissionEvidence) -> None:
        self.admission = admission

    def read_admission(
        self, tenant_id: str, request_id: str, admission_ref: str
    ) -> AnswerAdmissionEvidence | None:
        if (
            self.admission.tenant_id != tenant_id
            or self.admission.request_id != request_id
            or self.admission.admission_ref != admission_ref
        ):
            return None
        return self.admission


class _Executions:
    def __init__(self, receipt: AnswerExecutionEvidence, snapshot: AnswerResultEvidence) -> None:
        self.receipt = receipt
        self.snapshot = snapshot

    def read_receipt(self, *args: object) -> AnswerExecutionEvidence | None:
        del args
        return self.receipt

    def read_result(self, *args: object) -> AnswerResultEvidence | None:
        del args
        return self.snapshot


def _fixture(
    *,
    lifecycle_state: str = "certified",
    products: int = 1,
    answer_as_of: datetime = NOW - timedelta(minutes=5),
    freshness_disposition: FreshnessDisposition = "current",
) -> tuple[DashboardCompositionService, _Provider, PublishDashboardCommand, dict[str, object]]:
    private_key = Ed25519PrivateKey.generate()
    contract = _contract(lifecycle_state=lifecycle_state, products=products)
    contracts = SQLiteDashboardContractRepository(":memory:")
    contracts.store(
        DashboardContractSigner("dashboard-key-1", private_key).sign(
            tenant_id="tenant-a", contract=contract
        )
    )
    product_ref = contract.data_product_versions[0] if products else _ref("product:orders-0")
    receipt = _materialization(product_ref)
    binding = _binding(product_ref, receipt)
    product_publication = ProductCatalogPublicationReceipt(
        publication_id="product-publication-1",
        tenant_id="tenant-a",
        operation_id="8" * 64,
        intent_digest="9" * 64,
        definition_digest="a" * 64,
        provider_kind="openmetadata",
        provider_version="1.13.3",
        product_ref=product_ref,
        generation=7,
        observation_digest="b" * 64,
        native_table_definition_digest="c" * 64,
        native_table_observation_digest="d" * 64,
        table_fully_qualified_name="warehouse.analytics.orders_current",
        round_trip_verified=True,
        published_at=NOW - timedelta(minutes=4),
    )
    answer = DashboardAnswerAuthority(
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=6,
        answer_id="answer-1",
        title="Revenue by region",
        execution_receipt_ref="execution-1",
        result_ref="result-1",
        result_digest="5" * 64,
        product_generation_refs=(
            DashboardProductGenerationReference(product_ref=product_ref, generation=7),
        ),
        metric_version_refs=contract.metric_versions,
        as_of=answer_as_of,
        freshness_disposition=freshness_disposition,
        delivered_at=NOW - timedelta(minutes=1),
    )
    connection = DashboardDatasetConnectionBinding(
        tenant_id="tenant-a",
        engine_kind="postgresql",
        consumption_object_ref=binding.consumption_object_ref,
        namespace=binding.namespace,
        relation_name=binding.relation_name,
        warehouse_binding_id="warehouse-1",
        warehouse_binding_revision=3,
        warehouse_binding_digest="7" * 64,
        connection_secret_ref="secret://tenant-a/superset-database",
    )
    provider = _Provider()
    from pillarmesh_bi_control import DashboardControlService

    dashboard_repository = SQLiteDashboardRepository(":memory:")

    service = DashboardCompositionService(
        contracts=contracts,
        contract_verifier=DashboardContractVerifier({"dashboard-key-1": private_key.public_key()}),
        answers=_AnswerReader(answer),
        query_bindings=_BindingReader(binding),
        materializations=_MaterializationReader(receipt),
        product_publications=_ProductPublicationReader(product_publication),
        connections=_ConnectionReader(connection),
        dashboard_control=DashboardControlService(
            dashboard_repository, provider, clock=lambda: NOW
        ),
        clock=lambda: NOW,
    )
    command = PublishDashboardCommand(
        tenant_id="tenant-a",
        dashboard_id="dashboard:revenue",
        dashboard_version=1,
        request_id="request-1",
        answer_id="answer-1",
        expected_revision=1,
    )
    return (
        service,
        provider,
        command,
        {
            "answer": answer,
            "binding": binding,
            "receipt": receipt,
            "product_publication": product_publication,
            "connection": connection,
            "dashboard_repository": dashboard_repository,
        },
    )


def _replace_authority(
    service: DashboardCompositionService, authority_name: str, value: object | None
) -> None:
    if authority_name == "answer":
        assert value is None or isinstance(value, DashboardAnswerAuthority)
        service._answers = _AnswerReader(value)
    elif authority_name == "binding":
        assert value is None or isinstance(value, ApprovedProductQueryBinding)
        service._query_bindings = _BindingReader(value)
    elif authority_name == "receipt":
        assert value is None or isinstance(value, ProductMaterializationReceipt)
        service._materializations = _MaterializationReader(value)
    elif authority_name == "product_publication":
        assert value is None or isinstance(value, ProductCatalogPublicationReceipt)
        service._product_publications = _ProductPublicationReader(value)
    else:
        assert authority_name == "connection"
        assert value is None or isinstance(value, DashboardDatasetConnectionBinding)
        service._connections = _ConnectionReader(value)


def test_composition_loads_all_provider_values_from_authorities() -> None:
    service, provider, command, authority = _fixture()

    result = service.publish(command)

    assert result.revision == 1
    definition = provider.definitions[0]
    binding = authority["binding"]
    assert isinstance(binding, ApprovedProductQueryBinding)
    assert definition.title == "Revenue by region"
    assert definition.dataset_generation == 7
    assert definition.dataset_namespace == binding.namespace
    assert definition.dataset_relation_name == binding.relation_name
    assert definition.connection_secret_ref == "secret://tenant-a/superset-database"
    assert definition.dataset_stable_key.startswith("pm-dataset-")
    assert "metric:revenue" in definition.metric_refs[0]
    repository = authority["dashboard_repository"]
    answer = authority["answer"]
    assert isinstance(repository, SQLiteDashboardRepository)
    assert isinstance(answer, DashboardAnswerAuthority)
    desired = repository.load_current_desired("tenant-a", "dashboard:revenue", 1)
    assert desired is not None
    assert desired.source_answer == answer
    assert desired.consumption_object_ref == binding.consumption_object_ref
    assert desired.materialization_receipt_ref == binding.materialization_receipt_ref
    product_publication = authority["product_publication"]
    assert isinstance(product_publication, ProductCatalogPublicationReceipt)
    assert desired.product_publication_ref == ArtifactReference(
        artifact_id=product_publication.publication_id,
        version=product_publication.generation,
        digest=digest(product_publication),
    )
    assert desired.warehouse_binding_id == "warehouse-1"
    assert desired.warehouse_binding_digest == "7" * 64
    assert (
        desired.dataset_stable_key
        == desired.model_copy(update={"title": "Changed"}).dataset_stable_key
    )


def test_exact_replay_does_not_repeat_the_provider_effect() -> None:
    service, provider, command, _authority = _fixture()

    first = service.publish(command)
    replay = service.publish(command)

    assert replay == first
    assert len(provider.definitions) == 1


@pytest.mark.parametrize("lifecycle_state", ("draft", "archived"))
def test_only_certified_contracts_can_start_publication(lifecycle_state: str) -> None:
    service, provider, command, _authority = _fixture(lifecycle_state=lifecycle_state)

    with pytest.raises(DashboardNoValidPlan, match="certified"):
        service.publish(command)

    assert provider.definitions == []


def test_multiple_contract_products_are_rejected_before_effects() -> None:
    service, provider, command, _authority = _fixture(products=2)

    with pytest.raises(DashboardNoValidPlan, match="one data product"):
        service.publish(command)

    assert provider.definitions == []


@pytest.mark.parametrize(
    ("authority_name", "replacement", "message"),
    (
        ("answer", None, "answer authority"),
        ("binding", None, "query binding authority"),
        ("receipt", None, "materialization authority"),
        ("product_publication", None, "product publication authority"),
        ("connection", None, "connection authority"),
    ),
)
def test_missing_authority_fails_before_provider_effect(
    authority_name: str, replacement: object | None, message: str
) -> None:
    service, provider, command, _authority = _fixture()
    _replace_authority(service, authority_name, replacement)

    with pytest.raises(DashboardCompositionError, match=message):
        service.publish(command)

    assert provider.definitions == []


def test_product_publication_for_another_generation_fails_before_provider_effect() -> None:
    service, provider, command, authority = _fixture()
    publication = authority["product_publication"]
    assert isinstance(publication, ProductCatalogPublicationReceipt)
    service._product_publications = _ProductPublicationReader(
        publication.model_copy(update={"generation": 8})
    )

    with pytest.raises(DashboardCompositionError, match="publication authority does not match"):
        service.publish(command)

    assert provider.definitions == []


def test_invalid_request_management_answer_evidence_fails_before_provider_effect() -> None:
    service, provider, command, _authority = _fixture()

    class InvalidAnswerReader:
        def read_exact(
            self, *, tenant_id: str, request_id: str, answer_id: str
        ) -> DashboardAnswerAuthority | None:
            del tenant_id, request_id, answer_id
            raise GovernedAnswerVerificationError("result digest mismatch")

    service._answers = InvalidAnswerReader()

    with pytest.raises(DashboardCompositionError, match="answer authority is invalid"):
        service.publish(command)

    assert provider.definitions == []


@pytest.mark.parametrize(
    ("authority_name", "error", "message"),
    (
        (
            "answer",
            GovernedAnswerVerificationError("corrupt"),
            "answer authority is invalid",
        ),
        (
            "binding",
            ProductQueryBindingAuthorityError("corrupt"),
            "query binding authority is invalid",
        ),
        (
            "receipt",
            MaterializationAuthorityError("corrupt"),
            "materialization authority is invalid",
        ),
        (
            "product_publication",
            ProductCatalogPublicationAuthorityError("corrupt"),
            "product publication authority is invalid",
        ),
        (
            "connection",
            DashboardConnectionAuthorityError("corrupt"),
            "connection authority is invalid",
        ),
    ),
)
def test_corrupt_authority_reader_fails_before_provider_effect(
    authority_name: str, error: Exception, message: str
) -> None:
    service, provider, command, _authority = _fixture()

    class CorruptReader:
        def read_exact(self, **kwargs: object) -> None:
            del kwargs
            raise error

        def read_current(self, **kwargs: object) -> None:
            del kwargs
            raise error

        def read_receipt(self, **kwargs: object) -> None:
            del kwargs
            raise error

        def read_for_product_generation(self, **kwargs: object) -> None:
            del kwargs
            raise error

        def resolve(self, **kwargs: object) -> None:
            del kwargs
            raise error

    if authority_name == "answer":
        service._answers = CorruptReader()
    elif authority_name == "binding":
        service._query_bindings = CorruptReader()
    elif authority_name == "receipt":
        service._materializations = CorruptReader()
    elif authority_name == "product_publication":
        service._product_publications = CorruptReader()
    else:
        service._connections = CorruptReader()

    with pytest.raises(DashboardCompositionError, match=message):
        service.publish(command)

    assert provider.definitions == []


@pytest.mark.parametrize("error", (ValueError("programming defect"), AssertionError("defect")))
def test_unexpected_authority_reader_errors_propagate_without_provider_effect(
    error: Exception,
) -> None:
    service, provider, command, _authority = _fixture()

    class DefectiveAnswerReader:
        def read_exact(self, **kwargs: object) -> None:
            del kwargs
            raise error

    service._answers = DefectiveAnswerReader()

    with pytest.raises(type(error), match="defect"):
        service.publish(command)

    assert provider.definitions == []


@pytest.mark.parametrize(
    ("authority_name", "update", "message"),
    (
        ("answer", {"tenant_id": "tenant-b"}, "answer identity"),
        ("answer", {"product_generation_refs": ()}, "one product generation"),
        (
            "answer",
            {
                "product_generation_refs": (
                    DashboardProductGenerationReference(
                        product_ref=_ref("product:other"), generation=7
                    ),
                )
            },
            "product identity",
        ),
        ("answer", {"metric_version_refs": (_ref("metric:other"),)}, "metric"),
        ("binding", {"lineage_digest": "9" * 64}, "lineage"),
        ("connection", {"namespace": "other"}, "connection"),
        ("connection", {"engine_kind": "clickhouse"}, "connection"),
    ),
)
def test_authority_mismatches_fail_before_provider_effect(
    authority_name: str, update: dict[str, object], message: str
) -> None:
    service, provider, command, authority = _fixture()
    original = authority[authority_name]
    assert isinstance(
        original,
        (
            DashboardAnswerAuthority,
            ApprovedProductQueryBinding,
            ProductMaterializationReceipt,
            DashboardDatasetConnectionBinding,
        ),
    )
    _replace_authority(service, authority_name, original.model_copy(update=update))

    with pytest.raises((DashboardCompositionError, DashboardNoValidPlan), match=message):
        service.publish(command)

    assert provider.definitions == []


def test_invalid_contract_signature_is_rejected_before_provider_effect() -> None:
    service, provider, command, _authority = _fixture()
    service._contract_verifier = DashboardContractVerifier({})

    with pytest.raises(DashboardCompositionError, match="contract authority is invalid"):
        service.publish(command)

    assert provider.definitions == []


def test_expired_materialization_is_rejected_before_provider_effect() -> None:
    service, provider, command, authority = _fixture()
    receipt = authority["receipt"]
    binding = authority["binding"]
    assert isinstance(receipt, ProductMaterializationReceipt)
    assert isinstance(binding, ApprovedProductQueryBinding)
    expired = receipt.model_copy(update={"retained_until": NOW})
    replacement_binding = _binding(binding.product_ref, expired)
    service._materializations = _MaterializationReader(expired)
    service._query_bindings = _BindingReader(replacement_binding)
    connection = authority["connection"]
    assert isinstance(connection, DashboardDatasetConnectionBinding)
    service._connections = _ConnectionReader(
        connection.model_copy(
            update={"consumption_object_ref": replacement_binding.consumption_object_ref}
        )
    )

    with pytest.raises(DashboardCompositionError, match="retention"):
        service.publish(command)

    assert provider.definitions == []


@pytest.mark.parametrize("freshness_disposition", ("stale", "unknown", "not_applicable"))
def test_non_current_answer_is_rejected_even_when_materialization_is_new(
    freshness_disposition: FreshnessDisposition,
) -> None:
    service, provider, command, _authority = _fixture(freshness_disposition=freshness_disposition)

    with pytest.raises(DashboardCompositionError, match="freshness"):
        service.publish(command)

    assert provider.definitions == []


def test_stale_answer_as_of_is_rejected_even_when_materialization_is_new() -> None:
    service, provider, command, _authority = _fixture(answer_as_of=NOW - timedelta(hours=2))

    with pytest.raises(DashboardCompositionError, match="freshness"):
        service.publish(command)

    assert provider.definitions == []


def test_future_answer_as_of_is_rejected_before_provider_effect() -> None:
    service, provider, command, _authority = _fixture(answer_as_of=NOW + timedelta(seconds=1))

    with pytest.raises(DashboardCompositionError, match="freshness"):
        service.publish(command)

    assert provider.definitions == []


def test_stale_revision_is_rejected_before_a_second_effect() -> None:
    service, provider, command, authority = _fixture()
    service.publish(command)
    answer = authority["answer"]
    assert isinstance(answer, DashboardAnswerAuthority)
    service._answers = _AnswerReader(answer.model_copy(update={"result_digest": "6" * 64}))

    with pytest.raises(DashboardStaleRevision, match="revision"):
        service.publish(command)

    assert len(provider.definitions) == 1


def _request_owned_answer_reader(
    answer_authority: DashboardAnswerAuthority,
    *,
    request_revision: int = 7,
    request_tenant_id: str = "tenant-a",
) -> RequestManagementDashboardAnswerAuthorityReader:
    columns = (
        AnswerQueryColumnEvidence(name="region", value_type="string"),
        AnswerQueryColumnEvidence(name="revenue", value_type="decimal"),
    )
    rows = (("east", "99.00"), ("west", "30.00"))
    result_digest = digest({"columns": columns, "rows": rows})
    snapshot = AnswerResultEvidence(
        result_ref="result-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest="8" * 64,
        product_generation_refs=answer_authority.product_generation_refs,
        columns=columns,
        rows=rows,
        row_count=2,
        byte_count=len(canonical_bytes(rows)),
        result_schema_digest=digest(columns),
        result_digest=result_digest,
        created_at=NOW - timedelta(minutes=3),
        expires_at=NOW + timedelta(hours=1),
    )
    receipt = AnswerExecutionEvidence(
        receipt_id="receipt-1",
        tenant_id="tenant-a",
        request_id="request-1",
        plan_digest="8" * 64,
        product_generation_refs=answer_authority.product_generation_refs,
        attempt=1,
        started_at=NOW - timedelta(minutes=4),
        completed_at=NOW - timedelta(minutes=2),
        outcome="succeeded",
        row_count=2,
        byte_count=snapshot.byte_count,
        suppressed_group_count=0,
        result_schema_digest=snapshot.result_schema_digest,
        result_digest=result_digest,
        result_ref=snapshot.result_ref,
        freshness_observation_ref="freshness-1",
        quality_observation_ref="quality-1",
    )
    answer = GovernedAnswer(
        answer_id="answer-1",
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=6,
        restatement="Revenue grouped by region.",
        admission_ref="admission-1",
        execution_receipt_ref=receipt.receipt_id,
        metric_version_refs=answer_authority.metric_version_refs,
        product_generation_refs=answer_authority.product_generation_refs,
        as_of=NOW - timedelta(minutes=5),
        freshness_disposition="current",
        material_quality_limitations=(),
        lineage_refs=(_ref("lineage:revenue"),),
        narrative="Two verified rows are available.",
        narrative_source="template",
        result_ref=snapshot.result_ref,
        result_digest=result_digest,
        refreshes_answer_ref=None,
        delivered_at=NOW - timedelta(minutes=1),
    )
    request = InboxRequest(
        title="Revenue by region from request management",
        request_id="request-1",
        tenant_id=request_tenant_id,
        requester_id="requester-a",
        payload=StakeholderQuestion(purpose="Review revenue", question="Revenue by region?"),
        state=RequestState.DELIVERED,
        revision=request_revision,
        submitted_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=1),
    )
    admission = AnswerAdmissionEvidence(
        admission_ref="admission-1",
        admission_kind="policy",
        tenant_id="tenant-a",
        request_id="request-1",
        admission_request_revision=4,
        verifying_request_revision=6,
        validation_digest="9" * 64,
        plan_digest="8" * 64,
        policy_id="policy-1",
        policy_revision=1,
        policy_snapshot_digest="a" * 64,
        entitlement_snapshot_digest="b" * 64,
    )
    return RequestManagementDashboardAnswerAuthorityReader(
        requests=_Requests(request),
        answers=_Answers(answer),
        admissions=_Admissions(admission),
        executions=_Executions(receipt, snapshot),
        clock=lambda: NOW,
    )


def test_request_owned_answer_and_persisted_authorities_reach_one_durable_publication() -> None:
    service, provider, command, authority = _fixture()
    answer_authority = authority["answer"]
    binding = authority["binding"]
    connection = authority["connection"]
    repository = authority["dashboard_repository"]
    assert isinstance(answer_authority, DashboardAnswerAuthority)
    assert isinstance(binding, ApprovedProductQueryBinding)
    assert isinstance(connection, DashboardDatasetConnectionBinding)
    assert isinstance(repository, SQLiteDashboardRepository)
    query_bindings = SQLiteProductQueryBindingRepository(":memory:")
    query_bindings.store(binding)
    connections = SQLiteDashboardConnectionRepository(":memory:")
    connections.store(connection)
    service._answers = _request_owned_answer_reader(answer_authority)
    service._query_bindings = query_bindings
    service._connections = connections

    receipt = service.publish(command)

    assert receipt.revision == 1
    assert len(provider.definitions) == 1
    assert provider.definitions[0].title == "Revenue by region from request management"
    desired = repository.load_current_desired("tenant-a", "dashboard:revenue", 1)
    assert desired is not None
    assert desired.source_answer.result_ref == "result-1"
    assert repository.load_receipt("tenant-a", "dashboard:revenue", 1, 1) == receipt


@pytest.mark.parametrize(
    ("request_revision", "request_tenant_id"),
    ((8, "tenant-a"), (7, "tenant-b")),
)
def test_stale_or_cross_tenant_request_authority_prevents_provider_effect(
    request_revision: int, request_tenant_id: str
) -> None:
    service, provider, command, authority = _fixture()
    answer_authority = authority["answer"]
    assert isinstance(answer_authority, DashboardAnswerAuthority)
    service._answers = _request_owned_answer_reader(
        answer_authority,
        request_revision=request_revision,
        request_tenant_id=request_tenant_id,
    )

    with pytest.raises(DashboardCompositionError, match="answer authority is unavailable"):
        service.publish(command)

    assert provider.definitions == []
