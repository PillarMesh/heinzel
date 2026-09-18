from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from heinzel_compiler import ProductGenerationReference
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import SourceFreshnessObservation
from heinzel_runtime import (
    AnswerProductGenerationReference,
    ProductMaterializationReceipt,
    QueryGenerationState,
)
from heinzel_semantic_registry import ApprovedProductVersionMetadata

from tests.acceptance.console_answer_authority import ProductAnswerAuthority


class MaterializationReceiptReader(Protocol):
    def read_receipt(
        self,
        *,
        tenant_id: str,
        product_id: str,
        product_revision: int,
        product_generation: int,
    ) -> ProductMaterializationReceipt | None: ...


class SourceFreshnessReader(Protocol):
    def read_for_generation(
        self, *, tenant_id: str, input_generation_digest: str
    ) -> SourceFreshnessObservation | None: ...


class ProductGenerationAuthorityReader(Protocol):
    def observe(self, reference: AnswerProductGenerationReference) -> QueryGenerationState: ...


class ApprovedProductAnswerMetadataReader(Protocol):
    def read_current(
        self,
        *,
        tenant_id: str,
        product_ref: ArtifactReference,
        generation: int,
    ) -> ApprovedProductVersionMetadata | None: ...


class DurableProductAnswerAuthorityReader:
    def __init__(
        self,
        *,
        materializations: MaterializationReceiptReader,
        freshness: SourceFreshnessReader,
        product_metadata: ApprovedProductAnswerMetadataReader,
        generations: ProductGenerationAuthorityReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._materializations = materializations
        self._freshness = freshness
        self._product_metadata = product_metadata
        self._generations = generations
        self._clock = clock

    def read_current(
        self,
        *,
        tenant_id: str,
        product_generation_refs: tuple[ProductGenerationReference, ...],
    ) -> ProductAnswerAuthority | None:
        now = self._now()
        if not product_generation_refs or len(set(product_generation_refs)) != len(
            product_generation_refs
        ):
            return None

        receipts: list[ProductMaterializationReceipt] = []
        freshness_observations: list[SourceFreshnessObservation] = []
        lineage_refs: list[ArtifactReference] = []
        limitations: list[ArtifactReference] = []
        narrative_terms: list[str] = []

        for reference in product_generation_refs:
            receipt = self._materializations.read_receipt(
                tenant_id=tenant_id,
                product_id=reference.product_ref.artifact_id,
                product_revision=reference.product_ref.version,
                product_generation=reference.generation,
            )
            if receipt is None or receipt.retained_until <= now:
                return None
            if (
                receipt.quality_assertion_count == 0
                or receipt.quality_disposition == "not_asserted"
            ):
                return None

            metadata = self._product_metadata.read_current(
                tenant_id=tenant_id,
                product_ref=ArtifactReference.model_validate(
                    reference.product_ref.model_dump(mode="python")
                ),
                generation=reference.generation,
            )
            receipt_ref = ArtifactReference(
                artifact_id=receipt.run_id,
                version=receipt.product_generation,
                digest=digest(receipt),
            )
            if (
                metadata is None
                or metadata.tenant_id != tenant_id
                or metadata.product_ref.model_dump(mode="python")
                != reference.product_ref.model_dump(mode="python")
                or metadata.generation != reference.generation
                or metadata.contract_ref.digest != receipt.contract_digest
                or metadata.materialization_receipt_ref != receipt_ref
                or metadata.lineage_digest != receipt.lineage_digest
            ):
                return None

            generation_state = self._generations.observe(
                AnswerProductGenerationReference.model_validate(reference.model_dump(mode="python"))
            )
            if not generation_state.addressable:
                return None

            observations = self._read_freshness(
                tenant_id=tenant_id,
                input_generation_digests=receipt.input_generation_digests,
                now=now,
            )
            if observations is None:
                return None

            receipts.append(receipt)
            freshness_observations.extend(observations)
            lineage_refs.append(
                ArtifactReference(
                    artifact_id=f"{reference.product_ref.artifact_id}:lineage",
                    version=reference.generation,
                    digest=receipt.lineage_digest,
                )
            )
            if receipt.quality_disposition == "limited":
                limitations.append(
                    ArtifactReference(
                        artifact_id=f"{reference.product_ref.artifact_id}:dbt-run-results",
                        version=reference.generation,
                        digest=receipt.dbt_run_results_digest,
                    )
                )
            for term in metadata.approved_narrative_terms:
                if term not in narrative_terms:
                    narrative_terms.append(term)

        return ProductAnswerAuthority(
            tenant_id=tenant_id,
            product_generation_refs=product_generation_refs,
            freshness_observation_ref=digest(
                {
                    "domain": "heinzel-source-freshness-set-v1",
                    "observations": tuple(freshness_observations),
                }
            ),
            quality_observation_ref=digest(
                {
                    "domain": "heinzel-product-quality-set-v1",
                    "receipts": tuple(receipts),
                }
            ),
            freshness_disposition="current",
            quality_blocked=bool(limitations),
            material_quality_limitations=tuple(limitations),
            lineage_refs=tuple(lineage_refs),
            as_of=min(observation.watermark_at for observation in freshness_observations),
            approved_narrative_terms=tuple(narrative_terms),
        )

    def _read_freshness(
        self,
        *,
        tenant_id: str,
        input_generation_digests: tuple[str, ...],
        now: datetime,
    ) -> tuple[SourceFreshnessObservation, ...] | None:
        observations: list[SourceFreshnessObservation] = []
        for generation_digest in input_generation_digests:
            observation = self._freshness.read_for_generation(
                tenant_id=tenant_id, input_generation_digest=generation_digest
            )
            if (
                observation is None
                or observation.tenant_id != tenant_id
                or observation.input_generation_digest != generation_digest
                or observation.observed_at > now
                or observation.watermark_at > now
            ):
                return None
            observations.append(observation)
        return tuple(observations)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("clock must return a timezone-aware UTC timestamp")
        return value.astimezone(UTC)
