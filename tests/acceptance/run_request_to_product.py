from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from heinzel_compiler import NoValidPlan, compile_product_iir
from heinzel_console.governed_adapters import (
    GovernedApprovedProductIntentSources,
    GovernedProductIntentAuthority,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import (
    AcquisitionActivationApproval,
    ActivatedAcquisitionContract,
    ActivatedAcquisitionContractRecord,
    ProductIntentBoundActivationService,
    SourceObservation,
    SQLiteAcquisitionContractLifecycleRepository,
    SQLiteSourceObservationRepository,
    ValidatedSourceBinding,
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
from heinzel_provider_sdk import AcquisitionField, AcquisitionObjectSchema
from heinzel_request_management import (
    ApprovedProductIntent,
    DeliveryIntent,
    DimensionIntent,
    FreshnessObjective,
    Grain,
    MeasureIntent,
    ProductIntent,
    ProductIntentApprovalService,
    ProductIntentAuthorityRefs,
    ProductIntentCandidate,
    ProductIntentCandidateService,
    ProductIntentConstraints,
    ProductIntentSourceCoverage,
    RequestManagementService,
    SQLiteRequestRepository,
)
from heinzel_semantic_registry import (
    SQLiteProductCatalogPublicationRepository,
    SQLiteSemanticVersionRepository,
)

from tests.acceptance.console_native_answer_fixture import DIMENSION, METRIC, SEMANTIC_VERSION
from tests.acceptance.run_console_governed import ARCHITECT, REQUESTER, TENANT

_SOURCE_REF = "source-live-a"
_PRODUCT_REF = "product_revenue_v1"
_SOURCE_RELATION = "source_revenue"
_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_POSTGRESQL_VERSION = (
    "postgres:18.6-bookworm@sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
)
_POSTGRESQL_CAPABILITIES = (
    "binary_collation",
    "decimal_38_9_sum",
    "group_by",
    "project",
    "quoted_identifiers",
    "utc_timezone",
)


def _clock() -> datetime:
    return _NOW


class ProductPublicationAuthority(Protocol):
    def list_publications(self, *, tenant_id: str) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class RequestToProductCompilation:
    request_id: str
    request_revision: int
    product_publications_before_request: int
    product_publications_before_compilation: int
    product_publications_after_compilation: int
    candidate: ProductIntentCandidate
    approved_intent: ApprovedProductIntent
    activation: ActivatedAcquisitionContractRecord
    activation_replay: ActivatedAcquisitionContractRecord
    product_iir: ProductIntentIR
    compiler_outcome: NoValidPlan


class _SQLiteProductPublicationInventory:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._repository = SQLiteProductCatalogPublicationRepository(
            str(database_path), check_same_thread=False
        )

    def close(self) -> None:
        self._repository.close()

    def list_publications(self, *, tenant_id: str) -> tuple[object, ...]:
        # The repository intentionally exposes lookup by exact product generation rather than
        # tenant-wide enumeration. This acceptance-only inventory reads its durable intent table
        # so a seeded or partially prepared publication cannot satisfy the fresh-journey proof.
        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                "SELECT operation_id FROM product_catalog_publication_intents "
                "WHERE tenant_id = ? ORDER BY operation_id",
                (tenant_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)


def require_empty_product_publication_authority(
    authority: ProductPublicationAuthority,
    *,
    tenant_id: str,
) -> int:
    publications = authority.list_publications(tenant_id=tenant_id)
    if publications:
        raise ValueError(
            "request-to-product acceptance requires an empty product publication authority"
        )
    return len(publications)


def build_product_iir(approved_intent: ApprovedProductIntent) -> ProductIntentIR:
    intent = approved_intent.intent
    expected_shape = (
        intent.source_refs == (_SOURCE_REF,)
        and intent.grain.keys == (DIMENSION.artifact_id,)
        and intent.measures == (MeasureIntent(metric_ref=METRIC.artifact_id, aggregation="sum"),)
        and intent.dimensions == (DimensionIntent(dimension_ref=DIMENSION.artifact_id),)
        and intent.filters == ()
    )
    if not expected_shape:
        raise ValueError("approved product intent does not match the revenue product IIR mapping")

    source = SourceRelation(
        relation_namespace="raw",
        relation_name=_SOURCE_RELATION,
        alias=_SOURCE_RELATION,
        columns=(
            ColumnDeclaration(name="region", value_type="string", nullable=False),
            ColumnDeclaration(name="amount", value_type="decimal", nullable=False),
        ),
    )
    return ProductIntentIR(
        product_ref=_PRODUCT_REF,
        source=source,
        operations=(
            ProjectOperation(
                expressions=(
                    NamedExpression(
                        output_name="region",
                        expression=ColumnReference(
                            relation_alias=_SOURCE_RELATION,
                            column_name="region",
                        ),
                    ),
                    NamedExpression(
                        output_name="amount",
                        expression=ColumnReference(
                            relation_alias=_SOURCE_RELATION,
                            column_name="amount",
                        ),
                    ),
                )
            ),
            AggregateOperation(
                group_by=(ColumnReference(relation_alias=_SOURCE_RELATION, column_name="region"),),
                measures=(
                    AggregateMeasure(
                        function="sum",
                        output_name="total_revenue",
                        argument=ColumnReference(
                            relation_alias=_SOURCE_RELATION,
                            column_name="amount",
                        ),
                    ),
                ),
            ),
        ),
        grain=(ColumnReference(relation_alias=_SOURCE_RELATION, column_name="region"),),
        freshness_seconds=intent.freshness.maximum_age_seconds,
    )


def _seed_governed_authority(directory: Path) -> ProductIntentAuthorityRefs:
    """Record the approved semantic version and a current source observation the intent uses.

    Approval derives its constraints from these records alone, so the journey must create them
    rather than assert constraints to the approval service.
    """
    semantic_versions = SQLiteSemanticVersionRepository(
        str(directory / "semantic-versions.sqlite3")
    )
    source_observations = SQLiteSourceObservationRepository(
        str(directory / "source-observations.sqlite3")
    )
    try:
        semantic_version = semantic_versions.store(SEMANTIC_VERSION)
        observation = source_observations.store(
            SourceObservation(
                observation_id="source-observation-live-a",
                tenant_id=TENANT,
                version=1,
                source_ref=_SOURCE_REF,
                schema_digest="4" * 64,
                observed_at=_NOW - timedelta(hours=1),
                valid_until=_NOW + timedelta(days=1),
            )
        )
    finally:
        semantic_versions.close()
        source_observations.close()
    return ProductIntentAuthorityRefs(
        semantic_version=ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        ),
        source_observations=(
            ArtifactReference(
                artifact_id=observation.observation_id,
                version=observation.version,
                digest=digest(observation),
            ),
        ),
    )


def _activation_inputs(
    approval: ApprovedProductIntent, authority_refs: ProductIntentAuthorityRefs
) -> dict[str, Any]:
    """Acquisition activation for the approved intent, citing the observation approval used."""
    source_observation = authority_refs.source_observations[0]
    process_ref = SEMANTIC_VERSION.process_package_ref
    fields = (
        AcquisitionField(name="region", value_type="string", nullable=False),
        AcquisitionField(name="amount", value_type="decimal", nullable=False),
    )
    activated_at = _NOW
    return {
        "contract": ActivatedAcquisitionContract.model_validate(
            {
                "tenant_id": TENANT,
                "contract_ref": "acquisition-contract-revenue",
                "contract_digest": digest({"domain": "request-to-product", "approval": approval}),
                "process_package_ref": process_ref,
                "product_intent_ref": approval.artifact_reference,
                "destination_product_ref": _PRODUCT_REF,
                "source_binding_ref": _SOURCE_REF,
                "source_binding_revision": 1,
                "credential_revision": 1,
                "acknowledgement_consumer_ref": "runtime-request-to-product",
                "capability_profile_digest": "d" * 64,
                "source_observation_ref": source_observation.artifact_id,
                "source_observation_digest": source_observation.digest,
                "lifecycle_state": "activated",
                "acquisition_modes": ("snapshot",),
                "object_schemas": (
                    AcquisitionObjectSchema(
                        logical_object_ref=_SOURCE_RELATION,
                        schema_digest=digest(fields),
                        fields=fields,
                        record_key_fields=("region",),
                        source_updated_at_field=None,
                    ),
                ),
                "record_ceiling": 10_000,
                "encoded_byte_ceiling": 10_000_000,
                "activated_by": ARCHITECT,
                "activated_at": activated_at,
            }
        ),
        "approval": AcquisitionActivationApproval(
            tenant_id=TENANT,
            process_package_ref=process_ref,
            product_intent_ref=approval.artifact_reference,
            destination_product_ref=_PRODUCT_REF,
            approved_by=ARCHITECT,
            approved_at=approval.approved_at,
        ),
        "source_validation": ValidatedSourceBinding(
            tenant_id=TENANT,
            source_binding_ref=_SOURCE_REF,
            source_binding_revision=1,
            credential_revision=1,
            capability_profile_digest="d" * 64,
            source_observation_ref=source_observation.artifact_id,
            source_observation_digest=source_observation.digest,
            validated_at=activated_at,
        ),
    }


def execute_postgresql_request_to_product(directory: Path) -> RequestToProductCompilation:
    directory.mkdir(parents=True, exist_ok=True)
    authority_refs = _seed_governed_authority(directory)
    publication_inventory = _SQLiteProductPublicationInventory(
        directory / "product-publications.sqlite3"
    )
    with closing(publication_inventory):
        before_request = require_empty_product_publication_authority(
            publication_inventory, tenant_id=TENANT
        )
        request_repository = SQLiteRequestRepository(
            sqlite3.connect(directory / "requests.sqlite3"), _owns_connection=True
        )
        with closing(request_repository):
            requests = RequestManagementService(request_repository, clock=_clock)
            candidates = ProductIntentCandidateService(request_repository, clock=_clock)
            semantic_versions = SQLiteSemanticVersionRepository(
                str(directory / "semantic-versions.sqlite3")
            )
            source_observations = SQLiteSourceObservationRepository(
                str(directory / "source-observations.sqlite3")
            )
            approvals = ProductIntentApprovalService(
                request_repository,
                clock=_clock,
                authority=GovernedProductIntentAuthority(
                    semantic_versions=semantic_versions,
                    source_observations=source_observations,
                ),
            )
            request = requests.submit_question(
                tenant_id=TENANT,
                requester_id=REQUESTER,
                purpose="Build a governed revenue product for regional review.",
                question="Create current revenue by region as a dataset, table, and dashboard.",
                title="Current revenue by region",
            )
            intent = ProductIntent(
                request_id=request.request_id,
                title="Current revenue by region",
                business_outcome="Give revenue leaders one governed current regional view.",
                source_refs=(_SOURCE_REF,),
                grain=Grain(keys=(DIMENSION.artifact_id,)),
                measures=(MeasureIntent(metric_ref=METRIC.artifact_id, aggregation="sum"),),
                dimensions=(DimensionIntent(dimension_ref=DIMENSION.artifact_id),),
                filters=(),
                freshness=FreshnessObjective(maximum_age_seconds=86_400),
                delivery=DeliveryIntent(outputs=("dataset", "table", "dashboard")),
            )
            constraints = ProductIntentConstraints(
                approved_source_refs=(_SOURCE_REF,),
                approved_metric_refs=(METRIC.artifact_id,),
                approved_dimension_refs=(DIMENSION.artifact_id,),
                minimum_source_interval_seconds=86_400,
            )
            candidate = candidates.propose(
                tenant_id=TENANT,
                request_id=request.request_id,
                request_revision=request.revision,
                idempotency_key="request-to-product-candidate-1",
                proposed_by="external-interpreter",
                intent=intent,
                constraints=constraints,
                source_coverage=(
                    ProductIntentSourceCoverage(
                        source_ref=_SOURCE_REF,
                        covered_fields=("region", "amount"),
                        authorized=True,
                    ),
                ),
                unresolved_constraints=(),
                authority_refs=authority_refs,
            )
            try:
                approval = approvals.approve(
                    tenant_id=TENANT,
                    request_id=request.request_id,
                    request_revision=request.revision,
                    approved_by=ARCHITECT,
                    intent=candidate.intent,
                    authority_refs=candidate.authority_refs,
                )
            finally:
                semantic_versions.close()
                source_observations.close()
            if not isinstance(approval, ApprovedProductIntent):
                raise AssertionError("acceptance product intent was not approved")
            if candidates.current_candidate(TENANT, request.request_id) != candidate:
                raise AssertionError("product intent candidate was not durably recorded")
            if approvals.list_for_request(TENANT, request.request_id) != (approval,):
                raise AssertionError("product intent approval was not durably recorded")

            activation_inputs = _activation_inputs(approval, authority_refs)
            contracts = SQLiteAcquisitionContractLifecycleRepository(
                str(directory / "acquisition-lifecycle.sqlite3")
            )
            try:
                activations = ProductIntentBoundActivationService(
                    contracts,
                    product_intents=GovernedApprovedProductIntentSources(approvals),
                )
                activation = activations.activate(
                    idempotency_key="request-to-product-activation-1", **activation_inputs
                )
                activation_replay = activations.activate(
                    idempotency_key="request-to-product-activation-1", **activation_inputs
                )
            finally:
                contracts.close()

            before_compilation = require_empty_product_publication_authority(
                publication_inventory, tenant_id=TENANT
            )
            product_iir = build_product_iir(approval)
            compiler_outcome = compile_product_iir(
                product_iir,
                engine="postgresql",
                engine_version=_POSTGRESQL_VERSION,
                capabilities=_POSTGRESQL_CAPABILITIES,
            )
            after_compilation = require_empty_product_publication_authority(
                publication_inventory, tenant_id=TENANT
            )
            return RequestToProductCompilation(
                request_id=request.request_id,
                request_revision=request.revision,
                product_publications_before_request=before_request,
                product_publications_before_compilation=before_compilation,
                product_publications_after_compilation=after_compilation,
                candidate=candidate,
                approved_intent=approval,
                activation=activation,
                activation_replay=activation_replay,
                product_iir=product_iir,
                compiler_outcome=compiler_outcome,
            )
