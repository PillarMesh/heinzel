from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogPersistenceError,
)
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactReference,
    ManagedIntegrationContract,
    digest,
)
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_provider_sdk import (
    CatalogWarehouseHierarchyAuthority,
    catalog_warehouse_service_external_key,
)
from heinzel_semantic_registry import (
    ApprovedProductVersionRepository,
    ProductCatalogDefinitionAuthority,
    ProductCatalogPublicationExecutionService,
    ProductCatalogPublicationProvider,
    ProductCatalogPublicationProviderError,
    ProductCatalogPublicationRepository,
    ProductCatalogPublicationService,
    ProductQueryBindingApproval,
    ProductQueryBindingAuthorityService,
    ProductQueryBindingDeclaration,
    ProductQueryBindingRepository,
    ProductVersionAuthorityService,
)
from heinzel_semantic_registry.product_authority import MaterializedProductEvidence
from heinzel_warehouse_control import WarehouseBinding, WarehouseBindingState
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .product_materialization import (
    CatalogPublicationError,
    MaterializationRequest,
    ProductMaterializationReceipt,
)


class WarehouseBindingAuthority(Protocol):
    def load(self, tenant_id: str, binding_id: str) -> WarehouseBinding | None: ...


class CatalogBindingAuthority(Protocol):
    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding: ...


