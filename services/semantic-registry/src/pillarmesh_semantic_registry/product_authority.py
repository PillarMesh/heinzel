from __future__ import annotations

import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ManagedIntegrationContract,
    SemanticObject,
    canonical_bytes,
    digest,
)
from pydantic import ConfigDict, Field, field_validator, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class MaterializedProductEvidence(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    product_ref: ArtifactReference
    generation: int = Field(ge=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    materialization_receipt_ref: ArtifactReference
    lineage_digest: str = Field(pattern=_DIGEST_PATTERN)
    materialized_at: datetime

    @field_validator("materialized_at")
    @classmethod
    def _materialized_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("materialized_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _receipt_names_generation(self) -> MaterializedProductEvidence:
        if self.materialization_receipt_ref.version != self.generation:
            raise ValueError("materialization receipt version must match the product generation")
        return self


class ApprovedProductVersionMetadata(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    product_ref: ArtifactReference
    generation: int = Field(ge=1)
    contract_ref: ArtifactReference
    semantic_version_ref: ArtifactReference
    materialization_receipt_ref: ArtifactReference
    lineage_digest: str = Field(pattern=_DIGEST_PATTERN)
    approved_narrative_terms: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _receipt_names_generation(self) -> ApprovedProductVersionMetadata:
        if self.materialization_receipt_ref.version != self.generation:
            raise ValueError("materialization receipt version must match the product generation")
        if len(set(self.approved_narrative_terms)) != len(self.approved_narrative_terms):
            raise ValueError("approved narrative terms must be unique")
        if any(not value.strip() for value in self.approved_narrative_terms):
            raise ValueError("approved narrative terms must be nonempty")
        return self


class ApprovedProductVersionRepository(Protocol):
    def store(self, metadata: ApprovedProductVersionMetadata) -> ApprovedProductVersionMetadata: ...

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductVersionMetadata | None: ...


class SQLiteApprovedProductVersionRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS approved_product_versions ("
            "tenant_id TEXT NOT NULL, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, "
            "generation INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, product_id, product_revision, generation))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def store(self, metadata: ApprovedProductVersionMetadata) -> ApprovedProductVersionMetadata:
        metadata = ApprovedProductVersionMetadata.model_validate(
            metadata.model_dump(mode="python"), strict=True
        )
        payload = canonical_bytes(metadata)
        identity = (
            metadata.tenant_id,
            metadata.product_ref.artifact_id,
            metadata.product_ref.version,
            metadata.generation,
        )
        try:
            self._connection.execute(
                "INSERT INTO approved_product_versions "
                "(tenant_id, product_id, product_revision, generation, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, product_id, product_revision, generation) DO NOTHING",
                (*identity, payload),
            )
            row = self._connection.execute(
                "SELECT payload FROM approved_product_versions "
                "WHERE tenant_id = ? AND product_id = ? AND product_revision = ? "
                "AND generation = ?",
                identity,
            ).fetchone()
            if row is None or bytes(row[0]) != payload:
                raise ValueError("product version authority is immutable")
            self._connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        return metadata

    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductVersionMetadata | None:
        row = self._connection.execute(
            "SELECT tenant_id, product_id, product_revision, generation, payload "
            "FROM approved_product_versions WHERE tenant_id = ? AND product_id = ? "
            "AND product_revision = ? AND generation = ?",
            (tenant_id, product_ref.artifact_id, product_ref.version, generation),
        ).fetchone()
        if row is None:
            return None
        metadata = ApprovedProductVersionMetadata.model_validate_json(row[4])
        if (
            row[0] != metadata.tenant_id
            or row[1] != metadata.product_ref.artifact_id
            or row[2] != metadata.product_ref.version
            or row[3] != metadata.generation
            or metadata.tenant_id != tenant_id
            or metadata.product_ref != product_ref
            or metadata.generation != generation
        ):
            raise ValueError("product version authority index does not match its payload")
        return metadata


class ProductVersionAuthorityService:
    def __init__(self, repository: ApprovedProductVersionRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        contract: ManagedIntegrationContract,
        semantic_version: ApprovedSemanticVersion,
        materialization: MaterializedProductEvidence,
    ) -> ApprovedProductVersionMetadata:
        contract = ManagedIntegrationContract.model_validate(
            contract.model_dump(mode="python"), strict=True
        )
        semantic_version = ApprovedSemanticVersion.model_validate(
            semantic_version.model_dump(mode="python"), strict=True
        )
        materialization = MaterializedProductEvidence.model_validate(
            materialization.model_dump(mode="python"), strict=True
        )
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
        if not semantic_version.approval_ids or contract.semantic_version_ref != semantic_ref:
            raise ValueError("contract does not bind an approved semantic version")
        if not contract.approval_ids:
            raise ValueError("product contract is not approved")
        if (
            contract.tenant_id != semantic_version.tenant_id
            or materialization.tenant_id != contract.tenant_id
            or materialization.product_ref != product_ref
            or materialization.contract_digest != digest(contract)
        ):
            raise ValueError("materialization does not match approved product authority")

        return self._repository.store(
            ApprovedProductVersionMetadata(
                tenant_id=contract.tenant_id,
                product_ref=product_ref,
                generation=materialization.generation,
                contract_ref=ArtifactReference(
                    artifact_id=contract.contract_id,
                    version=contract.version,
                    digest=digest(contract),
                ),
                semantic_version_ref=semantic_ref,
                materialization_receipt_ref=materialization.materialization_receipt_ref,
                lineage_digest=materialization.lineage_digest,
                approved_narrative_terms=_approved_narrative_terms(
                    contract.destination_product.product_name, semantic_version
                ),
                recorded_at=materialization.materialized_at,
            )
        )


def _approved_narrative_terms(
    product_name: str, semantic_version: ApprovedSemanticVersion
) -> tuple[str, ...]:
    objects: tuple[SemanticObject, ...] = (
        *semantic_version.entities,
        *semantic_version.events,
        *semantic_version.states,
        *semantic_version.relationships,
        *semantic_version.metrics,
        *semantic_version.classifications,
    )
    values = [product_name]
    for item in objects:
        values.extend((item.name, item.definition))
    return tuple(dict.fromkeys(value for value in values if value.strip()))
