from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self

from heinzel_catalog_control import (
    CatalogBinding,
    CatalogBindingState,
    CatalogPersistenceError,
)
from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ContractFormationStatus,
    ManagedIntegrationContract,
    canonical_bytes,
    digest,
)
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_provider_sdk import (
    CatalogColumn,
    CatalogLineageSource,
    CatalogNativeTableDefinition,
    CatalogNativeTableObservation,
    CatalogProductDefinition,
    CatalogProductObservation,
    CatalogProductProvider,
    CatalogWarehouseHierarchyAuthority,
    ProviderError,
    ProviderErrorClassification,
    catalog_product_external_key,
)
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .product_authority import ApprovedProductVersionMetadata
from .query_binding import ApprovedProductQueryBinding

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ProductCatalogColumnAuthority(ArtifactModel):
    """Approved physical column metadata exposed through the managed catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    type_name: str = Field(min_length=1)
    nullable: bool
    description: str | None = Field(default=None, min_length=1)

    @field_validator("name", "type_name", "description")
    @classmethod
    def text_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("catalog column text must be nonblank")
        return value


class ProductCatalogDefinitionAuthority(ArtifactModel):
    """Provider-neutral display and physical metadata approved for publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner_refs: tuple[str, ...] = Field(min_length=1)
    namespace: str = Field(min_length=1)
    relation_name: str = Field(min_length=1)
    columns: tuple[ProductCatalogColumnAuthority, ...] = Field(min_length=1)

    @field_validator("name", "description", "namespace", "relation_name")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("catalog definition text must be nonblank")
        return value

    @model_validator(mode="after")
    def collections_are_canonical(self) -> Self:
        if any(not owner_ref.strip() for owner_ref in self.owner_refs) or self.owner_refs != tuple(
            sorted(set(self.owner_refs))
        ):
            raise ValueError("catalog owner references must be unique and canonically ordered")
        column_names = tuple(column.name for column in self.columns)
        if len(column_names) != len(set(column_names)):
            raise ValueError("catalog column names must be unique")
        return self