class ProductCatalogCompositionConfig(BaseModel):
    """Non-secret provider hierarchy settings selected by deployment configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    catalog_binding_id: str = Field(min_length=1)
    database_name: str = Field(min_length=1)


def _clock() -> datetime:
    return datetime.now(UTC)


def compose_authoritative_product_catalog(
    *,
    config: ProductCatalogCompositionConfig,
    warehouse_bindings: WarehouseBindingAuthority,
    catalog_bindings: CatalogBindingAuthority,
    publication_repository: ProductCatalogPublicationRepository,
    publication_provider: ProductCatalogPublicationProvider,
    product_version_repository: ApprovedProductVersionRepository,
    query_binding_repository: ProductQueryBindingRepository,
    contract: ManagedIntegrationContract,
    semantic_version: ApprovedSemanticVersion,
    definition_authority: ProductCatalogDefinitionAuthority,
    query_binding_declaration: ProductQueryBindingDeclaration,
    query_binding_approvals: tuple[ProductQueryBindingApproval, ...],
    source_freshness_observations: tuple[SourceFreshnessObservation, ...],
    clock: Callable[[], datetime] = _clock,
) -> AuthoritativeProductCatalog:
    """Wire durable authorities to the configured catalog provider for one product contract."""

    config = ProductCatalogCompositionConfig.model_validate(
        config.model_dump(mode="python"), strict=True
    )
    contract = ManagedIntegrationContract.model_validate(
        contract.model_dump(mode="python"), strict=True
    )
    try:
        catalog_binding = CatalogBinding.model_validate(
            catalog_bindings.load(contract.tenant_id, config.catalog_binding_id),
            strict=True,
        )
    except (CatalogPersistenceError, KeyError, ValidationError):
        raise ValueError("catalog binding authority is unavailable") from None
    if (
        catalog_binding.tenant_id != contract.tenant_id
        or catalog_binding.binding_id != config.catalog_binding_id
    ):
        raise ValueError("catalog binding authority returned a mismatched binding")
    if catalog_binding.lifecycle_state is not CatalogBindingState.READY:
        raise CatalogPublicationError("managed catalog binding is not ready")
    if publication_provider.provider_kind != catalog_binding.provider_kind:
        raise ValueError("catalog provider does not match configured binding authority")
    return AuthoritativeProductCatalog(
        warehouse_bindings=warehouse_bindings,
        catalog_binding=catalog_binding,
        publication_service=ProductCatalogPublicationService(
            repository=publication_repository,
            binding_authority=catalog_bindings,
        ),
        publication_execution=ProductCatalogPublicationExecutionService(
            repository=publication_repository,
            provider=publication_provider,
            clock=clock,
        ),
        product_version_authority=ProductVersionAuthorityService(product_version_repository),
        query_binding_authority=ProductQueryBindingAuthorityService(
            query_binding_repository,
            clock=clock,
        ),
        contract=contract,
        semantic_version=semantic_version,
        definition_authority=definition_authority,
        query_binding_declaration=query_binding_declaration,
        query_binding_approvals=query_binding_approvals,
        source_freshness_observations=source_freshness_observations,
        database_name=config.database_name,
    )


class AuthoritativeProductCatalog:
    """Publish a committed generation through its owning warehouse and catalog authorities."""

    def __init__(
        self,
        *,
        warehouse_bindings: WarehouseBindingAuthority,
        catalog_binding: CatalogBinding,
        publication_service: ProductCatalogPublicationService,
        publication_execution: ProductCatalogPublicationExecutionService,
        product_version_authority: ProductVersionAuthorityService,
        query_binding_authority: ProductQueryBindingAuthorityService,
        contract: ManagedIntegrationContract,
        semantic_version: ApprovedSemanticVersion,
        definition_authority: ProductCatalogDefinitionAuthority,
        query_binding_declaration: ProductQueryBindingDeclaration,
        query_binding_approvals: tuple[ProductQueryBindingApproval, ...],
        source_freshness_observations: tuple[SourceFreshnessObservation, ...],
        database_name: str,
    ) -> None:
        self._warehouse_bindings = warehouse_bindings
        self._catalog_binding = CatalogBinding.model_validate(
            catalog_binding.model_dump(mode="python"), strict=True
        )
        self._publication_service = publication_service
        self._publication_execution = publication_execution
        self._product_version_authority = product_version_authority
        self._query_binding_authority = query_binding_authority
        self._contract = ManagedIntegrationContract.model_validate(
            contract.model_dump(mode="python"), strict=True
        )
        self._semantic_version = ApprovedSemanticVersion.model_validate(
            semantic_version.model_dump(mode="python"), strict=True
        )
        self._definition_authority = ProductCatalogDefinitionAuthority.model_validate(
            definition_authority.model_dump(mode="python"), strict=True
        )
        self._query_binding_declaration = ProductQueryBindingDeclaration.model_validate(
            query_binding_declaration.model_dump(mode="python"), strict=True
        )
        self._query_binding_approvals = tuple(
            ProductQueryBindingApproval.model_validate(
                approval.model_dump(mode="python"), strict=True
            )
            for approval in query_binding_approvals
        )
        self._source_freshness_observations = tuple(
            SourceFreshnessObservation.model_validate(
                observation.model_dump(mode="python"), strict=True
            )
            for observation in source_freshness_observations
        )
        self._database_name = database_name

    def publish(
        self,
        request: MaterializationRequest,
        receipt: ProductMaterializationReceipt,
    ) -> str:
        request = MaterializationRequest.model_validate(
            request.model_dump(mode="python"), strict=True
        )
        receipt = ProductMaterializationReceipt.model_validate(
            receipt.model_dump(mode="python"), strict=True
        )
        self._validate_authority(request=request, receipt=receipt)
        binding = self._load_ready_warehouse_binding(request)
        receipt_reference = ArtifactReference(
            artifact_id=receipt.run_id,
            version=receipt.product_generation,
            digest=digest(receipt),
        )
        product_metadata = self._product_version_authority.record(
            contract=self._contract,
            semantic_version=self._semantic_version,
            materialization=MaterializedProductEvidence(
                tenant_id=receipt.tenant_id,
                product_ref=ArtifactReference(
                    artifact_id=self._contract.destination_product.product_name,
                    version=self._contract.version,
                    digest=digest(self._contract.destination_product),
                ),
                generation=receipt.product_generation,
                contract_digest=receipt.contract_digest,
                materialization_receipt_ref=receipt_reference,
                lineage_digest=receipt.lineage_digest,
                materialized_at=receipt.committed_at,
            ),
        )
        query_binding = self._query_binding_authority.record(
            contract=self._contract,
            semantic_version=self._semantic_version,
            product_metadata=product_metadata,
            declaration=self._query_binding_declaration,
            approvals=self._query_binding_approvals,
        )
        hierarchy = CatalogWarehouseHierarchyAuthority(
            tenant_id=binding.tenant_id,
            warehouse_binding_id=binding.binding_id,
            warehouse_binding_revision=binding.revision,
            warehouse_provider=binding.engine_kind.value,
            database_service_name=catalog_warehouse_service_external_key(
                tenant_id=binding.tenant_id,
                warehouse_binding_id=binding.binding_id,
            ),
            database_name=self._database_name,
            schema_name=query_binding.namespace,
            table_name=query_binding.relation_name,
        )
        intent = self._publication_service.prepare(
            binding=self._catalog_binding,
            warehouse_hierarchy=hierarchy,
            definition_authority=self._definition_authority,
            product_metadata=product_metadata,
            query_binding=query_binding,
            materialization_receipt_ref=receipt_reference,
            materialization_receipt_digest=digest(receipt),
            source_freshness_observations=self._source_freshness_observations,
            contract=self._contract,
            semantic_version=self._semantic_version,
        )
        try:
            publication = self._publication_execution.publish(intent=intent)
        except ProductCatalogPublicationProviderError as error:
            raise CatalogPublicationError(
                f"managed catalog publication is pending: {error.classification}"
            ) from error
        return publication.publication_id

    def _validate_authority(
        self,
        *,
        request: MaterializationRequest,
        receipt: ProductMaterializationReceipt,
    ) -> None:
        expected_receipt_identity = (
            request.run_id,
            request.tenant_id,
            request.product_id,
            request.product_revision,
            request.product_generation,
            request.contract_digest,
            request.input_generation_digests,
            request.physical_plan_digest,
            request.compiled_model_digest,
            request.expected_output_schema_digest,
        )
        actual_receipt_identity = (
            receipt.run_id,
            receipt.tenant_id,
            receipt.product_id,
            receipt.product_revision,
            receipt.product_generation,
            receipt.contract_digest,
            receipt.input_generation_digests,
            receipt.physical_plan_digest,
            receipt.compiled_model_digest,
            receipt.output_schema_digest,
        )
        contract_identity = (
            self._contract.tenant_id,
            self._contract.destination_product.product_name,
            self._contract.version,
            digest(self._contract),
        )
        if (
            expected_receipt_identity != actual_receipt_identity
            or (
                request.tenant_id,
                request.product_id,
                request.product_revision,
                request.contract_digest,
            )
            != contract_identity
        ):
            raise ValueError("materialization does not match approved product authority")
        freshness_generations = tuple(
            observation.input_generation_digest
            for observation in self._source_freshness_observations
        )
        if tuple(sorted(freshness_generations)) != tuple(sorted(request.input_generation_digests)):
            raise ValueError("source freshness does not match materialization inputs")

    def _load_ready_warehouse_binding(self, request: MaterializationRequest) -> WarehouseBinding:
        try:
            binding = self._warehouse_bindings.load(
                request.tenant_id,
                self._contract.destination_product.warehouse_binding_id,
            )
            if binding is not None:
                binding = WarehouseBinding.model_validate(
                    binding.model_dump(mode="python"), strict=True
                )
        except (KeyError, ValidationError):
            binding = None
        if binding is None or binding.lifecycle_state is not WarehouseBindingState.READY:
            raise CatalogPublicationError("managed warehouse binding is not ready")
        if (
            binding.tenant_id != request.tenant_id
            or binding.binding_id != self._contract.destination_product.warehouse_binding_id
            or binding.engine_kind.value != self._query_binding_declaration.engine_kind
            or binding.engine_kind.value not in self._contract.destination_product.supported_engines
        ):
            raise ValueError("warehouse binding does not match approved product authority")
        return binding
