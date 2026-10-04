from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol, Self

from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ManagedIntegrationContract,
    SemanticObject,
    canonical_bytes,
    digest,
)
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .product_authority import ApprovedProductVersionMetadata

type QueryEngine = Literal["postgresql", "clickhouse"]
type QueryAggregate = Literal["average", "count", "maximum", "minimum", "sum"]
type QueryDimensionKind = Literal["entity", "event", "state", "relationship", "classification"]
type Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class ProductQueryBindingAuthorityError(ValueError):
    pass


class _StrictQueryBindingModel(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProductQueryMetricBinding(_StrictQueryBindingModel):
    semantic_ref: ArtifactReference
    aggregate: QueryAggregate
    column_name: Identifier
    output_name: Identifier


class ProductQueryDimensionBinding(_StrictQueryBindingModel):
    semantic_ref: ArtifactReference
    semantic_kind: QueryDimensionKind
    column_name: Identifier
    output_name: Identifier


class ProductQueryBindingDeclaration(_StrictQueryBindingModel):
    schema_version: Literal["1"] = "1"
    engine_kind: QueryEngine
    namespace: Identifier
    relation_name: Identifier
    metric_bindings: tuple[ProductQueryMetricBinding, ...] = Field(min_length=1)
    dimension_bindings: tuple[ProductQueryDimensionBinding, ...] = Field(min_length=1)
    disclosure_entity_ref: ArtifactReference
    disclosure_entity_column: Identifier

    @model_validator(mode="after")
    def bindings_are_unambiguous(self) -> Self:
        semantic_refs = tuple(binding.semantic_ref for binding in self.metric_bindings) + tuple(
            binding.semantic_ref for binding in self.dimension_bindings
        )
        if len(semantic_refs) != len(set(semantic_refs)):
            raise ValueError("query bindings must not repeat a semantic reference")
        output_names = tuple(binding.output_name for binding in self.metric_bindings) + tuple(
            binding.output_name for binding in self.dimension_bindings
        )
        if len(output_names) != len(set(output_names)):
            raise ValueError("query binding output names must be unique")
        disclosure_bindings = tuple(
            binding
            for binding in self.dimension_bindings
            if binding.semantic_ref == self.disclosure_entity_ref
            and binding.column_name == self.disclosure_entity_column
            and binding.semantic_kind == "entity"
        )
        if len(disclosure_bindings) != 1:
            raise ValueError("disclosure entity must match one dimension binding")
        return self


class ProductQueryBindingApproval(_StrictQueryBindingModel):
    schema_version: Literal["1"] = "1"
    approval_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    product_ref: ArtifactReference
    generation: int = Field(ge=1)
    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_ref: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    decision: Literal["approve"]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)