class ProductCatalogPublicationIntent(ArtifactModel):
    """Exact, provider-neutral authority for publishing one product generation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    operation_id: str = Field(pattern=_DIGEST_PATTERN)
    tenant_id: str = Field(min_length=1)
    catalog_binding_id: str = Field(min_length=1)
    catalog_binding_revision: int = Field(ge=1)
    warehouse_hierarchy: CatalogWarehouseHierarchyAuthority
    definition_authority: ProductCatalogDefinitionAuthority
    product_metadata: ApprovedProductVersionMetadata
    query_binding: ApprovedProductQueryBinding
    materialization_receipt_ref: ArtifactReference
    materialization_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_freshness_observation_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    source_freshness_observations: tuple[SourceFreshnessObservation, ...] = Field(min_length=1)
    contract: ManagedIntegrationContract
    semantic_version: ApprovedSemanticVersion

    @model_validator(mode="after")
    def exact_authorities_agree(self) -> Self:
        semantic_ref = ArtifactReference(
            artifact_id=self.semantic_version.semantic_version_id,
            version=self.semantic_version.version,
            digest=digest(self.semantic_version),
        )
        contract_ref = ArtifactReference(
            artifact_id=self.contract.contract_id,
            version=self.contract.version,
            digest=digest(self.contract),
        )
        product_ref = ArtifactReference(
            artifact_id=self.contract.destination_product.product_name,
            version=self.contract.version,
            digest=digest(self.contract.destination_product),
        )
        expected_freshness_refs = tuple(
            ArtifactReference(
                artifact_id=observation.observation_id,
                version=observation.version,
                digest=digest(observation),
            )
            for observation in self.source_freshness_observations
        )
        source_refs = tuple(
            observation.source_ref for observation in self.source_freshness_observations
        )
        expected_column_names = frozenset(
            {
                *(binding.column_name for binding in self.query_binding.metric_bindings),
                *(binding.column_name for binding in self.query_binding.dimension_bindings),
            }
        )
        if (
            not self.contract.approval_ids
            or not self.semantic_version.approval_ids
            or self.contract.formation_status is not ContractFormationStatus.READY_TO_ACTIVATE
            or self.contract.semantic_version_ref != semantic_ref
        ):
            raise ValueError("product catalog publication requires approved contract authority")
        if (
            self.product_metadata.tenant_id != self.tenant_id
            or self.query_binding.tenant_id != self.tenant_id
            or self.contract.tenant_id != self.tenant_id
            or self.semantic_version.tenant_id != self.tenant_id
            or any(
                observation.tenant_id != self.tenant_id
                for observation in self.source_freshness_observations
            )
        ):
            raise ValueError("product catalog publication authorities must share one tenant")
        if (
            self.warehouse_hierarchy.tenant_id != self.tenant_id
            or self.warehouse_hierarchy.warehouse_binding_id
            != self.contract.destination_product.warehouse_binding_id
            or self.warehouse_hierarchy.warehouse_provider != self.query_binding.engine_kind
            or self.warehouse_hierarchy.schema_name != self.definition_authority.namespace
            or self.warehouse_hierarchy.table_name != self.definition_authority.relation_name
        ):
            raise ValueError("warehouse hierarchy does not match product publication authority")
        if (
            self.product_metadata.product_ref != product_ref
            or self.query_binding.product_ref != product_ref
            or self.product_metadata.generation != self.query_binding.generation
            or self.product_metadata.contract_ref != contract_ref
            or self.query_binding.contract_ref != contract_ref
            or self.product_metadata.semantic_version_ref != semantic_ref
            or self.query_binding.semantic_version_ref != semantic_ref
            or self.product_metadata.lineage_digest != self.query_binding.lineage_digest
        ):
            raise ValueError(
                "product catalog publication does not match approved product authority"
            )
        if (
            self.materialization_receipt_ref != self.product_metadata.materialization_receipt_ref
            or self.materialization_receipt_ref != self.query_binding.materialization_receipt_ref
            or self.materialization_receipt_digest != self.materialization_receipt_ref.digest
        ):
            raise ValueError("product catalog publication does not match materialization receipt")
        if (
            self.definition_authority.namespace != self.query_binding.namespace
            or self.definition_authority.relation_name != self.query_binding.relation_name
            or frozenset(column.name for column in self.definition_authority.columns)
            != expected_column_names
        ):
            raise ValueError("catalog definition does not match approved query binding")
        if self.source_freshness_observation_refs != expected_freshness_refs or len(
            expected_freshness_refs
        ) != len(set(expected_freshness_refs)):
            raise ValueError("source freshness references do not match exact observations")
        if source_refs != tuple(sorted(set(source_refs))):
            raise ValueError("source freshness observations must name canonical unique sources")
        if self.operation_id != _product_catalog_operation_id(
            tenant_id=self.tenant_id,
            catalog_binding_id=self.catalog_binding_id,
            catalog_binding_revision=self.catalog_binding_revision,
            product_ref=self.product_metadata.product_ref,
            generation=self.product_metadata.generation,
            definition_authority=self.definition_authority,
            warehouse_hierarchy=self.warehouse_hierarchy,
        ):
            raise ValueError("product catalog publication operation identity does not match")
        return self


class ProductCatalogPublicationReceipt(ArtifactModel):
    """Durable proof that the exact approved product definition was observed remotely."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    publication_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    operation_id: str = Field(pattern=_DIGEST_PATTERN)
    intent_digest: str = Field(pattern=_DIGEST_PATTERN)
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider_kind: Literal["openmetadata"]
    provider_version: str = Field(min_length=1)
    product_ref: ArtifactReference
    generation: int = Field(ge=1)
    observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    native_table_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    native_table_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    table_fully_qualified_name: str = Field(min_length=1)
    round_trip_verified: Literal[True] = True
    published_at: datetime

    @model_validator(mode="after")
    def publication_matches_product_generation(self) -> Self:
        if self.product_ref.version < 1:
            raise ValueError("published product reference must have a positive revision")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() != timedelta(0):
            raise ValueError("published_at must be timezone-aware UTC")
        object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))
        return self


class ProductCatalogPublicationProviderError(RuntimeError):
    def __init__(self, *, classification: ProviderErrorClassification) -> None:
        self.classification = classification
        super().__init__(f"product catalog publication failed: {classification}")


