from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self

from heinzel_contract_model import (
    ApprovedSemanticVersion,
    ArtifactModel,
    ArtifactReference,
    ContractFormationStatus,
    ManagedIntegrationContract,
    SemanticObject,
    digest,
)
from heinzel_request_management import (
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    InboxRequest,
    ResolutionFailure,
)
from pydantic import Field, field_validator, model_validator

from .publication import CatalogPublicationRepository

type FulfillmentAccessMode = Literal["query", "dashboard", "export"]


class FulfillmentAuthorityObservation(ArtifactModel):
    """Approved policy and observation inputs selected for one request."""

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    catalog_publication_id: str = Field(min_length=1)
    requester_id: str = Field(min_length=1)
    requester_principal_ref: str = Field(min_length=1)
    purpose_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_policy_ref: ArtifactReference
    approved_policy_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    entitlement_observation_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    classification_rule_refs: tuple[ArtifactReference, ...]
    permitted_data_product_refs: tuple[ArtifactReference, ...]
    permitted_access_modes: tuple[FulfillmentAccessMode, ...]
    maximum_expiry: datetime | None
    policy_authority_classifications: tuple[str, ...]
    freshness_observation_ref: ArtifactReference | None
    quality_observation_refs: tuple[ArtifactReference, ...]
    data_observation_refs: tuple[ArtifactReference, ...]
    observed_at: datetime
    valid_until: datetime

    @field_validator("maximum_expiry", "observed_at", "valid_until")
    @classmethod
    def requires_timezone_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("authority timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validates_authority_window_and_references(self) -> Self:
        if self.valid_until <= self.observed_at:
            raise ValueError("authority validity window must be positive")
        if self.maximum_expiry is not None and self.maximum_expiry < self.observed_at:
            raise ValueError("maximum expiry must not precede the authority observation")
        if self.authorization_policy_ref not in self.approved_policy_refs:
            raise ValueError("authorization policy must be approved")
        for field_name in (
            "approved_policy_refs",
            "entitlement_observation_refs",
            "classification_rule_refs",
            "permitted_data_product_refs",
            "permitted_access_modes",
            "policy_authority_classifications",
            "quality_observation_refs",
            "data_observation_refs",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        return self


class FulfillmentAuthorityResolver(Protocol):
    def resolve(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
    ) -> FulfillmentAuthorityObservation | ResolutionFailure: ...


class SemanticFulfillmentSnapshotAdapter:
    def __init__(
        self,
        *,
        publication_repository: CatalogPublicationRepository,
        authority_resolver: FulfillmentAuthorityResolver,
        clock: Callable[[], datetime],
    ) -> None:
        self._publication_repository = publication_repository
        self._authority_resolver = authority_resolver
        self._clock = clock

    def resolve(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
    ) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot] | ResolutionFailure:
        if request.tenant_id != tenant_id:
            return _failure("request_tenant_mismatch", "Use a request from the selected tenant.")
        try:
            authority = self._authority_resolver.resolve(tenant_id=tenant_id, request=request)
        except (KeyError, ValueError):
            return _failure(
                "authority_resolution_failed",
                "Resolve one current approved policy observation.",
            )
        if isinstance(authority, ResolutionFailure):
            return authority
        authority_failure = self._validate_authority(
            tenant_id=tenant_id,
            request=request,
            authority=authority,
        )
        if authority_failure is not None:
            return authority_failure

        try:
            intent, receipt, _private_references = self._publication_repository.load_publication(
                tenant_id=tenant_id,
                publication_id=authority.catalog_publication_id,
            )
            semantic_version, integration_contract = self._publication_repository.load_inputs(
                tenant_id=tenant_id,
                operation_id=intent.operation_id,
            )
            persisted_observations = self._publication_repository.load_observations(
                tenant_id=tenant_id,
                operation_id=intent.operation_id,
            )
        except KeyError:
            return _failure(
                "publication_not_found",
                "Publish one approved semantic version and integration contract.",
            )
        except (TypeError, ValueError):
            return _failure(
                "publication_integrity_failure",
                "Repair the immutable publication record.",
            )

        if not self._publication_is_exact(
            tenant_id=tenant_id,
            intent=intent,
            receipt=receipt,
            semantic_version=semantic_version,
            integration_contract=integration_contract,
            persisted_observations=persisted_observations,
        ):
            return _failure(
                "publication_integrity_failure",
                "Repair the immutable publication record.",
            )

        governed_products = (_product_reference(integration_contract),)
        if any(
            reference not in governed_products
            for reference in authority.permitted_data_product_refs
        ):
            return _failure(
                "policy_product_unreachable",
                "Bind policy only to a product in the approved contract.",
            )
        admitted_classifications = {
            semantic_object.object_id for semantic_object in semantic_version.classifications
        }
        if any(
            classification not in admitted_classifications
            for classification in authority.policy_authority_classifications
        ):
            return _failure(
                "policy_classification_unreachable",
                "Bind policy only to an approved semantic classification.",
            )

        semantic_reference = _artifact_reference(
            semantic_version.semantic_version_id,
            semantic_version.version,
            semantic_version,
        )
        contract_reference = _artifact_reference(
            integration_contract.contract_id,
            integration_contract.version,
            integration_contract,
        )
        grounding_payload = {
            "tenant_id": tenant_id,
            "publication_id": receipt.publication_id,
            "intent_digest": digest(intent),
            "round_trip_digest": receipt.round_trip_observation_digest,
            "semantic_reference": semantic_reference,
            "contract_reference": contract_reference,
            "authority_digest": digest(authority),
        }
        grounding = FulfillmentGroundingSnapshot(
            snapshot_id="grounding-" + digest(grounding_payload)[:24],
            tenant_id=tenant_id,
            catalog_publication_id=receipt.publication_id,
            catalog_publication_intent_digest=digest(intent),
            catalog_round_trip_observation_digest=receipt.round_trip_observation_digest,
            semantic_version_ref=semantic_reference,
            integration_contract_ref=contract_reference,
            process_package_ref=semantic_version.process_package_ref,
            governed_dataset_refs=governed_products,
            metric_refs=_semantic_references(semantic_version.metrics, semantic_version.version),
            classification_refs=_semantic_references(
                semantic_version.classifications, semantic_version.version
            ),
            lineage_refs=tuple(
                _artifact_reference(
                    "lineage-" + digest(mapping)[:24],
                    integration_contract.version,
                    mapping,
                )
                for mapping in sorted(integration_contract.mappings, key=digest)
            ),
            freshness_observation_ref=authority.freshness_observation_ref,
            quality_observation_refs=_sorted_references(authority.quality_observation_refs),
            authorization_policy_ref=authority.authorization_policy_ref,
            data_observation_refs=_sorted_references(authority.data_observation_refs),
            as_of=authority.observed_at,
            created_at=authority.observed_at,
        )
        policy = FulfillmentPolicySnapshot(
            snapshot_id="fulfillment-policy-" + digest(authority)[:24],
            tenant_id=tenant_id,
            requester_id=authority.requester_id,
            requester_principal_ref=authority.requester_principal_ref,
            purpose_digest=authority.purpose_digest,
            approved_policy_refs=_sorted_references(authority.approved_policy_refs),
            entitlement_observation_refs=_sorted_references(authority.entitlement_observation_refs),
            classification_rule_refs=_sorted_references(authority.classification_rule_refs),
            permitted_data_product_refs=_sorted_references(authority.permitted_data_product_refs),
            permitted_access_modes=tuple(sorted(authority.permitted_access_modes)),
            maximum_expiry=authority.maximum_expiry,
            policy_authority_classifications=tuple(
                sorted(authority.policy_authority_classifications)
            ),
            observed_at=authority.observed_at,
            valid_until=authority.valid_until,
        )
        return grounding, policy

    def _validate_authority(
        self,
        *,
        tenant_id: str,
        request: InboxRequest,
        authority: FulfillmentAuthorityObservation,
    ) -> ResolutionFailure | None:
        if authority.tenant_id != tenant_id:
            return _failure(
                "authority_tenant_mismatch",
                "Resolve policy authority in the selected tenant.",
            )
        if authority.requester_id != request.requester_id:
            return _failure(
                "authority_requester_mismatch",
                "Resolve policy authority for the request's requester.",
            )
        if authority.purpose_digest != digest(request.payload.purpose):
            return _failure(
                "authority_purpose_mismatch",
                "Resolve policy authority for the request's current purpose.",
            )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            return _failure("invalid_clock", "Use a timezone-aware UTC clock.")
        if authority.observed_at > now.astimezone(UTC):
            return _failure(
                "entitlement_not_yet_valid",
                "Resolve an authority observation that is already effective.",
            )
        if authority.valid_until <= now.astimezone(UTC):
            return _failure(
                "entitlement_expired",
                "Resolve a current approved entitlement observation.",
            )
        return None

    @staticmethod
    def _publication_is_exact(
        *,
        tenant_id: str,
        intent: object,
        receipt: object,
        semantic_version: ApprovedSemanticVersion,
        integration_contract: ManagedIntegrationContract,
        persisted_observations: tuple[object, ...],
    ) -> bool:
        from .publication import CatalogPublicationIntent, CatalogPublicationReceipt

        if not isinstance(intent, CatalogPublicationIntent) or not isinstance(
            receipt, CatalogPublicationReceipt
        ):
            return False
        semantic_reference = _artifact_reference(
            semantic_version.semantic_version_id,
            semantic_version.version,
            semantic_version,
        )
        contract_reference = _artifact_reference(
            integration_contract.contract_id,
            integration_contract.version,
            integration_contract,
        )
        return bool(
            intent.tenant_id
            == receipt.tenant_id
            == semantic_version.tenant_id
            == integration_contract.tenant_id
            == tenant_id
            and receipt.intent_digest == digest(intent)
            and receipt.round_trip_observation_digest == digest(persisted_observations)
            and intent.semantic_version_digest == digest(semantic_version)
            and intent.contract_digest == digest(integration_contract)
            and intent.contract_reference == contract_reference
            and integration_contract.semantic_version_ref == semantic_reference
            and integration_contract.formation_status is ContractFormationStatus.READY_TO_ACTIVATE
            and bool(semantic_version.approval_ids)
            and bool(integration_contract.approval_ids)
            and bool(receipt.published_refs)
            and all(reference == semantic_reference for reference in receipt.published_refs)
        )


def _artifact_reference(artifact_id: str, version: int, value: object) -> ArtifactReference:
    return ArtifactReference(artifact_id=artifact_id, version=version, digest=digest(value))


def _product_reference(contract: ManagedIntegrationContract) -> ArtifactReference:
    return _artifact_reference(
        contract.destination_product.product_name,
        contract.version,
        contract.destination_product,
    )


def _semantic_references(
    semantic_objects: tuple[SemanticObject, ...], version: int
) -> tuple[ArtifactReference, ...]:
    return tuple(
        _artifact_reference(item.object_id, version, item)
        for item in sorted(semantic_objects, key=lambda item: item.object_id)
    )


def _sorted_references(
    references: tuple[ArtifactReference, ...],
) -> tuple[ArtifactReference, ...]:
    return tuple(sorted(references, key=lambda item: (item.artifact_id, item.version, item.digest)))


# Reason codes and smallest changes name internal configuration, so they stay with the architect.
# A requester still deserves to know why their request closed, in words that reveal none of it.
_REQUESTER_SAFE_REFUSAL = (
    "Heinzel could not prepare a governed answer for this request with the workspace's "
    "current approved configuration."
)


def _failure(reason_code: str, smallest_change: str) -> ResolutionFailure:
    return ResolutionFailure(
        reason_codes=(reason_code,),
        constraint_refs=(),
        smallest_changes=(smallest_change,),
        requester_safe_explanation=_REQUESTER_SAFE_REFUSAL,
    )