class ApprovedProductQueryBinding(_StrictQueryBindingModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    product_ref: ArtifactReference
    generation: int = Field(ge=1)
    contract_ref: ArtifactReference
    semantic_version_ref: ArtifactReference
    materialization_receipt_ref: ArtifactReference
    lineage_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumption_object_ref: ArtifactReference
    engine_kind: QueryEngine
    namespace: Identifier
    relation_name: Identifier
    metric_bindings: tuple[ProductQueryMetricBinding, ...]
    dimension_bindings: tuple[ProductQueryDimensionBinding, ...]
    disclosure_entity_ref: ArtifactReference
    disclosure_entity_column: Identifier
    approvals: tuple[ProductQueryBindingApproval, ...] = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def generation_refs_match(self) -> Self:
        if (
            self.materialization_receipt_ref.version != self.generation
            or self.consumption_object_ref.version != self.generation
        ):
            raise ValueError("query binding references must match the product generation")
        declaration = ProductQueryBindingDeclaration(
            engine_kind=self.engine_kind,
            namespace=self.namespace,
            relation_name=self.relation_name,
            metric_bindings=self.metric_bindings,
            dimension_bindings=self.dimension_bindings,
            disclosure_entity_ref=self.disclosure_entity_ref,
            disclosure_entity_column=self.disclosure_entity_column,
        )
        if digest(declaration) != self.declaration_digest:
            raise ValueError("query binding declaration digest does not match its payload")
        if self.consumption_object_ref != _consumption_object_reference(
            tenant_id=self.tenant_id,
            product_ref=self.product_ref,
            generation=self.generation,
            materialization_receipt_ref=self.materialization_receipt_ref,
            declaration=declaration,
        ):
            raise ValueError("consumption object reference does not match query binding")
        if digest(self.approvals) != self.approval_digest:
            raise ValueError("query binding approval digest does not match its payload")
        approval_ids = tuple(approval.approval_id for approval in self.approvals)
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("query binding approval identifiers must be unique")
        if approval_ids != tuple(sorted(approval_ids)):
            raise ValueError("query binding approvals must be canonically ordered")
        expected_identity = (
            self.tenant_id,
            self.product_ref,
            self.generation,
            self.declaration_digest,
        )
        if any(
            (
                approval.tenant_id,
                approval.product_ref,
                approval.generation,
                approval.declaration_digest,
            )
            != expected_identity
            for approval in self.approvals
        ):
            raise ValueError("query binding approval does not match its payload")
        if any(approval.created_at > self.recorded_at for approval in self.approvals):
            raise ValueError("query binding approval cannot be created after its record")
        return self


class ProductQueryBindingRepository(Protocol):
    def store(self, binding: ApprovedProductQueryBinding) -> ApprovedProductQueryBinding: ...

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductQueryBinding | None: ...


class SQLiteProductQueryBindingRepository:
    def __init__(self, database_path: str, *, check_same_thread: bool = True) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS approved_product_query_bindings ("
            "tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
            "generation INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, product_id, product_revision, generation))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, binding: ApprovedProductQueryBinding) -> ApprovedProductQueryBinding:
        binding = ApprovedProductQueryBinding.model_validate(
            binding.model_dump(mode="python"), strict=True
        )
        payload = canonical_bytes(binding)
        identity = (
            binding.tenant_id,
            binding.product_ref.artifact_id,
            binding.product_ref.version,
            binding.generation,
        )
        try:
            self._connection.execute(
                "INSERT INTO approved_product_query_bindings "
                "(tenant_id, product_id, product_revision, generation, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, product_id, product_revision, generation) DO NOTHING",
                (*identity, payload),
            )
            row = self._connection.execute(
                "SELECT payload FROM approved_product_query_bindings "
                "WHERE tenant_id = ? AND product_id = ? AND product_revision = ? "
                "AND generation = ?",
                identity,
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("query binding authority is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return binding

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductQueryBinding | None:
        try:
            row = self._connection.execute(
                "SELECT tenant_id, product_id, product_revision, generation, payload "
                "FROM approved_product_query_bindings WHERE tenant_id = ? AND product_id = ? "
                "AND product_revision = ? AND generation = ?",
                (tenant_id, product_ref.artifact_id, product_ref.version, generation),
            ).fetchone()
        except sqlite3.Error as error:
            raise ProductQueryBindingAuthorityError(
                "query binding authority is unavailable"
            ) from error
        if row is None:
            return None
        try:
            binding = ApprovedProductQueryBinding.model_validate_json(row[4], strict=True)
        except (ValidationError, ValueError, TypeError):
            raise ProductQueryBindingAuthorityError(
                "stored query binding authority is invalid"
            ) from None
        if (
            row[0] != binding.tenant_id
            or row[1] != binding.product_ref.artifact_id
            or row[2] != binding.product_ref.version
            or row[3] != binding.generation
            or binding.tenant_id != tenant_id
            or binding.product_ref != product_ref
            or binding.generation != generation
        ):
            raise ProductQueryBindingAuthorityError(
                "query binding authority index does not match its payload"
            )
        return binding


class ProductQueryBindingAuthorityService:
    def __init__(
        self,
        repository: ProductQueryBindingRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._clock = clock

    def record(
        self,
        *,
        contract: ManagedIntegrationContract,
        semantic_version: ApprovedSemanticVersion,
        product_metadata: ApprovedProductVersionMetadata,
        declaration: ProductQueryBindingDeclaration,
        approvals: tuple[ProductQueryBindingApproval, ...],
    ) -> ApprovedProductQueryBinding:
        contract = ManagedIntegrationContract.model_validate(
            contract.model_dump(mode="python"), strict=True
        )
        semantic_version = ApprovedSemanticVersion.model_validate(
            semantic_version.model_dump(mode="python"), strict=True
        )
        product_metadata = ApprovedProductVersionMetadata.model_validate(
            product_metadata.model_dump(mode="python"), strict=True
        )
        declaration = ProductQueryBindingDeclaration.model_validate(
            declaration.model_dump(mode="python"), strict=True
        )
        approvals = tuple(
            sorted(
                (
                    ProductQueryBindingApproval.model_validate(
                        approval.model_dump(mode="python"), strict=True
                    )
                    for approval in approvals
                ),
                key=lambda approval: approval.approval_id,
            )
        )
        self._require_approved_authority(
            contract=contract,
            semantic_version=semantic_version,
            product_metadata=product_metadata,
            declaration=declaration,
            approvals=approvals,
        )
        declaration_digest = digest(declaration)
        approval_digest = digest(approvals)
        existing = self._repository.read_current(
            tenant_id=contract.tenant_id,
            product_ref=product_metadata.product_ref,
            generation=product_metadata.generation,
        )
        if existing is not None:
            if (
                existing.declaration_digest != declaration_digest
                or existing.approval_digest != approval_digest
                or existing.contract_ref != product_metadata.contract_ref
                or existing.semantic_version_ref != product_metadata.semantic_version_ref
                or existing.materialization_receipt_ref
                != product_metadata.materialization_receipt_ref
                or existing.lineage_digest != product_metadata.lineage_digest
            ):
                raise ValueError("query binding authority is immutable")
            return existing
        consumption_object_ref = _consumption_object_reference(
            tenant_id=contract.tenant_id,
            product_ref=product_metadata.product_ref,
            generation=product_metadata.generation,
            materialization_receipt_ref=product_metadata.materialization_receipt_ref,
            declaration=declaration,
        )
        return self._repository.store(
            ApprovedProductQueryBinding(
                tenant_id=contract.tenant_id,
                product_ref=product_metadata.product_ref,
                generation=product_metadata.generation,
                contract_ref=product_metadata.contract_ref,
                semantic_version_ref=product_metadata.semantic_version_ref,
                materialization_receipt_ref=product_metadata.materialization_receipt_ref,
                lineage_digest=product_metadata.lineage_digest,
                declaration_digest=declaration_digest,
                approval_digest=approval_digest,
                consumption_object_ref=consumption_object_ref,
                engine_kind=declaration.engine_kind,
                namespace=declaration.namespace,
                relation_name=declaration.relation_name,
                metric_bindings=declaration.metric_bindings,
                dimension_bindings=declaration.dimension_bindings,
                disclosure_entity_ref=declaration.disclosure_entity_ref,
                disclosure_entity_column=declaration.disclosure_entity_column,
                approvals=approvals,
                recorded_at=self._now(),
            )
        )

    def _require_approved_authority(
        self,
        *,
        contract: ManagedIntegrationContract,
        semantic_version: ApprovedSemanticVersion,
        product_metadata: ApprovedProductVersionMetadata,
        declaration: ProductQueryBindingDeclaration,
        approvals: tuple[ProductQueryBindingApproval, ...],
    ) -> None:
        semantic_ref = ArtifactReference(
            artifact_id=semantic_version.semantic_version_id,
            version=semantic_version.version,
            digest=digest(semantic_version),
        )
        product_ref = ArtifactReference(
            artifact_id=contract.destination_product.product_name,
            version=contract.version,
            digest=digest(contract.destination_product),
        )
        contract_ref = ArtifactReference(
            artifact_id=contract.contract_id,
            version=contract.version,
            digest=digest(contract),
        )
        if (
            not contract.approval_ids
            or not semantic_version.approval_ids
            or contract.semantic_version_ref != semantic_ref
        ):
            raise ValueError("query binding requires approved contract and semantic authority")
        if (
            product_metadata.tenant_id != contract.tenant_id
            or semantic_version.tenant_id != contract.tenant_id
            or product_metadata.product_ref != product_ref
            or product_metadata.contract_ref != contract_ref
            or product_metadata.semantic_version_ref != semantic_ref
        ):
            raise ValueError("query binding does not match approved product authority")
        declaration_digest = digest(declaration)
        expected_approval_identity = (
            contract.tenant_id,
            product_metadata.product_ref,
            product_metadata.generation,
            declaration_digest,
        )
        if not approvals or any(
            (
                approval.tenant_id,
                approval.product_ref,
                approval.generation,
                approval.declaration_digest,
            )
            != expected_approval_identity
            for approval in approvals
        ):
            raise ValueError("query binding requires explicit approval authority")
        if len({approval.approval_id for approval in approvals}) != len(approvals):
            raise ValueError("query binding approval identifiers must be unique")
        approved_authorities = {approval.authority_ref for approval in approvals}
        if not set(contract.access_policy.required_approver_refs).issubset(approved_authorities):
            raise ValueError("query binding lacks required owner approval authority")
        if declaration.engine_kind not in contract.destination_product.supported_engines:
            raise ValueError("contract does not support the query binding engine")
        metrics = _semantic_references(semantic_version.metrics, semantic_version.version)
        dimensions_by_kind = {
            "entity": _semantic_references(semantic_version.entities, semantic_version.version),
            "event": _semantic_references(semantic_version.events, semantic_version.version),
            "state": _semantic_references(semantic_version.states, semantic_version.version),
            "relationship": _semantic_references(
                semantic_version.relationships, semantic_version.version
            ),
            "classification": _semantic_references(
                semantic_version.classifications, semantic_version.version
            ),
        }
        if any(binding.semantic_ref not in metrics for binding in declaration.metric_bindings):
            raise ValueError("query metric binding does not match semantic kind")
        if any(
            binding.semantic_ref not in dimensions_by_kind[binding.semantic_kind]
            for binding in declaration.dimension_bindings
        ):
            raise ValueError("query dimension binding does not match semantic kind")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("clock must return a timezone-aware UTC timestamp")
        return value.astimezone(UTC)


def _semantic_references(
    objects: tuple[SemanticObject, ...], version: int
) -> frozenset[ArtifactReference]:
    return frozenset(
        ArtifactReference(artifact_id=item.object_id, version=version, digest=digest(item))
        for item in objects
    )


def _consumption_object_reference(
    *,
    tenant_id: str,
    product_ref: ArtifactReference,
    generation: int,
    materialization_receipt_ref: ArtifactReference,
    declaration: ProductQueryBindingDeclaration,
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=f"{product_ref.artifact_id}:consumption",
        version=generation,
        digest=digest(
            {
                "domain": "heinzel-product-query-consumption-v1",
                "tenant_id": tenant_id,
                "product_ref": product_ref,
                "generation": generation,
                "materialization_receipt_ref": materialization_receipt_ref,
                "engine_kind": declaration.engine_kind,
                "namespace": declaration.namespace,
                "relation_name": declaration.relation_name,
            }
        ),
    )