class ProductCatalogPublicationAuthorityError(RuntimeError):
    pass


class ProductCatalogPublicationProvider(CatalogProductProvider, Protocol):
    def publish_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation: ...

    def observe_native_table(
        self, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableObservation: ...


class ProductCatalogPublicationRepository(Protocol):
    def store(self, intent: ProductCatalogPublicationIntent) -> ProductCatalogPublicationIntent: ...

    def load(
        self, *, tenant_id: str, operation_id: str
    ) -> ProductCatalogPublicationIntent | None: ...

    def store_definition(
        self, *, intent: ProductCatalogPublicationIntent, definition: CatalogProductDefinition
    ) -> CatalogProductDefinition: ...

    def load_definition(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogProductDefinition | None: ...

    def store_native_table_definition(
        self, *, intent: ProductCatalogPublicationIntent, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableDefinition: ...

    def load_native_table_definition(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogNativeTableDefinition | None: ...

    def load_receipt(
        self, *, tenant_id: str, operation_id: str
    ) -> ProductCatalogPublicationReceipt | None: ...

    def read_for_product_generation(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductCatalogPublicationReceipt | None: ...

    def store_result(
        self,
        *,
        intent: ProductCatalogPublicationIntent,
        definition: CatalogProductDefinition,
        observation: CatalogProductObservation,
        native_table_definition: CatalogNativeTableDefinition,
        native_table_observation: CatalogNativeTableObservation,
        receipt: ProductCatalogPublicationReceipt,
    ) -> ProductCatalogPublicationReceipt: ...

    def load_observation(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogProductObservation: ...

    def load_native_table_observation(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogNativeTableObservation: ...


class ProductCatalogBindingAuthority(Protocol):
    def load(self, tenant_id: str, binding_id: str) -> CatalogBinding: ...


class SQLiteProductCatalogPublicationRepository:
    def __init__(self, database_path: str, *, check_same_thread: bool = True) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_catalog_publication_intents ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, "
            "catalog_binding_id TEXT NOT NULL, catalog_binding_revision INTEGER NOT NULL, "
            "product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
            "product_generation INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), "
            "UNIQUE (tenant_id, catalog_binding_id, catalog_binding_revision, "
            "product_id, product_revision, product_generation))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_catalog_publication_definitions ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), "
            "FOREIGN KEY (tenant_id, operation_id) REFERENCES "
            "product_catalog_publication_intents (tenant_id, operation_id))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_catalog_native_table_definitions ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), "
            "FOREIGN KEY (tenant_id, operation_id) REFERENCES "
            "product_catalog_publication_intents (tenant_id, operation_id))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_catalog_publication_receipts ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, publication_id TEXT NOT NULL, "
            "payload BLOB NOT NULL, observation BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), UNIQUE (tenant_id, publication_id), "
            "FOREIGN KEY (tenant_id, operation_id) REFERENCES "
            "product_catalog_publication_intents (tenant_id, operation_id))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_catalog_native_table_observations ("
            "tenant_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, operation_id), "
            "FOREIGN KEY (tenant_id, operation_id) REFERENCES "
            "product_catalog_publication_intents (tenant_id, operation_id))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, intent: ProductCatalogPublicationIntent) -> ProductCatalogPublicationIntent:
        intent = ProductCatalogPublicationIntent.model_validate(
            intent.model_dump(mode="python"), strict=True
        )
        payload = canonical_bytes(intent)
        identity = (intent.tenant_id, intent.operation_id)
        try:
            self._connection.execute(
                "INSERT INTO product_catalog_publication_intents ("
                "tenant_id, operation_id, catalog_binding_id, catalog_binding_revision, "
                "product_id, product_revision, product_generation, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, operation_id) DO NOTHING",
                (
                    *identity,
                    intent.catalog_binding_id,
                    intent.catalog_binding_revision,
                    intent.product_metadata.product_ref.artifact_id,
                    intent.product_metadata.product_ref.version,
                    intent.product_metadata.generation,
                    payload,
                ),
            )
            row = self._connection.execute(
                "SELECT payload FROM product_catalog_publication_intents "
                "WHERE tenant_id = ? AND operation_id = ?",
                identity,
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("product catalog publication authority is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return intent

    def load(self, *, tenant_id: str, operation_id: str) -> ProductCatalogPublicationIntent | None:
        row = self._connection.execute(
            "SELECT tenant_id, operation_id, catalog_binding_id, catalog_binding_revision, "
            "product_id, product_revision, product_generation, payload "
            "FROM product_catalog_publication_intents "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        intent = ProductCatalogPublicationIntent.model_validate_json(row[7])
        if (
            row[0] != intent.tenant_id
            or row[1] != intent.operation_id
            or row[2] != intent.catalog_binding_id
            or row[3] != intent.catalog_binding_revision
            or row[4] != intent.product_metadata.product_ref.artifact_id
            or row[5] != intent.product_metadata.product_ref.version
            or row[6] != intent.product_metadata.generation
            or intent.tenant_id != tenant_id
            or intent.operation_id != operation_id
        ):
            raise ValueError("product catalog publication index does not match its payload")
        return intent

    def store_definition(
        self, *, intent: ProductCatalogPublicationIntent, definition: CatalogProductDefinition
    ) -> CatalogProductDefinition:
        if definition != product_catalog_definition(intent):
            raise ValueError("product catalog definition does not match publication authority")
        payload = canonical_bytes(definition)
        identity = (intent.tenant_id, intent.operation_id)
        try:
            self._connection.execute(
                "INSERT INTO product_catalog_publication_definitions "
                "(tenant_id, operation_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, operation_id) DO NOTHING",
                (*identity, payload),
            )
            row = self._connection.execute(
                "SELECT payload FROM product_catalog_publication_definitions "
                "WHERE tenant_id = ? AND operation_id = ?",
                identity,
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("product catalog definition authority is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return definition

    def load_definition(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogProductDefinition | None:
        row = self._connection.execute(
            "SELECT payload FROM product_catalog_publication_definitions "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        return None if row is None else CatalogProductDefinition.model_validate_json(row[0])

    def store_native_table_definition(
        self, *, intent: ProductCatalogPublicationIntent, definition: CatalogNativeTableDefinition
    ) -> CatalogNativeTableDefinition:
        if definition != product_catalog_native_table_definition(intent):
            raise ValueError("native table definition does not match publication authority")
        payload = canonical_bytes(definition)
        identity = (intent.tenant_id, intent.operation_id)
        try:
            self._connection.execute(
                "INSERT INTO product_catalog_native_table_definitions "
                "(tenant_id, operation_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, operation_id) DO NOTHING",
                (*identity, payload),
            )
            row = self._connection.execute(
                "SELECT payload FROM product_catalog_native_table_definitions "
                "WHERE tenant_id = ? AND operation_id = ?",
                identity,
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("native table definition authority is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return definition

    def load_native_table_definition(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogNativeTableDefinition | None:
        row = self._connection.execute(
            "SELECT payload FROM product_catalog_native_table_definitions "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        return None if row is None else CatalogNativeTableDefinition.model_validate_json(row[0])

    def load_receipt(
        self, *, tenant_id: str, operation_id: str
    ) -> ProductCatalogPublicationReceipt | None:
        row = self._connection.execute(
            "SELECT tenant_id, operation_id, payload FROM product_catalog_publication_receipts "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        receipt = ProductCatalogPublicationReceipt.model_validate_json(row[2])
        if (
            row[0] != receipt.tenant_id
            or row[1] != receipt.operation_id
            or receipt.tenant_id != tenant_id
            or receipt.operation_id != operation_id
        ):
            raise ValueError("product catalog publication receipt index does not match payload")
        return receipt

    def read_for_product_generation(
        self, *, tenant_id: str, product_ref: ArtifactReference, generation: int
    ) -> ProductCatalogPublicationReceipt | None:
        try:
            rows = self._connection.execute(
                "SELECT intent.operation_id, intent.payload, receipt.payload "
                "FROM product_catalog_publication_intents AS intent "
                "INNER JOIN product_catalog_publication_receipts AS receipt "
                "ON receipt.tenant_id = intent.tenant_id "
                "AND receipt.operation_id = intent.operation_id "
                "WHERE intent.tenant_id = ? AND intent.product_id = ? "
                "AND intent.product_revision = ? AND intent.product_generation = ?",
                (tenant_id, product_ref.artifact_id, product_ref.version, generation),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ProductCatalogPublicationAuthorityError(
                    "product generation has ambiguous catalog publication authority"
                )
            operation_id, intent_payload, receipt_payload = rows[0]
            intent = ProductCatalogPublicationIntent.model_validate_json(intent_payload)
            receipt = ProductCatalogPublicationReceipt.model_validate_json(receipt_payload)
        except ProductCatalogPublicationAuthorityError:
            raise
        except (sqlite3.Error, ValidationError) as error:
            raise ProductCatalogPublicationAuthorityError(
                "product catalog publication authority is invalid or unavailable"
            ) from error
        if (
            intent.tenant_id != tenant_id
            or intent.operation_id != operation_id
            or intent.product_metadata.product_ref != product_ref
            or intent.product_metadata.generation != generation
            or receipt.tenant_id != tenant_id
            or receipt.operation_id != operation_id
            or receipt.product_ref != product_ref
            or receipt.generation != generation
            or receipt.intent_digest != digest(intent)
        ):
            raise ProductCatalogPublicationAuthorityError(
                "product catalog publication authority does not match its index"
            )
        return receipt

    def store_result(
        self,
        *,
        intent: ProductCatalogPublicationIntent,
        definition: CatalogProductDefinition,
        observation: CatalogProductObservation,
        native_table_definition: CatalogNativeTableDefinition,
        native_table_observation: CatalogNativeTableObservation,
        receipt: ProductCatalogPublicationReceipt,
    ) -> ProductCatalogPublicationReceipt:
        identity = (intent.tenant_id, intent.operation_id)
        _assert_publication_result(
            intent=intent,
            definition=definition,
            observation=observation,
            native_table_definition=native_table_definition,
            native_table_observation=native_table_observation,
            receipt=receipt,
        )
        try:
            stored_definition = self.load_definition(
                tenant_id=identity[0], operation_id=identity[1]
            )
            if stored_definition != definition:
                raise ValueError("product catalog definition authority is unavailable")
            stored_native_table = self.load_native_table_definition(
                tenant_id=identity[0], operation_id=identity[1]
            )
            if stored_native_table != native_table_definition:
                raise ValueError("native table definition authority is unavailable")
            self._connection.execute(
                "INSERT INTO product_catalog_native_table_observations "
                "(tenant_id, operation_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, operation_id) DO NOTHING",
                (*identity, canonical_bytes(native_table_observation)),
            )
            table_row = self._connection.execute(
                "SELECT payload FROM product_catalog_native_table_observations "
                "WHERE tenant_id = ? AND operation_id = ?",
                identity,
            ).fetchone()
            if table_row is None or bytes(table_row[0]) != canonical_bytes(
                native_table_observation
            ):
                raise ValueError("native table publication result is immutable")
            self._connection.execute(
                "INSERT INTO product_catalog_publication_receipts "
                "(tenant_id, operation_id, publication_id, payload, observation) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, operation_id) DO NOTHING",
                (
                    *identity,
                    receipt.publication_id,
                    canonical_bytes(receipt),
                    canonical_bytes(observation),
                ),
            )
            row = self._connection.execute(
                "SELECT payload, observation FROM product_catalog_publication_receipts "
                "WHERE tenant_id = ? AND operation_id = ?",
                identity,
            ).fetchone()
            if row is None or (
                bytes(row[0]) != canonical_bytes(receipt)
                or bytes(row[1]) != canonical_bytes(observation)
            ):
                raise ValueError("product catalog publication result is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return receipt

    def load_observation(self, *, tenant_id: str, operation_id: str) -> CatalogProductObservation:
        row = self._connection.execute(
            "SELECT tenant_id, operation_id, observation "
            "FROM product_catalog_publication_receipts "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, operation_id))
        observation = CatalogProductObservation.model_validate_json(row[2])
        if row[0] != tenant_id or row[1] != operation_id or observation.tenant_id != tenant_id:
            raise ValueError("product catalog observation index does not match payload")
        return observation

    def load_native_table_observation(
        self, *, tenant_id: str, operation_id: str
    ) -> CatalogNativeTableObservation:
        row = self._connection.execute(
            "SELECT payload FROM product_catalog_native_table_observations "
            "WHERE tenant_id = ? AND operation_id = ?",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            raise KeyError((tenant_id, operation_id))
        observation = CatalogNativeTableObservation.model_validate_json(row[0])
        if observation.definition.product.tenant_id != tenant_id:
            raise ValueError("native table observation index does not match payload")
        return observation

    def definition_for_reference(
        self, *, tenant_id: str, product_ref: ArtifactReference
    ) -> CatalogProductDefinition | None:
        rows = self._connection.execute(
            "SELECT intents.operation_id, intents.product_generation, intents.payload, "
            "definitions.payload FROM product_catalog_publication_intents AS intents "
            "JOIN product_catalog_publication_definitions AS definitions "
            "ON definitions.tenant_id = intents.tenant_id "
            "AND definitions.operation_id = intents.operation_id "
            "JOIN product_catalog_publication_receipts AS receipts "
            "ON receipts.tenant_id = intents.tenant_id "
            "AND receipts.operation_id = intents.operation_id "
            "WHERE intents.tenant_id = ? AND intents.product_id = ? "
            "AND intents.product_revision = ? ORDER BY intents.product_generation DESC",
            (tenant_id, product_ref.artifact_id, product_ref.version),
        ).fetchall()
        for operation_id, generation, intent_payload, definition_payload in rows:
            intent = ProductCatalogPublicationIntent.model_validate_json(intent_payload)
            definition = CatalogProductDefinition.model_validate_json(definition_payload)
            if (
                intent.tenant_id != tenant_id
                or intent.operation_id != operation_id
                or intent.product_metadata.generation != generation
                or definition != product_catalog_definition(intent)
            ):
                raise ValueError("product catalog publication index does not match its payload")
            if intent.product_metadata.product_ref == product_ref:
                return definition
        return None


class ProductCatalogPublicationService:
    def __init__(
        self,
        *,
        repository: ProductCatalogPublicationRepository,
        binding_authority: ProductCatalogBindingAuthority,
    ) -> None:
        self._repository = repository
        self._binding_authority = binding_authority

    def prepare(
        self,
        *,
        binding: CatalogBinding,
        warehouse_hierarchy: CatalogWarehouseHierarchyAuthority,
        definition_authority: ProductCatalogDefinitionAuthority,
        product_metadata: ApprovedProductVersionMetadata,
        query_binding: ApprovedProductQueryBinding,
        materialization_receipt_ref: ArtifactReference,
        materialization_receipt_digest: str,
        source_freshness_observations: tuple[SourceFreshnessObservation, ...],
        contract: ManagedIntegrationContract,
        semantic_version: ApprovedSemanticVersion,
    ) -> ProductCatalogPublicationIntent:
        binding = CatalogBinding.model_validate(binding.model_dump(mode="python"), strict=True)
        try:
            current_binding = CatalogBinding.model_validate(
                self._binding_authority.load(binding.tenant_id, binding.binding_id), strict=True
            )
        except (CatalogPersistenceError, KeyError, ValidationError):
            raise ValueError("catalog binding authority is unavailable") from None
        if current_binding != binding:
            raise ValueError("catalog binding revision is stale")
        if binding.lifecycle_state is not CatalogBindingState.READY:
            raise ValueError("catalog binding must be ready for product publication")
        operation_id = _product_catalog_operation_id(
            tenant_id=binding.tenant_id,
            catalog_binding_id=binding.binding_id,
            catalog_binding_revision=binding.revision,
            product_ref=product_metadata.product_ref,
            generation=product_metadata.generation,
            definition_authority=definition_authority,
            warehouse_hierarchy=warehouse_hierarchy,
        )
        intent = ProductCatalogPublicationIntent(
            operation_id=operation_id,
            tenant_id=binding.tenant_id,
            catalog_binding_id=binding.binding_id,
            catalog_binding_revision=binding.revision,
            warehouse_hierarchy=warehouse_hierarchy,
            definition_authority=definition_authority,
            product_metadata=product_metadata,
            query_binding=query_binding,
            materialization_receipt_ref=materialization_receipt_ref,
            materialization_receipt_digest=materialization_receipt_digest,
            source_freshness_observation_refs=tuple(
                ArtifactReference(
                    artifact_id=observation.observation_id,
                    version=observation.version,
                    digest=digest(observation),
                )
                for observation in source_freshness_observations
            ),
            source_freshness_observations=source_freshness_observations,
            contract=contract,
            semantic_version=semantic_version,
        )
        return self._repository.store(intent)


class ProductCatalogPublicationExecutionService:
    """Publish one durable product authority without invoking its materialization path."""

    def __init__(
        self,
        *,
        repository: ProductCatalogPublicationRepository,
        provider: ProductCatalogPublicationProvider,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._clock = clock

    def publish(
        self, *, intent: ProductCatalogPublicationIntent
    ) -> ProductCatalogPublicationReceipt:
        intent = ProductCatalogPublicationIntent.model_validate(
            intent.model_dump(mode="python"), strict=True
        )
        persisted_intent = self._repository.load(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
        if persisted_intent is None:
            raise ValueError("product catalog publication authority must be persisted first")
        if persisted_intent != intent:
            raise ValueError("persisted product catalog publication authority differs from input")
        existing = self._repository.load_receipt(
            tenant_id=intent.tenant_id, operation_id=intent.operation_id
        )
        if existing is not None:
            return existing

        definition = product_catalog_definition(intent)
        native_table_definition = product_catalog_native_table_definition(intent)
        self._repository.store_definition(intent=intent, definition=definition)
        self._repository.store_native_table_definition(
            intent=intent, definition=native_table_definition
        )
        try:
            published_observation = self._provider.publish(definition)
            published_table_observation = self._provider.publish_native_table(
                native_table_definition
            )
            round_trip_observation = self._provider.observe(
                tenant_id=intent.tenant_id,
                stable_external_key=definition.stable_external_key,
            )
            round_trip_table_observation = self._provider.observe_native_table(
                native_table_definition
            )
        except ProviderError as error:
            raise ProductCatalogPublicationProviderError(
                classification=error.classification
            ) from None
        if (
            published_observation != round_trip_observation
            or round_trip_observation.definition != definition
            or published_table_observation != round_trip_table_observation
            or round_trip_table_observation.definition != native_table_definition
            or round_trip_table_observation.provider_version
            != round_trip_observation.provider_version
        ):
            raise ProductCatalogPublicationProviderError(classification="invalid_provider_response")
        published_at = self._clock()
        if published_at.tzinfo is None or published_at.utcoffset() != timedelta(0):
            raise ValueError("publication clock must return timezone-aware UTC")
        receipt = ProductCatalogPublicationReceipt(
            publication_id="product-publication-" + intent.operation_id[:24],
            tenant_id=intent.tenant_id,
            operation_id=intent.operation_id,
            intent_digest=digest(intent),
            definition_digest=digest(definition),
            provider_kind=self._provider.provider_kind,
            provider_version=round_trip_observation.provider_version,
            product_ref=intent.product_metadata.product_ref,
            generation=intent.product_metadata.generation,
            observation_digest=digest(round_trip_observation),
            native_table_definition_digest=digest(native_table_definition),
            native_table_observation_digest=digest(round_trip_table_observation),
            table_fully_qualified_name=round_trip_table_observation.table_fully_qualified_name,
            published_at=published_at.astimezone(UTC),
        )
        return self._repository.store_result(
            intent=intent,
            definition=definition,
            observation=round_trip_observation,
            native_table_definition=native_table_definition,
            native_table_observation=round_trip_table_observation,
            receipt=receipt,
        )


def product_catalog_definition(
    intent: ProductCatalogPublicationIntent,
) -> CatalogProductDefinition:
    authority = intent.definition_authority
    lineage_sources = tuple(
        sorted(
            (
                CatalogLineageSource(
                    source_ref=observation.source_ref,
                    freshness_observation_ref=reference,
                    freshness_observation_digest=reference.digest,
                    watermark_at=observation.watermark_at,
                    observed_at=observation.observed_at,
                )
                for reference, observation in zip(
                    intent.source_freshness_observation_refs,
                    intent.source_freshness_observations,
                    strict=True,
                )
            ),
            key=lambda source: source.source_ref,
        )
    )
    return CatalogProductDefinition(
        tenant_id=intent.tenant_id,
        idempotency_key=intent.operation_id,
        stable_external_key=catalog_product_external_key(
            tenant_id=intent.tenant_id,
            product_id=intent.product_metadata.product_ref.artifact_id,
            product_revision=intent.product_metadata.product_ref.version,
            generation=intent.product_metadata.generation,
        ),
        catalog_binding_id=intent.catalog_binding_id,
        catalog_revision=intent.catalog_binding_revision,
        product_id=intent.product_metadata.product_ref.artifact_id,
        product_revision=intent.product_metadata.product_ref.version,
        generation=intent.product_metadata.generation,
        name=authority.name,
        description=authority.description,
        owner_refs=authority.owner_refs,
        namespace=authority.namespace,
        relation_name=authority.relation_name,
        columns=tuple(
            CatalogColumn(
                name=column.name,
                type_name=column.type_name,
                nullable=column.nullable,
                description=column.description,
            )
            for column in authority.columns
        ),
        lineage_sources=lineage_sources,
        contract_digest=digest(intent.contract),
        semantic_version_digest=digest(intent.semantic_version),
        materialization_receipt_ref=intent.materialization_receipt_ref,
        materialization_receipt_digest=intent.materialization_receipt_digest,
        publication_authority_digest=digest(intent),
    )


def product_catalog_native_table_definition(
    intent: ProductCatalogPublicationIntent,
) -> CatalogNativeTableDefinition:
    return CatalogNativeTableDefinition(
        product=product_catalog_definition(intent),
        warehouse=intent.warehouse_hierarchy,
    )


def _product_catalog_operation_id(
    *,
    tenant_id: str,
    catalog_binding_id: str,
    catalog_binding_revision: int,
    product_ref: ArtifactReference,
    generation: int,
    definition_authority: ProductCatalogDefinitionAuthority,
    warehouse_hierarchy: CatalogWarehouseHierarchyAuthority,
) -> str:
    return digest(
        {
            "tenant_id": tenant_id,
            "catalog_binding_id": catalog_binding_id,
            "catalog_binding_revision": catalog_binding_revision,
            "product_ref": product_ref,
            "generation": generation,
            "definition_authority_digest": digest(definition_authority),
            "warehouse_hierarchy_digest": digest(warehouse_hierarchy),
        }
    )


def _assert_publication_result(
    *,
    intent: ProductCatalogPublicationIntent,
    definition: CatalogProductDefinition,
    observation: CatalogProductObservation,
    native_table_definition: CatalogNativeTableDefinition,
    native_table_observation: CatalogNativeTableObservation,
    receipt: ProductCatalogPublicationReceipt,
) -> None:
    expected_receipt_identity = (
        "product-publication-" + intent.operation_id[:24],
        intent.tenant_id,
        intent.operation_id,
        digest(intent),
        digest(definition),
        intent.product_metadata.product_ref,
        intent.product_metadata.generation,
        observation.provider_version,
        digest(observation),
        digest(native_table_definition),
        digest(native_table_observation),
        native_table_observation.table_fully_qualified_name,
    )
    actual_receipt_identity = (
        receipt.publication_id,
        receipt.tenant_id,
        receipt.operation_id,
        receipt.intent_digest,
        receipt.definition_digest,
        receipt.product_ref,
        receipt.generation,
        receipt.provider_version,
        receipt.observation_digest,
        receipt.native_table_definition_digest,
        receipt.native_table_observation_digest,
        receipt.table_fully_qualified_name,
    )
    if definition != product_catalog_definition(intent):
        raise ValueError("product catalog definition does not match publication authority")
    if observation.definition != definition or observation.tenant_id != intent.tenant_id:
        raise ValueError("product catalog observation does not match publication authority")
    if (
        native_table_definition != product_catalog_native_table_definition(intent)
        or native_table_observation.definition != native_table_definition
    ):
        raise ValueError("native table observation does not match publication authority")
    if actual_receipt_identity != expected_receipt_identity:
        raise ValueError("product catalog receipt does not match publication result")
